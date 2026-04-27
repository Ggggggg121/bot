"""
Бот управления очередями на сдачу лабораторных работ.
v2.0 — приоритеты, антиспам, честная очистка, extra-очередь, самовыход, точечные команды.
"""

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

SUBJECTS = ["оаип", "чм", "аисд"]
SUBGROUPS = ["1", "2"]
EXTRA_QUEUE_MAX = 10

# Файлы хранения
STUDENTS_FILE = "students.json"
QUEUES_FILE   = "queues.json"
PRIORITY_FILE = "priority_pool.json"
EXTRA_FILE    = "extra_queue.json"
SETTINGS_FILE = "settings.json"

# Ключевые слова для определения предмета
SUBJECT_KEYWORDS = {
    "оаип": ["оаип"],
    "чм":   ["чм", "числовые методы", "числовых методов"],
    "аисд": ["аисд"],
}

# Фразы-триггеры для автозаписи
QUEUE_TRIGGERS = [
    "занимаю место на",
    "займу место на",
    "записываюсь на",
    "запишите меня на",
]

# Антиспам: уровни мута в секундах
MUTE_LEVELS = [10, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300]
SPAM_WINDOW  = 10   # окно в секундах
SPAM_LIMIT   = 5    # сообщений за окно

# Честная очистка: задержка в секундах (от 0 до 5 минут)
FAIR_CLEAR_DELAY = 300

# ══════════════════════════════════════════════════════════════════════
# IN-MEMORY СОСТОЯНИЕ (сбрасывается при рестарте)
# ══════════════════════════════════════════════════════════════════════

spam_tracker: dict[int, dict] = {}
pending_clears: dict[str, asyncio.Task] = {}

# ══════════════════════════════════════════════════════════════════════
# ХРАНИЛИЩЕ
# ══════════════════════════════════════════════════════════════════════

def _load(path: str, default_factory):
    if not os.path.exists(path):
        return default_factory()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_students() -> dict: return _load(STUDENTS_FILE, dict)
def load_queues() -> dict: return _load(QUEUES_FILE, lambda: {s: {"1": [], "2": []} for s in SUBJECTS})
def save_queues(d): _save(QUEUES_FILE, d)
def load_priority() -> dict: return _load(PRIORITY_FILE, lambda: {s: {"1": [], "2": []} for s in SUBJECTS})
def save_priority(d): _save(PRIORITY_FILE, d)
def load_extra() -> list: return _load(EXTRA_FILE, list)
def save_extra(d): _save(EXTRA_FILE, d)
def load_settings() -> dict: return _load(SETTINGS_FILE, lambda: {"group_chats": []})
def save_settings(d): _save(SETTINGS_FILE, d)

def register_chat(chat_id: int) -> None:
    s = load_settings()
    if chat_id not in s["group_chats"]:
        s["group_chats"].append(chat_id)
        save_settings(s)

# ══════════════════════════════════════════════════════════════════════
# АНТИСПАМ
# ══════════════════════════════════════════════════════════════════════

def check_spam(user_id: int) -> tuple[bool, int]:
    now = time.monotonic()
    info = spam_tracker.setdefault(user_id, {"timestamps": [], "mute_level": 0, "muted_until": 0.0})

    if info["muted_until"] > now:
        return True, int(info["muted_until"] - now)

    info["timestamps"] = [t for t in info["timestamps"] if now - t < SPAM_WINDOW]
    info["timestamps"].append(now)

    if len(info["timestamps"]) > SPAM_LIMIT:
        level = min(info["mute_level"], len(MUTE_LEVELS) - 1)
        duration = MUTE_LEVELS[level]
        info["muted_until"] = now + duration
        info["mute_level"]  = min(info["mute_level"] + 1, len(MUTE_LEVELS) - 1)
        info["timestamps"]  = []
        return True, duration

    return False, 0

# ══════════════════════════════════════════════════════════════════════
# ЛОГИКА ОЧЕРЕДИ
# ══════════════════════════════════════════════════════════════════════

