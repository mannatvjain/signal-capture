"""
Meal photo classification and analysis via claude -p with vision.

Classifies incoming Signal photos as meal vs non-meal, saves to the
appropriate folder, and logs meal analysis to the running log.
"""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
MEALS_DIR = VAULT_ROOT / "CLAUDE" / "Running" / "Meals"
SIGNAL_IMAGES_DIR = VAULT_ROOT / "CLAUDE" / "Signal"
LOG_DIR = VAULT_ROOT / "CLAUDE" / "Running" / "Log"
CHEAT_SHEET = VAULT_ROOT / "CLAUDE" / "Running" / "68-Day Cheat Sheet.md"
SIGNAL_CLI_ATTACHMENTS = Path.home() / ".local" / "share" / "signal-cli" / "attachments"

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}

CLAUDE_CLI = "/Users/mannatvjain/.local/bin/claude"

CLASSIFY_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "is_meal": {"type": "boolean"},
    },
    "required": ["is_meal"],
})

MEAL_ANALYSIS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "meal_type": {
            "type": "string",
            "enum": ["breakfast", "lunch", "snack", "post-run"],
        },
        "items": {"type": "string"},
        "protocol_status": {
            "type": "string",
            "enum": ["ON", "PARTIAL", "OFF"],
        },
        "explanation": {"type": "string"},
        "calories": {"type": "integer"},
        "protein_g": {"type": "integer"},
        "carbs_g": {"type": "integer"},
        "fat_g": {"type": "integer"},
        "missing": {"type": ["string", "null"]},
        "off_protocol": {"type": ["string", "null"]},
    },
    "required": ["meal_type", "items", "protocol_status", "explanation",
                  "calories", "protein_g", "carbs_g", "fat_g"],
})


def _ext_for_content_type(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/heic": ".heic",
        "image/webp": ".webp",
    }.get(content_type, ".jpg")


def _resolve_attachment(att: dict) -> Path | None:
    """Find the attachment file in signal-cli's store."""
    att_id = att.get("id")
    if not att_id:
        return None
    src = SIGNAL_CLI_ATTACHMENTS / att_id
    return src if src.exists() else None


def _save_image(src: Path, dest_dir: Path, date_str: str, ext: str) -> Path:
    """Copy image to dest_dir with date-indexed naming."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(dest_dir.glob(f"{date_str}_*"))
    next_idx = len(existing) + 1
    dest = dest_dir / f"{date_str}_{next_idx}{ext}"
    shutil.copy2(src, dest)
    return dest


def classify_image(image_path: Path) -> bool:
    """Use Claude vision to determine if an image is a meal photo."""
    prompt = (
        f"Read the image at {image_path} and determine: is this a photo of food or a meal? "
        "Answer true if it shows food/drink that someone is about to eat or has eaten. "
        "Answer false for everything else."
    )
    try:
        result = subprocess.run(
            [
                CLAUDE_CLI, "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", CLASSIFY_SCHEMA,
                "--system-prompt", "You are an image classifier. Read the image, then return only JSON.",
                "--allowedTools", "Read",
                "--permission-mode", "bypassPermissions",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=30,
        )
        parsed = json.loads(result.stdout.strip())
        data = parsed.get("structured_output", parsed)
        return data.get("is_meal", False)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
        print(f"Image classification failed: {e}", flush=True)
        return False


def analyze_meal(image_path: Path, context_message: str, date: datetime) -> dict | None:
    """Analyze a meal photo against the running protocol."""
    # Load the cheat sheet for protocol context
    protocol_context = ""
    if CHEAT_SHEET.exists():
        protocol_context = CHEAT_SHEET.read_text()

    context_line = ""
    if context_message:
        context_line = f"""The user sent this message alongside the photo: "{context_message}"
IMPORTANT: Include any extra items the user mentions (e.g. "got an extra apple") in your analysis even if they're not visible in the photo. The user's message is the ground truth for what was eaten.

"""

    prompt = f"""{context_line}Date: {date.strftime('%A, %B %d')}

Protocol meal guidelines (from 68-Day Cheat Sheet):
{protocol_context}

Key rules:
- Breakfast (~550 cal): 3 scrambled eggs, fist-sized broccoli, handful cucumber, handful carrots, 1 whole orange, glass low-fat milk
- Lunch (~600-700 cal): Grilled protein + rice + steamed greens + whole fruit (4 rotations)
- Post-run: Fairlife protein shake
- PM snack: 15-20 counted almonds
- No bread/gluten (celiac), no sweets, no fried food
- 750 cal max per meal

