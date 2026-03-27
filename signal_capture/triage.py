"""
Message triage via claude -p.

Classifies non-card messages into categories and routes them
to the appropriate vault location.
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from signal_capture.capture import DB_PATH
from signal_capture.cards import get_daily_note_path, ensure_daily_note

VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
SUNDRY = VAULT_ROOT / "4-Sundry"

TARGETS = {
    "reminder": None,  # reminders table in capture.db
    "resource": None,  # daily note ## Links
    "todo": None,      # daily note ### Todo
    "good-advice": SUNDRY / "A list of good advice.md",
    "founders": SUNDRY / "Founders.md",
    "deltas": SUNDRY / "Running Deltas.md",
    "sundry": SUNDRY / "Running Sundry.md",
}

CLASSIFY_PROMPT_TEMPLATE = """\
You are a message classifier. Given a captured message, classify it into exactly one category and return JSON.

Current time: {current_time}

Categories:
- "reminder": Time-bound reminders — messages that reference a specific time to be reminded about something (e.g. "speak with colin about mechinterp at 1pm tomorrow", "remind me to call mom at 5", "gym at 3pm"). The key signal is a specific time/date to fire the reminder.
- "resource": Links, articles, papers, videos, things to look at or read
- "todo": Near-term actionable items with time pressure but NO specific reminder time (e.g. "email Prof Fusi this week"). NOT vague aspirations like "read more books" or "explore X someday"
- "good-advice": Wisdom, life advice, principles to remember
- "founders": Specifically about David Senra's Founders Podcast (episodes, quotes, takeaways)
- "deltas": Changes, updates, observations about how things are going or shifting
- "sundry": Everything else — random thoughts, observations, ideas that don't fit above

For "reminder" messages:
- "cleaned": A short description of what to be reminded about
- "fire_at": The absolute ISO 8601 datetime with timezone offset (e.g. "2026-03-26T13:00:00-04:00"). Resolve relative references ("tomorrow", "thursday", "in 2 hours") using the current time above. Timezone is America/New_York (UTC-4 during EDT, UTC-5 during EST).

For "todo" messages:
- "cleaned": One concise action line
- "context": Optional brief context line (only if important detail would be lost)

Return ONLY valid JSON in this format:
{{"category": "<category>", "cleaned": "<description>", "context": "<for todos: optional or null>", "fire_at": "<for reminders: ISO 8601 or null>", "original": "<original message>"}}

Message:
"""


CLASSIFICATION_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["reminder", "resource", "todo", "good-advice", "founders", "deltas", "sundry"],
        },
        "cleaned": {"type": ["string", "null"]},
        "context": {"type": ["string", "null"]},
        "fire_at": {"type": ["string", "null"]},
        "original": {"type": "string"},
    },
    "required": ["category", "original"],
})


def classify_message(body: str) -> dict | None:
    """Call claude -p to classify a message. Returns parsed JSON or None."""
    now = datetime.now().astimezone()
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(current_time=now.isoformat()) + body.strip()

    try:
        result = subprocess.run(
            [
                "/Users/mannatvjain/.local/bin/claude", "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", CLASSIFICATION_SCHEMA,
                "--system-prompt", "You are a JSON classifier. No tools, no file reads, no exploration.",
                "--allowedTools", "",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout.strip()
        parsed = json.loads(output)
        # --output-format json puts the schema result in structured_output
        if "structured_output" in parsed:
            return parsed["structured_output"]
        return parsed
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
        print(f"Classification failed: {e}", flush=True)
        return None


def append_to_file(path: Path, text: str) -> None:
    """Append text to a file on the next line."""
    content = path.read_text() if path.exists() else ""
    content = content.rstrip() + "\n" + text.strip() + "\n"
    path.write_text(content)


def route_resource(body: str, dt: datetime) -> None:
    """Add a resource link to the daily note's ## Links section."""
    path = get_daily_note_path(dt)
    ensure_daily_note(path, dt)
    content = path.read_text()

    # Find ## Links and append after it
    if "## Links" in content:
        idx = content.index("## Links")
        end_of_line = content.index("\n", idx)
        # Find the next ## section or end of file
        next_section = content.find("\n## ", end_of_line + 1)
        if next_section == -1:
            insert_at = len(content)
        else:
            insert_at = next_section

        entry = body.strip()
        if not entry.startswith("-"):
            entry = f"- {entry}"
        content = content[:insert_at].rstrip() + "\n" + entry + "\n" + content[insert_at:]
        path.write_text(content)
    else:
        # No Links section — append one
        entry = body.strip()
        if not entry.startswith("-"):
            entry = f"- {entry}"
        content = content.rstrip() + "\n\n## Links\n\n" + entry + "\n"
        path.write_text(content)


