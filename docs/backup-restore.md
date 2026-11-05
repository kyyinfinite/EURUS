# Backup and restore

All persistent state lives under one directory (`~/.eurus` by default, or
`$EURUS_HOME`).

## What is stored where

| Path | Contents |
| ---- | -------- |
| `~/.eurus/config.toml` | Configuration. |
| `~/.eurus/.env` | Provider API keys (0600). |
| `~/.eurus/data/eurus.sqlite3` | Runs, sessions, memories, missions, tasks, attempts, events. |
| `~/.eurus/media/` | Conversation media payloads. |
| `~/.eurus/office/` | Office document artifacts. |
| `~/.eurus/skills/` | Promoted skills. |
| `~/.eurus/gateway-web.json` | Web gateway token. |
| `~/.eurus/security/` | Approval and policy state. |

## Backup

With Eurus stopped (including the autonomy daemon and gateways):

```bash
tar -czf eurus-backup-$(date +%Y%m%d).tar.gz ~/.eurus
```

The SQLite file may also be backed up online; the WAL journal makes a file copy of a
quiescent database consistent. For a hot backup prefer the archive approach with the
daemon stopped, or use `sqlite3 ~/.eurus/data/eurus.sqlite3 ".backup '/path/backup.sqlite3'"`.

## Restore

```bash
tar -xzf eurus-backup-YYYYMMDD.tar.gz -C ~/
```

Then start Eurus. Migrations run automatically; data written by the backup's version
is upgraded in place. See `docs/upgrade-guide.md` for the rollback path.

## What is not backed up

- Workspaces (project directories) are owned by your projects; back them up with the
  project, not with Eurus.
- Provider keys stay in `~/.eurus/.env`, which is included in the archive. Treat the
  archive as secret material.
