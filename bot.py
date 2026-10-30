"""
Особистий Telegram-бот-нагадувалка.
Задачі пишуться звичайним текстом, Claude перетворює їх на задачі з датою і повтором.
"""
import os
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic
from dateutil.rrule import rrulestr
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

# ---------- Налаштування (можна змінювати через змінні середовища) ----------
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Warsaw"))
DB_PATH = os.getenv("DB_PATH", "tasks.db")
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
REPEAT_EVERY = timedelta(hours=float(os.getenv("REPEAT_HOURS", "2")))  # повтор нагадування
QUIET_FROM = int(os.getenv("QUIET_FROM", "22"))   # з цієї години повтори не надсилаємо
QUIET_TO = int(os.getenv("QUIET_TO", "8"))        # до цієї години повтори не надсилаємо
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "21"))
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))  # підсумки тижня — у неділю
DEFAULT_HOUR = 10                                  # якщо час задачі не вказано
FMT = "%Y-%m-%d %H:%M"
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

client = AsyncAnthropic()  # бере ключ з ANTHROPIC_API_KEY


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def parse_dt(s: str) -> datetime:
    return datetime.strptime(s.replace("T", " ")[:16], FMT)


# ---------- База даних ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_at TEXT NOT NULL,
            rrule TEXT,
            repeat_text TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            last_reminded_at TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER, title TEXT, action TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)


def get_owner():
    if os.getenv("OWNER_ID"):
        return int(os.environ["OWNER_ID"])
    with db() as con:
        r = con.execute("SELECT value FROM settings WHERE key='owner'").fetchone()
    return int(r["value"]) if r else None


def set_owner(uid: int):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES('owner', ?)", (str(uid),))


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == get_owner()


def active_tasks():
    with db() as con:
        return con.execute("SELECT * FROM tasks WHERE status='active' ORDER BY due_at").fetchall()


def get_task(tid):
    with db() as con:
        return con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def log(task, action):
    with db() as con:
        con.execute("INSERT INTO history(task_id, title, action, at) VALUES(?,?,?,?)",
                    (task["id"], task["title"], action, now().strftime(FMT)))


# ---------- Логіка задач ----------
def next_occurrence(rule: str, after: datetime):
    r = rrulestr(rule, dtstart=after)
    nxt = r.after(after)
    while nxt and nxt <= now():
        nxt = r.after(nxt)
    return nxt


def fmt_due(s: str) -> str:
    d = parse_dt(s)
    today = now().date()
    if d.date() == today:
        day = "сьогодні"
    elif d.date() == today + timedelta(days=1):
        day = "завтра"
    elif d.date() == today - timedelta(days=1):
        day = "вчора"
    else:
        day = d.strftime("%d.%m")
    return f"{day} {d.strftime('%H:%M')}"


def task_line(t) -> str:
    warn = "⚠️ " if parse_dt(t["due_at"]) <= now() else ""
    rep = f" 🔁 {t['repeat_text']}" if t["rrule"] and t["repeat_text"] else (" 🔁" if t["rrule"] else "")
    return f"• {warn}{t['title']} — {fmt_due(t['due_at'])}{rep}"


def complete(t, action="done") -> str:
    """Зроблено/пропущено. Регулярна задача переїжджає на наступну дату."""
    log(t, action)
    with db() as con:
        if t["rrule"]:
            nxt = next_occurrence(t["rrule"], parse_dt(t["due_at"]))
            if nxt:
                con.execute("UPDATE tasks SET due_at=?, last_reminded_at=NULL WHERE id=?",
                            (nxt.strftime(FMT), t["id"]))
                return f"Наступного разу: {fmt_due(nxt.strftime(FMT))}"
        con.execute("UPDATE tasks SET status=? WHERE id=?",
                    ("done" if action == "done" else "cancelled", t["id"]))
    return ""


def delete(t):
    log(t, "deleted")
    with db() as con:
        con.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (t["id"],))


def snooze(t, when: datetime):
    log(t, "snoozed")
    with db() as con:
        con.execute("UPDATE tasks SET due_at=?, last_reminded_at=NULL WHERE id=?",
                    (when.strftime(FMT), t["id"]))


