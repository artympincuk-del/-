import asyncio
import json
import logging
import re
import urllib.parse

import aiohttp
import groq
from ddgs import DDGS

from bot.config import (
    FAST_MODEL,
    GROQ_API_KEY,
    GROQ_MAX_CONCURRENT,
    PREMIUM_MODEL,
    STT_MODEL,
    VISION_MODEL,
)

logger = logging.getLogger(__name__)

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
# Guards every actual Groq API call in this module (chat completions, STT,
# image-prompt translation) — a single user firing off several messages at
# once can't run more than GROQ_MAX_CONCURRENT requests against Groq
# simultaneously, which would otherwise burn through the shared free-tier
# rate limit for everyone.
_groq_semaphore = asyncio.Semaphore(GROQ_MAX_CONCURRENT)

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


# "auto" backend selection in ddgs can land on a flaky engine (startpage
# timed out repeatedly in testing) with no fallback. Try a short list of
# engines in order and use the first that actually returns results, instead
# of failing the whole search over one engine's outage.
_SEARCH_BACKENDS = ("duckduckgo", "google", "brave", "yahoo")


def _sync_search_web(query: str, max_results: int = 5) -> str:
    results = None
    for backend in _SEARCH_BACKENDS:
        try:
            results = DDGS(timeout=6).text(
                query, max_results=max_results, region="ru-ru", backend=backend
            )
        except Exception:
            results = None
        if results:
            break
    if not results:
        return "Поиск не дал результатов — свежих данных по этому запросу найти не удалось."
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
        "Пользователь сохранил через /remember следующие обязательные указания по стилю "
        "и содержанию ответа — это не фоновая информация, а инструкции, которые нужно "
        "соблюдать в КАЖДОМ ответе, а не только когда это удобно:\n"
        f"{notes_block}"
    )


# Both gpt-oss models and qwen sit on Groq's free-tier 8000 TPM cap, so we
# can't just raise max_tokens to avoid truncation — that risks a 413 instead.
# The real fix is spending fewer tokens on reasoning when the first attempt
# comes up empty, freeing more of the same budget for the visible answer.
_EFFORT_FALLBACK = {"high": "medium", "medium": "low", "default": "none"}


async def _complete_once(
    messages: list[dict],
    model: str,
    max_tokens: int,
    reasoning_effort: str | None,
    enable_search: bool,
) -> str | None:
    """One attempt at a full answer, including any tool-calling round trips.
    Returns the answer text, or None if the model ran out of its token
    budget on reasoning without producing one (caller may retry cheaper)."""
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": list(messages)}
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = reasoning_effort or "medium"
        kwargs["include_reasoning"] = False
    elif model.startswith("qwen/"):
        kwargs["reasoning_effort"] = reasoning_effort or "default"
        kwargs["reasoning_format"] = "hidden"

    # Tool calling is only wired up for gpt-oss (OpenAI-compatible tool_calls).
    if enable_search and model.startswith("openai/gpt-oss"):
        kwargs["tools"] = SEARCH_TOOL_SCHEMA
        kwargs["tool_choice"] = "auto"

    # Up to 2 tool-calling round trips before forcing a final answer, so a
    # model that keeps wanting to search can't loop forever.
    for _round in range(3):
        try:
            async with _groq_semaphore:
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
            return None
        return text or "…"

    return None


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

    if reasoning_effort is not None:
        effort = reasoning_effort
    elif model.startswith("openai/gpt-oss"):
        effort = "medium"
    elif model.startswith("qwen/"):
        effort = "default"
    else:
        effort = None

    text = await _complete_once(messages, model, max_tokens, effort, enable_search)
    while text is None and effort in _EFFORT_FALLBACK:
        effort = _EFFORT_FALLBACK[effort]
        text = await _complete_once(messages, model, max_tokens, effort, enable_search)

    if text is not None:
        return text
    return "Ответ получился слишком длинным для обработки. Попробуйте задать вопрос короче."


async def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    try:
        async with _groq_semaphore:
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
        async with _groq_semaphore:
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


async def check_configured_models() -> None:
    """Queries Groq's model list once at startup and logs a clear warning
    for any configured model id that Groq doesn't actually offer — without
    this, a typo'd/decommissioned model id only surfaces at runtime, as a
    confusing API error on the first photo or voice message that needs it
    (STT_MODEL, VISION_MODEL) rather than at deploy time."""
    configured = {
        "FAST_MODEL": FAST_MODEL,
        "PREMIUM_MODEL": PREMIUM_MODEL,
        "VISION_MODEL": VISION_MODEL,
        "STT_MODEL": STT_MODEL,
    }
    try:
        response = await _client.models.list()
    except Exception as e:
        logger.warning("Could not verify model availability with Groq at startup: %s", e)
        return

    available = {m.id for m in response.data}
    for setting_name, model_id in configured.items():
        if model_id not in available:
            logger.warning(
                "Configured %s=%r is not in Groq's current model list — requests using it "
                "will fail at runtime. Check for a typo or a decommissioned model id.",
                setting_name,
                model_id,
            )
