import ast
import importlib.util
import json
import logging
import os
import subprocess
import sys
import types

from pathlib import Path

import pytest

from swebench.harness.namerts.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    GOLDEN_BASELINE_FILENAME,
    MANIFEST_FILENAME,
    PARENT_CACHE_FILENAME,
    RUNTIME_RECIPE_VERSION,
    NameRTSArtifact,
    NameRTSArtifactError,
    filter_selection_for_baseline,
    load_artifact,
    make_error_report,
    make_regression_report,
    normalize_parsed_test_id,
    normalize_test_results,
    sha256_file,
    sha256_text,
)
from swebench.harness.namerts.container_bridge import (
    is_collection_failure,
)
from swebench.harness.namerts.evaluation import (
    UNSUPPORTED_INSTANCE_IDS,
    reusable_regression_report,
    run_regression_evaluation,
    skipped_upstream_report,
    terminal_timeout_report,
)
from swebench.harness.namerts.prepare import (
    _remove_existing_instance_containers,
    _remove_superseded_runtime_images,
    _runtime_image_name,
)
from swebench.harness.namerts.policy import read_policy
from swebench.harness.namerts.runtime import (
    NameRTSRuntimeError,
    install_runtime_bridge,
    make_harness_test_command_prefix,
    make_install_script,
    make_test_script,
    restore_test_environment,
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
    baseline.write_text(
        json.dumps(
            {
                "completed": True,
                "instance_policy": read_policy(spec.instance_id),
                "execution_mode": "grouped",
                "return_code": 0,
                "stdout": "",
                "timed_out": False,
                "slow_test_files": ["testing/test_slow.py"],
                "persisted_slow_test_files": [],
                "instrumentation_discovered_tests": ["testing/test_slow.py"],
                "slow_test_threshold_seconds": 300,
                "discovered_tests": ["testing/test_slow.py"],
                "instrumentation_results": [
                    {
                        "test_file": "testing/test_slow.py",
                        "duration": 301,
                        "return_code": 0,
                        "timed_out": False,
                    }
                ],
                "tests": [],
                "test_count": 0,
                "results": [],
                "collection_failed_files": [],
                "failure_ids": [],
            }
        )
    )
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
            "slow_test_threshold_seconds": 300,
            "slow_test_files": ["testing/test_slow.py"],
            "harness_test_command_sha256": "0" * 64,
        },
        "files": {
            "parent_cache": {"name": cache.name, "sha256": sha256_file(cache)},
            "golden_baseline": {
                "name": baseline.name,
                "sha256": sha256_file(baseline),
            },
        },
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


def test_make_harness_test_command_prefix_rejects_extra_command_after_target():
    eval_script = """#!/bin/bash
set -uxo pipefail
: '>>>>> Start Test Output'
pytest -rA tests/test_feature.py && echo unexpected
: '>>>>> End Test Output'
"""
    test_patch = """diff --git a/tests/test_feature.py b/tests/test_feature.py
--- a/tests/test_feature.py
+++ b/tests/test_feature.py
@@ -1 +1 @@
-old
+new
"""

    with pytest.raises(NameRTSRuntimeError, match="unsupported tokens"):
        make_harness_test_command_prefix(eval_script, test_patch, "pytest-dev/pytest")


def test_load_artifact_validates_hashes_and_dataset_provenance(tmp_path):
    spec = make_spec()
    instance = make_instance()
    directory = write_artifact(tmp_path, spec, instance)

    artifact = load_artifact(tmp_path, spec, instance)
    assert artifact.runtime_image == "sweb.namerts.test:abc"
    assert artifact.repo_head == "setup-parent"

    (directory / PARENT_CACHE_FILENAME).write_bytes(b"tampered")
    with pytest.raises(NameRTSArtifactError, match="Checksum mismatch"):
        load_artifact(tmp_path, spec, instance)


def test_load_artifact_rejects_changed_golden_patch(tmp_path):
    spec = make_spec()
    instance = make_instance()
    write_artifact(tmp_path, spec, instance)
    instance["patch"] = "changed"

    with pytest.raises(NameRTSArtifactError, match="patch provenance"):
        load_artifact(tmp_path, spec, instance)


