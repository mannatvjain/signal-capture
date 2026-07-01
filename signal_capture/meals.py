"""
Calorie estimation for photos with "meal" in the caption.

Reads the signal-cli-cached attachment in place, asks Claude vision for an
itemized calorie estimate, and persists the result to the shared running.db
`meal_log` table (plus a copy of the photo under Running/Meals/). The same
estimate is formatted into a short multi-line text to send back over Signal.

The persisted row stores macros — calories, protein_g, carbs_g, fat_g — plus
item names and image. Protocol-judgment fields (protocol_status, missing,
off_protocol, explanation) are intentionally left NULL. Summertime's 9:45 PM
summary and minutely's Sheet read these rows.
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

RUNNING_DB = Path.home() / "Documents" / "dot" / "CLAUDE" / "Running" / "running.db"
MEALS_DIR = RUNNING_DB.parent / "Meals"
CAPTURE_DB = Path.home() / "Documents" / "dot" / "CLAUDE" / "Artifacts" / "signal-capture" / "capture.db"

MEAL_BATCH_HOUR = 20
MEAL_BATCH_MINUTE = 30
MAX_ESTIMATE_ATTEMPTS = 3

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
        "total_protein_g": {"type": "integer"},
        "total_carbs_g": {"type": "integer"},
        "total_fat_g": {"type": "integer"},
    },
    "required": ["items", "total_calories", "total_protein_g", "total_carbs_g", "total_fat_g"],
})


def resolve_attachment(att: dict) -> Path | None:
    """Find the cached attachment file in signal-cli's store."""
    att_id = att.get("id")
    if not att_id:
        return None
    src = SIGNAL_CLI_ATTACHMENTS / att_id
    return src if src.exists() else None


