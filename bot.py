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

if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        memory = json.load(f)
else:
    memory = {}

def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

# 🎬 BUTTON (под каждым фильмом)
def similar_button(movie_name):
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "🎞 Похожие фильмы",
            callback_data=f"sim|{movie_name}"
        )
    )
    return markup

# 🤖 AI
def ask_ai(prompt):
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )
    return response.output_text

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

    if call.data.startswith("sim|"):
        movie = call.data.split("|")[1]

        prompt = f"""
Подбери 5 фильмов похожих на: {movie}

Формат:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 почему похож
"""

        answer = ask_ai(prompt)
        bot.send_message(chat_id, answer)
        return

# 💬 CHAT
@bot.message_handler(func=lambda message: True)
def chat(message):
    user_id = str(message.chat.id)
    text = message.text

    if user_id not in memory:
        memory[user_id] = []

    memory[user_id].append({"role": "user", "content": text})
    memory[user_id] = memory[user_id][-10:]

    prompt = f"""
Ты кино-эксперт уровня IMDb + Netflix.

Пользователь: {text}

Если это фильм → дай 3–5 фильмов.

ФОРМАТ КАЖДОГО ФИЛЬМА:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 описание (1–2 строки)
"""

    try:
        answer = ask_ai(prompt)

        memory[user_id].append({"role": "assistant", "content": answer})
        save_memory()

        chat_id = message.chat.id

        movies = answer.split("\n\n")

        for m in movies:
            if m.strip():
                title = m.split("\n")[0]

                bot.send_message(
                    chat_id,
                    m,
                    reply_markup=similar_button(title)
                )

    except Exception as e:
        print(e)
        bot.send_message(message.chat.id, "Ошибка AI")

# 🚀 START BOT
print("🎬 KinoBot AI запущен")
bot.infinity_polling(skip_pending=True)