def test_load_artifact_rejects_changed_execution_provenance(tmp_path):
    spec = make_spec()
    instance = make_instance()
    write_artifact(tmp_path, spec, instance)

    spec.log_parser = "parse_log_pytest_v2"
    with pytest.raises(NameRTSArtifactError, match="execution provenance"):
        load_artifact(tmp_path, spec, instance)


def test_load_artifact_rejects_changed_official_image(tmp_path):
    spec = make_spec()
    instance = make_instance()
    write_artifact(tmp_path, spec, instance)

    spec.image = "swebench/different:latest"
    with pytest.raises(NameRTSArtifactError, match="runtime provenance"):
        load_artifact(tmp_path, spec, instance)


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
        ("internal error", 3),
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


def test_slow_filter_only_changes_regression_selection():
    selection = {"tests_to_run": ["testing/test_a.py", "testing/test_slow.py"]}
    baseline = {"slow_test_files": ["testing/test_slow.py"]}
    filtered = filter_selection_for_baseline(selection, baseline)
    assert filtered["tests_to_run"] == ["testing/test_a.py"]
    assert filtered["slow_excluded_tests"] == ["testing/test_slow.py"]
    assert selection["tests_to_run"] == ["testing/test_a.py", "testing/test_slow.py"]


def test_collection_detection():
    assert is_collection_failure(
        {"return_code": 2, "stdout": "ERROR collecting tests/a.py"}
    )
    assert not is_collection_failure(
        {"return_code": 1, "stdout": "FAILED tests/a.py::test_a"}
    )
    assert not is_collection_failure(
        {"return_code": 1, "stdout": "captured: ERROR collecting inner.py\n981 failed, 787 passed"}
    )


def test_parsed_failure_identity_removes_ephemeral_values():
    assert normalize_parsed_test_id("/tmp/a/x 0x1234") == normalize_parsed_test_id(
        "/tmp/b/y 0x5678"
    )


def test_grouped_script_uses_one_official_invocation(tmp_path, monkeypatch):
    from swebench.harness.namerts.container_bridge import write_test_script

    config = types.ModuleType("src.config")
    config.get_test_target = lambda root, path: path
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.config", config)
    command = tmp_path / "command"
    command.write_text("tox --current-env -e py -- -rA")
    script = tmp_path / "run.sh"
    payload = write_test_script(
        "/testbed", ["tests/b.py", "tests/a.py"], str(command), str(script), "selected"
    )
    text = script.read_text()
    assert text.count("tox --current-env") == 1
    assert "tox --current-env -e py -- -rA -W ignore tests/a.py tests/b.py" in text
    assert "TOX_WORK_DIR" not in text
    assert payload["test_count"] == 2
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


@pytest.mark.parametrize("persisted", [[], ["tests/fast.py"]])
def test_preparation_instruments_serially_with_file_deadline(tmp_path, monkeypatch, persisted):
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
        return {
            "instrumentation_results": [
                {
                    "test_file": name,
                    "duration": duration,
                    "return_code": -1 if name == "tests/slow.py" else 0,
                    "timed_out": name == "tests/slow.py",
                }
                for name, duration in zip(files, [1, 179, 180])
                if name not in persisted
            ]
        }

    monkeypatch.setattr(bridge, "evaluate", evaluate)
    utils = types.ModuleType("src.utils")
    utils.collect_all_heuristic = lambda root: files
    config_module = types.ModuleType("src.config")
    config_module.get_test_target = lambda root, path: path
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.utils", utils)
    monkeypatch.setitem(sys.modules, "src.config", config_module)
    command = tmp_path / "command"
    command.write_text("pytest -rA")
    output = tmp_path / "baseline.json"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"slow_test_files": persisted}))
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
    assert baseline["tests"] == sorted(set(["tests/boundary.py", "tests/fast.py"]) - set(persisted))
    assert len(baseline["instrumentation_results"]) == 3 - len(persisted)
    monkeypatch.delenv("NAMERTS_EXCLUDED_TEST_FILES")
    assert baseline["completed"] is False  # Only host runner completion can set this.


