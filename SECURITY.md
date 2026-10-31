# Security Policy

Eurus is pre-alpha software that can run tools on your machine. Please take security reports seriously and report them privately.

## Reporting a vulnerability

Do not open a public issue for security problems. Use GitHub's **private vulnerability reporting** on this repository (Security tab, then "Report a vulnerability").

Please include the affected version, steps to reproduce, and the impact. You will get an acknowledgement within 7 days. Please allow time for a fix before any public disclosure.

## Supported versions

Only the latest release receives security fixes while the project is pre-alpha.

## Scope

Of particular interest:

- Shell or filesystem confinement bypass, including sandbox escape and symlink or path-traversal tricks.
- Approval or permission bypass, including execution of mutating tools without an approver.
- Leakage of API keys, tokens, or other secrets through logs, traces, journals, or the model context.
- Prompt injection that leads to unapproved actions.
- Authentication, origin, or host-check bypass in the web gateway or the MCP server.

Out of scope: issues that require an already-approved malicious action, running with sandboxing disabled (Termux host mode), or attacks that need prior control of the user's machine or home directory.

## Security model in brief

- **Fail-closed approvals.** Mutating tools are denied when no approver is available.
- **Isolated shell.** Commands run in a container with no network and dropped capabilities, and never fall back to the host. On Termux the host backend is used, and the approval gate is the only control.
- **Local by default.** The web gateway and MCP HTTP server bind to loopback and require a bearer token.
- **Secrets stay out.** Event payloads, logs, and traces are redacted, and Google tokens use the OS keyring or an encrypted file, never plaintext.

## Your responsibilities

- Review every approval. A changed command needs a new approval.
- Run Eurus in a disposable workspace and keep backups of anything valuable.
- Never put credentials into chat history or memory notes.
- Keep `.env` and credential files out of version control.
- Treat the container sandbox as unvalidated until you have verified it on your own host.
