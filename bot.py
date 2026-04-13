"""
OLX Telegram Parser Bot
━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
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
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters as tg_filters,
)

KYIV_TZ      = ZoneInfo("Europe/Kyiv")
DATA_DIR     = Path("data")
LOG_DIR      = Path("logs")
CONFIG_FILE  = Path(os.getenv("CONFIG_PATH", "config.json"))
SEEN_FILE    = DATA_DIR / "seen_ids.json"
FILTERS_FILE = DATA_DIR / "filters.json"
STATS_FILE   = DATA_DIR / "stats.json"
MAX_SEEN_IDS = 5000

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

def get_int_setting(env_name: str, config_key: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is not None:
        return int(raw)
    return int(CONFIG.get(config_key, default))

CONFIG = load_config()

BOT_TOKEN   = os.getenv("BOT_TOKEN") or CONFIG.get("bot_token")
CHAT_ID     = str(os.getenv("CHAT_ID") or CONFIG.get("chat_id", ""))
DELAY_MIN   = get_int_setting("DELAY_MIN_SECONDS", "delay_min_seconds", 50)
DELAY_MAX   = get_int_setting("DELAY_MAX_SECONDS", "delay_max_seconds", 110)
MAX_AGE_MIN = get_int_setting("MAX_AD_AGE_MINUTES", "max_ad_age_minutes", 30)

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing BOT_TOKEN or CHAT_ID. Set them in environment variables "
        "or in config.json."
    )

state: dict = {
    "paused": False,
    "current_filter_idx": 0,
    "last_check": None,
    "next_check": None,
}

# ── JSON utils ──────────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load JSON from %s: %s", path, e)
    return default

def save_json(path: Path, data):
    # ВИПРАВЛЕНО: атомарний запис через .tmp — не псує файл при краші
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

def load_seen() -> set:
    return set(load_json(SEEN_FILE, []))

def save_seen(seen: set):
    save_json(SEEN_FILE, list(seen))

def trim_seen(seen: set) -> set:
    # ВИПРАВЛЕНО: обмеження розміру seen_ids.json — без цього файл росте вічно
    if len(seen) > MAX_SEEN_IDS:
        lst = load_json(SEEN_FILE, [])
        return set(lst[-MAX_SEEN_IDS:])
    return seen

def load_filters() -> list:
    dynamic = load_json(FILTERS_FILE, [])
    static  = CONFIG.get("filters", [])
    # ВИПРАВЛЕНО: порівняння dict напряму ненадійне — порівнюємо по url
    static_urls = {f.get("url") for f in static}
    unique_dynamic = [f for f in dynamic if f.get("url") not in static_urls]
    return static + unique_dynamic

def save_dynamic_filters(lst: list):
    save_json(FILTERS_FILE, lst)

def load_stats() -> dict:
    return load_json(STATS_FILE, {
        "total_sent": 0,
        "total_checked": 0,
        "errors": 0,
        "started_at": datetime.now(KYIV_TZ).isoformat(),
    })

def save_stats(s: dict):
    save_json(STATS_FILE, s)

# ── Markdown escape ─────────────────────────────────────────────────────────

def escape_md(text: str) -> str:
    # ВИПРАВЛЕНО: назви товарів містять * _ ` [ — без екранування
    # Telegram кидає "Bad Request: can't parse entities"
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text

# ── Час оголошення ──────────────────────────────────────────────────────────

def parse_ad_time(location_date_text: str) -> datetime | None:
    now  = datetime.now(KYIV_TZ)
    text = location_date_text.strip().lower()
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    # ВИПРАВЛЕНО: валідація часу перед створенням datetime
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if any(w in text for w in ("сьогодні", "сегодня", "today")):
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if any(w in text for w in ("учора", "вчера", "yesterday")):
        return (now - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return None

def is_fresh(ad: dict, max_age_minutes: int) -> bool:
    ad_time = parse_ad_time(ad.get("location", ""))
    if ad_time is None:
        return False
    age = (datetime.now(KYIV_TZ) - ad_time).total_seconds() / 60
    # ВИПРАВЛЕНО: допускаємо -2 хв (розбіжність годинників сервера і OLX)
    return -2 <= age <= max_age_minutes

# ── HTTP ─────────────────────────────────────────────────────────────────────

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
        # ВИПРАВЛЕНО: текст помилки може містити Markdown-символи (URL, _)
        safe = text.replace("_", "\\_").replace("*", "\\*")
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🆘 *ПОМИЛКА ПАРСЕРА*\n\n{safe}",
            parse_mode=ParseMode.MARKDOWN,
        )
        s = load_stats(); s["errors"] += 1; save_stats(s)
    except Exception as e:
        log.error("Не вдалося надіслати помилку: %s", e)

async def fetch_page(session: aiohttp.ClientSession, url: str, app: Application) -> str | None:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                return await r.text()
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

# ── Парсинг ──────────────────────────────────────────────────────────────────

def parse_listings(html: str) -> list[dict]:
    soup    = BeautifulSoup(html, "lxml")
    results = []
    for card in soup.select("div[data-cy='l-card']"):
        try:
            ad_id = card.get("id", "").strip()
            if not ad_id:
                continue

            title_el = (
                card.select_one("[data-testid='ad-title']")
                or card.select_one("h4")
                or card.select_one("h6")
            )
            title = title_el.get_text(strip=True) if title_el else "Без назви"

            price_el = (
                card.select_one("p[data-testid='ad-price']")
                or card.select_one(".price-label")
            )
            price = price_el.get_text(strip=True) if price_el else "Ціна не вказана"

            link_el = card.select_one("a[href]")
            link    = link_el["href"] if link_el else ""
            if link and not link.startswith("http"):
                link = "https://www.olx.ua" + link
            if link:
                link = link.split("?")[0]

            # ВИПРАВЛЕНО: підтримка lazy-load (data-src)
            img_el = card.select_one("img")
            image  = None
            if img_el:
                for attr in ("src", "data-src"):
                    val = img_el.get(attr, "")
                    if val and "placeholder" not in val and not val.startswith("data:"):
                        image = val
                        break

            location_el = card.select_one("p[data-testid='location-date']")
            location    = location_el.get_text(strip=True) if location_el else ""

            results.append({
                "id": ad_id, "title": title, "price": price,
                "link": link, "image": image, "location": location,
            })
        except Exception as e:
            log.debug("Картка: %s", e)
    return results

# ── Відправка ────────────────────────────────────────────────────────────────

def build_message(ad: dict, label: str = "") -> str:
    now_str = datetime.now(KYIV_TZ).strftime("%H:%M")
    lines = []
    if label:
        lines.append(f"🔍 _{escape_md(label)}_")
    lines.append(f"🆕 *{escape_md(ad['title'])}*")
    lines.append(f"💰 {escape_md(ad['price'])}")
    if ad.get("location"):
        lines.append(f"📍 {escape_md(ad['location'])}")
    lines.append(f"🕐 Знайдено о {now_str} (Київ)")
    if ad.get("link"):
        lines.append(f"🔗 [Переглянути]({ad['link']})")
    return "\n".join(lines)

async def send_ad(app: Application, ad: dict, label: str = ""):
    text = build_message(ad, label)
    try:
        if ad.get("image"):
            await app.bot.send_photo(chat_id=CHAT_ID, photo=ad["image"],
                                     caption=text, parse_mode=ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=CHAT_ID, text=text,
                                       parse_mode=ParseMode.MARKDOWN)
        log.info("✉️  %s | %s", ad["title"], ad["price"])
        s = load_stats(); s["total_sent"] += 1; save_stats(s)
    except Exception as e:
        log.warning("Фото не вийшло (%s), пробую без", e)
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=text,
                                       parse_mode=ParseMode.MARKDOWN)
        except Exception as e2:
            log.error("Відправка: %s", e2)

# ── Перевірка фільтра ────────────────────────────────────────────────────────

async def check_filter(
    session: aiohttp.ClientSession,
    app: Application,
    seen: set,
    filter_cfg: dict,
    max_age: int,
) -> tuple[int, int]:
    url   = filter_cfg["url"]
    label = filter_cfg.get("label", "")

    html = await fetch_page(session, url, app)
    if not html:
        return 0, 0

    ads = parse_listings(html)
    log.info("«%s»: %d карток", label or url[:50], len(ads))

    s = load_stats(); s["total_checked"] += len(ads); save_stats(s)

    new_count = skip_count = 0
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
    seen.clear(); seen.update(seen_trimmed)
    save_seen(seen)

    log.info("Нових: %d | пропущено старих: %d", new_count, skip_count)
    return new_count, skip_count

# ── Команди ──────────────────────────────────────────────────────────────────

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        # ВИПРАВЛЕНО: update.message може бути None (edited_message тощо)
        if update.message is None:
            return
        if str(update.effective_chat.id) != CHAT_ID:
            await update.message.reply_text("⛔ Доступ заборонено.")
            return
        return await func(update, ctx)
    return wrapper

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        "🗑 /clearseen — скинути переглянуті",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl      = load_filters()
    seen    = load_seen()
    max_age = ctx.bot_data.get("max_age", MAX_AGE_MIN)
    paused  = "⏸ Призупинено" if state["paused"] else "✅ Активний"
    last_s  = state["last_check"].strftime("%H:%M:%S") if state["last_check"] else "—"
    next_s  = state["next_check"].strftime("%H:%M:%S") if state["next_check"] else "—"
    idx     = state["current_filter_idx"] % len(fl) if fl else 0
    cur     = fl[idx].get("label", f"#{idx+1}") if fl else "немає"
    await update.message.reply_text(
        f"📡 *Стан бота*\n\n"
        f"Статус: {paused}\n"
        f"Фільтрів: {len(fl)}\n"
        f"Переглянутих ID: {len(seen)}\n"
        f"Макс. вік оголошення: {max_age} хв\n"
        f"Затримка: {DELAY_MIN}–{DELAY_MAX} сек\n\n"
        f"Остання перевірка: {last_s}\n"
        f"Наступна перевірка: {next_s}\n"
        f"Поточний фільтр: _{escape_md(cur)}_",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl = load_filters()
    if not fl:
        await update.message.reply_text("📋 Немає фільтрів. Додай через /add")
        return
    static_n = len(CONFIG.get("filters", []))
    lines = ["📋 *Фільтри:*\n"]
    for i, f in enumerate(fl):
        icon  = "📌" if i < static_n else "➕"
        label = f.get("label", "без назви")
        url   = f.get("url", "")
        lines.append(f"{icon} *{i+1}.* {escape_md(label)}\n`{url[:70]}`\n")
    lines.append("📌 config.json  |  ➕ через бот\nВидалити: /remove `N`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

@admin_only
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = " ".join(ctx.args).strip()
    if "|" not in args:
        await update.message.reply_text(
            "⚠️ Формат: `/add Назва | URL`\n\nПриклад:\n`/add iPhone 14 | https://www.olx.ua/uk/...`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    parts = args.split("|", 1)
    label = parts[0].strip()
    url   = parts[1].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ URL має починатись з `https://`", parse_mode=ParseMode.MARKDOWN)
        return
    # ВИПРАВЛЕНО: перевірка дублікатів перед додаванням
    if any(f.get("url") == url for f in load_filters()):
        await update.message.reply_text("⚠️ Фільтр з таким URL вже існує\\!")
        return
    dynamic = load_json(FILTERS_FILE, [])
    dynamic.append({"label": label, "url": url})
    save_dynamic_filters(dynamic)
    await update.message.reply_text(
        f"✅ Додано!\n*{escape_md(label)}*\n`{url}`", parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("⚠️ Вкажи номер: `/remove 3`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        idx = int(ctx.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер має бути числом"); return
    fl = load_filters()
    static_n = len(CONFIG.get("filters", []))
    if idx < 0 or idx >= len(fl):
        await update.message.reply_text(f"❌ Немає фільтра #{idx+1}"); return
    if idx < static_n:
        await update.message.reply_text(
            "⚠️ Це статичний фільтр з `config.json`.\nРедагуй файл вручну і перезапусти бота.",
            parse_mode=ParseMode.MARKDOWN,
        ); return
    dynamic = load_json(FILTERS_FILE, [])
    removed = dynamic.pop(idx - static_n)
    save_dynamic_filters(dynamic)
    await update.message.reply_text(
        f"🗑 Видалено: *{escape_md(removed.get('label', removed['url']))}*",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("⏸ Парсинг призупинено. /resume — відновити")

@admin_only
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("▶️ Парсинг відновлено!")

@admin_only
async def cmd_setage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        cur = ctx.bot_data.get("max_age", MAX_AGE_MIN)
        await update.message.reply_text(
            f"⏱ Поточний макс. вік: *{cur} хв*\nЗмінити: `/setage 20`",
            parse_mode=ParseMode.MARKDOWN,
        ); return
    try:
        m = int(ctx.args[0])
        if not (1 <= m <= 1440): raise ValueError
        ctx.bot_data["max_age"] = m
        await update.message.reply_text(f"✅ Макс. вік: *{m} хв*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Число від 1 до 1440")

@admin_only
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fl = load_filters()
    if not fl:
        await update.message.reply_text("📋 Немає фільтрів"); return
    idx = state["current_filter_idx"] % len(fl)
    f   = fl[idx]
    await update.message.reply_text(
        f"🔍 Перевіряю: *{escape_md(f.get('label', 'фільтр'))}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    seen    = load_seen()
    max_age = ctx.bot_data.get("max_age", MAX_AGE_MIN)
    async with aiohttp.ClientSession() as session:
        new, skipped = await check_filter(session, ctx.application, seen, f, max_age)
    await update.message.reply_text(
        f"✅ Готово!\nНових: *{new}* | Пропущено старих: *{skipped}*",
        parse_mode=ParseMode.MARKDOWN,
    )

@admin_only
async def cmd_clearseen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(load_seen())
    save_seen(set())
    await update.message.reply_text(f"🗑 Очищено {n} ID. Наступна перевірка покаже свіжі оголошення.")

async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message and str(update.effective_chat.id) == CHAT_ID:
        await update.message.reply_text("❓ Невідома команда. /start — довідка")

# ── Цикл парсингу ────────────────────────────────────────────────────────────

async def parser_loop(app: Application):
    log.info("🔄 Цикл парсингу запущений")
    seen = load_seen()

    # ВИПРАВЛЕНО: TCPConnector з обмеженням з'єднань — не спамимо OLX
    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
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

            idx     = state["current_filter_idx"] % len(fl)
            f       = fl[idx]
            max_age = app.bot_data.get("max_age", MAX_AGE_MIN)

            state["last_check"] = datetime.now(KYIV_TZ)

            try:
                await check_filter(session, app, seen, f, max_age)
            except Exception as e:
                log.error("Цикл: %s", e)
                await notify_error(app, f"❌ *Критична помилка циклу*\n`{e}`")

            state["current_filter_idx"] += 1
            delay = random.randint(DELAY_MIN, DELAY_MAX)

            next_fl  = load_filters()
            next_idx = state["current_filter_idx"] % len(next_fl) if next_fl else 0
            next_lbl = next_fl[next_idx].get("label", f"#{next_idx+1}") if next_fl else "—"
            state["next_check"] = datetime.now(KYIV_TZ) + timedelta(seconds=delay)

            log.info("⏳ Наступний: «%s» через %d сек.", next_lbl, delay)
            await asyncio.sleep(delay)

# ── Init / main ──────────────────────────────────────────────────────────────

async def post_init(app: Application):
    app.bot_data.setdefault("max_age", MAX_AGE_MIN)
    fl = load_filters()
    await app.bot.send_message(
        chat_id=CHAT_ID,
        text=(
            "✅ *OLX\\-бот запущений\\!*\n\n"
            f"📋 Фільтрів: {len(fl)}\n"
            f"⏱ Макс\\. вік: {app.bot_data['max_age']} хв\n"
            f"🔄 Затримка: {DELAY_MIN}–{DELAY_MAX} сек\n\n"
            "Напиши /start щоб побачити команди"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    asyncio.create_task(parser_loop(app))

def main():
    log.info("🚀 Запуск...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    for cmd, handler in [
        ("start",     cmd_start),
        ("help",      cmd_start),
        ("status",    cmd_status),
        ("filters",   cmd_filters),
        ("add",       cmd_add),
        ("remove",    cmd_remove),
        ("pause",     cmd_pause),
        ("resume",    cmd_resume),
        ("setage",    cmd_setage),
        ("stats",     cmd_stats),
        ("check",     cmd_check),
        ("clearseen", cmd_clearseen),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(tg_filters.COMMAND, cmd_unknown))
    log.info("Polling started")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