def test_artifact_accepts_excluded_instrumentation_timeout(tmp_path):
    spec, instance = make_spec(), make_instance()
    directory = write_artifact(tmp_path, spec, instance)
    path = directory / GOLDEN_BASELINE_FILENAME
    baseline = json.loads(path.read_text())
    baseline["instrumentation_results"][0].update(
        duration=300, timed_out=True, return_code=-1
    )
    path.write_text(json.dumps(baseline))
    manifest_path = directory / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["golden_baseline"]["sha256"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest))
    load_artifact(tmp_path, spec, instance)


def test_artifact_rejects_falsified_slow_summary(tmp_path):
    spec, instance = make_spec(), make_instance()
    directory = write_artifact(tmp_path, spec, instance)
    path = directory / GOLDEN_BASELINE_FILENAME
    baseline = json.loads(path.read_text())
    baseline["slow_test_files"] = []
    path.write_text(json.dumps(baseline))
    manifest_path = directory / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["golden_baseline"]["sha256"] = sha256_file(path)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(NameRTSArtifactError, match="Slow-file summary"):
        load_artifact(tmp_path, spec, instance)


@pytest.mark.parametrize("opname", ["LOAD_METHOD", "LOAD_ATTR"])
@pytest.mark.parametrize(
    "receiver, expected",
    [("p", (None, None, "distance")),
     ("self", (None, "distance", None)),
     ("cls", (None, "distance", None)),
     ("sympy", (None, None, "?distance"))],
)
def test_namerts_method_load_is_not_a_plain_name(opname, receiver, expected):
    from collections.abc import Collection
    from typing import Optional

    source = Path(__file__).parents[1] / "NameRTS/src/bytecode.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "get_used_names_instr")
    namespace = {
        "dis": types.SimpleNamespace(Instruction=object),
        "opcode": types.SimpleNamespace(hasname={1}),
        "Optional": Optional, "Collection": Collection, "ROOT_PACKAGE": "sympy",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    classify = namespace["get_used_names_instr"]
    previous = types.SimpleNamespace(argval=receiver)
    instruction = types.SimpleNamespace(opcode=1, opname=opname, argval="distance")
    assert classify(instruction, previous) == expected
    instruction.opname = "LOAD_GLOBAL"
    assert classify(instruction, previous) == ("distance", None, None)


def test_namebdp_forwards_inner_instrumentation_results():
    source = Path(__file__).parents[1] / "NameRTS/src/namebdp.py"
    tree = ast.parse(source.read_text())
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NameBDP"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_and_update"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    results = [{"test_file": "tests/test_a.py", "duration": 400}]
    inner = types.SimpleNamespace(
        test_results=results, run_and_update=lambda *args: None
    )
    outer = types.SimpleNamespace(
        ekstazip=inner,
        instrument_monitored_functions=lambda: ({}, {}),
        file_recover=lambda backups: None,
    )
    namespace["run_and_update"](outer, ["tests/test_a.py"])
    assert outer.test_results is results


@pytest.mark.parametrize("quotes", [[], ["'"], ['"'], ["'", '"']])
def test_introspection_reference_avoids_enclosing_fstring_quotes(quotes):
    source = Path(__file__).parents[1] / "NameRTS/src/namebdp.py"
    node = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "_instrumentor_reference")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    parent = None
    for i, quote in enumerate(quotes):
        child = types.SimpleNamespace(type="string_start", start_byte=i, end_byte=i+1)
        parent = types.SimpleNamespace(type="string", children=[child], parent=parent)
    reference = namespace["_instrumentor_reference"](
        types.SimpleNamespace(parent=parent), "".join(quotes).encode()).decode()
    for quote in quotes:
        assert quote not in reference
    code = reference + "capture_getattr_name('name')"
    assert compile(code, "<reference>", "eval")


def test_function_monitor_does_not_inject_local_import():
    source = Path(__file__).parents[1] / "NameRTS/src/namebdp.py"
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NameBDP")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "instrument_monitored_functions")
    # The monitored body must not bind an extra local: functions can forward
    # locals() as constructor kwargs (Astropy Parameter.copy does exactly this).
    text = ast.get_source_segment(source.read_text(), method)
    assert 'leading_ws + "import nbdp_instrumentor' not in text
    assert 'added_lines[file_path][line] += 6' in text


