# Signal Capture

## Stack
- **Language**: Python 3.8+
- **Signal interface**: signal-cli 0.14.1 (Homebrew)
- **Database**: SQLite (stdlib `sqlite3`)
- **Scheduling**: launchd (macOS)
- **Target**: Obsidian vault at `~/Documents/Obsidian Vaults/dot`

## Architecture

### Data flow
```
Signal "Note to Self" → signal-cli → signal_capture.py → SQLite database → (manual/on-demand) → vault markdown
```

The database is the source of truth. Vault markdown files are NOT automatically generated — they are rendered on demand as a view layer over the database.

### Database
- Location: `~/Documents/Obsidian Vaults/dot/CLAUDE/Artifacts/signal-capture/capture.db`
- Tables: `messages` (id, timestamp, body, captured_at, etc.)
- All received messages are inserted here; nothing is written directly to vault markdown

### Scripts
- `signal_capture.py` — Main pipeline: polls signal-cli → filters Note to Self → inserts into SQLite → sends confirmation reply
- `health_check.py` — Staleness monitor: reads health timestamp, fires macOS notification if >1 hour stale
- `install.sh` — Writes and loads launchd plists
- `uninstall.sh` — Unloads and removes launchd plists

### Key functions (`signal_capture.py`)
- `receive_messages()` — Calls `signal-cli receive --output=json`, parses NDJSON output
- `extract_self_messages()` — Filters for messages where sender == own account (Note to Self)
- `insert_messages()` — Inserts captured messages into SQLite, deduplicates by Signal timestamp
- `send_confirmation()` — Replies to self with `[vault] N notes captured.`
- `update_health()` — Writes ISO timestamp to `~/.signal-capture-health`

### Config
- `.env` — `SIGNAL_ACCOUNT='+1XXXXXXXXXX'` (E.164 format, not committed)
- Environment variable `SIGNAL_ACCOUNT` takes precedence over `.env`

### launchd jobs
- `com.mannat.signal-capture` — Runs `signal_capture.py` every 120 seconds
- `com.mannat.signal-capture-health` — Runs `health_check.py` every 1800 seconds

## Dev commands
```bash
python signal_capture.py      # Manual run (requires .env)
python health_check.py        # Check pipeline health
./install.sh                  # Install launchd jobs
./uninstall.sh                # Remove launchd jobs
```

## Conventions
- No external Python dependencies (stdlib only: subprocess, json, pathlib, datetime, sqlite3)
- signal-cli is called as a subprocess, not via a library
- Health file at `~/.signal-capture-health` is a single ISO timestamp
- Confirmation replies are non-critical — failures are silently ignored
- `.env` is gitignored; never commit phone numbers
- Database lives inside the vault (visible to Obsidian) at `CLAUDE/Artifacts/signal-capture/`
