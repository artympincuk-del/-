import re

import groq

from bot.config import GROQ_API_KEY

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный AI-ассистент внутри Telegram-бота. "
    "Отвечай по существу, кратко и ясно, на языке пользователя. "
    "Если вопрос требует списка или кода — используй простое форматирование Markdown, "
    "уместное для Telegram (без сложных таблиц)."
)

_client = groq.AsyncGroq(api_key=GROQ_API_KEY)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>")


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text)
    return text.strip()


class AIError(Exception):
    pass


async def ask_ai(history: list[dict], user_content, model: str) -> str:
    """`user_content` is either a plain string (text-only turn) or a list of
    content blocks (e.g. text + image_url) for multimodal turns."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_content}
    ]
    try:
        response = await _client.chat.completions.create(
            model=model,
            max_tokens=2048,
            messages=messages,
        )
    except groq.APIStatusError as e:
        raise AIError(f"Groq API error: {e.status_code}") from e
    except groq.APIConnectionError as e:
        raise AIError("Groq API connection error") from e

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        return "Не могу ответить на этот запрос."

    text = _strip_thinking(choice.message.content or "")
    return text or "…"