def list_text(only_today=False) -> str:
    ts = active_tasks()
    if only_today:
        ts = [t for t in ts if parse_dt(t["due_at"]).date() <= now().date()]
    if not ts:
        return "На сьогодні задач немає 🎉" if only_today else "Задач немає 🎉"
    return "\n".join(task_line(t) for t in ts)


def keyboard(t):
    last = (InlineKeyboardButton("⏭ Пропустити раз", callback_data=f"skip:{t['id']}") if t["rrule"]
            else InlineKeyboardButton("❌ Видалити", callback_data=f"del:{t['id']}"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Зроблено", callback_data=f"done:{t['id']}")],
        [InlineKeyboardButton("⏰ +1 година", callback_data=f"h1:{t['id']}"),
         InlineKeyboardButton("🌙 Увечері", callback_data=f"ev:{t['id']}"),
         InlineKeyboardButton("📅 Завтра", callback_data=f"tm:{t['id']}")],
        [last],
    ])


# ---------- Розбір тексту через Claude ----------
TOOL = {
    "name": "apply_actions",
    "description": "Дії із задачами користувача",
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string",
                                 "enum": ["create", "update", "delete", "done", "list", "clarify", "chat"]},
                        "task_id": {"type": "integer"},
                        "title": {"type": "string"},
                        "due_at": {"type": "string", "description": "YYYY-MM-DD HH:MM, місцевий час"},
                        "rrule": {"type": "string", "description": "iCal RRULE без DTSTART або порожній рядок"},
                        "repeat_text": {"type": "string", "description": "Повтор словами: 'щомісяця 10-го'"},
                        "text": {"type": "string", "description": "Текст відповіді для clarify/chat"},
                    },
                    "required": ["type"],
                },
            }
        },
        "required": ["actions"],
    },
}


def system_prompt() -> str:
    n = now()
    tasks = "\n".join(
        f"id={t['id']}: {t['title']} | {t['due_at']} | {t['rrule'] or 'разова'}" for t in active_tasks()
    ) or "(задач немає)"
    return f"""Ти — особистий асистент-нагадувалка в Telegram. Зараз {n.strftime(FMT)}, {WEEKDAYS[n.weekday()]}.
Розбери повідомлення користувача і виклич apply_actions.

Правила:
- create — нова задача. title коротко, з великої літери, без дати. due_at — коли нагадати вперше.
  Час не вказано → {DEFAULT_HOUR:02d}:00. Дату не вказано → сьогодні (якщо час уже минув — завтра).
- Регулярна задача: заповни rrule (iCal RRULE без DTSTART), ЗАВЖДИ з BYHOUR і BYMINUTE;
  для щотижневих — BYDAY, для щомісячних — BYMONTHDAY. due_at — найближче майбутнє настання.
  repeat_text — повтор словами. Для разових задач rrule і repeat_text — порожні рядки.
- update — змінити наявну задачу: task_id і тільки змінені поля.
- delete — видалити задачу, done — позначити виконаною (потрібен task_id).
- list — користувач хоче побачити задачі.
- clarify — якщо незрозуміло, що зробити або коли; коротке питання в text.
- chat — повідомлення не про задачі; коротка відповідь у text.
- В одному повідомленні може бути кілька задач — поверни кілька дій.
- title, repeat_text і text пиши мовою користувача; за замовчуванням — українською.

Поточні задачі:
{tasks}"""


