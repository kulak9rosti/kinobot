import telebot
import requests
import json
import os
from openai import OpenAI

# 🔐 keys from Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

# 🎬 SYSTEM PROMPT (строгий кино-справочник)
SYSTEM_PROMPT = """
Ты кино-справочник как IMDb.

Правила:
- только факты
- без мнений
- без советов
- без эмоций

Формат:

🎬 Название (год)
⭐ IMDb рейтинг (примерно)
🧾 Описание: 1–3 предложения

Если список — 3–5 фильмов в этом формате.
"""

# 📦 MEMORY FILE
MEMORY_FILE = "memory.json"

# загрузка памяти
if os.path.exists(MEMORY_FILE):
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        memory = json.load(f)
else:
    memory = {}

def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

# 📷 постер (Wikipedia fallback)
def get_poster(title):
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
        r = requests.get(url, timeout=5)

        if r.status_code != 200:
            return None

        data = r.json()

        if "originalimage" in data:
            return data["originalimage"]["source"]

    except:
        return None

    return None

# 🤖 handler
@bot.message_handler(func=lambda message: True)
def handle(message):
    user_id = str(message.chat.id)
    text = message.text

    # init user memory
    if user_id not in memory:
        memory[user_id] = []

    # add user message
    memory[user_id].append({"role": "user", "content": text})
    memory[user_id] = memory[user_id][-20:]

    try:
        # AI request
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *memory[user_id]
            ]
        )

        answer = response.output_text

    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка AI запроса")
        print("OpenAI error:", e)
        return

    # save assistant reply
    memory[user_id].append({"role": "assistant", "content": answer})
    save_memory()

    # poster (safe)
    poster = get_poster(text.replace(" ", "_"))

    try:
        if poster:
            bot.send_photo(message.chat.id, poster, caption=answer[:1020])
        else:
            bot.send_message(message.chat.id, answer)

    except Exception as e:
        print("Telegram send error:", e)

# 🚀 start
print("🎬 KinoBot запущен")
bot.polling(none_stop=True, timeout=60)
