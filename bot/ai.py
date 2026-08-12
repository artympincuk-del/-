import groq

from bot.config import GROQ_API_KEY

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный AI-ассистент внутри Telegram-бота. "
    "Отвечай по существу, кратко и ясно, на языке пользователя. "
    "Если вопрос требует списка или кода — используй простое форматирование Markdown, "
    "уместное для Telegram (без сложных таблиц)."
)

_client = groq.AsyncGroq(api_key=GROQ_API_KEY)


class AIError(Exception):
    pass


async def ask_ai(history: list[dict], user_text: str, model: str) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_text}
    ]
    try:
        response = await _client.chat.completions.create(
            model=model,
            max_tokens=1024,
            messages=messages,
        )
    except groq.APIStatusError as e:
        raise AIError(f"Groq API error: {e.status_code}") from e
    except groq.APIConnectionError as e:
        raise AIError("Groq API connection error") from e

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        return "Не могу ответить на этот запрос."

    return (choice.message.content or "").strip() or "…"
