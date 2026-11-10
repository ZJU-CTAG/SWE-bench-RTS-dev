import inspect
from types import SimpleNamespace

import pytest

from swebench.harness.namerts import container_bridge, runtime
from swebench.harness import run_evaluation


@pytest.mark.parametrize("workers", [1, 4, 7])
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


def test_public_defaults_and_reject_invalid_workers():
    for function in (run_evaluation.main, run_evaluation.run_instance, run_evaluation.run_instances):
        assert inspect.signature(function).parameters["namerts_selection_workers"].default == 4
    with pytest.raises(ValueError, match="positive"):
        runtime.run_selection(None, None, None, patch_base="base", workers=1,
                              per_file_timeout=180, timeout=3600, selection_workers=0)


def test_legacy_image_rejected_before_enabling_hardcoded_pool(monkeypatch):
    monkeypatch.setattr(container_bridge, "configure_timeout", lambda value: SimpleNamespace())
    with pytest.raises(RuntimeError, match="lacks configurable"):
        container_bridge.command_select(SimpleNamespace(selection_workers=4, per_file_timeout=180))
