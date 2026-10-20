from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile

from pathlib import Path, PurePosixPath
from typing import Any
from unidiff import PatchSet

from swebench.harness.constants import (
    CONTAINER_PATCH_FILE,
    END_TEST_OUTPUT,
    NON_TEST_EXTS,
    START_TEST_OUTPUT,
    TEST_EXIT_CODE_VAR,
)
from swebench.harness.docker_utils import copy_to_container, exec_run_with_timeout
from swebench.harness.namerts.artifacts import NameRTSArtifact
from swebench.image_builder.constants import CONTAINER_WORKDIR


CONTAINER_BRIDGE = PurePosixPath("/opt/namerts/swebench_bridge.py")
CONTAINER_CACHE = PurePosixPath("/tmp/namerts-parent-cache.tar.gz")
CONTAINER_SELECTION = PurePosixPath("/tmp/namerts-selection.json")
CONTAINER_GOLDEN_BASELINE = PurePosixPath("/tmp/namerts-golden-baseline.json")
CONTAINER_TARGET_RESULTS = PurePosixPath("/tmp/namerts-target-results.json")
CONTAINER_TEST_SCRIPT = PurePosixPath("/tmp/namerts-test-setup.sh")
CONTAINER_INSTALL_SCRIPT = PurePosixPath("/tmp/namerts-install.sh")
CONTAINER_TEST_COMMAND = PurePosixPath("/tmp/namerts-test-command.txt")
_EXIT_MARKER = "__NAMERTS_EXIT_CODE__="


class NameRTSRuntimeError(RuntimeError):
    pass


def install_runtime_bridge(container) -> None:
    source = Path(__file__).with_name("container_bridge.py")
    # Upstream copy_to_container creates ``src.with_suffix('.tar')``. A shared
    # source therefore races across process-based instance workers. Give every
    # copy a private source path without changing the upstream helper.
    with tempfile.TemporaryDirectory(prefix="namerts-bridge-") as directory:
        private_source = Path(directory) / source.name
        shutil.copyfile(source, private_source)
        copy_to_container(container, private_source, CONTAINER_BRIDGE)


def copy_from_container(container, source: PurePosixPath, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream, _ = container.get_archive(str(source))
    temporary_tar = None
    temporary_output = destination.with_name(destination.name + ".tmp")
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".tar", dir=destination.parent, delete=False
        ) as archive:
            temporary_tar = Path(archive.name)
            for chunk in stream:
                archive.write(chunk)
        with tarfile.open(temporary_tar, "r") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and Path(member.name).name == source.name
            ]
            if len(members) != 1:
                raise NameRTSRuntimeError(
                    f"Container archive for {source} has {len(members)} matching files"
                )
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise NameRTSRuntimeError(f"Cannot extract {source} from container")
            with temporary_output.open("wb") as output:
                shutil.copyfileobj(extracted, output)
        os.replace(temporary_output, destination)
    finally:
        if temporary_tar is not None:
            temporary_tar.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)


def _bridge_command(
    artifact: NameRTSArtifact,
    command: str,
    *,
    workers: int,
    per_file_timeout: int,
    extra_args: list[str],
) -> str:
    arguments = [
        "conda",
        "run",
        "--no-capture-output",
        "-n",
        artifact.namerts_env,
        "python",
        str(CONTAINER_BRIDGE),
        command,
        "--repo-root",
        CONTAINER_WORKDIR,
        "--project-name",
        artifact.project_name,
        "--parent",
        artifact.repo_head,
        "--workers",
        str(workers),
        "--per-file-timeout",
        str(per_file_timeout),
        *extra_args,
    ]
    return " ".join(shlex.quote(argument) for argument in arguments)


def _run_bridge(container, command: str, timeout: int) -> tuple[str, float]:
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
        raise NameRTSRuntimeError(f"NameRTS command timed out after {timeout} seconds")
    match = re.search(rf"\n{_EXIT_MARKER}(\d+)\s*$", output)
    if match is None:
        raise NameRTSRuntimeError("NameRTS command produced no exit status")
    if int(match.group(1)) != 0:
        raise NameRTSRuntimeError(
            f"NameRTS command exited with code {match.group(1)}:\n"
            f"{output[: match.start()][-4000:]}"
        )
    return output[: match.start()], runtime


