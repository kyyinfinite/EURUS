# Providers

Eurus is provider-agnostic. Providers are configured in `[providers]` sections of
`eurus.toml` and selected with `agent.model = "provider/model"`.

## Built-in provider catalog

The builtin catalog (`src/eurus/catalog.py`) ships with Groq, OpenAI-compatible
endpoints, Anthropic, Google Gemini, NVIDIA NIM, Ollama, and OpenRouter entries.
Each entry declares a protocol (`openai`, `anthropic`, or `gemini`), a base URL,
and the environment variable that holds its API key.

## Diagnosing failures

`eurus diagnose` probes the configured model chain without running an agent and
reports a reason code per model:

| Reason | Meaning |
| --- | --- |
| `edge_blocked` | Cloudflare rejected the client before the key was checked |
| `model_blocked` | The model is disabled for your organization or project |
| `region_blocked` | The request originated from a blocked region |
| `forbidden` | The key was rejected |

Pass explicit models to probe a specific chain:

```bash
eurus diagnose groq/llama-3.3-70b-versatile nvidia/meta/llama-3.3-70b-instruct
```

## Environment loading

Keys placed in `.env` (current directory or `EURUS_HOME`) are loaded
automatically, but only variables ending in `_API_KEY` plus `OLLAMA_BASE_URL`
and `NVIDIA_BASE_URL`. Set `EURUS_NO_DOTENV=1` to disable. Existing environment
values always win over `.env` files.

## Request behavior

- Rate limits, timeouts, and 5xx responses are retried with backoff, honoring
  `Retry-After`.
- If a model still fails, the next entry in `agent.fallback` is tried.
- A stream is only retried or failed over before its first token arrives; after
  the first token the turn ends with an error instead of replaying partial
  output.
- With `network.local_only = true`, only local endpoints (loopback, private
  ranges, Tailscale) are allowed.

## Failure classification

Provider failures are classified structurally
(`src/eurus/agent/evaluation.py`): authentication, rate limit, provider
unavailable, invalid request, timeout, and cancellation are recognized from
exception types and error codes before any message text is inspected. Unknown
errors are never assumed to be retryable.
