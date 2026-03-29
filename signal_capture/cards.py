"""
Card detection, daily note management, and anki-sync triggering.
"""

import re
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
TEMPLATE_PATH = VAULT_ROOT / "CLAUDE" / "Templates" / "1 daily-template.md"
SALIENCE_PATH = VAULT_ROOT / "CLAUDE" / "Running Salience.md"
ANKI_SYNC_BIN = Path.home() / "bin" / "anki-sync"

# Patterns to detect cards in messages
# Q. ... A. ... on one line (user texting shorthand)
QA_SINGLE_LINE = re.compile(r"^Q\.\s+(.+?)\s+A\.\s+(.+)$", re.DOTALL)
# Q. ... already on separate lines
QA_MULTI_LINE = re.compile(r"^Q\.\s+", re.MULTILINE)
# Cloze: C. ... with {braces}
CLOZE_PATTERN = re.compile(r"^C\.\s+.*\{.+\}", re.MULTILINE | re.DOTALL)


def _split_blocks(body: str) -> list[str]:
    """Split a message into blocks separated by blank lines."""
    blocks = re.split(r"\n\s*\n", body.strip())
    return [b.strip() for b in blocks if b.strip()]


def _is_single_card(block: str) -> bool:
    """Check if a single block is an Anki card."""
    if QA_SINGLE_LINE.match(block):
        return True
    if QA_MULTI_LINE.match(block) and re.search(r"^A\.\s+", block, re.MULTILINE):
        return True
    if CLOZE_PATTERN.match(block):
        return True
    return False


def is_card(body: str) -> bool:
    """Check if a message contains one or more Anki cards."""
    blocks = _split_blocks(body)
    return len(blocks) > 0 and all(_is_single_card(b) for b in blocks)


def _format_single_card(block: str) -> str:
    """Format a single card block for Obsidian-to-Anki."""
    m = QA_SINGLE_LINE.match(block)
    if m:
        return f"Q. {m.group(1).strip()}\nA. {m.group(2).strip()}"
    return block


def format_card(body: str) -> str:
    """Format a message into the correct card syntax for Obsidian-to-Anki.

    Splits multi-card messages and ensures Q./A. are on separate lines.
    """
    blocks = _split_blocks(body)
    return "\n\n".join(_format_single_card(b) for b in blocks)


def get_daily_note_path(dt: datetime) -> Path:
    """Get the path to the daily note for a given datetime."""
    year_num = dt.year - 2024
    year_folder = f"{year_num}-{dt.year}"
    month_folder = f"{dt.month}-{dt.strftime('%B')}"
    filename = dt.strftime("%m-%d") + ".md"
    return VAULT_ROOT / "0-Inbox" / year_folder / month_folder / filename


def ensure_daily_note(path: Path, dt: datetime) -> None:
    """Create the daily note from template if it doesn't exist."""
    if path.exists():
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    template = TEMPLATE_PATH.read_text()
    date_str = dt.strftime("%Y-%m-%d")
    content = template.replace("{{date}}", date_str)
    path.write_text(content)


def append_card_to_daily_note(card_text: str, dt: datetime) -> Path:
    """Append a card to the daily note under ## Signal section."""
    path = get_daily_note_path(dt)
    ensure_daily_note(path, dt)

    content = path.read_text()

    if "## Signal" not in content:
        # Add Signal section at the bottom
        content = content.rstrip() + "\n\n## Signal\n"

    # Append card after the Signal section
    content = content.rstrip() + "\n\n" + card_text + "\n"
    path.write_text(content)

    return path


def anki_pre_sync() -> bool:
    """Pull AnkiWeb → local Anki before appending cards. Blocking. Returns True on success."""
    try:
        result = subprocess.run(
            [str(ANKI_SYNC_BIN), "--sync-only"],
            timeout=60,
            capture_output=True,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"anki pre-sync failed: {e}", flush=True)
        return False


def trigger_anki_sync() -> None:
    """Fire anki-sync (full: scan vault + sync to AnkiWeb)."""
    try:
        subprocess.Popen(
            [str(ANKI_SYNC_BIN)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # anki-sync not installed, skip silently


def is_salience(body: str) -> bool:
    """Check if a message is a salience prompt (prefixed with [salience])."""
    return body.strip().lower().startswith("[salience]")


def process_salience(body: str, signal_timestamp: int) -> str:
    """Strip [salience] prefix, format the card, and append to Running Salience.

    Returns 'success', 'not_card', or 'sync_failed'.
    """
    stripped = re.sub(r"^\[salience\]\s*", "", body.strip(), flags=re.IGNORECASE)
    if not is_card(stripped):
        return "not_card"

    if not anki_pre_sync():
        print("Pre-sync failed, deferring salience card", flush=True)
        return "sync_failed"

    card_text = format_card(stripped)
    content = SALIENCE_PATH.read_text() if SALIENCE_PATH.exists() else ""
    content = content.rstrip() + "\n\n" + card_text + "\n"
    SALIENCE_PATH.write_text(content)

    print(f"Salience prompt appended to {SALIENCE_PATH.name}", flush=True)
    trigger_anki_sync()
    print("anki-sync triggered", flush=True)
    return "success"


def process_card(body: str, signal_timestamp: int) -> str:
    """If the message is a card, append to daily note and sync.

    Returns 'success', 'not_card', or 'sync_failed'.
    """
    if not is_card(body):
        return "not_card"

    if not anki_pre_sync():
        print("Pre-sync failed, deferring card", flush=True)
        return "sync_failed"

    dt = datetime.fromtimestamp(signal_timestamp / 1000)
    card_text = format_card(body)
    path = append_card_to_daily_note(card_text, dt)
    print(f"Card appended to {path.name}", flush=True)

    trigger_anki_sync()
    print("anki-sync triggered", flush=True)
    return "success"