def split_eval_script(eval_script: str, base_commit: str) -> tuple[str, str]:
    """Split official setup into candidate-install and test-patch scripts."""
    lines = eval_script.splitlines()
    checkout = None
    for index, line in enumerate(lines):
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if fields[:2] == ["git", "checkout"] and base_commit in fields[2:]:
            checkout = index
            break
    if checkout is None:
        raise NameRTSRuntimeError(
            "Could not locate the test-patch checkout in the official eval script"
        )
    test_start = next(
        (
            index
            for index in range(checkout + 1, len(lines))
            if START_TEST_OUTPUT in lines[index]
        ),
        None,
    )
    if test_start is None:
        raise NameRTSRuntimeError(
            "Could not locate the test-output marker in the official eval script"
        )
    install_script = "\n".join(lines[:checkout]) + "\n"
    test_script = "\n".join(
        ["#!/bin/bash", "set -uxo pipefail", *lines[checkout:test_start], ""]
    )
    return install_script, test_script


def make_install_script(eval_script: str, base_commit: str) -> str:
    install_script = split_eval_script(eval_script, base_commit)[0]
    lines = []
    for line in install_script.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            fields = []
        is_pip_install = "pip" in fields and "install" in fields
        is_editable = "-e" in fields or "--editable" in fields
        if is_pip_install and is_editable:
            # Instance images already bind editable installs to the real /testbed.
            # Reinstalling can resolve newer, incompatible build dependencies.
            lines.append(
                ": 'NameRTS reuses the official editable /testbed installation'"
            )
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def make_test_script(eval_script: str, base_commit: str) -> str:
    return split_eval_script(eval_script, base_commit)[1]


def _test_patch_directives(test_patch: str, repo: str) -> list[str]:
    directives = []
    for patched_file in PatchSet(test_patch):
        path = patched_file.path
        if not path or any(path.endswith(extension) for extension in NON_TEST_EXTS):
            continue
        if repo == "django/django":
            if path.startswith("tests/"):
                path = path[len("tests/") :]
            if path.endswith(".py"):
                path = path[: -len(".py")]
            path = path.replace("/", ".")
        if path not in directives:
            directives.append(path)
    return directives


