from __future__ import annotations

import json
import traceback

from dataclasses import replace
from pathlib import Path, PurePosixPath

from swebench.harness.constants import CONTAINER_PATCH_FILE
from swebench.harness.docker_utils import cleanup_container, copy_to_container
from swebench.harness.namerts.artifacts import (
    filter_selection_for_baseline,
    load_artifact,
    make_error_report,
    make_regression_report,
    make_skipped_report,
    sha256_text,
)
from swebench.harness.namerts.runtime import (
    apply_test_patch,
    install_runtime_bridge,
    make_harness_test_command_prefix,
    run_install_script,
    run_selected_tests,
    run_selection,
    restore_test_environment,
)
from swebench.types import TestSpec


UNSUPPORTED_INSTANCE_IDS = {
    "django__django-10097",
    "django__django-7530",
    "psf__requests-1724",
}


def is_unsupported_instance(instance_id: str) -> bool:
    return instance_id in UNSUPPORTED_INSTANCE_IDS


def reusable_regression_report(result: dict) -> bool:
    report = result.get("namerts_regression")
    return isinstance(report, dict) and isinstance(report.get("passed"), bool)


def skipped_upstream_report() -> dict:
    return make_skipped_report("upstream_unresolved")


def terminal_timeout_report(error: Exception | str) -> dict:
    return make_error_report("upstream_tests_timeout", error)


def run_regression_evaluation(
    *,
    test_spec: TestSpec,
    instance: dict,
    prediction: dict,
    client,
    container_factory,
    artifact_root: str,
    log_dir: Path,
    logger,
    workers: int,
    per_file_timeout: int,
    timeout: int,
) -> dict:
    if is_unsupported_instance(test_spec.instance_id):
        return {
            "completed": True,
            "passed": True,
            "skipped": True,
            "stage": "unsupported_python_runtime",
            "selected_tests": [],
            "selected_count": 0,
            "new_failure_ids": [],
            "new_failures": [],
        }
    stage = "artifact"
    container = None
    selection = None
    try:
        artifact = load_artifact(artifact_root, test_spec, instance)
        official_image = client.images.get(test_spec.image)
        expected_official_image_id = artifact.manifest["runtime"]["official_image_id"]
        if official_image.id != expected_official_image_id:
            raise ValueError(
                "Official image ID mismatch: expected "
                f"{expected_official_image_id}, got {official_image.id}"
            )
        image = client.images.get(artifact.runtime_image)
        expected_image_id = artifact.manifest["runtime"]["image_id"]
        if image.id != expected_image_id:
            raise ValueError(
                f"Runtime image ID mismatch: expected {expected_image_id}, got {image.id}"
            )
        test_command = make_harness_test_command_prefix(
            test_spec.eval_script, instance["test_patch"], test_spec.repo
        )
        expected_test_command_sha256 = artifact.manifest["preparation"].get(
            "harness_test_command_sha256"
        )
        actual_test_command_sha256 = sha256_text(test_command)
        if expected_test_command_sha256 != actual_test_command_sha256:
            raise ValueError(
                "SWE-bench test command mismatch: expected "
                f"{expected_test_command_sha256}, got {actual_test_command_sha256}"
            )
        stage = "container"
        runtime_spec = replace(test_spec, image=artifact.runtime_image)
        container = container_factory(runtime_spec)
        container.start()
        install_runtime_bridge(container)
        patch_path = log_dir / "patch.diff"
        patch_path.write_text(prediction.get("model_patch") or "")
        copy_to_container(container, patch_path, PurePosixPath(CONTAINER_PATCH_FILE))
        stage = "selection"
        selection = run_selection(
            container,
            artifact,
            log_dir,
            patch_base=instance["base_commit"],
            workers=workers,
            per_file_timeout=per_file_timeout,
            timeout=timeout,
        )
        selection = filter_selection_for_baseline(selection, artifact.baseline)
        (log_dir / "namerts_selection.json").write_text(
            json.dumps(selection, indent=2, sort_keys=True)
        )
        stage = "run_tests"
        if selection.get("tests_to_run"):
            stage = "candidate_setup"
            restore_test_environment(
                container, artifact, instance["base_commit"], log_dir, timeout
            )
            stage = "candidate_install"
            run_install_script(
                container,
                test_spec.eval_script,
                instance["base_commit"],
                log_dir,
                timeout,
            )
            stage = "run_tests"
            apply_test_patch(
                container,
                test_spec.eval_script,
                instance["base_commit"],
                log_dir,
                timeout,
            )
        target_results = run_selected_tests(
            container,
            artifact,
            log_dir,
            test_command=test_command,
            workers=workers,
            per_file_timeout=per_file_timeout,
            timeout=timeout,
        )
        return make_regression_report(test_spec, artifact, selection, target_results)
    except Exception as error:
        logger.error(
            "NameRTS %s failed for %s:\n%s",
            stage,
            test_spec.instance_id,
            traceback.format_exc(),
        )
        report = make_error_report(stage, error)
        if selection is not None:
            selected = sorted(selection.get("tests_to_run", []))
            report["selected_tests"] = selected
            report["selected_count"] = len(selected)
        return report
    finally:
        cleanup_container(client, container, logger)
