"""Container-side bridge between a SWE-bench image and NameRTS.

This file intentionally remains compatible with Python 3.6.
"""

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


CACHED_FILES = [
    "dependencies.json",
    "py_checksums_cache.json",
    "nbdp_cache.json",
    "critical_names.json",
]


def write_json(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.rename(temporary, path)


def run_checked(command, cwd=None):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(
            "Command failed ({}): {}\nstdout:\n{}\nstderr:\n{}".format(
                process.returncode, " ".join(command), stdout, stderr
            )
        )
    return stdout.strip()


def project_path(repo_root, project_name):
    if not os.path.isdir(repo_root):
        raise RuntimeError("Missing repository root: {}".format(repo_root))
    import src.config as config

    real_root = os.path.realpath(repo_root)
    logical_root = os.path.join("/opt/namerts/project-identities", project_name)
    os.environ["NAMERTS_PROJECT_NAME"] = project_name

    def logical_path(path):
        if path and os.path.realpath(path) == real_root:
            return logical_root
        return path

    original_get_test_command = config.get_test_command
    original_get_test_target = config.get_test_target

    def get_test_command(path):
        return original_get_test_command(logical_path(path))

    def get_test_target(path, test_file):
        return original_get_test_target(logical_path(path), test_file)

    config.get_test_command = get_test_command
    config.get_test_target = get_test_target
    physical_name = os.path.basename(os.path.normpath(repo_root))
    if project_name in config.INSTRUMENTATION_BLACKLIST:
        config.INSTRUMENTATION_BLACKLIST[physical_name] = set(
            config.INSTRUMENTATION_BLACKLIST[project_name]
        )
    return repo_root


def configure_timeout(per_file_timeout):
    import src.config as config

    config.PER_FILE_TEST_TIMEOUT = per_file_timeout
    if not os.path.isdir(config.CACHE_PATH):
        os.makedirs(config.CACHE_PATH)
    return config


def evaluate(parent, repo_path, patch_path, workers, reuse_parent_cache, current=None):
    from src.evaluate import CACHED_FILES_NBDP, evaluate_instance
    from src.namebdp import NameBDP

    return evaluate_instance(
        direct_parent=parent,
        true_parent=parent,
        current=current,
        target=True,
        tool_name="NameBDP",
        repo_path=repo_path,
        conda_env="testbed",
        tool_class=NameBDP,
        cached_files=CACHED_FILES_NBDP,
        time_tag="swebench_{}".format(int(time.time())),
        n=workers,
        use_isolation=False,
        run_parent=True,
        patch_path=patch_path,
        run_current_tests=False,
        reuse_parent_cache=reuse_parent_cache,
    )


def apply_patch(repo_path, patch_path):
    if os.path.getsize(patch_path) == 0:
        return
    run_checked(["git", "apply", "--check", patch_path], cwd=repo_path)
    run_checked(["git", "apply", patch_path], cwd=repo_path)


def make_analysis_patch(patch_path):
    with open(patch_path, "rb") as handle:
        lines = handle.readlines()
    sections = []
    current = []
    for line in lines:
        if line.startswith(b"diff --git ") and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    retained = []
    omitted = []
    for section in sections:
        placeholder = any(
            line.startswith(b"Binary files ") and line.rstrip().endswith(b" differ")
            for line in section
        )
        embedded_payload = any(line.rstrip() == b"GIT binary patch" for line in section)
        if placeholder and not embedded_payload:
            omitted.append(section[0].decode("utf-8", errors="replace").rstrip())
            continue
        retained.extend(section)
    if not omitted:
        return patch_path, omitted
    temporary = tempfile.NamedTemporaryFile(
        prefix="namerts-analysis-", suffix=".diff", delete=False
    )
    try:
        temporary.writelines(retained)
    finally:
        temporary.close()
    return temporary.name, omitted


def reset_for_patch(repo_path, patch_base, patch_path):
    run_checked(["git", "reset", "--hard", patch_base], cwd=repo_path)
    analysis_patch, ignored = make_analysis_patch(patch_path)
    try:
        summary = subprocess.check_output(
            ["git", "apply", "--summary", analysis_patch],
            cwd=repo_path,
            universal_newlines=True,
        )
    finally:
        if analysis_patch != patch_path and os.path.exists(analysis_patch):
            os.unlink(analysis_patch)
    for line in summary.splitlines():
        match = re.match(r"^\s*create mode \d+ (.+)$", line)
        if match is None:
            continue
        fields = shlex.split(match.group(1))
        if len(fields) != 1:
            raise RuntimeError("Cannot parse created patch path: {}".format(line))
        relative_path = fields[0]
        candidate = os.path.realpath(os.path.join(repo_path, relative_path))
        root = os.path.realpath(repo_path)
        if os.path.commonpath([root, candidate]) != root:
            raise RuntimeError(
                "Patch path escapes repository: {}".format(relative_path)
            )
        tracked = (
            subprocess.call(
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            == 0
        )
        if tracked or not os.path.lexists(candidate):
            continue
        if os.path.isdir(candidate) and not os.path.islink(candidate):
            shutil.rmtree(candidate)
        else:
            os.unlink(candidate)


def is_collection_failure(result):
    if result.get("return_code") == 0 or result.get("timed_out"):
        return False
    output = "\n".join(
        (str(result.get("stdout", "")), str(result.get("stderr", "")))
    ).lower()
    collection_markers = (
        "error collecting ",
        "error during collection",
        "errors during collection",
        "importerror while importing test module",
        "conftestimportfailure",
        "could not load conftest",
    )
    if any(marker in output for marker in collection_markers):
        return True
    return result.get("return_code") == 5 and (
        "no tests ran" in output or "no tests collected" in output
    )


def target_timeout_for_golden(result, default_timeout):
    """Give a completed golden file enough target-side runtime variance."""
    if result.get("timed_out"):
        return default_timeout
    try:
        duration = float(result.get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return max(default_timeout, int(math.ceil(duration * 3.0 + 60.0)))


def deterministic_test_command(test_command):
    """Pin runner-level randomness that is independent of Python's hash seed."""
    try:
        tokens = shlex.split(test_command)
    except ValueError:
        tokens = []
    if (
        any(token == "bin/test" or token.endswith("/bin/test") for token in tokens)
        and "--seed" not in tokens
    ):
        return test_command + " --seed 0"
    return test_command


def run_test_files(
    repo_path,
    tests,
    test_command_path,
    workers,
    per_file_timeout,
    scope,
    golden_results=None,
):
    from src.config import get_test_target
    from src.utils import run_test_files_parallel

    with open(test_command_path) as handle:
        test_command = handle.read().strip()
    if not test_command or "\n" in test_command:
        raise RuntimeError("Invalid SWE-bench test command")
    test_command = deterministic_test_command(test_command)
    tests = sorted(set(tests))
    targets = {item: get_test_target(repo_path, item) for item in tests}
    golden_by_file = {
        item.get("test_file"): item
        for item in (golden_results or [])
        if isinstance(item, dict) and item.get("test_file")
    }
    test_timeouts = {
        test_file: target_timeout_for_golden(
            golden_by_file.get(test_file, {}), per_file_timeout
        )
        for test_file in tests
    }
    started = time.time()
    results = run_test_files_parallel(
        repo_path,
        "testbed",
        test_command,
        tests,
        workers,
        timeout=per_file_timeout,
        test_targets=targets,
        test_timeouts=test_timeouts,
    )
    for result in results:
        result["collection_failed"] = is_collection_failure(result)
        if scope == "golden_full_suite":
            result["target_timeout_seconds"] = target_timeout_for_golden(
                result, per_file_timeout
            )
    return {
        "completed": True,
        "duration": time.time() - started,
        "execution_mode": "per_file",
        "test_process_isolation": "per_file_temp_root",
        "scope": scope,
        "tests": tests,
        "test_count": len(tests),
        "command": test_command,
        "workers": workers,
        "per_file_timeout_seconds": per_file_timeout,
        "collection_failed_files": sorted(
            item["test_file"] for item in results if item["collection_failed"]
        ),
        "results": results,
    }


def command_prepare(args):
    preparation_started = time.time()
    config = configure_timeout(args.per_file_timeout)
    repo_path = project_path(args.repo_root, args.project_name)
    run_checked(["git", "reset", "--hard", args.parent], cwd=repo_path)
    cache_started = time.time()
    evaluate(args.parent, repo_path, None, args.workers, False)
    cache_duration = time.time() - cache_started
    generated_cache = os.path.join(
        config.CACHE_PATH, "NameBDP_{}.tar.gz".format(args.parent)
    )
    if not os.path.isfile(generated_cache):
        raise RuntimeError("NameRTS did not create {}".format(generated_cache))
    shutil.copyfile(generated_cache, args.cache_output)
    golden_setup_started = time.time()
    run_checked(["git", "reset", "--hard", args.parent], cwd=repo_path)
    for cache_file in CACHED_FILES:
        path = os.path.join(repo_path, cache_file)
        if os.path.exists(path):
            os.unlink(path)
    apply_patch(repo_path, args.golden_patch)
    run_checked(["/bin/bash", args.install_script], cwd=repo_path)
    run_checked(["/bin/bash", args.test_script], cwd=repo_path)
    from src.utils import collect_all_heuristic

    tests = sorted(collect_all_heuristic(repo_path))
    if not tests:
        raise RuntimeError("NameRTS discovered zero tests in {}".format(repo_path))
    golden_setup_duration = time.time() - golden_setup_started
    baseline = run_test_files(
        repo_path,
        tests,
        args.test_command,
        args.workers,
        args.per_file_timeout,
        "golden_full_suite",
    )
    baseline["timing"] = {
        "cache_initialization_seconds": cache_duration,
        "golden_setup_seconds": golden_setup_duration,
        "golden_test_seconds": baseline["duration"],
        "container_preparation_seconds": time.time() - preparation_started,
    }
    write_json(args.baseline_output, baseline)


def command_select(args):
    config = configure_timeout(args.per_file_timeout)
    repo_path = project_path(args.repo_root, args.project_name)
    cache_destination = os.path.join(
        config.CACHE_PATH, "NameBDP_{}.tar.gz".format(args.parent)
    )
    shutil.copyfile(args.cache, cache_destination)
    reset_for_patch(repo_path, args.patch_base, args.patch)
    analysis_patch, omitted = make_analysis_patch(args.patch)
    try:
        selection = evaluate(
            args.parent,
            repo_path,
            analysis_patch,
            args.workers,
            True,
            current=args.patch_base,
        )
    finally:
        if analysis_patch != args.patch and os.path.exists(analysis_patch):
            os.unlink(analysis_patch)
    selection["completed"] = True
    selection["ignored_binary_diff_count"] = len(omitted)
    selection["ignored_binary_diff_headers"] = omitted
    write_json(args.output, selection)


def command_run_tests(args):
    configure_timeout(args.per_file_timeout)
    repo_path = project_path(args.repo_root, args.project_name)
    with open(args.selection) as handle:
        selection = json.load(handle)
    with open(args.baseline) as handle:
        baseline = json.load(handle)
    baseline_timeout = int(
        baseline.get("per_file_timeout_seconds", args.per_file_timeout)
    )
    effective_timeout = max(args.per_file_timeout, baseline_timeout)
    requested = sorted(set(selection.get("tests_to_run", [])))
    golden_collection_failures = set(baseline.get("collection_failed_files", []))
    golden_timeouts = {
        item.get("test_file")
        for item in baseline.get("results", [])
        if isinstance(item, dict) and item.get("timed_out")
    }
    collection_excluded = sorted(set(requested) & golden_collection_failures)
    timeout_excluded = sorted(set(requested) & golden_timeouts)
    incomplete = golden_collection_failures | golden_timeouts
    excluded = sorted(set(requested) & incomplete)
    tests = sorted(set(requested) - incomplete)
    payload = run_test_files(
        repo_path,
        tests,
        args.test_command,
        args.workers,
        effective_timeout,
        "selected",
        golden_results=baseline.get("results", []),
    )
    payload["requested_tests_before_baseline_filter"] = requested
    payload["golden_collection_excluded_tests"] = collection_excluded
    payload["golden_collection_excluded_count"] = len(collection_excluded)
    payload["golden_timeout_excluded_tests"] = timeout_excluded
    payload["golden_timeout_excluded_count"] = len(timeout_excluded)
    payload["golden_incomplete_excluded_tests"] = excluded
    payload["golden_incomplete_excluded_count"] = len(excluded)
    write_json(args.output, payload)


def common_arguments(parser):
    parser.add_argument("--repo-root", default="/testbed")
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per-file-timeout", type=int, default=120)


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    prepare = subparsers.add_parser("prepare")
    common_arguments(prepare)
    prepare.add_argument("--golden-patch", required=True)
    prepare.add_argument("--install-script", required=True)
    prepare.add_argument("--test-script", required=True)
    prepare.add_argument("--test-command", required=True)
    prepare.add_argument("--cache-output", required=True)
    prepare.add_argument("--baseline-output", required=True)
    prepare.set_defaults(handler=command_prepare)
    select = subparsers.add_parser("select")
    common_arguments(select)
    select.add_argument("--patch", required=True)
    select.add_argument("--patch-base", required=True)
    select.add_argument("--cache", required=True)
    select.add_argument("--output", required=True)
    select.set_defaults(handler=command_select)
    run_tests = subparsers.add_parser("run-tests")
    common_arguments(run_tests)
    run_tests.add_argument("--selection", required=True)
    run_tests.add_argument("--baseline", required=True)
    run_tests.add_argument("--test-command", required=True)
    run_tests.add_argument("--output", required=True)
    run_tests.set_defaults(handler=command_run_tests)
    args = parser.parse_args()
    if not hasattr(args, "handler"):
        parser.error("a command is required")
    return args


def main():
    args = parse_args()
    try:
        args.handler(args)
    except Exception as error:
        print("NameRTS bridge failed: {}".format(error), file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