def test_namerts_filter_excludes_non_runnable_test_directories(tmp_path, monkeypatch):
    package = types.ModuleType("src")
    package.__path__ = []
    logger_module = types.ModuleType("src.instance_logger")
    logger_module.InstanceLogger = object
    monkeypatch.setitem(sys.modules, "src", package)
    monkeypatch.setitem(sys.modules, "src.instance_logger", logger_module)
    monkeypatch.setitem(sys.modules, "networkx", types.ModuleType("networkx"))
    monkeypatch.setitem(sys.modules, "pandas", types.ModuleType("pandas"))
    source = Path(__file__).parents[1] / "NameRTS/src/utils.py"
    spec = importlib.util.spec_from_file_location("namerts_utils_for_test", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    django = tmp_path / "django"
    monkeypatch.setenv("NAMERTS_PROJECT_NAME", "django")
    assert not module.is_test_file_pytest(
        django / "tests/gis_tests/test_geo.py", django
    )
    assert module.is_test_file_pytest(django / "tests/auth_tests/test_forms.py", django)

    pytest_root = tmp_path / "pytest"
    monkeypatch.setenv("NAMERTS_PROJECT_NAME", "pytest")
    for directory in ("example", "examples", "example_scripts", "fixtures"):
        assert not module.is_test_file_pytest(
            pytest_root / "testing" / directory / "test_sample.py", pytest_root
        )
    assert module.is_test_file_pytest(
        pytest_root / "testing/test_collection.py", pytest_root
    )

    sympy_root = tmp_path / "sympy"
    monkeypatch.setenv("NAMERTS_PROJECT_NAME", "sympy")
    for relative in (
        "sympy/integrals/rubi/tests/test_utility_function.py",
        "sympy/integrals/rubi/rubi_tests/tests/test_sine.py",
        "sympy/utilities/tests/test_pytest.py",
    ):
        assert not module.is_test_file_pytest(sympy_root / relative, sympy_root)
        assert not module.is_test_file_pytest(Path(relative), sympy_root)
    for relative in (
        "sympy/integrals/tests/test_integrals.py",
        "sympy/integrals/rubi_extra/tests/test_rules.py",
        "tests/rubi/test_rules.py",
        "sympy/utilities/tests/test_pytest_extra.py",
    ):
        assert module.is_test_file_pytest(sympy_root / relative, sympy_root)
    monkeypatch.setenv("NAMERTS_PROJECT_NAME", "other")
    assert module.is_test_file_pytest(
        sympy_root / "sympy/integrals/rubi/tests/test_rules.py", sympy_root
    )
    assert module.is_test_file_pytest(
        sympy_root / "sympy/utilities/tests/test_pytest.py", sympy_root
    )


def test_namerts_parallel_runner_isolates_test_process_temp_roots(
    tmp_path, monkeypatch
):
    package = types.ModuleType("src")
    package.__path__ = []
    logger_module = types.ModuleType("src.instance_logger")

    class Logger:
        @staticmethod
        def get_logger():
            return logging.getLogger("test-namerts-temp-isolation")

    logger_module.InstanceLogger = Logger
    monkeypatch.setitem(sys.modules, "src", package)
    monkeypatch.setitem(sys.modules, "src.instance_logger", logger_module)
    monkeypatch.setitem(sys.modules, "networkx", types.ModuleType("networkx"))
    monkeypatch.setitem(sys.modules, "pandas", types.ModuleType("pandas"))
    source = Path(__file__).parents[1] / "NameRTS/src/utils.py"
    spec = importlib.util.spec_from_file_location(
        "namerts_utils_temp_isolation_test", source
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    seen_environments = []

    class Process:
        returncode = 0
        pid = 12345

        def __init__(self, *_args, **kwargs):
            seen_environments.append(kwargs["env"])

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(module, "resolve_conda_python", lambda _name: sys.executable)
    monkeypatch.setattr(module.subprocess, "Popen", Process)

    results = module.run_test_files_parallel(
        str(tmp_path),
        "testbed",
        "pytest -rA",
        ["tests/test_a.py", "tests/test_b.py"],
        workers=2,
        timeout=120,
    )

    assert len(results) == 2
    assert len(seen_environments) == 2
    temp_roots = {environment["TMPDIR"] for environment in seen_environments}
    assert len(temp_roots) == 2
    for environment in seen_environments:
        assert environment["TMP"] == environment["TMPDIR"]
        assert environment["TEMP"] == environment["TMPDIR"]
        assert environment["PYTEST_DEBUG_TEMPROOT"] == environment["TMPDIR"]
        assert environment["MPLCONFIGDIR"] == environment["TMPDIR"]
        assert not os.path.exists(environment["TMPDIR"])

    seen_environments.clear()
    monkeypatch.setenv("NAMERTS_EXCLUDED_TEST_FILES", '["tests/test_a.py"]')
    results = module.run_test_files_parallel(
        str(tmp_path), "testbed", "pytest", ["tests/test_a.py", "tests/test_b.py"],
        workers=1, timeout=180,
    )
    assert [r["test_file"] for r in results] == ["tests/test_b.py"]
    assert len(seen_environments) == 1


def test_runtime_bridge_copy_uses_a_private_source_per_call(monkeypatch):
    copied = []

    def record_copy(_container, source, _destination):
        assert source.is_file()
        copied.append(source)

    monkeypatch.setattr(
        "swebench.harness.namerts.runtime.copy_to_container", record_copy
    )

    install_runtime_bridge(object())
    install_runtime_bridge(object())

    assert copied[0] != copied[1]
    assert all(path.name == "container_bridge.py" for path in copied)


def test_terminal_and_skipped_reports_are_reusable():
    timeout = terminal_timeout_report("timed out")
    assert timeout["completed"] is False
    assert timeout["passed"] is False
    assert reusable_regression_report({"namerts_regression": timeout})

    skipped = skipped_upstream_report()
    assert skipped["completed"] is True
    assert skipped["passed"] is False
    assert skipped["stage"] == "upstream_unresolved"


def test_upstream_unresolved_does_not_start_namerts(monkeypatch):
    from swebench.harness import run_evaluation

    monkeypatch.setattr(
        run_evaluation,
        "run_regression_evaluation",
        lambda **_kwargs: pytest.fail("RTS should not run"),
    )
    spec = make_spec()
    report = {spec.instance_id: {"resolved": False}}
    updated = run_evaluation._add_namerts_result(
        report,
        spec,
        make_instance(),
        {"model_patch": "patch"},
        None,
        "run",
        Path("."),
        None,
        "artifacts",
        4,
        900,
        7200,
    )
    assert updated[spec.instance_id]["resolved"] is False
    assert updated[spec.instance_id]["namerts_regression"]["skipped"] is True


@pytest.mark.parametrize("override,expected", [(None, 3600), (7200, 7200)])
def test_rts_timeout_inherits_upstream_unless_overridden(
    tmp_path, monkeypatch, override, expected
):
    from swebench.harness import run_evaluation

    spec = make_spec()
    log_dir = tmp_path / "timeout-test" / "gold" / spec.instance_id
    log_dir.mkdir(parents=True)
    (log_dir / "report.json").write_text(
        json.dumps({spec.instance_id: {"resolved": True}})
    )
    monkeypatch.setattr(run_evaluation, "RUN_EVALUATION_LOG_DIR", tmp_path)
    received = []

    def add_result(*args):
        received.append(args[-1])
        return args[0]

    monkeypatch.setattr(run_evaluation, "_add_namerts_result", add_result)
    run_evaluation.run_instance(
        spec,
        {"model_name_or_path": "gold"},
        None,
        "timeout-test",
        timeout=3600,
        instance=make_instance(),
        namerts_artifact_root="artifacts",
        namerts_timeout=override,
    )
    assert received == [expected]


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


@pytest.mark.parametrize(
    ("regression_passed", "resolved"), [(True, True), (False, False)]
)
def test_final_verdict_combines_upstream_and_namerts(
    monkeypatch, regression_passed, resolved
):
    from swebench.harness import run_evaluation

    monkeypatch.setattr(
        run_evaluation,
        "run_regression_evaluation",
        lambda **_kwargs: {"completed": True, "passed": regression_passed},
    )
    spec = make_spec()
    report = {spec.instance_id: {"resolved": True}}
    updated = run_evaluation._add_namerts_result(
        report,
        spec,
        make_instance(),
        {"model_patch": "patch"},
        None,
        "run",
        Path("."),
        None,
        "artifacts",
        4,
        900,
        7200,
    )
    assert updated[spec.instance_id]["resolved"] is resolved


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


def test_runtime_rejects_changed_local_official_image(tmp_path):
    spec = make_spec()
    instance = make_instance()
    write_artifact(tmp_path, spec, instance)

    class Images:
        @staticmethod
        def get(_name):
            return type("Image", (), {"id": "sha256:different"})()

    client = type("Client", (), {"images": Images()})()
    report = run_regression_evaluation(
        test_spec=spec,
        instance=instance,
        prediction={"model_patch": ""},
        client=client,
        container_factory=lambda _spec: pytest.fail("container created"),
        artifact_root=str(tmp_path),
        log_dir=tmp_path,
        logger=logging.getLogger("test-namerts-image-id"),
        workers=1,
        per_file_timeout=1,
        timeout=1,
    )
    assert report["passed"] is False
    assert report["stage"] == "artifact"
    assert "Official image ID mismatch" in report["error"]


def test_error_report_is_conservatively_unresolved():
    report = make_error_report("selection", RuntimeError("boom"))
    assert report["completed"] is False
    assert report["passed"] is False
    assert report["error"] == "boom"


def test_runtime_image_has_dedicated_namespace():
    image = _runtime_image_name("pytest-dev__pytest-5809", "amd64", "1234567890abcdef")
    assert image == "sweb.namerts.x86_64.pytest-dev-pytest-5809:1234567890ab"
    assert not image.startswith("sweb.eval.")


def test_stale_instance_containers_are_removed_before_create():
    removed = []

    class Container:
        def __init__(self, name):
            self.name = name

        def remove(self, force=False):
            removed.append((self.name, force))

    containers = type(
        "Containers",
        (),
        {
            "list": staticmethod(
                lambda **_kwargs: [
                    Container("sweb.namerts.prepare.pytest-dev__pytest-1.old"),
                    Container("sweb.namerts.prepare.pytest-dev__pytest-10.other"),
                ]
            )
        },
    )()
    client = type("Client", (), {"containers": containers})()

    _remove_existing_instance_containers(
        client, "pytest-dev__pytest-1", "prepare", logging.getLogger("test")
    )

    assert removed == [("sweb.namerts.prepare.pytest-dev__pytest-1.old", True)]


def test_only_superseded_tags_for_same_instance_are_removed():
    removed = []
    repository = "sweb.namerts.x86_64.pytest-dev-pytest-1"
    current_tag = repository + ":current"
    current = type("Image", (), {"id": "sha256:current", "tags": [current_tag]})()
    old = type(
        "Image",
        (),
        {
            "id": "sha256:old",
            "tags": [repository + ":old", "sweb.namerts.x86_64.other:shared"],
        },
    )()

    class Images:
        @staticmethod
        def get(name):
            assert name == current_tag
            return current

        @staticmethod
        def list(name=None):
            assert name == repository
            return [current, old]

        @staticmethod
        def remove(name, force=False):
            removed.append((name, force))

    client = type("Client", (), {"images": Images()})()

    _remove_superseded_runtime_images(client, current_tag, logging.getLogger("test"))

    assert removed == [(repository + ":old", False)]


def test_container_bridge_parses_as_python_36():
    source = Path(__file__).parents[1] / "swebench/harness/namerts/container_bridge.py"
    ast.parse(source.read_text(), filename=str(source), feature_version=(3, 6))


def test_sympy_runtime_installs_pytest_in_testbed_only():
    from swebench.harness.namerts.prepare import _instrumentation_testbed_install

    command = _instrumentation_testbed_install("sympy__sympy-23413", "NameRTS39")
    assert command == (
        "RUN /opt/miniconda3/envs/testbed/bin/python -m pip install pytest==7.4.4\n"
    )
    assert "pytest==6.2.5" in _instrumentation_testbed_install(
        "sympy__sympy-1", "NameRTS36"
    )
    assert _instrumentation_testbed_install("django__django-1", "NameRTS39") == ""
