"""Container-side bridge between a SWE-bench image and NameRTS.

This file intentionally remains compatible with Python 3.6.
"""

import argparse
import json
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


# Only bin/test needs a local configuration edit: its runner overrides -W/env
# filters. Restore exact bytes after execution; never patch during selection.
SYMPY_WARNING_RUNNER = r'''
import pathlib, re, subprocess, sys
originals = {}
try:
    for name in ("sympy/utilities/runtests.py", "sympy/testing/runtests.py"):
        path = pathlib.Path(name)
        if not path.is_file():
            continue
        original = path.read_bytes()
        updated = re.sub(
            rb"(warnings\.(?:simplefilter|filterwarnings)\(\s*)(['\"])error\2",
            rb"\1'ignore'", original)
        if updated != original:
            originals[path] = original
            path.write_bytes(updated)
    status = subprocess.call(sys.argv[1:])
finally:
    for path, original in originals.items():
        path.write_bytes(original)
sys.exit(status)
'''


def rts_warning_command(command):
    """Use runner options, without modifying Python startup or warning APIs."""
    tokens = shlex.split(command)
    if "bin/test" in tokens:
        command = command.replace(
            "bin/test", "python -c " + shlex.quote(SYMPY_WARNING_RUNNER) + " bin/test", 1
        )
    elif any(token in ("pytest", "py.test", "tox") for token in tokens):
        command += " -W ignore"
    return command


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
        command = original_get_test_command(logical_path(path))
        return "env PYTHONWARNINGS=ignore " + rts_warning_command(command)

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
    if result.get("return_code") in (0, 1) or result.get("timed_out"):
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


def write_test_script(repo_path, tests, test_command_path, output_path, scope):
    """Prepare one official runner invocation; the host owns its timeout."""
    from src.config import get_test_target

    with open(test_command_path) as handle:
        command = handle.read().strip()
    if not command or "\n" in command:
        raise RuntimeError("Invalid SWE-bench test command")
    tests = sorted(set(tests))
    targets = [get_test_target(repo_path, item) for item in tests]
    script = [
        "#!/bin/bash",
        "set -e",
        "source /opt/miniconda3/bin/activate",
        "conda activate testbed",
        "cd " + shlex.quote(repo_path),
        "export PYTHONHASHSEED=0",
        "export PYTHONWARNINGS=ignore",
        "set +e",
        rts_warning_command(command) + " " + " ".join(shlex.quote(target) for target in targets)
        if tests
        else "true",
        "status=$?",
        "printf '\\n__NAMERTS_TEST_EXIT_CODE__=%s\\n' \"$status\"",
        'exit "$status"',
    ]
    with open(output_path, "w") as handle:
        handle.write("\n".join(script) + "\n")
    return {
        "completed": False,
        "execution_mode": "grouped",
        "scope": scope,
        "tests": tests,
        "test_count": len(tests),
        "command": command,
        "calculation_cache": "yes" if command.startswith("SYMPY_USE_CACHE=yes ") else "official",
    }


def command_prepare(args):
    preparation_started = time.time()
    config = configure_timeout(180)
    with open(args.instance_policy) as handle:
        policy = json.load(handle)
    persisted = set(policy.get("slow_test_files", []))
    skipped = set(policy.get("skip_test_files", {}))
    os.environ["NAMERTS_EXCLUDED_TEST_FILES"] = json.dumps(sorted(persisted | skipped))
    repo_path = project_path(args.repo_root, args.project_name)
    run_checked(["git", "reset", "--hard", args.parent], cwd=repo_path)
    cache_started = time.time()
    initialization = evaluate(args.parent, repo_path, None, 1, False)
    instrumentation = initialization.get("instrumentation_results", [])
    from src.utils import collect_all_heuristic

    discovered_parent = set(collect_all_heuristic(repo_path))
    expected = sorted(discovered_parent - persisted - skipped)
    if (
        not discovered_parent
        or sorted(item["test_file"] for item in instrumentation) != expected
    ):
        raise RuntimeError("Incomplete instrumentation file timings")
    slow = sorted(persisted | set(
        item["test_file"]
        for item in instrumentation
        if item.get("timed_out") or item["duration"] >= args.slow_test_threshold
    ))
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
    tests = sorted(collect_all_heuristic(repo_path))
    if not tests:
        raise RuntimeError("NameRTS discovered zero tests in {}".format(repo_path))
    golden_setup_duration = time.time() - golden_setup_started
    baseline = write_test_script(
        repo_path,
        sorted(set(tests) - set(slow) - skipped),
        args.test_command,
        args.runner_script,
        "golden_full_suite",
    )
    baseline["slow_test_threshold_seconds"] = args.slow_test_threshold
    baseline["slow_test_files"] = slow
    baseline["persisted_slow_test_files"] = sorted(persisted)
    baseline["instrumentation_discovered_tests"] = sorted(discovered_parent)
    baseline["instance_policy"] = policy
    baseline["skip_test_files"] = policy.get("skip_test_files", {})
    baseline["discovered_tests"] = tests
    baseline["instrumentation_results"] = [
        {
            key: item[key]
            for key in ("test_file", "duration", "return_code", "timed_out")
        }
        for item in instrumentation
    ]
    baseline["instrumentation_errors"] = [
        item for item in instrumentation
        if item["return_code"] in (2, 3, 4, 127) and not item["timed_out"]
    ]
    baseline["timing"] = {
        "cache_initialization_seconds": cache_duration,
        "golden_setup_seconds": golden_setup_duration,
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
    payload = write_test_script(
        repo_path,
        selection.get("tests_to_run", []),
        args.test_command,
        args.runner_script,
        "selected",
    )
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
    prepare.add_argument("--slow-test-threshold", type=float, default=180)
    prepare.add_argument("--instance-policy", required=True)
    prepare.add_argument("--runner-script", required=True)
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
    run_tests.add_argument("--runner-script", required=True)
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
