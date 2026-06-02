import telebot
import requests
import json
import os
from openai import OpenAI

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
Ты кино-справочник (как Кинопоиск или IMDb).

Твоя задача:
- давать СТРОГО фактическое описание фильма
- НЕ высказывать мнение
- НЕ писать "рекомендую", "стоит посмотреть"
- НЕ использовать эмоции

ФОРМАТ ОТВЕТА:

🎬 Название (год)
⭐️ Рейтинг IMDb (примерно)
🧾 Описание: (1-3 предложения, как в базе данных фильмов)

ДОПУСТИМО:
- жанр
- краткий сюжет

ЗАПРЕЩЕНО:
- личное мнение
- советы
- эмоциональные фразы
- длинные тексты
- выводы

Если пользователь просит список — дай 3–5 фильмов в таком же формате.
"""

MEMORY_FILE = "memory.json"

# память
if os.path.exists(MEMORY_FILE):
    memory = json.load(open(MEMORY_FILE, "r", encoding="utf-8"))
else:
    memory = {}

def save_memory():
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

# 📷 постер через Wikipedia
def get_poster(title):
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
        r = requests.get(url).json()

        if "originalimage" in r:
            return r["originalimage"]["source"]
    except:
        pass

    return None

@bot.message_handler(func=lambda message: True)
def handle(message):
    user_id = str(message.chat.id)
    text = message.text

    if user_id not in memory:
        memory[user_id] = []

    memory[user_id].append({"role": "user", "content": text})
    memory[user_id] = memory[user_id][-20:]

    # 🤖 AI ответ
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *memory[user_id]
        ]
    )

    answer = response.output_text
    memory[user_id].append({"role": "assistant", "content": answer})
    save_memory()

    # 🎬 пробуем найти постер
    poster = get_poster(text.replace(" ", "_"))

    if poster:
        bot.send_photo(user_id, poster, caption=answer)
    else:
        bot.send_message(user_id, answer)

print("🎬 KinoBot (без TMDB) запущен")
bot.polling()