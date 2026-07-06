import telebot
import json
import os
import re
import time
import hashlib
import logging
import requests
from io import BytesIO
from openai import OpenAI
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# ENV
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
KINOPOISK_API_KEY = os.environ.get("KINOPOISK_API_KEY", "").strip()

ADMIN_IDS = {
    admin_id.strip()
    for admin_id in os.environ.get("ADMIN_IDS", "").split(",")
    if admin_id.strip()
}

# Ссылка на CloudTips задаётся в Render → Environment → DONATE_URL
DONATE_URL = os.environ.get("DONATE_URL", "").strip()

# Текст доната задаётся в Render → Environment → DONATE_TEXT
DONATE_TEXT = os.environ.get(
    "DONATE_TEXT",
    "💛 Поддержать MixCinema\n\n"
    "Если бот оказался полезным, можно поддержать проект любой суммой.\n\n"
    "Спасибо за поддержку 🤍"
).strip()

# Готовое объявление для рассылки после починки Кинопоиск API
BOT_FIXED_ANNOUNCEMENT = (
    "🎬 Друзья, бот снова работает нормально!\n\n"
    "В последние дни у части пользователей могли не загружаться постеры и рекомендации — "
    "мы упёрлись в лимит запросов к Кинопоиск API.\n\n"
    "Сейчас лимит обновили, всё починили: подборки, постеры и описания снова должны работать стабильно.\n\n"
    "Спасибо, что пользуетесь ботом 🤍"
)

DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_TOKEN в переменных окружения")

if not OPENAI_API_KEY:
    raise RuntimeError("Не найден OPENAI_API_KEY в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# FILES
# =========================

MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RATINGS_FILE = os.path.join(DATA_DIR, "ratings.json")


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception(f"Ошибка чтения {path}")
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception(f"Ошибка сохранения {path}")


memory = load_json(MEMORY_FILE, {})
state = load_json(STATE_FILE, {
    "seen_movies": {},
    "user_profile": {},
    "callback_movies": {},
    "poster_cache": {},
    "film_detail_cache": {},
    "stats": {}
})
ratings = load_json(RATINGS_FILE, {})

state.setdefault("seen_movies", {})
state.setdefault("user_profile", {})
state.setdefault("callback_movies", {})
state.setdefault("poster_cache", {})
state.setdefault("film_detail_cache", {})
state.setdefault("stats", {})
state["stats"].setdefault("users", {})
state["stats"].setdefault("total_messages", 0)
state["stats"].setdefault("total_callbacks", 0)

# =========================
# SAVE
# =========================

def save_memory():
    save_json(MEMORY_FILE, memory)


def save_state():
    save_json(STATE_FILE, state)


def save_ratings():
    save_json(RATINGS_FILE, ratings)


# =========================
# HELPERS
# =========================

def send_long_message(chat_id, text, reply_markup=None):
    max_len = 3900
    text = str(text)

    if len(text) <= max_len:
        bot.send_message(chat_id, text, reply_markup=reply_markup)
        return

    parts = []
    current = ""

    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= max_len:
            current += paragraph + "\n\n"
        else:
            if current.strip():
                parts.append(current.strip())
            current = paragraph + "\n\n"

    if current.strip():
        parts.append(current.strip())

    for i, part in enumerate(parts):
        bot.send_message(
            chat_id,
            part,
            reply_markup=reply_markup if i == len(parts) - 1 else None
        )


def get_seen(user_id):
    state["seen_movies"].setdefault(user_id, [])

    if isinstance(state["seen_movies"][user_id], set):
        state["seen_movies"][user_id] = list(state["seen_movies"][user_id])

    return state["seen_movies"][user_id]


def get_profile(user_id):
    state["user_profile"].setdefault(user_id, {
        "likes": [],
        "dislikes": []
    })

    state["user_profile"][user_id].setdefault("likes", [])
    state["user_profile"][user_id].setdefault("dislikes", [])

    return state["user_profile"][user_id]


def add_unique(items, value, limit=100):
    value = str(value).strip()

    if not value:
        return items

    if value in items:
        items.remove(value)

    items.append(value)
    return items[-limit:]


def remove_if_exists(items, value):
    if value in items:
        items.remove(value)

    return items


def add_seen(user_id, movie_name):
    seen = get_seen(user_id)
    state["seen_movies"][user_id] = add_unique(seen, movie_name, limit=150)


def extract_movie_name(block):
    lines = [line.strip() for line in block.strip().splitlines() if line.strip()]

    if not lines:
        return "Фильм"

    first = lines[0]
    first = re.sub(r"^\d+[\).\-\s]+", "", first)
    first = first.replace("🎬", "").strip()
    first = re.sub(r"^Название:\s*", "", first, flags=re.IGNORECASE).strip()

    return first[:90] or "Фильм"


def normalize_movie_key(movie_name):
    text = str(movie_name).lower().strip().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        text = hashlib.sha1(str(movie_name).encode("utf-8")).hexdigest()

    return text[:120]


def clean_title_for_search(movie_name):
    title = str(movie_name)
    title = title.replace("🎬", "").strip()
    title = re.sub(r"\(\d{4}\)", "", title).strip()
    title = re.sub(r"^\d+[\).\-\s]+", "", title).strip()
    title = re.sub(r"\s+", " ", title).strip()

    return title


def extract_year_from_title(movie_name):
    match = re.search(r"\((\d{4})\)", str(movie_name))
    if match:
        return match.group(1)
    return None


def make_callback_key(movie_name):
    raw = f"{movie_name}:{time.time()}"
    key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    state["callback_movies"][key] = movie_name

    if len(state["callback_movies"]) > 3000:
        last_items = list(state["callback_movies"].items())[-2000:]
        state["callback_movies"] = dict(last_items)

    save_state()
    return key


def get_movie_by_callback_key(key):
    return state.get("callback_movies", {}).get(key)


def get_film_rating_value(film):
    raw = film.get("ratingKinopoisk") or film.get("rating") or film.get("ratingImdb") or 0

    try:
        return float(str(raw).replace(",", "."))
    except Exception:
        return 0.0


def film_has_poster(film):
    return bool(film.get("posterUrlPreview") or film.get("posterUrl"))


def shorten_text(text, limit=260):
    text = str(text or "").strip()

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)

    if len(text) <= limit:
        return text

    return text[:limit].rsplit(" ", 1)[0] + "..."


