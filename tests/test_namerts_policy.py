import json
import os
import subprocess
import sys
import types
import shlex
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from swebench.harness.namerts.policy import read_policy, record_slow_tests
from swebench.harness.namerts.prepare import _instrumentation_testbed_install
from swebench.harness.namerts.artifacts import filter_selection_for_baseline


def test_checked_in_policy_has_unique_keys_and_joint_exclusion():
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, "Duplicate policy key: " + key
            result[key] = value
        return result

    path = Path(__file__).resolve().parents[1] / "swebench/harness/namerts/instance_policy.json"
    policy = json.loads(path.read_text(), object_pairs_hook=unique_pairs)
    instance = policy["instances"]["sympy__sympy-22080"]
    assert "sympy/physics/mechanics/tests/test_joint.py" in instance["skip_test_files"]
    assert instance["slow_test_files"]
    # This revision omits raises in its legacy py.test branch. pytest 7.4.4
    # uses the fallback implementation; a blanket 4.6 pin breaks collection.
    sympy = policy["instances"]["sympy__sympy-11618"]
    assert sympy["pytest_version"] == "7.4.4"
    assert "py_version" not in sympy


@pytest.mark.parametrize("project,expected", [
    ("seaborn", ["-k", "not test_load_datasets and not test_load_cached_datasets and not test_dogplot"]),
    ("pandas", ["-m", "not single_cpu"]),
    ("matplotlib", ["-p", "matplotlib.testing.compare"]),
    ("sphinx", ["-k", "not test_latex_images"]),
    ("sympy", []),
    ("django", []),
    ("sklearn", []),
    ("scikit-learn", []),
])
def test_shared_filters_across_rts_phases(tmp_path, monkeypatch, project, expected):
    from swebench.harness.namerts.container_bridge import project_path, write_test_script

    source = Path(__file__).resolve().parents[1] / "NameRTS/src/config.py"
    if not source.exists():
        pytest.skip("Initialize the NameRTS submodule to test shared project policy")
    spec = importlib.util.spec_from_file_location("src.config", source)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    package = types.ModuleType("src")
    package.config = config
    monkeypatch.setitem(sys.modules, "src", package)
    monkeypatch.setitem(sys.modules, "src.config", config)
    monkeypatch.setenv("NAMERTS_PROJECT_NAME", project)
    project_path(str(tmp_path), project)  # Physical testbed basename is not project name.
    assert config.get_test_filter_args(str(tmp_path)) == expected
    instrumentation = shlex.split(config.get_test_command(str(tmp_path)))
    if expected:
        assert instrumentation[instrumentation.index(expected[0]):][:2] == expected
    official = "pytest --no-header -rA"
    command = tmp_path / "official-command"
    command.write_text(official)
    for scope in ("golden_full_suite", "selected"):
        script = tmp_path / (scope + ".sh")
        payload = write_test_script(str(tmp_path), ["tests/test_utils.py"],
                                    str(command), str(script), scope)
        assert payload["test_filter_args"] == expected
        assert shlex.split(payload["command"]) == shlex.split(official) + expected
        assert command.read_text() == official  # Never rewrite F2P/P2P input.
        assert ("export LOKY_MAX_CPU_COUNT=1" in script.read_text()) == (project in ("sklearn", "scikit-learn"))
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_grouped_runner_uses_generic_filter_api(tmp_path, monkeypatch):
    from swebench.harness.namerts.container_bridge import write_test_script

    config = types.ModuleType("src.config")
    config.get_test_target = lambda root, file: file
    config.get_test_filter_args = lambda root: ["--ignore", "tests/network cases.py"]
    monkeypatch.setitem(sys.modules, "src", types.ModuleType("src"))
    monkeypatch.setitem(sys.modules, "src.config", config)
    command = tmp_path / "command"
    command.write_text("tox --current-env -e py -- -rA")
    for scope in ("golden_full_suite", "selected"):
        payload = write_test_script("/testbed", ["tests/test_a.py"], str(command),
                                    str(tmp_path / "run.sh"), scope)
        assert shlex.split(payload["command"])[-2:] == config.get_test_filter_args(None)


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


def test_policy_rejects_escape(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"schema_version": 1, "instances": {}}')
    with pytest.raises(ValueError, match="Invalid slow test path"):
        record_slow_tests("case", ["../escape.py"], path)
    assert read_policy("case", path)["slow_test_files"] == []


@pytest.mark.parametrize("iid", ["15976", "16792", "17655"])
def test_old_sympy_dependency_pins(iid):
    command = _instrumentation_testbed_install("sympy__sympy-" + iid, "NameRTS39")
    assert "pytest==4.6.11 py==1.11.0" in command


