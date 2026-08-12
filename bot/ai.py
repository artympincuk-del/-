import anthropic

from bot.config import ANTHROPIC_API_KEY

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный AI-ассистент внутри Telegram-бота. "
    "Отвечай по существу, кратко и ясно, на языке пользователя. "
    "Если вопрос требует списка или кода — используй простое форматирование Markdown, "
    "уместное для Telegram (без сложных таблиц)."
)

_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


class AIError(Exception):
    pass


async def ask_claude(history: list[dict], user_text: str, model: str) -> str:
    messages = history + [{"role": "user", "content": user_text}]
    try:
        response = await _client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API error: {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Claude API connection error") from e

    if response.stop_reason == "refusal":
        return "Не могу ответить на этот запрос."

    parts = [block.text for block in response.content if block.type == "text"]
    return "".join(parts).strip() or "…"
