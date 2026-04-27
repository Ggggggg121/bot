import json
import logging
import os
import random
import time
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    TypeHandler,
    filters,
    ContextTypes,
    ApplicationHandlerStop,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789"))

STUDENTS_FILE = "students.json"
QUEUES_FILE = "queues.json"
PRIORITIES_FILE = "priorities.json"
USED_BTN_FILE = "used_buttons.json"  # Защита от спама кнопкой приоритета

SUBJECTS = ["оаип", "чм", "аисд"]
SUBGROUPS = ["1", "2"]
EXTRA_LIMIT = 10

SUBJECT_KEYWORDS = {
    "оаип": ["оаип"],
    "чм": ["чм", "числовые методы", "числовых методов"],
    "аисд": ["аисд"],
}

QUEUE_TRIGGERS = [
    "занимаю место на", "займу место на",
    "записываюсь на", "запишите меня на",
]

# ──────────────────────────────────────────────
# Антиспам система (в оперативной памяти)
# ──────────────────────────────────────────────
user_msg_times = {}
user_mute_level = {}
user_mute_until = {}
MUTE_STEPS = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300]


async def anti_spam_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    if user_id == ADMIN_ID:
        return  # Админ игнорирует лимиты

    now = time.time()

    # Проверка текущего мута
    if user_id in user_mute_until and now < user_mute_until[user_id]:
        raise ApplicationHandlerStop()

    # Сбор статистики сообщений
    if user_id not in user_msg_times:
        user_msg_times[user_id] = []

    user_msg_times[user_id].append(now)
    # Оставляем только сообщения за последние 10 секунд
    user_msg_times[user_id] = [t for t in user_msg_times[user_id] if now - t <= 10]

    # Если больше 5 сообщений за 10 сек -> Мут
    if len(user_msg_times[user_id]) > 5:
        level = user_mute_level.get(user_id, 0)
        mute_duration = MUTE_STEPS[min(level, len(MUTE_STEPS) - 1)]

        user_mute_until[user_id] = now + mute_duration
        user_mute_level[user_id] = level + 1
        user_msg_times[user_id] = []  # Сброс счетчика

        await update.effective_message.reply_text(
            f"🛑 *Сработала защита от спама!*\n"
            f"Вы добавлены в мут бота на *{mute_duration} секунд*.",
            parse_mode="Markdown"
        )
        raise ApplicationHandlerStop()


