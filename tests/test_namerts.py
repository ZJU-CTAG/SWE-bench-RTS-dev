"""Core NameRTS harness integration tests; no submodule or Docker daemon required."""
import json
import os
import shlex
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import docker
import pytest

from swebench.harness import run_evaluation
from swebench.harness.namerts import container_bridge, prepare, runtime
from swebench.harness.namerts.artifacts import (
    ARTIFACT_SCHEMA_VERSION, GOLDEN_BASELINE_FILENAME, MANIFEST_FILENAME,
    PARENT_CACHE_FILENAME, RUNTIME_RECIPE_VERSION, NameRTSArtifact,
    NameRTSArtifactError, filter_selection_for_baseline, make_regression_report,
    normalize_test_results, sha256_file, sha256_text,
)
from swebench.harness.namerts.evaluation import (
    UNSUPPORTED_INSTANCE_IDS, run_regression_evaluation,
)
from swebench.harness.namerts.policy import read_policy, record_slow_tests
from swebench.harness.namerts.runtime import (
    NameRTSRuntimeError, make_install_script, make_test_script,
    make_harness_test_command_prefix, restore_test_environment,
)
from swebench.types import TestSpec as HarnessTestSpec

def make_spec() -> HarnessTestSpec:
    return HarnessTestSpec(
        instance_id="pytest-dev__pytest-1",
        image="swebench/example:latest",
        eval_script_list=[],
        repo="pytest-dev/pytest",
        version="1",
        FAIL_TO_PASS=[],
        PASS_TO_PASS=[],
        log_parser="parse_log_pytest",
        eval_type="pass_and_fail",
    )


def make_instance() -> dict:
    return {
        "instance_id": "pytest-dev__pytest-1",
        "repo": "pytest-dev/pytest",
        "base_commit": "abc123",
        "patch": "golden patch",
        "test_patch": "test patch",
        "language": "py",
    }