def make_harness_test_command_prefix(
    eval_script: str, test_patch: str, repo: str
) -> str:
    """Return the official test command with its PR-specific targets removed."""
    lines = eval_script.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if START_TEST_OUTPUT in line
        )
        end = next(
            index
            for index in range(start + 1, len(lines))
            if END_TEST_OUTPUT in lines[index]
        )
    except StopIteration as error:
        raise NameRTSRuntimeError(
            "Could not locate the official test command in the eval script"
        ) from error
    commands = [
        line
        for line in lines[start + 1 : end]
        if line.strip() and not line.lstrip().startswith(f"{TEST_EXIT_CODE_VAR}=")
    ]
    if len(commands) != 1:
        raise NameRTSRuntimeError(
            "Expected exactly one official test command between the output markers, "
            f"found {len(commands)}"
        )
    command = commands[0]
    directives = _test_patch_directives(test_patch, repo)
    if not directives:
        raise NameRTSRuntimeError("The test patch produced no test directives")

    stream = io.StringIO(command)
    lexer = shlex.shlex(stream, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = []
    try:
        while True:
            start_offset = stream.tell()
            token = lexer.get_token()
            if token == lexer.eof:
                break
            tokens.append((start_offset, token))
    except ValueError as error:
        raise NameRTSRuntimeError(
            f"Cannot parse the official test command: {error}"
        ) from error
    target_indexes = [
        index for index, (_, token) in enumerate(tokens) if token in directives
    ]
    if not target_indexes:
        raise NameRTSRuntimeError(
            "None of the test-patch directives occur in the official test command"
        )
    first_target_index = target_indexes[0]
    first_target = tokens[first_target_index][0]
    suffix_tokens = [token for _, token in tokens[first_target_index:]]
    unexpected = [token for token in suffix_tokens if token not in directives]
    if unexpected:
        raise NameRTSRuntimeError(
            "Official test command has unsupported tokens after its first test target: "
            + " ".join(unexpected)
        )
    prefix = command[:first_target].rstrip()
    if not prefix:
        raise NameRTSRuntimeError("Official test command has an empty runner prefix")
    return prefix


def run_install_script(
    container,
    eval_script: str,
    base_commit: str,
    log_dir: Path,
    timeout: int,
) -> None:
    path = log_dir / "namerts_install.sh"
    path.write_text(make_install_script(eval_script, base_commit))
    copy_to_container(container, path, CONTAINER_INSTALL_SCRIPT)
    try:
        output, _ = _run_bridge(
            container, f"/bin/bash {CONTAINER_INSTALL_SCRIPT}", timeout
        )
    except Exception as error:
        (log_dir / "namerts_install.log").write_text(str(error))
        raise
    (log_dir / "namerts_install.log").write_text(output)


def run_selection(
    container,
    artifact: NameRTSArtifact,
    log_dir: Path,
    *,
    patch_base: str,
    workers: int,
    per_file_timeout: int,
    timeout: int,
) -> dict[str, Any]:
    copy_to_container(container, artifact.cache_path, CONTAINER_CACHE)
    command = _bridge_command(
        artifact,
        "select",
        workers=workers,
        per_file_timeout=per_file_timeout,
        extra_args=[
            "--patch",
            CONTAINER_PATCH_FILE,
            "--patch-base",
            patch_base,
            "--cache",
            str(CONTAINER_CACHE),
            "--output",
            str(CONTAINER_SELECTION),
        ],
    )
    try:
        output, runtime = _run_bridge(container, command, timeout)
    except Exception as error:
        (log_dir / "namerts_selection.log").write_text(str(error))
        raise
    (log_dir / "namerts_selection.log").write_text(output)
    selection_path = log_dir / "namerts_selection.json"
    try:
        copy_from_container(container, CONTAINER_SELECTION, selection_path)
        selection = json.loads(selection_path.read_text())
    except Exception as error:
        raise NameRTSRuntimeError(
            f"NameRTS selection did not produce valid output: {error}"
        ) from error
    if selection.get("completed") is not True:
        raise NameRTSRuntimeError("NameRTS selection was not completed")
    selection["wall_time_seconds"] = runtime
    return selection


def apply_test_patch(
    container,
    eval_script: str,
    base_commit: str,
    log_dir: Path,
    timeout: int,
) -> None:
    path = log_dir / "namerts_test_setup.sh"
    path.write_text(make_test_script(eval_script, base_commit))
    copy_to_container(container, path, CONTAINER_TEST_SCRIPT)
    try:
        output, _ = _run_bridge(
            container, f"/bin/bash {CONTAINER_TEST_SCRIPT}", timeout
        )
    except Exception as error:
        (log_dir / "namerts_test_patch.log").write_text(str(error))
        raise
    (log_dir / "namerts_test_patch.log").write_text(output)


def run_selected_tests(
    container,
    artifact: NameRTSArtifact,
    log_dir: Path,
    *,
    test_command: str,
    workers: int,
    per_file_timeout: int,
    timeout: int,
) -> dict[str, Any]:
    selection_path = log_dir / "namerts_selection.json"
    if not selection_path.is_file():
        raise NameRTSRuntimeError("Missing filtered NameRTS selection")
    copy_to_container(container, selection_path, CONTAINER_SELECTION)
    copy_to_container(
        container,
        artifact.directory / artifact.manifest["files"]["golden_baseline"]["name"],
        CONTAINER_GOLDEN_BASELINE,
    )
    command_path = log_dir / "namerts_test_command.txt"
    command_path.write_text(test_command + "\n")
    copy_to_container(container, command_path, CONTAINER_TEST_COMMAND)
    command = _bridge_command(
        artifact,
        "run-tests",
        workers=workers,
        per_file_timeout=per_file_timeout,
        extra_args=[
            "--selection",
            str(CONTAINER_SELECTION),
            "--baseline",
            str(CONTAINER_GOLDEN_BASELINE),
            "--test-command",
            str(CONTAINER_TEST_COMMAND),
            "--output",
            str(CONTAINER_TARGET_RESULTS),
        ],
    )
    try:
        output, runtime = _run_bridge(container, command, timeout)
    except Exception as error:
        (log_dir / "namerts_test.log").write_text(str(error))
        raise
    (log_dir / "namerts_test.log").write_text(output)
    result_path = log_dir / "namerts_test_output.json"
    try:
        copy_from_container(container, CONTAINER_TARGET_RESULTS, result_path)
        payload = json.loads(result_path.read_text())
    except Exception as error:
        raise NameRTSRuntimeError(
            f"NameRTS selected tests did not produce valid output: {error}"
        ) from error
    if payload.get("completed") is not True:
        raise NameRTSRuntimeError("NameRTS selected tests were not completed")
    payload["wall_time_seconds"] = runtime
    return payload
