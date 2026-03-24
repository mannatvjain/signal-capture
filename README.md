# Signal Capture

A Python script that pipes your Signal "Note to Self" messages into your Obsidian Vault.

## Features

- **Note to Self Pipeline** — Text yourself on Signal from your phone, messages appear in your vault automatically
- **E2E Encrypted** — Uses Signal's own protocol; messages are never plaintext on a server
- **Delivery Confirmation** — Bot replies "[vault] 1 note captured." so you know it landed
- **Vault-native Output** — Writes daily Markdown files with YAML frontmatter, ready for Obsidian
- **Month-based Organization** — Files sorted into `0-Inbox/2-2026/3-March/` matching your existing vault structure
- **Health Monitoring** — Alerts you via macOS notification if the pipeline hasn't run in over an hour
- **Runs via launchd** — Polls every 2 minutes in the background, no terminal window needed

## Requirements

- Python 3.8+
- [signal-cli](https://github.com/AsamK/signal-cli) (`brew install signal-cli`)
- Signal app on your phone
- macOS (for launchd scheduling)

## Setup

### 1. Link signal-cli to your phone

```bash
signal-cli link -n "Vault Capture" | tee >(xargs -L1 qrencode -t ANSI)
```

Open Signal on your phone → Settings → Linked Devices → Link New Device → scan the QR code.

### 2. Configure your phone number

Create a `.env` file in this directory:

```
SIGNAL_ACCOUNT='+1234567890'
```

### 3. Test it

Send yourself a "Note to Self" message on Signal, then run:

```bash
python signal_capture.py
```

### 4. Install the launchd jobs

```bash
./install.sh
```

This installs two launchd jobs:
- `com.mannat.signal-capture` — polls every 2 minutes
- `com.mannat.signal-capture-health` — health check every 30 minutes

## Usage

```bash
# Manual run
python signal_capture.py

# Check health
python health_check.py

# View launchd status
launchctl list | grep signal-capture

# Uninstall
./uninstall.sh
```

## Output Structure

```
dot/0-Inbox/
    2-2026/
        3-March/
            Signal Capture 2026-03-24.md
            Signal Capture 2026-03-25.md
        4-April/
            Signal Capture 2026-04-01.md
```

## Example Output

Each daily capture file looks like this:

```markdown
---
tags:
  - signal-capture
  - verdant
date: 2026-03-24
---

## Signal Captures

- **09:15** — Look into that paper on mechanistic interpretability Sarah mentioned
- **11:42** — Grocery run: oat milk, eggs, sriracha
- **14:30** — Idea: what if we used attention patterns as a proxy for feature importance in the AlphaGenome pipeline?
- **22:01** — Remember to email Prof. Fusi about office hours Thursday
```

## How It Works

1. You send a "Note to Self" message on Signal from your phone
2. `signal-cli` (linked as a secondary device) receives it via Signal's E2E encrypted protocol
3. The script filters for self-messages, writes them to the current day's capture file in `0-Inbox`
4. A confirmation reply is sent back to your Signal: `[vault] 1 note captured.`
5. A health file is updated; if it goes stale, macOS sends you a notification

## Limitations

- macOS only (launchd + signal-cli Homebrew formula)
- signal-cli is an unofficial client — Signal protocol updates may occasionally require a `brew upgrade signal-cli`
- Messages are pulled (polled), not pushed — up to 2 minute delay
- Only captures text messages; attachments (photos, voice notes) are ignored for now

## License

MIT or whatever
