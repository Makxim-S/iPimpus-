import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


STATE_FILE = Path("data/daily_features.json")


def today_key() -> str:
    return datetime.now(ZoneInfo("Europe/Minsk")).strftime("%Y-%m-%d")


def _load() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_state(child_key: str, date_key: str | None = None) -> dict:
    data = _load()
    date_key = date_key or today_key()
    return dict(data.get(child_key, {}).get(date_key, {}))


def update_state(child_key: str, values: dict, date_key: str | None = None) -> dict:
    data = _load()
    date_key = date_key or today_key()
    child = data.setdefault(child_key, {})
    state = child.setdefault(date_key, {})
    state.update(values)
    _save(data)
    return dict(state)


def emergency_used(child_key: str, date_key: str | None = None) -> bool:
    return bool(get_state(child_key, date_key).get("emergency_used"))


def mark_emergency_used(child_key: str, date_key: str | None = None) -> None:
    update_state(child_key, {"emergency_used": True}, date_key)


def save_diary_items(child_key: str, items: list[dict], date_key: str | None = None) -> None:
    update_state(child_key, {"diary_checked": True, "diary_items": items}, date_key)


def record_homework_result(child_key: str, subject_key: str, percent: int, date_key: str | None = None) -> dict:
    date_key = date_key or today_key()
    state = get_state(child_key, date_key)
    results = state.get("homework_results", {})
    results[subject_key] = {
        "percent": int(percent),
        "success": int(percent) > 75,
        "excellent": int(percent) > 90,
    }
    return update_state(child_key, {"homework_results": results}, date_key)


def mark_subject_completed(child_key: str, subject_key: str, percent: int = 100, date_key: str | None = None) -> dict:
    return record_homework_result(child_key, subject_key, percent, date_key)


def _day_is_complete(child_key: str, date_key: str) -> bool:
    state = get_state(child_key, date_key)
    items = state.get("diary_items", [])
    results = state.get("homework_results", {})
    subjects = [item.get("subject_key") for item in items if item.get("subject_key")]
    return bool(subjects) and all(results.get(subject, {}).get("success", False) for subject in subjects)


def day_is_excellent(child_key: str, date_key: str | None = None) -> bool:
    date_key = date_key or today_key()
    if not _day_is_complete(child_key, date_key):
        return False
    state = get_state(child_key, date_key)
    return all(
        state.get("homework_results", {}).get(item.get("subject_key"), {}).get("excellent", False)
        for item in state.get("diary_items", [])
        if item.get("subject_key")
    )


def qualifies_three_day_bonus(child_key: str, date_key: str | None = None) -> bool:
    date_key = date_key or today_key()
    current = datetime.strptime(date_key, "%Y-%m-%d").date()
    return all(day_is_excellent(child_key, (current - timedelta(days=offset)).isoformat()) for offset in range(3))


def hour_bonus_awarded(child_key: str, date_key: str | None = None) -> bool:
    return bool(get_state(child_key, date_key).get("hour_bonus_awarded"))


def mark_hour_bonus_awarded(child_key: str, date_key: str | None = None) -> None:
    update_state(child_key, {"hour_bonus_awarded": True}, date_key)


def reset_diary_today(child_key: str, date_key: str | None = None) -> None:
    date_key = date_key or today_key()
    state = get_state(child_key, date_key)
    state.update({
        "diary_checked": False,
        "diary_items": [],
        "diary_summary": "",
        "diary_empty_pending": None,
        "diary_parent_review": None,
        "homework_results": {},
        "diary_photo_paths": [],
    })
    update_state(child_key, state, date_key)
