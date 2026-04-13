import json
import logging
import os
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

STUDENTS_FILE = "students.json"
QUEUES_FILE = "queues.json"

SUBJECTS = ["оаип", "чм", "аисд"]
SUBGROUPS = ["1", "2"]

# Ключевые слова для определения предмета
SUBJECT_KEYWORDS = {
    "оаип": ["оаип"],
    "чм":   ["чм", "числовые методы", "числовых методов"],
    "аисд": ["аисд"],
}

# Фразы-триггеры для записи в очередь
QUEUE_TRIGGERS = [
    "занимаю место на",
    "займу место на",
    "записываюсь на",
    "запишите меня на",
]

# ──────────────────────────────────────────────
# Работа с данными
# ──────────────────────────────────────────────

def load_students() -> dict:
    with open(STUDENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_queues() -> dict:
    if not os.path.exists(QUEUES_FILE):
        return {s: {"1": [], "2": []} for s in SUBJECTS}
    with open(QUEUES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queues(queues: dict) -> None:
    with open(QUEUES_FILE, "w", encoding="utf-8") as f:
        json.dump(queues, f, ensure_ascii=False, indent=2)


def get_student(user_id: int, students: dict) -> dict | None:
    return students.get(str(user_id))


def detect_subject(text: str) -> str | None:
    text_lower = text.lower()
    for subject, keywords in SUBJECT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return subject
    return None


def is_queue_trigger(text: str) -> bool:
    text_lower = text.lower()
    return any(trigger in text_lower for trigger in QUEUE_TRIGGERS)


def format_queue_message(queues: dict) -> str:
    lines = ["📋 *Текущие очереди на сдачу лаб:*\n"]
    for subject in SUBJECTS:
        lines.append(f"━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📚 *{subject.upper()}*")
        for sub in SUBGROUPS:
            queue = queues[subject][sub]
            lines.append(f"  👥 Подгруппа {sub}:")
            if not queue:
                lines.append("    — очередь пуста")
            else:
                for i, entry in enumerate(queue, 1):
                    lines.append(f"    {i}. {entry['name']}  ·  {entry['time']}")
        lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Общие команды
# ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Привет! Я бот управления очередями на сдачу лаб.*\n\n"
        "📌 *Чтобы записаться*, напиши в группе:\n"
        "`занимаю место на оаип` (или чм / аисд)\n\n"
        "📋 /queue — посмотреть все очереди\n\n"
        "➕ *Принудительная запись (резерв):*\n"
        "/add\\_oaip — записаться на ОАИП\n"
        "/add\\_chm — записаться на ЧМ\n"
        "/add\\_aisd — записаться на АИСД\n\n"
        "🔧 *Команды администратора:*\n"
        "/remove `<предмет> <подгруппа> <user_id>` — убрать из очереди\n"
        "/clear\\_all — очистить все очереди\n"
        "/clear\\_sub `<1|2>` — очистить очереди подгруппы\n"
        "/clear\\_subject `<предмет> [подгруппа]` — очистить очередь предмета",
        parse_mode="Markdown",
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queues = load_queues()
    await update.message.reply_text(format_queue_message(queues), parse_mode="Markdown")


# ──────────────────────────────────────────────
# Парсинг сообщений группы
# ──────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if not is_queue_trigger(text):
        return

    subject = detect_subject(text)
    if not subject:
        await update.message.reply_text(
            "❓ Не могу определить предмет из сообщения.\n"
            "Используй принудительную запись:\n"
            "/add\\_oaip | /add\\_chm | /add\\_aisd",
            parse_mode="Markdown",
        )
        return

    user_id = update.message.from_user.id
    students = load_students()
    student = get_student(user_id, students)

    if not student:
        await update.message.reply_text(
            "❌ Твой Telegram ID не найден в базе студентов.\n"
            "Обратись к администратору."
        )
        return

    await _enqueue(update, subject, user_id, student)


# ──────────────────────────────────────────────
# Принудительная запись (резервные команды)
# ──────────────────────────────────────────────

async def _enqueue(
    update: Update,
    subject: str,
    user_id: int,
    student: dict,
) -> None:
    subgroup = str(student["subgroup"])
    name = f"{student['name']} {student['surname']}"

    queues = load_queues()
    queue = queues[subject][subgroup]

    if any(e["user_id"] == user_id for e in queue):
        await update.message.reply_text(
            f"⚠️ *{name}*, ты уже в очереди на *{subject.upper()}* "
            f"(подгруппа {subgroup}).",
            parse_mode="Markdown",
        )
        return

    entry = {
        "user_id": user_id,
        "name": name,
        "time": datetime.now().strftime("%H:%M  %d.%m.%Y"),
    }
    queues[subject][subgroup].append(entry)
    save_queues(queues)

    pos = len(queues[subject][subgroup])
    await update.message.reply_text(
        f"✅ *{name}* записан(а) в очередь на *{subject.upper()}*\n"
        f"Подгруппа: {subgroup}  |  Позиция: {pos}  |  {entry['time']}",
        parse_mode="Markdown",
    )


async def _force_add(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    subject: str,
) -> None:
    """
    Без аргументов — записывает себя.
    Администратор может передать user_id: /add_oaip 123456789
    """
    caller_id = update.effective_user.id
    students = load_students()

    target_id = caller_id
    if context.args:
        if caller_id != ADMIN_ID:
            await update.message.reply_text("❌ Только администратор может записывать других.")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неверный формат user_id. Пример: /add_oaip 123456789")
            return

    student = get_student(target_id, students)
    if not student:
        await update.message.reply_text(
            f"❌ ID `{target_id}` не найден в базе студентов.",
            parse_mode="Markdown",
        )
        return

    await _enqueue(update, subject, target_id, student)


async def cmd_add_oaip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _force_add(update, context, "оаип")


async def cmd_add_chm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _force_add(update, context, "чм")


async def cmd_add_aisd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _force_add(update, context, "аисд")


# ──────────────────────────────────────────────
# Команды администратора
# ──────────────────────────────────────────────

def _admin_only(func):
    """Декоратор — только для администратора."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("❌ У тебя нет прав администратора.")
            return
        await func(update, context)
    return wrapper


@_admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /remove <предмет> <подгруппа> <user_id>
    Убирает конкретного пользователя из очереди и сдвигает очередь.
    """
    if len(context.args) < 3:
        await update.message.reply_text(
            "Использование:\n`/remove <предмет> <подгруппа> <user_id>`\n\n"
            "Пример: `/remove оаип 1 123456789`",
            parse_mode="Markdown",
        )
        return

    subject = context.args[0].lower()
    subgroup = context.args[1]

    if subject not in SUBJECTS:
        await update.message.reply_text(
            f"❌ Предмет не найден. Доступные: `{', '.join(SUBJECTS)}`",
            parse_mode="Markdown",
        )
        return

    if subgroup not in SUBGROUPS:
        await update.message.reply_text("❌ Подгруппа должна быть 1 или 2.")
        return

    try:
        target_id = int(context.args[2])
    except ValueError:
        await update.message.reply_text("❌ user_id должен быть числом.")
        return

    queues = load_queues()
    queue = queues[subject][subgroup]
    new_queue = [e for e in queue if e["user_id"] != target_id]

    if len(new_queue) == len(queue):
        await update.message.reply_text("❌ Пользователь не найден в этой очереди.")
        return

    queues[subject][subgroup] = new_queue
    save_queues(queues)

    await update.message.reply_text(
        f"✅ Пользователь `{target_id}` удалён из очереди "
        f"*{subject.upper()}* (подгруппа {subgroup}).\n"
        f"Очередь сдвинута.",
        parse_mode="Markdown",
    )


@_admin_only
async def cmd_clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear_all — очистить все очереди."""
    queues = {s: {"1": [], "2": []} for s in SUBJECTS}
    save_queues(queues)
    await update.message.reply_text("✅ Все очереди полностью очищены.")


@_admin_only
async def cmd_clear_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear_sub <1|2> — очистить все очереди конкретной подгруппы."""
    if not context.args:
        await update.message.reply_text("Использование: `/clear_sub <1|2>`", parse_mode="Markdown")
        return

    subgroup = context.args[0]
    if subgroup not in SUBGROUPS:
        await update.message.reply_text("❌ Подгруппа должна быть 1 или 2.")
        return

    queues = load_queues()
    for s in SUBJECTS:
        queues[s][subgroup] = []
    save_queues(queues)
    await update.message.reply_text(f"✅ Все очереди подгруппы *{subgroup}* очищены.", parse_mode="Markdown")


@_admin_only
async def cmd_clear_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /clear_subject <предмет>          — очистить обе подгруппы предмета
    /clear_subject <предмет> <1|2>    — очистить конкретную подгруппу предмета
    """
    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "`/clear_subject <предмет>` — обе подгруппы\n"
            "`/clear_subject <предмет> <1|2>` — одна подгруппа\n\n"
            f"Предметы: `{', '.join(SUBJECTS)}`",
            parse_mode="Markdown",
        )
        return

    subject = context.args[0].lower()
    if subject not in SUBJECTS:
        await update.message.reply_text(
            f"❌ Предмет не найден. Доступные: `{', '.join(SUBJECTS)}`",
            parse_mode="Markdown",
        )
        return

    queues = load_queues()

    if len(context.args) >= 2:
        subgroup = context.args[1]
        if subgroup not in SUBGROUPS:
            await update.message.reply_text("❌ Подгруппа должна быть 1 или 2.")
            return
        queues[subject][subgroup] = []
        save_queues(queues)
        await update.message.reply_text(
            f"✅ Очередь *{subject.upper()}* подгруппы *{subgroup}* очищена.",
            parse_mode="Markdown",
        )
    else:
        queues[subject] = {"1": [], "2": []}
        save_queues(queues)
        await update.message.reply_text(
            f"✅ Все очереди *{subject.upper()}* (обе подгруппы) очищены.",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Общие команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))

    # Принудительная запись
    app.add_handler(CommandHandler("add_oaip", cmd_add_oaip))
    app.add_handler(CommandHandler("add_chm", cmd_add_chm))
    app.add_handler(CommandHandler("add_aisd", cmd_add_aisd))

    # Команды администратора
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("clear_all", cmd_clear_all))
    app.add_handler(CommandHandler("clear_sub", cmd_clear_sub))
    app.add_handler(CommandHandler("clear_subject", cmd_clear_subject))

    # Парсинг сообщений группы
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
