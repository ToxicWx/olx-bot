"""
OLX Telegram Parser Bot
"""

import asyncio
from html import escape as html_escape
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from bs4 import BeautifulSoup
from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters as tg_filters,
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")
DATA_DIR = Path("data")
LOG_DIR = Path("logs")
CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "config.json"))
SEEN_FILE = DATA_DIR / "seen_ids.json"
FILTERS_FILE = DATA_DIR / "filters.json"
STATS_FILE = DATA_DIR / "stats.json"
DEFAULT_MAX_SEEN_IDS = 5000
HTTP_ALERT_THRESHOLD = 3
HTTP_ALERT_REPEAT_MIN = 30

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


CONFIG = load_config()


def get_int_setting(env_name: str, config_key: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is not None:
        return int(raw)
    return int(CONFIG.get(config_key, default))


BOT_TOKEN = os.getenv("BOT_TOKEN") or CONFIG.get("bot_token")
CHAT_ID = str(os.getenv("CHAT_ID") or CONFIG.get("chat_id", ""))
DELAY_MIN = get_int_setting("DELAY_MIN_SECONDS", "delay_min_seconds", 50)
DELAY_MAX = get_int_setting("DELAY_MAX_SECONDS", "delay_max_seconds", 110)
MAX_AGE_MIN = get_int_setting("MAX_AD_AGE_MINUTES", "max_ad_age_minutes", 30)
MAX_SEEN_IDS = get_int_setting("MAX_SEEN_IDS", "max_seen_ids", DEFAULT_MAX_SEEN_IDS)

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing BOT_TOKEN or CHAT_ID. Set them in environment variables "
        "or in config.json."
    )

state: dict = {
    "paused": False,
    "loop_running": False,  # True поки parser_loop живий
    "current_filter_idx": 0,
    "last_check": None,
    "next_check": None,
    "http_failures": {},
    "http_alerted_at": {},
    "last_http_status": None,
    "last_http_url": None,
    "last_cards_count": 0,
    "last_new_count": 0,
    "last_skip_count": 0,
    "seen_count": 0,
}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📡 Статус", "🔍 Перевірити", "📋 Фільтри"],
        ["➕ Додати фільтр", "➖ Видалити фільтр", "📤 Експорт"],
        ["⏸ Пауза", "▶️ Продовжити", "📊 Статистика"],
        ["🗑 Очистити seen", "⏱ Вік оголошень", "ℹ️ Допомога"],
    ],
    resize_keyboard=True,
    is_persistent=False,
)


def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load JSON from %s: %s", path, e)
    return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_seen() -> set:
    return set(load_json(SEEN_FILE, []))


def save_seen(seen: set):
    save_json(SEEN_FILE, list(seen))


def trim_seen(seen: set) -> set:
    if len(seen) > MAX_SEEN_IDS:
        lst = load_json(SEEN_FILE, [])
        return set(lst[-MAX_SEEN_IDS:])
    return seen


def load_filters() -> list:
    dynamic = load_json(FILTERS_FILE, [])
    static = CONFIG.get("filters", [])
    static_urls = {f.get("url") for f in static}
    unique_dynamic = [f for f in dynamic if f.get("url") not in static_urls]
    return static + unique_dynamic


def save_dynamic_filters(lst: list):
    save_json(FILTERS_FILE, lst)


def load_stats() -> dict:
    return load_json(
        STATS_FILE,
        {
            "total_sent": 0,
            "total_checked": 0,
            "errors": 0,
            "started_at": datetime.now(KYIV_TZ).isoformat(),
        },
    )


def save_stats(s: dict):
    save_json(STATS_FILE, s)


