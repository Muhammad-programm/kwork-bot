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
KWORK_COOKIES = os.getenv("KWORK_COOKIES", "")  # необязательно: cookies сессии kwork.ru

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

# Категории для API kwork.ru (числовые ID совпадают с ?c= параметром)
KWORK_API_URL = "https://kwork.ru/projects/api/v2/wants"

async def fetch_page(url: str, client: httpx.AsyncClient) -> str | None:
    """HTTP GET с ротацией User-Agent. Возвращает текст ответа или None."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": "https://kwork.ru/",
    }
    if KWORK_COOKIES:
        headers["Cookie"] = KWORK_COOKIES
    try:
        response = await client.get(url, headers=headers, timeout=25, follow_redirects=True)
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


async def fetch_api(category_id: str, client: httpx.AsyncClient) -> list[dict]:
    """
    Пробуем получить заказы через внутренний JSON API kwork.ru.
    Возвращает список сырых объектов заказов или [] при ошибке.
    """
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://kwork.ru/projects?c={category_id}",
        "X-Requested-With": "XMLHttpRequest",
    }
    if KWORK_COOKIES:
        headers["Cookie"] = KWORK_COOKIES
    params = {"c": category_id, "page": 1}
    try:
        response = await client.get(
            KWORK_API_URL, headers=headers, params=params, timeout=25, follow_redirects=True
        )
        if response.status_code == 200:
            data = response.json()
            # Структура ответа: {"success": true, "data": {"wants": [...]}}
            wants = (
                data.get("data", {}).get("wants")
                or data.get("wants")
                or []
            )
            logger.info(f"API вернул {len(wants)} заказов для категории {category_id}")
            return wants
        else:
            logger.debug(f"API статус {response.status_code} для категории {category_id}")
            return []
    except Exception as e:
        logger.debug(f"Ошибка API для категории {category_id}: {e}")
        return []


def normalize_api_order(raw: dict, category_name: str) -> dict | None:
    """Преобразует сырой объект из API kwork.ru в наш формат."""
    try:
        kwork_id = str(raw.get("id", ""))
        if not kwork_id:
            return None

        title = raw.get("name") or raw.get("title") or "Без названия"
        description = raw.get("description") or raw.get("desc") or ""
        # Бюджет может быть в разных полях
        budget_val = raw.get("priceLimit") or raw.get("price") or raw.get("budget") or ""
        budget = f"{budget_val} ₽" if budget_val else "Не указан"
        # Дедлайн
        deadline = raw.get("dateEnd") or raw.get("deadline") or "Не указан"

        url = raw.get("url") or f"https://kwork.ru/projects/{kwork_id}/"
        if not url.startswith("http"):
            url = f"https://kwork.ru{url}"

        return {
            "kwork_id": kwork_id,
            "title": str(title).strip(),
            "description": str(description).strip()[:1000],
            "budget": str(budget).strip(),
            "deadline": str(deadline).strip(),
            "category": category_name,
            "url": url,
        }
    except Exception as e:
        logger.debug(f"Ошибка нормализации заказа: {e}")
        return None


def parse_cards_from_html(html: str, category_name: str) -> list[dict]:
    """
    HTML-парсер с диагностическим логированием.
    Перебирает известные классы разметки kwork.ru.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- Диагностика: показываем что реально есть в HTML ---
    all_divs_with_data_id = soup.find_all(attrs={"data-id": True})
    project_links = soup.find_all("a", href=re.compile(r"/projects/\d+"))
    logger.debug(
        f"HTML-диагностика: data-id элементов={len(all_divs_with_data_id)}, "
        f"ссылок на /projects/={{len(project_links)}}"
    )

    # Расширенный список известных классов карточек kwork.ru
    card_selectors = [
        # Актуальные классы (2024-2025)
        "div.want-card",
        "div.wants-card",
        "div.project-card",
        "li.want-card",
        "li.project-card",
        # По data-атрибутам
        "div[data-id][data-want-id]",
        "div[data-want-id]",
        "[data-id][class*='want']",
        "[data-id][class*='project']",
        # Общие контейнеры
        "article[class*='card']",
        "div[class*='want-card']",
        "div[class*='project']",
    ]

    project_cards = []
    matched_selector = None
    for selector in card_selectors:
        try:
            found = soup.select(selector)
            if found:
                project_cards = found
                matched_selector = selector
                break
        except Exception:
            continue

    if matched_selector:
        logger.debug(f"Сработал селектор: '{matched_selector}', карточек: {len(project_cards)}")
    else:
        logger.debug("CSS-селекторы не сработали, использую ссылочный fallback.")
        return parse_cards_link_fallback(soup, category_name, project_links)

    cards = []
    for card in project_cards:
        try:
            # ID
            kwork_id = (
                card.get("data-want-id")
                or card.get("data-id")
                or card.get("id", "")
            )
            if not kwork_id:
                link = card.find("a", href=re.compile(r"/projects/\d+"))
                if link:
                    m = re.search(r"/projects/(\d+)", link["href"])
                    kwork_id = m.group(1) if m else ""
            if not kwork_id:
                continue

            # Заголовок — ищем первый <a> или заголовочный тег внутри карточки
            title = ""
            for title_sel in [
                "[class*='title']", "[class*='name']", "[class*='heading']",
                "h2", "h3", "h4",
            ]:
                el = card.select_one(title_sel)
                if el:
                    title = el.get_text(strip=True)
                    break
            if not title:
                link_el = card.find("a", href=re.compile(r"/projects/\d+"))
                title = link_el.get_text(strip=True) if link_el else "Без названия"

            # Описание
            description = ""
            for desc_sel in [
                "[class*='desc']", "[class*='text']",
                "[class*='body']", "[class*='content']", "p",
            ]:
                el = card.select_one(desc_sel)
                if el:
                    description = el.get_text(strip=True)
                    break

            # Бюджет
            budget = "Не указан"
            for price_sel in [
                "[class*='price']", "[class*='budget']",
                "[class*='cost']", "[class*='money']", "[class*='pay']",
            ]:
                el = card.select_one(price_sel)
                if el:
                    budget = el.get_text(strip=True)
                    break

            # Дедлайн
            deadline = "Не указан"
            for dl_sel in [
                "[class*='deadline']", "[class*='date']",
                "[class*='time']", "[class*='period']",
            ]:
                el = card.select_one(dl_sel)
                if el:
                    deadline = el.get_text(strip=True)
                    break

            # URL
            link_el = card.find("a", href=re.compile(r"/projects/\d+"))
            if link_el:
                href = link_el["href"]
                url = href if href.startswith("http") else f"https://kwork.ru{href}"
            else:
                url = f"https://kwork.ru/projects/{kwork_id}/"

            cards.append({
                "kwork_id": str(kwork_id),
                "title": title[:300],
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


def parse_cards_link_fallback(
    soup: BeautifulSoup,
    category_name: str,
    project_links: list | None = None,
) -> list[dict]:
    """
    Финальный резервный парсер: находит заказы по ссылкам /projects/{id}/.
    Работает даже при полностью изменённой разметке.
    """
    cards = []
    seen_ids: set[str] = set()

    if project_links is None:
        project_links = soup.find_all("a", href=re.compile(r"/projects/\d+"))

    logger.debug(f"Ссылочный fallback: найдено {len(project_links)} ссылок на проекты")

    for link in project_links:
        href = link.get("href", "")
        m = re.search(r"/projects/(\d+)", href)
        if not m:
            continue

        kwork_id = m.group(1)
        if kwork_id in seen_ids:
            continue
        seen_ids.add(kwork_id)

        # Пробуем получить заголовок из текста ссылки
        title = link.get_text(strip=True)
        # Если ссылка — это просто иконка или кнопка без текста — ищем родителя
        if not title or len(title) < 5:
            parent = link.find_parent(["article", "li", "div"])
            if parent:
                # Ищем первый содержательный текст
                for child in parent.descendants:
                    if hasattr(child, "get_text"):
                        txt = child.get_text(strip=True)
                        if len(txt) > 10:
                            title = txt
                            break
        if not title:
            title = "Без названия"

        # Описание из ближайшего родительского блока
        description = ""
        budget = "Не указан"
        deadline = "Не указан"
        parent = link.find_parent(["article", "li", "div"])
        if parent:
            texts = [t.strip() for t in parent.stripped_strings if len(t.strip()) > 3]
            # Исключаем сам заголовок из описания
            desc_parts = [t for t in texts if t != title][:8]
            description = " ".join(desc_parts)[:800]

            # Грубый поиск бюджета — ищем числа с ₽ или руб
            budget_match = re.search(r"(\d[\d\s]*(?:₽|руб))", parent.get_text())
            if budget_match:
                budget = budget_match.group(1).strip()

        url = href if href.startswith("http") else f"https://kwork.ru{href}"

        cards.append({
            "kwork_id": kwork_id,
            "title": title[:300],
            "description": description,
            "budget": budget,
            "deadline": deadline,
            "category": category_name,
            "url": url,
        })

    return cards


async def fetch_category(category: dict, client: httpx.AsyncClient) -> list[dict]:
    """
    Основная функция получения заказов по одной категории.
    Стратегия: сначала пробуем JSON API, при неудаче — HTML-парсинг.
    """
    cat_id = category["url"].split("c=")[-1]
    cat_name = category["name"]

    # Стратегия 1: JSON API
    raw_orders = await fetch_api(cat_id, client)
    if raw_orders:
        cards = []
        for raw in raw_orders:
            card = normalize_api_order(raw, cat_name)
            if card:
                cards.append(card)
        if cards:
            return cards

    # Стратегия 2: HTML-парсинг
    html = await fetch_page(category["url"], client)
    if not html:
        return []

    # Диагностика: логируем размер HTML и первые видимые классы
    logger.debug(f"HTML размер: {len(html)} байт")

    cards = parse_cards_from_html(html, cat_name)

    # Если ничего не нашли — сохраняем фрагмент HTML для отладки
    if not cards:
        snippet = html[:2000].replace("\n", " ")
        logger.warning(
            f"[{cat_name}] 0 карточек. Начало HTML: {snippet[:500]}..."
        )

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
            cards = await fetch_category(category, client)
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
        "/resume — возобновить мониторинг\n"
        "/debug — диагностика парсера"
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


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    """Диагностический запрос к kwork.ru — показывает что реально находит парсер."""
    if message.from_user.id != YOUR_TELEGRAM_ID:
        return

    await message.answer("🔍 <i>Запускаю диагностику парсера...</i>", parse_mode="HTML")

    # Берём первую категорию для теста
    category = KWORK_CATEGORIES[0]
    cat_id = category["url"].split("c=")[-1]

    async with httpx.AsyncClient() as client:
        # Тест JSON API
        raw_orders = await fetch_api(cat_id, client)
        api_count = len(raw_orders)

        # Тест HTML
        html = await fetch_page(category["url"], client)
        html_size = len(html) if html else 0
        html_cards = parse_cards_from_html(html, category["name"]) if html else []

        # Ищем любые ссылки на /projects/ в HTML
        project_links_count = 0
        has_auth_redirect = False
        if html:
            soup = BeautifulSoup(html, "lxml")
            project_links_count = len(soup.find_all("a", href=re.compile(r"/projects/\d+")))
            # Проверка на редирект авторизации
            has_auth_redirect = bool(
                soup.find("form", action=re.compile(r"login|auth", re.I))
                or "войдите" in html.lower()
                or "авторизуйтесь" in html.lower()
                or re.search(r"/login|/auth|sign.in", html.lower())
            )

    lines = [
        f"🔎 <b>Диагностика парсера</b>",
        f"Категория: <code>{category['name']}</code>",
        "",
        f"<b>JSON API:</b> {api_count} заказов",
        f"<b>HTML размер:</b> {html_size:,} байт",
        f"<b>HTML карточек:</b> {len(html_cards)}",
        f"<b>Ссылок /projects/:</b> {project_links_count}",
        f"<b>Стена авторизации:</b> {'⚠️ ДА' if has_auth_redirect else '✅ НЕТ'}",
    ]

    if api_count == 0 and html_cards == 0 and project_links_count == 0:
        lines += [
            "",
            "⚠️ <b>Вероятная причина:</b> kwork.ru требует авторизацию "
            "или рендерит контент через JavaScript (SPA). "
            "Обычный HTTP-парсер не может получить карточки.",
            "",
            "💡 <b>Решение:</b> добавить cookies авторизованной сессии "
            "в заголовки запросов (см. README).",
        ]

    await message.answer("\n".join(lines), parse_mode="HTML")


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
