"""Telegram-бот для студента: расписание, напоминания о парах, ДЗ, дела, погода, IT-новости."""
from __future__ import annotations

import html
import json
import logging
import math
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

import httpx
import recurring_ical_events
from dotenv import load_dotenv
from icalendar import Calendar
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ICS_URL = os.environ["ICS_URL"]
NEWS_URL = os.getenv("NEWS_URL", "https://habr.com/ru/rss/news/?fl=ru")
NEWS_COUNT = int(os.getenv("NEWS_COUNT", "3"))
DEFAULT_TIME = os.getenv("MORNING_TIME", "07:30")
CITY_NAME = "Таганрог"
LAT, LON = 47.2362, 38.8969
TZ = ZoneInfo("Europe/Moscow")
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("morning_bot")

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
REL = {-1: "Вчера", 0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}

WMO = {
    0: "☀️ ясно", 1: "🌤 преимущественно ясно", 2: "⛅️ переменная облачность", 3: "☁️ пасмурно",
    45: "🌫 туман", 48: "🌫 изморозь", 51: "🌦 лёгкая морось", 53: "🌦 морось", 55: "🌧 сильная морось",
    56: "🌧 ледяная морось", 57: "🌧 ледяная морось", 61: "🌧 небольшой дождь", 63: "🌧 дождь",
    65: "🌧 сильный дождь", 66: "🌧 ледяной дождь", 67: "🌧 ледяной дождь", 71: "🌨 небольшой снег",
    73: "🌨 снег", 75: "❄️ сильный снег", 77: "🌨 снежные зёрна", 80: "🌦 ливень", 81: "🌧 сильный ливень",
    82: "⛈ очень сильный ливень", 85: "🌨 снегопад", 86: "❄️ сильный снегопад", 95: "⛈ гроза",
    96: "⛈ гроза с градом", 99: "⛈ сильная гроза с градом",
}