def interleave(queue: list) -> list:
    priority = sorted([e for e in queue if e.get("priority", 0) > 0], key=lambda x: -x["priority"])
    regular = [e for e in queue if e.get("priority", 0) == 0]
    result, pi, ri = [], 0, 0
    while pi < len(priority) or ri < len(regular):
        if pi < len(priority):
            result.append(priority[pi]); pi += 1
        if ri < len(regular):
            result.append(regular[ri]); ri += 1
    return result

def detect_subject(text: str) -> Optional[str]:
    t = text.lower()
    for subj, keywords in SUBJECT_KEYWORDS.items():
        if any(kw in t for kw in keywords): return subj
    return None

def is_trigger(text: str) -> bool:
    return any(tr in text.lower() for tr in QUEUE_TRIGGERS)

def _fmt_entry(i: int, entry: dict) -> str:
    p = entry.get("priority", 0)
    star = f" ⭐×{p}" if p > 0 else ""
    return f"    {i}. {entry['name']}{star}  ·  {entry['time']}"

def format_all_queues(queues: dict, extra: list) -> str:
    lines = ["📋 *Текущие очереди на сдачу лаб:*\n"]
    for subj in SUBJECTS:
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📚 *{subj.upper()}*")
        for sg in SUBGROUPS:
            ordered = interleave(queues[subj][sg])
            lines.append(f"  👥 Подгруппа {sg}:")
            if not ordered:
                lines.append("    — пусто")
            else:
                for i, e in enumerate(ordered, 1):
                    lines.append(_fmt_entry(i, e))
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🎓 *Extra-очередь* ({len(extra)}/{EXTRA_QUEUE_MAX}):")
    if not extra:
        lines.append("    — пусто")
    else:
        for i, e in enumerate(extra, 1):
            lines.append(_fmt_entry(i, e))

    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════