def estimate_calories(image_path: Path, caption: str = "") -> dict | None:
    """Ask Claude vision for an itemized calorie + macronutrient estimate.

    Returns {"items": [{"name", "calories"}], "total": int, "protein_g": int,
    "carbs_g": int, "fat_g": int}, or None on failure.
    """
    caption_line = f'The user added this caption: "{caption.strip()}"\n\n' if caption.strip() else ""
    prompt = (
        f"{caption_line}"
        f"Read the image at {image_path}. Identify each visible food item and estimate its calories. "
        "Also estimate the whole meal's total macronutrients in grams: protein, carbs, and fat. "
        "The user is strictly gluten-free (celiac): never identify any food as containing wheat, "
        "rye, or barley. Assume gluten-free versions instead (e.g. 'gluten-free toast', not "
        "'whole wheat toast'; 'gluten-free pasta', not 'wheat pasta'). "
        "The user's diet is primarily Indian — when there is any ambiguity about what a food is, "
        "default to the Indian interpretation (e.g. a thin crispy crepe is a dosa, not a French crepe; "
        "a flatbread is a roti or paratha, not a tortilla). "
        "If the caption mentions items not visible in the photo, include them too."
    )

    try:
        result = subprocess.run(
            [
                CLAUDE_CLI, "-p",
                "--model", "haiku",
                "--output-format", "json",
                "--json-schema", _CALORIE_SCHEMA,
                "--system-prompt", "You estimate calories and macronutrients from food images. Return only the requested JSON.",
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

    return {
        "items": items,
        "total": total,
        "protein_g": data.get("total_protein_g", 0),
        "carbs_g": data.get("total_carbs_g", 0),
        "fat_g": data.get("total_fat_g", 0),
    }


def format_breakdown(data: dict) -> str:
    """Render an estimate dict as the text we send back over Signal."""
    lines = [f"~{data.get('total', 0)} kcal"]
    for it in data.get("items", []):
        lines.append(f"  • {it.get('name', '?')}: ~{it.get('calories', 0)} kcal")
    lines.append(
        f"P {data.get('protein_g', 0)}g · C {data.get('carbs_g', 0)}g · F {data.get('fat_g', 0)}g"
    )
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
            n, kcal, protein, carbs, fat = rconn.execute(
                "SELECT COUNT(*), COALESCE(SUM(calories), 0), COALESCE(SUM(protein_g), 0), "
                "COALESCE(SUM(carbs_g), 0), COALESCE(SUM(fat_g), 0) FROM meal_log WHERE date = ?",
                (date,),
            ).fetchone()
        finally:
            rconn.close()

        mconn = sqlite3.connect(MINUTELY_DB, timeout=5.0)
        try:
            mconn.execute("PRAGMA journal_mode=WAL")
            mconn.execute(
                "CREATE TABLE IF NOT EXISTS food_days ("
                "day TEXT PRIMARY KEY, food_kcal INTEGER, food_meals INTEGER, "
                "food_protein_g INTEGER, food_carbs_g INTEGER, food_fat_g INTEGER, updated INTEGER)"
            )
            # add macro columns to a food_days created before they existed
            have = {r[1] for r in mconn.execute("PRAGMA table_info(food_days)")}
            for col in ("food_protein_g", "food_carbs_g", "food_fat_g"):
                if col not in have:
                    mconn.execute(f"ALTER TABLE food_days ADD COLUMN {col} INTEGER")
            mconn.execute(
                "INSERT INTO food_days "
                "(day, food_kcal, food_meals, food_protein_g, food_carbs_g, food_fat_g, updated) "
                "VALUES (?,?,?,?,?,?,strftime('%s','now')) "
                "ON CONFLICT(day) DO UPDATE SET "
                "food_kcal=excluded.food_kcal, food_meals=excluded.food_meals, "
                "food_protein_g=excluded.food_protein_g, food_carbs_g=excluded.food_carbs_g, "
                "food_fat_g=excluded.food_fat_g, updated=excluded.updated",
                (date, kcal, n, protein, carbs, fat),
            )
            mconn.commit()
        finally:
            mconn.close()
    except (sqlite3.Error, OSError) as e:
        print(f"Food mirror to minutely failed: {e}", flush=True)


def before_batch_time() -> bool:
    """True if the current local time is before the 8:30 PM batch window."""
    now = datetime.now()
    return now.hour * 60 + now.minute < MEAL_BATCH_HOUR * 60 + MEAL_BATCH_MINUTE


def pending_meals_due() -> bool:
    """True if queued meals are ready to estimate.

    Due once past the 8:30 PM window, or immediately for rows queued on a
    previous day (the daemon was down or asleep at their batch time).
    """
    conn = sqlite3.connect(str(CAPTURE_DB), timeout=5.0)
    try:
        rows = [r[0] for r in conn.execute("SELECT queued_at FROM meal_pending")]
    finally:
        conn.close()
    if not rows:
        return False
    if not before_batch_time():
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    return any(q[:10] < today for q in rows)


def queue_meal(image_path: Path, caption: str, content_type: str | None = None) -> str | None:
    """Copy image to Meals/ immediately and add to the 8:30 PM estimation queue.

    Returns the stored image_name, or None on failure.
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

        conn = sqlite3.connect(str(CAPTURE_DB), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO meal_pending (image_name, caption, content_type, queued_at) VALUES (?,?,?,?)",
                (image_name, caption, content_type, now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return image_name
    except (sqlite3.Error, OSError) as e:
        print(f"Meal queue failed: {e}", flush=True)
        return None


def _log_meal_entry(image_name: str, caption: str, data: dict, when: datetime, date: str) -> None:
    """Write one row to meal_log without re-copying the image."""
    items_text = ", ".join(it.get("name", "?") for it in data.get("items", [])) or None
    conn = sqlite3.connect(str(RUNNING_DB), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO meal_log "
            "(date, meal_type, items, calories, protein_g, carbs_g, fat_g, image_name, logged_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (date, _infer_meal_type(caption, when), items_text, data.get("total"),
             data.get("protein_g"), data.get("carbs_g"), data.get("fat_g"),
             image_name, when.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def process_pending_meals(send_fn) -> int:
    """Estimate and log all queued meals. Calls send_fn(text) for each result.

    A failed estimate stays queued and is retried on a later pass; only after
    MAX_ESTIMATE_ATTEMPTS is the row dropped, with a "couldn't estimate" alert
    (the photo is already saved in Meals/ either way). Returns the number of
    meals successfully processed.
    """
    conn = sqlite3.connect(str(CAPTURE_DB), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id, image_name, caption, queued_at, attempts FROM meal_pending ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    processed = 0
    for row_id, image_name, caption, queued_at, attempts in rows:
        image_path = MEALS_DIR / image_name
        if not image_path.exists() or image_path.stat().st_size == 0:
            _delete_pending(row_id)
            continue

        queued_dt = datetime.fromisoformat(queued_at)
        date = queued_at[:10]

        data = estimate_calories(image_path, caption)
        if data:
            _log_meal_entry(image_name, caption, data, queued_dt, date)
            mirror_food_to_minutely(date)
            send_fn(f"[meal]\n{format_breakdown(data)}")
            _delete_pending(row_id)
            processed += 1
        elif attempts + 1 >= MAX_ESTIMATE_ATTEMPTS:
            send_fn(f"[meal] couldn't estimate — photo kept as Meals/{image_name}")
            _delete_pending(row_id)
        else:
            _bump_attempts(row_id)

    return processed


def _delete_pending(row_id: int) -> None:
    conn = sqlite3.connect(str(CAPTURE_DB), timeout=5.0)
    try:
        conn.execute("DELETE FROM meal_pending WHERE id = ?", (row_id,))
        conn.commit()
    finally:
        conn.close()


def _bump_attempts(row_id: int) -> None:
    conn = sqlite3.connect(str(CAPTURE_DB), timeout=5.0)
    try:
        conn.execute("UPDATE meal_pending SET attempts = attempts + 1 WHERE id = ?", (row_id,))
        conn.commit()
    finally:
        conn.close()


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
                "INSERT INTO meal_log "
                "(date, meal_type, items, calories, protein_g, carbs_g, fat_g, image_name, logged_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (date, _infer_meal_type(caption, now), items_text, data.get("total"),
                 data.get("protein_g"), data.get("carbs_g"), data.get("fat_g"),
                 image_name, now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        mirror_food_to_minutely(date)
        return image_name
    except (sqlite3.Error, OSError) as e:
        print(f"Meal logging failed: {e}", flush=True)
        return None
