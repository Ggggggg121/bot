import json
import logging
import os
import random
import time
import asyncio
from collections import defaultdict
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

# DATA_DIR — путь к папке с данными (на Railway = /app/data через Volume)
# Если переменная не задана, используем текущую директорию (локально)
DATA_DIR = os.environ.get("DATA_DIR", ".")
STUDENTS_FILE = os.path.join(DATA_DIR, "students.json")
QUEUES_FILE   = os.path.join(DATA_DIR, "queues.json")
PRIORITY_FILE = os.path.join(DATA_DIR, "priority_data.json")

SUBJECTS = ["оаип", "чм", "аисд"]
SUBGROUPS = ["1", "2"]

SUBJECT_KEYWORDS = {
    "оаип": ["оаип"],
    "чм":   ["чм", "числовые методы", "числовых методов"],
    "аисд": ["аисд"],
}

QUEUE_TRIGGERS = [
    "занимаю место на",
    "займу место на",
    "записываюсь на",
    "запишите меня на",
]

# ──────────────────────────────────────────────
# Антиспам (Feature 2)
# Хранится в памяти — сбрасывается при рестарте
# ──────────────────────────────────────────────
SPAM_WINDOW = 10    # секунд — окно наблюдения
SPAM_LIMIT = 5      # максимум попыток за окно
# Длительности мутов: 10, 30, 60, 90, 120 ... 300 секунд
MUTE_DURATIONS = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300]

# {user_id: [unix_timestamp, ...]}
spam_tracker: dict[int, list[float]] = defaultdict(list)
# {user_id: {"count": int, "until": float}}
mute_state: dict[int, dict] = defaultdict(lambda: {"count": 0, "until": 0.0})


def check_and_record_spam(user_id: int) -> tuple[bool, str | None]:
    """
    Проверяет, заблокирован ли пользователь антиспамом.
    Записывает новую попытку и при превышении лимита выдаёт мут.
    Возвращает (заблокирован: bool, текст_предупреждения: str | None).
    """
    now = time.time()
    mute = mute_state[user_id]

    # Пользователь уже в муте
    if mute["until"] > now:
        remaining = int(mute["until"] - now)
        return True, f"🔇 Ты в муте ещё *{remaining}* сек. из-за спама."

    # Записываем новую попытку и чистим старые
    spam_tracker[user_id].append(now)
    spam_tracker[user_id] = [
        t for t in spam_tracker[user_id] if now - t <= SPAM_WINDOW
    ]

    # Лимит превышен — назначаем мут
    if len(spam_tracker[user_id]) > SPAM_LIMIT:
        spam_tracker[user_id].clear()
        step = mute["count"]
        duration = MUTE_DURATIONS[min(step, len(MUTE_DURATIONS) - 1)]
        mute["count"] += 1
        mute["until"] = now + duration
        return True, (
            f"🔇 Слишком много попыток за {SPAM_WINDOW} сек.!\n"
            f"Мут на *{duration}* сек."
        )

    return False, None


# ──────────────────────────────────────────────
# Работа с основными данными
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
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📚 *{subject.upper()}*")
        for sub in SUBGROUPS:
            queue = queues[subject][sub]
            lines.append(f"  👥 Подгруппа {sub}:")
            if not queue:
                lines.append("    — очередь пуста")
            else:
                for i, entry in enumerate(queue, 1):
                    # Показываем значок приоритета если он есть
                    badge = " 🔝" if entry.get("priority", 0) > 0 else ""
                    lines.append(
                        f"    {i}. {entry['name']}{badge}  ·  {entry['time']}"
                    )
        lines.append("")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Работа с приоритетами (Feature 1)
# ──────────────────────────────────────────────

