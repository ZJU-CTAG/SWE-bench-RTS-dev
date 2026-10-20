from __future__ import annotations

import hashlib
import json
import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from swebench.harness.constants import TestStatus
from swebench.harness.log_parsers import PARSER_REGISTRY
from swebench.types import TestSpec


ARTIFACT_SCHEMA_VERSION = 5
RUNTIME_RECIPE_VERSION = "11"
MANIFEST_FILENAME = "manifest.json"
PARENT_CACHE_FILENAME = "parent_cache.tar.gz"
GOLDEN_BASELINE_FILENAME = "golden_baseline.json"


class NameRTSArtifactError(ValueError):
    """Raised when a prepared NameRTS artifact cannot be trusted."""


def normalize_parsed_test_id(test_id: str) -> str:
    """Remove run-specific values that some project parsers include in IDs."""
    normalized = re.sub(r"/tmp/[^\s'\"`]+", "/tmp/<temp>", str(test_id))
    return re.sub(r"0x[0-9a-fA-F]+", "0x<address>", normalized)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_tree(root: Path, relative_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(relative_paths, key=lambda path: path.as_posix()):
        digest.update(relative_path.as_posix().encode())
        digest.update(b"\0")
        with (root / relative_path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _checked_file(directory: Path, filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise NameRTSArtifactError(f"Unsafe artifact filename: {filename!r}")
    path = directory / filename
    if not path.is_file():
        raise NameRTSArtifactError(f"Missing NameRTS artifact file: {path}")
    return path


@dataclass(frozen=True)
class NameRTSArtifact:
    directory: Path
    manifest: dict[str, Any]
    baseline: dict[str, Any]

    @property
    def cache_path(self) -> Path:
        return self.directory / self.manifest["files"]["parent_cache"]["name"]

    @property
    def runtime_image(self) -> str:
        return self.manifest["runtime"]["image"]

    @property
    def namerts_env(self) -> str:
        return self.manifest["runtime"]["namerts_env"]

    @property
    def repo_head(self) -> str:
        return self.manifest["runtime"]["repo_head"]

    @property
    def project_name(self) -> str:
        return self.manifest["runtime"]["project_name"]


def load_artifact(
    artifact_root: Path | str, test_spec: TestSpec, instance: dict
) -> NameRTSArtifact:
    """Load an artifact and verify its dataset and file provenance."""
    directory = Path(artifact_root).expanduser().resolve() / test_spec.instance_id
    manifest_path = _checked_file(directory, MANIFEST_FILENAME)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise NameRTSArtifactError(f"Cannot read {manifest_path}: {error}") from error

    expected = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "status": "complete",
        "instance_id": test_spec.instance_id,
        "repo": test_spec.repo,
        "base_commit": instance["base_commit"],
    }
    mismatches = {
        key: {"expected": value, "actual": manifest.get(key)}
        for key, value in expected.items()
        if manifest.get(key) != value
    }
    if mismatches:
        raise NameRTSArtifactError(
            f"NameRTS manifest does not match {test_spec.instance_id}: {mismatches}"
        )
    if manifest.get("language") != "py":
        raise NameRTSArtifactError("NameRTS currently supports Python instances only")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise NameRTSArtifactError("NameRTS manifest has no files map")
    verified: dict[str, Path] = {}
    for key in ("parent_cache", "golden_baseline"):
        descriptor = files.get(key)
        if not isinstance(descriptor, dict):
            raise NameRTSArtifactError(f"Missing NameRTS file descriptor: {key}")
        path = _checked_file(directory, descriptor.get("name", ""))
        actual_hash = sha256_file(path)
        if descriptor.get("sha256") != actual_hash:
            raise NameRTSArtifactError(
                f"Checksum mismatch for {path}: expected "
                f"{descriptor.get('sha256')}, got {actual_hash}"
            )
        verified[key] = path

    runtime = manifest.get("runtime")
    for key in (
        "official_image",
        "official_image_id",
        "image",
        "image_id",
        "namerts_env",
        "python_version",
        "python_minor",
        "project_name",
        "repo_head",
        "source_sha256",
        "fingerprint",
        "recipe_version",
    ):
        if not isinstance(runtime, dict) or not runtime.get(key):
            raise NameRTSArtifactError(f"Missing NameRTS runtime field: {key}")
    runtime_mismatches = {}
    if runtime["official_image"] != test_spec.image:
        runtime_mismatches["official_image"] = {
            "expected": test_spec.image,
            "actual": runtime["official_image"],
        }
    if runtime["recipe_version"] != RUNTIME_RECIPE_VERSION:
        runtime_mismatches["recipe_version"] = {
            "expected": RUNTIME_RECIPE_VERSION,
            "actual": runtime["recipe_version"],
        }
    if runtime_mismatches:
        raise NameRTSArtifactError(
            f"NameRTS runtime provenance mismatch: {runtime_mismatches}"
        )

    dataset = manifest.get("dataset")
    expected_dataset = {
        "eval_script_sha256": sha256_text(test_spec.eval_script),
        "log_parser": test_spec.log_parser,
        "eval_type": test_spec.eval_type,
    }
    actual_dataset = dataset if isinstance(dataset, dict) else {}
    dataset_mismatches = {
        key: {"expected": value, "actual": actual_dataset.get(key)}
        for key, value in expected_dataset.items()
        if actual_dataset.get(key) != value
    }
    if dataset_mismatches:
        raise NameRTSArtifactError(
            f"NameRTS dataset execution provenance mismatch: {dataset_mismatches}"
        )

    preparation = manifest.get("preparation")
    expected_hashes = {
        "golden_patch_sha256": sha256_text(instance["patch"]),
        "test_patch_sha256": sha256_text(instance["test_patch"]),
    }
    actual_hashes = preparation if isinstance(preparation, dict) else {}
    patch_mismatches = {
        key: {"expected": value, "actual": actual_hashes.get(key)}
        for key, value in expected_hashes.items()
        if actual_hashes.get(key) != value
    }
    if patch_mismatches:
        raise NameRTSArtifactError(
            "NameRTS artifact patch provenance does not match "
            f"{test_spec.instance_id}: {patch_mismatches}"
        )
    if actual_hashes.get("test_execution") != "per_file":
        raise NameRTSArtifactError(
            "NameRTS artifact does not use per-file test execution"
        )
    if actual_hashes.get("test_process_isolation") != "per_file_temp_root":
        raise NameRTSArtifactError(
            "NameRTS artifact does not isolate per-file temporary roots"
        )
    test_command_sha256 = actual_hashes.get("harness_test_command_sha256")
    if not isinstance(test_command_sha256, str) or len(test_command_sha256) != 64:
        raise NameRTSArtifactError(
            "NameRTS artifact has no valid SWE-bench test command hash"
        )

    try:
        baseline = json.loads(verified["golden_baseline"].read_text())
    except (json.JSONDecodeError, OSError) as error:
        raise NameRTSArtifactError(f"Cannot read golden baseline: {error}") from error
    if baseline.get("completed") is not True:
        raise NameRTSArtifactError("Golden full-suite baseline is incomplete")
    if baseline.get("execution_mode") != "per_file":
        raise NameRTSArtifactError(
            "Golden full-suite baseline was not run per test file"
        )
    if baseline.get("test_process_isolation") != "per_file_temp_root":
        raise NameRTSArtifactError(
            "Golden full-suite baseline does not isolate per-file temporary roots"
        )
    if not isinstance(baseline.get("failure_ids"), list):
        raise NameRTSArtifactError("Golden baseline has no failure_ids")
    results = baseline.get("results")
    if not isinstance(results, list):
        raise NameRTSArtifactError("Golden baseline has no per-file results")
    tests = baseline.get("tests")
    if not isinstance(tests, list) or len(tests) != len(set(tests)):
        raise NameRTSArtifactError("Golden baseline has invalid test files")
    result_files = [item.get("test_file") for item in results if isinstance(item, dict)]
    if len(result_files) != len(results) or sorted(result_files) != sorted(tests):
        raise NameRTSArtifactError(
            "Golden baseline per-file results do not match discovered tests"
        )
    collection_failed_files = baseline.get("collection_failed_files")
    if (
        not isinstance(collection_failed_files, list)
        or len(collection_failed_files) != len(set(collection_failed_files))
        or not set(collection_failed_files).issubset(tests)
    ):
        raise NameRTSArtifactError(
            "Golden baseline has invalid collection-failed test files"
        )
    recorded_collection_failures = {
        result["test_file"]
        for result in results
        if bool(result.get("collection_failed", False))
    }
    if set(collection_failed_files) != recorded_collection_failures:
        raise NameRTSArtifactError(
            "Golden baseline collection-failed summary does not match results"
        )
    for result in results:
        target_timeout = result.get("target_timeout_seconds")
        if not isinstance(target_timeout, int) or target_timeout <= 0:
            raise NameRTSArtifactError(
                "Golden baseline has no valid adaptive target timeout for "
                f"{result.get('test_file')}"
            )
    return NameRTSArtifact(directory, manifest, baseline)


def normalize_test_results(test_spec: TestSpec, payload: dict) -> dict[str, Any]:
    """Normalize test output into stable, comparable failure identities."""
    if payload.get("execution_mode") != "per_file":
        raise NameRTSArtifactError(
            "NameRTS test payload was not produced by per-file execution"
        )
    results = payload.get("results")
    if not isinstance(results, list):
        raise NameRTSArtifactError("NameRTS per-file test payload has no results")
    tests = payload.get("tests")
    if not isinstance(tests, list) or len(tests) != len(set(tests)):
        raise NameRTSArtifactError("NameRTS per-file test payload has invalid tests")
    parser = PARSER_REGISTRY[test_spec.log_parser]
    failed_statuses = {TestStatus.FAILED.value, TestStatus.ERROR.value}
    failures = {}
    seen_files = set()
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("test_file"), str):
            raise NameRTSArtifactError("Invalid NameRTS per-file test result")
        test_file = result["test_file"]
        if test_file in seen_files:
            raise NameRTSArtifactError(f"Duplicate NameRTS test result: {test_file}")
        seen_files.add(test_file)
        return_code = result.get("return_code")
        timed_out = bool(result.get("timed_out", False))
        collection_failed = bool(result.get("collection_failed", False))
        output = "\n".join(
            (str(result.get("stdout", "")), str(result.get("stderr", "")))
        )
        if collection_failed:
            identity = f"collect::{test_file}"
            failures[identity] = {
                "id": identity,
                "kind": "collection",
                "test_file": test_file,
                "return_code": return_code,
                "timed_out": False,
            }
            continue
        if timed_out:
            identity = f"timeout::{test_file}"
            failures[identity] = {
                "id": identity,
                "kind": "timeout",
                "test_file": test_file,
                "return_code": return_code,
                "timed_out": True,
            }
            continue
        try:
            status_map = parser(output, test_spec)
        except Exception:
            status_map = {}
        parsed = {
            normalize_parsed_test_id(str(case)): str(status)
            for case, status in status_map.items()
            if status in failed_statuses
        }
        for case, status in sorted(parsed.items()):
            identity = f"case::{test_file}::{case}"
            failures[identity] = {
                "id": identity,
                "kind": "test_case",
                "test_file": test_file,
                "test_case": case,
                "status": status,
                "return_code": return_code,
                "timed_out": False,
            }
        if return_code != 0 and not parsed:
            identity = f"file::{test_file}"
            failures[identity] = {
                "id": identity,
                "kind": "unparsed_nonzero",
                "test_file": test_file,
                "return_code": return_code,
                "timed_out": False,
            }
    if seen_files != set(tests) or len(results) != int(payload.get("test_count", -1)):
        raise NameRTSArtifactError(
            "NameRTS per-file results do not match the requested test files"
        )
    summarized_collection_failures = payload.get("collection_failed_files")
    if not isinstance(summarized_collection_failures, list) or set(
        summarized_collection_failures
    ) != {
        item["test_file"]
        for item in results
        if bool(item.get("collection_failed", False))
    }:
        raise NameRTSArtifactError(
            "NameRTS collection-failed summary does not match per-file results"
        )
    return {
        "failure_ids": sorted(failures),
        "failures": [failures[key] for key in sorted(failures)],
        "execution_mode": "per_file",
        "requested_test_count": int(payload.get("test_count", 0)),
        "process_count": len(results),
        "nonzero_processes": sum(item.get("return_code") != 0 for item in results),
        "timed_out_processes": sum(
            bool(item.get("timed_out", False)) for item in results
        ),
        "collection_failed_processes": sum(
            bool(item.get("collection_failed", False)) for item in results
        ),
    }


def filter_selection_for_baseline(selection: dict, baseline: dict) -> dict[str, Any]:
    """Exclude files whose golden result is incomplete and not comparable."""
    requested = sorted(set(selection.get("tests_to_run", [])))
    collection_excluded = sorted(
        set(requested) & set(baseline.get("collection_failed_files", []))
    )
    timed_out_files = {
        item.get("test_file")
        for item in baseline.get("results", [])
        if isinstance(item, dict) and bool(item.get("timed_out", False))
    }
    timeout_excluded = sorted(set(requested) & timed_out_files)
    excluded = sorted(set(collection_excluded) | set(timeout_excluded))
    filtered = dict(selection)
    filtered["tests_to_run"] = sorted(set(requested) - set(excluded))
    filtered["tests_to_run_before_golden_filter"] = requested
    filtered["golden_collection_excluded_tests"] = collection_excluded
    filtered["golden_collection_excluded_count"] = len(collection_excluded)
    filtered["golden_timeout_excluded_tests"] = timeout_excluded
    filtered["golden_timeout_excluded_count"] = len(timeout_excluded)
    filtered["golden_incomplete_excluded_tests"] = excluded
    filtered["golden_incomplete_excluded_count"] = len(excluded)
    return filtered


def make_regression_report(
    test_spec: TestSpec,
    artifact: NameRTSArtifact,
    selection: dict,
    target_results: dict,
) -> dict[str, Any]:
    normalized = normalize_test_results(test_spec, target_results)
    baseline_ids = set(artifact.baseline["failure_ids"])
    target_ids = set(normalized["failure_ids"])
    new_ids = sorted(target_ids - baseline_ids)
    failures = {item["id"]: item for item in normalized["failures"]}
    selected = sorted(target_results.get("tests", []))
    return {
        "completed": True,
        "passed": not new_ids,
        "selected_tests": selected,
        "selected_count": len(selected),
        "target_failure_ids": sorted(target_ids),
        "target_failure_count": len(target_ids),
        "golden_failure_count": len(baseline_ids),
        "golden_failure_ids_seen": sorted(target_ids & baseline_ids),
        "new_failure_ids": new_ids,
        "new_failure_count": len(new_ids),
        "new_failures": [failures[key] for key in new_ids],
        "target_summary": {
            "execution_mode": normalized["execution_mode"],
            "test_process_isolation": target_results.get("test_process_isolation"),
            "requested_test_count": normalized["requested_test_count"],
            "process_count": normalized["process_count"],
            "nonzero_processes": normalized["nonzero_processes"],
            "timed_out_processes": normalized["timed_out_processes"],
            "collection_failed_processes": normalized["collection_failed_processes"],
        },
        "golden_collection_excluded_tests": sorted(
            selection.get("golden_collection_excluded_tests", [])
        ),
        "golden_collection_excluded_count": int(
            selection.get("golden_collection_excluded_count", 0)
        ),
        "golden_timeout_excluded_tests": sorted(
            selection.get("golden_timeout_excluded_tests", [])
        ),
        "golden_timeout_excluded_count": int(
            selection.get("golden_timeout_excluded_count", 0)
        ),
        "golden_incomplete_excluded_tests": sorted(
            selection.get("golden_incomplete_excluded_tests", [])
        ),
        "golden_incomplete_excluded_count": int(
            selection.get("golden_incomplete_excluded_count", 0)
        ),
        "timing": {
            "select_time": selection.get("select_time"),
            "selection_wall_time_seconds": selection.get("wall_time_seconds"),
            "test_time_seconds": target_results.get("duration"),
            "test_wall_time_seconds": target_results.get("wall_time_seconds"),
        },
        "artifact": {
            "schema_version": artifact.manifest["schema_version"],
            "runtime_image": artifact.runtime_image,
            "source_sha256": artifact.manifest["runtime"]["source_sha256"],
            "runtime_bridge_sha256": sha256_file(
                Path(__file__).with_name("container_bridge.py")
            ),
        },
    }


def make_error_report(stage: str, error: Exception | str) -> dict[str, Any]:
    return {
        "completed": False,
        "passed": False,
        "stage": stage,
        "error": str(error),
        "selected_tests": [],
        "selected_count": 0,
        "new_failure_ids": [],
        "new_failures": [],
    }


def make_skipped_report(stage: str) -> dict[str, Any]:
    return {
        "completed": True,
        "passed": False,
        "skipped": True,
        "stage": stage,
        "selected_tests": [],
        "selected_count": 0,
        "new_failure_ids": [],
        "new_failures": [],
    }
