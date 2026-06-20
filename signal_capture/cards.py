"""
Card detection and daily-note appending.

Q./A./C. messages are written verbatim to the user's daily note under
## Signal; the obsidian-to-anki plugin parses them directly.
"""

import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path.home() / "Documents" / "dot"
TEMPLATE_PATH = VAULT_ROOT / "CLAUDE" / "Templates" / "1 daily-template.md"
SALIENCE_PATH = VAULT_ROOT / "CLAUDE" / "Running Salience.md"
ANKI_SYNC_BIN = VAULT_ROOT / "CLAUDE" / "Artifacts" / "anki-sync"

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


def get_daily_note_path(dt: datetime) -> Path:
    """Get the path to the daily note for a given datetime."""
    year_num = dt.year - 2025
    year_folder = f"{year_num}-{dt.year}"
    month_folder = f"{dt.month - 4}-{dt.strftime('%B')}"
    filename = dt.strftime("%m-%d") + ".md"
    return VAULT_ROOT / "0-Journal" / year_folder / month_folder / filename


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


_sync_timer: threading.Timer | None = None
_sync_lock = threading.Lock()
_SYNC_DEBOUNCE = 10  # seconds — wait for burst of cards to finish


def trigger_anki_sync() -> None:
    """Fire anki-sync, debounced. Resets timer on each call so a burst of cards syncs once."""
    global _sync_timer
    with _sync_lock:
        if _sync_timer is not None:
            _sync_timer.cancel()
        _sync_timer = threading.Timer(_SYNC_DEBOUNCE, _fire_anki_sync)
        _sync_timer.daemon = True
        _sync_timer.start()


def _fire_anki_sync() -> None:
    """Actually fire anki-sync after debounce window."""
    try:
        subprocess.Popen(
            [str(ANKI_SYNC_BIN)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("anki-sync triggered (debounced)", flush=True)
    except FileNotFoundError:
        pass  # anki-sync not installed, skip silently


def _blot_line(text: str) -> str:
    words = text.split(" ")
    result = []
    for word in words:
        if not word:
            result.append(word)
            continue
        trail = ""
        core = word
        while core and core[-1] in ".,;:!?)\"'":
            trail = core[-1] + trail
            core = core[:-1]
        if not core:
            result.append(word)
        elif len(core) == 1:
            result.append(core + trail)
        else:
            result.append(core[0] + "." * (len(core) - 1) + trail)
    return " ".join(result)


def blot_text(text: str) -> str:
    """Transform text so each word shows only its first letter, rest as dots.

    Preserves punctuation at word boundaries, capitalization, and numbers.
    Words are split on spaces only. Hyphenated words and contractions are one unit.
    Newlines are preserved — each line is blotted independently.
    """
    return "\n".join(_blot_line(line) for line in text.split("\n"))


def _parse_card(block: str) -> tuple[str, dict[str, str]] | None:
    """Parse a single card block into (note_type, fields). Returns None if not a card."""
    block = block.strip()
    if not block:
        return None
    if CLOZE_PATTERN.match(block):
        text = re.sub(r"^C\.\s+", "", block, count=1)
        return ("Cloze", {"Text": text})
    m = QA_SINGLE_LINE.match(block)
    if m:
        return ("Basic", {"Front": m.group(1).strip(), "Back": m.group(2).strip()})
    if QA_MULTI_LINE.match(block) and re.search(r"^A\.\s+", block, re.MULTILINE):
        front_match = re.match(r"^Q\.\s+(.*?)(?=^A\.\s+)", block, re.DOTALL | re.MULTILINE)
        back_match = re.search(r"^A\.\s+(.+)$", block, re.DOTALL | re.MULTILINE)
        if front_match and back_match:
            return ("Basic", {"Front": front_match.group(1).strip(), "Back": back_match.group(1).strip()})
    return None


def render_block(note_type: str, fields: dict[str, str]) -> str:
    """Emit a START/END block. First field is anonymous; subsequent fields are 'Name: ...' prefixed."""
    lines = ["START", note_type]
    for i, (name, value) in enumerate(fields.items()):
        value_lines = value.split("\n")
        if i == 0:
            lines.extend(value_lines)
        else:
            lines.append(f"{name}: {value_lines[0]}")
            lines.extend(value_lines[1:])
    lines.append("END")
    return "\n".join(lines)


def render_cards(body: str) -> str:
    """Render a (possibly multi-card) message as START/END blocks. Non-card blocks pass through."""
    blocks = _split_blocks(body)
    rendered = []
    for block in blocks:
        parsed = _parse_card(block)
        if parsed is None:
            rendered.append(block)
        else:
            note_type, fields = parsed
            rendered.append(render_block(note_type, fields))
    return "\n\n".join(rendered)


def is_blot(body: str) -> bool:
    """Check if a message is a blot request (prefixed with [blot])."""
    return body.strip().lower().startswith("[blot]")


def process_blot(body: str, signal_timestamp: int) -> str:
    """Strip [blot] prefix, generate Q (blotted) / A (original) card, append to daily note."""
    stripped = re.sub(r"^\[blot\]\s*", "", body.strip(), flags=re.IGNORECASE)
    if not stripped:
        return "not_card"

    if not anki_pre_sync():
        print("Pre-sync failed, deferring blot card", flush=True)
        return "sync_failed"

    card_text = render_block("Basic", {"Front": blot_text(stripped), "Back": stripped})

    dt = datetime.fromtimestamp(signal_timestamp / 1000)
    path = append_card_to_daily_note(card_text, dt)
    print(f"Blot card appended to {path.name}", flush=True)
    trigger_anki_sync()
    return "success"


def is_salience(body: str) -> bool:
    """Check if a message is a salience prompt (prefixed with [salience])."""
    return body.strip().lower().startswith("[salience]")


def process_salience(body: str, signal_timestamp: int) -> str:
    """Strip [salience] prefix and append the raw card text to Running Salience."""
    stripped = re.sub(r"^\[salience\]\s*", "", body.strip(), flags=re.IGNORECASE)
    if not is_card(stripped):
        return "not_card"

    if not anki_pre_sync():
        print("Pre-sync failed, deferring salience card", flush=True)
        return "sync_failed"

    content = SALIENCE_PATH.read_text() if SALIENCE_PATH.exists() else ""
    content = content.rstrip() + "\n\n" + render_cards(stripped) + "\n"
    SALIENCE_PATH.write_text(content)
    print(f"Salience prompt appended to {SALIENCE_PATH.name}", flush=True)
    trigger_anki_sync()
    return "success"


def process_card(body: str, signal_timestamp: int) -> str:
    """Append raw card text to today's daily note ## Signal, trigger anki-sync."""
    if not is_card(body):
        return "not_card"

    if not anki_pre_sync():
        print("Pre-sync failed, deferring card", flush=True)
        return "sync_failed"

    dt = datetime.fromtimestamp(signal_timestamp / 1000)
    path = append_card_to_daily_note(render_cards(body), dt)
    print(f"Card appended to {path.name}", flush=True)

    trigger_anki_sync()
    return "success"
