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
from swebench.harness.namerts.policy import read_policy


ARTIFACT_SCHEMA_VERSION = 8
RUNTIME_RECIPE_VERSION = "19"
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
    if actual_hashes.get("test_execution") != "grouped":
        raise NameRTSArtifactError("NameRTS artifact does not use grouped execution")
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
    if baseline.get("execution_mode") != "grouped":
        raise NameRTSArtifactError("Golden baseline does not use grouped execution")
    if test_spec.repo == "sympy/sympy" and baseline.get("calculation_cache") != "yes":
        raise NameRTSArtifactError("SymPy RTS baseline must enable calculation caching")
    normalized = normalize_test_results(test_spec, baseline)
    if baseline.get("failure_ids") != normalized["failure_ids"]:
        raise NameRTSArtifactError("Golden baseline failures do not match its output")
    timings = baseline.get("instrumentation_results")
    threshold = baseline.get("slow_test_threshold_seconds")
    if not isinstance(timings, list):
        raise NameRTSArtifactError("Missing instrumentation timings")
    if not isinstance(threshold, (int, float)) or not 0 < threshold < float("inf"):
        raise NameRTSArtifactError("Invalid slow-test threshold")
    seen = set()
    for item in timings:
        name = item.get("test_file")
        duration = item.get("duration")
        if (
            not isinstance(name, str)
            or name in seen
            or not isinstance(duration, (int, float))
            or not 0 <= duration < float("inf")
            or not isinstance(item.get("timed_out"), bool)
        ):
            raise NameRTSArtifactError("Incomplete or invalid instrumentation timings")
        seen.add(name)
    persisted = baseline.get("persisted_slow_test_files")
    if not isinstance(persisted, list) or any(not isinstance(f, str) for f in persisted):
        raise NameRTSArtifactError("Missing persisted slow-file policy")
    parent_tests = baseline.get("instrumentation_discovered_tests")
    skipped = baseline.get("skip_test_files", {})
    if not isinstance(skipped, dict):
        raise NameRTSArtifactError("Invalid skip test policy")
    if not isinstance(parent_tests, list) or not parent_tests or seen != set(parent_tests) - set(persisted) - set(skipped):
        raise NameRTSArtifactError("Incomplete instrumentation scope")
    slow = sorted(set(persisted) | {
        item["test_file"] for item in timings
        if item["timed_out"] or item["duration"] >= threshold
    })
    if baseline.get("slow_test_files") != slow:
        raise NameRTSArtifactError("Slow-file summary disagrees with instrumentation")
    current_policy = read_policy(test_spec.instance_id)
    saved_policy = baseline.get("instance_policy", {})
    if skipped != current_policy["skip_test_files"] or skipped != saved_policy.get("skip_test_files", {}):
        raise NameRTSArtifactError("Skip test policy changed; prepare a new artifact")
    if (not set(current_policy["slow_test_files"]).issubset(slow)
        or any(current_policy.get(k) != saved_policy.get(k)
               for k in ("pytest_version", "py_version", "test_dependencies"))):
        raise NameRTSArtifactError("Instance policy changed; prepare a new artifact")
    if (
        actual_hashes.get("slow_test_files") != slow
        or actual_hashes.get("slow_test_threshold_seconds") != threshold
    ):
        raise NameRTSArtifactError("Slow-file policy disagrees with manifest")
    discovered = baseline.get("discovered_tests")
    if (
        not isinstance(discovered, list)
        or sorted(set(discovered) - set(slow) - set(skipped)) != baseline["tests"]
    ):
        raise NameRTSArtifactError("Golden scope does not match the slow-file policy")
    return NameRTSArtifact(directory, manifest, baseline)


def normalize_test_results(test_spec: TestSpec, payload: dict) -> dict[str, Any]:
    """Compare official parser case IDs, never scope-dependent group exit IDs."""
    from swebench.harness.namerts.container_bridge import is_collection_failure

    tests = payload.get("tests")
    if (
        payload.get("execution_mode") != "grouped"
        or payload.get("completed") is not True
        or not isinstance(tests, list)
        or tests != sorted(set(tests))
        or len(tests) != payload.get("test_count")
        or payload.get("timed_out")
    ):
        raise NameRTSArtifactError("Incomplete or invalid grouped test result")
    output = str(payload.get("stdout", "")) + "\n" + str(payload.get("stderr", ""))
    if payload.get("return_code") not in (0, 1):
        raise NameRTSArtifactError("Grouped runner did not exit normally")
    if is_collection_failure(payload):
        raise NameRTSArtifactError(
            "Grouped collection failed; baseline comparison is unsafe"
        )
    statuses = PARSER_REGISTRY[test_spec.log_parser](output, test_spec)
    failed = {TestStatus.FAILED.value, TestStatus.ERROR.value}
    failures = {
        "case::" + normalize_parsed_test_id(str(case)): {
            "id": "case::" + normalize_parsed_test_id(str(case)),
            "kind": "test_case",
            "test_case": str(case),
            "status": status,
        }
        for case, status in statuses.items()
        if status in failed
    }
    if (payload.get("return_code") != 0 and not failures) or (tests and not statuses):
        raise NameRTSArtifactError("Runner output has no comparable test results")
    return {
        "failure_ids": sorted(failures),
        "failures": [failures[key] for key in sorted(failures)],
        "execution_mode": "grouped",
        "requested_test_count": len(tests),
        "process_count": int(bool(tests)),
        "nonzero_processes": int(payload.get("return_code") != 0),
    }


def filter_selection_for_baseline(selection: dict, baseline: dict) -> dict[str, Any]:
    """Filter only the additional RTS regression scope, never upstream tests."""
    requested = sorted(set(selection.get("tests_to_run", [])))
    excluded = sorted(set(requested) & set(baseline["slow_test_files"]))
    skipped = sorted(set(requested) & set(baseline.get("skip_test_files", {})))
    return {
        **selection,
        "tests_to_run": sorted(set(requested) - set(excluded) - set(skipped)),
        "compatibility_excluded_tests": skipped,
        "tests_to_run_before_slow_filter": requested,
        "slow_excluded_tests": excluded,
        "slow_excluded_count": len(excluded),
    }


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
            key: normalized[key]
            for key in (
                "execution_mode",
                "requested_test_count",
                "process_count",
                "nonzero_processes",
            )
        },
        "slow_excluded_tests": selection.get("slow_excluded_tests", []),
        "slow_excluded_count": selection.get("slow_excluded_count", 0),
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
