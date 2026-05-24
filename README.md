# Signal Capture

A Python daemon that pipes your Signal "Note to Self" messages into a local SQLite database, auto-classifies them with Claude, and routes them to the right destination — Obsidian, Notion, the reminders queue, or your Anki deck.

## Features

- **Note-to-Self pipeline** — Text yourself on Signal, messages land instantly via signal-cli's persistent daemon
- **AI triage** — Claude Haiku (via `claude -p`) classifies each message and routes it
- **Anki cards** — `Q. / A.` and `C. {cloze}` messages are written verbatim to the daily note; the obsidian-to-anki plugin syncs them to Anki + AnkiWeb
- **Notion todos** — Todo messages post to a Notion "In" database; failures queue locally and retry every 60s
- **Reminders** — Time-bound messages get an ISO 8601 `fire_at` and land in the reminders table for [Summertime](../summertime) to fire
- **Prefix short-circuits** — Skip the classifier with `todo:` or `reminder:` prefixes
- **Reply-based corrections** — Reply to a `[sorted]` message with a category name to reroute; reply with a time to reschedule a reminder
- **Persistent daemon** — Runs via launchd with `KeepAlive`; survives sleep/wake, auto-restarts on crash
- **Health monitoring** — macOS notification if the daemon hasn't run in over an hour
- **`sl` CLI** — Poll, daemon, view (TUI), list, count, health

## Requirements

