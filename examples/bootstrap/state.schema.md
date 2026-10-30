# ~/.eurus/bootstrap/state.json — bootstrap bookkeeping schema (v1)
# Managed by eurus bootstrap; API keys and tokens are never stored here
# (any key named *api_key*, *token*, *secret* or *password* would be redacted).

schema_version: 1

created_at: ISO-8601 UTC timestamp of first bootstrap run
last_checked: ISO-8601 UTC timestamp of last `eurus doctor` / `bootstrap --check`
platform: distro id recorded by the last platform detection (e.g. "debian", "termux")
architecture: normalised machine (aarch64 | x86_64 | armv7l | ...)
python:
  version: "3.12.16"
  venv: true|false
features: map of feature name -> installed | skipped
provider: { name: <provider> }            # metadata only, no secrets
last_quick_setup:
  started_at / finished_at: ISO-8601
  status: done | interrupted | skipped
  steps: map of the 13 quick-setup steps -> pending | done | skipped | failed
doctor:
  summary: READY | PARTIAL | DEGRADED | BLOCKED
  checked_at: ISO-8601
