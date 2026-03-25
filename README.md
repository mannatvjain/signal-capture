# Signal Capture

A Python script that pipes your Signal "Note to Self" messages into a local SQLite database, auto-classifies them, and routes them into your Obsidian vault.

## Features

- **Note to Self Pipeline** — Text yourself on Signal from your phone, messages land in a local database instantly
- **E2E Encrypted** — Uses Signal's own protocol; messages are never plaintext on a server
- **AI Triage** — Claude (via `claude -p`) classifies messages into categories and routes them to the right vault location
- **Anki Card Detection** — Messages in `Q. / A.` or `C. {cloze}` format are appended to the daily note and synced to Anki automatically
- **Reply-based Corrections** — Reply to a `[sorted]` or `[rerouted]` message with a category name to fix misclassifications
- **Persistent Daemon** — Runs via launchd with `KeepAlive`; survives sleep/wake, auto-restarts on crash
- **Health Monitoring** — Alerts you via macOS notification if the daemon hasn't run in over an hour
- **`sl` CLI** — Poll, view (TUI), list, count, health check

## Requirements

- Python 3.8+
- [signal-cli](https://github.com/AsamK/signal-cli) (`brew install signal-cli`)
- [Claude Code CLI](https://claude.com/claude-code) (for message classification)
- Signal app on your phone
- macOS (for launchd scheduling)

## Setup

### 1. Install the CLI

```bash
pip install -e .
```

### 2. Link signal-cli to your phone

```bash
signal-cli link -n "Vault Capture" | tee >(xargs -L1 qrencode -t ANSI)
```

Open Signal on your phone → Settings → Linked Devices → Link New Device → scan the QR code.

### 3. Configure your phone number

Create a `.env` file in this directory:

```
SIGNAL_ACCOUNT='+1234567890'
```

### 4. Test it

Send yourself a "Note to Self" message on Signal, then run:

```bash
sl poll
```

### 5. Start the daemon

```bash
./install.sh
```

This installs two launchd jobs:
- `com.mannat.signal-capture` — persistent daemon (instant delivery, auto-restart)
- `com.mannat.signal-capture-health` — health check every 30 minutes

## Usage

```bash
sl poll          # One-shot pull from Signal
sl daemon        # Run persistent daemon (managed by launchd)
sl view          # TUI message viewer (j/k, /, q)
sl list          # Print recent messages to stdout
sl list -n 5     # Last 5 messages
sl count         # Total message count
sl health        # Check pipeline health

# launchd management
./install.sh     # Install and start daemon
./uninstall.sh   # Stop and remove daemon
```

## Message Flow

Every message gets two Signal confirmations:

1. `[vault] captured.` — message is in the database
2. `[sorted] <category> — <message>` — message has been classified and routed

### Categories

| Category | Destination |
|---|---|
| **card** | Daily note `## Signal` section + `anki-sync` |
| **resource** | Daily note `## Links` section |
| **todo** | Daily note `### Todo` section (cleaned up) |
| **good-advice** | `4-Sundry/A list of good advice.md` |
| **founders** | `4-Sundry/Founders.md` |
| **deltas** | `4-Sundry/Running Deltas.md` |
| **sundry** | `4-Sundry/Running Sundry.md` |

### Corrections

If a message gets classified wrong, reply to the `[sorted]` message with the correct category:

```
[sorted] sundry — talk with Colin about mechinterp tomorrow
  ↳ reply: "todo"
[rerouted] sundry → todo — talk with Colin about mechinterp tomorrow
```

You can chain corrections by replying to `[rerouted]` messages too. Cards cannot be rerouted.

### Anki Cards

Messages starting with `Q.` / `A.` (basic) or `C.` with `{cloze}` (cloze deletion) are detected as cards. They are:
1. Reformatted for the Obsidian-to-Anki plugin regex
2. Appended to the daily note's `## Signal` section (using the message's send timestamp, not capture time)
3. Synced via `anki-sync` (launches Obsidian + Anki if needed)

## Output

Messages are stored in a SQLite database at:

```
~/Documents/Obsidian Vaults/dot/CLAUDE/Artifacts/signal-capture/capture.db
```

### Schema

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_timestamp INTEGER UNIQUE NOT NULL,
    body TEXT NOT NULL,
    captured_at TEXT NOT NULL
);
```

### Example

```
$ sl list
  Mar 24 23:15  C. All {privileged bases} are interpretable but not all {interpretable bases} are privileged.
  Mar 24 23:13  Q. What was the vision of enumerative safety? ↩ A. That we could understand every feature in a model and look through them for undesirable behavior.
  Mar 24 22:49  - Q. What do we call force fields applicable to any chemical system in any environment? ↩ A. Universal force fields.
```

## How It Works

1. You send a "Note to Self" message on Signal from your phone
2. `signal-cli` (linked as a secondary device, running as a daemon) receives it via Signal's E2E encrypted protocol
3. The daemon inserts the message into SQLite and replies `[vault] captured.`
4. If it's a card: appends to daily note `## Signal`, fires `anki-sync`
5. Otherwise: `claude -p --model haiku` classifies it, daemon routes to the appropriate vault file
6. Daemon replies `[sorted] <category> — <message>`
7. You can reply with a category name to correct misclassifications

## Limitations

- macOS only (launchd + signal-cli Homebrew formula)
- signal-cli is an unofficial client — Signal protocol updates may occasionally require a `brew upgrade signal-cli`
- Only captures text messages; attachments (photos, voice notes) are ignored for now
- Classification requires Claude Code CLI (`claude -p`) to be installed and authenticated
- Classification takes ~5-10 seconds per message (Haiku model)

## License

MIT or whatever
