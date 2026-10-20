import ast
import importlib.util
import json
import logging
import os
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
    deterministic_test_command,
    is_collection_failure,
    target_timeout_for_golden,
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
from swebench.harness.namerts.runtime import (
    NameRTSRuntimeError,
    install_runtime_bridge,
    make_harness_test_command_prefix,
    make_install_script,
    make_test_script,
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
                "execution_mode": "per_file",
                "test_process_isolation": "per_file_temp_root",
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
            "test_execution": "per_file",
            "test_process_isolation": "per_file_temp_root",
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
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            "bin/test -C --verbose",
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


def test_normalize_per_file_results_use_file_scoped_case_identity():
    payload = {
        "completed": True,
        "execution_mode": "per_file",
        "scope": "selected",
        "tests": ["testing/test_a.py", "testing/test_b.py"],
        "test_count": 2,
        "collection_failed_files": [],
        "results": [
            {
                "test_file": "testing/test_a.py",
                "return_code": 1,
                "timed_out": False,
                "collection_failed": False,
                "stdout": "FAILED testing/test_a.py::test_bad\n",
                "stderr": "",
            },
            {
                "test_file": "testing/test_b.py",
                "return_code": 0,
                "timed_out": False,
                "collection_failed": False,
                "stdout": "1 passed\n",
                "stderr": "",
            },
        ],
    }

    result = normalize_test_results(make_spec(), payload)

    assert result["failure_ids"] == [
        "case::testing/test_a.py::testing/test_a.py::test_bad"
    ]
    assert result["execution_mode"] == "per_file"
    assert result["requested_test_count"] == 2
    assert result["process_count"] == 2


def test_normalize_test_results_rejects_grouped_payload():
    with pytest.raises(NameRTSArtifactError, match="per-file execution"):
        normalize_test_results(
            make_spec(), {"execution_mode": "harness_grouped", "result": {}}
        )


def test_per_file_regression_subtracts_same_golden_case(tmp_path):
    golden_failure = "case::testing/test_a.py::testing/test_a.py::test_old"
    artifact = NameRTSArtifact(
        tmp_path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runtime": {
                "image": "sweb.namerts.test",
                "source_sha256": "source",
                "namerts_env": "NameRTS39",
                "repo_head": "head",
                "project_name": "pytest",
            },
        },
        {"failure_ids": [golden_failure]},
    )
    target = {
        "completed": True,
        "execution_mode": "per_file",
        "scope": "selected",
        "tests": ["testing/test_a.py", "testing/test_b.py"],
        "test_count": 2,
        "collection_failed_files": [],
        "results": [
            {
                "test_file": "testing/test_a.py",
                "return_code": 1,
                "timed_out": False,
                "collection_failed": False,
                "stdout": "FAILED testing/test_a.py::test_old\n",
                "stderr": "",
            },
            {
                "test_file": "testing/test_b.py",
                "return_code": 1,
                "timed_out": False,
                "collection_failed": False,
                "stdout": "FAILED testing/test_b.py::test_new\n",
                "stderr": "",
            },
        ],
    }

    report = make_regression_report(
        make_spec(),
        artifact,
        {"tests_to_run": ["testing/test_a.py", "testing/test_b.py"]},
        target,
    )

    assert report["passed"] is False
    assert report["golden_failure_ids_seen"] == [golden_failure]
    assert report["new_failure_ids"] == [
        "case::testing/test_b.py::testing/test_b.py::test_new"
    ]


def test_golden_collection_failure_is_filtered_from_selection():
    selection = {"tests_to_run": ["testing/test_a.py", "testing/test_b.py"]}
    baseline = {"collection_failed_files": ["testing/test_a.py"]}

    filtered = filter_selection_for_baseline(selection, baseline)

    assert filtered["tests_to_run"] == ["testing/test_b.py"]
    assert filtered["golden_collection_excluded_tests"] == ["testing/test_a.py"]
    assert filtered["golden_collection_excluded_count"] == 1
    assert filtered["golden_timeout_excluded_tests"] == []
    assert filtered["golden_incomplete_excluded_tests"] == ["testing/test_a.py"]


def test_golden_timeout_is_filtered_from_selection():
    selection = {"tests_to_run": ["testing/test_a.py", "testing/test_b.py"]}
    baseline = {
        "collection_failed_files": [],
        "results": [
            {"test_file": "testing/test_a.py", "timed_out": True},
            {"test_file": "testing/test_b.py", "timed_out": False},
        ],
    }

    filtered = filter_selection_for_baseline(selection, baseline)

    assert filtered["tests_to_run"] == ["testing/test_b.py"]
    assert filtered["golden_timeout_excluded_tests"] == ["testing/test_a.py"]
    assert filtered["golden_timeout_excluded_count"] == 1
    assert filtered["golden_incomplete_excluded_tests"] == ["testing/test_a.py"]
    assert filtered["golden_incomplete_excluded_count"] == 1


