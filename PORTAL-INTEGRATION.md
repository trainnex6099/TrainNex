# Summit and the WCSO portal

Summit is the executor for both Discord and the existing WCSO portal. The portal
is optional: when SUMMIT_API_BASE_URL and SUMMIT_API_SECRET are unset, existing
Discord workflows continue using local storage. No separate Summit website is
created.

Without the portal connection, administrators with Manage Server can use
`/summit shift-setup`, `/summit loa-setup` and `/summit training-setup` to configure
the bot directly in Discord. `/shift manage` accepts configured custom types and
uses the configured default when its type is omitted. In connected mode, setup
commands direct administrators to WCSO so settings have one authoritative source.

## Connected operation

Deploy the matching WCSO portal changes before deploying this bot version.
The existing Cloudflare migrations 0000 and 0001 must already be applied to DB.
No new D1 tables are required by this patch.

Keep the existing DISCORD_TOKEN, SUMMIT_API_BASE_URL and SUMMIT_API_SECRET in
Railway. Never put these values in source control or portal browser code.
Run exactly one bot replica. Mount a persistent Railway volume and set
SUMMIT_DATA_DIR to its mount directory to retain records and command receipts
through restarts. Without a volume, D1 restores the last successful backup;
work performed after that backup can be lost on container replacement.

Portal actions enter a durable D1 queue. The bot polls every five seconds,
fetches current Discord membership and applies the same local records, roles,
notifications and training state as the Discord controls. Completion is reported
only after a full state backup succeeds. Queued actions require the bot online.

Shift controls: start, break, resume and end, with exact shift IDs to reject stale
portal controls. LOA controls: request, approve, deny and end early. Training:
add/remove FTOs, claim/pass an assigned offer and claim an open trainee. The old
FIFO, offer timeout, DM and fallback workflow is retained.

Full shift and LOA records are backed up separately from display summaries, so
ongoing breaks, review metadata and persistent Discord button IDs survive restore.
Training snowflakes are transported as strings to avoid JavaScript rounding.
Backups are refreshed periodically (60 seconds) and after portal mutations.
Existing local records take precedence over backups after a temporary API outage.
Initial backup restoration must finish before the bot may overwrite remote state.

Portal owners configure additional settings manager roles and LOA reviewer roles
inside WCSO. Existing portal moderators retain settings access. Discord verifies
LOA reviewer roles again at execution time. Merely viewing a log channel no longer
grants permission to approve another employee's leave.

## Failure handling

Requests have stable IDs and atomic D1 claims. Local receipts prevent replay if an
acknowledgement is lost. An interrupted command with uncertain effects is failed
for manual inspection instead of automatically repeating a mutation. The portal
shows pending, processing, completed or failed. If a command fails after a Discord
side effect, check the current record before submitting another action.

The compact state backup has a 1 MB limit. Archive old history before reaching
that limit; automatic archival is not included. This is a small-server integration,
not a multi-replica distributed event store. No destructive reset operations are
added or consumed by this bridge.

## Verification

Install requirements.txt and run:

    python -m unittest -v test_summit test_dispatch

Tests use isolated SQLite databases and mocked Discord I/O. They never log into
Discord or use a real token. Perform a live smoke test in your test server before
production: start in the portal, break/resume in Discord, end in the portal;
request leave in the portal, approve in Discord, end early in the portal; claim a
TrainNex offer in one interface and confirm it is unavailable in the other.
