import os
import os
import uuid
import shutil
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from homework_ai import (
    check_homework,
    chat_with_ai,
    check_diary,
    assess_text_submission,
    assess_reading_submission,
    transcribe_voice,
)
from config import CHILDREN
from familylink_control import add_time_bonus, get_child_location
from schedule_store import (
    SUBJECTS as SCHEDULE_SUBJECTS,
    WEEKDAYS,
    add_subject,
    current_day_key,
    current_day_name,
    get_day_schedule,
    is_checked,
    mark_checked,
    remove_subject,
)
from daily_store import (
    day_is_excellent,
    emergency_used,
    get_state as get_daily_state,
    hour_bonus_awarded,
    mark_emergency_used,
    mark_hour_bonus_awarded,
    qualifies_three_day_bonus,
    record_homework_result,
    mark_subject_completed,
    reset_diary_today,
    save_diary_items,
    today_key,
    update_state as update_daily_state,
)


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

PARENT_ID = int(
    os.environ.get("PARENT_ID", "0")
)

ADMIN_ID = int(
    os.environ.get("ADMIN_ID", "0")
)

CHILD_1_ID = int(
    os.environ.get("CHILD_1_ID", "0")
)

CHILD_2_ID = int(
    os.environ.get("CHILD_2_ID", "0")
)

HOMEWORK_DIR = Path("data/homework")
DIARY_DIR = Path("data/diary")
SETTINGS_FILE = Path("data/admin_settings.json")

USER_CHILDREN = {}

CHILD_TELEGRAM_IDS = {
    CHILD_1_ID: "CHILD_1",
    CHILD_2_ID: "CHILD_2",
}

DEFAULT_ADMIN_SETTINGS = {
    "auto_rewards": True,
    "default_reward_minutes": 20,
    "maintenance_mode": False,
    "request_recipient": "parent",
    "test_day": None,
}


def load_admin_settings() -> dict:
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        return {**DEFAULT_ADMIN_SETTINGS, **loaded}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_ADMIN_SETTINGS)


def save_admin_settings() -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS_FILE.open("w", encoding="utf-8") as file:
        json.dump(ADMIN_SETTINGS, file, ensure_ascii=False, indent=2)


ADMIN_SETTINGS = load_admin_settings()


def effective_day_key() -> str | None:
    """Return the real weekday unless the administrator selected a test day."""
    selected = ADMIN_SETTINGS.get("test_day")
    valid_days = {key for key, _ in WEEKDAYS}
    return selected if selected in valid_days else current_day_key()


def effective_day_name() -> str:
    key = effective_day_key()
    return dict(WEEKDAYS).get(key, "Выходной")


def test_day_label() -> str:
    key = ADMIN_SETTINGS.get("test_day")
    return dict(WEEKDAYS).get(key, "автодень")


def prepare_test_day_state(child_key: str) -> None:
    """Start a fresh daily workflow when the administrator changes the test day."""
    selected = ADMIN_SETTINGS.get("test_day")
    if selected not in {key for key, _ in WEEKDAYS}:
        selected = None
    state = get_daily_state(child_key)
    previous = state.get("_test_day")
    if previous == selected:
        return
    if selected is not None or previous is not None:
        reset_diary_today(child_key)
        update_daily_state(child_key, {"_test_day": selected})