def route_todo(classification: dict, dt: datetime) -> None:
    """Add a todo to the daily note's ### Todo section."""
    path = get_daily_note_path(dt)
    ensure_daily_note(path, dt)
    content = path.read_text()

    cleaned = classification.get("cleaned", classification.get("original", ""))
    context = classification.get("context")

    entry = f"- [ ] {cleaned}"
    if context:
        entry += f"\n    - {context}"

    if "### Todo" in content:
        idx = content.index("### Todo")
        end_of_line = content.index("\n", idx)
        # Find next ### or ## section
        next_section_h3 = content.find("\n### ", end_of_line + 1)
        next_section_h2 = content.find("\n## ", end_of_line + 1)
        candidates = [x for x in [next_section_h3, next_section_h2] if x != -1]
        insert_at = min(candidates) if candidates else len(content)

        content = content[:insert_at].rstrip() + "\n" + entry + "\n" + content[insert_at:]
        path.write_text(content)
    else:
        # Create ### Todo after ## Links (or after frontmatter)
        if "## Links" in content:
            idx = content.index("## Links")
            end_of_line = content.index("\n", idx)
            # Insert Todo section right after Links section content
            next_section = content.find("\n## ", end_of_line + 1)
            if next_section == -1:
                insert_at = len(content)
            else:
                insert_at = next_section

            todo_block = f"\n\n### Todo\n\n{entry}\n"
            content = content[:insert_at].rstrip() + todo_block + content[insert_at:]
        else:
            content = content.rstrip() + "\n\n### Todo\n\n" + entry + "\n"
        path.write_text(content)


def route_reminder(body: str, fire_at: str, signal_timestamp: int) -> bool:
    """Insert a reminder into the reminders table. Returns True on success."""
    try:
        # Validate fire_at is a valid ISO 8601 datetime
        datetime.fromisoformat(fire_at)
    except (ValueError, TypeError):
        print(f"Invalid fire_at: {fire_at}", flush=True)
        return False

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "INSERT INTO reminders (body, fire_at, signal_timestamp, created_at) VALUES (?, ?, ?, ?)",
            (body.strip(), fire_at, signal_timestamp, datetime.now().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"Reminder insert failed: {e}", flush=True)
        return False
    finally:
        conn.close()


def remove_reminder(body: str) -> bool:
    """Remove a reminder by body text match. Returns True if found and removed."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.execute(
            "DELETE FROM reminders WHERE body = ? AND fired = 0", (body.strip(),)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def cancel_reminder_by_timestamp(signal_timestamp: int) -> str | None:
    """Cancel an unfired reminder by signal_timestamp. Returns the body if found."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT body FROM reminders WHERE signal_timestamp = ? AND fired = 0 AND cancelled = 0",
            (signal_timestamp,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE reminders SET cancelled = 1 WHERE signal_timestamp = ? AND fired = 0",
            (signal_timestamp,),
        )
        conn.commit()
        return row[0]
    finally:
        conn.close()


def reschedule_reminder_by_timestamp(signal_timestamp: int, new_fire_at: str) -> tuple[str | None, str | None]:
    """Reschedule an unfired reminder. Returns (body, old_fire_at) or (None, None)."""
    try:
        datetime.fromisoformat(new_fire_at)
    except (ValueError, TypeError):
        return None, None

    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT body, fire_at FROM reminders WHERE signal_timestamp = ? AND fired = 0 AND cancelled = 0",
            (signal_timestamp,),
        ).fetchone()
        if not row:
            return None, None
        conn.execute(
            "UPDATE reminders SET fire_at = ? WHERE signal_timestamp = ? AND fired = 0 AND cancelled = 0",
            (new_fire_at, signal_timestamp),
        )
        conn.commit()
        return row[0], row[1]
    finally:
        conn.close()


def parse_reschedule_time(text: str, current_fire_at: str | None = None) -> str | None:
    """Use Claude to parse a freeform time into ISO 8601. Returns fire_at or None."""
    now = datetime.now().astimezone()
    anchor_line = ""
    if current_fire_at:
        anchor_line = f"\nCurrently scheduled for: {current_fire_at}\nRelative adjustments like 'delay by 5 mins', '+2h', 'push back 30 min' should be relative to the CURRENT scheduled time, not the current time."

    prompt = f"""Current time: {now.isoformat()}
Timezone: America/New_York{anchor_line}

The user wants to reschedule a reminder to: {text.strip()}

Return the absolute ISO 8601 datetime with timezone offset (e.g. "2026-03-26T13:00:00-04:00").
Resolve absolute references ("tomorrow", "thursday", "3pm") using the current time. Resolve relative adjustments ("delay by 5 mins", "+2h") using the current scheduled time if provided, otherwise the current time."""

    schema = json.dumps({
        "type": "object",
        "properties": {
            "fire_at": {"type": "string"},
        },
        "required": ["fire_at"],
    })

    try:
        result = subprocess.run(
            [
                "/Users/mannatvjain/.local/bin/claude", "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", schema,
                "--system-prompt", "You are a time parser. Return only the requested JSON.",
                "--allowedTools", "",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=30,
        )
        parsed = json.loads(result.stdout.strip())
        fire_at = parsed.get("structured_output", parsed).get("fire_at")
        # Validate
        datetime.fromisoformat(fire_at)
        return fire_at
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, TypeError):
        return None


