from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import traceback

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import docker
import docker.errors

import swebench
from swebench.harness.docker_utils import (
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
)
from swebench.harness.namerts.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    GOLDEN_BASELINE_FILENAME,
    MANIFEST_FILENAME,
    PARENT_CACHE_FILENAME,
    RUNTIME_RECIPE_VERSION,
    normalize_test_results,
    sha256_file,
    sha256_text,
    sha256_tree,
)
from swebench.harness.namerts.runtime import (
    copy_from_container,
    make_harness_test_command_prefix,
    make_install_script,
    make_test_script,
)
from swebench.harness.utils import load_swebench_dataset, make_test_spec, str2bool
from swebench.image_builder.constants import CONTAINER_USER, CONTAINER_WORKDIR
from swebench.logger import close_logger, setup_logger


SUPPORTED_PYTHON_MINORS = {
    "3.6": ("environment_36.yml", "NameRTS36"),
    "3.7": ("environment_37.yml", "NameRTS37"),
    "3.8": ("environment_38.yml", "NameRTS38"),
    "3.9": ("environment_39.yml", "NameRTS39"),
    "3.10": ("environment_310.yml", "NameRTS310"),
    "3.11": ("environment_311.yml", "NameRTS311"),
    "3.12": ("environment_312.yml", "NameRTS312"),
    "3.13": ("environment_313.yml", "NameRTS313"),
}
CONDA_CHANNEL_MIRRORS = {
    "defaults": "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main",
    "conda-forge": "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge",
}
PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_NAMERTS_SOURCE = Path(__file__).resolve().parents[3] / "NameRTS"
_EXIT_MARKER = "__NAMERTS_EXIT_CODE__="


def _decode_exec(result, description: str) -> str:
    output = result.output.decode(errors="replace").strip()
    if result.exit_code != 0:
        raise RuntimeError(
            f"{description} failed with exit code {result.exit_code}: {output}"
        )
    return output


def _source_files(source_root: Path, environment_file: str) -> list[Path]:
    files = [
        path.relative_to(source_root)
        for path in (source_root / "src").rglob("*.py")
        if "__pycache__" not in path.parts
        and "pytest-xdist-isolation" not in path.parts
        and "build" not in path.parts
        and not any(part.endswith(".egg-info") for part in path.parts)
    ]
    files.append(Path(environment_file))
    return files


def _runtime_fingerprint(
    source_root: Path, environment_file: str, official_image_id: str
) -> tuple[str, str]:
    source_sha256 = sha256_tree(
        source_root, _source_files(source_root, environment_file)
    )
    bridge_sha256 = sha256_file(Path(__file__).with_name("container_bridge.py"))
    digest = hashlib.sha256()
    for value in (
        RUNTIME_RECIPE_VERSION,
        source_sha256,
        bridge_sha256,
        official_image_id,
    ):
        digest.update(value.encode())
        digest.update(b"\0")
    return source_sha256, digest.hexdigest()


def _runtime_image_name(instance_id: str, arch: str, fingerprint: str) -> str:
    instance = instance_id.lower().replace("__", "-").replace("_", "-")
    display_arch = "x86_64" if arch == "amd64" else arch
    return f"sweb.namerts.{display_arch}.{instance}:{fingerprint[:12]}"


def _remove_superseded_runtime_images(client, image_name: str, logger) -> None:
    """Keep only the current fingerprint tag for one NameRTS instance image."""
    repository = image_name.rsplit(":", 1)[0]
    current = client.images.get(image_name)
    for image in client.images.list(name=repository):
        for tag in list(image.tags):
            if not tag.startswith(repository + ":") or tag == image_name:
                continue
            try:
                client.images.remove(tag, force=False)
                logger.info(
                    "Removed superseded NameRTS runtime image tag %s (current %s)",
                    tag,
                    current.id,
                )
            except (docker.errors.APIError, docker.errors.ImageNotFound) as error:
                # Never force-remove an image used by an external container.
                logger.warning(
                    "Could not remove superseded NameRTS runtime image tag %s: %s",
                    tag,
                    error,
                )


