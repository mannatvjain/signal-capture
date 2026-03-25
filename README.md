# Signal Capture

A Python script that pipes your Signal "Note to Self" messages into a local SQLite database.

## Features

- **Note to Self Pipeline** — Text yourself on Signal from your phone, messages land in a local database instantly
- **E2E Encrypted** — Uses Signal's own protocol; messages are never plaintext on a server
- **Delivery Confirmation** — Replies "[vault] 1 note captured." so you know it landed
- **Persistent Daemon** — Runs via launchd with `KeepAlive`; survives sleep/wake, auto-restarts on crash
- **Health Monitoring** — Alerts you via macOS notification if the daemon hasn't run in over an hour
- **`sl` CLI** — Poll, view (TUI), list, count, health check

## Requirements

- Python 3.8+
- [signal-cli](https://github.com/AsamK/signal-cli) (`brew install signal-cli`)
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
  Mar 24 20:58  Test VII - daemon II
  Mar 24 19:48  Test IV - sent message
```

## How It Works

1. You send a "Note to Self" message on Signal from your phone
2. `signal-cli` (linked as a secondary device, running as a daemon) receives it via Signal's E2E encrypted protocol
3. The daemon filters for self-messages and inserts them into the SQLite database
4. A confirmation reply is sent back to your Signal via the daemon's JSON-RPC socket: `[vault] 1 note captured.`
5. A health file is updated; if it goes stale, macOS sends you a notification

## Limitations

- macOS only (launchd + signal-cli Homebrew formula)
- signal-cli is an unofficial client — Signal protocol updates may occasionally require a `brew upgrade signal-cli`
- Only captures text messages; attachments (photos, voice notes) are ignored for now

## License

MIT or whatever