def apply(a: dict) -> str:
    typ = a.get("type")
    if typ in ("clarify", "chat"):
        return a.get("text", "")
    if typ == "list":
        return list_text()

    if typ == "create":
        try:
            due = parse_dt(a["due_at"])
        except Exception:
            return "Не вдалося зрозуміти дату — уточніть, будь ласка."
        rule = (a.get("rrule") or "").strip() or None
        if rule:
            try:
                rrulestr(rule, dtstart=due)
            except Exception:
                return f"Не вдалося зрозуміти повтор для «{a.get('title')}» — переформулюйте, будь ласка."
        with db() as con:
            cur = con.execute(
                "INSERT INTO tasks(title, due_at, rrule, repeat_text, created_at) VALUES(?,?,?,?,?)",
                (a.get("title") or "Задача", due.strftime(FMT), rule,
                 (a.get("repeat_text") or "").strip() or None, now().strftime(FMT)))
        return "✅ Додано:\n" + task_line(get_task(cur.lastrowid))

    t = get_task(a.get("task_id"))
    if not t or t["status"] != "active":
        return "Не знаходжу такої задачі. Подивитися список — /list"

    if typ == "update":
        fields = {}
        if a.get("title"):
            fields["title"] = a["title"]
        if a.get("due_at"):
            try:
                fields["due_at"] = parse_dt(a["due_at"]).strftime(FMT)
                fields["last_reminded_at"] = None
            except Exception:
                return "Не вдалося зрозуміти нову дату."
        if "rrule" in a:
            fields["rrule"] = (a["rrule"] or "").strip() or None
            fields["repeat_text"] = (a.get("repeat_text") or "").strip() or None
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            with db() as con:
                con.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), t["id"]))
        return "✏️ Змінено:\n" + task_line(get_task(t["id"]))
    if typ == "delete":
        delete(t)
        return f"🗑 Видалено: {t['title']}"
    if typ == "done":
        note = complete(t)
        return f"✅ Позначено: {t['title']}" + (f"\n{note}" if note else "")
    return ""


