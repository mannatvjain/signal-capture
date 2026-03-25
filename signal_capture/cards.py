"""
Card detection, daily note management, and anki-sync triggering.
"""

import re
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
TEMPLATE_PATH = VAULT_ROOT / "CLAUDE" / "Templates" / "1 daily-template.md"
ANKI_SYNC_BIN = Path.home() / "bin" / "anki-sync"

# Patterns to detect cards in messages
# Q. ... A. ... on one line (user texting shorthand)
QA_SINGLE_LINE = re.compile(r"^Q\.\s+(.+?)\s+A\.\s+(.+)$", re.DOTALL)
# Q. ... already on separate lines
QA_MULTI_LINE = re.compile(r"^Q\.\s+", re.MULTILINE)
# Cloze: C. ... with {braces}
CLOZE_PATTERN = re.compile(r"^C\.\s+.*\{.+\}", re.MULTILINE)


def is_card(body: str) -> bool:
    """Check if a message contains an Anki card."""
    body = body.strip()
    if QA_SINGLE_LINE.match(body):
        return True
    if QA_MULTI_LINE.match(body) and re.search(r"^A\.\s+", body, re.MULTILINE):
        return True
    if CLOZE_PATTERN.match(body):
        return True
    return False


def format_card(body: str) -> str:
    """Format a message into the correct card syntax for Obsidian-to-Anki.

    Ensures Q./A. are on separate lines so the regex fires.
    """
    body = body.strip()

    # Single-line Q. ... A. ... → split onto two lines
    m = QA_SINGLE_LINE.match(body)
    if m:
        return f"Q. {m.group(1).strip()}\nA. {m.group(2).strip()}"

    # Already multi-line or cloze — return as-is
    return body


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


def process_card(body: str, signal_timestamp: int) -> bool:
    """If the message is a card, append to daily note and sync. Returns True if processed."""
    if not is_card(body):
        return False

    dt = datetime.fromtimestamp(signal_timestamp / 1000)
    card_text = format_card(body)
    path = append_card_to_daily_note(card_text, dt)
    print(f"Card appended to {path.name}", flush=True)

    trigger_anki_sync()
    print("anki-sync triggered", flush=True)

    return True
