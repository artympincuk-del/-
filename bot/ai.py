import asyncio
import json
import re
import urllib.parse

import aiohttp
import groq
from ddgs import DDGS

from bot.config import FAST_MODEL, GROQ_API_KEY, STT_MODEL

SYSTEM_PROMPT = (
    "Ты дружелюбный и полезный AI-ассистент внутри Telegram-бота. "
    "Отвечай по существу, кратко и ясно, на языке пользователя, грамотно и без "
    "орфографических и грамматических ошибок.\n\n"
    "Форматирование сообщения — ТОЛЬКО через HTML-теги, которые понимает Telegram, и НИКАКИХ "
    "других: <b>жирный</b>, <i>курсив</i>, <code>инлайн-код</code>, <pre>блок кода</pre>. "
    "Запрещены любые другие HTML-теги (<p>, <div>, <ul>, <li>, <h1> и т.п.) — Telegram их не "
    "поддерживает и сообщение не отправится. Для абзацев и списков используй просто перенос "
    "строки и дефис «- », без тегов.\n\n"
    "Никогда не используй Markdown (**жирный**, `код`, ### заголовки, - списки со звёздочкой) — "
    "Telegram не превращает его в форматирование, и пользователь увидит звёздочки и решётки "
    "прямо в тексте. Не используй заголовки (### и подобное) вообще: если нужно разделить "
    "смысловые блоки — переноси строку.\n\n"
    "Математику пиши обычным читаемым текстом, НИКОГДА не используй LaTeX (никаких $, \\frac, "
    "\\mathbf, \\left, \\right и других backslash-команд). Степени — знаком ^ (x^2) или "
    "юникод-надстрочными (x²), дроби — через слэш (7/13), корень — словом «корень» или знаком √. "
    "Пример правильной записи: x^2 - 2x - 3 = 0, а не $x^2-2x-3=0$.\n\n"
    "Если по смыслу нужен символ < или >, пиши словами «меньше»/«больше» — так безопаснее для "
    "разметки сообщения.\n\n"
    "Если для точного ответа нужна свежая информация (новости, курсы валют, актуальные цены, "
    "события) — используй инструмент search_web вместо того, чтобы гадать по памяти."
)

SEARCH_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web for current information — news, prices, exchange rates, "
                "events, facts that may have changed after training. Returns titles, URLs "
                "and short snippets of the top results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"],
            },
        },
    }
]

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


def _sync_search_web(query: str, max_results: int = 5) -> str:
    try:
        results = DDGS().text(query, max_results=max_results, region="ru-ru")
    except Exception as e:
        return f"Поиск не удался: {e}"
    if not results:
        return "Ничего не найдено."
    lines = []
    for r in results:
        title = r.get("title", "")
        href = r.get("href", "")
        body = (r.get("body") or "")[:300]
        lines.append(f"- {title}\n  {href}\n  {body}")
    return "\n".join(lines)


async def _search_web(query: str) -> str:
    return await asyncio.to_thread(_sync_search_web, query)


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
    enable_search: bool = False,
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
    elif model.startswith("qwen/"):
        # Qwen only accepts reasoning_effort "default"/"none" (not low/high).
        # "default" + hidden keeps answers grounded (vs "none", which tends
        # to guess) while capping reasoning tokens so dense photos don't blow
        # through Groq's free-tier per-minute token limit.
        kwargs["reasoning_effort"] = "default"
        kwargs["reasoning_format"] = "hidden"

    # Tool calling is only wired up for gpt-oss (OpenAI-compatible tool_calls).
    if enable_search and model.startswith("openai/gpt-oss"):
        kwargs["tools"] = SEARCH_TOOL_SCHEMA
        kwargs["tool_choice"] = "auto"

    # Up to 2 tool-calling round trips before forcing a final answer, so a
    # model that keeps wanting to search can't loop forever.
    for _round in range(3):
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
        message = choice.message

        if choice.finish_reason == "tool_calls" and message.tool_calls:
            kwargs["messages"].append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [tc.model_dump() for tc in message.tool_calls],
                }
            )
            for tc in message.tool_calls:
                if tc.function.name == "search_web":
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await _search_web(args.get("query", ""))
                else:
                    result = "Неизвестный инструмент."
                kwargs["messages"].append(
                    {"role": "tool", "tool_call_id": tc.id, "content": result}
                )
            continue

        if choice.finish_reason == "content_filter":
            return "Не могу ответить на этот запрос."

        text = _strip_thinking(message.content or "")
        if not text and choice.finish_reason == "length":
            return "Ответ получился слишком длинным для обработки. Попробуйте задать вопрос короче."
        return text or "…"

    return "Не удалось получить ответ после поиска в интернете. Попробуйте переформулировать вопрос."


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    try:
        transcription = await _client.audio.transcriptions.create(
            file=(filename, audio_bytes),
            model=STT_MODEL,
            response_format="text",
        )
    except groq.APIStatusError as e:
        raise AIError(
            f"Groq STT error: {e.status_code}",
            user_message="Не удалось распознать голосовое сообщение. Попробуйте ещё раз.",
        ) from e
    except groq.APIConnectionError as e:
        raise AIError(
            "Groq STT connection error",
            user_message="Не удалось распознать голосовое сообщение. Попробуйте ещё раз.",
        ) from e

    return transcription if isinstance(transcription, str) else str(transcription).strip()


IMAGE_GEN_URL = "https://image.pollinations.ai/prompt/{prompt}"


async def _translate_for_image(prompt: str) -> str:
    """Pollinations' free model barely understands non-English prompts (a
    Russian description reliably produced an unrelated image in testing) —
    translate to English first so the generated image actually matches."""
    kwargs = {
        "model": FAST_MODEL,
        "max_tokens": 200,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the user's image description into a short, literal English "
                    "prompt for an image generation model. Put the main subject first in "
                    "plain, unambiguous terms (e.g. 'a pug dog', not just 'a pug'). If the "
                    "subject is an animal, say so explicitly ('a real cat', 'a dog', etc.) "
                    "and do NOT phrase it in a way that could be misread as a human "
                    "character — image models sometimes draw a person instead of the "
                    "animal when an animal is described doing a human-like action (e.g. "
                    "wearing glasses, reading). Only add scene/style details the user "
                    "actually mentioned; don't invent extra atmosphere, lighting, or "
                    "backdrop. Reply with ONLY the translated prompt, nothing else."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    if FAST_MODEL.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"
        kwargs["include_reasoning"] = False
    try:
        response = await _client.chat.completions.create(**kwargs)
        translated = _strip_thinking(response.choices[0].message.content or "").strip()
        return translated or prompt
    except Exception:
        return prompt


async def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> bytes:
    prompt = await _translate_for_image(prompt)
    url = IMAGE_GEN_URL.format(prompt=urllib.parse.quote(prompt))
    # Pollinations' anonymous-tier default model (sana) is noticeably worse at
    # following the prompt than flux, which is also free/keyless — e.g. "a pug
    # on a hoverboard" produced an unrelated person with the default model but
    # an accurate pug with flux in testing.
    params = {"width": width, "height": height, "nologo": "true", "model": "flux"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    raise AIError(
                        f"Image gen error: {resp.status}",
                        user_message="Не удалось сгенерировать изображение. Попробуйте другое описание.",
                    )
                return await resp.read()
    except aiohttp.ClientError as e:
        raise AIError(
            f"Image gen connection error: {e}",
            user_message="Не удалось сгенерировать изображение. Попробуйте ещё раз.",
        ) from e
