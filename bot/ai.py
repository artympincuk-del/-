import asyncio
import base64
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
    POLLINATIONS_API_KEY,
    PREMIUM_MODEL,
    REQUEST_TOKEN_BUDGET,
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


class _PayloadTooLargeError(Exception):
    """Internal-only signal for ask_ai's retry loop, raised by _complete_once
    on a Groq 413 instead of AIError. Kept separate from AIError precisely so
    it does NOT propagate as a user-facing failure on the first or second
    occurrence — ask_ai catches it, shrinks the request (drop history, then
    truncate the current message), and retries before ever giving up. Only
    ask_ai's own final AIError (after exhausting retries) is meant to reach
    callers outside this module."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


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


# Rough, no-dependency token estimate — real tokenization would need a
# tokenizer library we don't otherwise use. ~4 chars/token is the usual rule
# of thumb for English text, but this bot's users write mostly Cyrillic,
# which tends to run fewer chars per token — use a smaller divisor so the
# estimate leans toward *overestimating* real usage rather than under.
_CHARS_PER_TOKEN = 2.5


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _content_text(content) -> str:
    """A message's `content` is either a plain string (text-only turn) or a
    list of content blocks (e.g. text + image_url) for multimodal turns —
    only the text blocks contribute meaningfully to the token estimate, an
    image_url's data URL doesn't reflect real image-token cost either way."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _estimate_message_tokens(content) -> int:
    return _estimate_tokens(_content_text(content))


def _trim_history_to_budget(
    history: list[dict], system_content: str, user_content, budget: int
) -> list[dict]:
    """Keeps the newest turns, silently dropping the oldest ones once the
    running estimated total would exceed REQUEST_TOKEN_BUDGET — a best-effort
    guard against Groq's per-minute token cap (413) for the common "long
    photo-recognized problem + long history" case. MAX_HISTORY_TURNS (applied
    by callers via db.get_dialog_history) remains the hard cap on turn
    *count*; this trims further, by estimated size, on top of that.

    The system prompt and the current user_content are never trimmed here —
    if they alone already fill the budget, history comes back empty and the
    oversized message is sent on its own, letting Groq decide (a resulting
    413 is handled separately by ask_ai's retry sequence, not here)."""
    used = _estimate_tokens(system_content) + _estimate_message_tokens(user_content)
    kept = []
    for turn in reversed(history):
        turn_tokens = _estimate_message_tokens(turn.get("content"))
        if used + turn_tokens > budget:
            break
        used += turn_tokens
        kept.append(turn)
    kept.reverse()
    return kept


_TRUNCATION_NOTE = (
    "[начало сообщения обрезано из-за технического ограничения размера запроса]\n\n"
)


def _truncate_content_half(content):
    """Last resort before giving up on a 413 (see ask_ai): halves the
    current message's text, keeping the END — callers by convention put the
    actual question/instruction last, after any recognized/quoted text — and
    prepends a short note so the model knows the start was cut, without ever
    surfacing token/limit language to the user themselves."""
    if isinstance(content, str):
        half = len(content) // 2
        return _TRUNCATION_NOTE + content[-half:] if half else content
    if isinstance(content, list):
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                half = len(text) // 2
                block = {
                    **block,
                    "text": (_TRUNCATION_NOTE + text[-half:]) if half else text,
                }
            new_content.append(block)
        return new_content
    return content


def _extract_error_detail(e: "groq.APIStatusError") -> str:
    """Best-effort pull of Groq's own explanation for a 413 (e.g. exact
    token-limit numbers) for logging only — never shown to the user. Reads
    only Groq's error response body, never any part of the request itself."""
    body = e.body
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message", body))[:500]
        return str(body)[:500]
    return str(body)[:500] if body is not None else str(e)[:500]


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
                # Not an immediate refusal — ask_ai's caller-side retry loop
                # catches this, shrinks the request, and tries again before
                # any user ever sees a failure. See _PayloadTooLargeError.
                raise _PayloadTooLargeError(_extract_error_detail(e)) from e
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