def test_new_target_collection_failure_is_a_regression(tmp_path):
    artifact = NameRTSArtifact(
        tmp_path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runtime": {
                "image": "sweb.namerts.test",
                "source_sha256": "source",
                "namerts_env": "NameRTS39",
                "repo_head": "head",
                "project_name": "pytest",
            },
        },
        {"failure_ids": [], "collection_failed_files": []},
    )
    target = {
        "completed": True,
        "execution_mode": "per_file",
        "scope": "selected",
        "tests": ["testing/test_a.py"],
        "test_count": 1,
        "collection_failed_files": ["testing/test_a.py"],
        "results": [
            {
                "test_file": "testing/test_a.py",
                "return_code": 2,
                "timed_out": False,
                "collection_failed": True,
                "stdout": "ERROR collecting testing/test_a.py",
                "stderr": "",
            }
        ],
    }

    report = make_regression_report(
        make_spec(), artifact, {"tests_to_run": target["tests"]}, target
    )

    assert report["passed"] is False
    assert report["new_failure_ids"] == ["collect::testing/test_a.py"]


def test_unparsed_nonzero_is_comparable_at_file_scope(tmp_path):
    baseline_payload = {
        "execution_mode": "per_file",
        "scope": "golden_full_suite",
        "tests": ["testing/test_a.py"],
        "test_count": 1,
        "collection_failed_files": [],
        "results": [
            {
                "test_file": "testing/test_a.py",
                "return_code": 2,
                "timed_out": False,
                "collection_failed": False,
                "stdout": "internal error",
                "stderr": "",
            }
        ],
    }
    baseline = normalize_test_results(make_spec(), baseline_payload)
    artifact = NameRTSArtifact(
        tmp_path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "runtime": {
                "image": "sweb.namerts.test",
                "source_sha256": "source",
                "namerts_env": "NameRTS39",
                "repo_head": "head",
                "project_name": "pytest",
            },
        },
        baseline,
    )

    report = make_regression_report(
        make_spec(),
        artifact,
        {"tests_to_run": baseline_payload["tests"]},
        baseline_payload,
    )

    assert report["passed"] is True
    assert report["golden_failure_ids_seen"] == ["file::testing/test_a.py"]


def test_collection_detection_and_adaptive_timeout():
    assert is_collection_failure(
        {
            "return_code": 2,
            "timed_out": False,
            "stdout": "ERROR collecting tests/test_bad.py",
            "stderr": "",
        }
    )
    assert not is_collection_failure(
        {
            "return_code": 1,
            "timed_out": False,
            "stdout": "FAILED tests/test_bad.py::test_bad",
            "stderr": "",
        }
    )
    assert (
        target_timeout_for_golden({"duration": 110.0, "timed_out": False}, 120) == 390
    )
    assert target_timeout_for_golden({"duration": 20.0, "timed_out": False}, 120) == 120


def test_parsed_failure_identity_removes_ephemeral_values():
    golden = (
        "fitscheck:194 EXCEPTION '/tmp/fits-test-yr9h0fl6/checksum.fits' "
        "from object at 0x7f1234"
    )
    target = (
        "fitscheck:194 EXCEPTION '/tmp/fits-test-m4qvxt7s/checksum.fits' "
        "from object at 0x9abcde"
    )

    assert normalize_parsed_test_id(golden) == normalize_parsed_test_id(target)
    assert target_timeout_for_golden({"duration": 120.0, "timed_out": True}, 120) == 120


def test_sympy_runner_uses_deterministic_seed():
    command = (
        "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
        "bin/test -C --verbose"
    )
    assert deterministic_test_command(command) == command + " --seed 0"
    assert deterministic_test_command(command + " --seed 17") == command + " --seed 17"
    assert deterministic_test_command("pytest -rA") == "pytest -rA"


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


def test_pretest_namerts_error_is_terminal_and_unresolved():
    from swebench.harness import run_evaluation

    report = run_evaluation._make_namerts_pretest_error_report(
        "pytest-dev__pytest-1",
        {"model_patch": "patch"},
        "candidate_base_reset",
        "reset failed",
    )
    result = report["pytest-dev__pytest-1"]
    assert result["resolved"] is False
    assert result["infra_failure"] is True
    assert result["namerts_regression"]["passed"] is False
    assert reusable_regression_report(result)


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