MORNING_PRESETS = ["06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]
EVENING_PRESETS = ["19:00", "20:00", "21:00", "22:00"]
REMIND_PRESETS = [10, 15, 20, 30]
MAX_LIST = 10  # сколько дел / ДЗ показывать с кнопками


# ======================================================================
# Хранилище
# ======================================================================
def _defaults() -> dict:
    return {
        "chat_id": None, "time": DEFAULT_TIME, "evening": "20:00", "remind": 15,
        "todos": [], "next_id": 1, "hw": [], "next_hw_id": 1,
    }


def load_data() -> dict:
    d = _defaults()
    if DATA_FILE.exists():
        d.update(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    return d


def save_data(d: dict) -> None:
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ======================================================================
# Мелкие помощники оформления
# ======================================================================
def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def warn(text: str) -> str:
    return f"⚠️ {esc(text)}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def date_long(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}"


def date_short(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def day_title(d: date, today: date) -> tuple[str, str]:
    """('Завтра', 'вт, 6 октября') или ('Четверг', '8 октября')."""
    diff = (d - today).days
    if diff in REL:
        return REL[diff], date_short(d)
    return WEEKDAYS[d.weekday()].capitalize(), f"{d.day} {MONTHS[d.month - 1]}"


def day_label(d: date, today: date) -> str:
    main, sub = day_title(d, today)
    return f"{main} · {sub}"


def humanize(delta: timedelta) -> str:
    m = max(int(delta.total_seconds() // 60), 0)
    if m < 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    if m < 24 * 60:
        h, mm = divmod(m, 60)
        return f"{h} ч {mm} мин" if mm else f"{h} ч"
    days = m // (24 * 60)
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


def section(title: str, body: str, note: str = "", expandable: bool = False) -> str:
    head = f"<b>{title}</b>" + (f"  <i>{note}</i>" if note else "")
    open_tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{head}\n{open_tag}{body}</blockquote>"


def same_subject(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def fmt_t(x: float) -> str:
    v = round(x)
    return f"{v:+d}" if v else "0"


# ======================================================================
# Расписание (ICS с коротким кэшем)
# ======================================================================
_cal: dict = {"ts": 0.0, "obj": None}


async def get_calendar():
    if _cal["obj"] is not None and monotonic() - _cal["ts"] < 120:
        return _cal["obj"]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(ICS_URL)
            r.raise_for_status()
        _cal["obj"] = Calendar.from_ical(r.content)
        _cal["ts"] = monotonic()
    except Exception:
        if _cal["obj"] is None:
            raise
        log.warning("Не удалось обновить расписание, использую кэш", exc_info=True)
    return _cal["obj"]


def drop_cache() -> None:
    _cal["ts"] = 0.0


def _norm(ev) -> dict:
    start = ev.get("DTSTART").dt
    end = ev.get("DTEND").dt if ev.get("DTEND") else None
    all_day = not isinstance(start, datetime)
    if not all_day:
        start = start.astimezone(TZ) if start.tzinfo else start.replace(tzinfo=TZ)
        if isinstance(end, datetime):
            end = end.astimezone(TZ) if end.tzinfo else end.replace(tzinfo=TZ)
        else:
            end = None
    else:
        end = None
    return {
        "start": start, "end": end, "all_day": all_day,
        "date": start if all_day else start.date(),
        "summary": str(ev.get("SUMMARY", "Без названия")).strip(),
        "location": str(ev.get("LOCATION", "") or "").strip(),
    }


def _sort(events: list[dict]) -> list[dict]:
    return sorted(
        events,
        key=lambda e: (e["date"], not e["all_day"], "" if e["all_day"] else e["start"].strftime("%H:%M")),
    )


async def fetch_events(day: date) -> list[dict]:
    cal = await get_calendar()
    return _sort([_norm(e) for e in recurring_ical_events.of(cal).at(day)])


async def fetch_range(first: date, last: date) -> list[dict]:
    cal = await get_calendar()
    evs = recurring_ical_events.of(cal).between(first, last + timedelta(days=1))
    return _sort([_norm(e) for e in evs])


async def next_lesson(subject: str | None = None, days: int = 28) -> dict | None:
    now = datetime.now(TZ)
    for e in await fetch_range(now.date(), now.date() + timedelta(days=days)):
        if e["all_day"] or e["start"] <= now:
            continue
        if subject and not same_subject(subject, e["summary"]):
            continue
        return e
    return None


async def known_subjects(days: int = 21) -> list[str]:
    today = datetime.now(TZ).date()
    seen: dict[str, str] = {}
    for e in await fetch_range(today, today + timedelta(days=days)):
        if not e["all_day"] and e["summary"]:
            seen.setdefault(e["summary"].lower(), e["summary"])
    return sorted(seen.values(), key=str.lower)


def fmt_events(events: list[dict], now: datetime | None = None, hw: list[dict] | None = None) -> str:
    """Список пар. now — подсветить текущую и приглушить прошедшие; hw — ДЗ под парой."""
    if not events:
        return "Занятий нет 🎉"
    out = []
    for e in events:
        name = esc(e["summary"])
        if e["all_day"]:
            out.append(f"<code>весь день</code>  <b>{name}</b>")
            continue
        rng = e["start"].strftime("%H:%M") + (f"–{e['end'].strftime('%H:%M')}" if e["end"] else "")
        if now and e["end"] and e["end"] <= now:
            out.append(f"<i>{rng}  {name}  ✓</i>")
            continue
        ongoing = bool(now and e["end"] and e["start"] <= now < e["end"])
        lines = [f"{'🟢 ' if ongoing else ''}<code>{rng}</code>  <b>{name}</b>"]
        if e["location"]:
            lines.append(f"📍 {esc(e['location'])}")
        for h in hw or []:
            if h["due"] == e["date"].isoformat() and same_subject(h["subject"], e["summary"]):
                lines.append(f"📖 {esc(h['text'])}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def day_summary(events: list[dict]) -> str:
    timed = [e for e in events if not e["all_day"]]
    if not timed:
        return ""
    first = min(e["start"] for e in timed)
    last = max(e["end"] or e["start"] for e in timed)
    n = len(timed)
    return f"{n} {plural(n, 'пара', 'пары', 'пар')} · {first:%H:%M}–{last:%H:%M}"


async def schedule_block(day: date, now: datetime | None = None, hw: list[dict] | None = None) -> tuple[str, str]:
    """(текст, краткая сводка). Ошибка сети не роняет экран."""
    try:
        events = await fetch_events(day)
        return fmt_events(events, now, hw), day_summary(events)
    except Exception as ex:  # noqa: BLE001
        log.exception("schedule")
        return warn(f"Не удалось получить расписание ({type(ex).__name__})"), ""


# ======================================================================
# Погода и новости
# ======================================================================
async def weather_block() -> str:
    try:
        params = {
            "latitude": LAT, "longitude": LON, "timezone": "Europe/Moscow", "wind_speed_unit": "ms",
            "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        }
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
        j = r.json()
        cur, day = j["current"], j["daily"]
        desc = WMO.get(day["weather_code"][0], "")
        prob = day["precipitation_probability_max"][0] or 0
        text = (
            f"{desc} · <b>{fmt_t(cur['temperature_2m'])}°C</b> (ощущ. {fmt_t(cur['apparent_temperature'])}°)\n"
            f"↓ {fmt_t(day['temperature_2m_min'][0])}°  ↑ {fmt_t(day['temperature_2m_max'][0])}°"
            f"  ·  💨 {cur['wind_speed_10m']:.0f} м/с  ·  💧 {prob}%"
        )
        if prob >= 50:
            text += "\n☂️ Возьмите зонт"
        return text
    except Exception as ex:  # noqa: BLE001
        log.exception("weather")
        return warn(f"Не удалось получить погоду ({type(ex).__name__})")


async def news_text(limit: int) -> str:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(NEWS_URL, headers={"User-Agent": "Mozilla/5.0 morning-bot"})
            r.raise_for_status()
        root = ET.fromstring(r.content)
        lines = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if title and link:
                lines.append(f'• <a href="{html.escape(link, quote=True)}">{esc(title)}</a>')
            if len(lines) >= limit:
                break
        return "\n".join(lines) if lines else "Новостей пока нет."
    except Exception as ex:  # noqa: BLE001
        log.exception("news")
        return warn(f"Не удалось получить новости ({type(ex).__name__})")


# ======================================================================
# Дела
# ======================================================================
def todos_text(d: dict) -> str:
    if not d["todos"]:
        return "Список пуст"
    lines = []
    for t in d["todos"][:MAX_LIST]:
        if t["done"]:
            lines.append(f"✅ <b>{t['id']}</b>  <s>{esc(t['text'])}</s>")
        else:
            lines.append(f"⬜️ <b>{t['id']}</b>  {esc(t['text'])}")
    extra = len(d["todos"]) - MAX_LIST
    if extra > 0:
        lines.append(f"<i>…и ещё {extra}</i>")
    return "\n".join(lines)


def todos_brief(d: dict) -> str:
    pend = [t for t in d["todos"] if not t["done"]]
    lines = [f"⬜️ {esc(t['text'])}" for t in pend[:5]]
    if len(pend) > 5:
        lines.append(f"<i>…и ещё {len(pend) - 5}</i>")
    return "\n".join(lines)


# ======================================================================
# Домашние задания
# ======================================================================
def pending_hw(d: dict) -> list[dict]:
    items = [h for h in d["hw"] if not h["done"]]
    return sorted(items, key=lambda h: (h["due"] is None, h["due"] or "", h["id"]))


def hw_lines(items: list[dict]) -> str:
    return "\n".join(f"• <b>{esc(h['subject'])}</b> — {esc(h['text'])}" for h in items)


def hw_text(d: dict, today: date) -> str:
    items = pending_hw(d)
    if not items:
        return "Заданий нет 🎉\nЗапишите новое кнопкой ниже."
    shown, extra = items[:MAX_LIST], len(items) - MAX_LIST
    groups: dict[str | None, list[dict]] = {}
    for h in shown:
        groups.setdefault(h["due"], []).append(h)
    parts = []
    for due, hs in groups.items():
        if due is None:
            label = "Без даты"
        else:
            dd = date.fromisoformat(due)
            label = ("⚠️ " if dd < today else "") + day_label(dd, today)
        parts.append(section(label, hw_lines(hs)))
    if extra > 0:
        parts.append(f"<i>…и ещё {extra}</i>")
    return "\n".join(parts)


def add_hw(d: dict, subject: str, text: str, due: date | None) -> None:
    d["hw"] = [h for h in d["hw"] if not h["done"]]  # чистим выполненные
    d["hw"].append({
        "id": d["next_hw_id"], "subject": subject, "text": text[:300],
        "due": due.isoformat() if due else None, "done": False,
    })
    d["next_hw_id"] += 1
    save_data(d)


# ======================================================================
# Сводки
# ======================================================================
async def build_brief() -> str:
    now = datetime.now(TZ)
    today = now.date()
    d = load_data()
    sched, summ = await schedule_block(today, now)
    parts = [
        f"<b>☀️ Доброе утро!</b>\n{date_long(today)}",
        section("📚 Расписание", sched, summ),
        section(f"🌡 Погода · {CITY_NAME}", await weather_block()),
    ]
    due = [h for h in pending_hw(d) if h["due"] == today.isoformat()]
    if due:
        parts.append(section("📖 ДЗ на сегодня", hw_lines(due)))
    pend = [t for t in d["todos"] if not t["done"]]
    if pend:
        parts.append(section("📝 Дела", todos_brief(d), f"осталось {len(pend)}"))
    parts.append(section("📰 IT-новости", await news_text(NEWS_COUNT), expandable=True))
    return "\n\n".join(parts)


async def build_evening() -> str:
    today = datetime.now(TZ).date()
    tomorrow = today + timedelta(days=1)
    d = load_data()
    sched, summ = await schedule_block(tomorrow)
    parts = [
        f"<b>🌙 Добрый вечер!</b>\nЗавтра · {date_long(tomorrow)}",
        section("📚 Расписание на завтра", sched, summ),
    ]
    due = [h for h in pending_hw(d) if h["due"] == tomorrow.isoformat()]
    if due:
        parts.append(section("📖 ДЗ на завтра", hw_lines(due)))
    else:
        parts.append("<i>📖 На завтра ДЗ не записано. Если что-то задали — добавьте кнопкой ниже.</i>")
    return "\n\n".join(parts)


# ======================================================================
# Клавиатуры
# ======================================================================
def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


def nav(view: str) -> list[InlineKeyboardButton]:
    return [btn("🔄 Обновить", f"r:{view}"), btn("🏠 Меню", "v:menu")]


def cancel_kb(back: str) -> InlineKeyboardMarkup:
    return kb([btn("✖ Отмена", f"c:{back}")])


def main_menu_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("☀️ Сводка", "v:brief"), btn("📚 Сегодня", "v:day:0")],
        [btn("🌙 Завтра", "v:day:1"), btn("📖 ДЗ", "v:hw")],
        [btn("📝 Дела", "v:todos"), btn("📰 Новости", "v:news")],
        [btn("🌡 Погода", "v:weather"), btn("⚙️ Настройки", "v:settings")],
    )


def cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ======================================================================
# Экраны
# ======================================================================
async def render(view: str, notice: str = "") -> tuple[str, InlineKeyboardMarkup]:
    name, _, param = view.partition(":")
    now = datetime.now(TZ)
    today = now.date()
    d = load_data()

    if name == "brief":
        text = await build_brief()
        markup = kb(
            [btn("📖 ДЗ", "v:hw"), btn("📝 Дела", "v:todos"), btn("📰 Новости", "v:news")],
            nav("brief"),
        )
        return notice + text, markup

    if name == "evening":
        markup = kb(
            [btn("📖 ДЗ", "v:hw"), btn("➕ Записать ДЗ", "h:new")],
            [btn("📝 Дела", "v:todos"), btn("🏠 Меню", "v:menu")],
        )
        return notice + await build_evening(), markup

    if name == "day":
        offset = int(param or 0)
        day = today + timedelta(days=offset)
        body, summ = await schedule_block(day, now if offset == 0 else None, pending_hw(d))
        main, sub = day_title(day, today)
        text = section(f"📚 {main} · {sub}", body, summ)
        prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
        centre = btn("⏭ Ближайшая", "v:next") if offset == 0 else btn("Сегодня", "v:day:0")
        markup = kb(
            [btn(f"‹ {WEEKDAYS_SHORT[prev_d.weekday()]}", f"v:day:{offset - 1}"), centre,
             btn(f"{WEEKDAYS_SHORT[next_d.weekday()]} ›", f"v:day:{offset + 1}")],
            nav(f"day:{offset}"),
        )
        return notice + text, markup

    if name == "next":
        try:
            e = await next_lesson()
            if e is None:
                text = "<b>⏭ Ближайшая пара</b>\nНа месяц вперёд занятий нет."
            else:
                text = section(
                    "⏭ Ближайшая пара",
                    f"<b>{day_label(e['date'], today)}</b>\n\n{fmt_events([e], hw=pending_hw(d))}",
                    f"через {humanize(e['start'] - now)}",
                )
        except Exception as ex:  # noqa: BLE001
            log.exception("next")
            text = warn(f"Не удалось получить расписание ({type(ex).__name__})")
        markup = kb([btn("📚 Сегодня", "v:day:0"), btn("🌙 Завтра", "v:day:1")], nav("next"))
        return notice + text, markup

    if name == "weather":
        text = section(f"🌡 Погода · {CITY_NAME}", await weather_block())
        return notice + text, kb(nav("weather"))

    if name == "news":
        return notice + section("📰 IT-новости", await news_text(8)), kb(nav("news"))

    if name == "todos":
        pend = sum(1 for t in d["todos"] if not t["done"])
        text = section("📝 Дела", todos_text(d), f"осталось {pend}" if d["todos"] else "")
        rows = []
        for t in d["todos"][:MAX_LIST]:
            icon = "↩️" if t["done"] else "✅"
            rows.append([btn(f"{icon} {cut(t['text'], 24)}", f"t:done:{t['id']}"), btn("🗑", f"t:del:{t['id']}")])
        rows.append([btn("➕ Добавить", "t:add"), btn("🏠 Меню", "v:menu")])
        if any(t["done"] for t in d["todos"]):
            rows.append([btn("🧹 Убрать выполненные", "t:clear")])
        return notice + text, kb(*rows)

    if name == "hw":
        text = f"<b>📖 Домашние задания</b>\n{hw_text(d, today)}"
        rows = []
        for h in pending_hw(d)[:MAX_LIST]:
            label = cut(f"{h['subject']} — {h['text']}", 26)
            rows.append([btn(f"✅ {label}", f"h:done:{h['id']}"), btn("🗑", f"h:del:{h['id']}")])
        rows.append([btn("➕ Записать ДЗ", "h:new"), btn("🏠 Меню", "v:menu")])
        return notice + text, kb(*rows)

    if name == "hwsubj":
        try:
            subjects = (await known_subjects())[:14]
        except Exception:  # noqa: BLE001
            log.exception("subjects")
            subjects = []
        if not subjects:
            text = ("<b>📖 Записать ДЗ</b>\nНе удалось получить список предметов.\n"
                    "Можно так: <code>/hw Предмет: что задали</code>")
            return notice + text, kb([btn("‹ Назад", "v:hw")])
        text = "<b>📖 Записать ДЗ</b>\nВыберите предмет — задание привяжется к его ближайшей паре."
        rows = []
        for i in range(0, len(subjects), 2):
            rows.append([btn(cut(s, 24), f"h:s:{i + j}") for j, s in enumerate(subjects[i:i + 2])])
        rows.append([btn("‹ Назад", "v:hw")])
        return notice + text, kb(*rows)

    if name == "settings":
        rem = f"за {d['remind']} мин" if d["remind"] else "выкл"
        body = (
            f"☀️ Утренняя сводка — <b>{d['time']}</b>\n"
            f"🌙 Вечерняя сводка — <b>{d['evening'] or 'выкл'}</b>\n"
            f"⏰ Напоминание о паре — <b>{rem}</b>"
        )
        text = section("⚙️ Настройки", body) + "\n<i>Время указано по Москве.</i>"
        markup = kb(
            [btn("☀️ Утро", "v:set:morning"), btn("🌙 Вечер", "v:set:evening"), btn("⏰ Напоминание", "v:set:remind")],
            [btn("🏠 Меню", "v:menu")],
        )
        return notice + text, markup

    if name == "set":
        back = [btn("‹ Назад", "v:settings")]
        if param == "morning":
            row = [btn(("✔ " if t == d["time"] else "") + t, f"tm:morning:{t}") for t in MORNING_PRESETS]
            text = "<b>☀️ Утренняя сводка</b>\nВо сколько присылать?\n<i>Своё время: /time 07:45</i>"
            return notice + text, kb(row[:3], row[3:], back)
        if param == "evening":
            row = [btn(("✔ " if t == d["evening"] else "") + t, f"tm:evening:{t}") for t in EVENING_PRESETS]
            off = btn(("✔ " if not d["evening"] else "") + "🚫 Выкл", "tm:evening:off")
            text = "<b>🌙 Вечерняя сводка</b>\nРасписание и ДЗ на завтра.\n<i>Своё время: /evening 20:30</i>"
            return notice + text, kb(row[:2], row[2:], [off], back)
        row = [btn(("✔ " if d["remind"] == m else "") + f"{m} мин", f"tm:remind:{m}") for m in REMIND_PRESETS]
        off = btn(("✔ " if not d["remind"] else "") + "🚫 Выкл", "tm:remind:0")
        text = "<b>⏰ Напоминание о паре</b>\nЗа сколько минут предупреждать?\n<i>Своё значение: /remind 25</i>"
        return notice + text, kb(row[:2], row[2:], [off], back)

    # меню
    lines = ["<b>🏠 Меню</b>"]
    try:
        e = await next_lesson(days=14)
        if e:
            when = f"через {humanize(e['start'] - now)}"
            lines.append(f"⏭ <b>{esc(e['summary'])}</b> · {when}")
    except Exception:  # noqa: BLE001
        log.exception("menu")
    n_hw = len(pending_hw(d))
    n_todo = sum(1 for t in d["todos"] if not t["done"])
    lines.append(f"📖 ДЗ: <b>{n_hw}</b>   ·   📝 Дела: <b>{n_todo}</b>")
    return notice + "\n".join(lines), main_menu_kb()


# ======================================================================
# Настройки
# ======================================================================
def parse_hhmm(val: str) -> str:
    hh, mm = map(int, val.split(":"))
    time(hh, mm)  # проверка диапазона
    return f"{hh:02d}:{mm:02d}"


def apply_setting(app: Application, key: str, val: str) -> None:
    d = load_data()
    val = val.strip().lower()
    if key == "morning":
        d["time"] = parse_hhmm(val)
    elif key == "evening":
        d["evening"] = None if val in ("off", "выкл") else parse_hhmm(val)
    elif key == "remind":
        n = 0 if val in ("off", "выкл") else int(val)
        if not 0 <= n <= 120:
            raise ValueError("remind")
        d["remind"] = n
    else:
        raise ValueError(key)
    save_data(d)
    reschedule(app)


def reschedule(app: Application) -> None:
    jq = app.job_queue
    for name in ("morning", "evening"):
        for j in jq.get_jobs_by_name(name):
            j.schedule_removal()
    d = load_data()
    hh, mm = map(int, d["time"].split(":"))
    jq.run_daily(morning_job, time(hh, mm, tzinfo=TZ), name="morning")
    if d.get("evening"):
        hh, mm = map(int, d["evening"].split(":"))
        jq.run_daily(evening_job, time(hh, mm, tzinfo=TZ), name="evening")


# ======================================================================
# Фоновые задачи
# ======================================================================
async def push(context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup | None) -> None:
    d = load_data()
    if d["chat_id"]:
        await context.bot.send_message(
            d["chat_id"], text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )


async def morning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await render("brief")
    await push(context, text, markup)


async def evening_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    text, markup = await render("evening")
    await push(context, text, markup)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в минуту: если до пары осталось <= N минут — напомнить (один раз)."""
    d = load_data()
    mins = d.get("remind", 0)
    if not d["chat_id"] or not mins:
        return
    now = datetime.now(TZ)
    try:
        events = await fetch_events(now.date())
    except Exception:  # noqa: BLE001
        log.warning("reminder: расписание недоступно", exc_info=True)
        return
    sent: set = context.bot_data.setdefault("reminded", set())
    if len(sent) > 300:
        sent.clear()
    hw = pending_hw(d)
    for e in events:
        if e["all_day"]:
            continue
        left = (e["start"] - now).total_seconds() / 60
        key = (e["start"].isoformat(), e["summary"])
        if 0 < left <= mins and key not in sent:
            sent.add(key)
            text = f"<b>⏰ Через {math.ceil(left)} мин</b>\n" + fmt_events([e], hw=hw)
            await push(context, text, kb([btn("📖 ДЗ", "v:hw"), btn("🏠 Меню", "v:menu")]))


# ======================================================================
# Команды
# ======================================================================
HELP = (
    "<b>Команды</b>\n"
    "/menu — меню с кнопками\n"
    "/brief — сводка сейчас\n"
    "/today, /tomorrow — расписание\n"
    "/next — ближайшая пара\n"
    "/hw — домашние задания\n"
    "/hw <i>Предмет: текст</i> — записать ДЗ\n"
    "/list — дела\n"
    "/add <i>текст</i> — добавить дело\n"
    "/done <i>N</i> · /del <i>N</i> · /clear\n"
    "/weather · /news\n"
    "/time <i>ЧЧ:ММ</i> — утренняя сводка\n"
    "/evening <i>ЧЧ:ММ|off</i> — вечерняя сводка\n"
    "/remind <i>мин|off</i> — напоминание о паре"
)

BOT_COMMANDS = [
    ("menu", "Меню"), ("brief", "Сводка сейчас"), ("today", "Расписание на сегодня"),
    ("tomorrow", "Расписание на завтра"), ("next", "Ближайшая пара"), ("hw", "Домашние задания"),
    ("list", "Список дел"), ("weather", "Погода"), ("news", "IT-новости"),
]


def allowed(update: Update) -> bool:
    d = load_data()
    return d["chat_id"] is None or update.effective_chat.id == d["chat_id"]


async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup
    )


def guard(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not allowed(update):
            return
        context.user_data.pop("await", None)  # любая команда отменяет ожидание текста
        await fn(update, context)
    return wrapper


def view_command(view: str):
    @guard
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text, markup = await render(view)
        await reply(update, text, markup)
    return handler


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = load_data()
    if d["chat_id"] is None:
        d["chat_id"] = update.effective_chat.id
        save_data(d)
        reschedule(context.application)
    elif update.effective_chat.id != d["chat_id"]:
        return
    intro = (
        "<b>Привет! 👋</b>\n"
        f"Утром (<b>{d['time']}</b>) пришлю сводку, вечером — расписание на завтра, "
        "а перед парами буду напоминать.\n\nВсё управляется кнопками ниже."
    )
    await reply(update, intro, main_menu_kb())


@guard
async def cmd_help(update, context):
    await reply(update, HELP, main_menu_kb())


@guard
async def cmd_add(update, context):
    text = " ".join(context.args).strip()
    if not text:
        return await reply(update, "Использование: <code>/add купить хлеб</code>")
    d = load_data()
    d["todos"].append({"id": d["next_id"], "text": text[:300], "done": False})
    d["next_id"] += 1
    save_data(d)
    out, markup = await render("todos", "✅ <i>Добавлено</i>\n\n")
    await reply(update, out, markup)


def _find(d: dict, arg: str, key: str = "todos"):
    if not arg.isdigit():
        return None
    return next((t for t in d[key] if t["id"] == int(arg)), None)


@guard
async def cmd_done(update, context):
    d = load_data()
    t = _find(d, context.args[0]) if context.args else None
    if not t:
        return await reply(update, "Использование: <code>/done 3</code> (номер из /list)")
    t["done"] = True
    save_data(d)
    out, markup = await render("todos", "✅ <i>Готово</i>\n\n")
    await reply(update, out, markup)


@guard
async def cmd_del(update, context):
    d = load_data()
    t = _find(d, context.args[0]) if context.args else None
    if not t:
        return await reply(update, "Использование: <code>/del 3</code> (номер из /list)")
    d["todos"].remove(t)
    save_data(d)
    out, markup = await render("todos", "🗑 <i>Удалено</i>\n\n")
    await reply(update, out, markup)


@guard
async def cmd_clear(update, context):
    d = load_data()
    d["todos"] = [t for t in d["todos"] if not t["done"]]
    save_data(d)
    out, markup = await render("todos", "🧹 <i>Выполненные убраны</i>\n\n")
    await reply(update, out, markup)


@guard
async def cmd_hw(update, context):
    raw = " ".join(context.args).strip()
    if not raw:
        out, markup = await render("hw")
        return await reply(update, out, markup)
    subj, sep, body = raw.partition(":")
    subj, body = subj.strip(), body.strip()
    if not sep or not subj or not body:
        return await reply(update, "Использование: <code>/hw Математика: № 5–10</code>")
    try:
        match = next((s for s in await known_subjects() if same_subject(subj, s)), None)
        subject = match or subj
        nxt = await next_lesson(subject)
    except Exception:  # noqa: BLE001
        log.exception("hw")
        subject, nxt = subj, None
    d = load_data()
    add_hw(d, subject, body, nxt["date"] if nxt else None)
    notice = (f"✅ <i>Записано на {date_short(nxt['date'])}</i>\n\n" if nxt
              else "✅ <i>Записано без даты: ближайшая пара не найдена</i>\n\n")
    out, markup = await render("hw", notice)
    await reply(update, out, markup)


async def _setting_cmd(update, context, key: str, usage: str):
    arg = context.args[0] if context.args else ""
    try:
        apply_setting(context.application, key, arg)
    except ValueError:
        return await reply(update, f"Использование: <code>{usage}</code>")
    out, markup = await render("settings", "✅ <i>Сохранено</i>\n\n")
    await reply(update, out, markup)


@guard
async def cmd_time(update, context):
    await _setting_cmd(update, context, "morning", "/time 07:30")


@guard
async def cmd_evening(update, context):
    await _setting_cmd(update, context, "evening", "/evening 20:00  или  /evening off")


@guard
async def cmd_remind(update, context):
    await _setting_cmd(update, context, "remind", "/remind 15  или  /remind off")


# ======================================================================
# Кнопки и ввод текста
# ======================================================================
async def edit(q, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest as ex:
        if "not modified" not in str(ex).lower():
            raise


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = load_data()
    if d["chat_id"] is not None and q.message.chat.id != d["chat_id"]:
        await q.answer()
        return

    ud = context.user_data
    kind, _, arg = q.data.partition(":")
    view, toast = "menu", None

    if kind in ("v", "r", "c"):
        if kind == "r":
            drop_cache()
        ud.pop("await", None)
        view = arg

    elif kind == "t":
        action, _, tid = arg.partition(":")
        view = "todos"
        if action == "add":
            ud["await"] = {"kind": "todo", "msg": q.message.message_id}
            await q.answer()
            await edit(q, "<b>📝 Новое дело</b>\nОтправьте текст сообщением ✍️", cancel_kb("todos"))
            return
        if action == "clear":
            d["todos"] = [t for t in d["todos"] if not t["done"]]
        else:
            t = _find(d, tid)
            if t and action == "done":
                t["done"] = not t["done"]
            elif t and action == "del":
                d["todos"].remove(t)
        save_data(d)

    elif kind == "h":
        action, _, rest = arg.partition(":")
        view = "hw"
        if action == "new":
            view = "hwsubj"
        elif action == "s":
            try:
                subject = (await known_subjects())[int(rest)]
            except (ValueError, IndexError, Exception):  # noqa: BLE001
                toast, view = "Список предметов изменился, выберите заново", "hwsubj"
            else:
                try:
                    nxt = await next_lesson(subject)
                except Exception:  # noqa: BLE001
                    nxt = None
                ud["await"] = {
                    "kind": "hw", "subject": subject, "msg": q.message.message_id,
                    "due": nxt["date"].isoformat() if nxt else None,
                }
                when = (f"Ближайшая пара: <b>{date_short(nxt['date'])}, {nxt['start']:%H:%M}</b>" if nxt
                        else "Ближайшая пара не найдена — ДЗ сохранится без даты.")
                await q.answer()
                await edit(
                    q,
                    f"<b>📖 {esc(subject)}</b>\n{when}\n\nОтправьте текст задания сообщением ✍️",
                    cancel_kb("hw"),
                )
                return
        elif action in ("done", "del"):
            h = _find(d, rest, "hw")
            if h and action == "done":
                h["done"] = True
                toast = "Отмечено ✓"
            elif h and action == "del":
                d["hw"].remove(h)
            save_data(d)

    elif kind == "tm":
        target, _, val = arg.partition(":")
        view = "settings"
        try:
            apply_setting(context.application, target, val)
            toast = "Сохранено ✓"
        except ValueError:
            toast = "Некорректное значение"

    await q.answer(toast)
    text, markup = await render(view)
    await edit(q, text, markup)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обычное сообщение — это текст дела или ДЗ, если бот его ждёт."""
    if not allowed(update):
        return
    aw = context.user_data.get("await")
    text = (update.message.text or "").strip()
    if not aw or not text:
        return
    context.user_data.pop("await", None)
    d = load_data()

    if aw["kind"] == "todo":
        d["todos"].append({"id": d["next_id"], "text": text[:300], "done": False})
        d["next_id"] += 1
        save_data(d)
        view, notice = "todos", "✅ <i>Дело добавлено</i>\n\n"
    else:
        due = date.fromisoformat(aw["due"]) if aw.get("due") else None
        add_hw(d, aw["subject"], text, due)
        view = "hw"
        notice = (f"✅ <i>Записано на {date_short(due)}</i>\n\n" if due
                  else "✅ <i>Записано без даты</i>\n\n")

    try:  # убираем сообщение пользователя, чтобы чат оставался чистым
        await update.message.delete()
    except Exception:  # noqa: BLE001
        pass
    out, markup = await render(view, notice)
    chat_id = update.effective_chat.id
    try:
        await context.bot.edit_message_text(
            text=out, chat_id=chat_id, message_id=aw["msg"], parse_mode=ParseMode.HTML,
            reply_markup=markup, disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        await context.bot.send_message(
            chat_id, out, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )


# ======================================================================
# Запуск
# ======================================================================
async def post_init(app: Application) -> None:
    reschedule(app)
    app.job_queue.run_repeating(reminder_job, interval=60, first=10, name="reminder")
    try:
        await app.bot.set_my_commands([BotCommand(c, t) for c, t in BOT_COMMANDS])
    except Exception:  # noqa: BLE001
        log.warning("Не удалось обновить список команд", exc_info=True)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    for name, view in {
        "menu": "menu", "brief": "brief", "today": "day:0", "tomorrow": "day:1", "next": "next",
        "weather": "weather", "news": "news", "list": "todos",
    }.items():
        app.add_handler(CommandHandler(name, view_command(view)))
    for name, fn in {
        "add": cmd_add, "done": cmd_done, "del": cmd_del, "clear": cmd_clear, "hw": cmd_hw,
        "time": cmd_time, "evening": cmd_evening, "remind": cmd_remind,
    }.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
