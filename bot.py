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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
KINOPOISK_API_KEY = os.environ.get("KINOPOISK_API_KEY")

ADMIN_IDS = {
    admin_id.strip()
    for admin_id in os.environ.get("ADMIN_IDS", "").split(",")
    if admin_id.strip()
}

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


def get_film_description(film):
    description = (
        film.get("shortDescription")
        or film.get("description")
        or ""
    )

    if description:
        return shorten_text(description)

    kinopoisk_id = get_kinopoisk_id(film)

    if not kinopoisk_id or not KINOPOISK_API_KEY:
        return "Описание пока не найдено."

    cache_key = str(kinopoisk_id)

    cached = state.get("film_detail_cache", {}).get(cache_key)
    if cached:
        return cached

    url = f"https://kinopoiskapiunofficial.tech/api/v2.2/films/{kinopoisk_id}"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception:
        logging.exception(f"Не удалось получить описание фильма {kinopoisk_id}")
        return "Описание пока не найдено."

    description = (
        data.get("shortDescription")
        or data.get("description")
        or ""
    )

    description = shorten_text(description) or "Описание пока не найдено."

    state["film_detail_cache"][cache_key] = description
    save_state()

    return description


# =========================
# COUNTRY FILTERS
# =========================

CIS_COUNTRY_KEYWORDS = [
    "россия",
    "russia",
    "ссср",
    "ussr",
    "советский союз",
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
    is_cis = is_cis_or_post_soviet_film(film)

    if category == "foreign":
        return not is_cis

    if category == "cis":
        return is_cis

    return True


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


def get_user_top_movies(limit=10):
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

    text = "🏆 Топ фильмов по оценкам пользователей\n\n"

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
        InlineKeyboardButton("🇷🇺 СНГ по годам", callback_data="menu_years_cis"),
        InlineKeyboardButton("🏆 Топ Кинопоиска", callback_data="menu_kp_top"),
        InlineKeyboardButton("⭐️ Топ пользователей", callback_data="menu_user_top"),
        InlineKeyboardButton("🎲 Случайный", callback_data="menu_random"),
        InlineKeyboardButton("🧠 Мой вкус", callback_data="menu_profile"),
        InlineKeyboardButton("♻️ Начать подбор заново", callback_data="menu_reset")
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
            InlineKeyboardButton("🇷🇺 СНГ 2026", callback_data="year_cis:2026:0")
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
        text = f"➡️ Ещё 5 фильмов СНГ {year}"

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
Ты KinoBot AI — Telegram-бот для подбора фильмов и сериалов.
Отвечай на русском языке.

Жёсткие правила:
- если просят подборку, выдавай строго 5 фильмов, не больше
- если просят похожие фильмы, выдавай строго 5 фильмов, не больше
- если просят случайный фильм, выдавай строго 1 фильм
- не добавляй английский перевод названия фильма
- не пиши название в формате "русское / английское"
- показывай только русское название и год
- если русского названия не знаешь, напиши наиболее известное название на русском
- не повторяй фильмы из списка уже показанных
- учитывай лайки и дизлайки пользователя
- не делай длинных вступлений
- каждый фильм отделяй пустой строкой

Формат каждого фильма строго такой:
🎬 Название (год)
⭐️ Рейтинг: примерный рейтинг или "—"
🎭 Жанр: 1-3 жанра
🧾 Почему стоит посмотреть: коротко и по делу
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    )

    return response.output_text.strip()


def build_prompt(user_id, user_text, mode="search"):
    seen = get_seen(user_id)
    profile = get_profile(user_id)

    if mode == "similar":
        task = f"Подбери строго 5 фильмов, похожих на: {user_text}"
    elif mode == "series":
        task = "Подбери строго 5 сильных сериалов."
    elif mode == "random":
        task = "Подбери строго 1 случайный хороший фильм, который реально стоит посмотреть."
    elif mode == "genre":
        task = f"Подбери строго 5 лучших фильмов в жанре: {user_text}."
    elif mode == "movies":
        task = "Подбери строго 5 хороших фильмов как умную Netflix-подборку."
    else:
        task = f"Запрос пользователя: {user_text}. Подбери строго 5 подходящих фильмов или сериалов."

    return f"""
{task}

Уже показывали пользователю:
{seen[-80:]}

Пользователю нравится:
{profile.get('likes', [])[-30:]}

Пользователю не нравится:
{profile.get('dislikes', [])[-30:]}

Важно:
- не повторяй уже показанные фильмы
- не добавляй английские названия
- не добавляй английский перевод
- не предлагай то, что пользователь дизлайкнул
- подборка должна быть полезной, не только из самых очевидных фильмов
"""


def split_recommendation_blocks(answer):
    blocks = [b.strip() for b in answer.split("\n\n") if b.strip()]

    if len(blocks) <= 1 and "🎬" in answer:
        raw = re.split(r"(?=🎬)", answer)
        blocks = [b.strip() for b in raw if b.strip()]

    return blocks


