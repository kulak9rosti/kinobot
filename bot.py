import telebot
import json
import os
from openai import OpenAI
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 🔐 ENV
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

# 🧠 MEMORY
MEMORY_FILE = "memory.json"
STATE_FILE = "state.json"

if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        memory = json.load(f)
else:
    memory = {}

# 🧠 STATE (анти-повторы + профиль)
seen_movies = {}
user_profile = {}

# загрузка состояния
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        seen_movies = {k: set(v) for k, v in data.get("seen_movies", {}).items()}
        user_profile = data.get("user_profile", {})

def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def save_state():
    data = {
        "seen_movies": {k: list(v) for k, v in seen_movies.items()},
        "user_profile": user_profile
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 🎬 LAST QUERY
last_query = {}

# 🤖 AI
def ask_ai(prompt):
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )
    return response.output_text

# 🎬 BUTTON
def similar_button(movie_name):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🎞 Похожие фильмы",
            callback_data=f"sim|{movie_name}"
        )
    )
    return markup

# 🚀 START
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "🎬 KinoBot AI\nНапиши фильм или жанр",
    )

# 🎯 CALLBACK
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)

    if call.data.startswith("sim|"):
        movie = call.data.split("|")[1]

        prompt = f"""
Подбери 5 фильмов похожих на: {movie}

Формат:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 почему похож

НЕ ПОВТОРЯЙ старые фильмы.
"""

        answer = ask_ai(prompt)
        bot.send_message(chat_id, answer)
        return

    if call.data == "movies":
        prompt = "Дай 5 фильмов как Netflix подборку"

    elif call.data == "series":
        prompt = "Дай 5 сериалов как Netflix подборку"

    elif call.data == "top":
        prompt = "Топ 5 фильмов мира по IMDb"

    elif call.data == "random":
        prompt = "1 случайный хороший фильм"

    else:
        return

    answer = ask_ai(prompt)
    bot.send_message(chat_id, answer)

# 💬 CHAT
@bot.message_handler(func=lambda message: True)
def chat(message):
    user_id = str(message.chat.id)
    text = message.text

    last_query[user_id] = text

    if user_id not in memory:
        memory[user_id] = []

    if user_id not in seen_movies:
        seen_movies[user_id] = set()

    if user_id not in user_profile:
        user_profile[user_id] = {"likes": [], "dislikes": []}

    memory[user_id].append({"role": "user", "content": text})
    memory[user_id] = memory[user_id][-10:]

    prompt = f"""
Ты кино-эксперт уровня Netflix.

Пользователь: {text}

Уже показанные фильмы:
{list(seen_movies[user_id])}

Вкус пользователя:
{user_profile[user_id]}

ВАЖНО:
- не повторяй фильмы
- подбирай новые
- учитывай вкус

ФОРМАТ:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 описание
"""

    try:
        answer = ask_ai(prompt)

        memory[user_id].append({"role": "assistant", "content": answer})
        save_memory()

        chat_id = message.chat.id

        movies = answer.split("\n\n")

        for m in movies:
            if "🎬" in m:
                title = m.split("\n")[0]
                seen_movies[user_id].add(title)

                bot.send_message(
                    chat_id,
                    m,
                    reply_markup=similar_button(title)
                )

        save_state()
    
    except Exception as e:
        print(e)
        bot.send_message(message.chat.id, "Ошибка AI")

# 🚀 START BOT
print("🎬 KinoBot AI запущен")
bot.infinity_polling(skip_pending=True)