def get_kinopoisk_id(film):
    return (
        film.get("kinopoiskId")
        or film.get("filmId")
        or film.get("id")
    )


def get_kinopoisk_details(kinopoisk_id):
    """
    Загружает полную карточку фильма из Кинопоиска и кеширует её.
    В старых версиях в film_detail_cache могли храниться строки описаний —
    такие записи игнорируются и заменяются полной карточкой.
    """
    if not kinopoisk_id or not KINOPOISK_API_KEY:
        return None

    cache_key = str(kinopoisk_id)
    cached = state.get("film_detail_cache", {}).get(cache_key)

    if isinstance(cached, dict):
        return cached

    url = f"https://kinopoiskapiunofficial.tech/api/v2.2/films/{kinopoisk_id}"
    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=12)
        response.raise_for_status()
        details = response.json()
    except Exception:
        logging.exception(f"Не удалось получить карточку фильма {kinopoisk_id}")
        return None

    state["film_detail_cache"][cache_key] = details
    save_state()
    return details


def get_film_description(film):
    """
    Возвращает только описание из Кинопоиска.
    Ничего не генерирует и не дописывает самостоятельно.
    """
    if not film:
        return "Описание на Кинопоиске отсутствует."

    description = (
        film.get("shortDescription")
        or film.get("description")
        or ""
    )

    if description:
        return shorten_text(description, limit=320)

    kinopoisk_id = get_kinopoisk_id(film)
    details = get_kinopoisk_details(kinopoisk_id)

    if details:
        description = (
            details.get("shortDescription")
            or details.get("description")
            or ""
        )

        if description:
            return shorten_text(description, limit=320)

    return "Описание на Кинопоиске отсутствует."


# =========================
# COUNTRY FILTERS
# =========================

# Индия специально добавлена в РФ/СНГ-группу:
# значит индийские фильмы НЕ попадут в "Зарубежные по годам",
# а будут попадать в "РФ/СНГ + Индия".
CIS_COUNTRY_KEYWORDS = [
    "россия",
    "russia",
    "ссср",
    "ussr",
    "советский союз",
    "индия",
    "india",
    "украина",
    "ukraine",
    "беларусь",
    "belarus",
    "казахстан",
    "kazakhstan",
    "армения",
    "armenia",
    "азербайджан",
    "azerbaijan",
    "грузия",
    "georgia",
    "молдова",
    "moldova",
    "узбекистан",
    "uzbekistan",
    "кыргызстан",
    "киргизия",
    "kyrgyzstan",
    "таджикистан",
    "tajikistan",
    "туркменистан",
    "turkmenistan",
    "эстония",
    "estonia",
    "латвия",
    "latvia",
    "литва",
    "lithuania"
]


def is_cis_or_post_soviet_film(film):
    countries = film.get("countries", []) or []

    for country in countries:
        name = str(country.get("country", "")).lower().strip()

        for keyword in CIS_COUNTRY_KEYWORDS:
            if keyword in name:
                return True

    return False


def passes_year_category_filter(film, category):
    is_cis_group = is_cis_or_post_soviet_film(film)

    if category == "foreign":
        return not is_cis_group

    if category == "cis":
        return is_cis_group

    return True


# =========================
# YEAR TOP EXCLUSIONS
# =========================

EXCLUDED_YEAR_TOP_KEYWORDS = [
    "bbc",
    "би-би-си",
    "би би си"
]


def is_excluded_from_year_top(film):
    """
    Убирает из подборок по годам нежелательные фильмы/документалки,
    например BBC.
    """
    text_parts = [
        film.get("nameRu") or "",
        film.get("nameOriginal") or "",
        film.get("nameEn") or "",
        film.get("description") or "",
        film.get("shortDescription") or ""
    ]

    text = " ".join(text_parts).lower()

    for keyword in EXCLUDED_YEAR_TOP_KEYWORDS:
        if keyword in text:
            return True

    return False


# =========================
# ADMIN
# =========================

def is_admin_user_id(user_id):
    return str(user_id) in ADMIN_IDS


def is_admin_message(message):
    return message.from_user and is_admin_user_id(message.from_user.id)


def register_user_activity(message=None, call=None):
    if message:
        user = message.from_user
        chat_id = str(message.chat.id)
        activity_type = "message"
    elif call:
        user = call.from_user
        chat_id = str(call.message.chat.id)
        activity_type = "callback"
    else:
        return

    if not user:
        return

    user_id = str(user.id)

    state["stats"]["users"].setdefault(user_id, {
        "chat_id": chat_id,
        "username": "",
        "first_name": "",
        "last_name": "",
        "messages": 0,
        "callbacks": 0,
        "first_seen": int(time.time()),
        "last_seen": int(time.time())
    })

    record = state["stats"]["users"][user_id]
    record["chat_id"] = chat_id
    record["username"] = user.username or ""
    record["first_name"] = user.first_name or ""
    record["last_name"] = user.last_name or ""
    record["last_seen"] = int(time.time())

    if activity_type == "message":
        record["messages"] = int(record.get("messages", 0)) + 1
        state["stats"]["total_messages"] = int(state["stats"].get("total_messages", 0)) + 1

    if activity_type == "callback":
        record["callbacks"] = int(record.get("callbacks", 0)) + 1
        state["stats"]["total_callbacks"] = int(state["stats"].get("total_callbacks", 0)) + 1

    save_state()


def get_admin_stats_text():
    users_count = len(state.get("stats", {}).get("users", {}))

    total_likes = 0
    total_dislikes = 0
    rated_movies = 0

    for record in ratings.values():
        likes = int(record.get("likes", 0))
        dislikes = int(record.get("dislikes", 0))

        if likes + dislikes > 0:
            rated_movies += 1

        total_likes += likes
        total_dislikes += dislikes

    poster_cache_count = len(state.get("poster_cache", {}))
    detail_cache_count = len(state.get("film_detail_cache", {}))
    memory_users_count = len(memory)
    callback_count = len(state.get("callback_movies", {}))

    return (
        "👑 Админ-статистика KinoBot AI\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"💬 Сообщений: {state['stats'].get('total_messages', 0)}\n"
        f"🔘 Нажатий кнопок: {state['stats'].get('total_callbacks', 0)}\n\n"
        f"🎬 Фильмов с оценками: {rated_movies}\n"
        f"👍 Лайков всего: {total_likes}\n"
        f"👎 Дизлайков всего: {total_dislikes}\n\n"
        f"🖼 Постеров в кэше: {poster_cache_count}\n"
        f"🧾 Описаний в кэше: {detail_cache_count}\n"
        f"🔑 Callback-ключей: {callback_count}\n"
        f"🧠 Пользователей в memory.json: {memory_users_count}"
    )


