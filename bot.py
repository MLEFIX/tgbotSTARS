"""Telegram-бот: заработок звёзд за выполнение заданий (aiogram 3 + SQLite)."""
import asyncio
import logging
import math
import os
from html import escape

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

REF_REWARD = 1.0                      # звёзд за приглашённого
WITHDRAW_OPTIONS = [15, 25, 50, 100]  # варианты вывода

BTN_EARN = "⭐ Заработать"
BTN_WITHDRAW = "💸 Вывести"
BTN_REF = "👥 Рефералка"
BTN_PROFILE = "👤 Профиль"
BTN_BACK = "⬅️ Назад"
WITHDRAW_BTNS = {f"{n} ⭐": n for n in WITHDRAW_OPTIONS}

TYPES = {
    "channel": {"icon": "📢", "stat": "подписались"},
    "bot": {"icon": "🤖", "stat": "активировали бота"},
    "reaction": {"icon": "❤️", "stat": "поставили реакцию"},
}
INSTRUCTIONS = {
    "channel": "1️⃣ Подпишись на канал\n2️⃣ Нажми «✅ Проверить подписку»",
    "bot": "1️⃣ Перейди по ссылке и запусти бота\n2️⃣ Сделай скриншот\n"
           "3️⃣ Нажми «✅ Я выполнил» и отправь скриншот",
    "reaction": "1️⃣ Перейди к посту и поставь реакцию\n2️⃣ Сделай скриншот\n"
                "3️⃣ Нажми «✅ Я выполнил» и отправь скриншот",
}

router = Router()
ADMIN = F.from_user.id == ADMIN_ID
db: aiosqlite.Connection


class Submit(StatesGroup):
    photo = State()


class AdminWrite(StatesGroup):
    text = State()


# ───────────────────────── База данных ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT, full_name TEXT,
    balance REAL NOT NULL DEFAULT 0,
    referrer_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL, title TEXT NOT NULL,
    url TEXT, chat_id INTEGER, description TEXT,
    reward REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS completions(
    user_id INTEGER, task_id INTEGER, reward REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, task_id)
);
CREATE TABLE IF NOT EXISTS submissions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, task_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / rework / approved
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, amount REAL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / done / rejected
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


async def fetchone(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchone()


async def fetchall(sql, args=()):
    async with db.execute(sql, args) as cur:
        return await cur.fetchall()


async def execute(sql, args=()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


# ───────────────────────── Помощники ─────────────────────────
def fmt(x: float) -> str:
    """0.5 -> '0.5', 15.0 -> '15'"""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def parse_reward(s: str):
    try:
        v = float(s.replace(",", "."))
    except ValueError:
        return None
    return round(v, 2) if math.isfinite(v) and v > 0 else None


def normalize_url(s: str):
    if s.startswith("@"):
        return "https://t.me/" + s[1:]
    if s.startswith("t.me/"):
        return "https://" + s
    return s if s.startswith(("http://", "https://")) else None


async def upsert_user(u, referrer_id=None) -> bool:
    """Регистрирует/обновляет пользователя. True — если он новый."""
    cur = await db.execute(
        "INSERT OR IGNORE INTO users(user_id, username, full_name, referrer_id) VALUES(?,?,?,?)",
        (u.id, u.username, u.full_name, referrer_id))
    is_new = cur.rowcount == 1
    if not is_new:
        await db.execute("UPDATE users SET username=?, full_name=? WHERE user_id=?",
                         (u.username, u.full_name, u.id))
    await db.commit()
    return is_new


async def get_balance(uid: int) -> float:
    row = await fetchone("SELECT balance FROM users WHERE user_id=?", (uid,))
    return row["balance"] if row else 0.0


async def add_balance(uid: int, amount: float):
    await execute("UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?", (amount, uid))


async def user_tag(uid: int) -> str:
    row = await fetchone("SELECT username, full_name FROM users WHERE user_id=?", (uid,))
    name = escape((row["full_name"] if row else None) or str(uid))
    tag = f'<a href="tg://user?id={uid}">{name}</a>'
    if row and row["username"]:
        tag += f" (@{escape(row['username'])})"
    return tag


async def get_task(task_id: int, active_only=True):
    sql = "SELECT * FROM tasks WHERE id=?" + (" AND active=1" if active_only else "")
    return await fetchone(sql, (task_id,))


async def is_done(uid: int, task_id: int) -> bool:
    return bool(await fetchone("SELECT 1 FROM completions WHERE user_id=? AND task_id=?",
                               (uid, task_id)))


async def complete_task(uid: int, task_id: int, reward: float) -> bool:
    """Засчитывает задание и начисляет награду. False — если уже было засчитано."""
    cur = await db.execute(
        "INSERT OR IGNORE INTO completions(user_id, task_id, reward) VALUES(?,?,?)",
        (uid, task_id, reward))
    if cur.rowcount == 0:
        return False
    await db.execute("UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?", (reward, uid))
    await db.commit()
    return True


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=BTN_EARN), KeyboardButton(text=BTN_WITHDRAW)],
        [KeyboardButton(text=BTN_REF), KeyboardButton(text=BTN_PROFILE)],
    ])


