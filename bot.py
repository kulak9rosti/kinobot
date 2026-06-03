import telebot
import json
import os
from openai import OpenAI
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 🔐 ENV (Render)
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

# 🎬 MENU
def main_menu():
    markup = InlineKeyboardMarkup()

    markup.add(
        InlineKeyboardButton("🎬 Фильмы", callback_data="movies"),
        InlineKeyboardButton("📺 Сериалы", callback_data="series")
    )

    markup.add(
        InlineKeyboardButton("🔥 Топ дня", callback_data="top"),
        InlineKeyboardButton("🎲 Случайный", callback_data="random")
    )

    return markup

# 🚀 START
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "🎬 KinoBot AI\nВыбери, что хочешь посмотреть:",
        reply_markup=main_menu()
    )

# 🤖 AI FUNCTION
def ask_ai(prompt):
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )
    return response.output_text

# 🎯 CALLBACK BUTTONS
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    user_id = str(chat_id)

    if user_id not in memory:
        memory[user_id] = []

    if call.data == "movies":
        prompt = "Дай 5 фильмов как Netflix подборку. Формат: 🎬 название (год) ⭐️ рейтинг 🧾 1-2 строки описание"

    elif call.data == "series":
        prompt = "Дай 5 сериалов как Netflix подборку. Формат: 🎬 название (год) ⭐️ рейтинг 🧾 краткое описание"

    elif call.data == "top":
        prompt = "Дай топ 5 фильмов мира по IMDb и популярности сейчас. Формат Netflix карточки"

    elif call.data == "random":
        prompt = "Выбери 1 случайный хороший фильм и дай краткое описание как IMDb"

    else:
        bot.send_message(chat_id, "Ошибка кнопки")
        return

    try:
        answer = ask_ai(prompt)

        # 🧠 memory
        memory[user_id].append({"role": "assistant", "content": answer})
        memory[user_id] = memory[user_id][-10:]
        save_memory()

        bot.send_message(chat_id, answer)

    except Exception as e:
        print(e)
        bot.send_message(chat_id, "Ошибка AI запроса")

# 💬 FREE CHAT (обычный ввод)
@bot.message_handler(func=lambda message: True)
def chat(message):
    user_id = str(message.chat.id)
    text = message.text

    if user_id not in memory:
        memory[user_id] = []

    memory[user_id].append({"role": "user", "content": text})
    memory[user_id] = memory[user_id][-10:]

    prompt = f"""
Ты кино-эксперт как IMDb.

Пользователь написал: {text}

Если это запрос на:
- "похожие фильмы"
- "что посмотреть если понравился фильм"
- "как {фильм}"

ТО ДЕЛАЙ:
👉 дай 5 похожих фильмов списком

Если это просто фильм:
👉 дай описание фильма

ФОРМАТ:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 1-2 строки описание
"""

    try:
        answer = ask_ai(prompt)

        memory[user_id].append({"role": "assistant", "content": answer})
        save_memory()

        bot.send_message(message.chat.id, answer)

    except Exception as e:
        print(e)
        bot.send_message(message.chat.id, "Ошибка AI")

# 🚀 START BOT
print("🎬 KinoBot AI запущен")
bot.infinity_polling(skip_pending=True)
