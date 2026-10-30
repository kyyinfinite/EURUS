from eurus.dotenv import is_allowed, load_dotenv, parse_dotenv


def test_parse_dotenv_handles_quotes_export_and_comments():
    text = "\n".join(
        [
            "# comment",
            "GROQ_API_KEY=gsk_one",
            'export NVIDIA_API_KEY="nvapi-two"',
            "OPENAI_API_KEY='sk-three'  ",
            "XAI_API_KEY=plain # trailing note",
            "broken line",
            "=novalue",
        ]
    )
    values = parse_dotenv(text)
    assert values == {
        "GROQ_API_KEY": "gsk_one",
        "NVIDIA_API_KEY": "nvapi-two",
        "OPENAI_API_KEY": "sk-three",
        "XAI_API_KEY": "plain",
    }


def test_only_provider_keys_are_loaded(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "GROQ_API_KEY=gsk_x\nEURUS_PERMISSION_MODE=yolo\nNVIDIA_BASE_URL=http://h/v1\nPATH=/evil\n"
    )
    environ = {"PATH": "/bin"}
    assert load_dotenv([path], environ) == [path]
    assert environ == {
        "PATH": "/bin",
        "GROQ_API_KEY": "gsk_x",
        "NVIDIA_BASE_URL": "http://h/v1",
        "EURUS_PERMISSION_MODE": "yolo",
    }


def test_supported_eurus_vars_are_loaded(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "EURUS_MODEL=groq/llama-3.3-70b-versatile",
                "EURUS_FALLBACK=openai/gpt-4o-mini",
                "EURUS_TELEGRAM_BOT_TOKEN=123456789:AAfakeTokenForTestsOnly000",
                "EURUS_TELEGRAM_ALLOWED_USER_IDS=111,222",
                "EURUS_REMINDER_TIMEZONE=Asia/Jakarta",
                "OLLAMA_BASE_URL=http://127.0.0.1:11434",
            ]
        )
    )
    environ = {}
    load_dotenv([path], environ)
    assert environ["EURUS_MODEL"] == "groq/llama-3.3-70b-versatile"
    assert environ["EURUS_FALLBACK"] == "openai/gpt-4o-mini"
    assert environ["EURUS_TELEGRAM_BOT_TOKEN"] == "123456789:AAfakeTokenForTestsOnly000"
    assert environ["EURUS_TELEGRAM_ALLOWED_USER_IDS"] == "111,222"
    assert environ["EURUS_REMINDER_TIMEZONE"] == "Asia/Jakarta"
    assert environ["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"


def test_unsupported_eurus_vars_are_rejected(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EURUS_HOME=/evil\nEURUS_NO_DOTENV=1\nEURUS_RANDOM=x\n")
    environ = {}
    load_dotenv([path], environ)
    assert environ == {}


def test_existing_values_win_but_empty_ones_are_filled(tmp_path):
    path = tmp_path / ".env"
    path.write_text("GROQ_API_KEY=from_file\nOPENAI_API_KEY=from_file\n")
    environ = {"GROQ_API_KEY": "from_env", "OPENAI_API_KEY": "  "}
    load_dotenv([path], environ)
    assert environ["GROQ_API_KEY"] == "from_env"
    assert environ["OPENAI_API_KEY"] == "from_file"


def test_missing_file_is_ignored(tmp_path):
    assert load_dotenv([tmp_path / "none.env"], {}) == []
    assert is_allowed("MYHOST_API_KEY")
    assert is_allowed("EURUS_MODEL")
    assert is_allowed("EURUS_TELEGRAM_BOT_TOKEN")
    assert not is_allowed("EURUS_HOME")
    assert not is_allowed("EURUS_NO_DOTENV")
    assert not is_allowed("PATH")


def test_cdp_endpoint_is_loaded_from_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EURUS_CDP_ENDPOINT=http://127.0.0.1:9222\nEURUS_CHROMIUM=/usr/bin/chromium\n")
    environ = {}
    load_dotenv([path], environ)
    assert environ["EURUS_CDP_ENDPOINT"] == "http://127.0.0.1:9222"
    assert environ["EURUS_CHROMIUM"] == "/usr/bin/chromium"


def test_cdp_endpoint_env_value_wins_over_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EURUS_CDP_ENDPOINT=http://from-file:9222\n")
    environ = {"EURUS_CDP_ENDPOINT": "http://from-env:9222"}
    load_dotenv([path], environ)
    assert environ["EURUS_CDP_ENDPOINT"] == "http://from-env:9222"


def test_cdp_endpoint_allowed_but_home_still_rejected():
    assert is_allowed("EURUS_CDP_ENDPOINT")
    assert not is_allowed("EURUS_HOME")
