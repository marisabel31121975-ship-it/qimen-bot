# -*- coding: utf-8 -*-
import sqlite3
import threading
import re
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config

DB_PATH = "qimen.db"
db_lock = threading.Lock()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            context TEXT,
            tariff TEXT DEFAULT 'Не выбран',
            status TEXT NOT NULL DEFAULT 'awaiting_tariff',
            is_followup INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            answered_at TEXT,
            reminder_sent INTEGER NOT NULL DEFAULT 0
        );
        """)

def add_question(user_id, text, context, is_followup=0, parent_id=None):
    with db_lock, db() as con:
        cur = con.execute(
            "INSERT INTO questions(user_id, text, context, is_followup, parent_id, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, text, context or "", is_followup, parent_id,
             datetime.now().isoformat()))
        return cur.lastrowid

def get_question(qid):
    with db_lock, db() as con:
        return con.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()

def set_status(qid, status, tariff=None):
    with db_lock, db() as con:
        fields = "status=?"
        args = [status]
        if status == "in_queue":
            fields += ", paid_at=?"
            args.append(datetime.now().isoformat())
        if status == "answered":
            fields += ", answered_at=?"
            args.append(datetime.now().isoformat())
        if tariff:
            fields += ", tariff=?"
            args.append(tariff)
        args.append(qid)
        con.execute("UPDATE questions SET " + fields + " WHERE id=?", args)

def queue_position(qid):
    row = get_question(qid)
    if row["status"] == "in_work":
        return 0, "скоро"
    with db_lock, db() as con:
        ahead = con.execute(
            "SELECT COUNT(*) c FROM questions WHERE status='in_queue'"
            " AND is_followup=0 AND id < ?", (qid,)).fetchone()["c"]
    position = ahead + 1
    minutes = position * config.AVG_MINUTES_PER_QUESTION
    now = datetime.now()
    eta = now + timedelta(minutes=minutes)
    if eta.hour >= config.WORK_END or eta.hour < config.WORK_START:
        eta = now.replace(hour=config.WORK_START, minute=0, second=0)
        if now.hour >= config.WORK_START:
            eta += timedelta(days=1)
    return position, eta.strftime("%H:%M (%d.%m)")

def today_count():
    day = datetime.now().strftime("%Y-%m-%d")
    with db_lock, db() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM questions WHERE is_followup=0"
            " AND substr(created_at,1,10)=?", (day,)).fetchone()["c"]

def my_questions(user_id):
    with db_lock, db() as con:
        return con.execute(
            "SELECT * FROM questions WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_id,)).fetchall()

def delete_user_data(user_id):
    with db_lock, db() as con:
        con.execute("DELETE FROM questions WHERE user_id=?", (user_id,))

def due_for_reminder():
    edge = datetime.now() - timedelta(days=config.REMINDER_DAYS)
    with db_lock, db() as con:
        return con.execute(
            "SELECT * FROM questions WHERE status='answered' AND is_followup=0"
            " AND reminder_sent=0 AND answered_at < ? LIMIT 10",
            (edge.isoformat(),)).fetchall()

def mark_reminded(qid):
    with db_lock, db() as con:
        con.execute("UPDATE questions SET reminder_sent=1 WHERE id=?", (qid,))

# --- КЛАВИАТУРЫ ---
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Задать вопрос", callback_data="ask")],
        [InlineKeyboardButton(text="📁 Мои ситуации", callback_data="my"),
         InlineKeyboardButton(text="❓ Как это работает", callback_data="how")],
        [InlineKeyboardButton(text="🗑 Стереть историю", callback_data="delete")],
    ])

def admin_kb(qid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплата получена", callback_data="paid:" + str(qid)),
         InlineKeyboardButton(text="🔍 Взять в работу", callback_data="take:" + str(qid))],
    ])

def tariff_kb(qid):
    kb = []
    for key, data in config.TARIFFS.items():
        kb.append([InlineKeyboardButton(text=f"{data['name']} — {data['price']}", callback_data=f"tariff:{key}:{qid}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def delete_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, стереть всё", callback_data="delete_yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="menu")],
    ])

# --- СОСТОЯНИЯ ---
class Ask(StatesGroup):
    waiting_text = State()
    waiting_context = State()
    waiting_time = State()
    waiting_birthdate = State()

class Followup(StatesGroup):
    waiting_text = State()

async def send_long(bot: Bot, chat_id: int, text: str):
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i:i + 4000], parse_mode="Markdown")

def is_admin(message: Message) -> bool:
    return message.from_user and message.from_user.id == config.ADMIN_ID

def fmt(text_key, **kw):
    return config.TEXTS[text_key].format(**kw)

# --- ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ---
async def cmd_start(message: Message):
    await message.answer(fmt("welcome"), parse_mode="Markdown", reply_markup=main_kb())

async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(fmt("welcome"), parse_mode="Markdown", reply_markup=main_kb())
    await callback.answer()

async def cb_how(callback: CallbackQuery):
    await callback.message.answer(
        fmt("how", work_start=config.WORK_START, work_end=config.WORK_END, limit=config.MAX_QUESTIONS_PER_DAY),
        parse_mode="Markdown")
    await callback.answer()

async def cb_ask(callback: CallbackQuery, state: FSMContext):
    if today_count() >= config.MAX_QUESTIONS_PER_DAY:
        await callback.message.answer(fmt("limit_reached", limit=config.MAX_QUESTIONS_PER_DAY))
        await callback.answer()
        return
    await state.set_state(Ask.waiting_text)
    await callback.message.answer(fmt("ask_start"))
    await callback.answer()

async def ask_got_text(message: Message, state: FSMContext):
    if len(message.text or "") < 5:
        await message.answer("Напишите чуть подробнее — пару предложений о ситуации.")
        return
    await state.update_data(text=message.text.strip())
    await state.set_state(Ask.waiting_context)
    await message.answer(fmt("ask_context"))

async def ask_got_context(message: Message, state: FSMContext):
    await state.update_data(context=message.text.strip())
    await state.set_state(Ask.waiting_time)
    await message.answer(fmt("ask_time"))

async def ask_got_time(message: Message, state: FSMContext):
    await state.update_data(time=message.text.strip())
    await state.set_state(Ask.waiting_birthdate)
    await message.answer(fmt("ask_birthdate"))

async def ask_got_birthdate(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    birth_date = message.text.strip()
    
    full_context = f"Текущий город: {data['context']}\nВремя вопроса: {data['time']}\nДата/город рождения: {birth_date}"
    
    qid = add_question(message.from_user.id, data["text"], full_context, is_followup=0)
    await state.clear()
    
    await message.answer("Все данные приняты! Выберите формат разбора:", reply_markup=tariff_kb(qid))
    
    admin_text = (
        f"🆕 *Новый вопрос #{qid}* (ожидает выбора тарифа)\n"
        f"От: @{message.from_user.username or message.from_user.id}\n\n"
        f"❓ Вопрос: {data['text']}\n\n"
        f"{full_context}"
    )
    await bot.send_message(config.ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=admin_kb(qid))

async def cb_tariff(callback: CallbackQuery, bot: Bot):
    _, tariff_key, qid_str = callback.data.split(":")
    qid = int(qid_str)
    t_data = config.TARIFFS[tariff_key]
    
    set_status(qid, 'awaiting_payment', tariff=f"{t_data['name']} ({t_data['price']})")
    
    text = (f"Вы выбрали: *{t_data['name']}*\n_{t_data['desc']}_\nСтоимость: *{t_data['price']}*\n\n"
            f"{config.PAYMENT_DETAILS}\n\nПосле оплаты нажмите кнопку ниже.")
    
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data=f"confirm_paid:{qid}")]])
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    await callback.answer()

async def cb_confirm_paid(callback: CallbackQuery, bot: Bot):
    qid = int(callback.data.split(":")[1])
    row = get_question(qid)
    set_status(qid, 'awaiting_admin_confirm')
    
    await callback.message.edit_text("Спасибо! Я проверю поступление средств и поставлю ваш вопрос в очередь. Обычно это занимает пару минут.")
    
    admin_msg = (
        f"💰 Пользователь нажал 'Я оплатил' по вопросу #{qid}.\n"
        f"Тариф: {row['tariff']}\n\n"
        f"Проверьте перевод и нажмите кнопку ниже.\n\n"
        f"👇 *Как ответить:* Когда решите ответить, просто зажмите пальцем это сообщение (или следующее уведомление), выберите «Ответить» (Reply) и напишите текст разбора."
    )
    await bot.send_message(config.ADMIN_ID, admin_msg, parse_mode="Markdown", reply_markup=admin_kb(qid))
    await callback.answer()

async def cb_my(callback: CallbackQuery):
    rows = my_questions(callback.from_user.id)
    if not rows:
        await callback.message.answer("Пока вопросов нет. «Мои ситуации» — ваш архив разборов.")
        await callback.answer()
        return
    status_map = {"awaiting_tariff": "⏳ ждёт выбора тарифа", "awaiting_payment": "⏳ ждёт оплаты", "awaiting_admin_confirm": "⏳ проверка оплаты", "in_queue": "📋 в очереди", "in_work": "🔍 в работе", "answered": "✅ отвечен"}
    lines = ["*Ваши ситуации:*\n"]
    for r in rows:
        snippet = r["text"][:40].replace("*", "")
        prefix = "↳ уточнение к " if r["is_followup"] else ""
        lines.append(f"#{r['id']} {prefix}{status_map.get(r['status'], r['status'])} — {snippet}…")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")
    await callback.answer()

async def cb_delete(callback: CallbackQuery):
    await callback.message.answer("Удалить *все* ваши вопросы и ответы из базы бота? Действие необратимо.", parse_mode="Markdown", reply_markup=delete_confirm_kb())
    await callback.answer()

async def cb_delete_yes(callback: CallbackQuery):
    delete_user_data(callback.from_user.id)
    await callback.message.answer(fmt("deleted"), reply_markup=main_kb())
    await callback.answer()

async def cb_followup(callback: CallbackQuery, state: FSMContext):
    parent_id = int(callback.data.split(":")[1])
    parent = get_question(parent_id)
    if not parent or parent["status"] != "answered":
        await callback.answer("По этому вопросу уточнение недоступно.", show_alert=True)
        return
    if parent["answered_at"] and (datetime.now() - datetime.fromisoformat(parent["answered_at"])) > timedelta(hours=config.FOLLOWUP_HOURS):
        await callback.answer(f"Прошло больше {config.FOLLOWUP_HOURS} часов — уточнение больше не принимается, но можно задать новый.", show_alert=True)
        return
    with db_lock, db() as con:
        used = con.execute("SELECT COUNT(*) c FROM questions WHERE parent_id=? AND is_followup=1", (parent_id,)).fetchone()["c"]
    if used >= 1:
        await callback.answer("Уточнение по этому вопросу уже использовано.", show_alert=True)
        return
    await state.update_data(parent_id=parent_id)
    await state.set_state(Followup.waiting_text)
    await callback.message.answer("Напишите уточняющий вопрос по этой ситуации.")
    await callback.answer()

async def followup_got_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    parent_id = data["parent_id"]
    await state.clear()
    qid = add_question(message.from_user.id, message.text.strip(), "Уточнение", is_followup=1, parent_id=parent_id)
    with db_lock, db() as con:
        con.execute("UPDATE questions SET status='in_queue' WHERE id=?", (qid,))
    await message.answer(fmt("followup_taken", work_start=config.WORK_START, work_end=config.WORK_END))
    
    # Отправляем админу карточку уточнения
    admin_msg = f"💬 Уточнение #{qid} к вопросу #{parent_id} от @{message.from_user.username or message.from_user.id}\n\n{message.text}\n\n👇 *Чтобы ответить, зажмите это сообщение и выберите «Ответить» (Reply).*"
    await bot.send_message(config.ADMIN_ID, admin_msg, parse_mode="Markdown")

# --- ХЕНДЛЕРЫ АДМИНА ---
async def cb_admin_paid(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Нет прав.", show_alert=True)
        return
    qid = int(callback.data.split(":")[1])
    row = get_question(qid)
    if not row or row["status"] not in ['awaiting_payment', 'awaiting_admin_confirm']:
        await callback.answer("Уже обработано.")
        return
    set_status(qid, "in_queue")
    pos, eta = queue_position(qid)
    await bot.send_message(row["user_id"], fmt("in_queue", qid=qid, position=pos, eta=eta), parse_mode="Markdown")
    
    # Отправляем вам карточку вопроса для ответа
    msg_text = (
        f"✅ Оплата по вопросу #{qid} подтверждена. Вопрос в очереди.\n\n"
        f"❓ Вопрос: {row['text']}\n"
        f"📍 {row['context']}\n\n"
        f"👇 *Как ответить:* Зажмите это сообщение, выберите «Ответить» (Reply) и напишите текст разбора."
    )
    await bot.send_message(config.ADMIN_ID, msg_text, parse_mode="Markdown")
    await callback.answer("Оплата подтверждена, карточка вопроса отправлена вам в чат.")

async def cb_admin_take(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer("Нет прав.", show_alert=True)
        return
    qid = int(callback.data.split(":")[1])
    row = get_question(qid)
    set_status(qid, "in_work")
    await bot.send_message(row["user_id"], fmt("in_work", qid=qid))
    await callback.answer(f"Вопрос #{qid} в работе.")

# --- ОТВЕТЫ КЛИЕНТАМ (Команда и Reply) ---
async def process_answer(message: Message, qid: int, answer: str, bot: Bot):
    row = get_question(qid)
    if not row:
        await message.answer("Вопрос не найден.")
        return
    set_status(qid, "answered")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Задать уточнение (бесплатно)", callback_data=f"followup:{qid}")]])
    header = f"*Ответ на вопрос #{qid}*\n\n{answer}\n\n---\n"
    footer = f"Этот разбор сохранён у вас в «Мои ситуации». В течение {config.FOLLOWUP_HOURS} часов можно задать одно уточнение — бесплатно."
    
    await send_long(bot, row["user_id"], header + footer)
    await bot.send_message(row["user_id"], "Нажмите кнопку ниже, если хотите уточнить детали:", reply_markup=kb)
    await message.answer(f"✅ Ответ на #{qid} успешно доставлен клиенту.")

async def cmd_answer(message: Message, command: CommandObject, bot: Bot):
    if not is_admin(message): return
    if not command.args:
        await message.answer("Формат: /answer ID текст ответа")
        return
    try:
        qid_str, answer = command.args.split(" ", 1)
        qid = int(qid_str)
    except ValueError:
        await message.answer("Формат: /answer ID текст ответа")
        return
    await process_answer(message, qid, answer, bot)

async def handle_reply_answer(message: Message, bot: Bot):
    if not is_admin(message) or not message.reply_to_message: return
    
    match = re.search(r'#(\d+)', message.reply_to_message.text or "")
    if not match:
        await message.answer("Не могу найти номер вопроса в сообщении, на которое вы ответили. Убедитесь, что там есть #цифра.")
        return
    
    qid = int(match.group(1))
    await process_answer(message, qid, message.text, bot)

# Шпаргалка для админа
async def cmd_help(message: Message):
    if not is_admin(message): return
    help_text = (
        "👑 *Шпаргалка мастера:*\n\n"
        "1️⃣ *Как отвечать клиентам:*\n"
        "Просто зажмите пальцем сообщение с вопросом (где есть #номер) и выберите «Ответить» (Reply). Напишите текст разбора и отправьте. Бот сам отправит его клиенту!\n\n"
        "2️⃣ *Очередь:*\n"
        "Команда `/queue` — покажет все неотвеченные вопросы.\n\n"
        "3️⃣ *Статистика:*\n"
        "Команда `/stats` — покажет, сколько вопросов за сегодня.\n\n"
        "4️⃣ *Старый способ:*\n"
        "Команда `/answer ID текст` тоже работает."
    )
    await message.answer(help_text, parse_mode="Markdown")

async def cmd_queue(message: Message):
    if not is_admin(message): return
    with db_lock, db() as con:
        rows = con.execute(
            "SELECT id, user_id, substr(text,1,80) t, is_followup, parent_id, status"
            " FROM questions WHERE status IN ('awaiting_payment','awaiting_admin_confirm','in_queue','in_work')"
            " ORDER BY id").fetchall()
    if not rows:
        await message.answer("Очередь пуста.")
        return
    lines = []
    for r in rows:
        line = f"#{r['id']} [{r['status']}] от {r['user_id']}: {r['t']}"
        if r["is_followup"]:
            line += f" (уточнение к #{r['parent_id']})"
        lines.append(line)
    await message.answer("Очередь:\n" + "\n".join(lines))

async def cmd_stats(message: Message):
    if not is_admin(message): return
    day = datetime.now().strftime("%Y-%m-%d")
    with db_lock, db() as con:
        total_today = con.execute("SELECT COUNT(*) c FROM questions WHERE is_followup=0 AND substr(created_at,1,10)=?", (day,)).fetchone()["c"]
        waiting = con.execute("SELECT COUNT(*) c FROM questions WHERE status IN ('awaiting_payment','awaiting_admin_confirm','in_queue','in_work')").fetchone()["c"]
        answered = con.execute("SELECT COUNT(*) c FROM questions WHERE status='answered'").fetchone()["c"]
    await message.answer(f"Сегодня вопросов: {total_today} / {config.MAX_QUESTIONS_PER_DAY}\nВ работе и очереди: {waiting}\nВсего отвечено: {answered}")

# --- ЗАПУСК ---
async def send_reminders(bot: Bot):
    for row in due_for_reminder():
        snippet = row["text"][:80]
        try:
            await bot.send_message(row["user_id"], fmt("reminder", snippet=snippet), parse_mode="Markdown", reply_markup=main_kb())
            mark_reminded(row["id"])
        except Exception:
            pass

async def main():
    init_db()
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_answer, Command("answer"))
    dp.message.register(cmd_queue, Command("queue"))
    dp.message.register(cmd_stats, Command("stats"))

    # Регистрируем хэндлер для ответа через Reply (если это админ)
    dp.message.register(handle_reply_answer, F.reply_to_message & (F.from_user.id == config.ADMIN_ID))

    dp.callback_query.register(cb_menu, F.data == "menu")
    dp.callback_query.register(cb_how, F.data == "how")
    dp.callback_query.register(cb_ask, F.data == "ask")
    dp.callback_query.register(cb_my, F.data == "my")
    dp.callback_query.register(cb_delete, F.data == "delete")
    dp.callback_query.register(cb_delete_yes, F.data == "delete_yes")
    dp.callback_query.register(cb_tariff, F.data.startswith("tariff:"))
    dp.callback_query.register(cb_confirm_paid, F.data.startswith("confirm_paid:"))
    dp.callback_query.register(cb_admin_paid, F.data.startswith("paid:"))
    dp.callback_query.register(cb_admin_take, F.data.startswith("take:"))
    dp.callback_query.register(cb_followup, F.data.startswith("followup:"))

    dp.message.register(ask_got_text, Ask.waiting_text)
    dp.message.register(ask_got_context, Ask.waiting_context)
    dp.message.register(ask_got_time, Ask.waiting_time)
    dp.message.register(ask_got_birthdate, Ask.waiting_birthdate)
    dp.message.register(followup_got_text, Followup.waiting_text)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "interval", minutes=30, args=[bot])
    scheduler.start()

    print("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