def load_priority() -> dict:
    if not os.path.exists(PRIORITY_FILE):
        return {s: {"1": [], "2": []} for s in SUBJECTS}
    with open(PRIORITY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Гарантируем наличие всех ключей
    for s in SUBJECTS:
        if s not in data:
            data[s] = {"1": [], "2": []}
        for sg in SUBGROUPS:
            if sg not in data[s]:
                data[s][sg] = []
    return data


def save_priority(priority_data: dict) -> None:
    with open(PRIORITY_FILE, "w", encoding="utf-8") as f:
        json.dump(priority_data, f, ensure_ascii=False, indent=2)


def add_user_priority(user_id: int, name: str, subject: str, subgroup: str) -> int:
    """
    Увеличивает приоритет пользователя на 1.
    Возвращает новый уровень приоритета.
    """
    priority_data = load_priority()
    p_list = priority_data[subject][subgroup]
    existing = next((p for p in p_list if p["user_id"] == user_id), None)
    if existing:
        existing["priority"] += 1
        new_priority = existing["priority"]
    else:
        p_list.append({"user_id": user_id, "name": name, "priority": 1})
        new_priority = 1
    save_priority(priority_data)
    return new_priority


def give_tail_priority(queue: list, subject: str, subgroup: str) -> None:
    """
    Выдаёт приоритет +1 последним 3 участникам очереди.
    Вызывается непосредственно перед очисткой.
    """
    if not queue:
        return
    tail = queue[max(0, len(queue) - 3):]
    priority_data = load_priority()
    p_list = priority_data[subject][subgroup]
    for entry in tail:
        uid = entry["user_id"]
        existing = next((p for p in p_list if p["user_id"] == uid), None)
        if existing:
            existing["priority"] += 1
        else:
            p_list.append({
                "user_id": uid,
                "name": entry["name"],
                "priority": 1,
            })
    save_priority(priority_data)


def build_priority_queue(subject: str, subgroup: str) -> list:
    """
    Формирует начало новой очереди из приоритетных участников
    (сортировка по убыванию приоритета).
    После использования обнуляет их приоритет.
    """
    priority_data = load_priority()
    p_list = sorted(
        priority_data[subject][subgroup],
        key=lambda x: x["priority"],
        reverse=True,
    )
    new_queue = []
    for p_entry in p_list:
        new_queue.append({
            "user_id": p_entry["user_id"],
            "name": p_entry["name"],
            "time": datetime.now().strftime("%H:%M  %d.%m.%Y"),
            "priority": p_entry["priority"],  # для отображения значка 🔝
        })
    # Приоритет использован — сбрасываем
    priority_data[subject][subgroup] = []
    save_priority(priority_data)
    return new_queue


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
        "🔝 *Не успел сдать лабораторную?*\n"
        "/missed `<предмет>` — получить приоритет в следующей очереди\n"
        "_(пример: /missed оаип)_\n\n"
        "🚪 *Выйти из очереди самостоятельно:*\n"
        "/leave `<предмет>` — покинуть очередь\n"
        "_(пример: /leave чм)_\n\n"
        "🔧 *Команды администратора:*\n"
        "/remove `<предмет> <подгруппа> <user_id>` — убрать из очереди\n"
        "/clear\\_all — очистить все очереди *(с задержкой до 5 мин)*\n"
        "/clear\\_sub `<1|2>` — очистить очереди подгруппы\n"
        "/clear\\_subject `<предмет> [подгруппа]` — очистить очередь предмета",
        parse_mode="Markdown",
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queues = load_queues()
    await update.message.reply_text(format_queue_message(queues), parse_mode="Markdown")


# ──────────────────────────────────────────────
# /missed — регистрация приоритета (Feature 1)
# ──────────────────────────────────────────────

async def cmd_missed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/missed <предмет> — зарегистрировать приоритет для следующей очереди."""
    user_id = update.effective_user.id
    students = load_students()
    student = get_student(user_id, students)

    if not student:
        await update.message.reply_text(
            "❌ Твой Telegram ID не найден в базе студентов.\n"
            "Обратись к администратору."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: `/missed <предмет>`\n"
            f"Предметы: `{', '.join(SUBJECTS)}`\n\n"
            "_Пример: /missed оаип_",
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

    subgroup = str(student["subgroup"])
    name = f"{student['name']} {student['surname']}"
    new_priority = add_user_priority(user_id, name, subject, subgroup)

    await update.message.reply_text(
        f"🔝 *{name}*, приоритет для *{subject.upper()}* (подгруппа {subgroup}) зарегистрирован!\n\n"
        f"Твой уровень приоритета: *{new_priority}*\n"
        f"При следующей очистке очереди ты будешь автоматически добавлен(а) в начало.",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# /leave — самостоятельный выход из очереди (Feature 4)
# ──────────────────────────────────────────────

async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/leave <предмет> — покинуть очередь самостоятельно."""
    user_id = update.effective_user.id
    students = load_students()
    student = get_student(user_id, students)

    if not student:
        await update.message.reply_text(
            "❌ Твой Telegram ID не найден в базе студентов.\n"
            "Обратись к администратору."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "Использование: `/leave <предмет>`\n"
            f"Предметы: `{', '.join(SUBJECTS)}`\n\n"
            "_Пример: /leave чм_",
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

    subgroup = str(student["subgroup"])
    name = f"{student['name']} {student['surname']}"

    queues = load_queues()
    queue = queues[subject][subgroup]
    new_queue = [e for e in queue if e["user_id"] != user_id]

    if len(new_queue) == len(queue):
        await update.message.reply_text(
            f"❌ *{name}*, ты не находишься в очереди на *{subject.upper()}* "
            f"(подгруппа {subgroup}).",
            parse_mode="Markdown",
        )
        return

    queues[subject][subgroup] = new_queue
    save_queues(queues)

    await update.message.reply_text(
        f"✅ *{name}*, ты вышел(ла) из очереди на *{subject.upper()}* "
        f"(подгруппа {subgroup}).\n"
        f"Очередь сдвинута.",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# Парсинг сообщений группы (с антиспамом)
# ──────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text
    if not is_queue_trigger(text):
        return

    user_id = update.message.from_user.id

    # Антиспам — проверяем перед любым действием с очередью
    blocked, warn_msg = check_and_record_spam(user_id)
    if blocked:
        await update.message.reply_text(warn_msg, parse_mode="Markdown")
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
    Без аргументов — записывает себя (с проверкой антиспама).
    Администратор может передать user_id: /add_oaip 123456789
    """
    caller_id = update.effective_user.id
    students = load_students()

    target_id = caller_id

    if context.args:
        # Запись другого — только для администратора
        if caller_id != ADMIN_ID:
            await update.message.reply_text("❌ Только администратор может записывать других.")
            return
        try:
            target_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат user_id. Пример: /add_oaip 123456789"
            )
            return
    else:
        # Самозапись — проверяем антиспам (кроме администратора)
        if caller_id != ADMIN_ID:
            blocked, warn_msg = check_and_record_spam(caller_id)
            if blocked:
                await update.message.reply_text(warn_msg, parse_mode="Markdown")
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
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast <сообщение> — отправить сообщение всем студентам в ЛС."""
    if not context.args:
        await update.message.reply_text(
            "❌ Напиши сообщение после команды.\n"
            "Пример: /broadcast Ребята, завтра лабы отменяются!",
            parse_mode="Markdown"
        )
        return

    # Получаем весь текст после команды /broadcast
    text = update.message.text.split(maxsplit=1)[1]

    students = load_students()
    success_count = 0
    fail_count = 0

    await update.message.reply_text(f"⏳ Начинаю рассылку для {len(students)} студентов...")

    for uid_str in students.keys():
        try:
            user_id = int(uid_str)
            # Отправляем сообщение
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 *Объявление:*\n\n{text}",
                parse_mode="Markdown"
            )
            success_count += 1

            # Небольшая пауза, чтобы Telegram не забанил бота за спам (Flood Wait)
            await asyncio.sleep(0.05)

        except Exception as e:
            # Сюда попадут те, кто не запустил бота в ЛС или заблокировал его
            logger.warning(f"Не удалось отправить рассылку пользователю {uid_str}: {e}")
            fail_count += 1

    # Отчет администратору
    await update.message.reply_text(
        f"✅ *Рассылка завершена!*\n\n"
        f"Успешно доставлено: *{success_count}*\n"
        f"Не доставлено: *{fail_count}* _(люди не запустили бота в ЛС или заблокировали его)_",
        parse_mode="Markdown"
    )

@_admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remove <предмет> <подгруппа> <user_id>"""
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


# ──────────────────────────────────────────────
# Отложенная очистка — джоб (Feature 3)
# ──────────────────────────────────────────────

async def clear_queue_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Выполняет отложенную очистку очередей.
    1. Даёт приоритет последним 3 участникам каждой очереди.
    2. Очищает очереди.
    3. Авто-добавляет приоритетных участников в начало новых очередей.
    4. Уведомляет группу и каждого участника лично.
    """
    data = context.job.data
    chat_id: int = data["chat_id"]
    pairs: list = data["pairs"]   # [[subject, subgroup], ...]
    bot = context.bot

    queues = load_queues()
    affected_users: set[int] = set()

    for pair in pairs:
        subject, subgroup = pair[0], pair[1]
        queue = queues[subject][subgroup]

        # Запоминаем всех участников для личных уведомлений
        for entry in queue:
            affected_users.add(entry["user_id"])

        # Приоритет последним 3 (до очистки!)
        give_tail_priority(queue, subject, subgroup)

        # Строим новую очередь из приоритетных и сбрасываем их приоритет
        new_queue = build_priority_queue(subject, subgroup)
        queues[subject][subgroup] = new_queue

    save_queues(queues)

    # Проверяем, есть ли кто-то авто-добавленный
    has_priority_users = any(
        entry.get("priority", 0) > 0
        for pair in pairs
        for entry in queues[pair[0]][pair[1]]
    )

    priority_note = (
        "\n🔝 Приоритетные участники автоматически добавлены в начало новой очереди."
        if has_priority_users else ""
    )

    # Уведомляем группу
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🗑️ *Очередь очищена!*{priority_note}\n\n"
                f"Записывайтесь заново! /queue — посмотреть текущие очереди."
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Ошибка отправки в чат {chat_id}: {e}")

    # Личные уведомления каждому участнику
    for uid in affected_users:
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    "📣 *Очередь была очищена!*\n\n"
                    "Если у тебя был приоритет — ты уже добавлен(а) в начало новой очереди.\n"
                    "Если нет — запишись заново!\n\n"
                    "/queue — посмотреть текущие очереди"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            # Пользователь не начал диалог с ботом — пропускаем
            pass


def _schedule_clear(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    pairs: list,
) -> None:
    """
    Планирует очистку через случайное время от 0 до 5 минут.
    Сериализуемые данные (пары subject/subgroup) передаём как списки, не кортежи.
    """
    delay = random.randint(0, 300)
    context.job_queue.run_once(
        clear_queue_job,
        when=delay,
        data={"chat_id": chat_id, "pairs": [[s, sg] for s, sg in pairs]},
    )
    logger.info(f"Очистка запланирована через {delay} сек. Пары: {pairs}")


@_admin_only
async def cmd_clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear_all — очистить все очереди (с случайной задержкой до 5 мин)."""
    pairs = [(s, sg) for s in SUBJECTS for sg in SUBGROUPS]
    _schedule_clear(context, update.effective_chat.id, pairs)
    await update.message.reply_text(
        "⏳ *Все очереди будут очищены в течение 5 минут.*\n\n"
        "Время выбрано случайно — никто не знает точный момент.\n"
        "Каждый участник получит личное уведомление.",
        parse_mode="Markdown",
    )


@_admin_only
async def cmd_clear_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/clear_sub <1|2> — очистить все очереди конкретной подгруппы."""
    if not context.args:
        await update.message.reply_text(
            "Использование: `/clear_sub <1|2>`", parse_mode="Markdown"
        )
        return

    subgroup = context.args[0]
    if subgroup not in SUBGROUPS:
        await update.message.reply_text("❌ Подгруппа должна быть 1 или 2.")
        return

    pairs = [(s, subgroup) for s in SUBJECTS]
    _schedule_clear(context, update.effective_chat.id, pairs)
    await update.message.reply_text(
        f"⏳ *Очереди подгруппы {subgroup} будут очищены в течение 5 минут.*\n\n"
        f"Время выбрано случайно. Все участники получат уведомление.",
        parse_mode="Markdown",
    )


@_admin_only
async def cmd_clear_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /clear_subject <предмет>       — очистить обе подгруппы предмета
    /clear_subject <предмет> <1|2> — очистить конкретную подгруппу предмета
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

    if len(context.args) >= 2:
        subgroup = context.args[1]
        if subgroup not in SUBGROUPS:
            await update.message.reply_text("❌ Подгруппа должна быть 1 или 2.")
            return
        pairs = [(subject, subgroup)]
        scope = f"*{subject.upper()}* (подгруппа {subgroup})"
    else:
        pairs = [(subject, sg) for sg in SUBGROUPS]
        scope = f"*{subject.upper()}* (обе подгруппы)"

    _schedule_clear(context, update.effective_chat.id, pairs)
    await update.message.reply_text(
        f"⏳ Очередь {scope} будет очищена *в течение 5 минут*.\n\n"
        f"Время выбрано случайно. Все участники получат уведомление.",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────

def main() -> None:
    # При старте копируем students.json из репо в Volume (только на Railway)
    if DATA_DIR != ".":
        import shutil
        if not os.path.exists(STUDENTS_FILE) and os.path.exists("students.json"):
            shutil.copy("students.json", STUDENTS_FILE)
            logger.info(f"students.json скопирован в {STUDENTS_FILE}")
        elif not os.path.exists(STUDENTS_FILE):
            logger.error("КРИТИЧНО: students.json не найден ни в репо, ни в Volume!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Общие команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))

    # Новые пользовательские команды
    app.add_handler(CommandHandler("missed", cmd_missed))
    app.add_handler(CommandHandler("leave", cmd_leave))

    # Принудительная запись
    app.add_handler(CommandHandler("add_oaip", cmd_add_oaip))
    app.add_handler(CommandHandler("add_chm", cmd_add_chm))
    app.add_handler(CommandHandler("add_aisd", cmd_add_aisd))

    # Команды администратора
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
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