def send_recommendations(chat_id, user_id, user_text, mode="search"):
    bot.send_chat_action(chat_id, "typing")

    prompt = build_prompt(user_id, user_text, mode)
    answer = ask_ai(prompt)

    memory.setdefault(user_id, [])
    memory[user_id].append({"role": "user", "content": user_text})
    memory[user_id].append({"role": "assistant", "content": answer})
    memory[user_id] = memory[user_id][-20:]

    blocks = split_recommendation_blocks(answer)

    limit_by_mode = {
        "random": 1,
        "movies": 5,
        "series": 5,
        "genre": 5,
        "similar": 5,
        "search": 5
    }

    max_movies = limit_by_mode.get(mode, 5)

    movie_blocks = [
        block for block in blocks
        if "🎬" in block
    ][:max_movies]

    sent_any = False

    for block in movie_blocks:
        movie_name = extract_movie_name(block)
        add_seen(user_id, movie_name)

        block_with_rating = f"{block}\n\n{format_rating_line(movie_name)}"

        poster_url = find_kinopoisk_poster(movie_name)
        send_movie_card(chat_id, block_with_rating, movie_name, poster_url)

        sent_any = True

    if not sent_any:
        send_long_message(chat_id, answer)

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
        return None, "Не найден KINOPOISK_API_KEY. Добавь его в Render → Environment."

    category = str(category)
    year = int(year)
    offset = int(offset)

    now = time.time()
    cache_key = f"{category}:{year}"

    cached = KP_YEAR_CACHE.get(cache_key)
    if cached and now - cached["time"] < KP_CACHE_TTL_SECONDS:
        films = cached["films"]
        return films[offset:offset + limit], None

    url = "https://kinopoiskapiunofficial.tech/api/v2.2/films"

    headers = {
        "X-API-KEY": KINOPOISK_API_KEY,
        "Content-Type": "application/json"
    }

    collected = []

    is_new_year = year == 2026
    max_pages = 15 if is_new_year else 8

    # Для 2026 не требуем рейтинг, потому что у новых фильмов его часто ещё нет.
    min_rating = 0 if is_new_year else 6.0

    for page in range(1, max_pages + 1):
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
            response = requests.get(url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", "?")
            logging.exception("Kinopoisk year top HTTP error")
            return None, f"Кинопоиск вернул ошибку {status}. Проверь API-ключ или лимиты."
        except Exception:
            logging.exception("Kinopoisk year top error")
            return None, "Не получилось получить топ по году. Попробуй позже."

        items = data.get("items", []) or data.get("films", [])

        if not items:
            break

        for film in items:
            if not passes_year_category_filter(film, category):
                continue

            name = film.get("nameRu") or film.get("nameOriginal") or ""

            if not name:
                continue

            if is_new_year:
                if not film_has_poster(film):
                    continue

            collected.append(film)

        if len(collected) >= offset + limit + 10:
            break

    KP_YEAR_CACHE[cache_key] = {
        "time": now,
        "films": collected
    }

    return collected[offset:offset + limit], None


def format_kinopoisk_film(film, index=None, source_text=""):
    name = film.get("nameRu") or film.get("nameOriginal") or "Без названия"
    year = film.get("year") or "—"
    rating = film.get("rating") or film.get("ratingKinopoisk") or "—"

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
        movie_name, text = format_kinopoisk_film(film, index=i)

        poster_url = film.get("posterUrlPreview") or film.get("posterUrl")
        full_text = f"{text}\n\n{format_rating_line(movie_name)}"

        send_movie_card(chat_id, full_text, movie_name, poster_url)

    save_state()
    save_ratings()


def send_year_top(chat_id, category, year, offset=0, limit=5):
    films, error = get_top_by_year(category, year, offset=offset, limit=limit)

    if error:
        bot.send_message(chat_id, error)
        return

    category_title = "зарубежных фильмов" if category == "foreign" else "фильмов СНГ"
    category_short = "зарубежные" if category == "foreign" else "СНГ"

    if not films:
        bot.send_message(
            chat_id,
            f"Пока не нашёл достаточно {category_title} за {year}.\n\n"
            "Попробуй другой год или нажми позже.",
            reply_markup=years_markup(category)
        )
        return

    start_number = offset + 1

    # Без отдельной шапки. Сразу отправляем карточки фильмов.
    for i, film in enumerate(films, start=start_number):
        movie_name, text = format_kinopoisk_film(
            film,
            index=i
        )

        poster_url = film.get("posterUrlPreview") or film.get("posterUrl")
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
        "/admin_top — топ фильмов пользователей\n"
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

    send_long_message(message.chat.id, get_user_top_movies(limit=20))


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
                "🇷🇺 Выбери год СНГ-топа.",
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
                bot.answer_callback_query(call.id, "Ищу новинки СНГ 2026 🇷🇺")
            else:
                bot.answer_callback_query(call.id, f"Ищу СНГ-топ {year} 🇷🇺")

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
            send_long_message(chat_id, get_user_top_movies(limit=10))
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

logging.info("🎬 KinoBot AI запущен")
bot.infinity_polling(skip_pending=True)