# =========================
# POSTERS
# =========================

def download_poster_image(poster_url):
    if not poster_url:
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(
            poster_url,
            headers=headers,
            timeout=7
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        if "image" not in content_type:
            logging.warning(f"Poster URL вернул не картинку: {content_type}")
            return None

        image = BytesIO(response.content)
        image.name = "poster.jpg"
        image.seek(0)

        return image

    except Exception:
        logging.exception("Не удалось скачать постер")
        return None


def title_similarity_score(search_title, film):
    search_clean = normalize_movie_key(search_title)

    name_ru = film.get("nameRu") or ""
    name_en = film.get("nameEn") or ""
    name_original = film.get("nameOriginal") or ""

    names = [
        normalize_movie_key(name_ru),
        normalize_movie_key(name_en),
        normalize_movie_key(name_original)
    ]

    score = 0

    for name in names:
        if not name:
            continue

        if name == search_clean:
            score = max(score, 100)
        elif name.startswith(search_clean + " "):
            score = max(score, 80)
        elif f" {search_clean} " in f" {name} ":
            score = max(score, 60)
        elif search_clean in name:
            score = max(score, 30)

    return score


def find_kinopoisk_film(movie_name):
    """
    Ищет точный фильм по русскому названию и году,
    затем возвращает полную карточку Кинопоиска.
    """
    if not KINOPOISK_API_KEY:
        return None

    search_title = clean_title_for_search(movie_name)
    search_year = extract_year_from_title(movie_name)

    if not search_title:
        return None

    url = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params={"keyword": search_title, "page": 1},
            timeout=12
        )
        response.raise_for_status()
        films = response.json().get("films", [])
    except Exception:
        logging.exception(f"Ошибка поиска фильма в Кинопоиске: {movie_name}")
        return None

    candidates = []

    for film in films:
        score = title_similarity_score(search_title, film)
        film_year = str(film.get("year") or "")

        if search_year and film_year == search_year:
            score += 100
        elif search_year and film_year and film_year != search_year:
            score -= 60

        film_type = str(film.get("type") or "").lower()
        if "film" in film_type or "фильм" in film_type:
            score += 10
        elif "series" in film_type or "serial" in film_type:
            score -= 25

        candidates.append((score, film))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_film = candidates[0]

    # Лучше пропустить фильм, чем показать карточку другого фильма.
    if best_score < 80:
        logging.warning(
            f"Нет точного совпадения Кинопоиска для {movie_name}: score={best_score}"
        )
        return None

    return get_kinopoisk_details(get_kinopoisk_id(best_film))


def find_kinopoisk_poster(movie_name):
    if not KINOPOISK_API_KEY:
        return None

    search_title = clean_title_for_search(movie_name)
    search_year = extract_year_from_title(movie_name)

    if not search_title:
        return None

    cache_key = normalize_movie_key(movie_name)

    cached = state.get("poster_cache", {}).get(cache_key)
    if cached:
        return cached

    url = "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    params = {
        "keyword": search_title,
        "page": 1
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logging.exception("Ошибка поиска постера в Kinopoisk API")
        return None

    films = data.get("films", [])

    if not films:
        return None

    candidates = []

    for film in films:
        score = title_similarity_score(search_title, film)
        film_year = str(film.get("year") or "")

        if search_year and film_year == search_year:
            score += 50
        elif search_year and film_year and film_year != search_year:
            score -= 20

        film_type = str(film.get("type") or "").lower()

        if "film" in film_type or "фильм" in film_type:
            score += 10
        elif "series" in film_type or "serial" in film_type:
            score -= 30

        poster_url = film.get("posterUrlPreview") or film.get("posterUrl")

        if poster_url:
            candidates.append((score, film, poster_url))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)

    best_score, best_film, poster_url = candidates[0]

    if best_score < 50:
        logging.warning(f"Слабое совпадение постера для {movie_name}: score={best_score}")
        return None

    state["poster_cache"][cache_key] = poster_url
    save_state()

    return poster_url


def send_movie_card(chat_id, text, movie_name, poster_url=None):
    markup = movie_markup(movie_name)

    if poster_url:
        try:
            image = download_poster_image(poster_url)

            if image:
                if len(text) <= 950:
                    bot.send_photo(
                        chat_id,
                        image,
                        caption=text,
                        reply_markup=markup
                    )
                else:
                    bot.send_photo(chat_id, image)
                    bot.send_message(
                        chat_id,
                        text,
                        reply_markup=markup
                    )

                return

        except Exception:
            logging.exception("Не удалось отправить постер в Telegram")

    bot.send_message(
        chat_id,
        text,
        reply_markup=markup
    )


# =========================
# RATINGS
# =========================

def get_rating_record(movie_name):
    movie_key = normalize_movie_key(movie_name)

    ratings.setdefault(movie_key, {
        "title": movie_name,
        "likes": 0,
        "dislikes": 0,
        "users": {}
    })

    ratings[movie_key].setdefault("title", movie_name)
    ratings[movie_key].setdefault("likes", 0)
    ratings[movie_key].setdefault("dislikes", 0)
    ratings[movie_key].setdefault("users", {})

    return movie_key, ratings[movie_key]


