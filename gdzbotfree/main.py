import logging
import os
import re
import tempfile
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

try:
    import pytesseract
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter
except ImportError:
    pytesseract = None
    Image = None

BOT_TOKEN = os.getenv("BOT_TOKEN", "8725558943:AAHE9LufLAbjgomA33rZZWMNpUzljL8TwSE").strip()
GDZ_DOMAIN = "gdz.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 15
MAX_RESULTS = 6

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("gdz-bot")


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:700]


def normalize_search_text(text: str) -> str:
    text = clean_text(text)
    # Remove common filler words but keep subject, class, author, exercise numbers.
    text = re.sub(r"[^\w\s№.,:;!?()\-А-Яа-яЁё]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def build_query(text: str, grade: str | None = None, subject: str | None = None) -> str:
    parts = []
    if grade:
        parts.append(f"{grade} класс")
    if subject:
        parts.append(subject)
    if text:
        parts.append(text)
    parts.append("site:gdz.ru")
    return " ".join(parts)


def unwrap_ddg_url(href: str) -> str:
    """DuckDuckGo sometimes wraps the destination in a redirect URL."""
    if not href:
        return ""
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        params = parse_qs(parsed.query)
        raw = params.get("uddg", [""])[0]
        return unquote(raw) if raw else ""
    return href


def search_gdz(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """
    Search results are discovered through a search index, but only links hosted
    on gdz.ru are returned. No other sites are used as answer sources.
    """
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    response = requests.get(
        url,
        params={"q": query, "kl": "ru-ru"},
        headers=headers,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    found = []
    seen = set()

    for item in soup.select(".result"):
        a = item.select_one(".result__a")
        if not a:
            continue
        target = unwrap_ddg_url(a.get("href", ""))
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower()
        if host != GDZ_DOMAIN and not host.endswith("." + GDZ_DOMAIN):
            continue
        if target in seen:
            continue
        seen.add(target)
        snippet_node = item.select_one(".result__snippet")
        title = clean_text(a.get_text(" ", strip=True)) or "Страница на ГДЗ.ру"
        snippet = clean_text(snippet_node.get_text(" ", strip=True)) if snippet_node else ""
        found.append({"title": title, "url": target, "snippet": snippet})
        if len(found) >= limit:
            break

    return found


def ocr_image(path: str) -> str:
    if pytesseract is None or Image is None:
        raise RuntimeError(
            "OCR не установлен. Установи Pillow, pytesseract и системный "
            "Tesseract с русским языковым пакетом."
        )
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("L")
        image = ImageEnhance.Contrast(image).enhance(1.7)
        image = image.filter(ImageFilter.SHARPEN)
        # Use Russian + English; Tesseract language packs must be installed.
        result = pytesseract.image_to_string(image, lang="rus+eng", config="--psm 6")
    return clean_text(result)


def parse_grade_subject(text: str) -> tuple[str | None, str | None, str]:
    grade_match = re.search(r"\b(1[01]|[1-9])\s*(?:-?\s*й\s*)?класс", text, re.I)
    grade = f"{grade_match.group(1)}" if grade_match else None

    subjects = {
        "русский язык": ["русский язык", "русс яз", "русский"],
        "математика": ["математика", "матем"],
        "алгебра": ["алгебра"],
        "геометрия": ["геометрия", "геом"],
        "история": ["история", "истор"],
        "география": ["география", "геогр"],
        "английский язык": ["английский язык", "англ яз", "английский"],
        "литература": ["литература", "литер"],
        "биология": ["биология", "биол"],
        "физика": ["физика", "физ"],
        "химия": ["химия", "хим"],
        "обществознание": ["обществознание", "общество"],
        "информатика": ["информатика", "информ"],
    }
    lowered = text.lower()
    subject = None
    for canonical, aliases in subjects.items():
        if any(alias in lowered for alias in aliases):
            subject = canonical
            break

    remaining = text
    if grade_match:
        remaining = remaining.replace(grade_match.group(0), " ")
    if subject:
        for alias in subjects[subject]:
            remaining = re.sub(re.escape(alias), " ", remaining, flags=re.I)
    remaining = re.sub(r"\s+", " ", remaining).strip()
    return grade, subject, remaining


def format_results(results: list[dict], query: str) -> str:
    if not results:
        return (
            "Не нашёл подходящих страниц на ГДЗ.ру по этому запросу.\n\n"
            "Попробуй добавить предмет, класс, автора учебника или номер упражнения. "
            "Для контрольной укажи тему или параграф."
        )
    lines = ["Нашёл страницы на ГДЗ.ру:"]
    for i, item in enumerate(results, 1):
        lines.append(f"\n{i}. {item['title']}\n{item['url']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet'][:220]}")
    lines.append(
        "\nПроверь автора и издание учебника: похожие задания могут отличаться."
    )
    return "\n".join(lines)[:3900]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я ищу учебные задания и похожие контрольные только на ГДЗ.ру.\n\n"
        "Примеры:\n"
        "• упр 13 7 класс русский язык\n"
        "• история 7 класс параграф 5 контрольная\n"
        "• математика 6 класс Виленкин номер 245\n\n"
        "Можешь прислать фотографию задания. Я распознаю текст и по нему поищу "
        "подходящие страницы. Точное совпадение контрольной не гарантируется.\n\n"
        "Команды: /help — помощь, /search — поиск текстом."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1. Напиши предмет, класс и номер упражнения либо тему/параграф.\n"
        "2. Или отправь чёткое фото целой страницы.\n"
        "3. Я распознаю текст и ищу страницы на ГДЗ.ру.\n\n"
        "Если результатов нет, добавь автора учебника, название темы или номер "
        "параграфа. Бот не обходит авторизацию и не получает закрытые материалы."
    )


async def do_search(update: Update, query_text: str, grade=None, subject=None) -> None:
    status = await update.message.reply_text("Ищу на ГДЗ.ру…")
    query = build_query(query_text, grade=grade, subject=subject)
    try:
        results = search_gdz(query)
        answer = format_results(results, query)
        await status.edit_text(answer, disable_web_page_preview=True)
    except requests.RequestException as exc:
        log.warning("Search failed: %s", exc)
        await status.edit_text(
            "Не получилось выполнить поиск прямо сейчас. Попробуй ещё раз позже "
            "или открой https://gdz.ru/ вручную."
        )
    except Exception:
        log.exception("Unexpected search error")
        await status.edit_text("Возникла ошибка при поиске. Попробуй ещё раз.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = normalize_search_text(update.message.text or "")
    if not text:
        await update.message.reply_text("Напиши предмет, класс и номер задания.")
        return
    grade, subject, rest = parse_grade_subject(text)
    # Keep the original terms in the search query, even if parsing is uncertain.
    await do_search(update, text, grade=grade, subject=subject)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    caption = message.caption or ""
    photo = message.photo[-1] if message.photo else None
    if not photo:
        await message.reply_text("Не вижу фотографию. Отправь фото как изображение.")
        return

    status = await message.reply_text("Получил фото. Распознаю текст…")
    try:
        file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
            path = temp.name
        try:
            await file.download_to_drive(path)
            extracted = ocr_image(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        if not extracted or len(extracted) < 4:
            await status.edit_text(
                "Не удалось прочитать текст на фото. Пришли снимок ровнее, "
                "с хорошим освещением, чтобы текст был крупным и резким."
            )
            return

        combined = normalize_search_text(f"{caption} {extracted}")
        grade, subject, _ = parse_grade_subject(combined)
        await status.edit_text(
            "Распознал текст:\n"
            f"{extracted[:900]}\n\n"
            "Ищу подходящие страницы на ГДЗ.ру…"
        )
        query = build_query(combined, grade=grade, subject=subject)
        results = search_gdz(query)
        answer = format_results(results, query)
        await message.reply_text(answer, disable_web_page_preview=True)
    except RuntimeError as exc:
        await status.edit_text(str(exc))
    except requests.RequestException as exc:
        log.warning("Photo search failed: %s", exc)
        await status.edit_text(
            "Не удалось выполнить поиск. Проверь доступ хостинга к интернету "
            "и попробуй ещё раз."
        )
    except Exception:
        log.exception("Photo handler failed")
        await status.edit_text(
            "Не получилось обработать фото. Проверь, что установлены Tesseract "
            "и русский языковой пакет, и попробуй снова."
        )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Пример: /search история 7 класс параграф 4 контрольная"
        )
        return
    await do_search(update, normalize_search_text(query))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Добавь токен бота в переменные окружения хостинга."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("Starting bot with long polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