def _copy_namerts_source(source_root: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name
            in {".git", "__pycache__", "runtime", "pytest-xdist-isolation", "build"}
            or name.endswith(".egg-info")
            or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(source_root / "src", destination / "src", ignore=ignore)
    shutil.copy2(
        Path(__file__).with_name("container_bridge.py"),
        destination / "swebench_bridge.py",
    )


def _write_mirrored_environment(source: Path, destination: Path) -> None:
    """Use explicit mirrors without changing the NameRTS submodule checkout."""
    lines = source.read_text().splitlines()
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            channel = stripped[2:]
            if channel in CONDA_CHANNEL_MIRRORS:
                indentation = line[: len(line) - len(line.lstrip())]
                line = f"{indentation}- {CONDA_CHANNEL_MIRRORS[channel]}"
        rewritten.append(line)
    if not any(line.strip() == "- nodefaults" for line in rewritten):
        rewritten.insert(rewritten.index("dependencies:"), "  - nodefaults")
    destination.write_text("\n".join(rewritten) + "\n")


def _ensure_official_image(client, image_name: str, timeout: int, logger):
    try:
        return client.images.get(image_name)
    except docker.errors.ImageNotFound:
        logger.info("Pulling official image %s", image_name)
    result = subprocess.run(
        ["docker", "pull", image_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not pull official image {image_name}: {result.stdout.strip()}"
        )
    return client.images.get(image_name)


def _remove_existing_instance_containers(
    client, instance_id: str, purpose: str, logger
) -> None:
    """Remove containers left by an interrupted run of the same stage."""
    prefix = f"sweb.namerts.{purpose}.{instance_id.lower()}."
    for container in client.containers.list(all=True, filters={"name": prefix}):
        if not container.name.startswith(prefix):
            continue
        try:
            container.remove(force=True)
            logger.info("Removed stale NameRTS container %s", container.name)
        except docker.errors.NotFound:
            pass


def _create_container(client, image: str, instance_id: str, purpose: str, logger):
    _remove_existing_instance_containers(client, instance_id, purpose, logger)
    suffix = f"{os.getpid()}.{time.time_ns()}"
    container = client.containers.create(
        image=image,
        name=f"sweb.namerts.{purpose}.{instance_id.lower()}.{suffix}",
        labels={
            "com.swebench.namerts.instance_id": instance_id,
            "com.swebench.namerts.purpose": purpose,
        },
        user=CONTAINER_USER,
        detach=True,
        command="tail -f /dev/null",
        cap_add=["SYS_ADMIN"],
    )
    container.start()
    return container


def _inspect_official_image(client, image: str, instance_id: str, logger):
    container = None
    try:
        container = _create_container(client, image, instance_id, "inspect", logger)
        repo_head = _decode_exec(
            container.exec_run("git rev-parse HEAD", workdir=CONTAINER_WORKDIR),
            "reading repository HEAD",
        )
        python_version = _decode_exec(
            container.exec_run(
                [
                    "/opt/miniconda3/envs/testbed/bin/python",
                    "-S",
                    "-c",
                    "import platform; print(platform.python_version())",
                ]
            ),
            "reading testbed Python version",
        )
        parts = python_version.split(".")
        if len(parts) < 2:
            raise RuntimeError(f"Unexpected Python version: {python_version}")
        return repo_head, python_version, ".".join(parts[:2])
    finally:
        cleanup_container(client, container, logger)


def _build_runtime_image(
    client,
    *,
    instance_id: str,
    official_image: str,
    arch: str,
    source_root: Path,
    environment_file: str,
    namerts_env: str,
    image_name: str,
    artifact_root: Path,
    force_rebuild: bool,
    logger,
) -> None:
    try:
        client.images.get(image_name)
        if not force_rebuild:
            return
        client.images.remove(image_name, force=True)
    except docker.errors.ImageNotFound:
        pass
    build_root = artifact_root / "_build"
    build_root.mkdir(parents=True, exist_ok=True)
    build_directory = Path(tempfile.mkdtemp(prefix=f"{instance_id}.", dir=build_root))
    try:
        _write_mirrored_environment(
            source_root / environment_file, build_directory / "environment.yml"
        )
        namerts_destination = build_directory / "namerts"
        namerts_destination.mkdir()
        _copy_namerts_source(source_root, namerts_destination)
        dockerfile = build_directory / "Dockerfile"
        dockerfile.write_text(
            f"""FROM {official_image}

USER root
COPY environment.yml /opt/namerts-environment.yml
ENV PIP_INDEX_URL={PIP_INDEX_URL}
ENV PIP_DEFAULT_TIMEOUT=300
RUN /bin/bash -lc "source /opt/miniconda3/etc/profile.d/conda.sh && conda env create --file /opt/namerts-environment.yml"
COPY namerts /opt/namerts
RUN /opt/miniconda3/envs/testbed/bin/python -m pip install --no-deps /opt/namerts/src/instrumentor
RUN test "$(/opt/miniconda3/envs/{namerts_env}/bin/python -S -c 'import sys; print(str(sys.version_info[0]) + \".\" + str(sys.version_info[1]))')" = "$(/opt/miniconda3/envs/testbed/bin/python -S -c 'import sys; print(str(sys.version_info[0]) + \".\" + str(sys.version_info[1]))')"
WORKDIR /testbed
"""
        )
        platform = "linux/amd64" if arch == "amd64" else "linux/arm64/v8"
        command = [
            "docker",
            "buildx",
            "build",
            f"--platform={platform}",
            f"--tag={image_name}",
            f"--file={dockerfile}",
            "--progress=plain",
            "--load",
        ]
        if force_rebuild:
            command.append("--no-cache")
        command.append(str(build_directory))
        build_log = build_root / f"{instance_id}.log"
        logger.info("Building NameRTS runtime image %s", image_name)
        with build_log.open("w") as output:
            result = subprocess.run(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if result.returncode != 0:
            raise RuntimeError(f"Runtime image build failed; see {build_log}")
    finally:
        shutil.rmtree(build_directory, ignore_errors=True)


def _run_checked_container(container, command: str, timeout: int):
    script = (
        f"{command}\n"
        "status=$?\n"
        f"printf '\\n{_EXIT_MARKER}%s\\n' \"$status\"\n"
        'exit "$status"'
    )
    output, timed_out, runtime = exec_run_with_timeout(
        container, "/bin/bash -lc " + shlex.quote(script), timeout
    )
    if timed_out:
        raise RuntimeError(f"NameRTS preparation timed out after {timeout} seconds")
    match = re.search(rf"\n{_EXIT_MARKER}(\d+)\s*$", output)
    if match is None:
        raise RuntimeError("NameRTS preparation produced no exit status")
    if int(match.group(1)) != 0:
        raise RuntimeError(
            f"NameRTS preparation exited with code {match.group(1)}:\n"
            f"{output[: match.start()][-4000:]}"
        )
    return output[: match.start()], runtime


def _run_preparation(
    client,
    *,
    instance_id: str,
    runtime_image: str,
    namerts_env: str,
    repo_head: str,
    project_name: str,
    golden_patch: Path,
    install_script: Path,
    test_script: Path,
    test_command: Path,
    staging: Path,
    logger,
    workers: int,
    per_file_timeout: int,
    total_timeout: int,
) -> float:
    container = None
    try:
        container = _create_container(
            client, runtime_image, instance_id, "prepare", logger
        )
        copy_to_container(container, golden_patch, PurePosixPath("/tmp/golden.patch"))
        copy_to_container(container, install_script, PurePosixPath("/tmp/install.sh"))
        copy_to_container(container, test_script, PurePosixPath("/tmp/test-setup.sh"))
        copy_to_container(
            container, test_command, PurePosixPath("/tmp/test-command.txt")
        )
        arguments = [
            "conda",
            "run",
            "--no-capture-output",
            "-n",
            namerts_env,
            "python",
            "/opt/namerts/swebench_bridge.py",
            "prepare",
            "--repo-root",
            CONTAINER_WORKDIR,
            "--project-name",
            project_name,
            "--parent",
            repo_head,
            "--workers",
            str(workers),
            "--per-file-timeout",
            str(per_file_timeout),
            "--golden-patch",
            "/tmp/golden.patch",
            "--install-script",
            "/tmp/install.sh",
            "--test-script",
            "/tmp/test-setup.sh",
            "--test-command",
            "/tmp/test-command.txt",
            "--cache-output",
            "/tmp/parent_cache.tar.gz",
            "--baseline-output",
            "/tmp/golden_baseline.json",
        ]
        command = " ".join(shlex.quote(argument) for argument in arguments)
        output, runtime = _run_checked_container(container, command, total_timeout)
        (staging / "preparation.log").write_text(output)
        logger.info("NameRTS preparation runtime: %.2f seconds", runtime)
        copy_from_container(
            container,
            PurePosixPath("/tmp/parent_cache.tar.gz"),
            staging / PARENT_CACHE_FILENAME,
        )
        copy_from_container(
            container,
            PurePosixPath("/tmp/golden_baseline.json"),
            staging / GOLDEN_BASELINE_FILENAME,
        )
        return runtime
    finally:
        cleanup_container(client, container, logger)


def prepare_instance(
    client,
    instance: dict,
    artifact_root: Path | str,
    namerts_source: Path | str,
    *,
    workers: int = 4,
    per_file_timeout: int = 120,
    total_timeout: int = 86_400,
    force_rebuild: bool = False,
    image_pull_timeout: int = 900,
) -> Path:
    artifact_root = Path(artifact_root).expanduser().resolve()
    source_root = Path(namerts_source).expanduser().resolve()
    if not (source_root / "src" / "namebdp.py").is_file():
        raise ValueError(
            f"NameRTS source is not initialized at {source_root}; run "
            "`git submodule update --init --recursive`"
        )
    test_spec = make_test_spec(instance)
    if instance.get("language", "py") != "py":
        raise ValueError(f"NameRTS only supports Python: {test_spec.instance_id}")
    artifact_root.mkdir(parents=True, exist_ok=True)
    destination = artifact_root / test_spec.instance_id
    if destination.exists() and not force_rebuild:
        raise FileExistsError(
            f"Artifact already exists: {destination}; use --force_rebuild true"
        )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{test_spec.instance_id}.tmp.", dir=artifact_root)
    )
    logger = setup_logger(
        f"namerts.{test_spec.instance_id}", staging / "prepare_instance.log"
    )
    preparation_started = time.monotonic()
    try:
        official_image_started = time.monotonic()
        official_image = _ensure_official_image(
            client, test_spec.image, image_pull_timeout, logger
        )
        official_image_seconds = time.monotonic() - official_image_started
        repo_head, python_version, python_minor = _inspect_official_image(
            client, test_spec.image, test_spec.instance_id, logger
        )
        if python_minor not in SUPPORTED_PYTHON_MINORS:
            raise ValueError(
                f"Unsupported NameRTS Python minor {python_minor} for {test_spec.instance_id}"
            )
        environment_file, namerts_env = SUPPORTED_PYTHON_MINORS[python_minor]
        if not (source_root / environment_file).is_file():
            raise FileNotFoundError(source_root / environment_file)
        source_sha256, fingerprint = _runtime_fingerprint(
            source_root, environment_file, official_image.id
        )
        arch = official_image.attrs.get("Architecture", "amd64")
        runtime_image = _runtime_image_name(test_spec.instance_id, arch, fingerprint)
        try:
            client.images.get(runtime_image)
            runtime_image_reused = not force_rebuild
        except docker.errors.ImageNotFound:
            runtime_image_reused = False
        image_build_started = time.monotonic()
        _build_runtime_image(
            client,
            instance_id=test_spec.instance_id,
            official_image=test_spec.image,
            arch=arch,
            source_root=source_root,
            environment_file=environment_file,
            namerts_env=namerts_env,
            image_name=runtime_image,
            artifact_root=artifact_root,
            force_rebuild=force_rebuild,
            logger=logger,
        )
        _remove_superseded_runtime_images(client, runtime_image, logger)
        image_build_seconds = time.monotonic() - image_build_started
        runtime_image_object = client.images.get(runtime_image)
        golden_patch = staging / "golden.patch"
        test_patch = staging / "test.patch"
        golden_patch.write_text(instance["patch"])
        test_patch.write_text(instance["test_patch"])
        install_script = staging / "install.sh"
        install_script.write_text(
            make_install_script(test_spec.eval_script, instance["base_commit"])
        )
        test_script = staging / "test-setup.sh"
        test_script.write_text(
            make_test_script(test_spec.eval_script, instance["base_commit"])
        )
        test_command = staging / "test-command.txt"
        harness_test_command = make_harness_test_command_prefix(
            test_spec.eval_script, instance["test_patch"], test_spec.repo
        )
        test_command.write_text(harness_test_command + "\n")
        container_preparation_seconds = _run_preparation(
            client,
            instance_id=test_spec.instance_id,
            runtime_image=runtime_image,
            namerts_env=namerts_env,
            repo_head=repo_head,
            project_name=test_spec.repo.rsplit("/", 1)[-1],
            golden_patch=golden_patch,
            install_script=install_script,
            test_script=test_script,
            test_command=test_command,
            staging=staging,
            logger=logger,
            workers=workers,
            per_file_timeout=per_file_timeout,
            total_timeout=total_timeout,
        )
        baseline_path = staging / GOLDEN_BASELINE_FILENAME
        baseline = json.loads(baseline_path.read_text())
        baseline.update(normalize_test_results(test_spec, baseline))
        baseline.update(
            {
                "instance_id": test_spec.instance_id,
                "repo": test_spec.repo,
                "base_commit": instance["base_commit"],
                "golden_patch_sha256": sha256_file(golden_patch),
                "test_patch_sha256": sha256_file(test_patch),
            }
        )
        baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True))
        manifest = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "status": "complete",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "swebench_version": swebench.__version__,
            "instance_id": test_spec.instance_id,
            "repo": test_spec.repo,
            "base_commit": instance["base_commit"],
            "language": "py",
            "dataset": {
                "eval_script_sha256": sha256_text(test_spec.eval_script),
                "log_parser": test_spec.log_parser,
                "eval_type": test_spec.eval_type,
            },
            "runtime": {
                "official_image": test_spec.image,
                "official_image_id": official_image.id,
                "image": runtime_image,
                "image_id": runtime_image_object.id,
                "repo_head": repo_head,
                "python_version": python_version,
                "python_minor": python_minor,
                "namerts_env": namerts_env,
                "project_name": test_spec.repo.rsplit("/", 1)[-1],
                "source_sha256": source_sha256,
                "fingerprint": fingerprint,
                "recipe_version": RUNTIME_RECIPE_VERSION,
            },
            "preparation": {
                "workers": workers,
                "per_file_timeout": per_file_timeout,
                "total_timeout": total_timeout,
                "golden_patch_sha256": sha256_file(golden_patch),
                "test_patch_sha256": sha256_file(test_patch),
                "discovered_test_files": baseline["test_count"],
                "golden_failure_count": len(baseline["failure_ids"]),
                "golden_collection_failure_count": len(
                    baseline["collection_failed_files"]
                ),
                "golden_timeout_count": baseline["timed_out_processes"],
                "test_execution": "per_file",
                "test_process_isolation": baseline["test_process_isolation"],
                "harness_test_command_sha256": sha256_text(harness_test_command),
                "timing": {
                    "official_image_seconds": official_image_seconds,
                    "runtime_image_build_seconds": image_build_seconds,
                    "runtime_image_reused": runtime_image_reused,
                    "container_preparation_seconds": container_preparation_seconds,
                    "total_seconds": time.monotonic() - preparation_started,
                    **baseline.get("timing", {}),
                },
            },
            "files": {
                "parent_cache": {
                    "name": PARENT_CACHE_FILENAME,
                    "sha256": sha256_file(staging / PARENT_CACHE_FILENAME),
                },
                "golden_baseline": {
                    "name": GOLDEN_BASELINE_FILENAME,
                    "sha256": sha256_file(baseline_path),
                },
            },
        }
        (staging / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True)
        )
        golden_patch.unlink()
        test_patch.unlink()
        install_script.unlink()
        test_script.unlink()
        test_command.unlink()
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
        return destination
    except Exception:
        logger.error(traceback.format_exc())
        failure_root = artifact_root / "_failures" / test_spec.instance_id
        failure_root.mkdir(parents=True, exist_ok=True)
        failure_destination = failure_root / datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ"
        )
        os.replace(staging, failure_destination)
        print(f"NameRTS diagnostics preserved at {failure_destination}")
        raise
    finally:
        close_logger(logger)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _resolve_dataset_path(dataset_name: str, split: str) -> str:
    root = Path(dataset_name).expanduser()
    if not root.is_dir():
        return dataset_name
    candidates = sorted((root / "data").glob(f"{split}-*.parquet"))
    if len(candidates) == 1:
        return str(candidates[0])
    return dataset_name