def withdraw_kb() -> ReplyKeyboardMarkup:
    btns = [KeyboardButton(text=t) for t in WITHDRAW_BTNS]
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        btns[:2], btns[2:], [KeyboardButton(text=BTN_BACK)],
    ])


async def tasks_markup(uid: int):
    """Inline-список заданий, которые пользователь ещё не выполнил."""
    rows = await fetchall(
        "SELECT * FROM tasks t WHERE t.active=1 AND NOT EXISTS "
        "(SELECT 1 FROM completions c WHERE c.task_id=t.id AND c.user_id=?) ORDER BY t.id", (uid,))
    kb = [[InlineKeyboardButton(
        text=f"{TYPES[t['type']]['icon']} {t['title']} · +{fmt(t['reward'])} ⭐",
        callback_data=f"task:{t['id']}")] for t in rows]
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


async def refresh_list(call: CallbackQuery):
    kb = await tasks_markup(call.from_user.id)
    text = "Выбери задание:" if kb else "Новых заданий пока нет. Загляни позже ⏳"
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        pass


# ───────────────────────── Старт и меню ─────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    uid = message.from_user.id
    ref_id = int(command.args) if command.args and command.args.isdigit() else None
    if ref_id == uid or (ref_id and not await fetchone(
            "SELECT 1 FROM users WHERE user_id=?", (ref_id,))):
        ref_id = None

    is_new = await upsert_user(message.from_user, ref_id)
    if is_new and ref_id:
        await add_balance(ref_id, REF_REWARD)
        try:
            await bot.send_message(
                ref_id, f"🎉 По твоей ссылке пришёл новый пользователь! +{fmt(REF_REWARD)} ⭐")
        except TelegramAPIError:
            pass

    text = "Привет! 👋 Выполняй задания, зарабатывай звёзды ⭐ и выводи их."
    if uid == ADMIN_ID:
        text += "\n\nТы админ — список команд: /admin"
    await message.answer(text, reply_markup=main_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb())