def vote_movie(user_id, movie_name, vote):
    movie_key, record = get_rating_record(movie_name)
    users = record["users"]
    old_vote = users.get(user_id)

    if old_vote == vote:
        return record, "same"

    if old_vote == "like":
        record["likes"] = max(0, record["likes"] - 1)
    elif old_vote == "dislike":
        record["dislikes"] = max(0, record["dislikes"] - 1)

    if vote == "like":
        record["likes"] += 1
    elif vote == "dislike":
        record["dislikes"] += 1
    else:
        raise ValueError("Некорректный тип голоса")

    users[user_id] = vote
    record["title"] = movie_name
    ratings[movie_key] = record

    profile = get_profile(user_id)

    if vote == "like":
        profile["likes"] = add_unique(profile["likes"], movie_name, limit=100)
        profile["dislikes"] = remove_if_exists(profile["dislikes"], movie_name)
    else:
        profile["dislikes"] = add_unique(profile["dislikes"], movie_name, limit=100)
        profile["likes"] = remove_if_exists(profile["likes"], movie_name)

    save_ratings()
    save_state()

    return record, "changed"


def format_rating_line(movie_name):
    _, record = get_rating_record(movie_name)
    likes = record.get("likes", 0)
    dislikes = record.get("dislikes", 0)

    return f"👍 {likes}   👎 {dislikes}"


def get_user_top_movies(limit=50):
    if not ratings:
        return "🏆 Топ пользователей пока пуст. Поставь лайки фильмам, и рейтинг начнёт собираться."

    items = []

    for record in ratings.values():
        likes = int(record.get("likes", 0))
        dislikes = int(record.get("dislikes", 0))
        total = likes + dislikes

        if total == 0:
            continue

        score = likes - dislikes
        approval = round((likes / total) * 100)

        items.append({
            "title": record.get("title", "Фильм"),
            "likes": likes,
            "dislikes": dislikes,
            "score": score,
            "approval": approval,
            "total": total
        })

    if not items:
        return "🏆 Топ пользователей пока пуст. Поставь лайки фильмам, и рейтинг начнёт собираться."

    items.sort(
        key=lambda x: (x["score"], x["likes"], -x["dislikes"], x["approval"]),
        reverse=True
    )

    text = "🏆 Топ-50 фильмов по оценкам пользователей\n\n"

    for i, item in enumerate(items[:limit], start=1):
        text += (
            f"{i}. 🎬 {item['title']}\n"
            f"👍 {item['likes']}   👎 {item['dislikes']}   ⭐️ Одобрение: {item['approval']}%\n\n"
        )

    return text.strip()


# =========================
# KEYBOARDS
# =========================

def menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)

    markup.add(
        InlineKeyboardButton("🎬 Фильмы", callback_data="menu_movies"),
        InlineKeyboardButton("📺 Сериалы", callback_data="menu_series"),
        InlineKeyboardButton("🔥 Новинки 2026", callback_data="menu_new_2026"),
        InlineKeyboardButton("🎭 Жанры", callback_data="menu_genres"),
        InlineKeyboardButton("🌍 Зарубежные по годам", callback_data="menu_years_foreign"),
        InlineKeyboardButton("🇷🇺 РФ/СНГ + Индия", callback_data="menu_years_cis"),
        InlineKeyboardButton("🏆 Топ Кинопоиска", callback_data="menu_kp_top"),
        InlineKeyboardButton("⭐️ Топ пользователей", callback_data="menu_user_top"),
        InlineKeyboardButton("🎲 Случайный", callback_data="menu_random"),
        InlineKeyboardButton("🧠 Мой вкус", callback_data="menu_profile"),
        InlineKeyboardButton("💛 Донат", callback_data="menu_donate"),
        InlineKeyboardButton("♻️ Начать подбор заново", callback_data="menu_reset")
    )

    return markup


def donate_markup():
    markup = InlineKeyboardMarkup(row_width=1)

    if DONATE_URL:
        markup.add(
            InlineKeyboardButton("💳 Поддержать через CloudTips", url=DONATE_URL)
        )

    markup.add(
        InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")
    )

    return markup


def genres_markup():
    markup = InlineKeyboardMarkup(row_width=2)

    genres = [
        ("😂 Комедия", "комедия"),
        ("🧠 Триллер", "триллер"),
        ("😱 Ужасы", "ужасы"),
        ("🚀 Фантастика", "фантастика"),
        ("🕵️ Детектив", "детектив"),
        ("💥 Боевик", "боевик"),
        ("💘 Романтика", "романтика"),
        ("🎭 Драма", "драма"),
        ("👨‍👩‍👧 Семейные", "семейный фильм"),
        ("🎨 Мультфильмы", "мультфильм"),
        ("🐉 Фэнтези", "фэнтези"),
        ("🤖 Аниме", "аниме")
    ]

    for title, genre in genres:
        markup.add(InlineKeyboardButton(title, callback_data=f"genre:{genre}"))

    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_back"))

    return markup


def years_markup(category):
    markup = InlineKeyboardMarkup(row_width=2)

    if category == "foreign":
        markup.add(
            InlineKeyboardButton("🔥 Новинки 2026", callback_data="year_foreign:2026:0")
        )
    else:
        markup.add(
            InlineKeyboardButton("🇷🇺 РФ/СНГ + Индия 2026", callback_data="year_cis:2026:0")
        )

    years = list(range(2025, 1989, -1))

    for year in years:
        if category == "foreign":
            callback = f"year_foreign:{year}:0"
        else:
            callback = f"year_cis:{year}:0"

        markup.add(
            InlineKeyboardButton(str(year), callback_data=callback)
        )

    markup.add(InlineKeyboardButton("⬅️ Назад", callback_data="menu_back"))

    return markup


def year_more_markup(category, year, next_offset):
    markup = InlineKeyboardMarkup(row_width=1)

    if category == "foreign":
        callback = f"year_foreign:{year}:{next_offset}"
        choose_callback = "menu_years_foreign"
        text = f"➡️ Ещё 5 зарубежных фильмов {year}"
    else:
        callback = f"year_cis:{year}:{next_offset}"
        choose_callback = "menu_years_cis"
        text = f"➡️ Ещё 5 РФ/СНГ + Индия {year}"

    markup.add(
        InlineKeyboardButton(text, callback_data=callback)
    )

    markup.add(
        InlineKeyboardButton("📅 Выбрать другой год", callback_data=choose_callback)
    )

    return markup


def movie_markup(movie_name):
    key = make_callback_key(movie_name)

    markup = InlineKeyboardMarkup(row_width=2)

    markup.add(
        InlineKeyboardButton("🎞 Похожие", callback_data=f"sim:{key}")
    )

    markup.add(
        InlineKeyboardButton("👍 Лайк", callback_data=f"like:{key}"),
        InlineKeyboardButton("👎 Дизлайк", callback_data=f"dislike:{key}")
    )

    return markup


