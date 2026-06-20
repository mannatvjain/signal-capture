# Signal Capture

## Stack
- **Language**: Python 3.10+ (uses PEP 604 union syntax; pyproject claims 3.8 but is wrong)
- **Signal interface**: signal-cli 0.14.1 (Homebrew), run as a persistent daemon over a Unix socket
- **Classification**: Claude Code CLI (`claude -p --model haiku`) at `/Users/mannat/.local/bin/claude`
- **Database**: SQLite (stdlib `sqlite3`, WAL mode)
- **TUI**: Textual
- **Notion**: REST API via stdlib `urllib`
- **Scheduling**: launchd (macOS)
- **Vault root**: `~/Documents/dot`

## Architecture

### Data flow
```
Signal "Note to Self"
        │
        ▼
signal-cli daemon ──► JSON over Unix socket
        │
        ▼
signal_capture daemon (persistent)
        │
        ├── insert into capture.db messages
        ├── send "[vault] captured." back to Signal
        │
        ▼
priority routing:
  1. [blot] / [salience] / Q.A. / C.{cloze}  → daily note ## Signal → anki-sync
  2. todo: prefix                            → Notion "In"
  3. reminder: prefix                        → Claude Haiku extracts fire_at → capture.db reminders
  4. else                                    → Claude Haiku classifier → category route
        │
        ▼
debounced [sorted] confirmation back to Signal
```

### Package (`signal_capture/`)
- `cli.py` — `sl` entry point. Subcommands: `poll`, `daemon`, `view`, `list`, `count`, `health`
- `capture.py` — Config, `.env` loading, `init_db` (messages + reminders + notion_queue tables + migrations), `send_alert` (prefers Summertime socket, falls back to signal-cli subprocess), one-shot `poll` plumbing
- `daemon.py` — Persistent daemon. Runs signal-cli over Unix socket, three background threads (health writer, [sorted] debounce flusher, Notion drainer), `handle_correction` for reply-based corrections
- `cards.py` — Card detection (`Q. A.` / `C.{cloze}` / `[blot]` / `[salience]`), parse + render to `START`/`END` block form (`_parse_card`, `_render_block`, `render_cards`), daily-note appending, `anki-sync` script invocation (debounced 10s)
- `triage.py` — Claude classifier + routing. `todo:` and `reminder:` prefix short-circuits, `_remove_from_category` for reroute cleanup, `parse_reschedule_time` and `extract_reminder_fire_at` Claude helpers
- `notion.py` — Notion REST client. `send_or_queue` posts to the In database; `drain_queue` retries; `dequeue_by_name` for reroute cleanup
- `viewer.py` — Textual TUI (vim keys: j/k, /, g/G, q)
- `health.py` — Standalone staleness check; macOS notification if `~/.signal-capture-health` is >1hr old
- `meals.py` — Minimal calorie estimator. `estimate_calories(image_path, caption)` calls Claude Haiku vision and returns formatted text like `~650 kcal\n  • grilled chicken: ~250 kcal\n  • rice: ~200 kcal`. Nothing saved to disk or DB.

### Database
- Location: `~/Documents/dot/CLAUDE/Artifacts/signal-capture/capture.db`
- Tables:
  - `messages (id, signal_timestamp UNIQUE, body, captured_at, obsidian_synced)`
  - `reminders (id, body, fire_at, signal_timestamp, created_at, fired, cancelled)`
  - `notion_queue (id, name, context, created_at, last_attempt_at, last_error, attempts)`
- All messages land here first; vault files and Notion are routing destinations, not source of truth

### Categories (active classifier output)
| Category | Destination |
|---|---|
| `reminder` | `capture.db` reminders table (fired by Summertime) |
| `todo` | Notion "In" database (via `signal_capture.notion`; queued + retried on failure) |
| `resource` | Daily note `## Links` |
| `sundry` | `4-Sundry/Running Sundry.md` |

The classifier prompt + JSON schema enumerate exactly these four (`triage.py:54-67`). Cards are detected *before* the classifier and never reach it.

### Card pipeline
Card-shaped messages bypass the classifier entirely. Detection happens in `cards.is_card` (regex match on `Q.`/`A.`/`C.{cloze}`). Once detected:

1. `anki_pre_sync()` runs `~/Documents/dot/CLAUDE/Artifacts/anki-sync --sync-only` (pulls AnkiWeb → local Anki). Blocking, 60s timeout.
2. `render_cards(body)` parses each block via `_parse_card` (returns `(note_type, fields)`) and re-emits each as a `START`/`END` block via `_render_block`. This is uniform — single-line and multi-line cards both become block form. Reason: shorthand `Q.`/`A.` and `C.` cannot represent blank lines inside a field, which breaks multi-line answers (the plugin's per-line regex stops at the first `\s*$`). `START`/`END` is the only delimiter the plugin parses that tolerates arbitrary intra-field content.
3. Block-rendered text is appended to today's daily note under `## Signal`.
4. `trigger_anki_sync()` schedules a debounced (10s) `anki-sync` run that:
   - Triggers the obsidian-to-anki plugin's `anki-scan-vault` via `obsidian://advanced-uri`
   - Calls AnkiConnect `sync` to push to AnkiWeb

The obsidian-to-anki plugin is what actually creates the Anki notes — signal-capture never calls AnkiConnect directly.

**Block format emitted:**
```
START
Basic              # or Cloze
<first field content, possibly multi-line>
Back: <next field content, possibly multi-line>   # Basic only — Cloze has one field
END
```
First line after `START` is the note type. Subsequent lines accumulate into the current field; a line starting with `FieldName:` switches to that field. Plugin source: `main.js:604-652` (`Note` class).

**Variants:**
- `[blot] <body>` → blots each line independently (`blot_text` is newline-aware), then emits `_render_block("Basic", {Front: blotted, Back: stripped})`. Multi-line blot preserves the line structure in both fields.
- `[salience] <card>` → parses and re-emits via `render_cards`, writes to `CLAUDE/Running Salience.md` instead of the daily note.

### Prefix short-circuits (skip the classifier)
Implemented at the top of `route_message` (`triage.py:388`):

- `todo: <body>` → strips the prefix, calls `notion.send_or_queue(body, None)`, returns `"todo"`
- `reminder: <body>` → strips the prefix, calls `extract_reminder_fire_at` (a focused Claude Haiku call that returns `{cleaned, fire_at}`). If a valid ISO 8601 fire_at comes back, inserts into `reminders` table. If no time can be parsed, falls back to Notion as a todo.

### Daily note path
`get_daily_note_path` (`cards.py:51`) uses school-year-style numbering:
`~/Documents/dot/0-Journal/{year-2025}-{year}/{month-4}-{Month}/{MM-DD}.md`

Example for 2026-05-24: `0-Journal/1-2026/1-May/05-24.md` (May = month 1 since the year started in April).

### Reminders (cross-project)
- Inserted into `capture.db` reminders table
- **Summertime** (`~/Developer/summertime`) polls this table and fires Signal alerts at `fire_at`
- The reminder text comes back as a `[reminder] body` Signal message, which the user can 👍/👎 react to

### Confirmations
The daemon talks back via Signal. Two-stage:
1. `[vault] captured.` — DB insert confirmed (sent immediately on insert)
2. `[sorted] <category> — <body>` — classification + routing confirmed (debounced 8s; if multiple captures arrive in a burst, they're batched into one `[sorted] N captures` message)
   - When `<body>` is a multi-line block (the `START…END` form that `card`/`salience` render to), the single-capture confirmation drops the ` — ` and puts the block on its own line: `[sorted] card\nSTART…END`. Single-line bodies keep the ` — body` form. Keyed on a newline in the body (`_flush_pending_sorted`, `daemon.py`), so it covers cards and salience without naming categories. The batched `[sorted] N captures` path still inlines bodies as `— <category>: <body>`.

Other confirmation types:
- `[rerouted] <old> → <new> — <body>` — reply-based reroute applied
- `[cancelled] <body>` — reminder cancelled by reply
- `[rescheduled] <old_time> → <new_time> — <body>` — reminder time changed by reply
- `[error] <reason>` — something failed
- `[vault] card queued (Anki sync pending)` — card detected but `anki_pre_sync` failed; will retry on next message

### Corrections
Reply to a `[sorted]` or `[rerouted]` confirmation with a category name (`reminder|todo|resource|sundry`) to reroute. Chains supported. Cards cannot be rerouted.

For reminders specifically (reply to `[sorted] reminder @ TIME — body`):
- `cancel` — marks reminder as cancelled
- A time string (`6pm`, `+30 min`, `delay by 5 mins`) — reschedules via `parse_reschedule_time` (Haiku call with the current `fire_at` as the relative anchor)
- A category name — cancels the reminder and reroutes to the new category

The correction handler (`daemon.py:145`) parses the quoted text to recover the original body, looks up its `signal_timestamp` in the messages table, and operates from there.

### Notion integration
- Module: `signal_capture/notion.py`
- DB ID: `36a0e6f5-a58a-80b0-88bb-db490de36821` (hardcoded — the "In" database in the "Mapmaker" workspace)
- Schema: `Name` (title), `Context` (rich_text), `Date` (created_time, auto)
- Token: read from `NOTION_TOKEN` env var, supplied via the launchd plist's `EnvironmentVariables` block
- Failure handling: on any HTTP/network error, the row is queued in `notion_queue` and retried by the daemon's `_notion_drainer` thread every 60s. Items are kept in insertion order; the drainer stops on the first failure to preserve order.
- Reroute cleanup: if a `[sorted] todo` is corrected to another category, `_remove_from_category("todo")` calls `notion.dequeue_by_name(body)` to pull it from the queue if still pending. Once a row has been sent to Notion, **manual deletion is required** — there's no page-ID tracking.

### send_alert socket behavior
`send_alert()` in `capture.py` prefers Summertime's Unix socket (`~/.summertime-alert.socket`) over a direct `signal-cli send` subprocess. This avoids signal-cli account lock conflicts: Summertime's daemon already runs `signal-cli daemon` on the alert account, so a parallel subprocess would block on the account lock. If the socket isn't there (Summertime not running), it falls back to subprocess.

### Image attachments
**Trigger:** message has at least one image attachment AND the body contains the word "meal" (case-insensitive, substring match). Anything else with an image is ignored.

When triggered, for each image:
1. Resolve the cached file at `~/.local/share/signal-cli/attachments/{id}` (signal-cli stores incoming attachments there). No copy made.
2. Call `estimate_calories(path, body)` — Claude Haiku vision, with the body passed as caption. Returns itemized text.
3. Send the result back via `send_alert` (Summertime socket → alert recipient), prefixed with `[meal]`. On failure: `[meal] couldn't estimate`.

No vault writes, no DB inserts, no monthly log. The `messages` row is still inserted into `capture.db` (every Signal message is recorded), but the image itself isn't touched beyond the model call.

The confirmation filter at `daemon.py:369` skips `[meal]` and `[photo]` so the daemon doesn't loop on its own replies.

### Config (.env)
- `SIGNAL_ACCOUNT` — your phone number (E.164 format)
- `SIGNAL_ALERT_ACCOUNT` — phone number for outgoing alert messages (separate Google Voice bot account)
- `SIGNAL_ALERT_RECIPIENT` — recipient for alerts (your own number)
- Environment variables take precedence over `.env`
- `.env` is gitignored

### launchd jobs
- `com.mannat.signal-capture` — persistent daemon with `KeepAlive` (auto-restart on crash/wake), `EnvironmentVariables` block carries `NOTION_TOKEN`
- `com.mannat.signal-capture-health` — health check every 1800 seconds (30 min)
- `install.sh` requires `NOTION_TOKEN` to be exported before running, or it errors out (avoids silently wiping the token on reinstall)
- After plist changes, use `launchctl bootout` + `launchctl bootstrap` to reload (a plain `kickstart -k` restarts the process but doesn't re-read the plist)

## Dev commands
```bash
sl poll            # One-shot pull
sl daemon          # Persistent daemon (managed by launchd)
sl view            # TUI viewer
sl list            # Recent messages (default 20)
sl list -n 50      # Last 50
sl count           # Total count
sl health          # Pipeline health
./install.sh       # Install launchd jobs (requires NOTION_TOKEN env var)
./uninstall.sh     # Remove launchd jobs
```

## Conventions
- Daemon must be restarted after code changes (process caches modules)
- Daily notes use the message's Signal timestamp, not capture time
- Bullet points: don't double-bullet if message already starts with `-`
- Health file at `~/.signal-capture-health` is a single ISO timestamp, updated every 5 min by `_health_writer`
- Classification uses `claude -p` with `--json-schema` for structured output and `--allowedTools ""` to prevent tool use (image classification, when re-enabled, would use `--allowedTools Read --permission-mode bypassPermissions` for vision)

## Known dead code
- `signal_capture/daemon.py:36` — `VALID_CATEGORIES` constant defined but never referenced
