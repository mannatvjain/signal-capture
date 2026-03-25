"""
Message triage via claude -p.

Classifies non-card messages into categories and routes them
to the appropriate vault location.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from signal_capture.cards import get_daily_note_path, ensure_daily_note

VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
SUNDRY = VAULT_ROOT / "4-Sundry"

TARGETS = {
    "resource": None,  # daily note ## Links
    "todo": None,      # daily note ### Todo
    "good-advice": SUNDRY / "A list of good advice.md",
    "founders": SUNDRY / "Founders.md",
    "deltas": SUNDRY / "Running Deltas.md",
    "sundry": SUNDRY / "Running Sundry.md",
}

CLASSIFY_PROMPT = """\
You are a message classifier. Given a captured message, classify it into exactly one category and return JSON.

Categories:
- "resource": Links, articles, papers, videos, things to look at or read
- "todo": Near-term actionable items with time pressure (e.g. "talk with Colin about X tomorrow", "email Prof Fusi Thursday"). NOT vague aspirations like "read more books" or "explore X someday"
- "good-advice": Wisdom, life advice, principles to remember
- "founders": Specifically about David Senra's Founders Podcast (episodes, quotes, takeaways)
- "deltas": Changes, updates, observations about how things are going or shifting
- "sundry": Everything else — random thoughts, observations, ideas that don't fit above

For "todo" messages, also provide a cleaned-up version:
- One concise action line
- Optional brief context line (only if the original message has important detail that would be lost)

Return ONLY valid JSON in this format:
{"category": "<category>", "cleaned": "<for todos: one-liner action>", "context": "<for todos: optional extra context or null>", "original": "<original message>"}

Message:
"""


CLASSIFICATION_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["resource", "todo", "good-advice", "founders", "deltas", "sundry"],
        },
        "cleaned": {"type": ["string", "null"]},
        "context": {"type": ["string", "null"]},
        "original": {"type": "string"},
    },
    "required": ["category", "original"],
})


def classify_message(body: str) -> dict | None:
    """Call claude -p to classify a message. Returns parsed JSON or None."""
    prompt = CLASSIFY_PROMPT + body.strip()

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


def _route_to_category(body: str, category: str, dt: datetime, classification: dict | None = None) -> None:
    """Route a message body to a specific category."""
    if category == "resource":
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

    _route_to_category(body, category, dt, classification)
    print(f"Routed to {category}", flush=True)

    return category


def reroute_message(body: str, signal_timestamp: int, old_category: str, new_category: str) -> bool:
    """Remove a message from old_category and route to new_category."""
    dt = datetime.fromtimestamp(signal_timestamp / 1000)

    removed = _remove_from_category(body, old_category, dt)
    if not removed:
        print(f"Warning: could not remove from {old_category}, routing to {new_category} anyway", flush=True)

    # For todos routed via correction, use body as-is (no claude cleanup)
    _route_to_category(body, new_category, dt)
    return True
