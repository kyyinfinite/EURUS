import os
import stat

import pytest

from eurus.bootstrap.configuration.env_manager import (
    DIR_PERMISSION,
    ENV_PERMISSION,
    GLOBAL_ENV_TEMPLATE,
    EnvFile,
    atomic_write,
    ensure_global_env,
    ensure_private_dir,
    global_env_path,
    tighten_permissions,
)
from eurus.bootstrap.configuration.secrets import (
    clean_state_payload,
    mask_secret,
    secrets_from_environ,
    validate_secret_format,
    validate_telegram_token,
)
from eurus.bootstrap.configuration.toml_manager import TomlManager, dumps
from eurus.bootstrap.models import BootstrapError
from eurus.dotenv import KEY_NAME


def test_atomic_write_sets_private_mode(tmp_path):
    target = tmp_path / "sub" / "file.txt"
    atomic_write(target, "secret", mode=ENV_PERMISSION)
    assert target.read_text(encoding="utf-8") == "secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "file.txt"
    atomic_write(target, "one", mode=0o644)
    atomic_write(target, "two", mode=0o644)
    assert target.read_text(encoding="utf-8") == "two"
    assert not list(tmp_path.glob(".*tmp*"))


def test_ensure_private_dir_mode(tmp_path):
    created = ensure_private_dir(tmp_path / "a" / "b")
    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_tighten_permissions_transitions(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o644)
    assert tighten_permissions(target) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert tighten_permissions(target) is False
    assert tighten_permissions(tmp_path / "missing") is False


def test_env_file_parses_quotes_export_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nexport A=1\nB='two words'\nC=\"has #hash\"\nD=plain\n", encoding="utf-8"
    )
    env = EnvFile.load(path)
    assert env.get("A") == "1"
    assert env.get("B") == "two words"
    assert env.get("C") == "has #hash"
    assert env.get("D") == "plain"
    assert env.has("A") and not env.has("E")


def test_env_file_missing_file_is_empty(tmp_path):
    env = EnvFile.load(tmp_path / "nope.env")
    assert env.values == {}
    assert env.render() == ""


def test_env_file_set_updates_existing_line_in_place(tmp_path):
    path = tmp_path / ".env"
    path.write_text("export A=old\nB=keep\n", encoding="utf-8")
    env = EnvFile.load(path)
    assert env.set("A", "new") is True
    assert env.set("A", "new") is False
    rendered = env.render()
    assert "export A=new" in rendered
    assert "B=keep" in rendered


def test_env_file_set_appends_new_key(tmp_path):
    env = EnvFile(tmp_path / ".env")
    assert env.set("NEW_KEY", "value") is True
    assert env.get("NEW_KEY") == "value"
    assert env.render() == "NEW_KEY=value\n"


def test_env_file_rejects_bad_names_and_newlines(tmp_path):
    env = EnvFile(tmp_path / ".env")
    with pytest.raises(BootstrapError):
        env.set("1BAD NAME", "x")
    with pytest.raises(BootstrapError):
        env.set("GOOD_NAME", "line\nbreak")


def test_env_file_remove(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2\n", encoding="utf-8")
    env = EnvFile.load(path)
    assert env.remove("A") is True
    assert env.remove("A") is False
    assert env.render() == "B=2\n"


def test_env_file_quote_handling():
    assert EnvFile._quote("plain") == "plain"
    assert EnvFile._quote("with # hash") == '"with # hash"'
    assert EnvFile._quote('"quoted"') == '"\\"quoted\\""'
    assert EnvFile._quote("") == ""


def test_env_file_snapshot_and_save_if_changed(tmp_path):
    path = tmp_path / ".env"
    env = EnvFile(path)
    env.set("A", "1")
    before = env.snapshot()
    assert env.save_if_changed(before) is False
    env.set("A", "2")
    assert env.save_if_changed(before) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_name_pattern_matches_env_rules():
    assert KEY_NAME.match("GROQ_API_KEY")
    assert not KEY_NAME.match("1BAD")


def test_global_env_path_uses_eurus_home(tmp_path):
    assert global_env_path({"EURUS_HOME": str(tmp_path)}) == tmp_path / ".env"


def test_ensure_global_env_creates_template_once(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path)}
    path, created = ensure_global_env(environ)
    assert created is True
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    for line in GLOBAL_ENV_TEMPLATE:
        assert line in text
    path2, created2 = ensure_global_env(environ)
    assert created2 is False
    assert path2 == path


