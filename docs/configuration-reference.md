# Configuration reference

Configuration is layered; later sources win:

1. Built-in defaults (shown below).
2. `~/.eurus/config.toml` (user file).
3. Project file: `eurus.toml` or `.eurus/config.toml` in the working directory.
4. Environment overrides (`EURUS_MODEL`, `EURUS_FALLBACK`, `EURUS_LOG_LEVEL`, ...).

Run `eurus config show` to see the effective configuration and `eurus config init` to
write a starter file. `eurus.example.toml` in the repository is a fully commented
template with every default.

API keys never live in this file. Providers reference environment variables through
`api_key_env`; values are read from `~/.eurus/.env` or the process environment.

## `[agent]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `model` | required | Provider/model pair, for example `groq/llama-3.3-70b-versatile`. |
| `fallback` | `[]` | Models tried in order when the primary fails. |
| `iteration_budget` | `500` | Hard cap on agent loop iterations per run. |
| `temperature` | `0.2` | Sampling temperature. |
| `system_prompt` | `""` | Appended to the built-in prompt. |
| `context_tokens` | `32000` | Context window assumed for planning. |

## `[permissions]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `mode` | `"ask"` | `ask` requires interactive approval for mutating tools; `auto` approves nothing silently and is not available to remote callers. |
| `workspace` | `"."` | Root directory tools may touch. |

## `[sandbox]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `backend` | `"host"` | `host` runs commands directly (no confinement); `container` runs them in a read-only rootfs with the workspace bind-mounted. |

## `[browser]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `enabled` | `false` | Enable browser automation tools. |
| `backend` | `"auto"` | `playwright` or `cdp`. |
| `allowed_domains` | `[]` | Allowlist of domains the browser may reach. |
| `blocked_domains` | `[]` | Denylist. |
| `allow_private_addresses` | `false` | Permit loopback/private destinations. Required for local testing against `127.0.0.1`. |
| `max_download_mb` | `50` | Download cap per file. |

## `[network]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `timeout_seconds` | `120` | Per-request timeout for web tools. |
| `max_retries` | `3` | Retry budget per request. |
| `local_only` | `false` | Restrict web tools to private ranges (offline development). |

## `[logging]`

| Key | Default | Meaning |
| --- | ------- | ------- |
| `level` | `"INFO"` | `DEBUG` .. `ERROR`. |
| `json` | `false` | Structured JSON log lines. |

## `[audio]`

Speech-to-text and text-to-speech for the multimodal agent.

| Key | Default | Meaning |
| --- | ------- | ------- |
| `stt_model` | `""` | Transcription model as `provider/model` (for example `openai/whisper-1`, `groq/whisper-large-v3`). Empty disables transcription; the provider must be OpenAI-compatible and the model must declare `audio_input` + `text_output` capabilities. |
| `stt_language` | `""` | ISO language hint passed to the transcriber (for example `"id"`, `"en"`); empty lets the provider auto-detect. |
| `stt_timeout_seconds` | `180.0` | Transcription request timeout. |
| `tts_model` | `""` | Speech synthesis model as `provider/model` (for example `openai/tts-1`). Empty disables voice replies; the model must declare `audio_output`. |
| `tts_voice` | `"alloy"` | Default voice name. |
| `tts_format` | `"mp3"` | Output format: `mp3`, `opus`, `aac`, `flac`, or `wav`. Use `opus` for Telegram voice messages. |
| `tts_timeout_seconds` | `120.0` | Speech synthesis request timeout. |

## Media limits (environment variables)

| Variable | Default | Meaning |
| --- | ------- | ------- |
| `EURUS_MEDIA_MAX_BYTES` | `20971520` | Maximum stored media size in bytes. |
| `EURUS_MEDIA_MAX_DURATION_SECONDS` | `7200` | Maximum audio/video duration in seconds. |
| `EURUS_MEDIA_MAX_PIXELS` | `100000000` | Maximum image pixel count. |
| `EURUS_MEDIA_MAX_DIMENSION` | `30000` | Maximum single image dimension. |
| `EURUS_MEDIA_PROCESSING_TIMEOUT_SECONDS` | `60` | Per-file metadata extraction timeout. |
| `EURUS_MEDIA_MAX_CONCURRENT_PROCESSING` | `2` | Parallel processing slots. |
| `EURUS_MEDIA_MAX_PROCESSING_RETRIES` | `2` | Retry budget for transient extraction failures. |
| `EURUS_MEDIA_RETENTION_DAYS` | `0` | Days before old media is purged (0 keeps forever). |

## `[autonomy]`, `[scheduler]`, `[office]`, `[research]`, `[tools]`, `[memory]`, `[learning]`

Covered by `eurus.example.toml`; every key has a safe default and `eurus config show`
prints the effective values. The web gateway also exposes a subset through
`PATCH /api/config` (only the sections listed as editable; provider credentials are
never writable through the gateway).

## Environment variables

| Variable | Meaning |
| -------- | ------- |
| `EURUS_HOME` | Override the state directory (default `~/.eurus`). |
| `EURUS_MODEL` / `EURUS_FALLBACK` | Override `agent.model` / `agent.fallback`. |
| `EURUS_LOG_LEVEL` | Override `logging.level`. |
| `EURUS_PERMISSION_MODE` | Override `permissions.mode`. |
| `EURUS_LOCAL_ONLY` | Override `network.local_only`. |
| `EURUS_STT_MODEL` / `EURUS_STT_LANGUAGE` | Override `audio.stt_model` / `audio.stt_language`. |
| `EURUS_TTS_MODEL` / `EURUS_TTS_VOICE` / `EURUS_TTS_FORMAT` | Override `audio.tts_model` / `audio.tts_voice` / `audio.tts_format`. |
| `EURUS_TELEGRAM_BOT_TOKEN` | Telegram bot token for the gateway. |
| `EURUS_TELEGRAM_ALLOWED_USER_IDS` | Numeric Telegram user IDs allowed to talk to the bot. |
| `EURUS_MCP_SERVER_TOKEN` | Bearer token for the built-in MCP server. |

Provider keys (`OPENAI_API_KEY`, `GROQ_API_KEY`, ...) are read from `~/.eurus/.env`.
