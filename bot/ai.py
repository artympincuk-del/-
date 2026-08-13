import re

import groq

from bot.config import GROQ_API_KEY

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный AI-ассистент внутри Telegram-бота. "
    "Отвечай по существу, кратко и ясно, на языке пользователя, грамотно и без "
    "орфографических и грамматических ошибок. "
    "Если вопрос требует списка или кода — используй простое форматирование Markdown, "
    "уместное для Telegram (без сложных таблиц)."
)

_client = groq.AsyncGroq(api_key=GROQ_API_KEY)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*", re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>")


def _strip_thinking(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    # A response can get cut off (max_tokens) mid-reasoning, leaving an
    # unclosed <think> tag — drop that dangling reasoning too rather than
    # showing raw internal monologue to the user.
    text = _UNCLOSED_THINK_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text)
    return text.strip()


class AIError(Exception):
    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message or "Не удалось получить ответ. Попробуйте ещё раз."


def _build_system_prompt(notes: list[str] | None) -> str:
    if not notes:
        return SYSTEM_PROMPT
    notes_block = "\n".join(f"- {n}" for n in notes)
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Заметки о пользователе, которые нужно учитывать при ответе "
        "(это факты, сохранённые самим пользователем через /remember):\n"
        f"{notes_block}"
    )


async def ask_ai(
    history: list[dict],
    user_content,
    model: str,
    notes: list[str] | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """`user_content` is either a plain string (text-only turn) or a list of
    content blocks (e.g. text + image_url) for multimodal turns."""
    messages = [{"role": "system", "content": _build_system_prompt(notes)}] + history + [
        {"role": "user", "content": user_content}
    ]
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = reasoning_effort or "medium"
        kwargs["include_reasoning"] = False

    try:
        response = await _client.chat.completions.create(**kwargs)
    except groq.APIStatusError as e:
        if e.status_code == 413:
            raise AIError(
                "Groq API error: 413",
                user_message=(
                    "Слишком много данных для бесплатного лимита модели за один запрос. "
                    "Пришлите фото с меньшим количеством текста или задайте вопрос короче."
                ),
            ) from e
        if e.status_code == 429:
            raise AIError(
                "Groq API error: 429",
                user_message="Сейчас слишком много запросов. Попробуйте через несколько секунд.",
            ) from e
        raise AIError(f"Groq API error: {e.status_code}") from e
    except groq.APIConnectionError as e:
        raise AIError("Groq API connection error") from e

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        return "Не могу ответить на этот запрос."

    text = _strip_thinking(choice.message.content or "")
    if not text and choice.finish_reason == "length":
        return "Ответ получился слишком длинным для обработки. Попробуйте задать вопрос короче."
    return text or "…"
