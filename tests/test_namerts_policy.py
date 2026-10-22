import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from swebench.harness.namerts.policy import read_policy, record_slow_tests
from swebench.harness.namerts.prepare import _instrumentation_testbed_install
from swebench.harness.namerts.artifacts import filter_selection_for_baseline


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