# ──────────────────────────────────────────────
# Работа с данными
# ──────────────────────────────────────────────
def load_json(filepath: str, default: dict) -> dict:
    if not os.path.exists(filepath):
        return default
    with open(filepath, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json(filepath: str, data: dict) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_students() -> dict:
    return load_json(STUDENTS_FILE, {})


def load_queues() -> dict:
    default_queues = {"subjects": {s: {"1": [], "2": []} for s in SUBJECTS}, "extra": []}
    data = load_json(QUEUES_FILE, default_queues)
    if "subjects" not in data:  # Миграция со старого формата
        return {"subjects": data, "extra": []}
    return data


def save_queues(queues: dict) -> None:
    save_json(QUEUES_FILE, queues)


def load_priorities() -> dict:
    return load_json(PRIORITIES_FILE, {s: {} for s in SUBJECTS})


def save_priorities(priorities: dict) -> None:
    save_json(PRIORITIES_FILE, priorities)


def load_used_btns() -> dict:
    return load_json(USED_BTN_FILE, {s: [] for s in SUBJECTS})


def save_used_btns(data: dict) -> None:
    save_json(USED_BTN_FILE, data)


def get_student(user_id: int, students: dict) -> dict | None:
    return students.get(str(user_id))


def get_next_pos(queue: list) -> int:
    """Находит первое свободное место в очереди"""
    occupied = {e.get('pos', 0) for e in queue}
    pos = 1
    while pos in occupied:
        pos += 1
    return pos


# ──────────────────────────────────────────────
# Форматирование и Вывод
# ──────────────────────────────────────────────
def format_queue_message(queues: dict) -> str:
    lines = ["📋 *Текущие очереди на сдачу лаб:*\n"]
    for subject in SUBJECTS:
        lines.append(f"━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📚 *{subject.upper()}*")
        for sub in SUBGROUPS:
            queue = sorted(queues["subjects"][subject][sub], key=lambda x: x.get('pos', 999))
            lines.append(f"  👥 Подгруппа {sub}:")
            if not queue:
                lines.append("    — очередь пуста")
            else:
                for entry in queue:
                    marker = "🔥" if "Приоритет" in entry['name'] else "👤"
                    lines.append(f"    [{entry.get('pos', '?')}] {marker} {entry['name']}  ·  {entry['time']}")
        lines.append("")

    # Extra очередь
    lines.append(f"━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🎓 *EXTRA-ОЧЕРЕДЬ (внеучебная)* [{len(queues['extra'])}/{EXTRA_LIMIT}]")
    if not queues['extra']:
        lines.append("    — очередь пуста")
    else:
        for i, entry in enumerate(queues['extra'], 1):
            lines.append(f"    {i}. {entry['name']}  ·  {entry['time']}")

    return "\n".join(lines)


def get_queue_keyboard():
    keyboard = []
    row = []
    for subj in SUBJECTS:
        row.append(InlineKeyboardButton(f"❌ Не сдал {subj.upper()}", callback_data=f"missed_{subj}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


# ──────────────────────────────────────────────
# Логика Очередей и Приоритетов
# ──────────────────────────────────────────────
async def _enqueue(update: Update, subject: str, user_id: int, student: dict) -> None:
    subgroup = str(student["subgroup"])
    name = f"{student['name']} {student['surname']}"

    queues = load_queues()
    queue = queues["subjects"][subject][subgroup]

    if any(e["user_id"] == user_id for e in queue):
        await update.message.reply_text(
            f"⚠️ *{name}*, ты уже в очереди на *{subject.upper()}* (подгруппа {subgroup}).",
            parse_mode="Markdown"
        )
        return

    pos = get_next_pos(queue)
    entry = {
        "user_id": user_id,
        "name": name,
        "time": datetime.now().strftime("%H:%M %d.%m"),
        "pos": pos
    }
    queues["subjects"][subject][subgroup].append(entry)
    save_queues(queues)

    await update.message.reply_text(
        f"✅ *{name}* занял(а) место *№{pos}* на *{subject.upper()}*\n"
        f"Подгруппа: {subgroup} | {entry['time']}",
        parse_mode="Markdown",
    )


async def btn_missed_lab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    subject = query.data.split("_")[1]
    user_id = query.from_user.id

    students = load_students()
    if not get_student(user_id, students):
        await query.message.reply_text("❌ Твой ID не найден в базе студентов.")
        return

    used_btns = load_used_btns()
    if user_id in used_btns[subject]:
        await query.message.reply_text(f"⚠️ Ты уже запрашивал приоритет на {subject.upper()} в этом цикле!")
        return

    # Начисляем приоритет
    priorities = load_priorities()
    uid_str = str(user_id)
    priorities[subject][uid_str] = priorities[subject].get(uid_str, 0) + 1

    # Фиксируем нажатие
    used_btns[subject].append(user_id)

    save_priorities(priorities)
    save_used_btns(used_btns)

    await query.message.reply_text(
        f"🔥 Приоритет на *{subject.upper()}* повышен! При следующей очистке ты займешь первые места.",
        parse_mode="Markdown")


# ──────────────────────────────────────────────
# Команды пользователей
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Бот управления очередями*\n\n"
        "Записаться: `занимаю место на оаип`\n"
        "Выйти: `/leave оаип`\n"
        "Extra-очередь: `/extra` (выйти: `/leave_extra`)\n\n"
        "📋 Посмотреть очереди: /queue",
        parse_mode="Markdown"
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queues = load_queues()
    await update.message.reply_text(
        format_queue_message(queues),
        reply_markup=get_queue_keyboard(),
        parse_mode="Markdown"
    )


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower()
    if not any(trigger in text for trigger in QUEUE_TRIGGERS):
        return

    subject = next((s for s, kw in SUBJECT_KEYWORDS.items() if any(k in text for k in kw)), None)
    if not subject:
        return

    user_id = update.message.from_user.id
    student = get_student(user_id, load_students())

    if not student:
        await update.message.reply_text("❌ ID не найден в базе студентов.")
        return

    await _enqueue(update, subject, user_id, student)


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: `/leave <предмет>`", parse_mode="Markdown")
        return

    subject = context.args[0].lower()
    if subject not in SUBJECTS:
        return

    user_id = update.effective_user.id
    student = get_student(user_id, load_students())
    if not student: return

    subgroup = str(student["subgroup"])
    queues = load_queues()
    queue = queues["subjects"][subject][subgroup]

    new_queue = [e for e in queue if e["user_id"] != user_id]
    if len(new_queue) == len(queue):
        await update.message.reply_text("⚠️ Вас нет в этой очереди.")
        return

    queues["subjects"][subject][subgroup] = new_queue
    save_queues(queues)
    await update.message.reply_text(f"✅ Вы покинули очередь по {subject.upper()}. Место освободилось.")


# --- Extra Queue ---
async def cmd_extra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    student = get_student(user_id, load_students())
    if not student: return

    queues = load_queues()
    if any(e["user_id"] == user_id for e in queues["extra"]):
        await update.message.reply_text("⚠️ Вы уже в Extra-очереди.")
        return

    if len(queues["extra"]) >= EXTRA_LIMIT:
        await update.message.reply_text("❌ Extra-очередь переполнена (максимум 10 человек).")
        return

    name = f"{student['name']} {student['surname']}"
    queues["extra"].append({
        "user_id": user_id,
        "name": name,
        "time": datetime.now().strftime("%H:%M")
    })
    save_queues(queues)
    await update.message.reply_text(f"✅ {name} записан в Extra-очередь на позицию {len(queues['extra'])}.")


async def cmd_leave_extra(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    queues = load_queues()
    new_extra = [e for e in queues["extra"] if e["user_id"] != user_id]

    if len(new_extra) == len(queues["extra"]):
        await update.message.reply_text("⚠️ Вас нет в Extra-очереди.")
        return

    queues["extra"] = new_extra
    save_queues(queues)
    await update.message.reply_text("✅ Вы покинули Extra-очередь.")


# ──────────────────────────────────────────────
# Логика Честной Очистки (Админ)
# ──────────────────────────────────────────────
def _admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ Доступ запрещен.")
            return
        await func(update, context)

    return wrapper


async def execute_clear_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фоновая задача честной очистки очереди"""
    data = context.job.data
    subject = data['subject']
    subgroup = data['subgroup']
    chat_id = data['chat_id']

    queues = load_queues()
    priorities = load_priorities()
    students = load_students()
    used_btns = load_used_btns()

    queue = queues["subjects"][subject][subgroup]

    # 1. Выдаем приоритет последним 3 людям перед удалением
    queue_sorted = sorted(queue, key=lambda x: x.get('pos', 999))
    for entry in queue_sorted[-3:]:
        uid_str = str(entry["user_id"])
        priorities[subject][uid_str] = priorities[subject].get(uid_str, 0) + 1

    # 2. Очищаем саму очередь
    queues["subjects"][subject][subgroup] = []

    # Сбрасываем кулдаун кнопки "Не успел" для предмета
    used_btns[subject] = []
    save_used_btns(used_btns)

    # 3. Распределяем места приоритетникам
    prio_users = []
    for uid_str, prio in priorities[subject].items():
        if prio > 0:
            student = get_student(int(uid_str), students)
            if student and str(student['subgroup']) == subgroup:
                prio_users.append({
                    "uid": int(uid_str),
                    "prio": prio,
                    "name": f"{student['name']} {student['surname']}"
                })

    # Сортируем: у кого больше приоритет - тот выше
    prio_users.sort(key=lambda x: x['prio'], reverse=True)

    # Назначаем позиции через одну: 1, 3, 5, 7...
    pos = 1
    for p_user in prio_users:
        queues["subjects"][subject][subgroup].append({
            "user_id": p_user["uid"],
            "name": f"{p_user['name']} [Приоритет]",
            "time": "Авто",
            "pos": pos
        })
        # Сбрасываем приоритет после успешного применения
        priorities[subject][str(p_user["uid"])] = 0
        pos += 2

    save_queues(queues)
    save_priorities(priorities)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔄 *Очередь на {subject.upper()} (подгр. {subgroup}) очищена!*\n"
             f"Приоритетные места распределены автоматически. Можно занимать пустые слоты.",
        parse_mode="Markdown"
    )


@_admin_only
async def cmd_clear_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: `/clear <предмет> [подгруппа]`", parse_mode="Markdown")
        return

    subject = context.args[0].lower()
    if subject not in SUBJECTS: return

    subgroups_to_clear = [context.args[1]] if len(context.args) > 1 else SUBGROUPS

    delay = random.randint(30, 300)  # Случайная задержка от 30 сек до 5 минут

    for sg in subgroups_to_clear:
        if sg in SUBGROUPS:
            context.job_queue.run_once(
                execute_clear_job,
                when=delay,
                data={"subject": subject, "subgroup": sg, "chat_id": update.effective_chat.id}
            )

    await update.message.reply_text(
        f"⏳ Запущена честная очистка *{subject.upper()}*.\n"
        f"Очередь сбросится и пересчитается в случайное время в течение 5 минут. Всем придет уведомление.",
        parse_mode="Markdown"
    )


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # 0. Anti-Spam Middleware (выполняется до команд)
    app.add_handler(TypeHandler(Update, anti_spam_middleware), group=-1)

    # Общие команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("leave", cmd_leave))

    # Extra очередь
    app.add_handler(CommandHandler("extra", cmd_extra))
    app.add_handler(CommandHandler("leave_extra", cmd_leave_extra))

    # Кнопки "Не успел сдать"
    app.add_handler(CallbackQueryHandler(btn_missed_lab, pattern="^missed_"))

    # Админские команды
    app.add_handler(CommandHandler("clear", cmd_clear_subject))  # Переработан под отложенный вызов

    # Парсинг текста
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    logger.info("Бот с новой архитектурой запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()