# ---------- Обробники Telegram ----------
HELP = (
    "Просто пишіть задачі звичайним текстом:\n"
    "• «завтра о 15:00 написати клієнту»\n"
    "• «щомісяця 10-го оплатити оренду»\n"
    "• «щопонеділка о 9 звіт»\n"
    "• «перенеси оренду на 12-те», «видали звіт», «оренду оплатила»\n\n"
    f"Якщо не натиснути «Зроблено», нагадаю знову через {REPEAT_EVERY.seconds // 3600 or 1} год "
    f"(вночі з {QUIET_FROM}:00 до {QUIET_TO}:00 мовчу).\n"
    f"Зведення: зранку о {MORNING_HOUR}:00, увечері о {EVENING_HOUR}:00, підсумки тижня — у неділю.\n\n"
    "/today — на сьогодні, /list — усі задачі"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner is None:
        set_owner(update.effective_user.id)
    elif owner != update.effective_user.id:
        await update.message.reply_text("Це особистий бот.")
        return
    await update.message.reply_text("Привіт! Я стежу за вашими задачами.\n\n" + HELP)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text(HELP)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text("📋 Усі задачі:\n" + list_text())


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text("📅 На сьогодні:\n" + list_text(only_today=True))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = update.message.text
    hist = ctx.chat_data.setdefault("hist", [])
    msgs = []
    for u, r in hist[-4:]:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": r}]
    msgs.append({"role": "user", "content": text})
    try:
        resp = await client.messages.create(
            model=MODEL, max_tokens=1024, system=system_prompt(), messages=msgs,
            tools=[TOOL], tool_choice={"type": "tool", "name": "apply_actions"})
        block = next(b for b in resp.content if b.type == "tool_use")
        replies = [apply(a) for a in block.input.get("actions", [])]
    except Exception:
        logging.exception("Помилка розбору")
        replies = ["Не вдалося обробити повідомлення, спробуйте ще раз."]
    reply = "\n\n".join(r for r in replies if r) or "Готово."
    hist.append((text, reply))
    del hist[:-4]
    await update.message.reply_text(reply)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        await q.answer()
        return
    act, tid = q.data.split(":")
    t = get_task(int(tid))
    if not t or t["status"] != "active":
        await q.answer("Задачу вже закрито")
        await q.edit_message_reply_markup(None)
        return
    n = now()
    if act == "done":
        note = complete(t)
        msg = f"✅ {t['title']}" + (f"\n{note}" if note else "")
    elif act == "skip":
        note = complete(t, "skipped")
        msg = f"⏭ Пропущено: {t['title']}" + (f"\n{note}" if note else "")
    elif act == "del":
        delete(t)
        msg = f"🗑 Видалено: {t['title']}"
    else:
        if act == "h1":
            when = n + timedelta(hours=1)
        elif act == "ev":
            day = n if n.hour < 19 else n + timedelta(days=1)
            when = day.replace(hour=19, minute=0)
        else:
            when = (n + timedelta(days=1)).replace(hour=DEFAULT_HOUR, minute=0)
        snooze(t, when)
        msg = f"⏰ {t['title']} — нагадаю {fmt_due(when.strftime(FMT))}"
    await q.answer()
    await q.edit_message_text(msg)


# ---------- Фонові завдання ----------
async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    """Щохвилини: надсилає нагадування про задачі, час яких настав."""
    owner = get_owner()
    if not owner:
        return
    n = now()
    quiet = n.hour >= QUIET_FROM or n.hour < QUIET_TO
    for t in active_tasks():
        try:
            if parse_dt(t["due_at"]) > n:
                continue
            if t["last_reminded_at"] is None:
                text = f"🔔 {t['title']}"
            else:
                if quiet or n - parse_dt(t["last_reminded_at"]) < REPEAT_EVERY:
                    continue
                text = f"🔔 Нагадую ще раз: {t['title']}\n(з {fmt_due(t['due_at'])})"
            await ctx.bot.send_message(owner, text, reply_markup=keyboard(t))
            with db() as con:
                con.execute("UPDATE tasks SET last_reminded_at=? WHERE id=?", (n.strftime(FMT), t["id"]))
        except Exception:
            logging.exception("Помилка нагадування")


async def morning(ctx: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if owner:
        await ctx.bot.send_message(owner, "☀️ Доброго ранку! План на сьогодні:\n" + list_text(only_today=True))


async def evening(ctx: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if not owner:
        return
    overdue = [t for t in active_tasks() if parse_dt(t["due_at"]) <= now()]
    if overdue:
        text = ("🌙 Поки не зроблено:\n" + "\n".join(task_line(t) for t in overdue) +
                "\n\nМожна написати, що перенести, наприклад «оренду на завтра».")
    else:
        text = "🌙 На сьогодні все зроблено 👏"
    await ctx.bot.send_message(owner, text)


async def weekly(ctx: ContextTypes.DEFAULT_TYPE):
    owner = get_owner()
    if not owner or now().weekday() != 6:  # тільки неділя
        return
    since = (now() - timedelta(days=7)).strftime(FMT)
    with db() as con:
        rows = con.execute("SELECT title, action FROM history WHERE at >= ?", (since,)).fetchall()
    done = Counter(r["title"] for r in rows if r["action"] == "done")
    snoozed = Counter(r["title"] for r in rows if r["action"] == "snoozed").most_common(3)
    overdue = [t for t in active_tasks() if parse_dt(t["due_at"]) <= now()]
    parts = [f"📊 Підсумки тижня\n\n✅ Зроблено: {sum(done.values())}"]
    if done:
        parts[0] += "\n" + "\n".join(f"• {k}" + (f" ×{v}" if v > 1 else "") for k, v in done.most_common(15))
    if snoozed:
        parts.append("⏰ Найчастіше відкладали:\n" + "\n".join(f"• {k} ×{v}" for k, v in snoozed))
    if overdue:
        parts.append("⚠️ Досі чекають:\n" + "\n".join(task_line(t) for t in overdue))
    await ctx.bot.send_message(owner, "\n\n".join(parts))


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("today", "Задачі на сьогодні"),
        BotCommand("list", "Усі задачі"),
        BotCommand("help", "Як користуватися"),
    ])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # не писати в логи адреси запитів (у них токен бота) і щохвилинний шум планувальника
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    init_db()
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    jq.run_repeating(tick, interval=60, first=10)
    jq.run_daily(morning, dtime(MORNING_HOUR, 0, tzinfo=TZ))
    jq.run_daily(evening, dtime(EVENING_HOUR, 0, tzinfo=TZ))
    jq.run_daily(weekly, dtime(WEEKLY_HOUR, 0, tzinfo=TZ))
    app.run_polling()


if __name__ == "__main__":
    main()
