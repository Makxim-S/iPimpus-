import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import CHILDREN


SCHEDULE_FILE = Path("data/schedules.json")
WEEKDAYS = (
    ("mon", "Понедельник"),
    ("tue", "Вторник"),
    ("wed", "Среда"),
    ("thu", "Четверг"),
    ("fri", "Пятница"),
)
SUBJECTS = {
    "bel_lit": "Бел. Лит",
    "bel_language": "Бел.яз",
    "english": "Англ.яз",
    "labor": "Труд",
    "pe": "Физра",
    "russian_lit": "Русск. Лит",
    "russian_language": "Русск.яз",
    "math": "Матем",
    "person_world": "Человек и мир",
    "safety": "ОБЖ",
    "art": "ИЗО",
    "music": "Музыка",
}


def current_day_key() -> str | None:
    weekday = datetime.now(ZoneInfo("Europe/Minsk")).weekday()
    return WEEKDAYS[weekday][0] if weekday < len(WEEKDAYS) else None


def current_day_name() -> str:
    key = current_day_key()
    return dict(WEEKDAYS).get(key, "Выходной")


def _empty_schedule() -> dict[str, Any]:
    return {
        child_key: {day_key: [] for day_key, _ in WEEKDAYS}
        for child_key in CHILDREN
    }


def _load() -> dict[str, Any]:
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = _empty_schedule()
    changed = False
    for child_key in CHILDREN:
        if child_key not in data or not isinstance(data[child_key], dict):
            data[child_key] = {}
            changed = True
        for day_key, _ in WEEKDAYS:
            if not isinstance(data[child_key].get(day_key), list):
                data[child_key][day_key] = []
                changed = True
    if changed or not SCHEDULE_FILE.exists():
        _save(data)
    return data


def _save(data: dict[str, Any]) -> None:
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_day_schedule(child_key: str, day_key: str | None = None) -> list[str]:
    data = _load()
    day_key = day_key or current_day_key()
    if not day_key:
        return []
    return list(data.get(child_key, {}).get(day_key, []))


def add_subject(child_key: str, day_key: str, subject_key: str) -> None:
    if child_key not in CHILDREN or day_key not in dict(WEEKDAYS):
        raise ValueError("Unknown child or weekday")
    if subject_key not in SUBJECTS:
        raise ValueError("Unknown subject")
    data = _load()
    subjects = data[child_key][day_key]
    if subject_key not in subjects:
        subjects.append(subject_key)
        _save(data)


def remove_subject(child_key: str, day_key: str, subject_key: str) -> None:
    data = _load()
    subjects = data.get(child_key, {}).get(day_key, [])
    if subject_key in subjects:
        subjects.remove(subject_key)
        _save(data)


def mark_checked(child_key: str, subject_key: str, when: datetime | None = None) -> None:
    if child_key not in CHILDREN or subject_key not in SUBJECTS:
        return
    when = when or datetime.now(ZoneInfo("Europe/Minsk"))
    data = _load()
    statuses = data.setdefault("_checked", {})
    statuses.setdefault(child_key, {}).setdefault(when.strftime("%Y-%m-%d"), {})[subject_key] = True
    _save(data)


def is_checked(child_key: str, subject_key: str, when: datetime | None = None) -> bool:
    when = when or datetime.now(ZoneInfo("Europe/Minsk"))
    data = _load()
    return bool(
        data.get("_checked", {})
        .get(child_key, {})
        .get(when.strftime("%Y-%m-%d"), {})
        .get(subject_key, False)
    )
