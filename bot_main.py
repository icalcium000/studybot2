import os
import sqlite3
import hashlib
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_SECRET = os.getenv("API_SECRET", "")
PORT = int(os.getenv("PORT", 8080))
TZ_NAME = os.getenv("TZ", "Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "data/deadlines_jobs.sqlite")
APP_DB_PATH = os.getenv("APP_DB_PATH", "data/app_data.sqlite")
DEFAULT_GROUP_ID = os.getenv("GROUP_CHAT_ID", "")

if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана в .env!")

TZ = ZoneInfo(TZ_NAME)
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
os.makedirs(os.path.dirname(APP_DB_PATH) or ".", exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{DB_PATH}")}
scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=TZ)


# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(APP_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                task_name TEXT,
                course_name TEXT,
                raw_date TEXT,
                iso_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS members (
                chat_id TEXT,
                user_id INTEGER,
                full_name TEXT,
                username TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        if DEFAULT_GROUP_ID:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('group_chat_id', ?)",
                (str(DEFAULT_GROUP_ID),)
            )


def get_target_group_id() -> str | None:
    with sqlite3.connect(APP_DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'group_chat_id'").fetchone()
        return row[0] if row else None


def set_target_group_id(chat_id: str | int):
    with sqlite3.connect(APP_DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('group_chat_id', ?)",
            (str(chat_id),)
        )


def register_member(chat_id: str | int, user_id: int, full_name: str, username: str | None):
    with sqlite3.connect(APP_DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO members (chat_id, user_id, full_name, username)
            VALUES (?, ?, ?, ?)
        """, (str(chat_id), user_id, full_name, username or ""))


def get_chat_members(chat_id: str | int):
    with sqlite3.connect(APP_DB_PATH) as conn:
        return conn.execute(
            "SELECT user_id, full_name, username FROM members WHERE chat_id = ?",
            (str(chat_id),)
        ).fetchall()


# --- КЛАВИАТУРЫ ---
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Дедлайны на этой неделе"), KeyboardButton(text="📋 Все дедлайны")],
        [KeyboardButton(text="🔔 Активные таймеры")]
    ],
    resize_keyboard=True
)

checkin_inline_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🙋‍♂️ Отмечаюсь (пинговать меня при дедлайнах)", callback_data="checkin_me")]
    ]
)


# --- НАПОМИНАНИЯ И ТЕГ ВСЕХ УЧАСТНИКОВ ---
async def ping_all_members(chat_id: str | int):
    members = get_chat_members(chat_id)
    if not members:
        return

    mentions = []
    for user_id, full_name, username in members:
        if username:
            mentions.append(f"@{username}")
        else:
            safe_name = (full_name or "Студент").replace("<", "").replace(">", "")
            mentions.append(f'<a href="tg://user?id={user_id}">{safe_name}</a>')

    chunk_size = 5
    for i in range(0, len(mentions), chunk_size):
        chunk = " ".join(mentions[i:i + chunk_size])
        try:
            await bot.send_message(chat_id=chat_id, text=f"📢 Пинг: {chunk}")
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"Ошибка при теге участников: {e}")


async def send_reminder(task_name: str, course_name: str, raw_date: str, time_left_label: str):
    target_chat = get_target_group_id()
    if not target_chat:
        logging.warning("Группа для напоминаний не задана! Введите /setgroup в нужном чате.")
        return

    text = (
        f"🚨 <b>НАПОМИНАНИЕ О ДЕДЛАЙНЕ ({time_left_label})!</b>\n\n"
        f"📌 <b>{task_name}</b>\n"
        f"📚 {course_name}\n"
        f"🔥 Срок сдачи: <code>{raw_date}</code>"
    )
    try:
        await bot.send_message(chat_id=target_chat, text=text, reply_markup=checkin_inline_kb)
        await ping_all_members(target_chat)
    except Exception as e:
        logging.error(f"Ошибка отправки напоминания в чат {target_chat}: {e}")


# --- HTTP API ДЛЯ ПРИЁМА ЗАДАЧ ИЗ РАСШИРЕНИЯ ---
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    return response


async def health_check(request: web.Request):
    return web.json_response({"status": "ok", "jobs_count": len(scheduler.get_jobs())})


async def handle_new_tasks(request: web.Request):
    if API_SECRET and request.headers.get("X-API-Key") != API_SECRET:
        return web.json_response({"ok": False, "error": "Неверный пароль API_SECRET"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный JSON"}, status=400)

    tasks = data.get("tasks", [])
    req_chat_id = data.get("chat_id")

    if req_chat_id and not get_target_group_id():
        set_target_group_id(req_chat_id)

    target_chat = get_target_group_id() or req_chat_id
    if not tasks:
        return web.json_response({"ok": False, "error": "Пустой список задач"}, status=400)

    now = datetime.now(TZ)
    scheduled_info = []
    summary_lines = []

    with sqlite3.connect(APP_DB_PATH) as conn:
        for t in tasks:
            task_name = t.get("taskName", "Без названия")
            course_name = t.get("courseName", "ЦУ")
            raw_date = t.get("rawDateText", "")
            iso_date = t.get("isoDate")

            task_id = hashlib.md5(f"{task_name}|{course_name}|{iso_date or raw_date}".encode()).hexdigest()
            conn.execute("""
                INSERT OR REPLACE INTO tasks (id, task_name, course_name, raw_date, iso_date)
                VALUES (?, ?, ?, ?, ?)
            """, (task_id, task_name, course_name, raw_date, iso_date))

            summary_lines.append(f"📌 <b>{task_name}</b> ({course_name})\n⏳ <code>{raw_date}</code>")

            if iso_date:
                try:
                    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00")).astimezone(TZ)
                    intervals = [
                        ("остались сутки", dt - timedelta(hours=24)),
                        ("осталось 2 часа", dt - timedelta(hours=2)),
                    ]

                    for label, run_time in intervals:
                        if run_time > now:
                            job_id = hashlib.md5(f"{task_id}|{label}".encode()).hexdigest()
                            scheduler.add_job(
                                send_reminder,
                                trigger="date",
                                run_date=run_time,
                                kwargs={
                                    "task_name": task_name,
                                    "course_name": course_name,
                                    "raw_date": raw_date,
                                    "time_left_label": label
                                },
                                id=job_id,
                                replace_existing=True,
                                misfire_grace_time=3600
                            )
                            scheduled_info.append(f"• {task_name}: <i>{label}</i> ({run_time.strftime('%d.%m в %H:%M')})")
                except Exception as e:
                    logging.error(f"Ошибка разбора даты {iso_date}: {e}")

    msg = f"✅ <b>Выгружены новые задачи ЦУ ({len(tasks)} шт.):</b>\n\n" + "\n\n".join(summary_lines)
    if scheduled_info:
        msg += "\n\n🔔 <b>Запланированы напоминания:</b>\n" + "\n".join(scheduled_info)

    if target_chat:
        try:
            await bot.send_message(chat_id=target_chat, text=msg, reply_markup=checkin_inline_kb)
            await ping_all_members(target_chat)
        except Exception as e:
            logging.error(f"Ошибка отправки сводки в TG: {e}")

    return web.json_response({"ok": True, "scheduled_count": len(scheduled_info)})


# --- КОМАНДЫ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type in ("group", "supergroup"):
        register_member(message.chat.id, message.from_user.id, message.from_user.full_name, message.from_user.username)

    await message.answer(
        f"👋 Привет! Это общий бот дедлайнов ЦУ.\n\n"
        f"• <b>На этой неделе:</b> /week\n"
        f"• <b>Все актуальные дедлайны:</b> /deadlines\n"
        f"• <b>Записаться на тег в группе:</b> /checkin\n"
        f"• <b>Сделать этот чат главным для уведомлений:</b> /setgroup\n\n"
        f"ID этого чата: <code>{message.chat.id}</code>",
        reply_markup=main_kb
    )


@dp.message(Command("setgroup"))
async def cmd_setgroup(message: Message):
    set_target_group_id(message.chat.id)
    if message.from_user:
        register_member(message.chat.id, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await message.answer(
        f"✅ Чат <code>{message.chat.id}</code> установлен как основной для отправки дедлайнов!\n"
        f"Нажмите кнопку ниже, чтобы бот отмечал вас при напоминаниях:",
        reply_markup=checkin_inline_kb
    )


@dp.message(Command("checkin"))
async def cmd_checkin(message: Message):
    target_chat = message.chat.id if message.chat.type in ("group", "supergroup") else (get_target_group_id() or message.chat.id)
    register_member(target_chat, message.from_user.id, message.from_user.full_name, message.from_user.username)
    members_count = len(get_chat_members(target_chat))
    await message.answer(f"🙋‍♂️ Готово, <b>{message.from_user.first_name}</b>! Теперь бот будет тегать тебя при напоминаниях (всего в списке: {members_count} чел.).")


@dp.callback_query(F.data == "checkin_me")
async def cb_checkin_me(callback: CallbackQuery):
    register_member(callback.message.chat.id, callback.from_user.id, callback.from_user.full_name, callback.from_user.username)
    members_count = len(get_chat_members(callback.message.chat.id))
    await callback.answer(f"Ты в списке на пинг! (Всего: {members_count} чел.)", show_alert=False)


def fetch_upcoming_tasks(days_limit: int | None = None):
    now = datetime.now(TZ)
    limit_dt = now + timedelta(days=days_limit) if days_limit else None
    result = []

    with sqlite3.connect(APP_DB_PATH) as conn:
        rows = conn.execute("SELECT task_name, course_name, raw_date, iso_date FROM tasks").fetchall()

    for task_name, course_name, raw_date, iso_date in rows:
        if iso_date:
            try:
                dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00")).astimezone(TZ)
                if dt < now:
                    continue
                if limit_dt and dt > limit_dt:
                    continue
                result.append((dt, task_name, course_name, raw_date))
            except Exception:
                pass
        elif not days_limit:
            result.append((datetime.max.replace(tzinfo=TZ), task_name, course_name, raw_date))

    result.sort(key=lambda x: x[0])
    return result


@dp.message(Command("week"))
@dp.message(F.text == "📅 Дедлайны на этой неделе")
async def cmd_week_deadlines(message: Message):
    tasks = fetch_upcoming_tasks(days_limit=7)
    if not tasks:
        await message.answer("🎉 На ближайшие 7 дней активных дедлайнов нет!")
        return

    lines = []
    for dt, task_name, course_name, raw_date in tasks:
        date_str = dt.strftime("%d.%m (%a) в %H:%M")
        lines.append(f"🔥 <b>{date_str}</b> — {task_name}\n   <i>📚 {course_name}</i>")

    await message.answer(
        f"📅 <b>Дедлайны на ближайшие 7 дней ({len(tasks)} шт.):</b>\n\n" + "\n\n".join(lines)
    )


@dp.message(Command("deadlines"))
@dp.message(F.text == "📋 Все дедлайны")
async def cmd_all_deadlines(message: Message):
    tasks = fetch_upcoming_tasks(days_limit=None)
    if not tasks:
        await message.answer("📭 Список актуальных дедлайнов пуст.")
        return

    lines = []
    for dt, task_name, course_name, raw_date in tasks:
        date_str = dt.strftime("%d.%m.%Y в %H:%M") if dt.year < 9999 else raw_date
        lines.append(f"📌 <b>{task_name}</b> ({course_name})\n   ⏰ <code>{date_str}</code>")

    await message.answer(
        f"📋 <b>Все предстоящие дедлайны ({len(tasks)} шт.):</b>\n\n" + "\n\n".join(lines)
    )


@dp.message(Command("jobs"))
@dp.message(F.text == "🔔 Активные таймеры")
async def cmd_jobs(message: Message):
    jobs = [j for j in scheduler.get_jobs() if j.next_run_time]
    if not jobs:
        await message.answer("📭 Сейчас нет запланированных таймеров.")
        return

    lines = []
    for j in sorted(jobs, key=lambda x: x.next_run_time):
        kw = j.kwargs
        run_str = j.next_run_time.astimezone(TZ).strftime("%d.%m в %H:%M")
        lines.append(f"🕒 <code>{run_str}</code> — <b>{kw['task_name']}</b> ({kw['time_left_label']})")

    await message.answer("🔔 <b>Расписание будущих уведомлений:</b>\n\n" + "\n".join(lines))


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def auto_collect_group_member(message: Message):
    if message.from_user and not message.from_user.is_bot:
        register_member(message.chat.id, message.from_user.id, message.from_user.full_name, message.from_user.username)


async def main():
    init_db()
    scheduler.start()

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", health_check)
    app.router.add_post("/api/tasks", handle_new_tasks)
    app.router.add_options("/api/tasks", handle_new_tasks)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logging.info(f"API-сервер запущен на порту {PORT}")
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())