@router.message(F.text == BTN_PROFILE)
async def profile(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    await upsert_user(u)
    nick = f"@{u.username}" if u.username else u.full_name
    await message.answer(
        f"👤 <b>Профиль</b>\n\nНикнейм: {escape(nick)}\nID: <code>{u.id}</code>\n"
        f"Баланс: <b>{fmt(await get_balance(u.id))} ⭐</b>")


@router.message(F.text == BTN_REF)
async def referral(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid = message.from_user.id
    await upsert_user(message.from_user)
    me = await bot.me()
    cnt = (await fetchone("SELECT COUNT(*) AS c FROM users WHERE referrer_id=?", (uid,)))["c"]
    await message.answer(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"За каждого приглашённого ты получаешь <b>{fmt(REF_REWARD)} ⭐</b>.\n\n"
        f"Твоя ссылка:\n<code>https://t.me/{me.username}?start={uid}</code>\n\n"
        f"Приглашено: <b>{cnt}</b>")


@router.message(F.text == BTN_EARN)
async def earn(message: Message, state: FSMContext):
    await state.clear()
    await upsert_user(message.from_user)
    kb = await tasks_markup(message.from_user.id)
    if not kb:
        await message.answer("Пока нет доступных заданий. Загляни позже ⏳")
        return
    await message.answer("Выбери задание:", reply_markup=kb)


# ───────────────────────── Вывод ─────────────────────────
@router.message(F.text == BTN_WITHDRAW)
async def withdraw_menu(message: Message, state: FSMContext):
    await state.clear()
    await upsert_user(message.from_user)
    bal = await get_balance(message.from_user.id)
    await message.answer(f"Твой баланс: <b>{fmt(bal)} ⭐</b>\nСколько звёзд вывести?",
                         reply_markup=withdraw_kb())


@router.message(F.text == BTN_BACK)
async def back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню", reply_markup=main_kb())


@router.message(F.text.in_(WITHDRAW_BTNS))
async def withdraw(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uid, amount = message.from_user.id, WITHDRAW_BTNS[message.text]
    await upsert_user(message.from_user)

    # Атомарно списываем звёзды, только если их хватает
    cur = await db.execute(
        "UPDATE users SET balance=ROUND(balance-?,2) WHERE user_id=? AND ROUND(balance,2)>=?",
        (amount, uid, amount))
    if cur.rowcount == 0:
        bal = await get_balance(uid)
        await message.answer(f"❌ Недостаточно звёзд. Нужно {amount} ⭐, у тебя {fmt(bal)} ⭐.")
        return
    wd = await db.execute("INSERT INTO withdrawals(user_id, amount) VALUES(?,?)", (uid, amount))
    await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Вывел", callback_data=f"wd_ok:{wd.lastrowid}"),
        InlineKeyboardButton(text="❌ Отклонить (вернуть)", callback_data=f"wd_no:{wd.lastrowid}"),
    ]])
    await bot.send_message(
        ADMIN_ID,
        f"💸 <b>Заявка на вывод #{wd.lastrowid}</b>\n\n"
        f"Пользователь: {await user_tag(uid)}\nID: <code>{uid}</code>\n"
        f"Сумма: <b>{amount} ⭐</b>", reply_markup=kb)
    await message.answer(
        f"✅ Заявка на вывод {amount} ⭐ создана. Звёзды списаны с баланса, "
        f"ожидай — как только их отправят, придёт уведомление.", reply_markup=main_kb())


@router.callback_query(F.data.startswith("wd_ok:"), ADMIN)
async def wd_ok(call: CallbackQuery, bot: Bot):
    wd_id = int(call.data.split(":")[1])
    cur = await execute("UPDATE withdrawals SET status='done' WHERE id=? AND status='pending'", (wd_id,))
    if cur.rowcount == 0:
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    wd = await fetchone("SELECT * FROM withdrawals WHERE id=?", (wd_id,))
    try:
        await bot.send_message(wd["user_id"], f"✅ Твои {fmt(wd['amount'])} ⭐ были выведены! "
                                              f"Проверь свой аккаунт.")
    except TelegramAPIError:
        pass
    await call.message.edit_text(call.message.html_text + "\n\n✅ <b>Выведено</b>", reply_markup=None)
    await call.answer()


@router.callback_query(F.data.startswith("wd_no:"), ADMIN)
async def wd_no(call: CallbackQuery, bot: Bot):
    wd_id = int(call.data.split(":")[1])
    cur = await execute("UPDATE withdrawals SET status='rejected' WHERE id=? AND status='pending'", (wd_id,))
    if cur.rowcount == 0:
        await call.answer("Заявка уже обработана", show_alert=True)
        return
    wd = await fetchone("SELECT * FROM withdrawals WHERE id=?", (wd_id,))
    await add_balance(wd["user_id"], wd["amount"])
    try:
        await bot.send_message(wd["user_id"], f"❌ Заявка на вывод отклонена, "
                                              f"{fmt(wd['amount'])} ⭐ возвращены на баланс.")
    except TelegramAPIError:
        pass
    await call.message.edit_text(call.message.html_text + "\n\n❌ <b>Отклонено, звёзды возвращены</b>",
                                 reply_markup=None)
    await call.answer()


# ───────────────────────── Задания (пользователь) ─────────────────────────
@router.callback_query(F.data == "tasks")
async def tasks_back(call: CallbackQuery):
    await refresh_list(call)
    await call.answer()