async def _complete_with_effort_fallback(
    messages: list[dict],
    model: str,
    max_tokens: int,
    effort: str | None,
    enable_search: bool,
) -> str | None:
    """Retries with a cheaper reasoning_effort while the model comes back
    empty from running out of its token budget mid-reasoning (finish_reason
    == "length") — a different failure mode from a 413 (request rejected
    outright for being too big to even start). A _PayloadTooLargeError from
    _complete_once is intentionally NOT caught here; it propagates to
    ask_ai's own retry-on-413 sequence below."""
    text = await _complete_once(messages, model, max_tokens, effort, enable_search)
    while text is None and effort in _EFFORT_FALLBACK:
        effort = _EFFORT_FALLBACK[effort]
        text = await _complete_once(messages, model, max_tokens, effort, enable_search)
    return text


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
    content blocks (e.g. text + image_url) for multimodal turns.

    History is trimmed to REQUEST_TOKEN_BUDGET before the first attempt (see
    _trim_history_to_budget), and if Groq still rejects the request as too
    large (413) — e.g. a long photo-recognized problem plus history — this
    retries up to two more times before ever giving up: first with history
    dropped entirely, then with the current message's own text halved too.
    Only a third 413 in a row produces a plain-language refusal; a success on
    any attempt reaches the caller as an ordinary answer, no retry visible."""
    system_content = _build_system_prompt(notes)
    trimmed_history = _trim_history_to_budget(
        history, system_content, user_content, REQUEST_TOKEN_BUDGET
    )

    if reasoning_effort is not None:
        effort = reasoning_effort
    elif model.startswith("openai/gpt-oss"):
        effort = "medium"
    elif model.startswith("qwen/"):
        effort = "default"
    else:
        effort = None

    # attempt 1: budget-trimmed history, full message
    # attempt 2 (only if attempt 1 gets a 413): no history, full message
    # attempt 3 (only if attempt 2 also gets a 413): no history, halved message
    attempts_history = [trimmed_history, [], []]
    last_detail = ""
    for attempt_num, hist in enumerate(attempts_history, start=1):
        content = user_content if attempt_num < 3 else _truncate_content_half(user_content)
        messages = [{"role": "system", "content": system_content}] + hist + [
            {"role": "user", "content": content}
        ]
        try:
            text = await _complete_with_effort_fallback(
                messages, model, max_tokens, effort, enable_search
            )
        except _PayloadTooLargeError as e:
            estimated_size = (
                _estimate_tokens(system_content)
                + sum(_estimate_message_tokens(m.get("content")) for m in hist)
                + _estimate_message_tokens(content)
            )
            logger.warning(
                "Groq 413 on attempt %d/3 (model=%s, estimated ~%d tokens): %s",
                attempt_num,
                model,
                estimated_size,
                e.detail,
            )
            last_detail = e.detail
            continue

        if text is not None:
            return text
        return "Ответ получился слишком длинным для обработки. Попробуйте задать вопрос короче."

    logger.warning("Groq 413 persisted after 3 attempts (model=%s): %s", model, last_detail)
    raise AIError(
        f"Groq API error: 413 (exhausted retries; last detail: {last_detail})",
        user_message=(
            "Запрос получился слишком большим, чтобы обработать его за один раз. "
            "Пришлите, пожалуйста, один вопрос или один разворот текста за раз — так я смогу ответить."
        ),
    )


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


async def describe_image_for_generation(image_bytes: bytes) -> str:
    """Literal visual description of a photo — the free fallback for
    'editing' when POLLINATIONS_API_KEY isn't configured (see
    handlers._process_image_edit_request): describe the photo, hand that
    description plus the user's edit instruction to generate_image (flux,
    free). This draws a NEW similar-looking image from the description, not
    a pixel edit of the actual photo — a real difference in fidelity, but
    zero-cost, unlike edit_image (kontext, paid)."""
    image_b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:image/jpeg;base64,{image_b64}"
    content = [
        {
            "type": "text",
            "text": (
                "Describe exactly what is in this image in enough visual detail "
                "(main subject, appearance, pose, colors, background, style) that "
                "an image generation model could redraw something similar from "
                "your description alone, with no other context. Be concise but "
                "specific — no commentary, just the description."
            ),
        },
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    return await ask_ai([], content, VISION_MODEL, max_tokens=500)


IMAGE_EDIT_URL = "https://gen.pollinations.ai/v1/images/edits"


async def edit_image(image_bytes: bytes, prompt: str) -> bytes:
    """Edits an existing image (the kontext model) via Pollinations'
    OpenAI-Images-Edits-compatible endpoint — takes the image as raw bytes
    over multipart POST, never as a URL, specifically so nothing here ever
    needs to hand a third party a URL that could leak credentials (e.g. a
    Telegram file link, which has BOT_TOKEN baked into it). Requires
    POLLINATIONS_API_KEY; unlike generate_image, there's no free anonymous
    tier for this endpoint."""
    if not POLLINATIONS_API_KEY:
        raise AIError(
            "Image edit: POLLINATIONS_API_KEY not configured",
            user_message="Редактирование фото сейчас недоступно.",
        )
    prompt = await _translate_for_image(prompt)
    form = aiohttp.FormData()
    form.add_field("image", image_bytes, filename="photo.jpg", content_type="image/jpeg")
    form.add_field("prompt", prompt)
    form.add_field("model", "kontext")
    headers = {"Authorization": f"Bearer {POLLINATIONS_API_KEY}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async with session.post(IMAGE_EDIT_URL, data=form, headers=headers) as resp:
                if resp.status == 402:
                    raise AIError(
                        "Image edit: 402 payment required (API key pollen balance exhausted)",
                        user_message=(
                            "Не удалось отредактировать фото — на API-ключе для редактирования "
                            "закончился баланс. Сообщите администратору."
                        ),
                    )
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise AIError(
                        f"Image edit error: {resp.status} {body}",
                        user_message="Не удалось отредактировать фото. Попробуйте другое описание.",
                    )
                payload = await resp.json()
    except aiohttp.ClientError as e:
        raise AIError(
            f"Image edit connection error: {e}",
            user_message="Не удалось отредактировать фото. Попробуйте ещё раз.",
        ) from e

    try:
        b64_data = payload["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError) as e:
        raise AIError(
            f"Image edit: unexpected response shape: {payload!r}",
            user_message="Не удалось отредактировать фото. Попробуйте ещё раз.",
        ) from e
    return base64.b64decode(b64_data)


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
