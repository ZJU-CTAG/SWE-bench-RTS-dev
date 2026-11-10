"""Preparation CLI scope and artifact reuse, without running Docker."""
import json
import sys
from unittest.mock import Mock

import docker
import pytest

from swebench.harness.namerts import prepare
from test_namerts import make_instance, make_spec, write_artifact


def args(monkeypatch, *extra):
    monkeypatch.setattr(sys, "argv", ["prepare", "--dataset_name", "dataset.json", *extra])
    return prepare.parse_args()


def test_optional_instance_ids(monkeypatch):
    assert args(monkeypatch).instance_ids is None
    assert args(monkeypatch, "--instance_ids", "a", "b").instance_ids == ["a", "b"]
    with pytest.raises(SystemExit):
        args(monkeypatch, "--instance_ids")


@pytest.mark.parametrize("filtered", [False, True])
def test_prepare_scope_and_unsupported_skip(tmp_path, monkeypatch, filtered, capsys):
    rows = [make_instance(), {**make_instance(), "instance_id": "django__django-7530"},
            {**make_instance(), "instance_id": "pytest-dev__pytest-2"}]
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(rows))
    options = args(monkeypatch)
    options.dataset_name = str(dataset)
    options.slow_test_threshold = 150
    options.instance_ids = [rows[2]["instance_id"]] if filtered else None
    client = Mock()
    monkeypatch.setattr(prepare.docker, "from_env", lambda: client)
    initialize = Mock(return_value=tmp_path)
    monkeypatch.setattr(prepare, "prepare_instance", initialize)
    prepare.main(**vars(options))
    expected = [rows[2]] if filtered else [rows[0], rows[2]]
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


def test_prepare_closes_client_on_failure(monkeypatch):
    client = Mock()
    monkeypatch.setattr(prepare.docker, "from_env", lambda: client)
    monkeypatch.setattr(prepare, "load_swebench_dataset", lambda *a: [make_instance()])
    initialize = Mock(side_effect=RuntimeError("failed"))
    monkeypatch.setattr(prepare, "prepare_instance", initialize)
    with pytest.raises(RuntimeError, match="failed"):
        prepare.main(**vars(args(monkeypatch)))
    initialize.assert_called_once()
    client.close.assert_called_once()