# =========================
# OPENAI
# =========================

def ask_ai(user_prompt):
    system_prompt = """
Ты выбираешь фильмы и сериалы для Telegram-бота.

Жёсткие правила:
- отвечай только на русском языке
- количество кандидатов указано в запросе пользователя
- не пиши описание, сюжет, рейтинг, жанры или причину выбора
- не добавляй английское название
- обязательно указывай точный год выхода
- каждый фильм или сериал начинай с 🎬
- каждый вариант отделяй пустой строкой
- не повторяй варианты внутри одного ответа

Формат строго такой:
🎬 Название (год)
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    )

    return response.output_text.strip()


def build_prompt(user_id, user_text, mode="search", candidate_count=10, extra_excluded=None):
    seen = get_seen(user_id)
    profile = get_profile(user_id)
    extra_excluded = extra_excluded or []

    if mode == "similar":
        task = (
            f"Подбери {candidate_count} разных фильмов, максимально похожих на: "
            f"{user_text}."
        )
    elif mode == "series":
        task = f"Подбери {candidate_count} сильных сериалов."
    elif mode == "random":
        task = "Подбери 1 случайный хороший фильм, который реально стоит посмотреть."
    elif mode == "genre":
        task = f"Подбери {candidate_count} лучших фильмов в жанре: {user_text}."
    elif mode == "movies":
        task = f"Подбери {candidate_count} хороших фильмов как умную Netflix-подборку."
    else:
        task = (
            f"Запрос пользователя: {user_text}. "
            f"Подбери {candidate_count} подходящих фильмов или сериалов."
        )

    return f"""
{task}

Уже показывали пользователю:
{seen[-100:]}

Уже предложены в текущей попытке, их тоже не повторяй:
{extra_excluded[-50:]}

Пользователю нравится:
{profile.get('likes', [])[-30:]}

Пользователю не нравится:
{profile.get('dislikes', [])[-30:]}

