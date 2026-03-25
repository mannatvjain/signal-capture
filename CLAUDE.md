# Signal Capture

## Stack
- **Language**: Python 3.8+
- **Signal interface**: signal-cli 0.14.1 (Homebrew)
- **Classification**: Claude Code CLI (`claude -p --model haiku`)
- **Database**: SQLite (stdlib `sqlite3`)
- **TUI**: Textual
- **Scheduling**: launchd (macOS)
- **Target**: Obsidian vault at `~/Documents/Obsidian Vaults/dot`

## Architecture

### Data flow
```
Signal "Note to Self" → signal-cli daemon → SQLite database
                                          → classify via claude -p
                                          → route to vault file
                                          → [if card] anki-sync
```

### Package (`signal_capture/`)
- `cli.py` — Entry point. Subcommands: `poll`, `daemon`, `view`, `list`, `count`, `health`
- `capture.py` — Core: signal-cli subprocess, DB init, message insertion, config loading
- `daemon.py` — Persistent daemon: runs signal-cli in daemon mode with Unix socket, handles confirmations, corrections, and routing
- `cards.py` — Anki card detection (`Q./A.`, `C.{cloze}`), daily note creation from template, `## Signal` section management, `anki-sync` triggering
- `triage.py` — Claude classification via `claude -p --model haiku --json-schema`, routing to vault files, rerouting on corrections
- `viewer.py` — Textual TUI with vim keys, search, terminal-native colors
- `health.py` — Staleness monitor with macOS notifications

### Database
- Location: `~/Documents/Obsidian Vaults/dot/CLAUDE/Artifacts/signal-capture/capture.db`
- Schema: `messages (id, signal_timestamp UNIQUE, body, captured_at)`
- All messages go here first; vault files are a routing destination, not source of truth

### Message routing
| Category | Destination |
|---|---|
| card | Daily note `## Signal` + `anki-sync` |
| resource | Daily note `## Links` |
| todo | Daily note `### Todo` (cleaned up by Claude) |
| good-advice | `4-Sundry/A list of good advice.md` |
| founders | `4-Sundry/Founders.md` |
| deltas | `4-Sundry/Running Deltas.md` |
| sundry | `4-Sundry/Running Sundry.md` |

### Confirmations
Two messages sent per capture:
1. `[vault] captured.` — DB insert confirmed
2. `[sorted] <category> — <body>` — classification + routing confirmed

### Corrections
Reply to `[sorted]` or `[rerouted]` with a category name to fix misclassification. Chains supported. Cards cannot be rerouted.

### Config
- `.env` — `SIGNAL_ACCOUNT='+1XXXXXXXXXX'` (E.164 format, not committed)
- Environment variable `SIGNAL_ACCOUNT` takes precedence over `.env`

### launchd jobs
- `com.mannat.signal-capture` — Persistent daemon with `KeepAlive` (auto-restart on crash/wake)
- `com.mannat.signal-capture-health` — Health check every 1800 seconds

## Dev commands
```bash
sl poll          # One-shot pull
sl daemon        # Persistent daemon (managed by launchd)
sl view          # TUI viewer
sl list          # Recent messages (default 20)
sl list -n 50    # Last 50
sl count         # Total count
sl health        # Pipeline health
./install.sh     # Install launchd jobs
./uninstall.sh   # Remove launchd jobs
```

## Conventions
- Dependencies: textual (TUI), stdlib for everything else
- signal-cli called as subprocess, daemon mode with Unix socket at `~/.signal-capture.socket`
- Classification via `claude -p` with `--json-schema` for structured output, `--allowedTools ""` to prevent tool use
- Health file at `~/.signal-capture-health` is a single ISO timestamp
- `.env` is gitignored; never commit phone numbers
- Daily notes use message's Signal timestamp, not capture time
- Bullet points: don't double-bullet if message already starts with `-`
- Daemon must be restarted after code changes (process caches modules)
