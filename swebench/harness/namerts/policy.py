"""Version-controlled, per-instance RTS exclusions and dependency pins."""
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

POLICY_PATH = Path(__file__).with_name("instance_policy.json")


def read_policy(instance_id, path=None):
    data = json.loads(Path(path or POLICY_PATH).read_text())
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported NameRTS instance policy")
    project = instance_id.rsplit("-", 1)[0]
    policy = {**data.get("projects", {}).get(project, {}),
              **data["instances"].get(instance_id, {})}
    files = policy.get("slow_test_files", [])
    skipped = policy.get("skip_test_files", {})
    if not isinstance(skipped, dict) or any(not isinstance(reason, str) or not reason for reason in skipped.values()):
        raise ValueError("Invalid skip test reasons")
    if not isinstance(files, list) or any(
        not isinstance(f, str) or PurePosixPath(f).is_absolute()
        or ".." in PurePosixPath(f).parts or not f.endswith(".py")
        for f in list(files) + list(skipped)
    ):
        raise ValueError("Invalid slow test path")
    for key in ("pytest_version", "py_version"):
        if key in policy and not re.fullmatch(r"\d+\.\d+\.\d+", policy[key]):
            raise ValueError("Invalid dependency pin")
    dependencies = policy.get("test_dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(pin, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+==\d+\.\d+\.\d+", pin)
        for pin in dependencies
    ):
        raise ValueError("Invalid test dependency pins")
    return {**policy, "slow_test_files": sorted(set(files)), "skip_test_files": skipped}


def record_slow_tests(instance_id, files, path=None):
    """Merge under a process lock; retain pins and other workers' observations."""
    path = Path(path or POLICY_PATH)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        policy = read_policy(instance_id, path)
        merged = sorted(set(policy["slow_test_files"]) | set(files))
        if merged == policy["slow_test_files"]:
            return
        data = json.loads(path.read_text())
        data["instances"][instance_id] = {**data["instances"].get(instance_id, {}), "slow_test_files": merged}
        fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            read_policy(instance_id, temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