Read the image at {image_path}, identify all visible food items AND any items the user mentioned, classify the meal type, estimate macros for the complete meal, and compare against protocol."""

    try:
        result = subprocess.run(
            [
                CLAUDE_CLI, "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", MEAL_ANALYSIS_SCHEMA,
                "--system-prompt", "You are a nutrition analyst for a runner following a strict protocol. Read the image, then return only JSON.",
                "--allowedTools", "Read",
                "--permission-mode", "bypassPermissions",
            ],
            input=prompt,
            capture_output=True, text=True, timeout=60,
        )
        parsed = json.loads(result.stdout.strip())
        return parsed.get("structured_output", parsed)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
        print(f"Meal analysis failed: {e}", flush=True)
        return None


def _get_log_path(date: datetime) -> Path:
    """Get the monthly log file path."""
    return LOG_DIR / f"{date.strftime('%Y-%m')}.md"


def append_meal_to_log(analysis: dict, image_name: str, date: datetime) -> bool:
    """Append meal analysis to the monthly running log."""
    log_path = _get_log_path(date)
    if not log_path.exists():
        return False

    meal_type = analysis.get("meal_type", "meal")
    items = analysis.get("items", "unknown")
    status = analysis.get("protocol_status", "OFF")
    explanation = analysis.get("explanation", "")
    cal = analysis.get("calories", 0)
    protein = analysis.get("protein_g", 0)
    carbs = analysis.get("carbs_g", 0)
    fat = analysis.get("fat_g", 0)
    missing = analysis.get("missing")
    off_protocol = analysis.get("off_protocol")

    entry_lines = [
        f"- {meal_type.title()}: {items} — **{status} PROTOCOL** ({explanation}). ~{cal} kcal | {protein}g P | {carbs}g C | {fat}g F",
    ]
    if missing:
        entry_lines.append(f"    - Missing: {missing}")
    if off_protocol:
        entry_lines.append(f"    - Off-protocol: {off_protocol}")
    entry_lines.append(f"- ![[{image_name}]]")

    entry = "\n".join(entry_lines)

    content = log_path.read_text()

    # Find today's log entry and append diet info
    day_header = f"### {date.strftime('%B')} {date.day}"
    if day_header in content:
        # Append after the day's section, before the next ### or end
        idx = content.index(day_header)
        next_section = content.find("\n### ", idx + len(day_header))
        if next_section == -1:
            insert_at = len(content)
        else:
            insert_at = next_section
        content = content[:insert_at].rstrip() + "\n" + entry + "\n" + content[insert_at:]
    else:
        # No entry for today yet — append at end
        content = content.rstrip() + f"\n\n{day_header}\n\n{entry}\n"

    log_path.write_text(content)
    return True


def process_image_attachments(
    attachments: list[dict],
    timestamp_ms: int,
    context_message: str,
) -> tuple[list[Path], list[dict | None]]:
    """Process image attachments: classify, save, and analyze meals.

    Returns (saved_paths, analyses) where analyses[i] is the meal analysis
    dict for meal photos, or None for non-meal photos.
    """
    date = datetime.fromtimestamp(timestamp_ms / 1000)
    date_str = date.strftime("%Y-%m-%d")

    saved_paths = []
    analyses = []

    for att in attachments:
        ct = att.get("contentType", "")
        if ct not in IMAGE_CONTENT_TYPES:
            continue

        src = _resolve_attachment(att)
        if not src:
            print(f"Attachment file not found: {att.get('id')}", flush=True)
            continue

        ext = _ext_for_content_type(ct)
        is_meal = classify_image(src)

        if is_meal:
            dest = _save_image(src, MEALS_DIR, date_str, ext)
            print(f"Meal photo saved: {dest.name}", flush=True)

            analysis = analyze_meal(dest, context_message, date)
            if analysis:
                append_meal_to_log(analysis, dest.name, date)

            saved_paths.append(dest)
            analyses.append(analysis)
        else:
            dest = _save_image(src, SIGNAL_IMAGES_DIR, date_str, ext)
            print(f"Non-meal photo saved to Signal/: {dest.name}", flush=True)
            saved_paths.append(dest)
            analyses.append(None)

    return saved_paths, analyses
