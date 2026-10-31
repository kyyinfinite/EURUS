# Persistent Telegram reminders

Eurus Telegram supports persistent reminders created through the agent tools `create_reminder`, `list_reminders`, `reschedule_reminder`, and `cancel_reminder`. Reminder records are stored in the runtime database `~/.eurus/data/eurus.sqlite3` (formerly `sessions.sqlite3`), so they survive process restarts. The Telegram reminder dispatcher runs alongside polling and sends messages even when the owner is not actively chatting with Eurus.

## Time handling

Reminder tools require a concrete ISO-8601 `due_at` timestamp. The model must resolve relative expressions against the current date/time and include a timezone, or ask a clarifying question when the date or time is ambiguous. `EURUS_REMINDER_TIMEZONE` configures the default IANA timezone; it defaults to `Asia/Jakarta`. A recurring reminder uses `interval_seconds` (minimum 60 seconds), for example `120` for every two minutes. One-time reminders omit `interval_seconds`.

## Delivery and recovery

Due reminders are claimed transactionally in SQLite. Telegram delivery uses the existing adapter and rate limiter. Failed sends retry with bounded exponential backoff; a one-time reminder becomes `completed` after successful delivery. Recurring reminders are rescheduled from their scheduled occurrence and skip missed intervals rather than sending an unbounded backlog after downtime. Pending reminders are recovered after restart. Because Telegram does not provide an idempotency key for `sendMessage`, a process crash after Telegram accepts a message but before SQLite records success can still produce a duplicate on recovery; delivery is therefore at-least-once at that crash boundary.

Reminder ownership is scoped to the Telegram user, and delivery is bound to the originating conversation. The current implementation dispatches reminders to Telegram; other gateways are rejected explicitly rather than broadcast.

## Natural-language cancellation

The agent can cancel reminders without requiring the user to know a reminder ID. Phrases such as "Eurus, udah jangan ingetin gue", "stop reminding me", or "cancel that reminder" should invoke `cancel_reminder`. Without a specific reminder message or ID, cancellation applies to all active reminders in the current owner, gateway, and conversation only. If the user names the reminder message, pass it as `message` to cancel matching reminders. A supplied reminder ID is also checked against the current owner and conversation. Cancelled records are retained with status `cancelled` for audit and are excluded from future dispatch; they are not physically deleted from SQLite.