async def award_weekend_bonus(child_key: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Give each child a two-hour weekend bonus once per calendar weekend day."""
    now = datetime.now(ZoneInfo("Europe/Minsk"))
    if now.weekday() < 5:
        return
    date_key = today_key()
    state = get_daily_state(child_key)
    if state.get("_weekend_bonus_date") == date_key:
        return
    try:
        reward = await add_time_bonus(child_key, 120)
    except Exception as error:
        print(f"[WEEKEND BONUS ERROR] {child_key}: {error}")
        return
    update_daily_state(child_key, {
        "_weekend_bonus_date": date_key,
        "weekend_bonus_minutes": int(reward.get("minutes", 120)),
    })
    await context.bot.send_message(
        chat_id=next((telegram_id for telegram_id, key in CHILD_TELEGRAM_IDS.items() if key == child_key), 0),
        text=(
            "🎉 Поздравляю! Учебная неделя закончена.\n\n"
            f"Тебе добавлено +{int(reward.get('minutes', 120))} минут — целых 2 часа на выходные!"
        ),
    )


def request_recipient_ids() -> list[int]:
    recipients = []
    choice = ADMIN_SETTINGS.get("request_recipient", "parent")
    if choice in {"parent", "both"} and PARENT_ID:
        recipients.append(PARENT_ID)
    if choice in {"admin", "both"} and ADMIN_ID:
        recipients.append(ADMIN_ID)
    return list(dict.fromkeys(recipients))


def request_recipient_label() -> str:
    return {"parent": "родитель", "admin": "администратор", "both": "родитель и администратор"}.get(
        ADMIN_SETTINGS.get("request_recipient", "parent"), "родитель"
    )


def user_role(telegram_id: int) -> str:
    if ADMIN_ID and telegram_id == ADMIN_ID:
        return "admin"
    if PARENT_ID and telegram_id == PARENT_ID:
        return "parent"
    if telegram_id in CHILD_TELEGRAM_IDS and CHILD_TELEGRAM_IDS[telegram_id]:
        return "child"
    return "unknown"


def child_key_for_user(telegram_id: int) -> str | None:
    return CHILD_TELEGRAM_IDS.get(telegram_id)


def child_display_name(child_key: str) -> str:
    return CHILDREN.get(child_key, {}).get("name", child_key)


# ============================================================
# SUBJECTS
# ============================================================

SUBJECTS = {
    "russian": "Русский язык",
    "literature": "Литература",
    "english": "Английский язык",
    "math": "Математика",
    "physics": "Физика",
    "chemistry": "Химия",
    "geography": "География",
    "history": "История",
    "biology": "Биология",
    "informatics": "Информатика",
}


# ============================================================
# MAIN MENU
# ============================================================

def main_menu_keyboard(role: str = "child"):
    buttons = [
        [InlineKeyboardButton("📚 Расписание", callback_data="schedule_menu")],
        [InlineKeyboardButton("💬 Общение", callback_data="menu_chat")],
        [InlineKeyboardButton("📚 Проверка домашки", callback_data="menu_homework")],
    ]
    if role == "admin":
        buttons.insert(0, [InlineKeyboardButton("🛠 Панель администратора", callback_data="admin_dashboard")])
        buttons.insert(1, [InlineKeyboardButton("📍 Где дети?", callback_data="location_menu")])
    elif role == "parent":
        buttons.insert(0, [InlineKeyboardButton("👨‍👩‍👧 Дети и награды", callback_data="parent_dashboard")])
        buttons.insert(1, [InlineKeyboardButton("📍 Где дети?", callback_data="location_menu")])
    else:
        buttons.insert(0, [InlineKeyboardButton("🆘 +5 мин", callback_data="emergency_bonus")])
    return InlineKeyboardMarkup(buttons)


def admin_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Геопозиция детей", callback_data="location_menu")],
        [InlineKeyboardButton("🧪 Тест дневника AI", callback_data="admin_diary_test")],
        [InlineKeyboardButton("🔄 Сбросить дневник за сегодня", callback_data="admin_diary_reset")],
        [InlineKeyboardButton("⏱ Добавить время", callback_data="admin_bonus_menu")],
        [InlineKeyboardButton("📱 Дети и устройства", callback_data="admin_devices")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])


def admin_bonus_keyboard():
    rows = []
    for key, child in CHILDREN.items():
        rows.append([InlineKeyboardButton(
            f"{child['name']}: выбрать", callback_data=f"admin_bonus_child:{key}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dashboard")])
    return InlineKeyboardMarkup(rows)


def admin_child_bonus_keyboard(child_key: str):
    child = CHILDREN[child_key]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("+15 минут", callback_data=f"admin_bonus:{child_key}:15"),
         InlineKeyboardButton("+20 минут", callback_data=f"admin_bonus:{child_key}:20")],
        [InlineKeyboardButton("+30 минут", callback_data=f"admin_bonus:{child_key}:30"),
         InlineKeyboardButton("+60 минут", callback_data=f"admin_bonus:{child_key}:60")],
        [InlineKeyboardButton("✍️ Свой объём", callback_data=f"admin_bonus_custom:{child_key}")],
        [InlineKeyboardButton("⬅️ К детям", callback_data="admin_bonus_menu")],
    ])


def settings_keyboard():
    state = "вкл" if ADMIN_SETTINGS["auto_rewards"] else "выкл"
    maintenance = "вкл" if ADMIN_SETTINGS["maintenance_mode"] else "выкл"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📨 Запросы: {request_recipient_label()}", callback_data="admin_toggle_request_recipient")],
        [InlineKeyboardButton(f"🎁 Автонаграды: {state}", callback_data="admin_toggle_auto_rewards")],
        [InlineKeyboardButton(f"🔧 Техрежим: {maintenance}", callback_data="admin_toggle_maintenance")],
        [InlineKeyboardButton(f"🧪 День тестирования: {test_day_label()}", callback_data="admin_toggle_test_day")],
        [InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dashboard")],
    ])


# ============================================================
# HOMEWORK SUBJECT MENU
# ============================================================

def subject_keyboard():

    buttons = [
        [
            InlineKeyboardButton(
                "🤖 Авто (beta)",
                callback_data="subject_AUTO",
            )
        ],
        [
            InlineKeyboardButton(
                "🇷🇺 Русский язык",
                callback_data="subject_russian",
            ),
            InlineKeyboardButton(
                "📖 Литература",
                callback_data="subject_literature",
            ),
        ],
        [
            InlineKeyboardButton(
                "🇬🇧 Английский язык",
                callback_data="subject_english",
            ),
            InlineKeyboardButton(
                "➗ Математика",
                callback_data="subject_math",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚛️ Физика",
                callback_data="subject_physics",
            ),
            InlineKeyboardButton(
                "🧪 Химия",
                callback_data="subject_chemistry",
            ),
        ],
        [
            InlineKeyboardButton(
                "🌍 География",
                callback_data="subject_geography",
            ),
            InlineKeyboardButton(
                "📜 История",
                callback_data="subject_history",
            ),
        ],
        [
            InlineKeyboardButton(
                "🧬 Биология",
                callback_data="subject_biology",
            ),
            InlineKeyboardButton(
                "💻 Информатика",
                callback_data="subject_informatics",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="main_menu",
            )
        ],
    ]

    return InlineKeyboardMarkup(buttons)


# ============================================================
# UPLOAD KEYBOARD
# ============================================================

def upload_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data="cancel_homework",
                )
            ]
        ]
    )


def reading_submission_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 Проверить пересказ", callback_data="reading_evaluate")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_homework")],
        ]
    )


def check_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Проверить",
                    callback_data="check_homework",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отменить",
                    callback_data="cancel_homework",
                )
            ],
        ]
    )


# ============================================================
# PARENT APPROVAL KEYBOARD
# ============================================================

def parent_approval_keyboard(request_id: str):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да, AI прочитал правильно",
                    callback_data=f"parent_yes:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Нет, AI прочитал неправильно",
                    callback_data=f"parent_no:{request_id}",
                )
            ],
        ]
    )


# ============================================================
# CLEANUP
# ============================================================

def cleanup_submission(
    context: ContextTypes.DEFAULT_TYPE,
):

    submission_id = context.user_data.get(
        "submission_id"
    )

    if submission_id:

        submission_dir = (
            HOMEWORK_DIR / submission_id
        )

        try:

            if submission_dir.exists():

                shutil.rmtree(
                    submission_dir
                )

                print(
                    f"[CLEANUP] Deleted "
                    f"{submission_dir}"
                )

        except Exception as e:

            print(
                f"[CLEANUP ERROR] "
                f"{submission_dir}: {e}"
            )

    context.user_data.pop(
        "submission_id",
        None,
    )

    reading_submission = context.user_data.pop("reading_submission", None)
    if reading_submission:
        reading_dir = Path(reading_submission.get("directory", ""))
        try:
            if reading_dir.exists() and reading_dir.is_dir():
                shutil.rmtree(reading_dir)
                print(f"[CLEANUP] Deleted {reading_dir}")
        except Exception as error:
            print(f"[CLEANUP ERROR] {reading_dir}: {error}")

    context.user_data.pop(
        "status_message_id",
        None,
    )

    context.user_data.pop(
        "selected_subject",
        None,
    )

    context.user_data.pop(
        "chat_mode",
        None,
    )

    chat_photo_path = context.user_data.pop("chat_photo_path", None)
    if chat_photo_path:
        try:
            Path(chat_photo_path).unlink(missing_ok=True)
        except OSError:
            pass
    context.user_data.pop("chat_history", None)

    context.user_data.pop(
        "parent_approval",
        None,
    )


# ============================================================
# /start
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_id = update.effective_user.id
    role = user_role(telegram_id)

    if role == "admin":
        cleanup_submission(context)
        await update.message.reply_text(
            "🛠 Привет, администратор!\n\n"
            "Панель суперпользователя готова. Здесь можно управлять "
            "детьми, наградами и настройками бота.",
            reply_markup=admin_dashboard_keyboard(),
        )
        print(f"[ADMIN START] telegram_id={telegram_id}")
        return

    if role == "parent":
        cleanup_submission(context)
        await update.message.reply_text(
            "👋 Привет!\n\n"
            "Вы — родитель. Я буду присылать сюда спорные места "
            "домашних работ для подтверждения.",
            reply_markup=main_menu_keyboard("parent"),
        )
        print(f"[PARENT START] telegram_id={telegram_id}")
        return

    child_key = child_key_for_user(telegram_id)
    if role == "child" and child_key:
        USER_CHILDREN[telegram_id] = child_key
        cleanup_submission(context)
        await award_weekend_bonus(child_key, context)
        name = child_display_name(child_key)
        await update.message.reply_text(
            f"👋 Привет, {name}!\n\nВыбери, что хочешь сделать:",
            reply_markup=main_menu_keyboard("child"),
        )
        return

    await update.message.reply_text(
        "⛔ Твой Telegram ID пока не добавлен в семейную систему."
    )


# ============================================================
# /help
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    role = user_role(update.effective_user.id)
    if role == "admin":
        await update.message.reply_text(
            "🛠 Администраторский режим.\n\n"
            "Открой /start, чтобы перейти в dashboard."
        )
        return

    if role == "parent":
        await update.message.reply_text(
            "👩 Родительский режим.\n\n"
            "Бот будет присылать сюда запросы, "
            "если AI не сможет уверенно разобрать "
            "почерк в домашней работе."
        )

        return

    if role == "child":
        name = child_display_name(child_key_for_user(update.effective_user.id) or "")
    else:
        name = "пользователь"

    await update.message.reply_text(
        "📚 Что умеет бот:\n\n"
        "💬 Общение — задать вопрос AI.\n"
        "📚 Проверка домашки — отправить фотографии "
        "школьной работы на проверку.\n\n"
        "Выбери действие в меню:",
        reply_markup=main_menu_keyboard("child" if role == "child" else "parent"),
    )


# ============================================================
# MAIN MENU CALLBACK
# ============================================================

async def main_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    cleanup_submission(context)

    context.user_data.pop("diary_mode", None)
    context.user_data.pop("diary_id", None)
    context.user_data.pop("diary_photo_paths", None)

    if context.user_data.get("location_messages"):
        await clear_location_messages(context, query.message.chat_id, query.message.message_id)
        role = user_role(query.from_user.id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🏠 Главное меню\n\nВыбери действие:",
            reply_markup=main_menu_keyboard(role if role != "unknown" else "child"),
        )
        return

    role = user_role(query.from_user.id)
    child_key = child_key_for_user(query.from_user.id)
    if role == "child" and child_key:
        await award_weekend_bonus(child_key, context)
    await query.edit_message_text(
        "🏠 Главное меню\n\nВыбери действие:",
        reply_markup=main_menu_keyboard(role if role != "unknown" else "child"),
    )


async def admin_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    await query.edit_message_text(
        "🛠 Панель администратора\n\n"
        "Выбери раздел управления:",
        reply_markup=admin_dashboard_keyboard(),
    )


async def admin_bonus_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    await query.edit_message_text(
        "⏱ Добавление времени\n\nВыбери ребёнка:",
        reply_markup=admin_bonus_keyboard(),
    )


async def admin_bonus_child_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    child_key = query.data.split(":", 1)[1]
    if child_key not in CHILDREN:
        await query.edit_message_text("Неизвестный ребёнок.", reply_markup=admin_dashboard_keyboard())
        return
    child = CHILDREN[child_key]
    await query.edit_message_text(
        f"⏱ {child['name']}\n\nВыбери количество минут:",
        reply_markup=admin_child_bonus_keyboard(child_key),
    )


async def admin_bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    _, child_key, minutes_text = query.data.split(":")
    await query.edit_message_text("⏳ Отправляю запрос в Family Link...")
    try:
        result = await add_time_bonus(child_key, int(minutes_text))
        await query.edit_message_text(
            f"✅ Google принял запрос.\n\n"
            f"Ребёнок: {child_display_name(child_key)}\n"
            f"Добавлено: {result['minutes']} минут\n"
            f"HTTP: {result['status']}",
            reply_markup=admin_dashboard_keyboard(),
        )
    except Exception as error:
        print(f"[ADMIN BONUS ERROR] {error}")
        await query.edit_message_text(
            f"❌ Не удалось добавить время:\n{error}",
            reply_markup=admin_dashboard_keyboard(),
        )


async def admin_bonus_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    child_key = query.data.split(":", 1)[1]
    context.user_data["admin_custom_bonus_child"] = child_key
    await query.edit_message_text(
        f"✍️ Введи количество минут для {child_display_name(child_key)}.\n"
        "Допустимый диапазон: от 1 до 1440.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Отмена", callback_data="admin_bonus_menu")]
        ]),
    )


async def admin_devices_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    lines = ["📱 Дети и устройства:\n"]
    for key, child in CHILDREN.items():
        lines.append(
            f"{child['name']} ({key})\n"
            f"Child ID: {child['family_link_child_id']}\n"
            f"Device ID: {child['family_link_device_id']}\n"
            f"Награда: {child['reward_minutes']} минут\n"
        )
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=admin_dashboard_keyboard(),
    )


async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    await query.edit_message_text(
        "⚙️ Настройки бота\n\n"
        f"Автоматическая награда за правильную домашку: {'включена' if ADMIN_SETTINGS['auto_rewards'] else 'выключена'}\n"
        f"Технический режим: {'включён' if ADMIN_SETTINGS['maintenance_mode'] else 'выключен'}\n"
        f"День для проверки функций: {test_day_label()}",
        reply_markup=settings_keyboard(),
    )


async def admin_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    if query.data == "admin_toggle_request_recipient":
        current = ADMIN_SETTINGS.get("request_recipient", "parent")
        ADMIN_SETTINGS["request_recipient"] = {"parent": "admin", "admin": "both", "both": "parent"}.get(current, "parent")
    elif query.data == "admin_toggle_auto_rewards":
        ADMIN_SETTINGS["auto_rewards"] = not ADMIN_SETTINGS["auto_rewards"]
    elif query.data == "admin_toggle_maintenance":
        ADMIN_SETTINGS["maintenance_mode"] = not ADMIN_SETTINGS["maintenance_mode"]
    elif query.data == "admin_toggle_test_day":
        day_keys = [key for key, _ in WEEKDAYS]
        current = ADMIN_SETTINGS.get("test_day")
        if current in day_keys:
            index = day_keys.index(current)
            ADMIN_SETTINGS["test_day"] = day_keys[index + 1] if index + 1 < len(day_keys) else None
        else:
            ADMIN_SETTINGS["test_day"] = day_keys[0]
    save_admin_settings()
    await admin_settings_callback(update, context)


async def parent_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "parent":
        await query.answer("⛔ Только для родителя.", show_alert=True)
        return
    lines = ["👨‍👩‍👧 Дети и награды:\n"]
    for child in CHILDREN.values():
        lines.append(f"{child['name']}: награда {child['reward_minutes']} минут")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard("parent"),
    )


def location_children_keyboard():
    rows = [
        [InlineKeyboardButton(
            f"📍 {child['name']}", callback_data=f"location_show:{key}:0"
        )]
        for key, child in CHILDREN.items()
    ]
    rows.append([InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def location_refresh_keyboard(child_key: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🔄 Обновить немедленно",
            callback_data=f"location_show:{child_key}:1",
        )],
        [InlineKeyboardButton("⬅️ Выбрать ребёнка", callback_data="location_menu")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])


async def clear_location_messages(context, chat_id: int, include_message_id: int | None = None):
    stored = context.user_data.pop("location_messages", {})
    message_ids = {stored.get("location_id"), stored.get("details_id"), include_message_id}
    for message_id in message_ids:
        if message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass


async def location_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if context.user_data.get("location_messages"):
        await clear_location_messages(context, query.message.chat_id, query.message.message_id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📍 Геопозиция детей\n\nВыбери ребёнка:",
            reply_markup=location_children_keyboard(),
        )
        return
    if user_role(query.from_user.id) not in {"admin", "parent"}:
        await query.answer("⛔ Геопозиция доступна только родителям и администратору.", show_alert=True)
        return
    await query.edit_message_text(
        "📍 Геопозиция детей\n\nВыбери ребёнка:",
        reply_markup=location_children_keyboard(),
    )


async def location_show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) not in {"admin", "parent"}:
        await query.answer("⛔ Геопозиция доступна только родителям и администратору.", show_alert=True)
        return

    _, child_key, refresh_flag = query.data.split(":")
    refresh = refresh_flag == "1"
    if child_key not in CHILDREN:
        await query.edit_message_text("Неизвестный ребёнок.", reply_markup=location_children_keyboard())
        return

    try:
        location = await get_child_location(child_key, refresh=refresh)
    except Exception as error:
        print(f"[LOCATION ERROR] {error}")
        await query.edit_message_text(
            f"❌ Не удалось получить геопозицию:\n{error}",
            reply_markup=location_refresh_keyboard(child_key),
        )
        return

    if not location:
        await query.edit_message_text(
            f"📍 Для {child_display_name(child_key)} геопозиция недоступна.\n\n"
            "Проверь, включён ли Family Link Location Sharing и есть ли у телефона интернет.",
            reply_markup=location_refresh_keyboard(child_key),
        )
        return

    previous = context.user_data.get("location_messages", {}).get(child_key, {})
    chat_id = query.message.chat_id
    for message_id in (previous.get("location_id"), previous.get("details_id"), query.message.message_id):
        if message_id:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

    location_message = await context.bot.send_location(
        chat_id=chat_id,
        latitude=location["latitude"],
        longitude=location["longitude"],
    )
    if location.get("timestamp"):
        updated_at = datetime.fromtimestamp(
            location["timestamp"] / 1000,
            tz=ZoneInfo("Europe/Minsk"),
        ).strftime("%d.%m.%Y %H:%M")
    else:
        updated_at = "неизвестно"
    details = [
        f"📍 {location['child_name']}",
        f"Последнее обновление: {updated_at}",
        f"Точность: {location.get('accuracy') or 'неизвестна'} м",
    ]
    if location.get("battery_level") is not None:
        details.append(f"Заряд: {location['battery_level']}%")
    details_message = await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(details),
        reply_markup=location_refresh_keyboard(child_key),
    )
    messages = context.user_data.setdefault("location_messages", {})
    messages[child_key] = {
        "location_id": location_message.message_id,
        "details_id": details_message.message_id,
    }


# ============================================================
# SCHEDULE
# ============================================================

def emergency_bonus_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, использовать", callback_data="emergency_bonus_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="emergency_bonus_no")],
    ])


async def emergency_bonus_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = child_key_for_user(query.from_user.id)
    if user_role(query.from_user.id) != "child" or not child_key:
        await query.answer("⛔ Эта кнопка доступна только детям.", show_alert=True)
        return
    if emergency_used(child_key):
        await query.edit_message_text(
            "🆘 Ты уже использовал срочные 5 минут сегодня.\n\nПопробуй снова завтра.",
            reply_markup=main_menu_keyboard("child"),
        )
        return
    await query.edit_message_text(
        "Эта кнопка даёт 5 минут для срочных нужд 1 раз в день.\n\n"
        "Ты уверен, что хочешь использовать её прямо сейчас?",
        reply_markup=emergency_bonus_keyboard(),
    )


async def emergency_bonus_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = child_key_for_user(query.from_user.id)
    if user_role(query.from_user.id) != "child" or not child_key:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    if query.data == "emergency_bonus_no":
        await query.edit_message_text("Хорошо, 5 минут пока сохранены.", reply_markup=main_menu_keyboard("child"))
        return
    if emergency_used(child_key):
        await query.edit_message_text("🆘 5 минут уже были использованы сегодня.", reply_markup=main_menu_keyboard("child"))
        return
    try:
        result = await add_time_bonus(child_key, 5)
        mark_emergency_used(child_key)
        await query.edit_message_text(
            f"✅ Добавлено {result['minutes']} минут для срочных нужд.\n\nДо завтра кнопка недоступна.",
            reply_markup=main_menu_keyboard("child"),
        )
    except Exception as error:
        print(f"[EMERGENCY BONUS ERROR] {error}")
        await query.edit_message_text("⚠️ Не удалось добавить 5 минут. Попробуй ещё раз позже.", reply_markup=main_menu_keyboard("child"))


def diary_check_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Проверить дневник AI", callback_data="diary_check")],
        [InlineKeyboardButton("❌ Отмена", callback_data="diary_cancel")],
    ])


def admin_diary_test_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Получить полный summary AI", callback_data="admin_diary_test_check")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_diary_test_cancel")],
    ])


def admin_diary_test_child_keyboard():
    rows = [
        [InlineKeyboardButton(child["name"], callback_data=f"admin_diary_test_child:{key}")]
        for key, child in CHILDREN.items()
    ]
    rows.append([InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dashboard")])
    return InlineKeyboardMarkup(rows)


async def admin_diary_reset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    await query.edit_message_text(
        "🔄 Сбросить дневниковую проверку за сегодня для всех детей?\n\n"
        "Будут очищены найденные задания и результаты сдачи за сегодня.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, сбросить", callback_data="admin_diary_reset_yes"),
             InlineKeyboardButton("❌ Отмена", callback_data="admin_diary_reset_no")],
        ]),
    )


async def admin_diary_reset_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    if query.data == "admin_diary_reset_yes":
        for child_key in CHILDREN:
            reset_diary_today(child_key)
        await query.edit_message_text("✅ Проверка дневника и результаты сдачи за сегодня сброшены.", reply_markup=admin_dashboard_keyboard())
    else:
        await query.edit_message_text("Сброс отменён.", reply_markup=admin_dashboard_keyboard())


async def admin_diary_test_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    context.user_data["admin_diary_test_mode"] = True
    context.user_data["admin_diary_test_paths"] = []
    context.user_data.pop("admin_diary_test_id", None)
    context.user_data.pop("admin_diary_test_child", None)
    await query.edit_message_text(
        "🧪 Тест проверки дневника AI\n\n"
        "Сначала выбери ребёнка, для которого нужно взять расписание и проверить дневник.",
        reply_markup=admin_diary_test_child_keyboard(),
    )


async def admin_diary_test_child_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    child_key = query.data.split(":", 1)[1]
    if child_key not in CHILDREN:
        await query.answer("Неизвестный ребёнок.", show_alert=True)
        return
    context.user_data["admin_diary_test_child"] = child_key
    await query.edit_message_text(
        f"🧪 Тест дневника для {child_display_name(child_key)}.\n\n"
        f"День для проверки: {effective_day_name()}\n"
        "Пришли фото дневника с домашним заданием. Можно отправить несколько фото, "
        "затем нажми кнопку получения полного summary.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="admin_dashboard")]]),
    )


async def admin_diary_test_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    for key in ("admin_diary_test_mode", "admin_diary_test_paths", "admin_diary_test_id", "admin_diary_test_child"):
        context.user_data.pop(key, None)
    await query.edit_message_text("🧪 Тест дневника отменён.", reply_markup=admin_dashboard_keyboard())


async def admin_diary_test_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) != "admin":
        await query.answer("⛔ Только для администратора.", show_alert=True)
        return
    paths = context.user_data.get("admin_diary_test_paths", [])
    child_key = context.user_data.get("admin_diary_test_child")
    if not context.user_data.get("admin_diary_test_mode") or not child_key:
        await query.edit_message_text("Сначала выбери ребёнка для теста дневника.", reply_markup=admin_diary_test_child_keyboard())
        return
    if not paths:
        await query.edit_message_text("Сначала пришли фото дневника.", reply_markup=admin_dashboard_keyboard())
        return
    await query.edit_message_text("🤖 AI читает дневник и формирует полный summary...")
    try:
        day_key = effective_day_key()
        expected_subjects = [SCHEDULE_SUBJECTS[key] for key in get_day_schedule(child_key, day_key)] if day_key else []
        result = await asyncio.to_thread(check_diary, paths, effective_day_name(), expected_subjects, datetime.now(ZoneInfo("Europe/Minsk")).strftime("%d.%m.%Y"))
    except Exception as error:
        print(f"[ADMIN DIARY TEST ERROR] {error}")
        await query.edit_message_text(f"⚠️ Ошибка AI при проверке дневника:\n{error}", reply_markup=admin_dashboard_keyboard())
        return
    lines = [
        "🧪 Полный summary AI по тесту дневника",
        "",
        f"День для проверки: {effective_day_name()}",
        f"Домашнее задание найдено: {'да' if result.has_homework else 'нет'}",
        f"Требуется подтверждение родителя: {'да' if result.needs_parent_approval else 'нет'}",
        f"Summary: {result.summary or 'без дополнительной сводки'}",
        "",
        "Задания по предметам:",
    ]
    if result.items:
        lines.extend(f"• {item.subject}: {item.task}" for item in result.items)
    else:
        lines.append("• Не найдено")
    if result.issues:
        lines.extend(["", "Замечания строгого аудита:"])
        lines.extend(f"• {issue}" for issue in result.issues)
    for key in ("admin_diary_test_mode", "admin_diary_test_paths", "admin_diary_test_id", "admin_diary_test_child"):
        context.user_data.pop(key, None)
    await query.edit_message_text("\n".join(lines), reply_markup=admin_dashboard_keyboard())


async def diary_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = child_key_for_user(query.from_user.id)
    if user_role(query.from_user.id) != "child" or not child_key:
        await query.answer("⛔ Эта функция доступна только детям.", show_alert=True)
        return
    prepare_test_day_state(child_key)
    if get_daily_state(child_key).get("diary_checked"):
        await query.edit_message_text("📔 Дневник уже проверен AI сегодня. Повторная проверка будет доступна завтра.", reply_markup=main_menu_keyboard("child"))
        return
    cleanup_submission(context)
    context.user_data["diary_mode"] = True
    context.user_data.pop("diary_id", None)
    context.user_data["diary_photo_paths"] = []
    await query.edit_message_text(
        "📔 Пришли фото дневника со строкой сегодняшнего дня.\n\n"
        "Можно отправить несколько страниц, затем нажми кнопку проверки.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
    )


async def diary_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("diary_mode", None)
    context.user_data.pop("diary_id", None)
    context.user_data.pop("diary_photo_paths", None)
    await query.edit_message_text("Проверка дневника отменена.", reply_markup=main_menu_keyboard("child"))


async def diary_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = child_key_for_user(query.from_user.id)
    paths = context.user_data.get("diary_photo_paths", [])
    if not child_key or not context.user_data.get("diary_mode") or not paths:
        await query.edit_message_text("Сначала отправь фото дневника.", reply_markup=main_menu_keyboard("child"))
        return
    if get_daily_state(child_key).get("diary_checked"):
        await query.edit_message_text("📔 Дневник уже проверен AI сегодня.", reply_markup=main_menu_keyboard("child"))
        return
    await query.edit_message_text("🤖 AI проверяет домашнее задание в дневнике за сегодня...")
    try:
        day_key = effective_day_key()
        expected_subjects = [SCHEDULE_SUBJECTS[key] for key in get_day_schedule(child_key, day_key)] if day_key else []
        result = await asyncio.to_thread(check_diary, paths, effective_day_name(), expected_subjects, datetime.now(ZoneInfo("Europe/Minsk")).strftime("%d.%m.%Y"))
    except Exception as error:
        print(f"[DIARY AI ERROR] {error}")
        await query.edit_message_text("⚠️ Не удалось прочитать дневник. Пришли фото ещё раз.", reply_markup=main_menu_keyboard("child"))
        return
    schedule_subjects = set(get_day_schedule(child_key, effective_day_key())) if effective_day_key() else set()
    normalized_items = []
    recognized_subjects = set()
    for item in result.items:
        subject_key = normalize_diary_subject(item.subject)
        if subject_key:
            recognized_subjects.add(subject_key)
        if subject_key and subject_key in schedule_subjects and item.homework_present and item.task.strip():
            normalized_items.append({
                "subject_key": subject_key,
                "subject": item.subject,
                "task": item.task,
            })
    issues = list(result.issues)
    missing_subjects = schedule_subjects - recognized_subjects
    if missing_subjects:
        issues.append("Не удалось уверенно проверить предметы: " + ", ".join(SCHEDULE_SUBJECTS[key] for key in sorted(missing_subjects)))
    matched_homework = bool(normalized_items)
    save_diary_items(child_key, normalized_items)
    update_daily_state(child_key, {"diary_summary": result.summary, "diary_photo_paths": list(paths)})
    context.user_data.pop("diary_mode", None)
    context.user_data.pop("diary_id", None)
    context.user_data.pop("diary_photo_paths", None)
    if issues:
        request_id = uuid.uuid4().hex[:12]
        update_daily_state(child_key, {
            "diary_checked": False,
            "diary_empty_pending": None,
            "diary_parent_review": request_id,
            "diary_parent_issues": issues,
        })
        issue_text = "\n".join(
            f"• {' '.join(str(issue).split())[:180]}"
            for issue in list(dict.fromkeys(issues))[:3]
        )
        notes_for_parent = "\n".join(f"• {item['subject']}: {item['task']}" for item in normalized_items) or "• Не найдено"
        await query.edit_message_text("⚠️ В дневнике обнаружены неуверенные или спорные места. Жду подтверждение родителя.")
        for recipient_id in request_recipient_ids():
            await context.bot.send_message(
                chat_id=recipient_id,
                text=(f"⚠️ Строгая проверка дневника {child_display_name(child_key)} требует подтверждения.\n\n"
                       f"Предварительно найденное ДЗ:\n{notes_for_parent}\n\n"
                       f"Замечания AI:\n{issue_text}\n\nПодтвердить данные дневника?"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📷 Смотреть фото", callback_data=f"diary_view:{request_id}")],
                    [InlineKeyboardButton("✅ Подтвердить", callback_data=f"diary_review_yes:{request_id}"),
                     InlineKeyboardButton("❌ Запросить новое фото", callback_data=f"diary_review_no:{request_id}")],
                ]),
            )
        return
    if matched_homework:
        notes = "\n".join(f"• {item['subject']}: {item['task']}" for item in normalized_items)
        text = f"📔 Домашнее задание на сегодня:\n\n{notes}"
        context.user_data["selected_subject"] = "AUTO"
        await query.edit_message_text(
            f"{text}\n\n✅ Дневник проверен. Теперь отправь фото выполненной домашней работы.",
            reply_markup=upload_keyboard(),
        )
        for recipient_id in request_recipient_ids():
            await context.bot.send_message(chat_id=recipient_id, text=f"📔 Дневник {child_display_name(child_key)} проверен AI.\n\n{text}")
        return
    request_id = uuid.uuid4().hex[:12]
    update_daily_state(child_key, {"diary_checked": False, "diary_empty_pending": request_id})
    await query.edit_message_text("📔 AI не нашёл домашнее задание. Жду подтверждение родителя.")
    for recipient_id in request_recipient_ids():
        await context.bot.send_message(
            chat_id=recipient_id,
            text=f"📔 В дневнике {child_display_name(child_key)} не заполнены домашние задания за сегодня.\n\nПодтвердить, что домашку действительно не задали?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📷 Смотреть фото", callback_data=f"diary_view:{request_id}")],
                [InlineKeyboardButton("✅ Да, домашки нет", callback_data=f"diary_parent_yes:{request_id}"),
                 InlineKeyboardButton("❌ Нет, запросить заново", callback_data=f"diary_parent_no:{request_id}")],
            ]),
        )


async def diary_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Отправляю фото дневника…")
    if user_role(query.from_user.id) not in {"parent", "admin"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    request_id = query.data.split(":", 1)[1]
    pending_child = None
    for child_key in CHILDREN:
        state = get_daily_state(child_key)
        if request_id in {state.get("diary_parent_review"), state.get("diary_empty_pending")}:
            pending_child = child_key
            break
    if not pending_child:
        await query.answer("Запрос уже обработан или устарел.", show_alert=True)
        return
    paths = get_daily_state(pending_child).get("diary_photo_paths", [])
    sent = 0
    for path in paths:
        try:
            with Path(path).open("rb") as photo_file:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=photo_file,
                    caption=f"📷 Дневник {child_display_name(pending_child)}: фото {sent + 1}",
                )
            sent += 1
        except (FileNotFoundError, OSError) as error:
            print(f"[DIARY PHOTO ERROR] {error}")
    if not sent:
        await query.answer("Фото дневника больше недоступны.", show_alert=True)


async def diary_parent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) not in {"parent", "admin"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    action, request_id = query.data.split(":", 1)
    pending_child = None
    pending_type = None
    for child_key in CHILDREN:
        state = get_daily_state(child_key)
        if state.get("diary_empty_pending") == request_id:
            pending_child = child_key
            pending_type = "empty"
            break
        if state.get("diary_parent_review") == request_id:
            pending_child = child_key
            pending_type = "review"
            break
    if not pending_child:
        await query.edit_message_text("⚠️ Этот запрос уже обработан или устарел.")
        return
    child_id = next((telegram_id for telegram_id, key in CHILD_TELEGRAM_IDS.items() if key == pending_child), 0)
    if pending_type == "review":
        if action == "diary_review_yes":
            update_daily_state(pending_child, {"diary_checked": True, "diary_parent_review": None, "diary_parent_issues": []})
            await query.edit_message_text("✅ Родитель подтвердил спорные места дневника.")
            if child_id:
                await context.bot.send_message(chat_id=child_id, text="✅ Дневник подтверждён родителем. Теперь можно сдавать домашнее задание.", reply_markup=main_menu_keyboard("child"))
        else:
            reset_diary_today(pending_child)
            await query.edit_message_text("❌ Проверка отклонена, ребёнку нужно прислать новое фото дневника.")
            if child_id:
                await context.bot.send_message(chat_id=child_id, text="📔 Родитель просит прислать более чёткое фото дневника. Нажми «📚 Проверка домашки» и отправь его заново.", reply_markup=main_menu_keyboard("child"))
        return
    if action == "diary_parent_yes":
        update_daily_state(pending_child, {"diary_checked": True, "diary_empty_pending": None, "diary_parent_confirmed": True})
        await query.edit_message_text("✅ Подтверждено: домашнее задание не задано.")
        if child_id:
            await context.bot.send_message(chat_id=child_id, text="🎉 Поздравляю, сегодня тебе не задали домашку и ты можешь поиграть!", reply_markup=main_menu_keyboard("child"))
    else:
        update_daily_state(pending_child, {"diary_empty_pending": None})
        await query.edit_message_text("❌ Запрос отправлен ребёнку повторно.")
        if child_id:
            await context.bot.send_message(chat_id=child_id, text="📔 Родитель просит проверить дневник ещё раз. Нажми «📚 Проверка домашки» и пришли новое фото дневника за сегодня.", reply_markup=main_menu_keyboard("child"))


def schedule_children_keyboard():
    rows = [[InlineKeyboardButton(child["name"], callback_data=f"schedule_child:{key}")]
            for key, child in CHILDREN.items()]
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def normalize_diary_subject(value: str) -> str | None:
    normalized = "".join(ch for ch in (value or "").lower() if ch.isalnum())
    aliases = {
        "беллит": "bel_lit", "белорусскаялитература": "bel_lit",
        "беляз": "bel_language", "бел язык": "bel_language", "белязык": "bel_language", "белорусскийязык": "bel_language",
        "англяз": "english", "английскийязык": "english",
        "труд": "labor", "технология": "labor",
        "физра": "pe", "физкультура": "pe",
        "руссклит": "russian_lit", "русслит": "russian_lit", "литература": "russian_lit",
        "русскяз": "russian_language", "руссязык": "russian_language", "русскийязык": "russian_language",
        "матем": "math", "математика": "math",
        "человекимир": "person_world",
        "обж": "safety", "изо": "art", "музыка": "music",
    }
    if normalized in aliases:
        return aliases[normalized]
    for key, label in SCHEDULE_SUBJECTS.items():
        if normalized == "".join(ch for ch in label.lower() if ch.isalnum()):
            return key
    return None


def assigned_homework_items(child_key: str) -> list[dict]:
    state = get_daily_state(child_key)
    results = state.get("homework_results", {})
    return [
        item for item in state.get("diary_items", [])
        if item.get("subject_key") and not results.get(item.get("subject_key"), {}).get("success", False)
    ]


def homework_submission_keyboard(child_key: str):
    rows = [[InlineKeyboardButton(
        f"📝 {SCHEDULE_SUBJECTS.get(item['subject_key'], item.get('subject', ''))}",
        callback_data=f"homework_submit:{item['subject_key']}",
    )] for item in assigned_homework_items(child_key)]
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def schedule_text(child_key: str) -> str:
    day_key = effective_day_key()
    if not day_key:
        return f"📚 Расписание {child_display_name(child_key)}\n\nСегодня выходной."
    subjects = get_day_schedule(child_key, day_key)
    daily_state = get_daily_state(child_key)
    assigned = {item.get("subject_key") for item in daily_state.get("diary_items", [])}
    results = daily_state.get("homework_results", {})
    lines = [f"📚 Расписание: {child_display_name(child_key)}", f"День — {effective_day_name()}", ""]
    if not subjects:
        lines.append("На сегодня предметы ещё не добавлены.")
    else:
        for subject in subjects:
            checked = (
                (daily_state.get("diary_checked") and subject not in assigned)
                or results.get(subject, {}).get("success", False)
                or is_checked(child_key, subject)
            )
            lines.append(f"{'✅' if checked else '❌'} {SCHEDULE_SUBJECTS.get(subject, subject)}")
    return "\n".join(lines)


def schedule_child_keyboard(child_key: str, can_edit: bool, can_submit: bool = False):
    rows = []
    if can_edit:
        rows.append([InlineKeyboardButton("✏️ Изменить расписание", callback_data=f"schedule_edit:{child_key}")])
    if can_submit:
        rows.extend([[InlineKeyboardButton(
            f"📝 Сдать {SCHEDULE_SUBJECTS.get(item['subject_key'], item.get('subject', ''))}",
            callback_data=f"homework_submit:{item['subject_key']}",
        )] for item in assigned_homework_items(child_key)])
    rows.extend([
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"schedule_child:{child_key}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    return InlineKeyboardMarkup(rows)


def schedule_day_edit_keyboard(child_key: str, day_key: str):
    current = get_day_schedule(child_key, day_key)
    rows = [[InlineKeyboardButton(f"➖ {SCHEDULE_SUBJECTS.get(subject, subject)}", callback_data=f"schedule_remove:{child_key}:{day_key}:{subject}")]
            for subject in current]
    available = [key for key in SCHEDULE_SUBJECTS if key not in current]
    rows.extend([[InlineKeyboardButton(f"➕ {SCHEDULE_SUBJECTS[key]}", callback_data=f"schedule_add:{child_key}:{day_key}:{key}")] for key in available])
    rows.append([InlineKeyboardButton("◀️ К дням", callback_data=f"schedule_edit:{child_key}")])
    return InlineKeyboardMarkup(rows)


def schedule_edit_text(child_key: str, day_key: str) -> str:
    day_name = dict(WEEKDAYS).get(day_key, day_key)
    subjects = get_day_schedule(child_key, day_key)
    shown = ", ".join(SCHEDULE_SUBJECTS.get(subject, subject) for subject in subjects) or "пусто"
    return f"✏️ Расписание {child_display_name(child_key)}\n\n{day_name}: {shown}\n\nДобавь предмет или нажми на предмет, чтобы удалить его."


async def schedule_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role = user_role(query.from_user.id)
    if role == "child":
        child_key = child_key_for_user(query.from_user.id)
        await query.edit_message_text(schedule_text(child_key), reply_markup=schedule_child_keyboard(child_key, False, True))
        return
    if role not in {"admin", "parent"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    await query.edit_message_text("📚 Расписание\n\nВыбери ребёнка:", reply_markup=schedule_children_keyboard())


async def schedule_child_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = query.data.split(":", 1)[1]
    role = user_role(query.from_user.id)
    if child_key not in CHILDREN or role not in {"admin", "parent", "child"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    if role == "child" and child_key != child_key_for_user(query.from_user.id):
        await query.answer("⛔ Можно просматривать только своё расписание.", show_alert=True)
        return
    await query.edit_message_text(schedule_text(child_key), reply_markup=schedule_child_keyboard(child_key, role in {"admin", "parent"}, role == "child"))


async def schedule_edit_child_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) not in {"admin", "parent"}:
        await query.answer("⛔ Редактировать расписание могут только родители и администратор.", show_alert=True)
        return
    child_key = query.data.split(":", 1)[1]
    rows = [[InlineKeyboardButton(day_name, callback_data=f"schedule_edit_day:{child_key}:{day_key}")] for day_key, day_name in WEEKDAYS]
    rows.append([InlineKeyboardButton("◀️ К расписанию", callback_data=f"schedule_child:{child_key}")])
    await query.edit_message_text(f"✏️ Выбери день для {child_display_name(child_key)}:", reply_markup=InlineKeyboardMarkup(rows))


async def schedule_edit_day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) not in {"admin", "parent"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    _, child_key, day_key = query.data.split(":")
    await query.edit_message_text(schedule_edit_text(child_key, day_key), reply_markup=schedule_day_edit_keyboard(child_key, day_key))


async def schedule_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if user_role(query.from_user.id) not in {"admin", "parent"}:
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return
    action, child_key, day_key, subject_key = query.data.split(":")
    try:
        (add_subject if action == "schedule_add" else remove_subject)(child_key, day_key, subject_key)
    except ValueError:
        await query.answer("Некорректный предмет или день.", show_alert=True)
        return
    await query.edit_message_text(schedule_edit_text(child_key, day_key), reply_markup=schedule_day_edit_keyboard(child_key, day_key))


# ============================================================
# HOMEWORK MENU
# ============================================================

async def homework_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    cleanup_submission(context)

    child_key = child_key_for_user(query.from_user.id)
    if child_key:
        prepare_test_day_state(child_key)
    if user_role(query.from_user.id) == "child" and child_key and not get_daily_state(child_key).get("diary_checked"):
        context.user_data["diary_mode"] = True
        context.user_data.pop("diary_id", None)
        context.user_data["diary_photo_paths"] = []
        await query.edit_message_text(
            "📔 Сначала пришли фото дневника со строкой сегодняшнего дня.\n\n"
            "AI проверит его один раз в сутки, после чего откроется проверка домашки.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
        )
        return

    if user_role(query.from_user.id) == "child" and child_key:
        items = assigned_homework_items(child_key)
        if items:
            await query.edit_message_text(
                "📚 Выбери домашнее задание из дневника, которое сдаёшь сейчас:",
                reply_markup=homework_submission_keyboard(child_key),
            )
            return
        await query.edit_message_text(
            "✅ По сегодняшнему дневнику домашнее задание не найдено.",
            reply_markup=main_menu_keyboard("child"),
        )
        return

    context.user_data["selected_subject"] = "AUTO"

    await query.edit_message_text(
        "📚 Проверка домашки\n\n"
        "Режим: 🤖 Авто (beta)\n\n"
        "Отправь фотографии домашней работы.",
        reply_markup=upload_keyboard(),
    )


async def homework_submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    child_key = child_key_for_user(query.from_user.id)
    subject_key = query.data.split(":", 1)[1]
    if not child_key or subject_key not in SCHEDULE_SUBJECTS or subject_key not in {
        item.get("subject_key") for item in assigned_homework_items(child_key)
    }:
        await query.answer("Это задание уже недоступно.", show_alert=True)
        return
    cleanup_submission(context)
    context.user_data["selected_subject"] = subject_key
    item = next(
        (item for item in assigned_homework_items(child_key) if item.get("subject_key") == subject_key),
        None,
    )
    context.user_data["active_homework_item"] = item or {"subject_key": subject_key, "task": ""}
    await query.edit_message_text(
        f"📝 Сдача: {SCHEDULE_SUBJECTS[subject_key]}\n\n"
        "Отправь фотографии выполненной работы. После последней нажми «✅ Проверить».",
        reply_markup=upload_keyboard(),
    )


async def register_homework_result(child_key: str | None, subject_key: str | None, percent: int) -> str:
    if not child_key or subject_key not in SCHEDULE_SUBJECTS:
        return ""
    record_homework_result(child_key, subject_key, percent)
    if percent <= 75:
        return ""
    if qualifies_three_day_bonus(child_key) and not hour_bonus_awarded(child_key):
        try:
            reward = await add_time_bonus(child_key, 60)
            mark_hour_bonus_awarded(child_key)
            return f"\n\n🎉 Три дня подряд все работы сданы отлично. В Family Link добавлен бонус +{reward['minutes']} минут (1 час)!"
        except Exception as error:
            print(f"[THREE DAY BONUS ERROR] {error}")
            return "\n\n⚠️ Три дня подряд выполнены отлично, но бонусный час не удалось начислить."
    return ""


# ============================================================
# SUBJECT SELECT
# ============================================================

async def subject_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    selected_subject = "AUTO"
    subject_name = "Авто (beta)"

    cleanup_submission(context)

    context.user_data[
        "selected_subject"
    ] = selected_subject

    await query.edit_message_text(
        f"📚 Проверка: {subject_name}\n\n"
        "Отправь фотографии домашней работы.\n\n"
        "Можно отправить несколько страниц.\n"
        "После последней фотографии нажми "
        "«✅ Проверить».\n\n"
        "Если выбран «Авто (beta)», AI "
        "самостоятельно определит предмет.",
        reply_markup=upload_keyboard(),
    )


# ============================================================
# PHOTO
# ============================================================

async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    telegram_id = update.effective_user.id

    if context.user_data.get("chat_mode"):
        caption = (update.message.caption or "").strip()
        if not caption:
            await update.message.reply_text(
                "📷 Для работы с фото добавь к нему текст: что именно нужно разобрать или объяснить."
            )
            return
        chat_dir = HOMEWORK_DIR / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        photo_path = chat_dir / f"{telegram_id}.jpg"
        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        await telegram_file.download_to_drive(str(photo_path))
        history = context.user_data.setdefault("chat_history", [])
        await update.message.reply_text("🧠 Анализирую фото и твой вопрос...")
        try:
            answer = await asyncio.to_thread(
                chat_with_ai,
                caption,
                [str(photo_path)],
                history,
            )
        except Exception as error:
            print(f"[CHAT PHOTO AI ERROR] {error}")
            await update.message.reply_text("⚠️ Не удалось обработать фото. Попробуй ещё раз с более точным текстом.")
            return
        context.user_data["chat_photo_path"] = str(photo_path)
        history.extend([
            {"role": "user", "content": caption},
            {"role": "assistant", "content": answer},
        ])
        await update.message.reply_text(
            answer,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
        )
        return

    if user_role(telegram_id) == "admin" and context.user_data.get("admin_diary_test_mode"):
        test_id = context.user_data.setdefault("admin_diary_test_id", f"admin_{uuid.uuid4().hex[:12]}")
        test_dir = DIARY_DIR / "admin_test" / test_id
        test_dir.mkdir(parents=True, exist_ok=True)
        paths = context.user_data.setdefault("admin_diary_test_paths", [])
        file_path = test_dir / f"{len(paths) + 1:03d}.jpg"
        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        await telegram_file.download_to_drive(str(file_path))
        paths.append(str(file_path))
        await update.message.reply_text(
            f"🧪 Фото дневника получено: {len(paths)}.\n\n"
            "Можешь прислать ещё фото или нажать получение полного summary AI.",
            reply_markup=admin_diary_test_keyboard(),
        )
        return

    if telegram_id not in USER_CHILDREN:

        await update.message.reply_text(
            "Сначала нажми /start."
        )

        return

    if context.user_data.get(
        "parent_approval"
    ):

        await update.message.reply_text(
            "⏳ Сейчас ожидаю подтверждение "
            "родителя по спорному месту."
        )

        return

    if context.user_data.get("diary_mode"):
        child_key = child_key_for_user(telegram_id)
        if not child_key:
            await update.message.reply_text("Сначала нажми /start.")
            return
        diary_id = context.user_data.setdefault("diary_id", f"{child_key}_{today_key()}_{uuid.uuid4().hex[:8]}")
        diary_dir = DIARY_DIR / diary_id
        diary_dir.mkdir(parents=True, exist_ok=True)
        paths = context.user_data.setdefault("diary_photo_paths", [])
        file_path = diary_dir / f"{len(paths) + 1:03d}.jpg"
        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        await telegram_file.download_to_drive(str(file_path))
        paths.append(str(file_path))
        await update.message.reply_text(
            f"📔 Фото дневника получено: {len(paths)}.\n\n"
            "Можешь прислать ещё фото или запустить проверку AI.",
            reply_markup=diary_check_keyboard(),
        )
        return

    reading_submission = context.user_data.get("reading_submission")
    if reading_submission:
        reading_dir = Path(reading_submission["directory"])
        reading_dir.mkdir(parents=True, exist_ok=True)
        photo = update.message.photo[-1]
        telegram_file = await context.bot.get_file(photo.file_id)
        photo_path = reading_dir / f"text_{len(reading_submission['photo_paths']) + 1:03d}.jpg"
        await telegram_file.download_to_drive(str(photo_path))
        reading_submission["photo_paths"].append(str(photo_path))
        voice_ready = bool(reading_submission.get("voice_path"))
        await update.message.reply_text(
            "📖 Фото текста получено. "
            + ("Теперь нажми «🤖 Проверить пересказ»." if voice_ready else "Теперь отправь голосовой краткий пересказ текста."),
            reply_markup=reading_submission_keyboard() if voice_ready else InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отменить", callback_data="cancel_homework")]
            ]),
        )
        return

    if "selected_subject" not in context.user_data:

        await update.message.reply_text(
            "Сначала выбери:\n\n"
            "📚 Проверка домашки",
            reply_markup=main_menu_keyboard(),
        )

        return

    if "submission_id" not in context.user_data:

        context.user_data[
            "submission_id"
        ] = uuid.uuid4().hex[:12]

    submission_id = context.user_data[
        "submission_id"
    ]

    submission_dir = (
        HOMEWORK_DIR / submission_id
    )

    submission_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    existing_photos = sorted(
        submission_dir.glob("*.jpg")
    )

    photos_count = len(existing_photos) + 1

    photo = update.message.photo[-1]

    telegram_file = await context.bot.get_file(
        photo.file_id
    )

    file_path = (
        submission_dir
        / f"{photos_count:03d}.jpg"
    )

    await telegram_file.download_to_drive(
        str(file_path)
    )

    print(
        f"[PHOTO] {file_path}"
    )

    selected_subject = context.user_data.get(
        "selected_subject",
        "AUTO",
    )

    if selected_subject == "AUTO":

        subject_name = "🤖 Авто (beta)"

    else:

        subject_name = SUBJECTS.get(
            selected_subject,
            selected_subject,
        )

    status_message_id = context.user_data.get(
        "status_message_id"
    )

    status_text = (
        f"📚 Проверка: {subject_name}\n\n"
        f"📸 Получено: {photos_count} "
        f"{'страница' if photos_count == 1 else 'страниц'}\n\n"
        "Можешь отправить следующую страницу "
        "или нажать «✅ Проверить»."
    )

    if not status_message_id:

        message = await update.message.reply_text(
            status_text,
            reply_markup=check_keyboard(),
        )

        context.user_data[
            "status_message_id"
        ] = message.message_id

        return

    try:

        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_message_id,
            text=status_text,
            reply_markup=check_keyboard(),
        )

    except Exception as e:

        print(
            f"[STATUS MESSAGE ERROR] {e}"
        )

        message = await update.message.reply_text(
            status_text,
            reply_markup=check_keyboard(),
        )

        context.user_data[
            "status_message_id"
        ] = message.message_id


# ============================================================
# CROP IMAGE
# ============================================================

def crop_parent_fragment(
    photo_path: Path,
    request,
    output_path: Path,
):

    from PIL import Image

    with Image.open(photo_path) as image:

        image_width, image_height = image.size

        x = float(request.x)
        y = float(request.y)
        width = float(request.width)
        height = float(request.height)

        left = int(
            image_width * x
        )

        top = int(
            image_height * y
        )

        right = int(
            image_width * (x + width)
        )

        bottom = int(
            image_height * (y + height)
        )

        padding_x = int(
            image_width * 0.03
        )

        padding_y = int(
            image_height * 0.03
        )

        left = max(
            0,
            left - padding_x,
        )

        top = max(
            0,
            top - padding_y,
        )

        right = min(
            image_width,
            right + padding_x,
        )

        bottom = min(
            image_height,
            bottom + padding_y,
        )

        cropped = image.crop(
            (
                left,
                top,
                right,
                bottom,
            )
        )

        cropped.save(
            output_path,
            format="JPEG",
            quality=95,
        )


# ============================================================
# SEND PARENT APPROVAL
# ============================================================

async def send_parent_approval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result,
    photos,
):

    if not request_recipient_ids():

        raise RuntimeError("Не настроен получатель запросов")

    submission_id = context.user_data[
        "submission_id"
    ]

    requests = result.parent_approval_requests

    context.user_data[
        "parent_approval"
    ] = {
        "requests": requests,
        "answers": {},
        "result": result,
        "photos": [
            str(photo)
            for photo in photos
        ],
        "current_index": 0,
    }

    await update.callback_query.edit_message_text(
        "⚠️ Нашёл спорное место.\n\n"
        "Я отправлю его родителю для подтверждения.\n"
        "Проверка пока приостановлена."
    )

    first_request = requests[0]

    await send_next_parent_request(
        context,
        first_request,
        0,
    )


# ============================================================
# SEND NEXT PARENT REQUEST
# ============================================================

async def send_next_parent_request(
    context: ContextTypes.DEFAULT_TYPE,
    request,
    index: int,
):

    approval = context.user_data.get(
        "parent_approval"
    )

    if not approval:
        return

    photos = approval["photos"]

    photo_index = int(
        request.photo_index
    )

    if photo_index < 0 or photo_index >= len(photos):

        print(
            "[PARENT ERROR] "
            f"Invalid photo index: {photo_index}"
        )

        return

    photo_path = Path(
        photos[photo_index]
    )

    submission_id = context.user_data[
        "submission_id"
    ]

    fragment_path = (
        HOMEWORK_DIR
        / submission_id
        / f"parent_fragment_{index}.jpg"
    )

    try:

        crop_parent_fragment(
            photo_path,
            request,
            fragment_path,
        )

    except Exception as e:

        print(
            f"[PARENT CROP ERROR] {e}"
        )

        for recipient_id in request_recipient_ids():
            await context.bot.send_message(
                chat_id=recipient_id,
                text=("⚠️ Не удалось автоматически обрезать спорный фрагмент.\n\n"
                       f"{request.question}"),
            )

        return

    alternatives_text = ""

    if request.alternatives:

        alternatives_text = (
            "\n\nВозможные варианты AI:\n"
            + "\n".join(
                f"• {value}"
                for value in request.alternatives
            )
        )

    caption = (
        "👨‍👩‍👧 Нужна помощь родителя\n\n"
        f"{request.question}\n\n"
        f"🤖 AI предполагает: "
        f"«{request.ai_interpretation}»"
        f"{alternatives_text}\n\n"
        "Посмотри на фрагмент фотографии "
        "и подтверди, правильно ли AI "
        "прочитал написанное."
    )

    request_id = f"{index}_{uuid.uuid4().hex[:8]}"

    approval[
        "current_request_id"
    ] = request_id

    approval[
        "request_map"
    ] = approval.get(
        "request_map",
        {},
    )

    approval[
        "request_map"
    ][request_id] = {
        "index": index,
        "request": request,
    }

    with fragment_path.open("rb") as photo_file:

        for recipient_id in request_recipient_ids():
            photo_file.seek(0)
            await context.bot.send_photo(
                chat_id=recipient_id,
                photo=photo_file,
                caption=caption,
                reply_markup=parent_approval_keyboard(request_id),
            )

    print(
        f"[PARENT REQUEST] "
        f"index={index}, "
        f"request_id={request_id}"
    )


# ============================================================
# PARENT ANSWER
# ============================================================

async def parent_approval_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    if query.from_user.id not in request_recipient_ids():

        await query.answer(
            "⛔ У вас нет доступа.",
            show_alert=True,
        )

        return

    approval = context.user_data.get(
        "parent_approval"
    )

    if not approval:

        await query.edit_message_caption(
            caption=(
                "⚠️ Эта проверка уже завершена "
                "или больше не активна."
            )
        )

        return

    callback = query.data

    if ":" not in callback:
        return

    action, request_id = callback.split(
        ":",
        1,
    )

    request_map = approval.get(
        "request_map",
        {},
    )

    request_info = request_map.get(
        request_id
    )

    if not request_info:

        await query.answer(
            "Запрос уже не активен.",
            show_alert=True,
        )

        return

    index = request_info["index"]

    request = request_info["request"]

    if action == "parent_yes":

        answer = {
            "approved": True,
            "text": request.ai_interpretation,
        }

        await query.edit_message_caption(
            caption=(
                "✅ Родитель подтвердил.\n\n"
                f"AI прочитал: "
                f"«{request.ai_interpretation}»"
            )
        )

    elif action == "parent_no":

        answer = {
            "approved": False,
            "text": "",
        }

        await query.edit_message_caption(
            caption=(
                "❌ Родитель отклонил "
                "распознавание AI."
            )
        )

    else:
        return

    approval[
        "answers"
    ][index] = answer

    approval[
        "current_index"
    ] = index + 1

    requests = approval["requests"]

    if index + 1 < len(requests):

        next_request = requests[
            index + 1
        ]

        await send_next_parent_request(
            context,
            next_request,
            index + 1,
        )

        return

    await finalize_after_parent(
        context
    )


# ============================================================
# FINALIZE AFTER PARENT
# ============================================================

async def finalize_after_parent(
    context: ContextTypes.DEFAULT_TYPE,
):

    approval = context.user_data.get(
        "parent_approval"
    )

    if not approval:
        return

    result = approval["result"]

    answers = approval["answers"]

    rejected = [
        index
        for index, answer in answers.items()
        if not answer.get("approved", False)
    ]

    if rejected:

        result.correct = False

        result.correctness_percent = min(
            result.correctness_percent,
            99,
        )

        result.recommendation = (
            "Родитель не подтвердил "
            "распознавание спорного места. "
            "Нужна дополнительная проверка."
        )

    else:

        result.correct = False

        result.correctness_percent = min(
            result.correctness_percent,
            99,
        )

        result.recommendation = (
            "Родитель подтвердил спорные места. "
            "Требуется финальная проверка AI."
        )

    print(
        "[PARENT APPROVAL] "
        "Все ответы родителя получены."
    )


# ============================================================
# CHECK HOMEWORK
# ============================================================

async def check_homework_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    submission_id = context.user_data.get(
        "submission_id"
    )

    if not submission_id:

        await query.edit_message_text(
            "❌ Нет загруженной домашней работы.",
            reply_markup=main_menu_keyboard(),
        )

        return

    submission_dir = (
        HOMEWORK_DIR / submission_id
    )

    photos = sorted(
        submission_dir.glob("*.jpg")
    )

    if not photos:

        cleanup_submission(context)

        await query.edit_message_text(
            "❌ Фотографии не найдены.",
            reply_markup=main_menu_keyboard(),
        )

        return

    selected_subject = context.user_data.get(
        "selected_subject",
        "AUTO",
    )

    if selected_subject == "AUTO":

        subject_for_ai = "AUTO"

    else:

        subject_for_ai = SUBJECTS.get(
            selected_subject,
            selected_subject,
        )
        active_item = context.user_data.get("active_homework_item", {})
        task_text = active_item.get("task", "").strip()
        if task_text:
            subject_for_ai = f"{subject_for_ai}. Задание из дневника: {task_text}"

    await query.edit_message_text(
        "🧠 Проверяю домашнюю работу...\n\n"
        f"📄 Страниц: {len(photos)}\n"
        "🔎 Выполняю 3 независимых прохода AI.\n\n"
        "⏳ Пожалуйста, подожди."
    )

    print(
        f"[AI CHECK START] "
        f"{submission_id}: "
        f"{len(photos)} photos, "
        f"subject={subject_for_ai}"
    )

    try:

        result = await asyncio.to_thread(
            check_homework,
            [str(photo) for photo in photos],
            subject_for_ai,
        )

    except Exception as e:

        print(
            f"[AI ERROR] "
            f"{submission_id}: {e}"
        )

        await query.edit_message_text(
            "⚠️ Не удалось проверить "
            "домашнюю работу.\n\n"
            "Фотографии сохранены.\n"
            "Попробуй нажать проверку ещё раз "
            "немного позже.",
            reply_markup=check_keyboard(),
        )

        return

    print(
        f"[AI RESULT] "
        f"{submission_id}: "
        f"is_homework={result.is_homework}, "
        f"correct={result.correct}, "
        f"percent={result.correctness_percent}, "
        f"confidence={result.confidence:.2f}, "
        f"subject={result.subject}"
    )

    if not result.is_homework:

        await query.edit_message_text(
            "❌ Это не похоже на школьную "
            "домашнюю работу.\n\n"
            f"{result.summary}\n\n"
            f"🤖 Уверенность проверки: "
            f"{result.confidence:.0%}",
            reply_markup=main_menu_keyboard(),
        )

        cleanup_submission(context)

        return

    checked_subject = selected_subject
    if checked_subject == "AUTO":
        detected_subject = (result.subject or "").strip().lower()
        checked_subject = next(
            (key for key, label in SCHEDULE_SUBJECTS.items() if label.lower() == detected_subject),
            None,
        )
    child_key = USER_CHILDREN.get(query.from_user.id)

    if (
        result.needs_parent_approval
        and result.parent_approval_requests
    ):

        if not request_recipient_ids():

            await query.edit_message_text(
                "⚠️ AI обнаружил спорное место, "
                "но Telegram ID родителя "
                "не настроен.\n\n"
                "Фотографии сохранены.",
                reply_markup=check_keyboard(),
            )

            return

        try:

            await send_parent_approval(
                update,
                context,
                result,
                photos,
            )

        except Exception as e:

            print(
                f"[PARENT ERROR] {e}"
            )

            await query.edit_message_text(
                "⚠️ Не удалось отправить "
                "запрос родителю.\n\n"
                "Фотографии сохранены.",
                reply_markup=check_keyboard(),
            )

        return

    bonus_text = await register_homework_result(child_key, checked_subject, result.correctness_percent)
    context.user_data.pop("active_homework_item", None)

    if not result.correct:

        mistakes_text = ""

        if result.mistakes:

            mistakes_text = (
                "\n\n❌ Найденные ошибки:\n"
            )

            for index, mistake in enumerate(
                result.mistakes,
                start=1,
            ):

                mistakes_text += (
                    f"{index}. {mistake}\n"
                )

        await query.edit_message_text(
            "📚 Домашняя работа проверена.\n\n"
            f"{'✅' if result.correctness_percent > 75 else '❌'} Результат: "
            f"{'успешная сдача.' if result.correctness_percent > 75 else 'нужно исправить.'}\n\n"
            f"📊 Выполнено правильно: "
            f"{result.correctness_percent}%\n\n"
            f"{result.summary}"
            f"{mistakes_text}\n"
            f"💡 {result.recommendation}\n\n"
            f"🤖 Уверенность AI: "
            f"{result.confidence:.0%}"
            f"{bonus_text}",
            reply_markup=main_menu_keyboard(),
        )

        cleanup_submission(context)

        return

    reward_text = "⏳ Награда за правильную работу отключена."
    if ADMIN_SETTINGS["auto_rewards"] and not ADMIN_SETTINGS["maintenance_mode"] and child_key:
        reward_minutes = CHILDREN.get(child_key, {}).get("reward_minutes", ADMIN_SETTINGS["default_reward_minutes"])
        try:
            reward = await add_time_bonus(child_key, int(reward_minutes))
            reward_text = f"🎁 В Family Link добавлено {reward['minutes']} минут."
        except Exception as error:
            print(f"[AUTO REWARD ERROR] {error}")
            reward_text = "⚠️ Домашка правильная, но награду начислить не удалось."

    await query.edit_message_text(
        "🎉 Домашняя работа выполнена правильно!\n\n"
        f"📚 Предмет: {result.subject}\n\n"
        f"📊 Выполнено правильно: "
        f"{result.correctness_percent}%\n\n"
        f"{result.summary}\n\n"
        f"🤖 Уверенность AI: "
        f"{result.confidence:.0%}\n\n"
        f"💡 {result.recommendation}\n\n"
        f"{reward_text}"
        f"{bonus_text}",
        reply_markup=main_menu_keyboard("child"),
    )

    print(
        f"[AI APPROVED] "
        f"{submission_id}: "
        f"{result.correctness_percent}%"
    )

    cleanup_submission(context)


# ============================================================
# CANCEL HOMEWORK
# ============================================================

async def cancel_homework(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    cleanup_submission(context)

    await query.edit_message_text(
        "❌ Проверка домашки отменена.\n\n"
        "Все загруженные фотографии удалены.\n\n"
        "Можешь начать новую проверку.",
        reply_markup=main_menu_keyboard(),
    )


# ============================================================
# CHAT MENU
# ============================================================

async def chat_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    cleanup_submission(context)

    context.user_data[
        "chat_mode"
    ] = True
    context.user_data["chat_history"] = []

    await query.edit_message_text(
        "💬 Общение с AI\n\n"
        "Напиши свой вопрос обычным сообщением.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🏠 Главное меню",
                        callback_data="main_menu",
                    )
                ]
            ]
        ),
    )


# ============================================================
# READING / VOICE SUBMISSION
# ============================================================

async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    child_key = child_key_for_user(telegram_id)
    reading_submission = context.user_data.get("reading_submission")
    if not child_key or not reading_submission:
        if child_key:
            await update.message.reply_text("🎙️ Голосовой ответ принимается для задания «прочитать» после его выбора.")
        return

    reading_dir = Path(reading_submission["directory"])
    reading_dir.mkdir(parents=True, exist_ok=True)
    voice_path = reading_dir / "retelling.ogg"
    telegram_file = await context.bot.get_file(update.message.voice.file_id)
    await telegram_file.download_to_drive(str(voice_path))
    reading_submission["voice_path"] = str(voice_path)
    if reading_submission.get("photo_paths"):
        text = "🎙️ Пересказ получен. Фото текста тоже есть — нажми «🤖 Проверить пересказ»."
        markup = reading_submission_keyboard()
    else:
        text = "🎙️ Пересказ получен. Теперь пришли фото текста, который нужно было прочитать."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_homework")]])
    await update.message.reply_text(text, reply_markup=markup)


async def reading_evaluate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    reading_submission = context.user_data.get("reading_submission")
    child_key = child_key_for_user(query.from_user.id)
    if not child_key or not reading_submission:
        await query.edit_message_text("⚠️ Сценарий пересказа уже завершён или устарел.", reply_markup=main_menu_keyboard("child"))
        return
    photos = reading_submission.get("photo_paths", [])
    voice_path = reading_submission.get("voice_path")
    if not photos or not voice_path:
        missing = "фото текста" if not photos else "голосовой пересказ"
        await query.answer(f"Сначала отправь {missing}.", show_alert=True)
        return

    await query.edit_message_text("🧠 Расшифровываю голосовой ответ и передаю пересказ AI на оценку…")
    try:
        transcript = await asyncio.to_thread(transcribe_voice, voice_path)
        assessment = await asyncio.to_thread(
            assess_reading_submission,
            reading_submission.get("task", ""),
            transcript,
            photos,
        )
    except Exception as error:
        print(f"[READING AI ERROR] {error}")
        await query.edit_message_text("⚠️ Не удалось расшифровать или оценить пересказ. Фото и голос сохранены, попробуй ещё раз.", reply_markup=reading_submission_keyboard())
        return

    percent = int(assessment.percent)
    successful = bool(assessment.accepted and percent > 75)
    if successful:
        subject_key = reading_submission.get("subject_key")
        bonus_text = await register_homework_result(child_key, subject_key, percent)
        task_name = SCHEDULE_SUBJECTS.get(subject_key, reading_submission.get("subject", "задание"))
        context.user_data.pop("active_homework_item", None)
        cleanup_submission(context)
        await query.edit_message_text(
            f"✅ Пересказ принят.\n\n📚 Предмет: {task_name}\n📊 Оценка пересказа: {percent}%\n\n{assessment.summary}{bonus_text}",
            reply_markup=main_menu_keyboard("child"),
        )
        return

    await query.edit_message_text(
        f"❌ Пересказ пока не принят.\n\n📊 Оценка: {percent}%\n{assessment.summary}\n\nПришли более содержательный краткий пересказ и снова нажми проверку.",
        reply_markup=reading_submission_keyboard(),
    )


# ============================================================
# CHAT MESSAGE
# ============================================================

async def handle_chat_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if user_role(update.effective_user.id) == "admin" and context.user_data.get("admin_custom_bonus_child"):
        child_key = context.user_data.pop("admin_custom_bonus_child")
        try:
            minutes = int((update.message.text or "").strip())
            result = await add_time_bonus(child_key, minutes)
            await update.message.reply_text(
                f"✅ Google принял запрос. {child_display_name(child_key)} получает "
                f"{result['minutes']} минут.",
                reply_markup=admin_dashboard_keyboard(),
            )
        except Exception as error:
            await update.message.reply_text(
                f"❌ Не удалось добавить время:\n{error}",
                reply_markup=admin_dashboard_keyboard(),
            )
        return

    active_item = context.user_data.get("active_homework_item")
    child_key = child_key_for_user(update.effective_user.id)
    if active_item and child_key:
        text = (update.message.text or "").strip()
        if not text:
            return
        await update.message.reply_text("🤖 Проверяю, можно ли принять это задание по сообщению...")
        try:
            assessment = await asyncio.to_thread(
                assess_text_submission,
                active_item.get("task", ""),
                text,
            )
        except Exception as error:
            print(f"[TEXT SUBMISSION AI ERROR] {error}")
            await update.message.reply_text("⚠️ Не удалось оценить ответ. Пришли фото выполненного задания.")
            return
        task_lower = active_item.get("task", "").lower()
        if any(word in task_lower for word in ("прочитать", "прочти", "пересказ", "прочтение")):
            assessment.accepted = False
            assessment.needs_photo = True
            assessment.needs_voice = True
        if assessment.accepted and not assessment.needs_photo and not assessment.needs_voice:
            subject_key = active_item.get("subject_key")
            bonus_text = await register_homework_result(child_key, subject_key, max(76, assessment.percent))
            context.user_data.pop("active_homework_item", None)
            remaining = assigned_homework_items(child_key)
            if remaining:
                next_text = "\nВыбери следующее задание в разделе «Проверка домашки»."
            else:
                next_text = "\n🎉 Все задания на сегодня закрыты!"
            await update.message.reply_text(
                f"✅ {SCHEDULE_SUBJECTS.get(subject_key, active_item.get('subject', 'Задание'))} принято по сообщению."
                f"\n{assessment.summary}{bonus_text}{next_text}",
                reply_markup=main_menu_keyboard("child"),
            )
            return
        if assessment.needs_voice:
            reading_id = f"reading_{uuid.uuid4().hex[:12]}"
            reading_dir = HOMEWORK_DIR / reading_id
            reading_dir.mkdir(parents=True, exist_ok=True)
            context.user_data["reading_submission"] = {
                "directory": str(reading_dir),
                "subject_key": active_item.get("subject_key"),
                "subject": active_item.get("subject", ""),
                "task": active_item.get("task", ""),
                "photo_paths": [],
                "voice_path": None,
            }
            await update.message.reply_text(
                "📖 Для задания «прочитать» нужно прислать фото текста и короткий голосовой пересказ своими словами.\n\n"
                "Сначала можно отправить любой из них, затем второй. После этого нажми «🤖 Проверить пересказ».",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_homework")]]),
            )
            return
        context.user_data["selected_subject"] = active_item.get("subject_key", "AUTO")
        requirement = "голосовой пересказ и фото текста" if assessment.needs_voice else "фото выполненного задания"
        await update.message.reply_text(
            f"❌ Одного сообщения недостаточно. Для этого задания пришли {requirement}.\n\n"
            f"{assessment.summary}",
            reply_markup=upload_keyboard(),
        )
        return

    if not context.user_data.get(
        "chat_mode",
        False,
    ):

        return

    text = update.message.text

    if not text:
        return

    history = context.user_data.setdefault("chat_history", [])
    image_paths = []
    chat_photo_path = context.user_data.get("chat_photo_path")
    if chat_photo_path and Path(chat_photo_path).exists():
        image_paths.append(chat_photo_path)

    await update.message.reply_text(
        "🧠 Думаю..."
    )

    try:

        answer = await asyncio.to_thread(
            chat_with_ai,
            text,
            image_paths,
            history,
        )

    except Exception as e:

        print(
            f"[CHAT AI ERROR] {e}"
        )

        await update.message.reply_text(
            "⚠️ Не удалось получить ответ AI.\n"
            "Попробуй ещё раз."
        )

        return

    history.extend([
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ])

    await update.message.reply_text(
        answer,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🏠 Главное меню",
                        callback_data="main_menu",
                    )
                ]
            ]
        ),
    )


# ============================================================
# MAIN
# ============================================================

def main():

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            main_menu_callback,
            pattern="^main_menu$",
        )
    )

    app.add_handler(CallbackQueryHandler(admin_dashboard_callback, pattern="^admin_dashboard$"))
    app.add_handler(CallbackQueryHandler(admin_bonus_menu_callback, pattern="^admin_bonus_menu$"))
    app.add_handler(CallbackQueryHandler(admin_bonus_child_callback, pattern="^admin_bonus_child:"))
    app.add_handler(CallbackQueryHandler(admin_bonus_callback, pattern="^admin_bonus:[A-Z]+:[0-9]+$"))
    app.add_handler(CallbackQueryHandler(admin_bonus_custom_callback, pattern="^admin_bonus_custom:"))
    app.add_handler(CallbackQueryHandler(admin_devices_callback, pattern="^admin_devices$"))
    app.add_handler(CallbackQueryHandler(admin_settings_callback, pattern="^admin_settings$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_callback, pattern="^admin_toggle_(auto_rewards|maintenance|request_recipient|test_day)$"))
    app.add_handler(CallbackQueryHandler(parent_dashboard_callback, pattern="^parent_dashboard$"))
    app.add_handler(CallbackQueryHandler(location_menu_callback, pattern="^location_menu$"))
    app.add_handler(CallbackQueryHandler(location_show_callback, pattern="^location_show:"))
    app.add_handler(CallbackQueryHandler(schedule_main_callback, pattern="^schedule_menu$"))
    app.add_handler(CallbackQueryHandler(schedule_child_callback, pattern="^schedule_child:"))
    app.add_handler(CallbackQueryHandler(schedule_edit_child_callback, pattern="^schedule_edit:[A-Z]+$"))
    app.add_handler(CallbackQueryHandler(schedule_edit_day_callback, pattern="^schedule_edit_day:[A-Z]+:(mon|tue|wed|thu|fri)$"))
    app.add_handler(CallbackQueryHandler(schedule_change_callback, pattern="^schedule_(add|remove):[A-Z]+:(mon|tue|wed|thu|fri):[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(emergency_bonus_callback, pattern="^emergency_bonus$"))
    app.add_handler(CallbackQueryHandler(emergency_bonus_confirm_callback, pattern="^emergency_bonus_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(diary_start_callback, pattern="^diary_start$"))
    app.add_handler(CallbackQueryHandler(diary_cancel_callback, pattern="^diary_cancel$"))
    app.add_handler(CallbackQueryHandler(diary_check_callback, pattern="^diary_check$"))
    app.add_handler(CallbackQueryHandler(diary_parent_callback, pattern="^diary_(parent|review)_(yes|no):"))
    app.add_handler(CallbackQueryHandler(diary_view_callback, pattern="^diary_view:") )
    app.add_handler(CallbackQueryHandler(admin_diary_test_callback, pattern="^admin_diary_test$"))
    app.add_handler(CallbackQueryHandler(admin_diary_test_child_callback, pattern="^admin_diary_test_child:[A-Z]+$"))
    app.add_handler(CallbackQueryHandler(admin_diary_test_check_callback, pattern="^admin_diary_test_check$"))
    app.add_handler(CallbackQueryHandler(admin_diary_test_cancel_callback, pattern="^admin_diary_test_cancel$"))
    app.add_handler(CallbackQueryHandler(admin_diary_reset_callback, pattern="^admin_diary_reset$"))
    app.add_handler(CallbackQueryHandler(admin_diary_reset_confirm_callback, pattern="^admin_diary_reset_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(homework_submit_callback, pattern="^homework_submit:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(reading_evaluate_callback, pattern="^reading_evaluate$"))

    app.add_handler(
        CallbackQueryHandler(
            homework_menu_callback,
            pattern="^menu_homework$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            chat_menu_callback,
            pattern="^menu_chat$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            subject_callback,
            pattern="^subject_",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            check_homework_button,
            pattern="^check_homework$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_homework,
            pattern="^cancel_homework$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            parent_approval_callback,
            pattern="^parent_(yes|no):",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.VOICE,
            handle_voice_message,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_chat_message,
        )
    )

    print(
        "🤖 Telegram homework bot запущен"
    )

    if PARENT_ID:

        print(
            "👨‍👩‍👧 Parent approval: enabled "
            "(hidden)"
        )

    else:

        print(
            "⚠️ Parent approval: "
            "PARENT_ID не задан"
        )

    app.run_polling()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