def clear_pending_action(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_action", None)
    ctx.user_data.pop("new_filter_label", None)


def set_pending_action(ctx: ContextTypes.DEFAULT_TYPE, action: str):
    ctx.user_data["pending_action"] = action


def get_pending_action(ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    return ctx.user_data.get("pending_action")


def escape_md(text: str) -> str:
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def export_filters_text() -> str:
    fl = load_filters()
    if not fl:
        return "Фільтрів поки немає."
    lines = []
    for i, item in enumerate(fl, start=1):
        label = item.get("label", "без назви")
        url = item.get("url", "")
        lines.append(f"{i}. {label} | {url}")
    return "\n".join(lines)


MONTHS_UK = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    # Російські назви на випадок мікс-контенту
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def parse_ad_time(location_date_text: str) -> datetime | None:
    now = datetime.now(KYIV_TZ)
    text = location_date_text.strip().lower()

    # Формат "Сьогодні 14:35" або "Сегодня 14:35"
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if any(w in text for w in ("сьогодні", "сегодня", "today")):
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if any(w in text for w in ("учора", "вчора", "вчера", "yesterday")):
            return (now - timedelta(days=1)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
        return None

    # Формат "12 квітня 2026 р." або "12 квітня 2026"
    m2 = re.search(r"(\d{1,2})\s+([а-яіїєґёa-z]+)\s+(\d{4})", text)
    if m2:
        day = int(m2.group(1))
        month_word = m2.group(2)
        year = int(m2.group(3))
        month = MONTHS_UK.get(month_word)
        if month:
            try:
                return datetime(year, month, day, 12, 0, tzinfo=KYIV_TZ)
            except ValueError:
                return None

    return None


def is_fresh(ad: dict, max_age_minutes: int) -> bool:
    """
    Повертає True якщо оголошення достатньо свіже.
    Якщо час взагалі не вдається розпізнати — повертає True,
    щоб не губити оголошення без дати (краще зайве, ніж пропустити).
    """
    ad_time = parse_ad_time(ad.get("location", ""))
    if ad_time is None:
        # Час не знайдено — пропускаємо лише якщо location явно каже "вчора/учора"
        loc_low = ad.get("location", "").lower()
        if any(w in loc_low for w in ("учора", "вчора", "вчера", "yesterday")):
            return False
        return True  # Невідомий час — відправляємо, щоб не пропустити
    age = (datetime.now(KYIV_TZ) - ad_time).total_seconds() / 60
    return -2 <= age <= max_age_minutes


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.olx.ua/",
}


async def notify_error(app: Application, text: str):
    try:
        safe = text.replace("_", "\\_").replace("*", "\\*")
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🆘 *ПОМИЛКА ПАРСЕРА*\n\n{safe}",
            parse_mode=ParseMode.MARKDOWN,
        )
        s = load_stats()
        s["errors"] += 1
        save_stats(s)
    except Exception as e:
        log.error("Не вдалося надіслати помилку: %s", e)


def reset_http_failures():
    state["http_failures"].clear()


async def maybe_notify_http_streak(app: Application, status: int, url: str):
    if status not in (403, 429):
        return
    key = str(status)
    count = int(state["http_failures"].get(key, 0))
    if count < HTTP_ALERT_THRESHOLD:
        return
    now = datetime.now(KYIV_TZ)
    last = state["http_alerted_at"].get(key)
    if last and (now - last).total_seconds() < HTTP_ALERT_REPEAT_MIN * 60:
        return
    state["http_alerted_at"][key] = now
    await notify_error(
        app,
        f"⚠️ *Повторюваний HTTP {status}*\n"
        f"Поспіль вже {count} відповіді(ей).\n`{url}`",
    )


async def fetch_page(session: aiohttp.ClientSession, url: str, app: Application) -> str | None:
    try:
        async with session.get(
            url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            state["last_http_status"] = r.status
            state["last_http_url"] = url
            if r.status == 200:
                reset_http_failures()
                return await r.text()
            if r.status in (403, 429):
                key = str(r.status)
                state["http_failures"][key] = int(state["http_failures"].get(key, 0)) + 1
                await maybe_notify_http_streak(app, r.status, url)
            if r.status == 403:
                msg = f"🚫 *Заблоковано (403)*\nIP сервера, можливо, в бані.\n`{url}`"
            elif r.status == 429:
                msg = f"⏱ *Забагато запитів (429)*\nOLX тимчасово обмежив.\n`{url}`"
            elif r.status in (502, 503, 504):
                msg = f"🔧 *OLX недоступний ({r.status})*"
            else:
                msg = f"⚠️ *HTTP {r.status}*\n`{url}`"
            await notify_error(app, msg)
            log.warning("HTTP %s: %s", r.status, url)
    except asyncio.TimeoutError:
        await notify_error(app, f"⏳ *Таймаут 30 сек*\n`{url}`")
    except aiohttp.ClientConnectorError as e:
        await notify_error(app, f"🌐 *Помилка мережі*\n`{e}`")
    except Exception as e:
        await notify_error(app, f"❌ *Невідома помилка fetch*\n`{e}`")
    return None


def parse_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    # OLX змінював розмітку — пробуємо всі відомі варіанти селекторів карток.
    # Новий OLX (2025+): картки не мають data-cy='l-card', але всередині є
    # [data-testid='ad-card-title'] — знаходимо їх і беремо батьківський контейнер.
    cards = soup.select("div[data-cy='l-card']") or soup.select("li[data-cy='l-card']")

    if not cards:
        # Новий layout: шукаємо по внутрішньому елементу і піднімаємось до контейнера
        title_anchors = soup.select("[data-testid='ad-card-title']")
        seen_containers = set()
        container_list = []
        for el in title_anchors:
            # Піднімаємось до спільного контейнера картки (4–6 рівнів вгору)
            container = el
            for _ in range(8):
                parent = container.parent
                if parent is None or parent.name in ("body", "html", "[document]"):
                    break
                container = parent
                cid = id(container)
                if cid not in seen_containers:
                    # Перевіряємо що контейнер містить і ціну і посилання
                    if container.select_one("[data-testid='ad-price']") and container.select_one("a[href]"):
                        seen_containers.add(cid)
                        container_list.append(container)
                        break
        cards = container_list

    log.debug("parse_listings: знайдено %d карток", len(cards))

    for card in cards:
        try:
            # ID: шукаємо спочатку атрибут id/data-id, потім витягуємо з URL
            ad_id = (card.get("id") or card.get("data-id") or "").strip()
            if not ad_id:
                link_tmp = card.select_one("a[href]")
                if link_tmp:
                    href = link_tmp.get("href", "")
                    # Новий формат: /d/uk/obyavlenie/назва-ID10h2JW.html
                    # Старий формат: /uk/назва/123456789.html
                    m = re.search(r"-([A-Za-z0-9]{6,}?)\.html", href)
                    if m:
                        ad_id = m.group(1)
            if not ad_id:
                continue

            title_el = (
                card.select_one("[data-testid='ad-card-title'] h4")
                or card.select_one("[data-testid='ad-card-title'] h6")
                or card.select_one("[data-testid='ad-card-title'] h3")
                or card.select_one("[data-testid='ad-card-title'] a")
                or card.select_one("[data-testid='ad-title']")
                or card.select_one("[data-cy='ad-card-title'] h4")
                or card.select_one("[data-cy='ad-card-title'] h6")
                or card.select_one("h4")
                or card.select_one("h6")
                or card.select_one("h3")
            )
            title = title_el.get_text(strip=True) if title_el else "Без назви"

            price_el = (
                card.select_one("p[data-testid='ad-price']")
                or card.select_one("[data-testid='ad-price']")
                or card.select_one(".price-label")
                or card.select_one("strong[data-testid]")
            )
            price = price_el.get_text(strip=True) if price_el else "Ціна не вказана"

            link_el = card.select_one("a[href]")
            link = link_el["href"] if link_el else ""
            if link and not link.startswith("http"):
                link = "https://www.olx.ua" + link
            if link:
                link = link.split("?")[0]

            img_el = card.select_one("img")
            image = None
            if img_el:
                for attr in ("src", "data-src", "data-lazy-src"):
                    val = img_el.get(attr, "")
                    if val and "placeholder" not in val and not val.startswith("data:"):
                        image = val
                        break

            location_el = (
                card.select_one("p[data-testid='location-date']")
                or card.select_one("[data-testid='location-date']")
                or card.select_one("p[data-cy='location-date']")
            )
            location = location_el.get_text(strip=True) if location_el else ""

            # Fallback для заголовка через alt зображення або title посилання
            if title == "Без назви":
                if link_el:
                    title = (link_el.get("title") or "").strip() or title
                if img_el:
                    title = (img_el.get("alt") or "").strip() or title

            results.append(
                {
                    "id": ad_id,
                    "title": title,
                    "price": price,
                    "link": link,
                    "image": image,
                    "location": location,
                }
            )
        except Exception as e:
            log.debug("Картка: %s", e)
    return results


def build_html_message(ad: dict, label: str = "") -> str:
    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")
    lines = []
    if label:
        lines.append(f"🔍 <i>{html_escape(label)}</i>")
    lines.append(f"🆕 <b>{html_escape(ad['title'])}</b>")
    lines.append(f"💰 {html_escape(ad['price'])}")
    if ad.get("location"):
        lines.append(f"📍 {html_escape(ad['location'])}")
    lines.append(f"🕐 Знайдено о {now_str} (Київ)")
    if ad.get("link"):
        lines.append(f'🔗 <a href="{html_escape(ad["link"], quote=True)}">Переглянути</a>')
    return "\n".join(lines)


async def send_ad(app: Application, ad: dict, label: str = ""):
    text = build_html_message(ad, label)
    try:
        if ad.get("image"):
            await app.bot.send_photo(
                chat_id=CHAT_ID,
                photo=ad["image"],
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        log.info("Sent %s | %s", ad["title"], ad["price"])
        s = load_stats()
        s["total_sent"] += 1
        save_stats(s)
    except Exception as e:
        log.warning("Фото не вийшло (%s), пробую без", e)
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e2:
            log.error("Відправка: %s", e2)


async def check_filter(
    session: aiohttp.ClientSession,
    app: Application,
    seen: set,
    filter_cfg: dict,
    max_age: int,
) -> tuple[int, int]:
    url = filter_cfg["url"]
    label = filter_cfg.get("label", "")

    html = await fetch_page(session, url, app)
    if not html:
        return 0, 0

    ads = parse_listings(html)
    state["last_cards_count"] = len(ads)
    log.info('"%s": %d карток', label or url[:50], len(ads))

    s = load_stats()
    s["total_checked"] += len(ads)
    save_stats(s)

    new_count = 0
    skip_count = 0
    for ad in ads:
        if ad["id"] in seen:
            continue
        if not is_fresh(ad, max_age):
            seen.add(ad["id"])
            skip_count += 1
            log.debug("Старе: %s | %s", ad["title"], ad["location"])
            continue
        seen.add(ad["id"])
        await send_ad(app, ad, label)
        new_count += 1
        await asyncio.sleep(1.5)

    seen_trimmed = trim_seen(seen)
    seen.clear()
    seen.update(seen_trimmed)
    save_seen(seen)
    state["last_new_count"] = new_count
    state["last_skip_count"] = skip_count
    state["seen_count"] = len(seen)

    log.info("Нових: %d | пропущено старих: %d", new_count, skip_count)
    return new_count, skip_count


def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.message is None:
            return
        if str(update.effective_chat.id) != CHAT_ID:
            await update.message.reply_text("⛔ Доступ заборонено.")
            return
        return await func(update, ctx)

    return wrapper


def button_labels() -> set[str]:
    return {
        "📡 Статус",
        "🔍 Перевірити",
        "📋 Фільтри",
        "➕ Додати фільтр",
        "➖ Видалити фільтр",
        "📤 Експорт",
        "⏸ Пауза",
        "▶️ Продовжити",
        "📊 Статистика",
        "🗑 Очистити seen",
        "⏱ Вік оголошень",
        "ℹ️ Допомога",
    }


@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    await update.message.reply_text(
        "👋 *OLX Parser Bot*\n\n"
        "📋 /filters — список фільтрів\n"
        "➕ /add `Назва | URL` — додати фільтр\n"
        "➖ /remove `N` — видалити фільтр\n"
        "📡 /status — стан бота\n"
        "🔍 /check — перевірити зараз\n"
        "⏸ /pause — призупинити\n"
        "▶️ /resume — відновити\n"
        "⏱ /setage `хвилин` — вік оголошення\n"
        "📊 /stats — статистика\n"
        "🗑 /clearseen — скинути переглянуті\n"
        "📤 /export — експортувати фільтри\n"
        "⌨️ /menu — показати кнопки\n\n"
        "Кнопки нижче дублюють основні дії, а фільтри можна додавати і без команд.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_KEYBOARD,
    )


@admin_only
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    await update.message.reply_text(
        "Клавіатура відкрита. Її можна згорнути і знову відкрити кнопкою клавіатури біля поля вводу.",
        reply_markup=MAIN_KEYBOARD,
    )


@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl = load_filters()
    seen = load_seen()
    state["seen_count"] = len(seen)
    runtime_seen = max(len(seen), int(state.get("seen_count", 0)))
    max_age = ctx.bot_data.get("max_age", MAX_AGE_MIN)
    paused = "⏸ Призупинено" if state["paused"] else "✅ Активний"
    last_s = state["last_check"].strftime("%H:%M:%S") if state["last_check"] else "—"
    next_s = state["next_check"].strftime("%H:%M:%S") if state["next_check"] else "—"
    idx = state["current_filter_idx"] % len(fl) if fl else 0
    cur = fl[idx].get("label", f"#{idx + 1}") if fl else "немає"
    await update.message.reply_text(
        f"📡 *Стан бота*\n\n"
        f"Статус: {paused}\n"
        f"Фільтрів: {len(fl)}\n"
        f"Переглянутих ID: {len(seen)} / {MAX_SEEN_IDS}\n"
        f"Макс. вік оголошення: {max_age} хв\n"
        f"Затримка: {DELAY_MIN}–{DELAY_MAX} сек\n\n"
        f"Остання перевірка: {last_s}\n"
        f"Наступна перевірка: {next_s}\n"
        f"Поточний фільтр: _{escape_md(cur)}_",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_status_v2(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl = load_filters()
    seen = load_seen()
    runtime_seen = max(len(seen), int(state.get("seen_count", 0)))
    max_age = ctx.bot_data.get("max_age", MAX_AGE_MIN)

    if not state["loop_running"]:
        bot_status = "🔴 Loop не запущений"
    elif state["paused"]:
        bot_status = "⏸ Призупинено"
    else:
        bot_status = "✅ Активний"

    last_s = state["last_check"].strftime("%H:%M:%S") if state["last_check"] else "—"
    next_s = state["next_check"].strftime("%H:%M:%S") if state["next_check"] else "—"
    idx = state["current_filter_idx"] % len(fl) if fl else 0
    cur = fl[idx].get("label", f"#{idx + 1}") if fl else "немає"
    http_status = state["last_http_status"] if state["last_http_status"] is not None else "—"
    http_ok = "✅" if state["last_http_status"] == 200 else ("⚠️" if state["last_http_status"] else "")
    await update.message.reply_text(
        f"📡 *Стан бота*\n\n"
        f"Статус: {bot_status}\n"
        f"Фільтрів: {len(fl)}\n"
        f"Переглянутих ID: {runtime_seen} / {MAX_SEEN_IDS}\n"
        f"Макс\\. вік оголошення: {max_age} хв\n"
        f"Затримка: {DELAY_MIN}–{DELAY_MAX} сек\n\n"
        f"Остання перевірка: {last_s}\n"
        f"Наступна перевірка: {next_s}\n"
        f"Поточний фільтр: _{escape_md(cur)}_\n\n"
        f"HTTP останнього запиту: {http_ok} {http_status}\n"
        f"Знайдено карток: {state['last_cards_count']}\n"
        f"Нових / старих: {state['last_new_count']} / {state['last_skip_count']}",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl = load_filters()
    if not fl:
        await update.message.reply_text(
            "📋 Немає фільтрів. Додай через кнопку або /add",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    static_n = len(CONFIG.get("filters", []))
    lines = ["📋 *Фільтри:*\n"]
    for i, f in enumerate(fl):
        icon = "📌" if i < static_n else "➕"
        label = f.get("label", "без назви")
        url = f.get("url", "")
        lines.append(f"{icon} *{i+1}.* {escape_md(label)}\n`{url[:70]}`\n")
    if static_n:
        lines.append("📌 config.json  |  ➕ через бота\nВидалити: /remove `N` або кнопкою")
    else:
        lines.append("Видалити: /remove `N` або кнопкою")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def add_filter(label: str, url: str) -> tuple[bool, str]:
    if not url.startswith("http"):
        return False, "❌ URL має починатись з `https://`"
    if any(f.get("url") == url for f in load_filters()):
        return False, "⚠️ Фільтр з таким URL вже існує\\!"
    dynamic = load_json(FILTERS_FILE, [])
    dynamic.append({"label": label, "url": url})
    save_dynamic_filters(dynamic)
    return True, f"✅ Додано!\n*{escape_md(label)}*\n`{url}`"


@admin_only
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip()
    if "|" not in args:
        await update.message.reply_text(
            "⚠️ Формат: `/add Назва | URL`\n\nПриклад:\n`/add iPhone 14 | https://www.olx.ua/uk/...`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    label, url = [p.strip() for p in args.split("|", 1)]
    ok, msg = add_filter(label, url)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    if ok:
        clear_pending_action(ctx)


def remove_filter_by_number(idx: int) -> tuple[bool, str]:
    fl = load_filters()
    static_n = len(CONFIG.get("filters", []))
    if idx < 0 or idx >= len(fl):
        return False, f"❌ Немає фільтра #{idx + 1}"
    if idx < static_n:
        return (
            False,
            "⚠️ Це статичний фільтр з `config.json`.\n"
            "Редагуй файл вручну і перезапусти бота.",
        )
    dynamic = load_json(FILTERS_FILE, [])
    removed = dynamic.pop(idx - static_n)
    save_dynamic_filters(dynamic)
    return (
        True,
        f"🗑 Видалено: *{escape_md(removed.get('label', removed['url']))}*",
    )


@admin_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Вкажи номер: `/remove 3`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        idx = int(ctx.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер має бути числом")
        return
    _, msg = remove_filter_by_number(idx)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    clear_pending_action(ctx)


@admin_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    state["paused"] = True
    await update.message.reply_text("⏸ Парсинг призупинено. /resume — відновити")


@admin_only
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    state["paused"] = False
    await update.message.reply_text("▶️ Парсинг відновлено!")


@admin_only
async def cmd_setage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        cur = ctx.bot_data.get("max_age", MAX_AGE_MIN)
        await update.message.reply_text(
            f"⏱ Поточний макс. вік: *{cur} хв*\nЗмінити: `/setage 20`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        m = int(ctx.args[0])
        if not (1 <= m <= 1440):
            raise ValueError
        ctx.bot_data["max_age"] = m
        clear_pending_action(ctx)
        await update.message.reply_text(
            f"✅ Макс. вік: *{m} хв*",
            parse_mode=ParseMode.MARKDOWN,
        )
    except ValueError:
        await update.message.reply_text("❌ Число від 1 до 1440")


@admin_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    s = load_stats()
    try:
        started = datetime.fromisoformat(s["started_at"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        started = s.get("started_at", "?")
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"🚀 Запущено: {started}\n"
        f"✉️ Надіслано: {s.get('total_sent', 0)}\n"
        f"🔍 Перевірено карток: {s.get('total_checked', 0)}\n"
        f"❌ Помилок: {s.get('errors', 0)}",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    text = export_filters_text()
    await update.message.reply_text(
        f"📤 *Експорт фільтрів*\n\n```text\n{text}\n```",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    fl = load_filters()
    if not fl:
        await update.message.reply_text("📋 Немає фільтрів")
        return
    idx = state["current_filter_idx"] % len(fl)
    f = fl[idx]
    await update.message.reply_text(
        f"🔍 Перевіряю: *{escape_md(f.get('label', 'фільтр'))}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    seen = load_seen()
    max_age = ctx.bot_data.get("max_age", MAX_AGE_MIN)
    async with aiohttp.ClientSession() as session:
        new, skipped = await check_filter(session, ctx.application, seen, f, max_age)
    await update.message.reply_text(
        f"✅ Готово!\nНових: *{new}* | Пропущено старих: *{skipped}*",
        parse_mode=ParseMode.MARKDOWN,
    )


@admin_only
async def cmd_clearseen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    clear_pending_action(ctx)
    n = len(load_seen())
    save_seen(set())
    await update.message.reply_text(
        f"🗑 Очищено {n} ID. Наступна перевірка покаже свіжі оголошення."
    )


@admin_only
async def handle_menu_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text in button_labels():
        clear_pending_action(ctx)

    if text == "📡 Статус":
        await cmd_status_v2(update, ctx)
        return
    if text == "🔍 Перевірити":
        await cmd_check(update, ctx)
        return
    if text == "📋 Фільтри":
        await cmd_filters(update, ctx)
        return
    if text == "➕ Додати фільтр":
        set_pending_action(ctx, "add_label")
        await update.message.reply_text(
            "Введи назву фільтра одним повідомленням.\nНаприклад: `iPhone 14 до 20000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if text == "➖ Видалити фільтр":
        fl = load_filters()
        if not fl:
            await update.message.reply_text("📋 Немає фільтрів для видалення.")
            return
        set_pending_action(ctx, "remove_number")
        await cmd_filters(update, ctx)
        await update.message.reply_text(
            "Введи номер фільтра для видалення.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if text == "📤 Експорт":
        await cmd_export(update, ctx)
        return
    if text == "⏸ Пауза":
        await cmd_pause(update, ctx)
        return
    if text == "▶️ Продовжити":
        await cmd_resume(update, ctx)
        return
    if text == "📊 Статистика":
        await cmd_stats(update, ctx)
        return
    if text == "🗑 Очистити seen":
        await cmd_clearseen(update, ctx)
        return
    if text == "⏱ Вік оголошень":
        set_pending_action(ctx, "set_age")
        cur = ctx.bot_data.get("max_age", MAX_AGE_MIN)
        await update.message.reply_text(
            f"Поточний макс. вік: {cur} хв.\nВідправ число від 1 до 1440.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if text == "ℹ️ Допомога":
        await cmd_start(update, ctx)
        return

    pending = get_pending_action(ctx)
    if pending == "add_label":
        ctx.user_data["new_filter_label"] = text
        set_pending_action(ctx, "add_url")
        await update.message.reply_text(
            "Тепер відправ URL пошуку OLX для цього фільтра.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if pending == "add_url":
        label = ctx.user_data.get("new_filter_label", "").strip()
        ok, msg = add_filter(label, text)
        if ok:
            clear_pending_action(ctx)
        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if pending == "remove_number":
        try:
            idx = int(text) - 1
        except ValueError:
            await update.message.reply_text("❌ Введи номер фільтра цифрою.")
            return
        ok, msg = remove_filter_by_number(idx)
        if ok:
            clear_pending_action(ctx)
        await update.message.reply_text(
            msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if pending == "set_age":
        try:
            m = int(text)
            if not (1 <= m <= 1440):
                raise ValueError
            ctx.bot_data["max_age"] = m
            clear_pending_action(ctx)
            await update.message.reply_text(
                f"✅ Макс. вік змінено на {m} хв.",
                reply_markup=MAIN_KEYBOARD,
            )
        except ValueError:
            await update.message.reply_text("❌ Введи число від 1 до 1440.")
        return

    await update.message.reply_text(
        "Для керування використовуй кнопки нижче або команди через `/start`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and str(update.effective_chat.id) == CHAT_ID:
        await update.message.reply_text(
            "❓ Невідома команда. /start — довідка",
            reply_markup=MAIN_KEYBOARD,
        )


async def parser_loop(app: Application):
    log.info("Цикл парсингу запущений")
    state["loop_running"] = True
    seen = load_seen()

    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                if state["paused"]:
                    await asyncio.sleep(10)
                    continue

                fl = load_filters()
                if not fl:
                    log.warning("Немає фільтрів — чекаю 30 сек")
                    await asyncio.sleep(30)
                    continue

                idx = state["current_filter_idx"] % len(fl)
                f = fl[idx]
                max_age = app.bot_data.get("max_age", MAX_AGE_MIN)

                state["last_check"] = datetime.now(KYIV_TZ)

                try:
                    await check_filter(session, app, seen, f, max_age)
                except Exception as e:
                    log.error("Цикл: %s", e)
                    await notify_error(app, f"❌ *Критична помилка циклу*\n`{e}`")

                state["current_filter_idx"] += 1
                delay = random.randint(DELAY_MIN, DELAY_MAX)

                next_fl = load_filters()
                next_idx = state["current_filter_idx"] % len(next_fl) if next_fl else 0
                next_lbl = next_fl[next_idx].get("label", f"#{next_idx + 1}") if next_fl else "—"
                state["next_check"] = datetime.now(KYIV_TZ) + timedelta(seconds=delay)

                log.info('Наступний: "%s" через %d сек.', next_lbl, delay)
                await asyncio.sleep(delay)
    finally:
        state["loop_running"] = False
        log.error("parser_loop завершився несподівано!")


async def post_init(app: Application):
    app.bot_data.setdefault("max_age", MAX_AGE_MIN)
    fl = load_filters()
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "✅ *OLX\\-бот запущений\\!*\n\n"
            f"📋 Фільтрів: {len(fl)}\n"
            f"⏱ Макс\\. вік: {app.bot_data['max_age']} хв\n"
            f"🔄 Затримка: {DELAY_MIN}–{DELAY_MAX} сек\n"
            f"💾 Ліміт seen ID: {MAX_SEEN_IDS}\n\n"
            "Напиши /start або користуйся кнопками нижче"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=MAIN_KEYBOARD,
    )
    asyncio.create_task(parser_loop(app))


def main():
    log.info("Запуск...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for cmd, handler in [
        ("start", cmd_start),
        ("help", cmd_start),
        ("menu", cmd_menu),
        ("status", cmd_status_v2),
        ("filters", cmd_filters),
        ("add", cmd_add),
        ("remove", cmd_remove),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("setage", cmd_setage),
        ("stats", cmd_stats),
        ("check", cmd_check),
        ("clearseen", cmd_clearseen),
        ("export", cmd_export),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(tg_filters.COMMAND, cmd_unknown))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_menu_text))
    log.info("Polling started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