- Python 3.10+ (uses PEP 604 union syntax)
- [signal-cli](https://github.com/AsamK/signal-cli) (`brew install signal-cli`)
- [Claude Code CLI](https://claude.com/claude-code) (for classification)
- A Notion integration token + the In database shared with the integration
- Signal app on your phone
- macOS (launchd)
- For card sync: Anki desktop with [AnkiConnect](https://ankiweb.net/shared/info/2055492159), Obsidian with [obsidian-to-anki](https://github.com/Pseudonium/Obsidian_to_Anki) and [advanced-uri](https://github.com/Vinzent03/obsidian-advanced-uri) plugins

## Setup

### 1. Install the CLI

```bash
pip install -e .
```

### 2. Link signal-cli to your phone

```bash
signal-cli link -n "Vault Capture" | tee >(xargs -L1 qrencode -t ANSI)
```

Signal app on your phone → Settings → Linked Devices → Link New Device → scan the QR code.

### 3. Configure secrets

Create `.env` in this directory:

```
SIGNAL_ACCOUNT='+1234567890'
SIGNAL_ALERT_ACCOUNT='+1234567891'
SIGNAL_ALERT_RECIPIENT='+1234567890'
```

`SIGNAL_ACCOUNT` is your number. `SIGNAL_ALERT_ACCOUNT` is the bot number outgoing alerts come from (typically a Google Voice line). `SIGNAL_ALERT_RECIPIENT` is whoever receives the alerts (probably you).

### 4. Notion integration

1. Create an integration at https://www.notion.so/profile/integrations (Internal, with "Insert content" capability)
2. Open your "In" database in Notion → `···` → Connections → add the new integration
3. Export the token:

```bash
export NOTION_TOKEN='ntn_...'
```

### 5. Install the launchd jobs

```bash
./install.sh
```

This installs two jobs (and requires `NOTION_TOKEN` to be exported):
- `com.mannat.signal-capture` — persistent daemon with auto-restart
- `com.mannat.signal-capture-health` — health check every 30 minutes

The token is written into the daemon plist's `EnvironmentVariables`. Re-running `install.sh` without `NOTION_TOKEN` exported will fail loudly rather than wipe it.

### 6. Test it

Send yourself a Note to Self on Signal. Within a couple seconds you should get:
- `[vault] captured.` (DB insert)
- `[sorted] <category> — <body>` (classified + routed)

## Usage

```bash
sl poll          # One-shot pull from Signal
sl daemon        # Run persistent daemon (normally managed by launchd)
sl view          # TUI message viewer (j/k navigate, / search, q quit)
sl list          # Print recent messages
sl list -n 5     # Last 5
sl count         # Total message count
sl health        # Pipeline health check
```

## Message routing

| Shape / category | Destination |
|---|---|
| Image + body contains "meal" | Claude vision estimates itemized calories; result sent back via Summertime as `[meal] ...`. No image saved, no DB writes. |
| `card` (Q.A. / C.{cloze} / [blot] / [salience]) | Daily note `## Signal` + `anki-sync` |
| `reminder` | `capture.db` reminders table — fired by Summertime |
| `todo` | Notion "In" database (queued + retried on failure) |
| `resource` | Daily note `## Links` |
| `sundry` | `4-Sundry/Running Sundry.md` |

### Cards

Card-shaped messages bypass the classifier and are written **verbatim** to today's daily note under `## Signal` — the obsidian-to-anki plugin parses them directly. Recognized formats:

| Input | Output |
|---|---|
| `Q. front\nA. back` (or single line) | Basic card |
| `C. The capital of France is {Paris}` | Cloze (auto-numbered `{{c1::Paris}}`; multiple `{x}` become c1/c2/c3) |
| `[blot] she ran the mile` | Auto-generated `Q. s.. r.. t.. m...\nA. she ran the mile` |
| `[salience] Q. ... A. ...` | Written to `CLAUDE/Running Salience.md` instead of the daily note |

After writing, `anki-sync` runs (debounced 10s) and triggers the obsidian-to-anki plugin's vault scan → AnkiConnect → AnkiWeb.

### Prefix short-circuits

Skip the classifier entirely:

| Prefix | Behavior |
|---|---|
| `todo: <body>` | Strip prefix, post straight to Notion |
| `reminder: <body>` | Strip prefix, ask Claude for just the `fire_at` time, insert into reminders table. Falls back to todo if no time can be parsed. |

### Corrections

Reply to a `[sorted]` or `[rerouted]` confirmation with a category name to reroute:

```
[sorted] sundry — talk with Colin about mechinterp tomorrow
  ↳ reply: "todo"
[rerouted] sundry → todo — talk with Colin about mechinterp tomorrow
```

You can chain corrections by replying to `[rerouted]`. Cards cannot be rerouted.

Reminder-specific replies (to `[sorted] reminder @ TIME — body`):
- `cancel` → mark cancelled
- `6pm` or `+30 min` or `delay by 5 mins` → reschedule
- A category name → cancel reminder and reroute

## Database

```
~/Documents/dot/CLAUDE/Artifacts/signal-capture/capture.db
```

Three tables:

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_timestamp INTEGER UNIQUE NOT NULL,
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    obsidian_synced INTEGER  -- NULL=non-card, 0=card pending, 1=synced
);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    signal_timestamp INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notion_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    context TEXT,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    last_error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
```

## How it works

1. You send a Note to Self on Signal from your phone
2. `signal-cli` (linked as a secondary device, running as a persistent daemon over a Unix socket) receives it via Signal's E2E encrypted protocol
3. The daemon inserts the message into SQLite and replies `[vault] captured.`
4. Routing priority:
   - **Image + "meal" in body?** Claude vision estimates calories, reply via Summertime alert. Body is the caption.
   - **Card-shaped?** Write raw to daily note `## Signal`, fire `anki-sync`
   - **`todo:` prefix?** Post to Notion "In", queue on failure
   - **`reminder:` prefix?** Haiku extracts `fire_at`, insert into reminders table (or fall back to todo)
   - **Else** Haiku classifies into `reminder | todo | resource | sundry`, daemon routes accordingly
5. Daemon replies `[sorted] <category> — <message>` (debounced 8s — bursts batch into one message)
6. Reply with a category name or time to correct

## Limitations

- macOS only (launchd, signal-cli Homebrew formula)
- signal-cli is unofficial — Signal protocol updates may require `brew upgrade signal-cli`
- Voice notes are ignored
- Images only do something if the body contains "meal" — otherwise they're ignored entirely (no save, no log)
- Classification takes ~5-10s per message (Haiku call)
- Notion reroute-cleanup only works while a todo is still in the queue; once sent, you must delete from Notion manually

## License

MIT or whatever
