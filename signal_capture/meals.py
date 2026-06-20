"""
Calorie estimation for photos with "meal" in the caption.

Reads the signal-cli-cached attachment in place, asks Claude vision for an
itemized calorie estimate, and persists the result to the shared running.db
`meal_log` table (plus a copy of the photo under Running/Meals/). The same
estimate is formatted into a short multi-line text to send back over Signal.

The persisted row is intentionally a "simple estimate": date, meal_type, items,
calories, image_name. Protocol/macro fields (protocol_status, protein_g, …) are
left NULL — Summertime's 9:45 PM summary and minutely's Sheet read these rows.
"""

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}

SIGNAL_CLI_ATTACHMENTS = Path.home() / ".local" / "share" / "signal-cli" / "attachments"
CLAUDE_CLI = "/Users/mannat/.local/bin/claude"

# Shared running database (owned by Summertime). meal_log is the food source of
# truth; Summertime reads it for the nightly summary, minutely surfaces it.
RUNNING_DB = Path.home() / "Documents" / "dot" / "CLAUDE" / "Running" / "running.db"
MEALS_DIR = RUNNING_DB.parent / "Meals"

# minutely's own DB lives in ~/Library and its launchd server can't read the vault
# (macOS TCC blocks ~/Documents). We mirror per-day food totals there so the Sheet
# can surface them. See new-agent/minutely/db.py food_days.
MINUTELY_DB = Path.home() / "Library" / "Application Support" / "minutely" / "minutely.db"

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
}

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


def estimate_calories(image_path: Path, caption: str = "") -> dict | None:
    """Ask Claude vision for an itemized calorie estimate.

    Returns {"items": [{"name", "calories"}], "total": int}, or None on failure.
    """
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

    return {"items": items, "total": total}


def format_breakdown(data: dict) -> str:
    """Render an estimate dict as the text we send back over Signal."""
    lines = [f"~{data.get('total', 0)} kcal"]
    for it in data.get("items", []):
        lines.append(f"  • {it.get('name', '?')}: ~{it.get('calories', 0)} kcal")
    return "\n".join(lines)


def _infer_meal_type(caption: str, when: datetime) -> str:
    """Best-effort meal_type: explicit word in the caption, else by clock time."""
    c = caption.lower()
    for t in ("breakfast", "lunch", "dinner", "snack"):
        if t in c:
            return t
    h = when.hour
    if h < 11:
        return "breakfast"
    if h < 16:
        return "lunch"
    if h < 21:
        return "dinner"
    return "snack"


def mirror_food_to_minutely(date: str) -> None:
    """Mirror a day's food totals (from running.db) into minutely's own DB.

    minutely's launchd server can't read the vault (TCC), so it reads food_days
    from its own ~/Library DB instead. Recomputes the whole day so the mirror
    reflects every meal logged that date. Best-effort; never raises.
    """
    if not MINUTELY_DB.parent.exists():
        return  # minutely not installed on this machine
    try:
        rconn = sqlite3.connect(RUNNING_DB, timeout=5.0)
        try:
            n, kcal = rconn.execute(
                "SELECT COUNT(*), COALESCE(SUM(calories), 0) FROM meal_log WHERE date = ?",
                (date,),
            ).fetchone()
        finally:
            rconn.close()

        mconn = sqlite3.connect(MINUTELY_DB, timeout=5.0)
        try:
            mconn.execute("PRAGMA journal_mode=WAL")
            mconn.execute(
                "CREATE TABLE IF NOT EXISTS food_days ("
                "day TEXT PRIMARY KEY, food_kcal INTEGER, food_meals INTEGER, updated INTEGER)"
            )
            mconn.execute(
                "INSERT INTO food_days (day, food_kcal, food_meals, updated) "
                "VALUES (?,?,?,strftime('%s','now')) "
                "ON CONFLICT(day) DO UPDATE SET "
                "food_kcal=excluded.food_kcal, food_meals=excluded.food_meals, updated=excluded.updated",
                (date, kcal, n),
            )
            mconn.commit()
        finally:
            mconn.close()
    except (sqlite3.Error, OSError) as e:
        print(f"Food mirror to minutely failed: {e}", flush=True)


def log_meal(image_path: Path, caption: str, data: dict, content_type: str | None = None) -> str | None:
    """Persist one estimated meal to running.db meal_log and copy the photo into Meals/.

    Returns the stored image_name, or None on failure (logging never blocks the
    Signal reply — failures are printed and swallowed).
    """
    try:
        MEALS_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")

        ext = _EXT_BY_TYPE.get(content_type or "") or (image_path.suffix if image_path.suffix else ".jpg")
        n = 1
        while (MEALS_DIR / f"{date}_{n}{ext}").exists():
            n += 1
        image_name = f"{date}_{n}{ext}"
        shutil.copyfile(image_path, MEALS_DIR / image_name)

        items_text = ", ".join(it.get("name", "?") for it in data.get("items", [])) or None

        conn = sqlite3.connect(RUNNING_DB, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO meal_log (date, meal_type, items, calories, image_name, logged_at) "
                "VALUES (?,?,?,?,?,?)",
                (date, _infer_meal_type(caption, now), items_text, data.get("total"), image_name, now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        mirror_food_to_minutely(date)
        return image_name
    except (sqlite3.Error, OSError) as e:
        print(f"Meal logging failed: {e}", flush=True)
        return None