@router.callback_query(F.data.startswith("task:"))
async def show_task(call: CallbackQuery):
    task = await get_task(int(call.data.split(":")[1]))
    if not task:
        await call.answer("Это задание больше недоступно", show_alert=True)
        await refresh_list(call)
        return
    if await is_done(call.from_user.id, task["id"]):
        await call.answer("Ты уже выполнил это задание ✅", show_alert=True)
        await refresh_list(call)
        return

    text = (f"{TYPES[task['type']]['icon']} <b>{escape(task['title'])}</b>\n"
            f"Награда: <b>{fmt(task['reward'])} ⭐</b>\n\n{INSTRUCTIONS[task['type']]}")
    if task["description"]:
        text += f"\n\n📝 {escape(task['description'])}"

    rows = []
    if task["url"]:
        rows.append([InlineKeyboardButton(text="🔗 Перейти", url=task["url"])])
    if task["type"] == "channel":
        rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data=f"check:{task['id']}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Я выполнил", callback_data=f"done:{task['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ К заданиям", callback_data="tasks")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                                 disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("check:"))
async def check_sub(call: CallbackQuery, bot: Bot):
    task = await get_task(int(call.data.split(":")[1]))
    uid = call.from_user.id
    if not task:
        await call.answer("Это задание больше недоступно", show_alert=True)
        return
    if await is_done(uid, task["id"]):
        await call.answer("Ты уже получил награду за это задание ✅", show_alert=True)
        return
    try:
        member = await bot.get_chat_member(task["chat_id"], uid)
    except TelegramAPIError as e:
        logging.warning("Не удалось проверить подписку: %s", e)
        await call.answer("Не удалось проверить подписку, попробуй позже.", show_alert=True)
        return
    subscribed = member.status in ("member", "administrator", "creator") or (
        member.status == "restricted" and getattr(member, "is_member", False))
    if not subscribed:
        await call.answer("Ты ещё не подписан на канал ❌", show_alert=True)
        return
    if await complete_task(uid, task["id"], task["reward"]):
        await call.answer(f"✅ Подписка подтверждена! +{fmt(task['reward'])} ⭐", show_alert=True)
    else:
        await call.answer("Уже засчитано", show_alert=True)
    await refresh_list(call)


@router.callback_query(F.data.startswith("done:"))
async def start_submit(call: CallbackQuery, state: FSMContext):
    task = await get_task(int(call.data.split(":")[1]))
    uid = call.from_user.id
    if not task:
        await call.answer("Это задание больше недоступно", show_alert=True)
        return
    if await is_done(uid, task["id"]):
        await call.answer("Ты уже выполнил это задание ✅", show_alert=True)
        return
    if await fetchone("SELECT 1 FROM submissions WHERE user_id=? AND task_id=? AND status='pending'",
                      (uid, task["id"])):
        await call.answer("Твоя заявка уже на проверке ⏳", show_alert=True)
        return
    await state.set_state(Submit.photo)
    await state.update_data(task_id=task["id"])
    await call.message.answer("📸 Отправь скриншот выполнения задания (фото).\nОтмена — /cancel")
    await call.answer()


# ───────────────────────── Админ-команды ─────────────────────────
ADMIN_HELP = (
    "🛠 <b>Команды админа</b>\n\n"
    "<b>Добавить канал</b> (бот должен быть админом канала):\n"
    "<code>/add_channel @channel 0.5</code>\n"
    "Для приватного канала: <code>/add_channel -1001234567890 0.5 https://t.me/+invite</code>\n\n"
    "<b>Добавить бота:</b>\n"
    "<code>/add_bot 1 https://t.me/some_bot Запусти бота и нажми /start</code>\n\n"
    "<b>Добавить реакцию на пост:</b>\n"
    "<code>/add_reaction 0.3 https://t.me/channel/123 Поставь любую реакцию</code>\n\n"
    "Количество звёзд можно писать дробным (0.5, 1.25 …).\n\n"
    "/stats — статистика\n"
    "/tasks — список заданий и удаление\n"
)


@router.message(Command("admin"), ADMIN)
async def admin_help(message: Message):
    await message.answer(ADMIN_HELP)