def _route_to_category(body: str, category: str, dt: datetime, classification: dict | None = None, signal_timestamp: int = 0) -> None:
    """Route a message body to a specific category."""
    if category == "reminder":
        fire_at = (classification or {}).get("fire_at")
        cleaned = (classification or {}).get("cleaned", body)
        if fire_at:
            route_reminder(cleaned, fire_at, signal_timestamp)
        else:
            # No fire_at — re-classify needed, fall through to todo
            print("Reminder without fire_at, routing as todo", flush=True)
            cls = {"cleaned": cleaned, "context": None, "original": body}
            route_todo(cls, dt)
    elif category == "resource":
        route_resource(body, dt)
    elif category == "todo":
        cls = classification or {"cleaned": body, "context": None, "original": body}
        route_todo(cls, dt)
    elif category in TARGETS and TARGETS[category]:
        target = TARGETS[category]
        entry = body.strip()
        if not entry.startswith("-"):
            entry = f"- {entry}"
        append_to_file(target, entry)
    else:
        entry = body.strip()
        if not entry.startswith("-"):
            entry = f"- {entry}"
        append_to_file(TARGETS["sundry"], entry)


def _remove_from_category(body: str, category: str, dt: datetime) -> bool:
    """Remove a message from its current category location. Returns True if found and removed."""
    if category == "reminder":
        return remove_reminder(body)

    body_stripped = body.strip()

    if category in ("resource", "todo"):
        path = get_daily_note_path(dt)
        if not path.exists():
            return False
        content = path.read_text()

        # For todos, the cleaned version may differ from body — search for body substring
        # For resources, search for the body text
        lines = content.split("\n")
        new_lines = []
        removed = False
        skip_context = False

        for i, line in enumerate(lines):
            if not removed and body_stripped in line:
                removed = True
                skip_context = True
                continue
            # Skip indented context lines belonging to a removed todo
            if skip_context and line.startswith("    - "):
                skip_context = False
                continue
            skip_context = False
            new_lines.append(line)

        if removed:
            path.write_text("\n".join(new_lines))
            return True

    elif category in TARGETS and TARGETS[category]:
        target = TARGETS[category]
        if not target.exists():
            return False
        content = target.read_text()

        lines = content.split("\n")
        new_lines = []
        removed = False

        for line in lines:
            if not removed and body_stripped in line:
                removed = True
                continue
            new_lines.append(line)

        if removed:
            # Clean up double blank lines
            cleaned = "\n".join(new_lines)
            while "\n\n\n" in cleaned:
                cleaned = cleaned.replace("\n\n\n", "\n\n")
            target.write_text(cleaned)
            return True

    return False


def route_message(body: str, signal_timestamp: int) -> str | None:
    """Classify and route a non-card message. Returns the category or None."""
    classification = classify_message(body)
    if not classification:
        return None

    category = classification.get("category", "sundry")
    dt = datetime.fromtimestamp(signal_timestamp / 1000)

    _route_to_category(body, category, dt, classification, signal_timestamp)

    if category == "reminder":
        fire_at = classification.get("fire_at", "")
        try:
            fire_dt = datetime.fromisoformat(fire_at)
            time_str = fire_dt.strftime("%-I:%M %p")
            print(f"Routed to reminder @ {time_str}", flush=True)
            return f"reminder @ {time_str}"
        except (ValueError, TypeError):
            print(f"Routed to reminder (no valid time)", flush=True)
            return "reminder"
    else:
        print(f"Routed to {category}", flush=True)
        return category


def reroute_message(body: str, signal_timestamp: int, old_category: str, new_category: str) -> bool:
    """Remove a message from old_category and route to new_category."""
    dt = datetime.fromtimestamp(signal_timestamp / 1000)

    removed = _remove_from_category(body, old_category, dt)
    if not removed:
        print(f"Warning: could not remove from {old_category}, routing to {new_category} anyway", flush=True)

    if new_category == "reminder":
        # Need to classify to extract fire_at
        classification = classify_message(body)
        if classification and classification.get("fire_at"):
            _route_to_category(body, new_category, dt, classification, signal_timestamp)
        else:
            print("Reroute to reminder failed: could not extract time", flush=True)
            return False
    else:
        # For other categories, use body as-is (no claude cleanup)
        _route_to_category(body, new_category, dt, signal_timestamp=signal_timestamp)
    return True