def test_project_dependencies_and_static_exclusions():
    assert "GitPython==3.1.43" in _instrumentation_testbed_install("pylint-dev__pylint-6528", "NameRTS39")
    assert "xmlschema==1.10.0" in _instrumentation_testbed_install("pytest-dev__pytest-7982", "NameRTS39")
    assert "pytest==4.6.11" in _instrumentation_testbed_install("sympy__sympy-18211", "NameRTS39")
    assert "pytest==7.4.4" in _instrumentation_testbed_install("sympy__sympy-24443", "NameRTS39")
    policy = read_policy("pydata__xarray-6599")
    assert len(policy["skip_test_files"]) == 11
    assert not read_policy("pydata__xarray-7229")["skip_test_files"]
    filtered = filter_selection_for_baseline(
        {"tests_to_run": ["xarray/tests/test_dataset.py", "xarray/tests/test_units.py"]}, policy)
    assert filtered["tests_to_run"] == ["xarray/tests/test_units.py"]
    assert filtered["compatibility_excluded_tests"] == ["xarray/tests/test_dataset.py"]


def test_rts_ignores_collection_and_runtime_warnings(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\nfilterwarnings = error\n")
    (tmp_path / "test_warning.py").write_text(
        "import warnings, pytest\n"
        "warnings.warn('during collection', DeprecationWarning)\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    warnings.warn('setup', UserWarning)\n"
        "    yield\n"
        "    warnings.warn('teardown', RuntimeWarning)\n"
        "def test_phases(resource):\n"
        "    warnings.warn('runtime', FutureWarning)\n"
        "def test_warning():\n"
        "    warnings.warn('during test', DeprecationWarning)\n"
        "def test_explicit_capture():\n"
        "    with pytest.warns(UserWarning):\n"
        "        warnings.warn('expected', UserWarning)\n")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    original = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=tmp_path,
                              env=env, capture_output=True, text=True)
    assert original.returncode == 2
    fixed = subprocess.run([sys.executable, "-m", "pytest", "-W", "ignore", "-q"],
                           cwd=tmp_path, env=env, capture_output=True, text=True)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "3 passed" in fixed.stdout
    assert "warnings summary" not in fixed.stdout.lower()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_sympy_runner_restores_configuration(tmp_path, exit_code):
    from swebench.harness.namerts.container_bridge import SYMPY_WARNING_RUNNER
    runner = tmp_path / "sympy/utilities/runtests.py"
    runner.parent.mkdir(parents=True)
    original = b'import warnings\nwarnings.filterwarnings("error", category=DeprecationWarning)\n'
    runner.write_bytes(original)
    program = (
        "import runpy,warnings,sys; runpy.run_path('sympy/utilities/runtests.py'); "
        "warnings.warn('legacy runner', DeprecationWarning); sys.exit(%d)" % exit_code
    )
    result = subprocess.run([sys.executable, "-c", SYMPY_WARNING_RUNNER,
                             sys.executable, "-c", program], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    assert not result.stderr
    assert runner.read_bytes() == original


def test_warning_commands_do_not_shadow_capture_bootstrap():
    from swebench.harness.namerts.container_bridge import rts_warning_command
    assert rts_warning_command("pytest -rA") == "pytest -rA -W ignore"
    assert rts_warning_command("tox --current-env -e py -- -rA").endswith("-- -rA -W ignore")
    assert rts_warning_command("python tests/runtests.py") == "python tests/runtests.py"
    for command in ["pytest", "bin/test --verbose"]:
        adapted = rts_warning_command(command)
        assert "PYTHONPATH" not in adapted
        assert "sitecustomize" not in adapted


def test_instrumentation_overrides_ascii_output(tmp_path, monkeypatch):
    from swebench.harness.namerts.container_bridge import project_path
    config = types.ModuleType("src.config")
    config.get_test_command = lambda path: shlex.quote(sys.executable) + " -c " + shlex.quote(
        "print('Creating tables\\u2026')")
    config.get_test_target = lambda path, file: file
    config.get_test_filter_args = lambda path: []
    config.INSTRUMENTATION_BLACKLIST = {}
    package = types.ModuleType("src")
    package.config = config
    monkeypatch.setitem(sys.modules, "src", package)
    monkeypatch.setitem(sys.modules, "src.config", config)
    project_path(str(tmp_path), "django")
    result = subprocess.run(config.get_test_command(str(tmp_path)), shell=True,
                            env={**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": "ascii"},
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8") == "Creating tables…\n"