def main_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = []
    for subj in SUBJECTS:
        rows.append([
            InlineKeyboardButton(f"😞 Не успел: {subj.upper()}", callback_data=f"miss:{subj}"),
            InlineKeyboardButton(f"🚪 Выйти: {subj.upper()}", callback_data=f"leave:{subj}"),
        ])
    rows.append([
        InlineKeyboardButton("📝 Extra: записаться", callback_data="extra:join"),
        InlineKeyboardButton("🚪 Extra: выйти",      callback_data="extra:leave"),
    ])
    if is_admin:
        rows.append([InlineKeyboardButton("🔧 Панель управления", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🗑 Очистить ВСЁ", callback_data="clear:all:")]]
    for subj in SUBJECTS:
        rows.append([
            InlineKeyboardButton(f"🗑 {subj.upper()} (обе)", callback_data=f"clear:{subj}:"),
            InlineKeyboardButton(f"🗑 {subj.upper()} пг.1", callback_data=f"clear:{subj}:1"),
            InlineKeyboardButton(f"🗑 {subj.upper()} пг.2", callback_data=f"clear:{subj}:2"),
        ])
    rows.append([InlineKeyboardButton("🗑 Extra-очередь", callback_data="clear:extra:")])
    rows.append([
        InlineKeyboardButton("⛔ Отмена запланированных", callback_data="admin:cancel_clears"),
        InlineKeyboardButton("❌ Закрыть", callback_data="admin:close"),
    ])
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════════════════════════
# УВЕДОМЛЕНИЯ
# ══════════════════════════════════════════════════════════════════════

async def broadcast(app: Application, text: str) -> None:
    for chat_id in load_settings().get("group_chats", []):
        try:
            await app.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            logger.warning("broadcast failed for %s: %s", chat_id, exc)

# ══════════════════════════════════════════════════════════════════════
# ЧЕСТНАЯ ОЧИСТКА
# ══════════════════════════════════════════════════════════════════════

def _clear_key(subject: Optional[str], subgroup: Optional[str]) -> str:
    return f"{subject or 'all'}:{subgroup or ''}"

async def _execute_clear(app: Application, subject: Optional[str], subgroup: Optional[str]) -> None:
    key = _clear_key(subject, subgroup)
    queues = load_queues()
    priority = load_priority()
    notified_names: list[str] = []

    def _process(s: str, sg: str) -> None:
        q = queues[s][sg]
        if not q: return
        notified_names.extend(e["name"] for e in q)

        tail = q[-3:] if len(q) >= 3 else q[:]
        pool = priority[s][sg]
        pool_index = {pe["user_id"]: idx for idx, pe in enumerate(pool)}

        for entry in tail:
            uid = entry["user_id"]
            if uid in pool_index:
                pool[pool_index[uid]]["priority_level"] += 1
            else:
                pool.append({"user_id": uid, "name": entry["name"], "priority_level": 1})

        new_queue = [
            {"user_id": pe["user_id"], "name": pe["name"], "time": datetime.now().strftime("%H:%M  %d.%m.%Y"), "priority": pe["priority_level"]}
            for pe in sorted(pool, key=lambda x: -x["priority_level"])
        ]
        queues[s][sg] = new_queue
        priority[s][sg] = []

    if subject == "extra":
        extra = load_extra()
        notified_names.extend(e["name"] for e in extra)
        save_extra([])
        pending_clears.pop(key, None)
        await broadcast(app, "🔔 *Extra-очередь* очищена.")
        return

    # Логика определения что очищать
    if subject and subgroup:
        _process(subject, subgroup)
        label = f"*{subject.upper()}* (подгруппа {subgroup})"
    elif subject:
        for sg in SUBGROUPS: _process(subject, sg)
        label = f"*{subject.upper()}* (обе подгруппы)"
    elif subgroup:
        for s in SUBJECTS: _process(s, subgroup)
        label = f"*все предметы* (подгруппа {subgroup})"
    else:
        for s in SUBJECTS:
            for sg in SUBGROUPS: _process(s, sg)
        label = "*все очереди*"

    save_queues(queues)
    save_priority(priority)
    pending_clears.pop(key, None)

    unique_names = list(dict.fromkeys(notified_names))
    names_str = "\n".join(f"• {n}" for n in unique_names) if unique_names else "— очереди были пусты"

    await broadcast(
        app,
        f"🔔 Очищены {label}.\n\n"
        f"Люди с приоритетом автоматически поставлены в начало новой очереди.\n\n"
        f"*Были в очереди:*\n{names_str}",
    )

async def schedule_clear(app: Application, subject: Optional[str], subgroup: Optional[str]) -> int:
    key = _clear_key(subject, subgroup)
    if key in pending_clears and not pending_clears[key].done(): return -1
    delay = random.randint(1, FAIR_CLEAR_DELAY)
    async def _run():
        await asyncio.sleep(delay)
        await _execute_clear(app, subject, subgroup)
    pending_clears[key] = asyncio.create_task(_run())
    return delay

# ══════════════════════════════════════════════════════════════════════
# ЗАПИСЬ В ОЧЕРЕДЬ
# ══════════════════════════════════════════════════════════════════════

async def do_enqueue(update: Update, subject: str, user_id: int, student: dict) -> None:
    subgroup = str(student["subgroup"])
    name = f"{student['name']} {student['surname']}"

    queues = load_queues()
    queue = queues[subject][subgroup]

    if any(e["user_id"] == user_id for e in queue):
        await update.effective_message.reply_text(f"⚠️ *{name}*, ты уже в очереди на *{subject.upper()}* (пг. {subgroup}).", parse_mode="Markdown")
        return

    priority_data = load_priority()
    pool = priority_data[subject][subgroup]
    user_priority = 0
    priority_data[subject][subgroup] = []
    for pe in pool:
        if pe["user_id"] == user_id: user_priority = pe["priority_level"]
        else: priority_data[subject][subgroup].append(pe)
    save_priority(priority_data)

    entry = {"user_id": user_id, "name": name, "time": datetime.now().strftime("%H:%M  %d.%m.%Y"), "priority": user_priority}
    queues[subject][subgroup].append(entry)
    save_queues(queues)

    ordered = interleave(queues[subject][subgroup])
    pos = next((i + 1 for i, e in enumerate(ordered) if e["user_id"] == user_id), len(ordered))
    prio_str = f"  |  ⭐ Приоритет: {user_priority}" if user_priority > 0 else ""

    await update.effective_message.reply_text(
        f"✅ *{name}* записан(а) на *{subject.upper()}*\n"
        f"Пг. {subgroup}  |  Позиция: *{pos}*{prio_str}  |  {entry['time']}",
        parse_mode="Markdown",
    )

# ══════════════════════════════════════════════════════════════════════
# ОБЩИЕ КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "👋 *Бот управления очередями на сдачу лаб*\n\n"
        "📌 *Записаться* — напиши в группе:\n"
        "`занимаю место на оаип` (или чм / аисд)\n\n"
        "📋 /queue — очереди + кнопки управления\n\n"
        "🔧 *Команды администратора:*\n"
        "/remove `<предмет> <пг> <user_id>` — убрать из конкретной очереди\n"
        "/clear\\_user `<user_id>` — удалить человека ИЗ ВСЕХ очередей\n"
        "/clear\\_sub `<1|2>` — очистить подгруппу (с задержкой)\n"
        "/clear\\_subject `<предмет> [пг]` — очистить предмет (с задержкой)\n"
        "/cancel\\_clears — отменить запланированные очистки",
        parse_mode="Markdown",
    )

async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.chat.type in ("group", "supergroup"): register_chat(update.message.chat_id)
    await update.effective_message.reply_text(
        format_all_queues(load_queues(), load_extra()),
        parse_mode="Markdown",
        reply_markup=main_keyboard(update.effective_user.id == ADMIN_ID),
    )

# ══════════════════════════════════════════════════════════════════════
# ПАРСИНГ СООБЩЕНИЙ ГРУППЫ
# ══════════════════════════════════════════════════════════════════════

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text: return
    if update.message.chat.type in ("group", "supergroup"): register_chat(update.message.chat_id)

    text = update.message.text
    if not is_trigger(text): return

    user_id = update.message.from_user.id
    muted, secs = check_spam(user_id)
    if muted:
        await update.message.reply_text(f"🚫 Слишком много запросов. Подожди *{secs} сек.*", parse_mode="Markdown")
        return

    subject = detect_subject(text)
    if not subject:
        await update.message.reply_text("❓ Не могу определить предмет. Используй: /add\\_oaip | /add\\_chm | /add\\_aisd", parse_mode="Markdown")
        return

    student = load_students().get(str(user_id))
    if not student:
        await update.message.reply_text("❌ Твой Telegram ID не найден в базе. Обратись к администратору.")
        return

    await do_enqueue(update, subject, user_id, student)

# ══════════════════════════════════════════════════════════════════════
# КОМАНДЫ АДМИНИСТРАТОРА (ОЧИСТКИ)
# ══════════════════════════════════════════════════════════════════════

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.effective_message.reply_text("❌ Недостаточно прав.")
            return
        await func(update, context)
    return wrapper

@admin_only
async def _force_add(update: Update, context: ContextTypes.DEFAULT_TYPE, subject: str) -> None:
    target_id = update.effective_user.id
    if context.args:
        try: target_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("❌ Неверный user_id.")
            return

    student = load_students().get(str(target_id))
    if not student:
        await update.effective_message.reply_text(f"❌ ID `{target_id}` не найден в базе.", parse_mode="Markdown")
        return
    await do_enqueue(update, subject, target_id, student)

async def cmd_add_oaip(u, c): await _force_add(u, c, "оаип")
async def cmd_add_chm(u, c):  await _force_add(u, c, "чм")
async def cmd_add_aisd(u, c): await _force_add(u, c, "аисд")

@admin_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить из конкретной очереди: /remove <предмет> <пг> <user_id>"""
    if len(context.args) < 3:
        await update.effective_message.reply_text("Использование: `/remove <предмет> <пг> <user_id>`", parse_mode="Markdown")
        return
    subject, subgroup = context.args[0].lower(), context.args[1]
    if subject not in SUBJECTS or subgroup not in SUBGROUPS:
        await update.effective_message.reply_text("❌ Ошибка в предмете или подгруппе.")
        return
    try: target_id = int(context.args[2])
    except ValueError:
        await update.effective_message.reply_text("❌ user_id должен быть числом.")
        return

    queues = load_queues()
    old_len = len(queues[subject][subgroup])
    queues[subject][subgroup] = [e for e in queues[subject][subgroup] if e["user_id"] != target_id]

    if len(queues[subject][subgroup]) == old_len:
        await update.effective_message.reply_text("❌ Пользователь не найден в этой очереди.")
        return

    save_queues(queues)
    await update.effective_message.reply_text(f"✅ `{target_id}` удалён из *{subject.upper()}* (пг. {subgroup}).", parse_mode="Markdown")

@admin_only
async def cmd_clear_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Полностью стирает пользователя изо всех очередей."""
    if not context.args:
        await update.effective_message.reply_text("Использование: `/clear_user <user_id>`", parse_mode="Markdown")
        return
    try: target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ user_id должен быть числом.")
        return

    queues = load_queues()
    extra = load_extra()
    removed = False

    for s in SUBJECTS:
        for sg in SUBGROUPS:
            old_l = len(queues[s][sg])
            queues[s][sg] = [e for e in queues[s][sg] if e["user_id"] != target_id]
            if len(queues[s][sg]) < old_l: removed = True

    old_ex = len(extra)
    extra = [e for e in extra if e["user_id"] != target_id]
    if len(extra) < old_ex: removed = True

    if removed:
        save_queues(queues)
        save_extra(extra)
        await update.effective_message.reply_text(f"✅ Пользователь `{target_id}` выгнан **изо всех очередей**.", parse_mode="Markdown")
    else:
        await update.effective_message.reply_text("ℹ️ Этот пользователь нигде не записан.")

@admin_only
async def cmd_clear_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистить все предметы конкретной подгруппы (честная очистка)."""
    if not context.args or context.args[0] not in SUBGROUPS:
        await update.effective_message.reply_text("Использование: `/clear_sub <1|2>`", parse_mode="Markdown")
        return
    sg = context.args[0]
    delay = await schedule_clear(context.application, None, sg)
    await update.effective_message.reply_text(f"⏰ Честная очистка всех предметов **подгруппы {sg}** запланирована (через ~{delay} сек).", parse_mode="Markdown")

@admin_only
async def cmd_clear_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистить конкретный предмет (честная очистка)."""
    if not context.args:
        await update.effective_message.reply_text("Использование: `/clear_subject <предмет> [1|2]`", parse_mode="Markdown")
        return
    subj = context.args[0].lower()
    if subj not in SUBJECTS:
        await update.effective_message.reply_text(f"❌ Доступные предметы: {', '.join(SUBJECTS)}")
        return
    sg = context.args[1] if len(context.args) > 1 else None
    if sg and sg not in SUBGROUPS:
        await update.effective_message.reply_text("❌ Подгруппа должна быть 1 или 2.")
        return

    delay = await schedule_clear(context.application, subj, sg)
    await update.effective_message.reply_text(f"⏰ Честная очистка предмета **{subj.upper()}** запланирована (через ~{delay} сек).", parse_mode="Markdown")

@admin_only
async def cmd_cancel_clears(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cancelled = sum(1 for t in pending_clears.values() if not t.done() and not t.cancel())
    pending_clears.clear()
    await update.effective_message.reply_text(f"✅ Отменено запланированных очисток: *{cancelled}*.", parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК INLINE-КНОПОК
# ══════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, user_id, data = update.callback_query, update.callback_query.from_user.id, update.callback_query.data
    await query.answer()

    if data.startswith("miss:"):
        subject = data.split(":")[1]
        muted, secs = check_spam(user_id)
        if muted:
            await query.answer(f"🚫 Мут {secs} сек. за спам.", show_alert=True)
            return

        student = load_students().get(str(user_id))
        if not student:
            await query.answer("❌ Твой ID не найден в базе.", show_alert=True)
            return

        subgroup, name = str(student["subgroup"]), f"{student['name']} {student['surname']}"
        priority, pool = load_priority(), load_priority()[subject][str(student["subgroup"])]

        existing = next((pe for pe in pool if pe["user_id"] == user_id), None)
        if existing: existing["priority_level"], level = existing["priority_level"] + 1, existing["priority_level"] + 1
        else: pool.append({"user_id": user_id, "name": name, "priority_level": 1}); level = 1

        priority[subject][subgroup] = pool
        save_priority(priority)
        await query.answer(f"⭐ Приоритет на {subject.upper()} → уровень {level}.\nВ следующую сессию попадёшь в начало очереди.", show_alert=True)

    elif data.startswith("leave:"):
        subject = data.split(":")[1]
        student = load_students().get(str(user_id))
        if not student:
            await query.answer("❌ Твой ID не найден.", show_alert=True)
            return

        subgroup = str(student["subgroup"])
        queues, old_len = load_queues(), len(load_queues()[subject][subgroup])
        queues[subject][subgroup] = [e for e in queues[subject][subgroup] if e["user_id"] != user_id]

        if len(queues[subject][subgroup]) == old_len:
            await query.answer(f"ℹ️ Ты не записан(а) на {subject.upper()}.", show_alert=True)
            return

        save_queues(queues)
        await query.answer(f"✅ Ты вышел(а) из очереди {subject.upper()}.", show_alert=True)

    elif data == "extra:join":
        muted, secs = check_spam(user_id)
        if muted: return await query.answer(f"🚫 Мут {secs} сек.", show_alert=True)
        student = load_students().get(str(user_id))
        if not student: return await query.answer("❌ Твой ID не найден.", show_alert=True)

        extra = load_extra()
        if any(e["user_id"] == user_id for e in extra): return await query.answer("⚠️ Ты уже в Extra-очереди.", show_alert=True)
        if len(extra) >= EXTRA_QUEUE_MAX: return await query.answer(f"❌ Очередь заполнена ({EXTRA_QUEUE_MAX}).", show_alert=True)

        extra.append({"user_id": user_id, "name": f"{student['name']} {student['surname']}", "time": datetime.now().strftime("%H:%M  %d.%m.%Y"), "priority": 0})
        save_extra(extra)
        await query.answer(f"✅ Записан(а) в Extra! Позиция: {len(extra)}", show_alert=True)

    elif data == "extra:leave":
        extra = load_extra()
        new_extra = [e for e in extra if e["user_id"] != user_id]
        if len(new_extra) == len(extra): return await query.answer("ℹ️ Ты не в Extra-очереди.", show_alert=True)
        save_extra(new_extra)
        await query.answer("✅ Ты вышел(а) из Extra-очереди.", show_alert=True)

    elif data == "admin:panel":
        if user_id != ADMIN_ID: return await query.answer("❌ Нет прав.", show_alert=True)
        pending_info = f"⏳ Запланировано очисток: {sum(1 for t in pending_clears.values() if not t.done())}" if pending_clears else ""
        await query.message.reply_text(f"🔧 *Панель управления*\n{pending_info}", parse_mode="Markdown", reply_markup=admin_panel_keyboard())

    elif data == "admin:cancel_clears":
        if user_id != ADMIN_ID: return await query.answer("❌ Нет прав.", show_alert=True)
        cancelled = sum(1 for t in pending_clears.values() if not t.done() and not t.cancel())
        pending_clears.clear()
        await query.answer(f"✅ Отменено: {cancelled} задач.", show_alert=True)

    elif data == "admin:close":
        try: await query.message.delete()
        except Exception: pass

    elif data.startswith("clear:"):
        if user_id != ADMIN_ID: return await query.answer("❌ Нет прав.", show_alert=True)
        parts = data.split(":")
        subject, subgroup = (parts[1] if parts[1] != "all" else None), (parts[2] if len(parts) > 2 and parts[2] else None)

        delay = await schedule_clear(context.application, subject, subgroup)
        if delay == -1: await query.answer("⏳ Очистка уже запланирована!", show_alert=True)
        else:
            time_str = f"{delay // 60} мин {delay % 60} сек" if delay >= 60 else f"{delay} сек"
            await query.answer(f"⏰ Очистка запланирована через ~{time_str}.", show_alert=True)
            await query.message.reply_text(f"⏰ Очистка запланирована (в течение {FAIR_CLEAR_DELAY // 60} минут).", parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("queue", cmd_queue))

    app.add_handler(CommandHandler("add_oaip", cmd_add_oaip))
    app.add_handler(CommandHandler("add_chm",  cmd_add_chm))
    app.add_handler(CommandHandler("add_aisd", cmd_add_aisd))

    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("clear_user", cmd_clear_user))
    app.add_handler(CommandHandler("clear_sub", cmd_clear_sub))
    app.add_handler(CommandHandler("clear_subject", cmd_clear_subject))
    app.add_handler(CommandHandler("cancel_clears", cmd_cancel_clears))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()