Важно:
- не повторяй уже показанные фильмы
- не повторяй варианты из текущей попытки
- не добавляй английские названия
- не предлагай то, что пользователь дизлайкнул
- указывай точный год выхода
- выдай только названия и годы в требуемом формате
"""


def split_recommendation_blocks(answer):
    raw = re.split(r"(?=🎬)", str(answer))
    return [
        block.strip()
        for block in raw
        if block.strip() and "🎬" in block
    ]


def send_recommendations(chat_id, user_id, user_text, mode="search"):
    """
    Всегда старается отправить нужное количество карточек.

    OpenAI даёт запас кандидатов, а фильмы без точного совпадения
    в Кинопоиске пропускаются. Если карточек не хватает, выполняется
    дополнительная попытка с новыми кандидатами.
    """
    bot.send_chat_action(chat_id, "typing")

    target_count = 1 if mode == "random" else 5
    candidate_count = 1 if mode == "random" else 10
    max_attempts = 1 if mode == "random" else 3

    sent_count = 0
    attempted_names = []
    sent_movie_keys = set()
    all_answers = []

    for attempt in range(max_attempts):
        if sent_count >= target_count:
            break

        prompt = build_prompt(
            user_id=user_id,
            user_text=user_text,
            mode=mode,
            candidate_count=candidate_count,
            extra_excluded=attempted_names
        )

        answer = ask_ai(prompt)
        all_answers.append(answer)
        blocks = split_recommendation_blocks(answer)

        if not blocks:
            logging.warning(
                "OpenAI не вернул распознаваемые названия, попытка %s",
                attempt + 1
            )
            continue

        for block in blocks:
            if sent_count >= target_count:
                break

            requested_movie_name = extract_movie_name(block)

            if not requested_movie_name:
                continue

            requested_key = normalize_movie_key(requested_movie_name)

            if requested_key in {
                normalize_movie_key(name) for name in attempted_names
            }:
                continue

            attempted_names.append(requested_movie_name)

            kinopoisk_film = find_kinopoisk_film(requested_movie_name)

            if not kinopoisk_film:
                logging.warning(
                    "Пропускаю фильм без точной карточки Кинопоиска: %s",
                    requested_movie_name
                )
                continue

            movie_name, text = format_kinopoisk_film(kinopoisk_film)
            movie_key = normalize_movie_key(movie_name)

            if movie_key in sent_movie_keys:
                continue

            poster_url = (
                kinopoisk_film.get("posterUrlPreview")
                or kinopoisk_film.get("posterUrl")
            )
            full_text = f"{text}\n\n{format_rating_line(movie_name)}"

            add_seen(user_id, movie_name)
            send_movie_card(chat_id, full_text, movie_name, poster_url)

            sent_movie_keys.add(movie_key)
            sent_count += 1

        if sent_count < target_count and attempt < max_attempts - 1:
            bot.send_chat_action(chat_id, "typing")

    memory.setdefault(user_id, [])
    memory[user_id].append({"role": "user", "content": user_text})
    memory[user_id].append({
        "role": "assistant",
        "content": "\n\n".join(all_answers)
    })
    memory[user_id] = memory[user_id][-20:]

    if sent_count == 0:
        bot.send_message(
            chat_id,
            "Не удалось найти точные карточки фильмов в Кинопоиске. "
            "Попробуй немного изменить запрос."
        )
    elif sent_count < target_count:
        bot.send_message(
            chat_id,
            f"Нашёл только {sent_count} точных вариантов в Кинопоиске. "
            "Остальные кандидаты не прошли проверку названия и года."
        )

    save_memory()
    save_state()
    save_ratings()


# =========================
# KINOPOISK API
# =========================

KP_CACHE = {
    "time": 0,
    "page": None,
    "films": []
}

KP_YEAR_CACHE = {}

KP_CACHE_TTL_SECONDS = 60 * 60 * 6


def get_kinopoisk_top_films(page=1, limit=5):
    if not KINOPOISK_API_KEY:
        return None, "Не найден KINOPOISK_API_KEY. Добавь его в Render → Environment."

    now = time.time()

    if (
        KP_CACHE["page"] == page
        and KP_CACHE["films"]
        and now - KP_CACHE["time"] < KP_CACHE_TTL_SECONDS
    ):
        return KP_CACHE["films"][:limit], None

    url = "https://kinopoiskapiunofficial.tech/api/v2.2/films/top"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    params = {
        "type": "TOP_250_BEST_FILMS",
        "page": page
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        logging.exception("Kinopoisk API HTTP error")
        return None, f"Кинопоиск вернул ошибку {status}. Проверь API-ключ или лимиты."
    except Exception:
        logging.exception("Kinopoisk API error")
        return None, "Не получилось получить топ Кинопоиска. Попробуй позже."

    films = data.get("films") or data.get("items") or []
    films = films[:limit]

    if not films:
        return None, "Кинопоиск не вернул фильмы. Возможно, изменился формат API."

    KP_CACHE["time"] = now
    KP_CACHE["page"] = page
    KP_CACHE["films"] = films

    return films, None


def get_top_by_year(category, year, offset=0, limit=5):
    if not KINOPOISK_API_KEY:
        return None, "Не найден KINOPOISK_API_KEY."

    category = str(category)
    year = int(year)
    offset = int(offset)
    required_count = offset + limit
    cache_key = f"{category}:{year}"

    cache = KP_YEAR_CACHE.setdefault(cache_key, {
        "time": time.time(),
        "films": [],
        "next_page": 1,
        "finished": False,
        "ids": []
    })

    # Совместимость со старой структурой кэша.
    cache.setdefault("films", [])
    cache.setdefault("next_page", 1)
    cache.setdefault("finished", False)
    cache.setdefault("ids", [])

    url = "https://kinopoiskapiunofficial.tech/api/v2.2/films"
    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    is_new_year = year == 2026
    min_rating = 0 if is_new_year else 6.0

    while len(cache["films"]) < required_count and not cache["finished"]:
        page = int(cache["next_page"])

        params = {
            "order": "RATING",
            "type": "FILM",
            "ratingFrom": min_rating,
            "ratingTo": 10,
            "yearFrom": year,
            "yearTo": year,
            "page": page
        }

        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as error:
            status = getattr(error.response, "status_code", "?")
            logging.exception(
                f"Ошибка Кинопоиска для {year}, страница {page}"
            )
            return None, f"Кинопоиск вернул ошибку {status}."
        except Exception:
            logging.exception(
                f"Ошибка загрузки фильмов {year}, страница {page}"
            )
            return None, "Не получилось получить фильмы. Попробуй позже."

        items = data.get("items", []) or data.get("films", [])

        if not items:
            cache["finished"] = True
            break

        for film in items:
            if is_excluded_from_year_top(film):
                continue

            if not passes_year_category_filter(film, category):
                continue

            name = film.get("nameRu") or film.get("nameOriginal") or ""
            if not name:
                continue

            if is_new_year and not film_has_poster(film):
                continue

            film_id = get_kinopoisk_id(film)
            unique_key = str(film_id or f"{name}:{film.get('year')}")

            if unique_key in cache["ids"]:
                continue

            cache["ids"].append(unique_key)
            cache["films"].append(film)

        cache["next_page"] = page + 1
        cache["time"] = time.time()

        total_pages = data.get("totalPages") or data.get("pagesCount")

        if total_pages and page >= int(total_pages):
            cache["finished"] = True

        # Защита от бесконечного цикла при некорректном API-ответе.
        if page >= 100:
            cache["finished"] = True

    KP_YEAR_CACHE[cache_key] = cache
    return cache["films"][offset:offset + limit], None


def format_kinopoisk_film(film, index=None, source_text=""):
    name = film.get("nameRu") or film.get("nameOriginal") or "Без названия"
    year = film.get("year") or "—"
    rating = film.get("ratingKinopoisk") or film.get("rating") or "—"

    genres = film.get("genres", []) or []
    genres_text = ", ".join(
        g.get("genre", "") for g in genres[:3] if g.get("genre")
    ) or "—"

    countries = film.get("countries", []) or []
    countries_text = ", ".join(
        c.get("country", "") for c in countries[:3] if c.get("country")
    ) or "—"

    description = get_film_description(film)

    prefix = f"{index}. " if index else ""
    movie_name = f"{name} ({year})"

    text = (
        f"{prefix}🎬 {movie_name}\n"
        f"⭐️ Кинопоиск: {rating}\n"
        f"🎭 Жанр: {genres_text}\n"
        f"🌍 Страна: {countries_text}\n"
        f"🧾 {description}"
    )

    return movie_name, text


def send_kinopoisk_top(chat_id, page=1, limit=5):
    films, error = get_kinopoisk_top_films(page=page, limit=limit)

    if error:
        bot.send_message(chat_id, error)
        return

    bot.send_message(chat_id, "🏆 Топ Кинопоиска по рейтингу")

    for i, film in enumerate(films, start=1):
        details = get_kinopoisk_details(get_kinopoisk_id(film)) or film
        movie_name, text = format_kinopoisk_film(details, index=i)

        poster_url = details.get("posterUrlPreview") or details.get("posterUrl")
        full_text = f"{text}\n\n{format_rating_line(movie_name)}"

        send_movie_card(chat_id, full_text, movie_name, poster_url)

    save_state()
    save_ratings()


def send_year_top(chat_id, category, year, offset=0, limit=5):
    films, error = get_top_by_year(category, year, offset=offset, limit=limit)

    if error:
        bot.send_message(chat_id, error)
        return

    category_title = "зарубежных фильмов" if category == "foreign" else "фильмов РФ/СНГ + Индии"
    category_short = "зарубежные" if category == "foreign" else "РФ/СНГ + Индия"

    if not films:
        bot.send_message(
            chat_id,
            f"Пока не нашёл достаточно {category_title} за {year}.\n\n"
            "Попробуй другой год или нажми позже.",
            reply_markup=years_markup(category)
        )
        return

    start_number = offset + 1

    for i, film in enumerate(films, start=start_number):
        details = get_kinopoisk_details(get_kinopoisk_id(film)) or film
        movie_name, text = format_kinopoisk_film(
            details,
            index=i
        )

        poster_url = details.get("posterUrlPreview") or details.get("posterUrl")
        full_text = f"{text}\n\n{format_rating_line(movie_name)}"

        send_movie_card(chat_id, full_text, movie_name, poster_url)

    bot.send_message(
        chat_id,
        f"Хочешь ещё {category_short} фильмы за {year}?",
        reply_markup=year_more_markup(category, year, offset + limit)
    )

    save_state()
    save_ratings()


# =========================
# ADMIN HANDLERS
# =========================

@bot.message_handler(commands=["myid"])
def myid_command(message):
    register_user_activity(message=message)

    bot.send_message(
        message.chat.id,
        f"Твой Telegram ID:\n\n`{message.from_user.id}`",
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["admin"])
def admin_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к админке.")
        return

    bot.send_message(
        message.chat.id,
        "👑 Админ-команды:\n\n"
        "/admin_stats — статистика бота\n"
        "/admin_top — топ-50 фильмов пользователей\n"
        "/admin_broadcast_fixed — отправить объявление о починке всем пользователям\n"
        "/admin_broadcast текст — отправить свой текст всем пользователям\n"
        "/admin_clear_posters — очистить кэш постеров\n"
        "/admin_clear_details — очистить кэш описаний\n"
        "/myid — узнать свой Telegram ID"
    )


@bot.message_handler(commands=["admin_stats"])
def admin_stats_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    bot.send_message(message.chat.id, get_admin_stats_text())


@bot.message_handler(commands=["admin_top"])
def admin_top_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    send_long_message(message.chat.id, get_user_top_movies(limit=50))


def broadcast_to_all_users(text):
    users = state.get("stats", {}).get("users", {})
    chat_ids = set()

    for record in users.values():
        chat_id = str(record.get("chat_id", "")).strip()
        if chat_id:
            chat_ids.add(chat_id)

    sent = 0
    failed = 0

    for chat_id in chat_ids:
        try:
            bot.send_message(
                chat_id,
                text,
                reply_markup=menu_markup()
            )
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
            logging.exception(f"Не удалось отправить рассылку пользователю {chat_id}")

    return sent, failed


@bot.message_handler(commands=["admin_broadcast_fixed"])
def admin_broadcast_fixed_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    bot.send_message(message.chat.id, "📣 Отправляю объявление всем пользователям...")

    sent, failed = broadcast_to_all_users(BOT_FIXED_ANNOUNCEMENT)

    bot.send_message(
        message.chat.id,
        f"✅ Готово.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )


@bot.message_handler(commands=["admin_broadcast"])
def admin_broadcast_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    text = re.sub(
        r"^/admin_broadcast(@\w+)?\s*",
        "",
        message.text or "",
        flags=re.DOTALL
    ).strip()

    if not text:
        bot.send_message(
            message.chat.id,
            "Напиши текст рассылки после команды.\n\n"
            "Пример:\n"
            "/admin_broadcast 🎬 Друзья, бот снова работает нормально!"
        )
        return

    bot.send_message(message.chat.id, "📣 Начинаю рассылку...")

    sent, failed = broadcast_to_all_users(text)

    bot.send_message(
        message.chat.id,
        f"✅ Рассылка завершена.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )


@bot.message_handler(commands=["admin_clear_posters"])
def admin_clear_posters_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    state["poster_cache"] = {}
    save_state()

    bot.send_message(message.chat.id, "🖼 Кэш постеров очищен.")


@bot.message_handler(commands=["admin_clear_details"])
def admin_clear_details_command(message):
    register_user_activity(message=message)

    if not is_admin_message(message):
        bot.send_message(message.chat.id, "⛔️ У тебя нет доступа к этой команде.")
        return

    state["film_detail_cache"] = {}
    save_state()

    bot.send_message(message.chat.id, "🧾 Кэш описаний очищен.")


# =========================
# USER HANDLERS
# =========================

@bot.message_handler(commands=["start"])
def start(message):
    register_user_activity(message=message)

    bot.send_message(
        message.chat.id,
        "🎬 KinoBot AI\n\n"
        "Я подберу фильм или сериал под настроение.\n\n"
        "Напиши:\n"
        "• фильм на вечер\n"
        "• комедия без тупого юмора\n"
        "• фильмы как Интерстеллар\n"
        "• ужасы без скримеров\n"
        "• сериал как Во все тяжкие\n\n"
        "Я учитываю твои лайки и дизлайки, а лучшие фильмы попадают в общий топ пользователей.",
        reply_markup=menu_markup()
    )


@bot.message_handler(commands=["menu"])
def menu_command(message):
    register_user_activity(message=message)

    bot.send_message(
        message.chat.id,
        "Выбери раздел:",
        reply_markup=menu_markup()
    )


@bot.message_handler(commands=["help"])
def help_command(message):
    register_user_activity(message=message)

    bot.send_message(
        message.chat.id,
        "🎬 KinoBot AI\n\n"
        "Я подберу фильм или сериал под настроение.\n\n"
        "Примеры запросов:\n"
        "• фильм на вечер\n"
        "• комедия без тупого юмора\n"
        "• фильмы как Интерстеллар\n"
        "• ужасы без скримеров\n"
        "• сериал как Во все тяжкие\n"
        "• фильмы с неожиданной концовкой\n"
        "• что посмотреть с девушкой\n\n"
        "Ставь 👍 или 👎 под фильмами — так бот будет лучше понимать твой вкус.",
        reply_markup=menu_markup()
    )


@bot.message_handler(commands=["reset"])
def reset_command(message):
    register_user_activity(message=message)

    user_id = str(message.from_user.id)
    state["seen_movies"][user_id] = []
    save_state()

    bot.send_message(
        message.chat.id,
        "♻️ Повторы сброшены. Теперь могу советовать с нуля.",
        reply_markup=menu_markup()
    )


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    register_user_activity(call=call)

    chat_id = call.message.chat.id
    user_id = str(call.from_user.id)
    data = call.data

    try:
        if data == "menu_back":
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "Главное меню:", reply_markup=menu_markup())
            return

        if data == "menu_movies":
            bot.answer_callback_query(call.id, "Подбираю фильмы 🎬")
            send_recommendations(chat_id, user_id, "фильмы", mode="movies")
            return

        if data == "menu_series":
            bot.answer_callback_query(call.id, "Подбираю сериалы 📺")
            send_recommendations(chat_id, user_id, "сериалы", mode="series")
            return

        if data == "menu_new_2026":
            bot.answer_callback_query(call.id, "Ищу зарубежные новинки 2026 🔥")
            send_year_top(chat_id, "foreign", 2026, offset=0, limit=5)
            return

        if data == "menu_random":
            bot.answer_callback_query(call.id, "Ищу случайный фильм 🎲")
            send_recommendations(chat_id, user_id, "случайный фильм", mode="random")
            return

        if data == "menu_genres":
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id, "🎭 Выбери жанр:", reply_markup=genres_markup())
            return

        if data == "menu_years_foreign":
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                "🌍 Выбери год зарубежного топа.",
                reply_markup=years_markup("foreign")
            )
            return

        if data == "menu_years_cis":
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                "🇷🇺 Выбери год для РФ/СНГ + Индии.",
                reply_markup=years_markup("cis")
            )
            return

        if data.startswith("year_foreign:"):
            _, year, offset = data.split(":", 2)
            year_int = int(year)

            if year_int == 2026:
                bot.answer_callback_query(call.id, "Ищу зарубежные новинки 2026 🔥")
            else:
                bot.answer_callback_query(call.id, f"Ищу зарубежный топ {year} 🌍")

            send_year_top(chat_id, "foreign", year_int, int(offset), limit=5)
            return

        if data.startswith("year_cis:"):
            _, year, offset = data.split(":", 2)
            year_int = int(year)

            if year_int == 2026:
                bot.answer_callback_query(call.id, "Ищу РФ/СНГ + Индию 2026 🇷🇺")
            else:
                bot.answer_callback_query(call.id, f"Ищу РФ/СНГ + Индию {year} 🇷🇺")

            send_year_top(chat_id, "cis", year_int, int(offset), limit=5)
            return

        if data.startswith("genre:"):
            genre = data.split(":", 1)[1]
            bot.answer_callback_query(call.id, f"Жанр: {genre}")
            send_recommendations(chat_id, user_id, genre, mode="genre")
            return

        if data == "menu_kp_top":
            bot.answer_callback_query(call.id, "Загружаю топ Кинопоиска 🏆")
            send_kinopoisk_top(chat_id, page=1, limit=5)
            return

        if data == "menu_user_top":
            bot.answer_callback_query(call.id)
            send_long_message(chat_id, get_user_top_movies(limit=50))
            return

        if data == "menu_donate":
            bot.answer_callback_query(call.id)
            bot.send_message(
                chat_id,
                DONATE_TEXT,
                reply_markup=donate_markup()
            )
            return

        if data == "menu_profile":
            profile = get_profile(user_id)

            text = (
                "🧠 Твой вкус\n\n"
                f"👍 Нравится: {', '.join(profile.get('likes', [])[-10:]) or 'пока пусто'}\n\n"
                f"👎 Не нравится: {', '.join(profile.get('dislikes', [])[-10:]) or 'пока пусто'}"
            )

            bot.answer_callback_query(call.id)
            send_long_message(chat_id, text)
            return

        if data == "menu_reset":
            state["seen_movies"][user_id] = []
            save_state()

            bot.answer_callback_query(call.id, "Повторы сброшены")
            bot.send_message(
                chat_id,
                "♻️ Готово. Теперь могу снова предлагать фильмы с нуля.",
                reply_markup=menu_markup()
            )
            return

        if data.startswith("sim:"):
            key = data.split(":", 1)[1]
            movie_name = get_movie_by_callback_key(key)

            if not movie_name:
                bot.answer_callback_query(call.id, "Фильм не найден. Попробуй сделать новый запрос.")
                return

            bot.answer_callback_query(call.id, "Ищу похожие фильмы 🎞")
            bot.send_message(chat_id, f"🎞 Ищу 5 фильмов, похожих на: {movie_name}")

            try:
                send_recommendations(chat_id, user_id, movie_name, mode="similar")
            except Exception:
                logging.exception("Ошибка поиска похожих фильмов")
                bot.send_message(chat_id, "Не получилось подобрать похожие фильмы. Попробуй ещё раз.")

            return

        if data.startswith("like:"):
            key = data.split(":", 1)[1]
            movie_name = get_movie_by_callback_key(key)

            if not movie_name:
                bot.answer_callback_query(call.id, "Фильм не найден")
                return

            record, status = vote_movie(user_id, movie_name, "like")

            if status == "same":
                bot.answer_callback_query(call.id, "Ты уже поставил лайк 👍")
            else:
                bot.answer_callback_query(call.id, f"Лайк засчитан 👍 Всего лайков: {record['likes']}")

            return

        if data.startswith("dislike:"):
            key = data.split(":", 1)[1]
            movie_name = get_movie_by_callback_key(key)

            if not movie_name:
                bot.answer_callback_query(call.id, "Фильм не найден")
                return

            record, status = vote_movie(user_id, movie_name, "dislike")

            if status == "same":
                bot.answer_callback_query(call.id, "Ты уже поставил дизлайк 👎")
            else:
                bot.answer_callback_query(call.id, f"Дизлайк засчитан 👎 Всего дизлайков: {record['dislikes']}")

            return

        bot.answer_callback_query(call.id)

    except Exception:
        logging.exception("Ошибка callback")

        try:
            bot.answer_callback_query(call.id, "Ошибка")
        except Exception:
            pass

        bot.send_message(chat_id, "Что-то пошло не так. Попробуй ещё раз.")


@bot.message_handler(func=lambda message: True)
def chat(message):
    register_user_activity(message=message)

    user_id = str(message.from_user.id)
    text = (message.text or "").strip()

    if not text:
        bot.send_message(
            message.chat.id,
            "Напиши запрос текстом.",
            reply_markup=menu_markup()
        )
        return

    try:
        send_recommendations(message.chat.id, user_id, text, mode="search")
    except Exception:
        logging.exception("Ошибка chat")
        bot.send_message(message.chat.id, "Ошибка AI. Попробуй ещё раз чуть позже.")


# =========================
# START BOT
# =========================

def run_bot():
    logging.info("🎬 KinoBot AI запущен")

    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                long_polling_timeout=30
            )
        except Exception:
            logging.exception(
                "Ошибка соединения. Перезапуск через 5 секунд."
            )
            time.sleep(5)


if __name__ == "__main__":
    run_bot()