def main(
    dataset_name: str,
    split: str,
    instance_ids: list[str],
    artifact_root: str,
    namerts_source: str,
    workers: int,
    per_file_timeout: int,
    total_timeout: int,
    force_rebuild: bool,
    image_pull_timeout: int,
) -> None:
    instances = load_swebench_dataset(
        _resolve_dataset_path(dataset_name, split), split, instance_ids
    )
    client = docker.from_env()
    for index, instance in enumerate(instances, start=1):
        print(
            f"[NameRTS prepare] {index}/{len(instances)} {instance['instance_id']}",
            flush=True,
        )
        destination = prepare_instance(
            client,
            instance,
            artifact_root,
            namerts_source,
            workers=workers,
            per_file_timeout=per_file_timeout,
            total_timeout=total_timeout,
            force_rebuild=force_rebuild,
            image_pull_timeout=image_pull_timeout,
        )
        print(f"Prepared {destination}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare NameRTS caches and golden full-suite baselines."
    )
    parser.add_argument("--dataset_name", required=True)
    parser.add_argument("--split", default="test")
    parser.add_argument("--instance_ids", nargs="+", required=True)
    parser.add_argument("--artifact_root", default="namerts_artifacts")
    parser.add_argument("--namerts_source", default=str(DEFAULT_NAMERTS_SOURCE))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per_file_timeout", type=int, default=120)
    parser.add_argument("--total_timeout", type=int, default=86_400)
    parser.add_argument("--force_rebuild", type=str2bool, default=False)
    parser.add_argument("--image_pull_timeout", type=int, default=900)
    return parser.parse_args()


if __name__ == "__main__":
    main(**vars(parse_args()))
