import json
import logging

from eurus.config import LoggingConfig
from eurus.logs import get_logger, redact, setup_logging, shutdown_logging


def test_redact_known_patterns():
    text = "sk-abcdefgh12345678 Bearer abcdefghijkl token=abc123 gsk_abcdefgh1234"
    cleaned = redact(text)
    for leaked in ("sk-abcdefgh", "abcdefghijkl", "abc123", "gsk_abcdefgh"):
        assert leaked not in cleaned


def test_redact_explicit_secrets():
    assert "supersecretvalue" not in redact("v supersecretvalue", ["supersecretvalue"])


def test_file_log_is_json_and_redacted(tmp_path):
    target = tmp_path / "logs" / "eurus.log"
    try:
        logger = setup_logging(LoggingConfig("INFO", str(target), True), secrets=["topsecret123"])
        get_logger("test").info("hello topsecret123")
        for handler in logger.handlers:
            handler.flush()
        line = json.loads(target.read_text(encoding="utf-8").strip().splitlines()[-1])
    finally:
        shutdown_logging()
    assert line["level"] == "INFO"
    assert "hello" in line["message"]
    assert "topsecret123" not in line["message"]


def test_shutdown_logging_restores_logger_level(tmp_path):
    try:
        logger = setup_logging(LoggingConfig("DEBUG", str(tmp_path / "eurus.log"), True))
        assert logger.level == logging.DEBUG
    finally:
        shutdown_logging()
    assert logging.getLogger(logger.name).level == logging.NOTSET


def test_console_logging_can_be_disabled_without_disabling_file_logs(tmp_path, capsys):
    target = tmp_path / "eurus.log"
    try:
        logger = setup_logging(LoggingConfig("INFO", str(target), False), console=False)
        get_logger("gateway.telegram.adapter").warning("gateway status")
        for handler in logger.handlers:
            handler.flush()
        captured = capsys.readouterr()
        contents = target.read_text(encoding="utf-8")
    finally:
        shutdown_logging()
    assert "gateway status" not in captured.out
    assert "gateway status" not in captured.err
    assert "gateway status" in contents
