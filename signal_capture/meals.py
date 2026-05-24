"""
Calorie estimation for photos with "meal" in the caption.

Reads the signal-cli-cached attachment in place, asks Claude vision for an
itemized calorie estimate, returns a short multi-line text to send back.
Nothing is saved to the vault or any DB.
"""

import json
import subprocess
from pathlib import Path

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}

SIGNAL_CLI_ATTACHMENTS = Path.home() / ".local" / "share" / "signal-cli" / "attachments"
CLAUDE_CLI = "/Users/mannat/.local/bin/claude"

_CALORIE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "calories": {"type": "integer"},
                },
                "required": ["name", "calories"],
            },
        },
        "total_calories": {"type": "integer"},
    },
    "required": ["items", "total_calories"],
})


def resolve_attachment(att: dict) -> Path | None:
    """Find the cached attachment file in signal-cli's store."""
    att_id = att.get("id")
    if not att_id:
        return None
    src = SIGNAL_CLI_ATTACHMENTS / att_id
    return src if src.exists() else None


def estimate_calories(image_path: Path, caption: str = "") -> str | None:
    """Ask Claude vision for an itemized calorie estimate. Returns formatted text, or None."""
    caption_line = f'The user added this caption: "{caption.strip()}"\n\n' if caption.strip() else ""
    prompt = (
        f"{caption_line}"
        f"Read the image at {image_path}. Identify each visible food item and estimate calories. "
        "If the caption mentions items not visible in the photo, include them too."
    )

    try:
        result = subprocess.run(
            [
                CLAUDE_CLI, "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", _CALORIE_SCHEMA,
                "--system-prompt", "You estimate calories from food images. Return only the requested JSON.",
                "--allowedTools", "Read",
                "--permission-mode", "bypassPermissions",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=60,
        )
        parsed = json.loads(result.stdout.strip())
        data = parsed.get("structured_output", parsed)
        items = data.get("items", [])
        total = data.get("total_calories", 0)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"Calorie estimation failed: {e}", flush=True)
        return None

    lines = [f"~{total} kcal"]
    for it in items:
        lines.append(f"  • {it.get('name', '?')}: ~{it.get('calories', 0)} kcal")
    return "\n".join(lines)