@router.message(Command("add_channel"), ADMIN)
async def add_channel(message: Message, command: CommandObject, bot: Bot):
    args = (command.args or "").split()
    reward = parse_reward(args[1]) if len(args) >= 2 else None
    if reward is None:
        await message.answer("Формат: <code>/add_channel @channel 0.5</code>")
        return

    ref = args[0]
    if ref.startswith("https://t.me/"):
        ref = "@" + ref.rsplit("/", 1)[-1]
    if ref.lstrip("-").isdigit():
        ref = int(ref)
    elif not ref.startswith("@"):
        ref = "@" + ref
    try:
        chat = await bot.get_chat(ref)
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as e:
        await message.answer(f"❌ Не удалось получить канал: {escape(str(e))}")
        return
    if member.status not in ("administrator", "creator"):
        await message.answer("❌ Сначала добавь бота в канал как администратора.")
        return

    link = args[2] if len(args) > 2 else (f"https://t.me/{chat.username}" if chat.username else None)
    if not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except TelegramAPIError:
            link = None
    await execute("INSERT INTO tasks(type, title, url, chat_id, reward) VALUES('channel',?,?,?,?)",
                  (chat.title or str(chat.id), link, chat.id, reward))
    await message.answer(f"✅ Задание добавлено: подписка на «{escape(chat.title or '')}», "
                         f"награда {fmt(reward)} ⭐")


async def add_manual_task(message: Message, command: CommandObject, ttype: str, example: str):
    parts = (command.args or "").split(maxsplit=2)
    reward = parse_reward(parts[0]) if parts else None
    url = normalize_url(parts[1]) if len(parts) >= 2 else None
    if reward is None or url is None:
        await message.answer(f"Формат: <code>{example}</code>")
        return
    desc = parts[2] if len(parts) > 2 else None
    if ttype == "bot":
        title = "Запустить @" + url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    else:
        title = "Реакция на пост"
    await execute("INSERT INTO tasks(type, title, url, description, reward) VALUES(?,?,?,?,?)",
                  (ttype, title, url, desc, reward))
    await message.answer(f"✅ Задание добавлено: {escape(title)}, награда {fmt(reward)} ⭐")


@router.message(Command("add_bot"), ADMIN)
async def add_bot(message: Message, command: CommandObject):
    await add_manual_task(message, command, "bot",
                          "/add_bot 1 https://t.me/some_bot Запусти бота и нажми /start")


@router.message(Command("add_reaction"), ADMIN)
async def add_reaction(message: Message, command: CommandObject):
    await add_manual_task(message, command, "reaction",
                          "/add_reaction 0.3 https://t.me/channel/123 Поставь любую реакцию")


@router.message(Command("stats"), ADMIN)
async def stats(message: Message):
    users = await fetchone("SELECT COUNT(*) AS c, COALESCE(SUM(balance),0) AS s FROM users")
    pend = (await fetchone("SELECT COUNT(*) AS c FROM withdrawals WHERE status='pending'"))["c"]
    paid = (await fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM withdrawals WHERE status='done'"))["s"]
    rows = await fetchall(
        "SELECT t.id, t.type, t.title, t.reward, COUNT(c.user_id) AS cnt FROM tasks t "
        "LEFT JOIN completions c ON c.task_id=t.id WHERE t.active=1 GROUP BY t.id ORDER BY t.id")
    lines = [f"#{t['id']} {TYPES[t['type']]['icon']} {escape(t['title'])} (+{fmt(t['reward'])} ⭐)\n"
             f"     {TYPES[t['type']]['stat']}: <b>{t['cnt']}</b>" for t in rows]
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{users['c']}</b>\n"
        f"Сумма балансов: <b>{fmt(users['s'])} ⭐</b>\n"
        f"Выведено всего: <b>{fmt(paid)} ⭐</b>\n"
        f"Заявок на вывод в ожидании: <b>{pend}</b>\n\n"
        "<b>Задания:</b>\n" + ("\n".join(lines) if lines else "заданий нет"))


async def admin_tasks_view():
    rows = await fetchall("SELECT * FROM tasks WHERE active=1 ORDER BY id")
    if not rows:
        return "Заданий нет.", None
    text = "<b>Активные задания</b> (нажми, чтобы удалить):\n\n" + "\n".join(
        f"#{t['id']} {TYPES[t['type']]['icon']} {escape(t['title'])} — {fmt(t['reward'])} ⭐" for t in rows)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🗑 #{t['id']} {t['title'][:30]}", callback_data=f"deltask:{t['id']}")] for t in rows])
    return text, kb


