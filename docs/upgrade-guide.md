# Upgrade guide

## Automatic migrations

Eurus applies SQLite schema migrations on every start. Each migration runs inside a
`BEGIN IMMEDIATE` transaction that also inserts the schema version row, so an
interrupted migration rolls back and is retried safely on the next start. A database
newer than the installed package is rejected instead of being silently downgraded.

There is no manual migration command. Start any Eurus entry point (`eurus run`,
`eurus gateway web`, `eurus-tui`) and the database is brought up to date.

## Before upgrading

1. Back up the state directory (see `docs/backup-restore.md`).
2. Note your current version:

   ```bash
   eurus --version
   ```

3. Stop all running processes: the TUI, `eurus gateway web`, and the autonomy daemon.

## Upgrading the package

```bash
python -m pip install --upgrade eurus-agent
python -m pip install "eurus-agent[web,web-gateway,telegram,browser,documents]"
python -m playwright install chromium
```

## After upgrading

1. Migrate and validate:

   ```bash
   eurus doctor
   eurus mcp doctor
   ```

2. Verify old data is intact:

   ```bash
   eurus sessions
   eurus history
   ```

   Sessions, runs, memories, missions, and artifacts created by earlier versions
   remain accessible; the schema test suite pins this behaviour for every migration
   from the initial schema onward.

3. If the web gateway is part of your setup, no frontend step is needed: release
   wheels bundle the web UI. When installing from a source checkout, build the
   frontend once (`npm ci && npm run build` inside `frontend/`) before
   `pip install .`, or point `EURUS_GATEWAY_STATIC` at an existing build directory.
   See `docs/installation.md` for the full resolution order.

## Configuration compatibility

Configuration keys are additive. Keys removed across versions are still accepted and
ignored with a warning rather than failing startup. `eurus config show` lists the
effective values after an upgrade.

## Rolling back

1. Stop all Eurus processes.
2. Restore the state-directory backup.
3. Install the previous package version:

   ```bash
   python -m pip install "eurus-agent==0.3.0"
   ```

A database written by a newer Eurus is rejected by older versions on start; the
backup is what makes rollback possible, so take one before every upgrade.