def write_artifact(root: Path, spec: HarnessTestSpec, instance: dict) -> Path:
    directory = root / spec.instance_id
    directory.mkdir()
    cache = directory / PARENT_CACHE_FILENAME
    cache.write_bytes(b"cache")
    baseline = directory / GOLDEN_BASELINE_FILENAME
    tests = ["testing/test_a.py"]
    baseline.write_text(json.dumps({
        **grouped_payload("PASSED testing/test_a.py::test_ok\n", tests=tests),
        "instance_policy": read_policy(spec.instance_id),
        "slow_test_files": [], "persisted_slow_test_files": [],
        "instrumentation_discovered_tests": tests, "discovered_tests": tests,
        "slow_test_threshold_seconds": 180,
        "instrumentation_results": [dict(test_file=tests[0], duration=1,
                                         return_code=0, timed_out=False)],
        "failure_ids": [],
    }))
    manifest = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "complete",
        "instance_id": spec.instance_id,
        "repo": spec.repo,
        "base_commit": instance["base_commit"],
        "language": "py",
        "dataset": {
            "eval_script_sha256": sha256_text(spec.eval_script),
            "log_parser": spec.log_parser,
            "eval_type": spec.eval_type,
        },
        "runtime": {
            "official_image": spec.image,
            "official_image_id": "sha256:official",
            "image": "sweb.namerts.test:abc",
            "image_id": "sha256:image",
            "namerts_env": "NameRTS39",
            "python_version": "3.9.20",
            "python_minor": "3.9",
            "project_name": "pytest",
            "repo_head": "setup-parent",
            "source_sha256": "source",
            "fingerprint": "fingerprint",
            "recipe_version": RUNTIME_RECIPE_VERSION,
        },
        "preparation": {
            "golden_patch_sha256": sha256_text(instance["patch"]),
            "test_patch_sha256": sha256_text(instance["test_patch"]),
            "test_execution": "grouped",
            "slow_test_threshold_seconds": 180,
            "slow_test_files": [],
            "harness_test_command_sha256": "0" * 64,
        },
        "files": {key: {"name": path.name, "sha256": sha256_file(path)}
                  for key, path in (("parent_cache", cache), ("golden_baseline", baseline))},
    }
    (directory / MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return directory


def test_make_install_script_reuses_editable_install_and_excludes_test_patch():
    base_commit = "abc123"
    eval_script = """#!/bin/bash
set -uxo pipefail
cd /testbed
python -m pip install .
git checkout abc123 tests/test_feature.py
git apply - <<'EOF'
test patch
EOF
: '>>>>> Start Test Output'
pytest tests/test_feature.py
"""

    install_script = make_install_script(eval_script, base_commit)

    assert install_script.endswith("python -m pip install .\n")
    assert "git checkout" not in install_script
    assert "git apply" not in install_script
    assert "pytest" not in install_script

    test_script = make_test_script(eval_script, base_commit)
    assert "git checkout abc123 tests/test_feature.py" in test_script
    assert "git apply - <<'EOF'" in test_script
    assert "test patch" in test_script
    assert "python -m pip install" not in test_script
    assert "pytest" not in test_script

    editable_script = make_install_script(
        eval_script.replace("python -m pip install .", "python -m pip install -e ."),
        base_commit,
    )
    assert "pip install -e" not in editable_script
    assert "reuses the official editable" in editable_script


@pytest.mark.parametrize(
    ("repo", "command", "path", "expected"),
    [
        (
            "astropy/astropy",
            "pytest -rA -vv --tb=no astropy/modeling/tests/test_separable.py",
            "astropy/modeling/tests/test_separable.py",
            "pytest -rA -vv --tb=no",
        ),
        (
            "django/django",
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite "
            "--parallel 1 auth_tests.test_forms",
            "tests/auth_tests/test_forms.py",
            "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
        ),
        (
            "sphinx-doc/sphinx",
            "tox --current-env -epy39 -v -- tests/test_extension.py",
            "tests/test_extension.py",
            "tox --current-env -epy39 -v --",
        ),
        (
            "sympy/sympy",
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            "bin/test -C --verbose sympy/core/tests/test_evalf.py",
            "sympy/core/tests/test_evalf.py",
            "SYMPY_USE_CACHE=yes PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            "bin/test --verbose",
        ),
    ],
)
def test_make_harness_test_command_prefix_keeps_official_runner(
    repo, command, path, expected
):
    eval_script = f"""#!/bin/bash
set -uxo pipefail
: '>>>>> Start Test Output'
{command}
SWEBENCH_TEST_EXIT_CODE=$?
: '>>>>> End Test Output'
"""
    test_patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-old
+new
"""

    assert make_harness_test_command_prefix(eval_script, test_patch, repo) == expected
    assert command in eval_script  # The upstream F2P/P2P script stays unchanged.


def grouped_payload(output="", return_code=0, tests=None):
    tests = sorted(tests if tests is not None else ["testing/test_a.py"])
    return {
        "completed": True,
        "execution_mode": "grouped",
        "tests": tests,
        "test_count": len(tests),
        "return_code": return_code,
        "stdout": output,
        "stderr": "",
        "timed_out": False,
    }


def test_grouped_failure_identity_is_independent_of_scope(tmp_path):
    output = "FAILED testing/test_a.py::test_old\n"
    golden = normalize_test_results(
        make_spec(),
        grouped_payload(output, 1, ["testing/test_a.py", "testing/test_b.py"]),
    )
    artifact = NameRTSArtifact(
        tmp_path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runtime": {"image": "sweb.namerts.test", "source_sha256": "source"},
        },
        golden,
    )
    report = make_regression_report(
        make_spec(),
        artifact,
        {},
        grouped_payload(output + "FAILED testing/test_a.py::test_new\n", 1),
    )
    assert report["golden_failure_ids_seen"] == ["case::testing/test_a.py::test_old"]
    assert report["new_failure_ids"] == ["case::testing/test_a.py::test_new"]
    assert not report["passed"]
    same = make_regression_report(make_spec(), artifact, {}, grouped_payload(output, 1))
    assert same["passed"]
    assert same["target_summary"]["process_count"] == 1


@pytest.mark.parametrize(
    "output,code",
    [
        ("ERROR collecting testing/test_a.py", 2),
        ("FAILED testing/test_a.py::test_before_kill", 137),
        ("", 0),
    ],
)
def test_grouped_incomplete_results_never_pass(output, code):
    with pytest.raises(NameRTSArtifactError):
        normalize_test_results(make_spec(), grouped_payload(output, code))


def test_empty_regression_scope_does_not_collect_everything():
    result = normalize_test_results(make_spec(), grouped_payload(tests=[]))
    assert result["failure_ids"] == []
    assert result["process_count"] == 0


def test_grouped_script_uses_one_official_invocation(tmp_path, monkeypatch):
    from swebench.harness.namerts.container_bridge import write_test_script

    config = types.ModuleType("src.config")
    config.get_test_target = lambda root, path: path
    filters = ["--ignore", "tests/network cases.py"]
    config.get_test_filter_args = lambda root: filters
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.config", config)
    command = tmp_path / "command"
    command.write_text("tox --current-env -e py -- -rA")
    script = tmp_path / "run.sh"
    for scope in ("golden_full_suite", "selected"):
        payload = write_test_script(
            "/testbed", ["tests/b.py", "tests/a.py"], str(command), str(script), scope
        )
        text = script.read_text()
        assert text.count("tox --current-env") == 1
        runner = next(line for line in text.splitlines() if line.startswith("tox "))
        assert shlex.split(runner) == shlex.split(command.read_text()) + filters + [
            "-W", "ignore", "tests/a.py", "tests/b.py"]
        assert "TOX_WORK_DIR" not in text
        assert "export PYTHONIOENCODING=utf-8" in text
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            assert "export " + variable + "=1" in text
        assert payload["test_count"] == 2
        subprocess.run(["bash", "-n", str(script)], check=True)
    write_test_script("/testbed", [], str(command), str(script), "selected")
    assert "tox " not in script.read_text()
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_grouped_executor_uses_upstream_timeout_without_retry(tmp_path, monkeypatch):
    from swebench.harness.namerts import runtime

    calls = []
    monkeypatch.setattr(runtime, "copy_from_container", lambda *args: None)

    def execute(container, command, timeout):
        calls.append(timeout)
        return "partial output", True, 3600

    monkeypatch.setattr(runtime, "exec_run_with_timeout", execute)
    with pytest.raises(NameRTSRuntimeError, match="3600"):
        runtime.run_grouped_tests(object(), grouped_payload(), tmp_path, 3600)
    assert calls == [3600]
    assert (tmp_path / "namerts_test.log").read_text() == "partial output"


@pytest.mark.parametrize("persisted,skipped", [
    ([], {}), (["tests/fast.py"], {"tests/boundary.py": "network-only file"}),
])
def test_preparation_instruments_serially_with_file_deadline(tmp_path, monkeypatch, persisted, skipped):
    from swebench.harness.namerts import container_bridge as bridge

    files = ["tests/fast.py", "tests/boundary.py", "tests/slow.py"]
    config = types.SimpleNamespace(CACHE_PATH=str(tmp_path))
    (tmp_path / "NameBDP_parent.tar.gz").write_bytes(b"cache")
    monkeypatch.setattr(
        bridge,
        "configure_timeout",
        lambda timeout: (
            config
            if timeout == 180
            else pytest.fail("Instrumentation must have a 180-second deadline")
        ),
    )
    monkeypatch.setattr(bridge, "project_path", lambda *args: str(tmp_path))
    monkeypatch.setattr(bridge, "run_checked", lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge, "apply_patch", lambda *args: None)

    def evaluate(parent, root, patch, workers, reuse):
        assert workers == 1
        assert not reuse
        assert set(json.loads(os.environ["NAMERTS_EXCLUDED_TEST_FILES"])) == set(persisted) | set(skipped)
        return {
            "instrumentation_results": [
                {
                    "test_file": name,
                    "duration": duration,
                    "return_code": -1 if name == "tests/slow.py" else 0,
                    "timed_out": name == "tests/slow.py",
                }
                for name, duration in zip(files, [1, 179, 180])
                if name not in persisted and name not in skipped
            ]
        }

    monkeypatch.setattr(bridge, "evaluate", evaluate)
    utils = types.ModuleType("src.utils")
    utils.collect_all_heuristic = lambda root: files
    config_module = types.ModuleType("src.config")
    config_module.get_test_target = lambda root, path: path
    config_module.get_test_filter_args = lambda root: []
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.utils", utils)
    monkeypatch.setitem(sys.modules, "src.config", config_module)
    command = tmp_path / "command"
    command.write_text("pytest -rA")
    output = tmp_path / "baseline.json"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"slow_test_files": persisted, "skip_test_files": skipped}))
    bridge.command_prepare(
        types.SimpleNamespace(
            repo_root=str(tmp_path),
            project_name="pytest",
            parent="parent",
            workers=12,
            per_file_timeout=120,
            slow_test_threshold=180,
            instance_policy=str(policy),
            cache_output=str(tmp_path / "cache.tar.gz"),
            golden_patch="gold",
            install_script="install",
            test_script="setup",
            test_command=str(command),
            runner_script=str(tmp_path / "runner.sh"),
            baseline_output=str(output),
        )
    )
    baseline = json.loads(output.read_text())
    assert baseline["slow_test_files"] == sorted(persisted + ["tests/slow.py"])
    assert baseline["tests"] == sorted(set(["tests/boundary.py", "tests/fast.py"]) - set(persisted) - set(skipped))
    assert len(baseline["instrumentation_results"]) == 3 - len(persisted) - len(skipped)
    selected = filter_selection_for_baseline({"tests_to_run": files}, baseline)
    assert selected["tests_to_run"] == baseline["tests"]
    monkeypatch.delenv("NAMERTS_EXCLUDED_TEST_FILES")
    assert baseline["completed"] is False  # Only host runner completion can set this.


@pytest.mark.parametrize("enabled,override,expected", [
    (False, None, None), (True, None, 3600), (True, 7200, 7200),
])
def test_rts_switch_and_timeout(tmp_path, monkeypatch, enabled, override, expected):
    spec = make_spec()
    report = {spec.instance_id: {"resolved": True}}
    directory = tmp_path / "run" / "model" / spec.instance_id
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(json.dumps(report))
    monkeypatch.setattr(run_evaluation, "RUN_EVALUATION_LOG_DIR", tmp_path)
    add_result = Mock(return_value=report)
    monkeypatch.setattr(run_evaluation, "_add_namerts_result", add_result)
    assert run_evaluation.run_instance(
        spec, {"model_name_or_path": "model"}, None, "run", timeout=3600,
        instance=make_instance(), namerts_artifact_root="artifacts" if enabled else None,
        namerts_timeout=override,
    ) == (spec.instance_id, report)
    if enabled:
        add_result.assert_called_once()
        assert add_result.call_args.args[-2:] == (expected, 4)
    else:
        add_result.assert_not_called()


@pytest.mark.parametrize("upstream_passed,regression_passed", [
    (False, True), (True, True), (True, False),
])
def test_final_verdict_and_upstream_gate(monkeypatch, upstream_passed, regression_passed):
    regression = Mock(return_value={"completed": True, "passed": regression_passed})
    monkeypatch.setattr(run_evaluation, "run_regression_evaluation", regression)
    spec = make_spec()
    report = {spec.instance_id: {"resolved": upstream_passed}}
    updated = run_evaluation._add_namerts_result(
        report, spec, make_instance(), {"model_patch": "patch"}, None,
        "run", Path("."), None, "artifacts", 4, 900, 7200,
    )[spec.instance_id]
    assert updated["resolved"] is (upstream_passed and regression_passed)
    assert regression.call_count == int(upstream_passed)
    if not upstream_passed:
        assert updated["namerts_regression"]["skipped"] is True


@pytest.mark.parametrize("conflict", [False, True])
def test_restore_official_setup_preserves_candidate(tmp_path, monkeypatch, conflict):
    from swebench.harness.namerts import runtime

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "tox.ini").write_text("commands = pytest\n")
    (repo / "code.py").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (repo / "tox.ini").write_text("commands = pytest -rA\n")
    git("commit", "-am", "official setup")
    parent = git("rev-parse", "HEAD")
    git("reset", "--hard", base)
    (repo / "code.py").write_text("candidate\n")
    (repo / "new_file.py").write_text("candidate added file\n")
    if conflict:
        (repo / "tox.ini").write_text("commands = custom-runner\n")
    monkeypatch.setattr(runtime, "CONTAINER_WORKDIR", str(repo))

    def run(_container, command, _timeout):
        command = command.replace(
            "/tmp/namerts-official-setup.patch", str(tmp_path / "setup.patch")
        )
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        if result.returncode:
            raise NameRTSRuntimeError(result.stderr)
        return result.stdout, 0

    monkeypatch.setattr(runtime, "_run_bridge", run)
    artifact = NameRTSArtifact(tmp_path, {"runtime": {"repo_head": parent}}, {})
    if conflict:
        with pytest.raises(NameRTSRuntimeError):
            restore_test_environment(None, artifact, base, tmp_path, 60)
        assert (repo / "tox.ini").read_text() == "commands = custom-runner\n"
    else:
        restore_test_environment(None, artifact, base, tmp_path, 60)
        assert (repo / "tox.ini").read_text() == "commands = pytest -rA\n"
    assert (repo / "code.py").read_text() == "candidate\n"
    assert (repo / "new_file.py").read_text() == "candidate added file\n"


def test_incompatible_instances_passthrough_without_container():
    instance_id = next(iter(UNSUPPORTED_INSTANCE_IDS))
    spec = make_spec()
    spec.instance_id = instance_id
    report = run_regression_evaluation(
        test_spec=spec,
        instance=make_instance(),
        prediction={"model_patch": ""},
        client=None,
        container_factory=lambda _spec: pytest.fail("container created"),
        artifact_root="missing",
        log_dir=Path("."),
        logger=None,
        workers=1,
        per_file_timeout=1,
        timeout=1,
    )
    assert report["passed"] is True
    assert report["skipped"] is True


def args(monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", ["prepare", "--dataset_name", "dataset.json", *extra])
    return prepare.parse_args()


@pytest.mark.parametrize("filtered", [False, True])
def test_prepare_scope_and_unsupported_skip(tmp_path, monkeypatch, filtered, capsys):
    rows = [make_instance(), {**make_instance(), "instance_id": "django__django-7530"},
            {**make_instance(), "instance_id": "pytest-dev__pytest-2"},
            {**make_instance(), "instance_id": "pytest-dev__pytest-3"}]
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(rows))
    selected = ["--instance_ids", rows[0]["instance_id"], rows[2]["instance_id"]] if filtered else []
    options = args(monkeypatch, "--dataset_name", str(dataset),
                   "--slow_test_threshold", "150", *selected)
    assert options.instance_ids == ([rows[0]["instance_id"], rows[2]["instance_id"]] if filtered else None)
    if filtered:
        with pytest.raises(SystemExit):
            args(monkeypatch, "--instance_ids")
    client = Mock()
    monkeypatch.setattr(prepare.docker, "from_env", lambda: client)
    initialize = Mock(return_value=tmp_path)
    monkeypatch.setattr(prepare, "prepare_instance", initialize)
    prepare.main(**vars(options))
    expected = [rows[0], rows[2]] if filtered else [rows[0], rows[2], rows[3]]
    assert [call.args[1] for call in initialize.call_args_list] == expected
    assert all(call.kwargs["slow_test_threshold"] == 150 for call in initialize.call_args_list)
    assert ("official evaluation only" in capsys.readouterr().out) is not filtered
    client.close.assert_called_once()


@pytest.mark.parametrize("mode", ["reuse", "corrupt", "changed_image", "missing_image", "force"])
def test_prepare_existing_artifact(tmp_path, monkeypatch, mode):
    instance, spec = make_instance(), make_spec()
    directory = write_artifact(tmp_path, spec, instance)
    source = tmp_path / "NameRTS"
    (source / "src").mkdir(parents=True)
    (source / "src/namebdp.py").touch()
    monkeypatch.setattr(prepare, "make_test_spec", lambda _: spec)
    artifact = prepare.load_artifact(tmp_path, spec, instance)
    runtime = artifact.manifest["runtime"]
    client = Mock()
    images = {runtime["official_image"]: Mock(id=runtime["official_image_id"]),
              runtime["image"]: Mock(id=runtime["image_id"])}
    client.images.get.side_effect = images.__getitem__
    build = Mock(side_effect=RuntimeError("fresh preparation reached"))
    monkeypatch.setattr(prepare, "_ensure_official_image", build)
    if mode == "corrupt":
        artifact.cache_path.write_bytes(b"corrupt")
    elif mode == "changed_image":
        images[runtime["image"]].id = "different"
    elif mode == "missing_image":
        client.images.get.side_effect = docker.errors.ImageNotFound("missing")
    if mode == "reuse":
        assert prepare.prepare_instance(client, instance, tmp_path, source) == directory
    else:
        expected = {"corrupt": ValueError, "changed_image": ValueError,
                    "missing_image": docker.errors.ImageNotFound, "force": RuntimeError}[mode]
        with pytest.raises(expected):
            prepare.prepare_instance(client, instance, tmp_path, source, force_rebuild=mode == "force")
    assert build.call_count == int(mode == "force")


@pytest.mark.parametrize("workers", [1, 4])
def test_bridge_sets_independent_selection_workers(monkeypatch, workers):
    config = SimpleNamespace(CACHE_PATH="/cache", SELECT_WORKERS=4)
    monkeypatch.setattr(container_bridge, "configure_timeout", lambda value: config)
    monkeypatch.setattr(container_bridge, "project_path", lambda *args: "/testbed")
    monkeypatch.setattr(container_bridge.shutil, "copyfile", lambda *args: None)
    monkeypatch.setattr(container_bridge, "reset_for_patch", lambda *args: None)
    monkeypatch.setattr(container_bridge, "make_analysis_patch", lambda p: (p, []))
    monkeypatch.setattr(container_bridge, "evaluate", lambda *args, **kwargs: {})
    result = {}
    monkeypatch.setattr(container_bridge, "write_json", lambda path, value: result.update(value))
    container_bridge.command_select(SimpleNamespace(
        selection_workers=workers, per_file_timeout=180, repo_root="/testbed",
        project_name="django", parent="base", cache="cache", patch="patch",
        patch_base="base", workers=1, output="out"))
    assert config.SELECT_WORKERS == workers
    assert config.SELECT_USE_PARALLEL == (workers > 1)
    assert result["selection_workers"] == workers


def test_selection_rejects_invalid_workers():
    with pytest.raises(ValueError, match="positive"):
        runtime.run_selection(None, None, None, patch_base="base", workers=1,
                              per_file_timeout=180, timeout=3600, selection_workers=0)


def test_policy_atomic_union_preserves_pins(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 1, "instances": {
        "case": {"pytest_version": "4.6.11", "slow_test_files": ["tests/old.py"]}
    }}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: record_slow_tests("case", [f"tests/{n}.py"], path), range(20)))
    result = read_policy("case", path)
    assert result["pytest_version"] == "4.6.11"
    assert len(result["slow_test_files"]) == 21
    record_slow_tests("other", ["tests/other.py"], path)
    assert read_policy("case", path) == result