@router.message(Command("tasks"), ADMIN)
async def admin_tasks(message: Message):
    text, kb = await admin_tasks_view()
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("deltask:"), ADMIN)
async def del_task(call: CallbackQuery):
    await execute("UPDATE tasks SET active=0 WHERE id=?", (int(call.data.split(":")[1]),))
    text, kb = await admin_tasks_view()
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer("Задание удалено")


# ───────────────────────── Проверка скриншотов ─────────────────────────
@router.message(Submit.photo, F.photo)
async def got_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    task = await get_task(data["task_id"])
    if not task:
        await message.answer("Это задание больше недоступно.", reply_markup=main_kb())
        return
    uid = message.from_user.id
    cur = await execute("INSERT INTO submissions(user_id, task_id) VALUES(?,?)", (uid, task["id"]))
    sub_id = cur.lastrowid

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнил", callback_data=f"sub_ok:{sub_id}"),
        InlineKeyboardButton(text="✉️ Написать сообщение", callback_data=f"sub_msg:{sub_id}"),
    ]])
    await bot.send_photo(
        ADMIN_ID, message.photo[-1].file_id,
        caption=(f"📩 <b>Заявка #{sub_id}</b>\n"
                 f"Задание: {TYPES[task['type']]['icon']} {escape(task['title'])}\n"
                 f"Награда: {fmt(task['reward'])} ⭐\n"
                 f"Пользователь: {await user_tag(uid)}\nID: <code>{uid}</code>"),
        reply_markup=kb)
    await message.answer("✅ Скриншот отправлен на проверку. Ожидай ответа.", reply_markup=main_kb())


@router.message(Submit.photo)
async def not_photo(message: Message):
    await message.answer("Отправь именно фото (скриншот) или нажми /cancel.")


@router.callback_query(F.data.startswith("sub_ok:"), ADMIN)
async def sub_ok(call: CallbackQuery, bot: Bot):
    sub = await fetchone("SELECT * FROM submissions WHERE id=?", (int(call.data.split(":")[1]),))
    task = await get_task(sub["task_id"], active_only=False) if sub else None
    if not sub or not task:
        await call.answer("Заявка не найдена", show_alert=True)
        return
    if await complete_task(sub["user_id"], task["id"], task["reward"]):
        await execute("UPDATE submissions SET status='approved' WHERE user_id=? AND task_id=?",
                      (sub["user_id"], task["id"]))
        try:
            await bot.send_message(sub["user_id"],
                                   f"✅ Задание «{escape(task['title'])}» принято! "
                                   f"+{fmt(task['reward'])} ⭐")
        except TelegramAPIError:
            pass
        status = "✅ <b>Принято, звёзды начислены</b>"
    else:
        status = "ℹ️ <b>Пользователь уже получил награду за это задание</b>"
    await call.message.edit_caption(caption=call.message.html_text + f"\n\n{status}", reply_markup=None)
    await call.answer()


@router.callback_query(F.data.startswith("sub_msg:"), ADMIN)
async def sub_msg(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminWrite.text)
    await state.update_data(sub_id=int(call.data.split(":")[1]))
    await call.message.answer("✍️ Напиши сообщение для пользователя (одним сообщением).\nОтмена — /cancel")
    await call.answer()


@router.message(AdminWrite.text, ADMIN, F.text)
async def admin_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    sub = await fetchone("SELECT * FROM submissions WHERE id=?", (data["sub_id"],))
    task = await get_task(sub["task_id"], active_only=False)
    try:
        await bot.send_message(
            sub["user_id"],
            f"✉️ <b>Сообщение от администратора</b> по заданию «{escape(task['title'])}»:\n\n"
            f"{escape(message.text)}\n\n"
            f"Исправь и отправь скриншот заново: ⭐ Заработать → задание → «Я выполнил».")
    except TelegramAPIError:
        await message.answer("❌ Не удалось отправить (пользователь заблокировал бота?).")
        return
    await execute("UPDATE submissions SET status='rework' WHERE id=? AND status='pending'", (sub["id"],))
    await message.answer("✅ Сообщение отправлено пользователю.")


# ───────────────────────── Запуск ─────────────────────────
async def main():
    global db
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN or not ADMIN_ID:
        raise SystemExit("Заполни BOT_TOKEN и ADMIN_ID в файле .env")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
