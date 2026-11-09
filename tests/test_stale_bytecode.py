import os
import sys

from eurus.coding.validation import run_validation
from eurus.execution.host import scrubbed_env

OLD = "HOST = 'old.example'\nPORT = 8080\n"
NEW = "HOST = 'new.example'\nPORT = 8080\n"
TEST = "from config import HOST\n\n\ndef test_host():\n    assert HOST == 'new.example'\n"
CHECK = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")


def test_same_size_rewrite_in_same_second_is_not_stale(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(OLD, encoding="utf-8")
    (tmp_path / "test_config_task.py").write_text(TEST, encoding="utf-8")
    assert not run_validation(tmp_path, CHECK, timeout_seconds=60).passed
    stamp = config.stat().st_mtime_ns
    config.write_text(NEW, encoding="utf-8")
    os.utime(config, ns=(stamp, stamp))
    assert run_validation(tmp_path, CHECK, timeout_seconds=60).passed


def test_validation_and_host_env_disable_bytecode_writes():
    assert scrubbed_env()["PYTHONDONTWRITEBYTECODE"] == "1"
