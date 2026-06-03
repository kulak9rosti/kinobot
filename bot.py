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

# 🔎 Кинопоиск
def kinopoisk_link(query):
    return f"https://www.kinopoisk.ru/index.php?kp_query={query.replace(' ', '%20')}"

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
        "🎬 KinoBot AI\nВыбери действие:",
        reply_markup=main_menu()
    )

# 🤖 OPENAI
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
    text = call.data

    if text == "movies":
        prompt = "Дай 5 фильмов как Netflix подборку. Формат: 🎬 название (год) ⭐️ рейтинг 🧾 описание"
    elif text == "series":
        prompt = "Дай 5 сериалов как Netflix подборку. Формат: 🎬 название (год) ⭐️ рейтинг 🧾 описание"
    elif text == "top":
        prompt = "Дай топ 5 фильмов мира по IMDb сейчас. Формат как Netflix карточки"
    elif text == "random":
        prompt = "Выбери 1 случайный качественный фильм и дай краткое описание"
    else:
        return

    try:
        answer = ask_ai(prompt)

        kp = kinopoisk_link(text)
        answer += f"\n\n🔎 Кинопоиск: {kp}"

        bot.send_message(chat_id, answer)

    except Exception as e:
        print(e)
        bot.send_message(chat_id, "Ошибка AI")

# 💬 CHAT MODE
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

Если запрос про фильмы/похожие → дай 5 фильмов.
Если название фильма → дай описание.
Если непонятно → всё равно предложи фильмы.

ФОРМАТ:
🎬 название (год)
⭐️ рейтинг IMDb
🧾 описание
"""

    try:
        answer = ask_ai(prompt)

        memory[user_id].append({"role": "assistant", "content": answer})
        save_memory()

        kp = kinopoisk_link(text)
        answer += f"\n\n🔎 Кинопоиск: {kp}"

        bot.send_message(message.chat.id, answer)

    except Exception as e:
        print(e)
        bot.send_message(message.chat.id, "Ошибка AI")

# 🚀 START BOT
print("🎬 KinoBot AI запущен")
bot.infinity_polling(skip_pending=True)