def test_mask_secret_never_reveals_middle():
    assert mask_secret("") == "(not set)"
    assert mask_secret("   ") == "(not set)"
    assert mask_secret("short") == "***"
    masked = mask_secret("sk-1234567890abcdef")
    assert masked.startswith("sk-1")
    assert masked.endswith("cdef")
    assert "67890" not in masked


def test_validate_secret_format_rules():
    assert validate_secret_format("KEY", "") == "KEY must not be empty"
    assert "too long" in validate_secret_format("KEY", "x" * 600)
    assert "spaces" in validate_secret_format("KEY", "has space")
    assert "bearer" in validate_secret_format("KEY", "Bearer abc123").lower()
    assert validate_secret_format("KEY", "gsk_valid123") == ""


def test_validate_telegram_token_format():
    assert validate_telegram_token("") != ""
    assert validate_telegram_token("not-a-token") != ""
    assert validate_telegram_token("123456:short") != ""
    assert validate_telegram_token("123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c") == ""


def test_secrets_from_environ_filters_api_keys():
    environ = {
        "GROQ_API_KEY": "gsk-one",
        "EURUS_TELEGRAM_BOT_TOKEN": "123:abc",
        "PATH": "/usr/bin",
        "EMPTY_API_KEY": "  ",
    }
    secrets = secrets_from_environ(environ)
    assert "gsk-one" in secrets
    assert "123:abc" in secrets
    assert "/usr/bin" not in secrets
    assert len(secrets) == 2


def test_clean_state_payload_redacts_nested_keys():
    payload = {
        "provider": "groq",
        "api_key": "gsk-secret",
        "nested": {"bot_token": "123:abc", "safe": 1},
        "model_list": "ok",
    }
    cleaned = clean_state_payload(payload)
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["bot_token"] == "[REDACTED]"
    assert cleaned["nested"]["safe"] == 1
    assert cleaned["provider"] == "groq"


def test_dumps_scalars_arrays_and_tables():
    text = dumps(
        {
            "enabled": True,
            "count": 3,
            "ratio": 0.5,
            "name": "eurus",
            "tags": ["a", "b"],
            "browser": {"backend": "cdp", "nested": {"deep": 1}},
        }
    )
    assert "enabled = true" in text
    assert "count = 3" in text
    assert "ratio = 0.5" in text
    assert 'name = "eurus"' in text
    assert 'tags = ["a", "b"]' in text
    assert "[browser]" in text
    assert "[browser.nested]" in text


def test_dumps_quotes_unusual_keys():
    text = dumps({"agent": {"model": "x"}, "with space": 1})
    assert '"with space" = 1' in text


def test_dumps_rejects_unsupported_types():
    with pytest.raises(BootstrapError):
        dumps({"bad": object()})


def test_toml_manager_load_missing_file_is_empty(tmp_path):
    manager = TomlManager.load(tmp_path / "missing.toml")
    assert manager.data == {}


def test_toml_manager_merge_is_deep_and_validates(tmp_path):
    path = tmp_path / "config.toml"
    manager = TomlManager.load(path)
    manager.merge({"browser": {"enabled": True}})
    manager.merge({"browser": {"backend": "cdp"}})
    assert manager.data["browser"] == {"enabled": True, "backend": "cdp"}
    manager.validate({})
    manager.save(environ={"EURUS_HOME": str(tmp_path)})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = TomlManager.load(path)
    assert reloaded.section("browser")["enabled"] is True


def test_toml_manager_save_invalidates_bad_values(tmp_path):
    path = tmp_path / "config.toml"
    manager = TomlManager.load(path)
    manager.data = {"network": {"timeout_seconds": "not-a-number"}}
    with pytest.raises(BootstrapError):
        manager.save(environ={"EURUS_HOME": str(tmp_path)})
    assert not path.exists()


def test_toml_manager_update_section_merges(tmp_path):
    manager = TomlManager.load(tmp_path / "config.toml")
    manager.update_section("agent", {"model": "groq/x"})
    manager.update_section("agent", {"fallback": ["openai/y"]})
    assert manager.section("agent") == {"model": "groq/x", "fallback": ["openai/y"]}


def test_toml_manager_load_invalid_toml_raises(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("not [ valid toml", encoding="utf-8")
    with pytest.raises(BootstrapError):
        TomlManager.load(path)


def test_dir_permission_constant_is_private():
    assert DIR_PERMISSION == 0o700
    assert ENV_PERMISSION == 0o600
