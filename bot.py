"""
KworkBot — Telegram-бот для мониторинга фриланс-заказов на kwork.ru
"""

# ============================================================
# [IMPORTS & CONFIG]
# ============================================================
import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
YOUR_TELEGRAM_ID = int(os.getenv("YOUR_TELEGRAM_ID", "0"))
PARSE_INTERVAL_MINUTES = int(os.getenv("PARSE_INTERVAL_MINUTES", "15"))

DB_PATH = "db.sqlite3"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("KworkBot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Флаг паузы мониторинга
monitoring_paused = False

# Целевые категории kwork.ru
KWORK_CATEGORIES = [
    {"url": "https://kwork.ru/projects?c=41", "name": "Сайты / лендинги"},
    {"url": "https://kwork.ru/projects?c=19", "name": "Скрипты / боты"},
    {"url": "https://kwork.ru/projects?c=36", "name": "Telegram-боты"},
    {"url": "https://kwork.ru/projects?c=102", "name": "Чат-боты / ИИ"},
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


# ============================================================
# [DATABASE]
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kwork_id    TEXT    UNIQUE NOT NULL,
                title       TEXT,
                description TEXT,
                budget      TEXT,
                deadline    TEXT,
                category    TEXT,
                url         TEXT,
                is_suitable INTEGER DEFAULT 0,
                sent        INTEGER DEFAULT 0,
                created_at  TEXT
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована.")


async def is_duplicate(kwork_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM orders WHERE kwork_id = ?", (kwork_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def save_order(order: dict, is_suitable: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO orders
                (kwork_id, title, description, budget, deadline, category, url, is_suitable, sent, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                order["kwork_id"],
                order["title"],
                order["description"],
                order["budget"],
                order["deadline"],
                order["category"],
                order["url"],
                is_suitable,
                datetime.now().isoformat(),
            ),
        )
        await db.commit()


async def mark_sent(kwork_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET sent = 1 WHERE kwork_id = ?", (kwork_id,)
        )
        await db.commit()


async def get_order_by_id(kwork_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE kwork_id = ?", (kwork_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        since = (datetime.now() - timedelta(hours=24)).isoformat()

        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ?", (since,)
        ) as cur:
            total_24h = (await cur.fetchone())[0]

        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND is_suitable = 1", (since,)
        ) as cur:
            suitable_24h = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM orders") as cur:
            total_all = (await cur.fetchone())[0]

        async with db.execute("SELECT COUNT(*) FROM orders WHERE is_suitable = 1") as cur:
            suitable_all = (await cur.fetchone())[0]

    return {
        "total_24h": total_24h,
        "suitable_24h": suitable_24h,
        "total_all": total_all,
        "suitable_all": suitable_all,
    }


# ============================================================
# [PARSER]
# ============================================================

async def fetch_page(url: str, client: httpx.AsyncClient) -> str | None:
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        response = await client.get(url, headers=headers, timeout=20, follow_redirects=True)
        if response.status_code == 200:
            return response.text
        elif response.status_code in (403, 429):
            logger.warning(f"Блокировка ({response.status_code}) для {url}. Ожидание 5 минут.")
            await asyncio.sleep(300)
            return None
        else:
            logger.warning(f"Статус {response.status_code} для {url}")
            return None
    except httpx.RequestError as e:
        logger.error(f"Ошибка запроса к {url}: {e}")
        return None


def parse_cards(html: str, category_name: str) -> list[dict]:
    """Парсинг карточек заказов из HTML страницы kwork.ru"""
    soup = BeautifulSoup(html, "lxml")
    cards = []

    # kwork.ru использует карточки с классом want-card или подобным
    # Пробуем несколько селекторов под возможную разметку
    card_selectors = [
        "div.want-card",
        "article.project-card",
        "div[data-id]",
        "div.card",
    ]

    project_cards = []
    for selector in card_selectors:
        project_cards = soup.select(selector)
        if project_cards:
            break

    # Fallback: ищем ссылки на /projects/ в HTML
    if not project_cards:
        return parse_cards_fallback(soup, category_name)

    for card in project_cards:
        try:
            # ID заказа
            kwork_id = card.get("data-id") or card.get("id", "")
            if not kwork_id:
                link = card.find("a", href=re.compile(r"/projects/\d+"))
                if link:
                    m = re.search(r"/projects/(\d+)", link["href"])
                    kwork_id = m.group(1) if m else ""

            if not kwork_id:
                continue

            # Заголовок
            title_el = card.find(["h2", "h3", "a"], class_=re.compile(r"title|name|heading", re.I))
            title = title_el.get_text(strip=True) if title_el else "Без названия"

            # Описание
            desc_el = card.find(["p", "div"], class_=re.compile(r"desc|text|body|content", re.I))
            description = desc_el.get_text(strip=True) if desc_el else ""

            # Бюджет
            budget_el = card.find(["span", "div"], class_=re.compile(r"price|budget|cost|money", re.I))
            budget = budget_el.get_text(strip=True) if budget_el else "Не указан"

            # Дедлайн
            deadline_el = card.find(["span", "div"], class_=re.compile(r"deadline|date|time", re.I))
            deadline = deadline_el.get_text(strip=True) if deadline_el else "Не указан"

            # URL
            link_el = card.find("a", href=re.compile(r"/projects/\d+"))
            if link_el:
                href = link_el["href"]
                url = href if href.startswith("http") else f"https://kwork.ru{href}"
            else:
                url = f"https://kwork.ru/projects/{kwork_id}/"

            cards.append({
                "kwork_id": str(kwork_id),
                "title": title,
                "description": description[:1000],
                "budget": budget,
                "deadline": deadline,
                "category": category_name,
                "url": url,
            })
        except Exception as e:
            logger.debug(f"Ошибка парсинга карточки: {e}")
            continue

    return cards


def parse_cards_fallback(soup: BeautifulSoup, category_name: str) -> list[dict]:
    """Резервный парсер: ищет все ссылки на проекты"""
    cards = []
    seen_ids = set()

    for link in soup.find_all("a", href=re.compile(r"/projects/\d+")):
        m = re.search(r"/projects/(\d+)", link["href"])
        if not m:
            continue

        kwork_id = m.group(1)
        if kwork_id in seen_ids:
            continue
        seen_ids.add(kwork_id)

        title = link.get_text(strip=True) or "Без названия"
        parent = link.find_parent(["div", "article", "li"])
        description = ""
        budget = "Не указан"
        deadline = "Не указан"

        if parent:
            texts = [t.strip() for t in parent.stripped_strings if t.strip()]
            description = " ".join(texts[:5])[:800]

        href = link["href"]
        url = href if href.startswith("http") else f"https://kwork.ru{href}"

        cards.append({
            "kwork_id": kwork_id,
            "title": title,
            "description": description,
            "budget": budget,
            "deadline": deadline,
            "category": category_name,
            "url": url,
        })

    return cards


async def parse_kwork():
    """Главный цикл парсинга. Вызывается планировщиком."""
    global monitoring_paused
    if monitoring_paused:
        logger.info("Мониторинг на паузе, пропускаем итерацию.")
        return

    logger.info("Запуск парсинга kwork.ru...")
    new_found = 0

    async with httpx.AsyncClient() as client:
        for category in KWORK_CATEGORIES:
            html = await fetch_page(category["url"], client)
            if not html:
                continue

            cards = parse_cards(html, category["name"])
            logger.info(f"Категория '{category['name']}': найдено {len(cards)} карточек.")

            for card in cards:
                if not card["kwork_id"]:
                    continue

                if await is_duplicate(card["kwork_id"]):
                    continue

                # AI-фильтр
                suitable = await is_suitable_for_vibe(card)
                await save_order(card, int(suitable))

                if suitable:
                    await send_notification(card)
                    await mark_sent(card["kwork_id"])
                    new_found += 1

                # Случайная задержка между запросами к AI (защита от RPM-лимитов)
                await asyncio.sleep(random.uniform(1.5, 3.5))

            # Задержка между категориями
            await asyncio.sleep(random.uniform(2, 5))

    logger.info(f"Парсинг завершён. Новых подходящих заказов: {new_found}")


# ============================================================
# [AI FILTER]
# ============================================================

async def groq_request(messages: list[dict], max_tokens: int = 10) -> str | None:
    """Базовый запрос к Groq API."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(GROQ_API_URL, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}")
        return None


async def is_suitable_for_vibe(order: dict) -> bool:
    """AI-фильтр: подходит ли заказ для вайб-кодинга?"""
    prompt = f"""Ты эксперт по фрилансу. Определи, подходит ли следующий заказ для \
«вайб-кодинга» — то есть выполним ли он полностью с помощью ИИ-инструментов: \
написание кода, создание Telegram-бота, сайта, скрипта, автоматизации, парсера.

Заказ подходит, если требует: создание сайта, лендинга, веб-приложения, \
Telegram-бота, чат-бота, скрипта, парсера, автоматизации, API-интеграции.

Заказ НЕ подходит, если требует: ручной дизайн (Figma/Photoshop), \
написание текстов/статей, SEO-продвижение, консалтинг, звонки, \
верстка только по макету без логики.

Название: {order['title']}
Описание: {order['description']}
Категория: {order['category']}

Ответь строго одним словом: ДА или НЕТ"""

    result = await groq_request([{"role": "user", "content": prompt}], max_tokens=5)
    if result is None:
        return False
    return result.upper().startswith("ДА")


# ============================================================
# [NOTIFIER]
# ============================================================

async def send_notification(order: dict) -> None:
    """Отправка уведомления пользователю."""
    text = (
        f"🆕 <b>Новый заказ для вайб-кодинга!</b>\n\n"
        f"📌 <b>Название:</b> {order['title']}\n\n"
        f"📝 <b>Описание:</b>\n{order['description']}\n\n"
        f"💰 <b>Бюджет:</b> <code>{order['budget']}</code>\n"
        f"⏰ <b>Дедлайн:</b> {order['deadline']}\n"
        f"🗂 <b>Категория:</b> {order['category']}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✍️ Сгенерировать отклик",
                    callback_data=f"generate_response:{order['kwork_id']}",
                ),
                InlineKeyboardButton(
                    text="🔗 Перейти к заказу",
                    url=order["url"],
                ),
            ]
        ]
    )

    try:
        await bot.send_message(
            chat_id=YOUR_TELEGRAM_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления: {e}")


# ============================================================
# [AI RESPONSE GENERATOR]
# ============================================================

async def generate_response_text(order: dict) -> str | None:
    """Генерация профессионального отклика через Groq API."""
    prompt = f"""Ты — опытный фрилансер на kwork.ru, специализируешься на разработке: \
сайтов, Telegram-ботов, скриптов, автоматизации с помощью ИИ-инструментов.

Напиши профессиональный, живой отклик на следующий заказ.
Отклик должен:
- Начинаться с приветствия и краткого понимания задачи
- Показывать экспертизу (опыт с похожими проектами)
- Предлагать конкретное решение
- Быть уверенным, без лишней скромности
- Длина: 5-8 предложений
- Язык: русский

Название заказа: {order['title']}
Описание: {order['description']}
Бюджет: {order['budget']}"""

    return await groq_request(
        [{"role": "user", "content": prompt}],
        max_tokens=600,
    )


# ============================================================
# [HANDLERS]
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return
    text = (
        "👋 <b>KworkBot запущен!</b>\n\n"
        "Я мониторю kwork.ru каждые <b>{interval} минут</b> и нахожу заказы "
        "для вайб-кодинга — проекты, которые можно выполнить с помощью ИИ.\n\n"
        "<b>Команды:</b>\n"
        "/status — текущий статус мониторинга\n"
        "/stats — статистика за 24 часа\n"
        "/pause — приостановить мониторинг\n"
        "/resume — возобновить мониторинг"
    ).format(interval=PARSE_INTERVAL_MINUTES)
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return
    status = "⏸ На паузе" if monitoring_paused else "✅ Активен"
    stats = await get_stats()
    text = (
        f"<b>Статус мониторинга:</b> {status}\n"
        f"<b>Интервал:</b> каждые {PARSE_INTERVAL_MINUTES} мин.\n\n"
        f"<b>Всего заказов в БД:</b> {stats['total_all']}\n"
        f"<b>Подходящих (всего):</b> {stats['suitable_all']}"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return
    stats = await get_stats()
    text = (
        f"📊 <b>Статистика за последние 24 часа</b>\n\n"
        f"🔍 Найдено заказов: <b>{stats['total_24h']}</b>\n"
        f"✅ Подходящих для вайб-кодинга: <b>{stats['suitable_24h']}</b>\n\n"
        f"📦 <b>Всего в базе:</b> {stats['total_all']} заказов, "
        f"из них подходящих: {stats['suitable_all']}"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("pause"))
async def cmd_pause(message: Message):
    global monitoring_paused
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return
    monitoring_paused = True
    await message.answer("⏸ Мониторинг <b>приостановлен</b>. Используй /resume для возобновления.", parse_mode="HTML")


@dp.message(Command("resume"))
async def cmd_resume(message: Message):
    global monitoring_paused
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return
    monitoring_paused = False
    await message.answer("▶️ Мониторинг <b>возобновлён</b>!", parse_mode="HTML")


@dp.callback_query(F.data.startswith("generate_response:"))
async def callback_generate_response(callback: CallbackQuery):
    if callback.from_user.id != YOUR_TELEGRAM_ID:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    kwork_id = callback.data.split(":", 1)[1]
    await callback.answer("⏳ Генерирую отклик...")

    order = await get_order_by_id(kwork_id)
    if not order:
        await callback.message.answer("❌ Заказ не найден в базе данных.")
        return

    await callback.message.answer("⏳ <i>Генерирую отклик, подождите...</i>", parse_mode="HTML")

    response_text = await generate_response_text(order)
    if not response_text:
        await callback.message.answer("❌ Не удалось сгенерировать отклик. Попробуйте позже.")
        return

    result_text = (
        f"✍️ <b>Отклик для заказа:</b>\n"
        f"<i>{order['title']}</i>\n\n"
        f"<code>{response_text}</code>"
    )

    await callback.message.answer(result_text, parse_mode="HTML")


# ============================================================
# [MAIN]
# ============================================================

async def main():
    await init_db()

    # Запуск планировщика
    scheduler.add_job(
        parse_kwork,
        trigger="interval",
        minutes=PARSE_INTERVAL_MINUTES,
        id="parse_kwork_job",
        next_run_time=datetime.now(),  # первый запуск сразу
    )
    scheduler.start()
    logger.info(f"Планировщик запущен. Интервал: {PARSE_INTERVAL_MINUTES} мин.")

    # Запуск бота
    logger.info("Бот запущен.")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())