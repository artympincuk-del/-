import asyncio
import base64
import datetime
import io
import logging
import os
import re
import tempfile
import time
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from PIL import Image
from pypdf import PdfReader

from bot import ai, db
from bot.config import (
    ADMIN_IDS,
    DAILY_FREE_IMAGE_MESSAGES,
    DAILY_FREE_MESSAGES,
    DAILY_FREE_PREMIUM_MESSAGES,
    FAST_MODEL,
    FAST_REASONING_EFFORT,
    MAX_HISTORY_TURNS,
    PREMIUM_CREDIT_COST,
    POLLINATIONS_API_KEY,
    PREMIUM_MODEL,
    PREMIUM_REASONING_EFFORT,
    PROMO_BONUS_DAILY_MESSAGES,
    PROMO_BONUS_DAILY_PREMIUM_MESSAGES,
    QUOTA_TZ,
    REFERRAL_BONUS_MESSAGES,
    REFERRAL_DAILY_CAP,
    REFERRAL_MIN_MESSAGES,
    REFERRAL_SIGNUP_BONUS,
    SUBSCRIPTION_DAILY_MESSAGES,
    SUBSCRIPTION_DAILY_PREMIUM_MESSAGES,
    VISION_MODEL,
)
from bot.payments import (
    PACKAGES,
    PRICE_VERSION,
    SUBSCRIPTION,
    SUBSCRIPTION_PERIOD_SECONDS,
    TIME_PACKAGES,
    packages_keyboard,
    resolve_package,
)

logger = logging.getLogger(__name__)

router = Router()


class Form(StatesGroup):
    waiting_for_image_prompt = State()
    waiting_for_image_edit = State()


# (tier, choice) -> which actual Groq model + reasoning_effort to use. `tier`
# ('fast'/'premium') decides which quota bucket a message is billed against;
# `choice` decides which specific engine runs within that tier — the two
# GPT-OSS models are the default/recommended pick, the original Llama model
# is offered alongside the premium one as an alternative "flavor" within
# that tier. Llama 3.1 8B (fast tier) was removed on purpose: its Groq TPM
# ceiling (6000, see config.MODEL_TOKEN_CEILINGS) is noticeably smaller than
# gpt-oss-20b's (8000), so it refused even short questions once history and
# the reserved response budget were counted against it — strictly worse
# than 20B, which is both faster and stronger. _model_option already falls
# back to gptoss for any (tier, choice) it doesn't recognize, so an existing
# user whose stored preference is ("fast", "llama") is switched over
# automatically, no migration needed.
MODEL_OPTIONS = {
    ("fast", "gptoss"): {
        "model": FAST_MODEL,
        "reasoning": FAST_REASONING_EFFORT,
        "label": "⚡ GPT-OSS 20B",
    },
    ("premium", "gptoss"): {
        "model": PREMIUM_MODEL,
        "reasoning": PREMIUM_REASONING_EFFORT,
        "label": "💎 GPT-OSS 120B (глубокий анализ)",
    },
    ("premium", "llama"): {
        "model": "llama-3.3-70b-versatile",
        "reasoning": None,
        "label": "💎🦙 Llama 3.3 70B",
    },
    # Модели второго провайдера (AITUNNEL). Идут в премиум-тариф: у них нет
    # минутных лимитов Groq, но каждый запрос стоит денег с баланса, поэтому
    # бесплатному тарифу их не отдаём.
    ("premium", "qwen37"): {
        "model": "qwen3.7-flash",
        "reasoning": None,
        "label": "💎 Qwen 3.7 Flash",
    },
    ("premium", "gemini"): {
        "model": "gemini-3.5-flash-lite",
        "reasoning": None,
        "label": "💎 Gemini 3.5 Flash Lite",
    },
}


def _model_option(status: dict) -> dict:
    key = (status["model_pref"], status.get("model_choice") or "gptoss")
    return MODEL_OPTIONS.get(key, MODEL_OPTIONS[(status["model_pref"], "gptoss")])


# Default (gptoss) labels for the "you can pick a model" mention in the
# /start welcome and HELP_TEXT — pulled from MODEL_OPTIONS so they can never
# drift out of sync with what the "Модель" menu actually offers.
_FAST_MODEL_LABEL = MODEL_OPTIONS[("fast", "gptoss")]["label"]
_PREMIUM_MODEL_LABEL = MODEL_OPTIONS[("premium", "gptoss")]["label"]

BTN_BALANCE = "💰 Баланс / Пополнить"
BTN_BUY = "💎 Пополнить"
BTN_MODEL = "🧠 Модель"
BTN_NOTES = "📝 Заметки"
BTN_IMAGE = "🎨 Картинка"
BTN_INVITE = "🎁 Пригласить друга"
BTN_RESET = "🔄 Сбросить диалог"
BTN_HELP = "❓ Помощь"
BTN_REMINDER = "🔔 Напоминания"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_BALANCE, callback_data="menu:balance"),
                InlineKeyboardButton(text=BTN_MODEL, callback_data="menu:model"),
            ],
            [
                InlineKeyboardButton(text=BTN_NOTES, callback_data="menu:notes"),
                InlineKeyboardButton(text=BTN_IMAGE, callback_data="menu:image"),
            ],
            [
                InlineKeyboardButton(text=BTN_INVITE, callback_data="menu:invite"),
                InlineKeyboardButton(text=BTN_RESET, callback_data="menu:reset"),
            ],
            [
                InlineKeyboardButton(text=BTN_REMINDER, callback_data="menu:reminder"),
                InlineKeyboardButton(text=BTN_HELP, callback_data="menu:help"),
            ],
        ]
    )


PERSISTENT_MENU_BTN = "📋 Меню"


def persistent_keyboard() -> ReplyKeyboardMarkup:
    """Small always-visible bottom keyboard so the menu is reachable even
    after the inline menu card has scrolled out of view — unlike inline
    keyboards, this stays pinned regardless of how much the chat scrolls."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PERSISTENT_MENU_BTN)]],
        resize_keyboard=True,
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


_QUOTA_TZINFO = ZoneInfo(QUOTA_TZ)


def _format_local_time(utc_iso: str) -> str:
    """Converts a naive-UTC timestamp (as stored in the DB, e.g.
    unlimited_until) to a QUOTA_TZ-local display string — previously shown
    as raw UTC, which reads three hours off for Moscow-timezone users."""
    dt_utc = datetime.datetime.fromisoformat(utc_iso).replace(tzinfo=datetime.timezone.utc)
    return dt_utc.astimezone(_QUOTA_TZINFO).strftime("%Y-%m-%d %H:%M %Z")


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (11 <= n % 100 <= 14):
        return few
    return many


def _format_minutes_duration(minutes: int) -> str:
    """Human-readable duration for a promo bonus (e.g. "3 дня") — bonus is
    stored in minutes since that's what activate_unlimited() takes, but
    partners hand out day-sized bonuses, so round-trip through whichever
    unit divides evenly rather than always showing raw minutes."""
    if minutes > 0 and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} {_ru_plural(days, 'день', 'дня', 'дней')}"
    if minutes > 0 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} {_ru_plural(hours, 'час', 'часа', 'часов')}"
    return f"{minutes} мин."


# Per-user lock so a user firing off several messages in a row gets them
# processed one at a time instead of N concurrent Groq requests each — used
# only at top-level message/callback entry points (never inside a shared
# helper that's already called from behind one of these, or it'd deadlock:
# asyncio.Lock isn't reentrant).
_user_locks: dict[int, asyncio.Lock] = {}
BUSY_TEXT = "⏳ Дождись ответа на предыдущее сообщение."


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


_bot_username_cache: str | None = None


async def _get_bot_username(bot) -> str:
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


async def _should_respond_in_group(message: Message) -> bool:
    """Private chats: always respond. Groups: only when the bot is directly
    addressed (replied to, or @mentioned) — Telegram delivers every group
    message to the bot once privacy mode is off, and answering all of them
    would spam the whole chat instead of just the person who asked."""
    if message.chat.type == "private":
        return True
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == message.bot.id
    ):
        return True
    text = message.text or message.caption or ""
    username = await _get_bot_username(message.bot)
    return f"@{username}".lower() in text.lower()


async def _strip_mention(message: Message, text: str) -> str:
    if message.chat.type == "private" or not text:
        return text
    username = await _get_bot_username(message.bot)
    return re.sub(rf"@{re.escape(username)}", "", text, flags=re.IGNORECASE).strip()


@router.my_chat_member()
async def on_bot_membership_changed(event: ChatMemberUpdated) -> None:
    """Introduce the bot to the whole group the moment it's added, instead of
    staying silent until someone happens to @mention it — one add exposes the
    entire class/chat at once instead of relying on word of mouth."""
    if event.chat.type not in ("group", "supergroup"):
        return
    was_in = event.old_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )
    is_in = event.new_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )
    if was_in or not is_in:
        return

    username = await _get_bot_username(event.bot)
    await event.bot.send_message(
        event.chat.id,
        "👋 <b>Привет! Я AI-ассистент.</b>\n\n"
        "Помогаю с домашкой и вопросами: понимаю текст, фото, голосовые и PDF.\n\n"
        f"В этом чате отвечаю, только если меня <b>упомянуть</b> (@{username}) или "
        "<b>ответить</b> на моё сообщение — не буду встревать в каждый разговор.\n\n"
        "Написать в личку и посмотреть все возможности — /menu там.",
    )


MAX_IMAGE_DIM = 1600


def _prepare_image(raw: bytes) -> bytes:
    """Downscale/recompress a photo so it stays well under the vision API's
    request-size limit (large phone-camera photos otherwise trigger 413s)."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(img.size) > MAX_IMAGE_DIM:
        img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^\n*]+?)\*(?!\*)|(?<!_)_([^\n_]+?)_(?!_)")
_MD_LEADING_MARKER_RE = re.compile(r"(?m)^[ \t]*[*#]+[ \t]+")


def _convert_markdown(text: str) -> str:
    """The model is told (SYSTEM_PROMPT) not to use Markdown, but sometimes
    does anyway (**bold**, `code`) — converts the common cases into the
    whitelist HTML tags _sanitize_model_html/Telegram actually understand,
    so the user sees real formatting instead of literal asterisks. Runs
    BEFORE _sanitize_model_html on purpose: whatever this produces is then
    validated/escaped exactly like any other tag, so a leftover or
    mismatched marker just ends up shown as plain text, never breaks
    sending. Deliberately approximate, not a full parser: code spans first
    (so bold/italic markers inside `code` aren't touched), then **/__ bold
    (safe across a line break — two consecutive markers is unambiguous),
    then single */_ italic restricted to one line each (a lone '*' spanning
    several bullet-list lines would otherwise misread as an italic pair;
    this doesn't try to protect subscript-style "x_1" notation from a
    coincidental */_ pairing elsewhere in the same line — a known,
    accepted limit of a best-effort conversion). Any '*'/'#' still sitting
    at the start of a line afterwards (bullet lists, ### headings) gets
    dropped rather than shown raw."""

    def _code_sub(m):
        return f"<code>{m.group(1)}</code>"

    def _bold_sub(m):
        return f"<b>{m.group(1) if m.group(1) is not None else m.group(2)}</b>"

    def _italic_sub(m):
        return f"<i>{m.group(1) if m.group(1) is not None else m.group(2)}</i>"

    text = _MD_CODE_RE.sub(_code_sub, text)
    text = _MD_BOLD_RE.sub(_bold_sub, text)
    text = _MD_ITALIC_RE.sub(_italic_sub, text)
    text = _MD_LEADING_MARKER_RE.sub("", text)
    return text


_HTML_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre"}
_HTML_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)>")
# Matches a whole trusted <a href="...">title</a> source-link pair (see
# _build_sources_html) — used only by _strip_to_plain_text's fallback, never
# by _sanitize_model_html: <a> is deliberately NOT in _HTML_ALLOWED_TAGS,
# so the model's own output can never produce a real clickable link, only
# the source-citation block this bot builds itself from search results.
_HTML_A_PAIR_RE = re.compile(r'<a href="([^"<>]*)">(.*?)</a>', re.DOTALL)


def _escape_html_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sanitize_model_html(text: str) -> str:
    """The model is told to format with a handful of Telegram HTML tags, but
    it also sometimes writes literal '<'/'>' in ordinary math ("x < 5/4").
    Sent as-is, Telegram tries to parse those as tags and rejects the whole
    message. This escapes every '&'/'<'/'>' that isn't part of a genuinely
    well-formed, properly PAIRED whitelist tag — including a stray/unclosed
    <b> with no matching </b> — so Telegram always accepts the message and
    the user only ever sees real formatting or real characters, never raw
    markup and never a silently dropped reply."""
    tokens = []
    pos = 0
    for m in _HTML_TAG_RE.finditer(text):
        if m.start() > pos:
            tokens.append(("text", text[pos : m.start()]))
        tokens.append(("tag", bool(m.group(1)), m.group(2).lower(), m.group(0)))
        pos = m.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))

    # Only a tag that is both whitelisted AND properly nested/closed is kept
    # as real markup — everything else (unknown tags, mismatched or
    # never-closed whitelist tags) gets escaped and shown as literal text.
    valid = [False] * len(tokens)
    stack = []
    for idx, tok in enumerate(tokens):
        if tok[0] != "tag":
            continue
        _, is_close, name, _ = tok
        if name not in _HTML_ALLOWED_TAGS:
            continue
        if not is_close:
            stack.append((idx, name))
        elif stack and stack[-1][1] == name:
            open_idx, _ = stack.pop()
            valid[open_idx] = True
            valid[idx] = True
        # a close tag that doesn't match the top of the stack is left
        # invalid (escaped) rather than force-closing something else

    parts = []
    for idx, tok in enumerate(tokens):
        if tok[0] == "text":
            parts.append(_escape_html_text(tok[1]))
        else:
            _, is_close, name, raw = tok
            parts.append((f"</{name}>" if is_close else f"<{name}>") if valid[idx] else _escape_html_text(raw))
    return "".join(parts)


def _strip_to_plain_text(sanitized_html: str) -> str:
    """Last-resort fallback for _send_long, used only if Telegram still
    rejects the already-sanitized (+ trusted_suffix-appended) HTML — removes
    the (by construction, only valid whitelist) tags and un-escapes entities
    back to literal characters, since a plain-mode message doesn't decode
    HTML entities at all. A trusted <a href="...">title</a> source link (see
    _build_sources_html) survives as "title (url)" rather than losing the
    link or leaking raw tag markup. The user never sees raw markup, at worst
    just loses the bold/italic formatting."""
    without_links = _HTML_A_PAIR_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})", sanitized_html)
    without_tags = _HTML_TAG_RE.sub("", without_links)
    return without_tags.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _build_sources_html(sources: list[tuple[str, str]]) -> str:
    """A short <a href="...">title</a> block citing up to 3 sources an
    answer was actually built from (see ai.ask_ai's forced pre-search on a
    freshness marker) — only ever built from data this bot fetched itself,
    never fabricated and never derived from the model's own text. Returns
    "" when there's nothing to cite. <a> is deliberately not in
    _HTML_ALLOWED_TAGS (the model's own output never gets to produce a real
    link), so this is meant to be appended as _send_long's trusted_suffix,
    AFTER _sanitize_model_html has already run on the model's answer, not
    sanitized itself."""
    usable = [(title, url) for title, url in sources if url.startswith(("http://", "https://"))][:3]
    if not usable:
        return ""
    lines = ["\n\n📎 <b>Источники:</b>"]
    for title, url in usable:
        safe_title = _escape_html_text((title or url).strip())
        safe_url = url.replace('"', "%22")
        lines.append(f'• <a href="{safe_url}">{safe_title}</a>')
    return "\n".join(lines)


TELEGRAM_MAX_MESSAGE_LEN = 4000


async def _send_long(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    trusted_suffix: str = "",
) -> None:
    """Telegram rejects messages over ~4096 chars outright; split instead of crashing.
    Each chunk goes through _convert_markdown (turns stray **bold**/`code` the
    model wrote despite instructions not to into real tags) and then
    _sanitize_model_html, so the model's <b>/<i>/<code> formatting renders
    while stray '<'/'>' (e.g. from math) can never break parsing. If Telegram
    still rejects a chunk for some other reason, the fallback strips markup
    down to plain text instead of ever showing raw tags. `reply_markup` (if
    given) is attached only to the last chunk. `trusted_suffix` (if given) is
    caller-vetted HTML — e.g. _build_sources_html's source-links block, built
    from data this bot fetched itself, not model output — appended to the
    LAST chunk AFTER sanitization, never sanitized itself, so its
    <a href="..."> links survive (_sanitize_model_html doesn't allow <a> at
    all, on purpose)."""
    chunks = [
        text[i : i + TELEGRAM_MAX_MESSAGE_LEN]
        for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LEN)
    ] or [""]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        safe_chunk = _sanitize_model_html(_convert_markdown(chunk))
        if i == len(chunks) - 1 and trusted_suffix:
            safe_chunk += trusted_suffix
        try:
            await message.answer(safe_chunk, reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(_strip_to_plain_text(safe_chunk), parse_mode=None, reply_markup=markup)


QUICK_ACTIONS = {
    "detail": "Дай больше деталей и разверни предыдущий ответ подробнее.",
    "simpler": "Объясни то же самое проще, другими словами, как для новичка.",
    "example": "Приведи ещё один похожий пример с решением.",
}


def quick_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 Подробнее", callback_data="qa:detail"),
                InlineKeyboardButton(text="💡 Проще", callback_data="qa:simpler"),
                InlineKeyboardButton(text="📝 Пример", callback_data="qa:example"),
            ],
            [InlineKeyboardButton(text="📤 Поделиться", callback_data="qa:share")],
        ]
    )


def model_keyboard(current_pref: str, current_choice: str) -> InlineKeyboardMarkup:
    def label(tier: str, choice: str) -> str:
        text = MODEL_OPTIONS[(tier, choice)]["label"]
        return f"✅ {text}" if (tier, choice) == (current_pref, current_choice) else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label("fast", "gptoss"), callback_data="model:fast:gptoss")],
            [InlineKeyboardButton(text=label("premium", "llama"), callback_data="model:premium:llama")],
            [InlineKeyboardButton(text=label("premium", "gptoss"), callback_data="model:premium:gptoss")],
            [InlineKeyboardButton(text=label("premium", "qwen37"), callback_data="model:premium:qwen37")],
            [InlineKeyboardButton(text=label("premium", "gemini"), callback_data="model:premium:gemini")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
        ]
    )


def quota_denied_text(status: dict) -> str:
    tz_abbr = datetime.datetime.now(_QUOTA_TZINFO).strftime("%Z")
    if status.get("limit_source") == "promo":
        fast_cap, premium_cap = db.promo_effective_limits(
            True, status["promo_bonus_stacks"], status["subscription_until"] is not None
        )
        if status["model_pref"] == "premium":
            return (
                f"Дневной лимит бонуса по промокоду на премиум-запросы исчерпан "
                f"({status['premium_used_today']}/{premium_cap}), а докупленных сообщений "
                f"не хватает. Бонус ещё действует, лимит обновится завтра в 00:00 {tz_abbr}. "
                "Можно докупить пакет сообщений или час безлимита: /buy — либо "
                "переключиться на быструю модель: /model"
            )
        return (
            f"Дневной лимит бонуса по промокоду исчерпан ({status['used_today']}/{fast_cap} "
            f"запросов). Бонус ещё действует, лимит обновится завтра в 00:00 {tz_abbr}. "
            "Можно докупить пакет сообщений или час безлимита: /buy."
        )
    if status.get("limit_source") == "subscription":
        if status["model_pref"] == "premium":
            return (
                f"Дневной лимит подписки на премиум-запросы исчерпан "
                f"({status['premium_used_today']}/{SUBSCRIPTION_DAILY_PREMIUM_MESSAGES}), "
                f"а докупленных сообщений не хватает. Обновится в 00:00 {tz_abbr}. "
                "Можно докупить пакет сообщений или час безлимита: /buy — либо "
                "переключиться на быструю модель: /model"
            )
        return (
            f"Дневной лимит подписки исчерпан ({status['used_today']}/{SUBSCRIPTION_DAILY_MESSAGES} "
            f"запросов). Обновится в 00:00 {tz_abbr}. Можно докупить пакет сообщений "
            "или час безлимита: /buy."
        )
    if status["model_pref"] == "premium":
        return (
            f"Бесплатные премиум-запросы на сегодня закончились "
            f"({status['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES}), "
            "а докупленных сообщений не хватает. "
            "Пополните баланс: /buy, либо переключитесь на быструю модель: /model"
        )
    return (
        f"Бесплатный лимит на сегодня исчерпан ({DAILY_FREE_MESSAGES} сообщений). "
        "Докупите сообщения: /buy — или дождитесь сброса в полночь."
    )


def _image_quota_denied_text(status: dict) -> str:
    """For _process_image_request/_process_image_edit_request — images have
    their own flat daily pool (db.try_consume_image), separate from the
    premium chat tiers, so this doesn't need any subscription/promo
    branching the way quota_denied_text does."""
    return (
        f"Дневной лимит картинок исчерпан ({status['images_used_today']}/"
        f"{DAILY_FREE_IMAGE_MESSAGES}), а докупленных сообщений не хватает "
        f"({PREMIUM_CREDIT_COST} за картинку). Пополните баланс: /buy — или "
        "дождитесь сброса в полночь."
    )


async def _apply_referral(message: Message) -> None:
    """Registers the referral link and pays the referee's small immediate
    signup bonus. The referrer's (larger) bonus is NOT paid here — anti-abuse:
    a fake account started via a referral link earns its referrer nothing
    until it sends REFERRAL_MIN_MESSAGES real messages (see
    _credit_referral_progress, called from every real-message handler). The
    signup bonus isn't gated the same way — a fake account getting a few
    free messages on its own throwaway balance isn't the exploit."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("ref_"):
        return
    referrer_str = parts[1][len("ref_") :]
    if not referrer_str.isdigit():
        return
    referrer_id = int(referrer_str)
    user_id = message.from_user.id
    db.get_status(user_id, message.from_user.username)  # ensure the row exists first
    if not db.set_referrer(user_id, referrer_id):
        return
    db.register_referral(user_id, referrer_id)
    new_balance = db.add_bonus_credits(user_id, message.from_user.username, REFERRAL_SIGNUP_BONUS)
    await message.answer(
        f"🎁 Ты пришёл по приглашению — начислено <b>{REFERRAL_SIGNUP_BONUS}</b> "
        f"сообщений! Баланс: {new_balance}."
    )


async def _apply_promo(message: Message) -> None:
    """Registers a promo-code visit and grants its time-based bonus —
    parallel to _apply_referral above, entirely independent data (promo_visits
    vs. referrals/players.referred_by). Silent no-op (just the normal
    greeting) for every "nothing to do" case: no/expired/disabled code, the
    owner clicking their own link, or a user who already visited some promo
    code before — none of these are errors worth surfacing to the user."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("promo_"):
        return
    code = parts[1][len("promo_") :].strip().lower()
    if not code:
        return
    promo = db.get_promo_code(code)
    if promo is None or not promo["active"]:
        return
    user_id = message.from_user.id
    if promo["owner_user_id"] == user_id:
        return
    if not db.record_promo_visit(user_id, code):
        return
    username = message.from_user.username
    new_expiry = db.activate_promo_bonus(user_id, username, promo["bonus_minutes"])
    db.log_event(user_id, "promo_join")
    until_local = _format_local_time(new_expiry)
    duration = _format_minutes_duration(promo["bonus_minutes"])
    await message.answer(
        f"🎁 Бонус по промокоду на <b>{duration}</b> — до "
        f"{PROMO_BONUS_DAILY_MESSAGES} быстрых и {PROMO_BONUS_DAILY_PREMIUM_MESSAGES} "
        f"премиум-запросов в день! Действует до <b>{until_local}</b>."
    )


async def _credit_referral_progress(bot, user_id: int) -> None:
    """Call after each real (quota-approved) message a user sends, and only
    after the reply has already been sent to that user — this is secondary
    accounting, never allowed to cost the user their answer. Bumps their
    pending-referral progress and pays out the deferred bonus to both sides
    once REFERRAL_MIN_MESSAGES is reached — gated by REFERRAL_DAILY_CAP per
    referrer, so a farm of fake accounts can only cash in a handful of
    referrals per day no matter how many it creates."""
    try:
        result = db.try_credit_referral_message(
            user_id, REFERRAL_MIN_MESSAGES, REFERRAL_DAILY_CAP, REFERRAL_BONUS_MESSAGES
        )
        if result is None:
            return
        try:
            await bot.send_message(
                result["referrer_id"],
                f"🎉 Приглашённый тобой пользователь написал боту {REFERRAL_MIN_MESSAGES} "
                f"сообщения(-ий) — начислено <b>{REFERRAL_BONUS_MESSAGES}</b> сообщений! "
                f"Баланс: {result['referrer_balance']}.",
            )
        except Exception:
            pass  # referrer may have blocked the bot
    except Exception:
        logger.exception("Referral credit failed for user_id=%s", user_id)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _apply_referral(message)
    await _apply_promo(message)
    await message.answer(
        "🤖 <b>Привет! Я AI-ассистент.</b>\n"
        "Пиши текстом, голосом, присылай фото или PDF — отвечу на всё.\n\n"
        f"Бесплатно: <b>{DAILY_FREE_MESSAGES}</b> сообщений в день "
        f"(+{DAILY_FREE_PREMIUM_MESSAGES} премиум), сброс в 00:00.\n\n"
        f"Есть быстрая модель ({_FAST_MODEL_LABEL}) для простых вопросов и более "
        f"сильная ({_PREMIUM_MODEL_LABEL}) для разбора сложных задач — переключить "
        f"можно кнопкой «{BTN_MODEL}».\n\n"
        "Переписка сохраняется и может просматриваться поддержкой при разборе "
        "обращений и ошибок.\n\n"
        "Кнопка «📋 Меню» внизу — всегда под рукой. Подробнее — «Помощь».",
        reply_markup=persistent_keyboard(),
    )
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)  # cancel any pending "waiting for image prompt" etc.
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


@router.message(F.text == PERSISTENT_MENU_BTN)
async def btn_persistent_menu(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await message.answer("📋 <b>Меню</b>", reply_markup=main_menu_keyboard())


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")]]
    )


async def _edit_or_send(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    """Edits the button's own message in place instead of sending a new one,
    so navigating the menu doesn't spam the chat with a fresh message every
    tap. Falls back to sending a new message if editing isn't possible
    (e.g. re-selecting the same option leaves text/markup unchanged, which
    Telegram rejects as "message is not modified"). Pass parse_mode=None for
    screens that embed raw user content, so stray '<'/'&' can't crash the
    edit the way they used to for the old HTML-only menus."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


@router.callback_query(F.data == "menu:back")
async def cb_menu_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(None)
    await _edit_or_send(callback, "📋 <b>Меню</b>", main_menu_keyboard())


if POLLINATIONS_API_KEY:
    _HELP_IMAGE_BULLET = (
        "• <b>Картинки</b> — кнопка «Картинка» в меню (или «нарисуй ...») рисует "
        "с нуля; фото с подписью там же — редактирует именно это фото (например: "
        "«добавь усы»). Кнопки «Ещё раз» / «Изменить» под готовой картинкой — "
        "повторить или доработать.\n"
    )
else:
    _HELP_IMAGE_BULLET = (
        "• <b>Картинки</b> — кнопка «Картинка» в меню (или «нарисуй ...») рисует "
        "с нуля; фото с подписью там же — нарисует похожую картинку с учётом "
        "правки (не точное редактирование пикселей, а новая картинка по "
        "описанию фото). Кнопки «Ещё раз» / «Изменить» под готовой картинкой — "
        "повторить или доработать.\n"
    )

HELP_TEXT = (
    "🤖 <b>Что я умею</b>\n\n"
    "• <b>Текст, голос, фото, PDF</b> — присылай как есть, отвечу.\n"
    f"• <b>Модель</b> — по умолчанию быстрая ({_FAST_MODEL_LABEL}) для простых "
    f"вопросов, для сложных задач есть более сильная ({_PREMIUM_MODEL_LABEL}) — "
    f"переключить можно кнопкой «{BTN_MODEL}».\n"
    "• <b>Фото</b> — можно с подписью-вопросом, распознаю содержимое.\n"
    "• <b>Голосовые</b> — распознаю речь и отвечу как на текст.\n"
    "• <b>PDF</b> — прочитаю файл и отвечу по содержимому.\n"
    f"{_HELP_IMAGE_BULLET}"
    "• <b>Поиск в интернете</b> — сам решаю, когда нужны свежие данные "
    "(новости, курсы, факты).\n"
    "• <b>Заметки</b> — «Запомни: ...», учитываю в каждом ответе. Список — "
    "кнопка «Заметки», удалить — /forget &lt;номер&gt;.\n"
    "• <b>Уточнить ответ</b> — кнопки «Подробнее» / «Проще» / «Пример» под "
    "ответом, не нужно переписывать вопрос.\n"
    "• <b>В группах</b> отвечаю только по упоминанию (@username) или ответом "
    "на моё сообщение.\n\n"
    "💰 <b>Сколько это стоит</b>\n\n"
    f"• <b>Быстрая модель</b> — {DAILY_FREE_MESSAGES} бесплатных в день, дальше "
    "из докупленного пакета.\n"
    f"• <b>Премиум модель</b> (глубже думает, точнее на сложном) — "
    f"{DAILY_FREE_PREMIUM_MESSAGES} бесплатных в день, дальше по "
    f"{PREMIUM_CREDIT_COST} сообщения из пакета.\n"
    "• <b>Фото/голос/PDF</b> — как обычное сообщение выбранной модели.\n"
    f"• <b>Картинка</b> (генерация и редактирование) — свой лимит, "
    f"{DAILY_FREE_IMAGE_MESSAGES} бесплатных в день, дальше по "
    f"{PREMIUM_CREDIT_COST} сообщения из пакета.\n"
    f"• <b>Безлимит на {TIME_PACKAGES[0]['label']}</b> — {TIME_PACKAGES[0]['stars']} ⭐, "
    "сообщения не считаются, пока активен.\n"
    f"• <b>Подписка на месяц</b> — {SUBSCRIPTION['stars']} ⭐, до "
    f"{SUBSCRIPTION_DAILY_MESSAGES} быстрых и {SUBSCRIPTION_DAILY_PREMIUM_MESSAGES} "
    "премиум в день, автопродление (отменить — в «Баланс»).\n\n"
    "🎁 <b>Как получить больше бесплатно</b>\n\n"
    f"• <b>Пригласи друга</b> (кнопка в меню) — ему {REFERRAL_SIGNUP_BONUS} сообщений "
    f"сразу, тебе {REFERRAL_BONUS_MESSAGES}, когда он напишет боту "
    f"{REFERRAL_MIN_MESSAGES} сообщения(-ий).\n"
    f"• <b>Промокод</b> от блогера/партнёра — временный бонус (срок зависит от "
    f"ссылки), пока активен — до {PROMO_BONUS_DAILY_MESSAGES} быстрых и "
    f"{PROMO_BONUS_DAILY_PREMIUM_MESSAGES} премиум в день.\n"
    "• <b>Напоминания</b> 🔔 (кнопка в меню) — по желанию, раз в сутки. Выключены "
    "по умолчанию, без рассылок без явного включения.\n\n"
    "Переписка сохраняется и может просматриваться поддержкой при разборе "
    "обращений и ошибок.\n\n"
    "Открыть меню в любой момент — /menu."
)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.callback_query(F.data == "menu:help")
async def cb_menu_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(callback, HELP_TEXT, back_keyboard())


@router.message(F.text == BTN_HELP)
async def btn_help_text(message: Message) -> None:
    # Safety net: users who haven't re-opened /start since the reply-keyboard
    # menu was removed may still have the old buttons cached client-side.
    await message.answer(HELP_TEXT)


def _balance_text(user_id: int, username: str | None) -> str:
    status = db.get_status(user_id, username)
    model_name = _model_option(status)["label"]
    unlimited_line = ""
    if status["unlimited_until"]:
        until_local = _format_local_time(status["unlimited_until"])
        unlimited_line = f"⏱ <b>Безлимит активен до {until_local}</b> — лимиты ниже не расходуются.\n\n"

    # Subscription and promo bonus are shown as separate lines even when
    # both are active at once — someone paying for a subscription should
    # always see it on screen, never have it silently replaced by "bonus".
    subscription_line = ""
    if status["subscription_until"]:
        until_local = _format_local_time(status["subscription_until"])
        if status["subscription_status"] == "canceled":
            subscription_line = (
                f"⭐ <b>Подписка отменена, доступ до {until_local}</b> — дальше не продлится.\n\n"
            )
        else:
            subscription_line = f"⭐ <b>Подписка активна, продлится {until_local}</b>.\n\n"

    promo_bonus_line = ""
    if status["promo_bonus_until"]:
        until_local = _format_local_time(status["promo_bonus_until"])
        promo_bonus_line = f"🎁 <b>Бонус по промокоду активен до {until_local}</b>.\n\n"

    # Whichever allowance is actually being spent right now (see
    # try_consume_message for the identical order of checks): promo bonus
    # first (it expires, unlike a subscription), then subscription, then
    # the free tier. When a promo bonus and a subscription are both active,
    # promo_effective_limits() folds them into ONE number (summed or
    # capped, per promo_bonus_stacks) rather than showing two separately —
    # that's the actual number the person can use today.
    if status["promo_bonus_until"]:
        daily_cap, daily_premium_cap = db.promo_effective_limits(
            True, status["promo_bonus_stacks"], status["subscription_until"] is not None
        )
        usage_label = "сегодня"
    elif status["subscription_until"]:
        daily_cap, daily_premium_cap = SUBSCRIPTION_DAILY_MESSAGES, SUBSCRIPTION_DAILY_PREMIUM_MESSAGES
        usage_label = "по подписке сегодня"
    else:
        daily_cap, daily_premium_cap = DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES
        usage_label = "бесплатных сегодня"

    return (
        f"📊 <b>Баланс</b>\n\n"
        f"{unlimited_line}"
        f"{subscription_line}"
        f"{promo_bonus_line}"
        f"Модель: {model_name}\n"
        f"Быстрая, {usage_label}: {status['used_today']}/{daily_cap}\n"
        f"Премиум, {usage_label}: {status['premium_used_today']}/{daily_premium_cap}\n"
        f"Картинки сегодня: {status['images_used_today']}/{DAILY_FREE_IMAGE_MESSAGES}\n"
        f"Докупленные сообщения: <b>{status['bonus_credits']}</b>\n\n"
        f"Пополнить прямо здесь — выбери пакет ниже:"
    )


def _balance_keyboard(user_id: int, username: str | None) -> InlineKeyboardMarkup:
    status = db.get_status(user_id, username)
    kb = packages_keyboard()
    if status["subscription_until"]:
        is_canceled = status["subscription_status"] == "canceled"
        text = "🔄 Возобновить подписку" if is_canceled else "❌ Отменить подписку"
        kb.inline_keyboard.insert(
            -1, [InlineKeyboardButton(text=text, callback_data="subscription:toggle")]
        )
    return kb


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    db.log_event(message.from_user.id, "buy_opened")
    await message.answer(
        _balance_text(message.from_user.id, message.from_user.username),
        reply_markup=_balance_keyboard(message.from_user.id, message.from_user.username),
    )


@router.callback_query(F.data == "menu:balance")
async def cb_menu_balance(callback: CallbackQuery) -> None:
    await callback.answer()
    db.log_event(callback.from_user.id, "buy_opened")
    await _edit_or_send(
        callback,
        _balance_text(callback.from_user.id, callback.from_user.username),
        _balance_keyboard(callback.from_user.id, callback.from_user.username),
    )


@router.message(F.text == BTN_BALANCE)
async def btn_balance_text(message: Message) -> None:
    db.log_event(message.from_user.id, "buy_opened")
    await message.answer(
        _balance_text(message.from_user.id, message.from_user.username),
        reply_markup=_balance_keyboard(message.from_user.id, message.from_user.username),
    )


MODEL_MENU_TEXT = (
    "Выберите модель:\n\n"
    "⚡ — быстрый тариф, 💎 — премиум (глубже анализ, свой дневной лимит)\n"
    "🦙 — оригинальные модели Llama (альтернатива GPT-OSS)"
)


@router.message(Command("model"))
async def cmd_model(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer(
        MODEL_MENU_TEXT, reply_markup=model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.callback_query(F.data == "menu:model")
async def cb_menu_model(callback: CallbackQuery) -> None:
    await callback.answer()
    status = db.get_status(callback.from_user.id, callback.from_user.username)
    await _edit_or_send(
        callback, MODEL_MENU_TEXT, model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.message(F.text == BTN_MODEL)
async def btn_model_text(message: Message) -> None:
    status = db.get_status(message.from_user.id, message.from_user.username)
    await message.answer(
        MODEL_MENU_TEXT, reply_markup=model_keyboard(status["model_pref"], status["model_choice"])
    )


@router.callback_query(F.data.startswith("model:"))
async def cb_model(callback: CallbackQuery) -> None:
    _, tier, choice = callback.data.split(":")
    if (tier, choice) not in MODEL_OPTIONS:
        # A stale button from before a model was removed from the menu
        # (e.g. fast/llama) — same soft fallback as _model_option: settle
        # on gptoss for that tier instead of a KeyError below.
        choice = "gptoss"
    db.set_model_pref(callback.from_user.id, callback.from_user.username, tier, choice)
    label = MODEL_OPTIONS[(tier, choice)]["label"]
    await callback.answer(f"Модель: {label}")
    await _edit_or_send(callback, MODEL_MENU_TEXT, model_keyboard(tier, choice))


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.clear_dialog_history(message.from_user.id)
    await message.answer("Диалог сброшен. Начнём заново.")


@router.callback_query(F.data == "menu:reset")
async def cb_menu_reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    db.clear_dialog_history(callback.from_user.id)
    await callback.answer("Диалог сброшен")
    await _edit_or_send(callback, "✅ Диалог сброшен. Начнём заново.", back_keyboard())


@router.message(F.text == BTN_RESET)
async def btn_reset_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.clear_dialog_history(message.from_user.id)
    await message.answer("Диалог сброшен. Начнём заново.")


async def _invite_text(bot, user_id: int) -> str:
    username = await _get_bot_username(bot)
    link = f"https://t.me/{username}?start=ref_{user_id}"
    return (
        f"🎁 <b>Пригласи друга</b>\n\n"
        f"Отправь эту ссылку другу — он сразу получит {REFERRAL_SIGNUP_BONUS} "
        f"бесплатных сообщений. Твой бонус ({REFERRAL_BONUS_MESSAGES} сообщений) придёт "
        f"не сразу: как только друг задаст боту {REFERRAL_MIN_MESSAGES} вопроса(-ов) — "
        f"это защита от накрутки фейковыми аккаунтами.\n\n"
        f"<code>{link}</code>"
    )


@router.message(Command("invite"))
async def cmd_invite(message: Message) -> None:
    await message.answer(await _invite_text(message.bot, message.from_user.id))


@router.callback_query(F.data == "menu:invite")
async def cb_menu_invite(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(
        callback, await _invite_text(callback.bot, callback.from_user.id), back_keyboard()
    )


@router.message(F.text == BTN_INVITE)
async def btn_invite_text(message: Message) -> None:
    await message.answer(await _invite_text(message.bot, message.from_user.id))


def _promo_stats_text(stats: dict, admin_view: bool) -> str:
    lines = []
    if admin_view:
        status_label = "активен" if stats["active"] else "выключен"
        owner_label = str(stats["owner_user_id"]) if stats["owner_user_id"] else "не привязан"
        lines.append(f"🎟 <b>{stats['code']}</b> — {stats['title']} ({status_label})")
        lines.append(f"Владелец: {owner_label}")
        lines.append(
            f"Бонус: {_format_minutes_duration(stats['bonus_minutes'])}, "
            f"доля {stats['revenue_share']}%, окно {stats['window_days']} дн.\n"
        )
    else:
        lines.append(f"🎟 <b>Статистика по коду {stats['code']}</b>\n")
    lines.append(f"Переходов: {stats['total_visits']} (за 7 дней: {stats['visits_7d']})")
    lines.append(f"Пользовались ботом: {stats['active_users']}")
    lines.append(f"Оплат: {stats['payments_count']} на {stats['revenue_stars']}⭐")
    share_label = "Доля партнёра" if admin_view else "Твоя доля"
    lines.append(f"{share_label} ({stats['revenue_share']}%): {stats['partner_share_stars']}⭐")
    lines.append(f"\nВнутри окна атрибуции сейчас: {stats['in_window']}")
    return "\n".join(lines)


@router.message(Command("promo"))
async def cmd_promo(message: Message) -> None:
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /promo &lt;код&gt; &lt;слово&gt;")
        return
    code = parts[1].strip().lower()
    token = parts[2].strip()
    result = db.claim_promo_code(code, token, message.from_user.id)
    if result == "invalid":
        # Deliberately the same message whether the code doesn't exist or
        # the word is just wrong — telling those apart would let anyone
        # probe which codes exist.
        await message.answer("Код или слово для привязки неверные.")
    elif result == "already_owned":
        await message.answer(f"Промокод <code>{code}</code> уже привязан к другому аккаунту.")
    else:
        await message.answer(
            f"Готово — промокод <code>{code}</code> теперь привязан к тебе. Статистика: /mypromo"
        )


@router.message(Command("mypromo"))
async def cmd_mypromo(message: Message) -> None:
    promo = db.get_promo_code_by_owner(message.from_user.id)
    if promo is None:
        await message.answer(
            "У тебя нет привязанного промокода. Если у тебя есть код от админа — "
            "привяжи его: /promo &lt;код&gt;"
        )
        return
    stats = db.get_promo_stats(promo["code"])
    await _send_long(message, _promo_stats_text(stats, admin_view=False))


REMINDER_HOURS = [9, 12, 15, 18, 21]
REMINDER_MESSAGE_TEXT = (
    "👋 <i>Напоминание:</i> я всегда тут, если нужна помощь с домашкой — фото, "
    "текст или голосовое, отвечу за пару секунд.\n\nОткрыть меню — /menu."
)


def _reminder_text(status: dict) -> str:
    if status["enabled"]:
        return (
            "🔔 <b>Напоминания</b>\n\n"
            f"Включены, приходят раз в сутки в <b>{status['hour']:02d}:00</b> "
            f"({QUOTA_TZ}).\n\nНикаких других рассылок — только это, и только если сам включил."
        )
    return (
        "🔔 <b>Напоминания</b>\n\n"
        "Сейчас выключены. Можно включить ежедневное напоминание в выбранное время "
        f"({QUOTA_TZ}) — просто короткое сообщение раз в день, ничего больше.\n\n"
        "Выбери время:"
    )


def _reminder_keyboard(status: dict) -> InlineKeyboardMarkup:
    if status["enabled"]:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔕 Выключить напоминания", callback_data="reminder:off")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
            ]
        )
    hour_buttons = [
        InlineKeyboardButton(text=f"{h:02d}:00", callback_data=f"reminder:set:{h}")
        for h in REMINDER_HOURS
    ]
    rows = [hour_buttons[i : i + 3] for i in range(0, len(hour_buttons), 3)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:reminder")
async def cb_menu_reminder(callback: CallbackQuery) -> None:
    await callback.answer()
    status = db.get_reminder_status(callback.from_user.id)
    await _edit_or_send(callback, _reminder_text(status), _reminder_keyboard(status))


@router.message(F.text == BTN_REMINDER)
async def btn_reminder_text(message: Message) -> None:
    status = db.get_reminder_status(message.from_user.id)
    await message.answer(_reminder_text(status), reply_markup=_reminder_keyboard(status))


@router.callback_query(F.data.startswith("reminder:"))
async def cb_reminder_action(callback: CallbackQuery) -> None:
    await callback.answer()
    action = callback.data.split(":")[1]
    user_id = callback.from_user.id
    username = callback.from_user.username
    if action == "off":
        db.set_reminder(user_id, username, False, None)
    elif action == "set":
        hour = int(callback.data.split(":")[2])
        db.set_reminder(user_id, username, True, hour)
    status = db.get_reminder_status(user_id)
    await _edit_or_send(callback, _reminder_text(status), _reminder_keyboard(status))


BUY_TEXT = "💎 <b>Купить сообщения за Telegram Stars</b>\n\nВыберите пакет:"


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    db.log_event(message.from_user.id, "buy_opened")
    await message.answer(BUY_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data == "menu:buy")
async def cb_menu_buy(callback: CallbackQuery) -> None:
    await callback.answer()
    db.log_event(callback.from_user.id, "buy_opened")
    await _edit_or_send(callback, BUY_TEXT, packages_keyboard())


@router.message(F.text == BTN_BUY)
async def btn_buy_text(message: Message) -> None:
    db.log_event(message.from_user.id, "buy_opened")
    await message.answer(BUY_TEXT, reply_markup=packages_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    pkg = PACKAGES[idx]
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"{pkg['messages']} сообщений",
        description="Пополнение лимита сообщений AI-ассистента.",
        # payload carries the price version + index (not the raw amount), so
        # pre-checkout can look the package up in the *current* PACKAGES and
        # reject the invoice if prices changed since it was created.
        payload=f"messages:{PRICE_VERSION}:{idx}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{pkg['messages']} сообщений", amount=pkg["stars"])],
        provider_token="",
    )
    db.log_event(callback.from_user.id, "invoice_sent")


@router.callback_query(F.data.startswith("buytime:"))
async def cb_buy_time(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    pkg = TIME_PACKAGES[idx]
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"Безлимит на {pkg['label']}",
        description="Без ограничения по количеству сообщений на выбранное время.",
        payload=f"unlimited:{PRICE_VERSION}:{idx}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Безлимит {pkg['label']}", amount=pkg["stars"])],
        provider_token="",
    )
    db.log_event(callback.from_user.id, "invoice_sent")


@router.callback_query(F.data.startswith("buysub:"))
async def cb_buy_subscription(callback: CallbackQuery) -> None:
    await callback.answer()
    # Telegram Stars subscriptions can only be created via createInvoiceLink
    # (send_invoice/answer_invoice has no subscription_period parameter at
    # all) — the link is then handed to the user as a "Pay" button rather
    # than opening the payment sheet directly like the one-off invoices above.
    link = await callback.bot.create_invoice_link(
        title="Подписка на месяц",
        description=(
            f"До {SUBSCRIPTION_DAILY_MESSAGES} быстрых и {SUBSCRIPTION_DAILY_PREMIUM_MESSAGES} "
            f"премиум-запросов в день на {SUBSCRIPTION['days']} дней, автопродление через "
            "Telegram Stars."
        ),
        payload=f"subscription:{PRICE_VERSION}:0",
        currency="XTR",
        prices=[LabeledPrice(label="Подписка на месяц", amount=SUBSCRIPTION["stars"])],
        subscription_period=SUBSCRIPTION_PERIOD_SECONDS,
        provider_token="",
    )
    db.log_event(callback.from_user.id, "invoice_sent")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Оплатить {SUBSCRIPTION['stars']}", url=link)]
        ]
    )
    await callback.message.answer(
        f"⭐ <b>Подписка на месяц — {SUBSCRIPTION['stars']} ⭐/мес</b>\n\n"
        f"До {SUBSCRIPTION_DAILY_MESSAGES} быстрых и {SUBSCRIPTION_DAILY_PREMIUM_MESSAGES} "
        f"премиум-запросов в день, автопродление каждые {SUBSCRIPTION['days']} дней. "
        "Отменить в любой момент можно в «Баланс».",
        reply_markup=kb,
    )


def _parse_payload(payload: str) -> tuple[str, str, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    kind, version, idx_str = parts
    return kind, version, int(idx_str)


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    parsed = _parse_payload(pre_checkout_query.invoice_payload)
    if parsed is None:
        await pre_checkout_query.answer(
            ok=False, error_message="Некорректный платёж, оформите покупку заново."
        )
        return
    kind, version, idx = parsed
    pkg = resolve_package(kind, version, idx)
    if pkg is None:
        await pre_checkout_query.answer(
            ok=False,
            error_message="Этот пакет уже недействителен (изменились цены) — оформите покупку заново.",
        )
        return
    if pre_checkout_query.total_amount != pkg["stars"]:
        await pre_checkout_query.answer(
            ok=False, error_message="Цена не совпадает, оформите покупку заново."
        )
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    user_id = message.from_user.id
    username = message.from_user.username
    stars = sp.total_amount
    charge_id = sp.telegram_payment_charge_id

    # pre_checkout already validated the payload against current PACKAGES, so
    # this should always resolve — but re-validate defensively rather than
    # trust a payload blindly this far into the money-already-moved path.
    parsed = _parse_payload(sp.invoice_payload)
    pkg = resolve_package(*parsed) if parsed else None
    if pkg is None:
        logger.warning(
            "successful_payment with unresolvable payload=%r charge_id=%s user_id=%s",
            sp.invoice_payload, charge_id, user_id,
        )
        await message.answer(
            "✅ Оплата получена, но пакет не удалось определить автоматически. "
            f"Сообщи администратору этот номер платежа: <code>{charge_id}</code>"
        )
        return

    kind = parsed[0]
    if kind == "messages":
        amount = pkg["messages"]
    elif kind == "unlimited":
        amount = pkg["minutes"]
    else:
        amount = pkg["days"]
    outcome, result = db.record_payment_and_credit(user_id, username, kind, stars, charge_id, amount)

    if outcome == "duplicate":
        logger.warning(
            "Duplicate successful_payment ignored: charge_id=%s user_id=%s", charge_id, user_id
        )
        await message.answer("Этот платёж уже был обработан ранее — повторного начисления не будет.")
        return

    db.log_event(user_id, "paid")

    if kind == "unlimited":
        until_local = _format_local_time(result)
        await message.answer(
            f"✅ Оплата получена! Безлимит активирован до <b>{until_local}</b>.\n"
            f"Все сообщения без ограничений, пока безлимит активен."
        )
        return

    if kind == "subscription":
        until_local = _format_local_time(result)
        await message.answer(
            f"✅ Подписка активирована! Действует до <b>{until_local}</b>, дальше продлится "
            f"автоматически. Отменить можно в любой момент в «Баланс»."
        )
        return

    await message.answer(
        f"✅ Оплата получена! Начислено <b>{amount}</b> сообщений.\n"
        f"Доступно докупленных сообщений: <b>{result}</b>."
    )


@router.callback_query(F.data == "subscription:toggle")
async def cb_subscription_toggle(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    username = callback.from_user.username
    status = db.get_status(user_id, username)
    if not status["subscription_until"]:
        await callback.answer("Подписка сейчас не активна.", show_alert=True)
        return

    charge_id = db.get_active_subscription_charge_id(user_id)
    if not charge_id:
        await callback.answer(
            "Не найден платёж этой подписки — обратись в поддержку.", show_alert=True
        )
        return

    want_cancel = status["subscription_status"] != "canceled"
    try:
        ok = await callback.bot.edit_user_star_subscription(
            user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=want_cancel
        )
    except TelegramBadRequest as e:
        await callback.answer(f"Telegram отклонил запрос: {e}", show_alert=True)
        return
    if not ok:
        await callback.answer("Telegram вернул отказ по запросу.", show_alert=True)
        return

    db.set_subscription_status(user_id, "canceled" if want_cancel else "active")
    await callback.answer("Подписка отменена." if want_cancel else "Подписка возобновлена.")
    await _edit_or_send(
        callback, _balance_text(user_id, username), _balance_keyboard(user_id, username)
    )


def _notes_text(user_id: int) -> str:
    notes = db.list_notes(user_id)
    if not notes:
        return "Пока нет заметок. Просто напиши «Запомни: ...» или команду /remember &lt;текст&gt;."
    lines = ["📝 <b>Заметки</b>\n"]
    for note_id, content in notes:
        lines.append(f"{note_id}. {content}")
    lines.append("\nУдалить: /forget &lt;номер&gt;")
    return "\n".join(lines)


@router.message(Command("notes"))
async def cmd_notes(message: Message) -> None:
    await message.answer(_notes_text(message.from_user.id))


@router.callback_query(F.data == "menu:notes")
async def cb_menu_notes(callback: CallbackQuery) -> None:
    await callback.answer()
    await _edit_or_send(callback, _notes_text(callback.from_user.id), back_keyboard())


@router.message(F.text == BTN_NOTES)
async def btn_notes_text(message: Message) -> None:
    await message.answer(_notes_text(message.from_user.id))


# Wording depends on whether real photo editing (ai.edit_image, paid,
# POLLINATIONS_API_KEY) or the free fallback (ai.describe_image_for_generation
# + generate_image — a new similar image, not a literal pixel edit) is what
# will actually run — see _process_image_edit_request. Purely a text toggle:
# restoring the key later (config only) upgrades the wording automatically.
_PHOTO_EDITING_MENTION = (
    "Или пришли фото с подписью, что в нём изменить (например: «добавь усы») — "
    "отредактирую именно это фото, а не нарисую новое.\n\n"
    if POLLINATIONS_API_KEY
    else "Или пришли фото с подписью, что изменить (например: «добавь усы») — нарисую "
    "похожую картинку с учётом правки (не точное редактирование, а новая картинка "
    "по описанию фото).\n\n"
)

IMAGE_INTRO_TEXT = (
    f"🎨 <b>Генерация картинок</b>\n\n"
    f"Опиши следующим сообщением, что нарисовать (например: «закат над горами в стиле "
    f"акварели») — не нужна команда, просто напиши и отправь.\n\n"
    f"{_PHOTO_EDITING_MENTION}"
    f"Стоимость: {DAILY_FREE_IMAGE_MESSAGES} бесплатных картинок в день (свой лимит, "
    f"не общий с обычными сообщениями), а когда они кончатся — "
    f"{PREMIUM_CREDIT_COST} докупленных сообщений за картинку. Пополнить — кнопка «Баланс»."
)

IMAGE_PREFIXES = ("нарисуй:", "нарисуй,", "нарисуй ", "сгенерируй картинку", "сгенерируй изображение")


def image_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔁 Ещё раз", callback_data="img:retry"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data="img:edit"),
            ]
        ]
    )


async def _process_image_request(
    message: Message, state: FSMContext, prompt: str, user_id: int, username: str | None
) -> None:
    prompt = prompt.strip()
    if not prompt:
        await message.answer(IMAGE_INTRO_TEXT)
        return

    # Its own daily pool — separate from premium chat, see try_consume_image.
    allowed, status = db.try_consume_image(
        user_id, username, DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(_image_quota_denied_text(status))
        return

    await message.bot.send_chat_action(message.chat.id, "upload_photo")
    status_msg = await message.answer("🎨 <i>Генерирую картинку...</i>")
    t0 = time.monotonic()

    try:
        image_bytes = await ai.generate_image(prompt)
    except ai.AIError as e:
        db.refund_consumed_message(user_id, status["consumed"])
        await status_msg.edit_text(e.user_message)
        return
    except Exception:
        db.refund_consumed_message(user_id, status["consumed"])
        raise

    try:
        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        elapsed = time.monotonic() - t0

        db.log_message(user_id, username, "user", f"[генерация картинки] {prompt}")
        db.log_message(user_id, username, "assistant", "[изображение отправлено]")

        # Remembered so "Ещё раз"/"Изменить" can regenerate without the user
        # having to retype the description. last_image_mode="generate" tells
        # those buttons this was a from-scratch generation (prompt-level,
        # via flux) rather than a photo edit (see _process_image_edit_request,
        # which uses the kontext model on the actual image bytes instead).
        await state.update_data(last_image_prompt=prompt, last_image_mode="generate")

        await message.answer_photo(
            BufferedInputFile(image_bytes, filename="image.jpg"),
            caption=f"🎨 {prompt}\n\n⚡ <i>Готово за {elapsed:.1f} сек</i>",
            reply_markup=image_actions_keyboard(),
        )
    except Exception:
        # Image was already generated (the expensive/billable part
        # succeeded) but never actually reached the user — still a wasted
        # request from their side, so it still gets refunded.
        db.refund_consumed_message(user_id, status["consumed"])
        raise


async def _process_image_edit_request(
    message: Message, state: FSMContext, file_id: str, prompt: str, user_id: int, username: str | None
) -> None:
    """Edits an actual photo when POLLINATIONS_API_KEY is configured
    (kontext model, via ai.edit_image — real pixel editing). Without a key,
    falls back to a free approximation: describe the photo via Groq vision,
    hand that description + the edit instruction to generate_image (flux,
    free) — a NEW similar-looking image, not a literal edit of the original
    pixels, but zero-cost. Either way this is distinct from
    _process_image_request (from-scratch generation with no source photo).
    Billed identically regardless of path (same daily image pool, same
    refund-on-failure pattern). last_image_file_id is updated to the
    RESULT's own file_id (not the original), so "Ещё раз"/"Изменить" chain
    further edits onto the latest version rather than always the first
    upload."""
    prompt = prompt.strip()
    if not prompt:
        await message.answer(IMAGE_INTRO_TEXT)
        return

    allowed, status = db.try_consume_image(
        user_id, username, DAILY_FREE_IMAGE_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(_image_quota_denied_text(status))
        return

    real_edit = bool(POLLINATIONS_API_KEY)
    await message.bot.send_chat_action(message.chat.id, "upload_photo")
    status_msg = await message.answer(
        "🎨 <i>Редактирую фото...</i>" if real_edit else "🎨 <i>Рисую похожую картинку с учётом правки...</i>"
    )
    t0 = time.monotonic()

    try:
        file_buf = await message.bot.download(file_id)
        source_bytes = _prepare_image(file_buf.read())
        if real_edit:
            edited_bytes = await ai.edit_image(source_bytes, prompt)
        else:
            description = await ai.describe_image_for_generation(source_bytes)
            edited_bytes = await ai.generate_image(f"{description}, {prompt}")
    except ai.AIError as e:
        db.refund_consumed_message(user_id, status["consumed"])
        await status_msg.edit_text(e.user_message)
        return
    except Exception:
        db.refund_consumed_message(user_id, status["consumed"])
        raise

    try:
        try:
            await status_msg.delete()
        except TelegramBadRequest:
            pass

        elapsed = time.monotonic() - t0

        db.log_message(user_id, username, "user", f"[редактирование фото] {prompt}")
        db.log_message(user_id, username, "assistant", "[изображение отправлено]")

        # Honest about the fallback not being a literal edit — the result
        # can look meaningfully different from the original photo, and
        # silently passing it off as "edited" would be misleading.
        note = "" if real_edit else "\n<i>(новая похожая картинка по описанию, не редактирование пикселей)</i>"
        sent = await message.answer_photo(
            BufferedInputFile(edited_bytes, filename="image.jpg"),
            caption=f"🎨 {prompt}{note}\n\n⚡ <i>Готово за {elapsed:.1f} сек</i>",
            reply_markup=image_actions_keyboard(),
        )
        # Chain further edits onto THIS result, not the original upload —
        # Telegram now hosts the sent photo too, so its own file_id works
        # exactly like any other for the next download.
        await state.update_data(
            last_image_prompt=prompt,
            last_image_mode="edit",
            last_image_file_id=sent.photo[-1].file_id,
        )
    except Exception:
        db.refund_consumed_message(user_id, status["consumed"])
        raise


@router.message(Command("image"))
async def cmd_image(message: Message, state: FSMContext) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        await _process_image_request(
            message, state, parts[1], message.from_user.id, message.from_user.username
        )
        return
    await state.set_state(Form.waiting_for_image_prompt)
    await message.answer(IMAGE_INTRO_TEXT)


@router.callback_query(F.data == "menu:image")
async def cb_menu_image(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Form.waiting_for_image_prompt)
    await _edit_or_send(callback, IMAGE_INTRO_TEXT, back_keyboard())


@router.message(F.text == BTN_IMAGE)
async def btn_image_text(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.waiting_for_image_prompt)
    await message.answer(IMAGE_INTRO_TEXT)


@router.message(Form.waiting_for_image_prompt, F.text & ~F.text.startswith("/"))
async def handle_image_prompt_state(message: Message, state: FSMContext) -> None:
    await state.set_state(None)  # clear pending state only, keep conversation history
    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return
    async with lock:
        await _process_image_request(
            message, state, message.text, message.from_user.id, message.from_user.username
        )


@router.message(Form.waiting_for_image_prompt, F.photo)
async def handle_image_prompt_photo_state(message: Message, state: FSMContext) -> None:
    """A photo sent right after tapping "Картинка" is a request to edit
    THAT photo (kontext model), not to generate a new one — distinct from
    the default photo handler (handle_photo_message), which solves whatever
    problem/question is on the photo instead."""
    await state.set_state(None)  # clear pending state only, keep conversation history
    caption = await _strip_mention(message, message.caption)
    if not caption:
        await message.answer(
            "Опиши подписью к фото, что изменить (например: «добавь усы»), и пришли ещё раз."
        )
        return
    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return
    async with lock:
        await _process_image_edit_request(
            message, state, message.photo[-1].file_id, caption,
            message.from_user.id, message.from_user.username,
        )


NO_LAST_IMAGE_TEXT = "Не помню, что генерировал в прошлый раз — опишите картинку заново."


@router.callback_query(F.data == "img:retry")
async def cb_image_retry(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    prompt = data.get("last_image_prompt")
    if not prompt:
        await callback.message.answer(NO_LAST_IMAGE_TEXT)
        return
    lock = _get_user_lock(callback.from_user.id)
    if lock.locked():
        await callback.message.answer(BUSY_TEXT)
        return
    async with lock:
        if data.get("last_image_mode") == "edit":
            await _process_image_edit_request(
                callback.message, state, data["last_image_file_id"], prompt,
                callback.from_user.id, callback.from_user.username,
            )
        else:
            await _process_image_request(
                callback.message, state, prompt, callback.from_user.id, callback.from_user.username
            )


@router.callback_query(F.data == "img:edit")
async def cb_image_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("last_image_prompt"):
        await callback.message.answer(NO_LAST_IMAGE_TEXT)
        return
    await state.set_state(Form.waiting_for_image_edit)
    if data.get("last_image_mode") == "edit":
        await callback.message.answer(
            "✏️ Опишите, что изменить ещё (например: «добавь очки») — применю поверх "
            "текущей картинки."
        )
    else:
        await callback.message.answer(
            "✏️ Опишите, что изменить (например: «сделай фон синим», «добавь очки») — "
            "сгенерирую картинку заново с учётом правки."
        )


@router.message(Form.waiting_for_image_edit, F.text & ~F.text.startswith("/"))
async def handle_image_edit_state(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(None)  # clear pending state only, keep conversation history
    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return
    async with lock:
        if data.get("last_image_mode") == "edit":
            # Chains onto the current (already-edited) image with a fresh
            # instruction — kontext takes one instruction per call, so this
            # doesn't concatenate with the previous one the way the
            # text-generation path below does.
            await _process_image_edit_request(
                message, state, data["last_image_file_id"], message.text.strip(),
                message.from_user.id, message.from_user.username,
            )
            return
        base_prompt = data.get("last_image_prompt")
        prompt = f"{base_prompt}, {message.text.strip()}" if base_prompt else message.text
        await _process_image_request(
            message, state, prompt, message.from_user.id, message.from_user.username
        )


# Notes get mixed into the system prompt for every future reply (see
# ai._build_system_prompt), so a note asking the bot to swear/insult/be rude
# would otherwise let any single user push that tone onto themselves — the
# audience is schoolchildren, so these are rejected at save time rather than
# relying on the model to refuse. Plain substring check, no model call.
# Extend this list as new phrasings show up in practice.
NOTE_BLOCKED_PHRASES = (
    "матерись",
    "материться",
    "мат в ответ",
    "с матом",
    "используй мат",
    "используй маты",
    "добавляй маты",
    "добавь маты",
    "маты в ответ",
    "матом",
    "нецензур",
    "оскорбляй",
    "груби",
    "будь груб",
    "хами",
    "унижай",
    "ругайся",
)

NOTE_REJECTED_TEXT = (
    "Не могу сохранить такую заметку — она просит грубость, мат или неуместный тон, "
    "это здесь запрещено."
)


def _note_is_disallowed(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in NOTE_BLOCKED_PHRASES)


@router.message(Command("remember"))
async def cmd_remember(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи, что запомнить: /remember &lt;текст&gt;")
        return
    note_text = parts[1].strip()
    if _note_is_disallowed(note_text):
        await message.answer(NOTE_REJECTED_TEXT)
        return
    note_id = db.add_note(message.from_user.id, note_text)
    await message.answer(f"✅ Запомнил (заметка №{note_id}).")


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Укажи номер заметки: /forget &lt;номер&gt; (список — кнопка «Заметки»)")
        return
    note_id = int(parts[1].strip())
    if db.delete_note(message.from_user.id, note_id):
        await message.answer(f"Заметка №{note_id} удалена.")
    else:
        await message.answer("Заметка с таким номером не найдена.")


@router.message(Command("whoami"))
async def cmd_whoami(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


def _resolve_target(target: str) -> int | None:
    """Accepts a numeric Telegram ID, @username, or a bare username with no
    @ — case doesn't matter for usernames (find_user_id_by_username does a
    case-insensitive lookup). Username lookup only works for users who have
    messaged the bot at least once (that's the only way we learn it), and
    always resolves against their most recently seen username — Telegram
    usernames can change, and every message updates it in the DB, so this
    needs no separate sync."""
    if target.isdigit():
        return int(target)
    username = target[1:] if target.startswith("@") else target
    return db.find_user_id_by_username(username)


ADMIN_PAGE_SIZE = 8

ADMIN_MENU_TEXT = (
    "🔑 <b>Админ-панель</b>\n\n"
    "Команды по-прежнему работают: /grant, /users, /chatlog "
    "&lt;@username или id&gt;, /notes_of &lt;@username или id&gt;, "
    "/refund &lt;telegram_payment_charge_id&gt;, "
    "/promo_add, /promo_off, /promo_list, /promo_stat, /promo_owner "
    "&lt;код&gt; &lt;@username или id&gt;, /promo_token, /backup."
)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="📈 Воронка покупок", callback_data="admin:funnel")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await message.answer(ADMIN_MENU_TEXT, reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin:menu")
async def cb_admin_menu(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await _edit_or_send(callback, ADMIN_MENU_TEXT, admin_menu_keyboard())


def _admin_stats_text() -> str:
    s = db.get_admin_stats()
    return (
        "📊 <b>Статистика</b>\n\n"
        f"Всего пользователей: <b>{s['total_users']}</b>\n"
        f"Активны сегодня: <b>{s['active_today']}</b>\n"
        f"Докупленных сообщений на руках: <b>{s['bonus_outstanding']}</b>\n\n"
        f"⭐ Доход сегодня: <b>{s['revenue_today']}</b> ({s['payments_today']} плат.)\n"
        f"⭐ Доход за 7 дней: <b>{s['revenue_7d']}</b> ({s['payments_7d']} плат.)\n"
        f"⭐ Доход всего: <b>{s['revenue_all']}</b> ({s['payments_all']} плат.)"
    )


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")]]
    )
    await _edit_or_send(callback, _admin_stats_text(), kb)


def _admin_funnel_text() -> str:
    stats = db.get_funnel_stats()

    def pct(part: int, whole: int) -> str:
        return f"{part / whole * 100:.0f}%" if whole else "—"

    def block(label: str, counts: dict) -> str:
        opened = counts["buy_opened"]
        invoiced = counts["invoice_sent"]
        paid = counts["paid"]
        return (
            f"<b>{label}</b>\n"
            f"Открыли покупку: <b>{opened}</b>\n"
            f"Получили инвойс: <b>{invoiced}</b> ({pct(invoiced, opened)} от открывших)\n"
            f"Оплатили: <b>{paid}</b> ({pct(paid, invoiced)} от получивших инвойс, "
            f"{pct(paid, opened)} от открывших)\n"
        )

    return (
        "📈 <b>Воронка покупок</b> (уникальные пользователи)\n\n"
        f"{block('Сегодня', stats['today'])}\n"
        f"{block('За 7 дней', stats['week'])}"
    )


@router.callback_query(F.data == "admin:funnel")
async def cb_admin_funnel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:menu")]]
    )
    await _edit_or_send(callback, _admin_funnel_text(), kb)


def admin_users_keyboard(page: int, total: int, users: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for uid, uname, used, premium_used, bonus, pref, last_active in users:
        label = f"@{uname}" if uname else str(uid)
        rows.append(
            [InlineKeyboardButton(text=f"{label} · {bonus}💬", callback_data=f"admin:user:{uid}:{page}")]
        )
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:users:{page - 1}"))
    if (page + 1) * ADMIN_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ В меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin:users:"))
async def cb_admin_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int(callback.data.split(":")[2])
    await callback.answer()
    total = db.count_users()
    if total == 0:
        await _edit_or_send(callback, "Пока нет пользователей.", admin_menu_keyboard())
        return
    users = db.list_users(limit=ADMIN_PAGE_SIZE, offset=page * ADMIN_PAGE_SIZE)
    text = f"👥 <b>Пользователи</b> ({total}) — стр. {page + 1}"
    await _edit_or_send(callback, text, admin_users_keyboard(page, total, users))


ADMIN_RECENT_PAYMENTS = 5


def _admin_user_text(p: dict, payments: list[tuple]) -> str:
    name = f"@{p['username']}" if p["username"] else str(p["user_id"])
    lines = [
        f"👤 <b>{name}</b> (id <code>{p['user_id']}</code>)\n",
        f"Тариф: {p['model_pref']} / {p['model_choice']}",
        f"Бесплатно сегодня: {p['used_today']}/{DAILY_FREE_MESSAGES} + "
        f"{p['premium_used_today']}/{DAILY_FREE_PREMIUM_MESSAGES} премиум",
        f"Докупленных сообщений: <b>{p['bonus_credits']}</b>",
    ]
    if p["unlimited_until"]:
        lines.append(f"Безлимит до: {_format_local_time(p['unlimited_until'])}")
    lines.append(f"Последняя активность: {p['last_active_at']}")

    if payments:
        lines.append("\n💳 <b>Последние платежи:</b>")
        for pid, kind, stars, credited, charge_id, status, created_at in payments:
            mark = "✅" if status == "paid" else "↩️"
            cid = charge_id or "нет charge_id (платёж до миграции — возврат недоступен)"
            lines.append(f"#{pid} {mark} {kind} · {stars}⭐ · {created_at}\n   {cid}")
    return "\n".join(lines)


def admin_user_keyboard(uid: int, page: int, payments: list[tuple]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="+10", callback_data=f"admin:grant:{uid}:10:{page}"),
            InlineKeyboardButton(text="+50", callback_data=f"admin:grant:{uid}:50:{page}"),
            InlineKeyboardButton(text="-10", callback_data=f"admin:grant:{uid}:-10:{page}"),
        ],
        [InlineKeyboardButton(text="💬 Чатлог", callback_data=f"admin:chatlog:{uid}:{page}")],
    ]
    for pid, kind, stars, credited, charge_id, status, created_at in payments:
        if status == "paid" and charge_id:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"↩️ Возврат #{pid} ({stars}⭐)",
                        callback_data=f"admin:refund:{pid}:{page}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin:users:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin:user:"))
async def cb_admin_user(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, page_str = callback.data.split(":")
    uid, page = int(uid_str), int(page_str)
    await callback.answer()
    p = db.get_player(uid)
    if p is None:
        await _edit_or_send(callback, "Пользователь не найден.", admin_menu_keyboard())
        return
    payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
    await _edit_or_send(callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments))


@router.callback_query(F.data.startswith("admin:grant:"))
async def cb_admin_grant(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, amount_str, page_str = callback.data.split(":")
    uid, amount, page = int(uid_str), int(amount_str), int(page_str)
    new_balance = db.admin_add_bonus_credits(uid, amount)
    if new_balance is None:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    await callback.answer(f"Готово: {new_balance} сообщений")
    p = db.get_player(uid)
    payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
    await _edit_or_send(callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments))


async def _do_refund(bot, charge_id: str) -> str:
    """Shared by /refund and the admin-card refund button. Calls Telegram's
    Stars refund API first — only if that actually succeeds do we reverse
    the credit locally, so a Telegram-side failure can't leave the user
    stripped of messages they never got refunded for."""
    payment = db.get_payment_by_charge_id(charge_id)
    if payment is None:
        return f"Платёж с charge_id <code>{charge_id}</code> не найден."
    if payment["status"] != "paid":
        return f"Платёж #{payment['id']} уже в статусе «{payment['status']}» — повторный возврат не нужен."

    try:
        ok = await bot.refund_star_payment(
            user_id=payment["user_id"], telegram_payment_charge_id=charge_id
        )
    except TelegramBadRequest as e:
        return f"Telegram отклонил возврат: {e}"
    if not ok:
        return "Telegram вернул отказ по возврату (ok=False, без текста ошибки)."

    result = db.refund_payment(charge_id)
    if result is None:
        return (
            "⚠️ Возврат прошёл в Telegram, но локально платёж уже был помечен "
            "как возвращённый — сверь баланс пользователя вручную."
        )
    unit = "сообщений" if result["kind"] == "messages" else "мин. безлимита"
    return (
        f"✅ Возврат выполнен: {payment['amount_stars']}⭐, "
        f"списано {result['credited_amount']} {unit} у пользователя {result['user_id']}."
    )


@router.callback_query(F.data.startswith("admin:refund:"))
async def cb_admin_refund(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, pid_str, page_str = callback.data.split(":")
    pid, page = int(pid_str), int(page_str)
    await callback.answer("Выполняю возврат…")
    payment = db.get_payment(pid)
    if payment is None or not payment["charge_id"]:
        await callback.message.answer("Платёж не найден или у него нет charge_id.")
        return
    result_text = await _do_refund(callback.bot, payment["charge_id"])
    await callback.message.answer(result_text)

    uid = payment["user_id"]
    p = db.get_player(uid)
    if p is not None:
        payments = db.list_recent_payments(uid, ADMIN_RECENT_PAYMENTS)
        await _edit_or_send(
            callback, _admin_user_text(p, payments), admin_user_keyboard(uid, page, payments)
        )


@router.message(Command("refund"))
async def cmd_refund(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /refund &lt;telegram_payment_charge_id&gt;")
        return
    await message.answer(await _do_refund(message.bot, parts[1]))


def _chatlog_text(target_label: str, rows: list[tuple]) -> str:
    lines = [f"Чат с {target_label} (последние {len(rows)}):\n"]
    for role, content, created_at in rows:
        who = "[Я]" if role == "user" else "[Бот]"
        text = content if len(content) <= 300 else content[:300] + "…"
        lines.append(f"{who} [{created_at}] {text}")
    full_text = "\n".join(lines)
    if len(full_text) > 3500:
        full_text = full_text[-3500:]
    return full_text


@router.callback_query(F.data.startswith("admin:chatlog:"))
async def cb_admin_chatlog(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, _, uid_str, page_str = callback.data.split(":")
    uid, page = int(uid_str), int(page_str)
    await callback.answer()
    rows = db.get_recent_chat(uid, 20)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{uid}:{page}")]]
    )
    if not rows:
        await _edit_or_send(callback, "Нет сообщений для этого пользователя.", kb)
        return
    # Chat content is raw user/model text and can contain stray '<'/'&' that
    # would crash HTML parsing (this bit the old menus before), so this
    # screen is sent as plain text rather than risking that.
    await _edit_or_send(callback, _chatlog_text(str(uid), rows), kb, parse_mode=None)


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
        await message.answer("Использование: /grant &lt;@username или id&gt; &lt;amount&gt;")
        return
    target_id = _resolve_target(parts[1])
    if target_id is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    amount = int(parts[2])
    new_balance = db.admin_add_bonus_credits(target_id, amount)
    if new_balance is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    await message.answer(f"Готово. Баланс пользователя {parts[1]}: {new_balance} сообщений.")


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    users = db.list_users()
    if not users:
        await message.answer("Пока нет пользователей.")
        return
    lines = ["👥 <b>Пользователи</b>\n"]
    for uid, uname, used, premium_used, bonus, pref, last_active in users:
        name = f"@{uname}" if uname else str(uid)
        lines.append(
            f"{name} (id {uid}) — {pref}, "
            f"free {used}/{DAILY_FREE_MESSAGES}+{premium_used}/{DAILY_FREE_PREMIUM_MESSAGES}, "
            f"bonus {bonus}, активен {last_active}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("chatlog"))
async def cmd_chatlog(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /chatlog &lt;@username или id&gt; [N]")
        return
    target_id = _resolve_target(parts[1])
    if target_id is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 20
    rows = db.get_recent_chat(target_id, limit)
    if not rows:
        await message.answer("Нет сообщений для этого пользователя.")
        return
    await message.answer(_chatlog_text(parts[1], rows), parse_mode=None)


@router.message(Command("notes_of"))
async def cmd_notes_of(message: Message) -> None:
    """Lets admins see a specific user's /remember notes when triaging a
    complaint (e.g. a report about the bot's tone) — the notes themselves
    stay per-user and private otherwise."""
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /notes_of &lt;@username или id&gt;")
        return
    target_id = _resolve_target(parts[1])
    if target_id is None:
        await message.answer(f"Пользователь {parts[1]} не найден (он должен хотя бы раз написать боту).")
        return
    notes = db.list_notes(target_id)
    if not notes:
        await message.answer(f"У пользователя {parts[1]} нет сохранённых заметок.")
        return
    lines = [f"📝 Заметки пользователя {parts[1]} (id {target_id}):\n"]
    for note_id, content in notes:
        lines.append(f"{note_id}. {content}")
    # Note text is raw user input, not guaranteed safe HTML — plain text,
    # same reasoning as cmd_chatlog above.
    await message.answer("\n".join(lines), parse_mode=None)


PROMO_CODE_RE = re.compile(r"^[a-z0-9]{1,16}$")


@router.message(Command("promo_add"))
async def cmd_promo_add(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    usage = (
        "Использование: /promo_add &lt;код&gt; &lt;название партнёра&gt; "
        "&lt;бонус_минут&gt; &lt;доля_%&gt; &lt;окно_дней&gt;\n"
        "Например: /promo_add tt1 Иван TikTok 4320 40 30"
    )
    if len(parts) < 6:
        await message.answer(usage)
        return
    code = parts[1].strip().lower()
    if not PROMO_CODE_RE.match(code):
        await message.answer("Код должен быть латиницей и цифрами, до 16 символов.")
        return
    bonus_minutes_str, revenue_share_str, window_days_str = parts[-3], parts[-2], parts[-1]
    title = " ".join(parts[2:-3]).strip()
    if not title:
        await message.answer(usage)
        return
    if not (bonus_minutes_str.isdigit() and revenue_share_str.isdigit() and window_days_str.isdigit()):
        await message.answer(
            "Бонус (мин.), доля (%) и окно (дней) должны быть целыми положительными числами."
        )
        return
    bonus_minutes = int(bonus_minutes_str)
    revenue_share = int(revenue_share_str)
    window_days = int(window_days_str)
    if bonus_minutes <= 0 or window_days <= 0:
        await message.answer("Бонус и окно атрибуции должны быть больше нуля.")
        return
    if not (0 <= revenue_share <= 100):
        await message.answer("Доля партнёра должна быть от 0 до 100.")
        return
    token = db.create_promo_code(code, title, bonus_minutes, revenue_share, window_days)
    if token is None:
        await message.answer(f"Промокод <code>{code}</code> уже существует.")
        return
    username = await _get_bot_username(message.bot)
    link = f"https://t.me/{username}?start=promo_{code}"
    await message.answer(
        f"✅ Промокод <code>{code}</code> создан для «{title}».\n"
        f"Бонус: {_format_minutes_duration(bonus_minutes)}, доля {revenue_share}%, "
        f"окно атрибуции {window_days} дн.\n\n"
        f"Отправь партнёру лично (слово нигде больше не показывается — если "
        f"утечёт, перегенерируй: /promo_token {code} new):\n\n"
        f"Твоя ссылка: {link}\n"
        f"Привяжи её к себе командой боту: /promo {code} {token}"
    )


@router.message(Command("promo_off"))
async def cmd_promo_off(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /promo_off &lt;код&gt;")
        return
    code = parts[1].strip().lower()
    result = db.disable_promo_code(code)
    if result == "not_found":
        await message.answer(f"Промокод <code>{code}</code> не найден.")
    elif result == "already_off":
        await message.answer(f"Промокод <code>{code}</code> уже выключен.")
    else:
        await message.answer(
            f"Промокод <code>{code}</code> выключен. Уже привязанные пользователи и их "
            f"оплаты внутри окна атрибуции продолжают засчитываться."
        )


@router.message(Command("promo_list"))
async def cmd_promo_list(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    codes = db.list_promo_codes()
    if not codes:
        await message.answer("Промокодов пока нет. Создать: /promo_add")
        return
    lines = ["🎟 <b>Промокоды</b>\n"]
    for c in codes:
        status = "активен" if c["active"] else "выключен"
        lines.append(
            f"<code>{c['code']}</code> — {c['title']} · {status} · "
            f"переходов {c['total_visits']} · оплат {c['payments_count']} на "
            f"{c['revenue_stars']}⭐ (доля {c['partner_share_stars']}⭐)"
        )
    await _send_long(message, "\n".join(lines))


@router.message(Command("promo_stat"))
async def cmd_promo_stat(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Использование: /promo_stat &lt;код&gt;")
        return
    code = parts[1].strip().lower()
    stats = db.get_promo_stats(code)
    if stats is None:
        await message.answer(f"Промокод <code>{code}</code> не найден.")
        return
    await _send_long(message, _promo_stats_text(stats, admin_view=True))


@router.message(Command("promo_owner"))
async def cmd_promo_owner(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /promo_owner &lt;код&gt; &lt;@username или id&gt;")
        return
    code = parts[1].strip().lower()
    if db.get_promo_code(code) is None:
        await message.answer(f"Промокод <code>{code}</code> не найден.")
        return
    if parts[2] == "0":
        result = db.admin_set_promo_owner(code, None)
        if result == "ok":
            await message.answer(f"Владелец промокода <code>{code}</code> очищен.")
        return
    target_id = _resolve_target(parts[2])
    if target_id is None:
        await message.answer(
            f"Пользователь {parts[2]} не найден — бот узнаёт username только у тех, кто "
            "хотя бы раз ему писал, для остальных нужен числовой id."
        )
        return
    db.admin_set_promo_owner(code, target_id)
    await message.answer(f"Владелец промокода <code>{code}</code> назначен: {parts[2]}.")


@router.message(Command("promo_token"))
async def cmd_promo_token(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "new"):
        await message.answer("Использование: /promo_token &lt;код&gt; [new]")
        return
    code = parts[1].strip().lower()
    if len(parts) == 3:
        token = db.regenerate_promo_claim_token(code)
        if token is None:
            await message.answer(f"Промокод <code>{code}</code> не найден.")
            return
        await message.answer(
            f"Новое слово для <code>{code}</code>: <code>{token}</code>\n"
            "Старое больше не работает."
        )
        return
    token = db.get_promo_claim_token(code)
    if token is None:
        await message.answer(f"Промокод <code>{code}</code> не найден.")
        return
    await message.answer(f"Слово для <code>{code}</code>: <code>{token}</code>")


# Telegram's bot-upload limit is 50 MB; stop short of it rather than let a
# send attempt fail on a technicality right at the size boundary.
BACKUP_MAX_BYTES = 45 * 1024 * 1024


async def send_database_backup(bot, admin_id: int) -> None:
    """Builds a consistent DB snapshot (db.backup_database — SQLite's own
    backup API, not a raw file copy, safe under WAL and concurrent writes)
    and DMs it to one admin. Never touches whatever chat the request came
    from — callers always pass the admin's own user_id as the destination,
    so this can never end up posted in a group.

    The temp file lives in the OS temp directory (tempfile.mkstemp, no
    explicit dir= — defaults to $TMPDIR / /tmp), never inside the project
    / repo tree: that directory is what Railway wipes on every redeploy and
    is never picked up by git, so there's no path by which a DB snapshot
    could end up committed or survive longer than this one call needs it
    to. Deleted in a finally, whether the send succeeds, fails, or the
    backup itself blows up.

    db.backup_database is a blocking sqlite3 call — run via asyncio.to_thread
    so it can't stall the event loop / other message handling while it runs.

    Raises on failure (callers decide what to log/tell the admin) — this
    function itself doesn't swallow exceptions, callers do.
    """
    timestamp = datetime.datetime.now(_QUOTA_TZINFO).strftime("%Y-%m-%d-%H%M")
    filename = f"backup-{timestamp}.db"

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await asyncio.to_thread(db.backup_database, tmp_path)

        size = os.path.getsize(tmp_path)
        if size > BACKUP_MAX_BYTES:
            await bot.send_message(
                admin_id,
                f"⚠️ Бэкап базы весит {size / 1024 / 1024:.1f} МБ — это больше, чем "
                "Telegram разрешает загружать ботам. Нужен другой способ выгрузки, "
                "например прямой доступ к volume через Railway.",
            )
            return

        await bot.send_document(admin_id, FSInputFile(tmp_path, filename=filename))
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    admin_id = message.from_user.id
    try:
        await send_database_backup(message.bot, admin_id)
    except Exception:
        logger.exception("Manual /backup failed for admin_id=%s", admin_id)
        try:
            await message.bot.send_message(
                admin_id, "⚠️ Не удалось сделать бэкап базы — подробности в логах бота."
            )
        except Exception:
            pass  # admin may have blocked the bot, chat deleted, etc.


# Photo/PDF tasks are meant to be self-sufficient (photograph a problem, get
# an answer) — dialog history normally isn't sent along with them, so a long
# chat history can't push an already-big recognized problem past Groq's
# per-minute token limit. The one exception is a caption that explicitly
# refers back to earlier conversation ("а тут так же?", "как в прошлый раз",
# "продолжи") — checked here with plain substring matching, no model call.
# Extend this list as new phrasings show up in practice.
HISTORY_REFERENCE_PHRASES = (
    "так же",
    "такое же",
    "тот же",
    "та же",
    "то же",
    "как раньше",
    "как выше",
    "как до этого",
    "как в прошлый раз",
    "прошлый раз",
    "предыдущ",  # "предыдущее/-ий/-ему/..."
    "продолжи",
    "ещё раз",
    "еще раз",
    "аналогично",
    "по аналогии",
)


def _caption_references_history(caption: str) -> bool:
    lowered = caption.lower()
    return any(phrase in lowered for phrase in HISTORY_REFERENCE_PHRASES)


REMEMBER_PREFIXES = ("запомни:", "запомни,", "запомни ")


async def _answer_text_query(
    message: Message, state: FSMContext, text: str, user_id: int, username: str | None
) -> None:
    """Core pipeline for anything that resolves to a text question: typed
    messages, transcribed voice, and quick-action follow-ups alike. Callers
    have already handled remember/image-prefix detection."""
    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    opt = _model_option(status)
    model, reasoning_effort = opt["model"], opt["reasoning"]
    notes = [content for _id, content in db.list_notes(user_id)]

    history = db.get_dialog_history(user_id, MAX_HISTORY_TURNS)

    await message.bot.send_chat_action(message.chat.id, "typing")

    t0 = time.monotonic()
    try:
        reply_text, sources = await ai.ask_ai(
            history, text, model, notes=notes, reasoning_effort=reasoning_effort, enable_search=True
        )
        elapsed = time.monotonic() - t0

        db.log_message(user_id, username, "user", text)
        db.log_message(user_id, username, "assistant", reply_text)
        db.append_dialog_turn(user_id, text, reply_text, MAX_HISTORY_TURNS)

        footer = f"\n\n⚡ <i>Ответ за {elapsed:.1f} сек · {opt['label']}</i>"
        await _send_long(
            message,
            reply_text + footer,
            reply_markup=quick_actions_keyboard(),
            trusted_suffix=_build_sources_html(sources),
        )
    except ai.AIError as e:
        # The request already cost a message/credit above — an error means
        # the user got nothing for it, so give it back rather than making
        # them pay again to retry.
        db.refund_consumed_message(user_id, status["consumed"])
        await message.answer(e.user_message)
        return
    except Exception:
        db.refund_consumed_message(user_id, status["consumed"])
        raise

    # Referral bonus accounting is secondary to the reply above — a bug here
    # must never eat the response the user already paid a message for.
    try:
        await _credit_referral_progress(message.bot, user_id)
    except Exception:
        logger.exception("Referral credit progress failed for user_id=%s", user_id)


async def _process_text_query(message: Message, state: FSMContext, text: str) -> None:
    """Public entry point: checks remember/image prefixes first, then falls
    through to the shared answering pipeline."""
    lowered = text.strip().lower()

    for prefix in REMEMBER_PREFIXES:
        if lowered.startswith(prefix):
            note_text = text.strip()[len(prefix):].strip()
            if note_text:
                if _note_is_disallowed(note_text):
                    await message.answer(NOTE_REJECTED_TEXT)
                    return
                note_id = db.add_note(message.from_user.id, note_text)
                await message.answer(f"✅ Запомнил (заметка №{note_id}).")
                return
            break

    for prefix in IMAGE_PREFIXES:
        if lowered.startswith(prefix):
            prompt = text.strip()[len(prefix):].strip()
            await _process_image_request(
                message, state, prompt, message.from_user.id, message.from_user.username
            )
            return

    await _answer_text_query(message, state, text, message.from_user.id, message.from_user.username)


@router.callback_query(F.data == "qa:share")
async def cb_share(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    history = db.get_dialog_history(callback.from_user.id, MAX_HISTORY_TURNS)
    if not history or history[-1]["role"] != "assistant":
        await callback.message.answer("Нечего делиться — сначала задай вопрос.")
        return

    username = await _get_bot_username(callback.bot)
    link = f"https://t.me/{username}?start=ref_{callback.from_user.id}"
    share_text = (
        f"{history[-1]['content']}\n\n"
        f"—\n"
        f"🤖 Решено с помощью @{username} — бесплатный AI-ассистент в Telegram: "
        f"текст, фото, голос, PDF.\n"
        f"Попробуй: {link}"
    )
    await _send_long(callback.message, share_text)


@router.callback_query(F.data.startswith("qa:"))
async def cb_quick_action(callback: CallbackQuery, state: FSMContext) -> None:
    instruction = QUICK_ACTIONS.get(callback.data.split(":", 1)[1])
    await callback.answer()
    if not instruction:
        return
    lock = _get_user_lock(callback.from_user.id)
    if lock.locked():
        await callback.message.answer(BUSY_TEXT)
        return
    async with lock:
        await _answer_text_query(
            callback.message, state, instruction, callback.from_user.id, callback.from_user.username
        )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_chat_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return
    text = await _strip_mention(message, message.text)
    if not text:
        return
    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return
    async with lock:
        await _process_text_query(message, state, text)


# Whisper doesn't admit "I heard silence/noise" — on a quiet or noisy
# recording it instead hallucinates fragments of its training data (video
# subtitle credits, outro lines). Left unchecked, the bot took these at face
# value, spent a real AI answer on them, and charged the user's quota for
# it. Plain substring check against known artifacts, no model call —
# pluggable, extend as new ones show up in practice.
VOICE_HALLUCINATION_PHRASES = (
    "субтитры создавал",
    "субтитры делал",
    "редактор субтитров",
    "субтитры конец",
    "продолжение следует",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "dimatorzok",
)

MIN_VOICE_TEXT_CHARS = 3


def _looks_like_voice_hallucination(text: str) -> bool:
    if len(text) < MIN_VOICE_TEXT_CHARS:
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in VOICE_HALLUCINATION_PHRASES)


@router.message(F.voice)
async def handle_voice_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return

    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return

    async with lock:
        file_buf = await message.bot.download(message.voice.file_id)
        try:
            text = await ai.transcribe_audio(file_buf.read())
        except ai.AIError as e:
            await message.answer(e.user_message)
            return

        text = text.strip()
        if not text:
            await message.answer("Не удалось разобрать голосовое сообщение. Попробуйте ещё раз.")
            return

        await message.answer(f"🎙 <i>Распознано:</i> {text}")

        if _looks_like_voice_hallucination(text):
            # No quota has been touched yet at this point — try_consume_message
            # only runs inside _answer_text_query below, which this
            # deliberately never reaches, so there's nothing to refund; the
            # attempt is simply never billed in the first place.
            await message.answer(
                "Не разобрал голосовое — похоже, запись слишком тихая или зашумлённая. "
                "Попробуй записать ещё раз, ближе к микрофону."
            )
            return

        await _process_text_query(message, state, text)


MAX_PDF_CHARS = 20000


@router.message(F.document)
async def handle_document_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return

    user_id = message.from_user.id
    username = message.from_user.username

    doc = message.document
    filename = doc.file_name or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        await message.answer("Пока умею читать только PDF. Пришлите документ в формате .pdf.")
        return

    lock = _get_user_lock(user_id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return

    async with lock:
        allowed, status = db.try_consume_message(
            user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
        )
        if not allowed:
            await message.answer(quota_denied_text(status))
            return

        file_buf = await message.bot.download(doc.file_id)
        try:
            reader = PdfReader(file_buf)
            doc_text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception:
            db.refund_consumed_message(user_id, status["consumed"])
            await message.answer("Не удалось прочитать PDF. Возможно, файл повреждён.")
            return

        if not doc_text:
            db.refund_consumed_message(user_id, status["consumed"])
            await message.answer(
                "В этом PDF нет текстового слоя (похоже на скан без OCR). "
                "Пришлите страницы как фото — так я смогу распознать текст."
            )
            return

        truncated = len(doc_text) > MAX_PDF_CHARS
        doc_text = doc_text[:MAX_PDF_CHARS]

        caption = await _strip_mention(message, message.caption) or "Кратко перескажи документ и выдели главное."
        prompt = (
            f"Пользователь прислал PDF «{filename}» ({len(reader.pages)} стр."
            f"{', показана только часть текста' if truncated else ''}). Содержимое документа:\n\n"
            f"{doc_text}\n\n---\nЗадача: {caption}"
        )

        opt = _model_option(status)
        model, reasoning_effort = opt["model"], opt["reasoning"]
        notes = [content for _id, content in db.list_notes(user_id)]

        history = (
            db.get_dialog_history(user_id, MAX_HISTORY_TURNS)
            if _caption_references_history(caption)
            else []
        )

        await message.bot.send_chat_action(message.chat.id, "typing")

        try:
            reply_text, _ = await ai.ask_ai(
                history, prompt, model, notes=notes, reasoning_effort=reasoning_effort
            )

            short_ref = f"[документ «{filename}»] {caption}"
            db.log_message(user_id, username, "user", short_ref)
            db.log_message(user_id, username, "assistant", reply_text)

            # Keep a capped excerpt (not the full doc — history gets replayed
            # on every future turn) so follow-up questions about the same
            # PDF still have some content to work with without re-uploading it.
            MAX_DOC_HISTORY_CHARS = 2000
            history_entry = f"[Документ «{filename}»] {caption}\n\n{doc_text[:MAX_DOC_HISTORY_CHARS]}"
            db.append_dialog_turn(user_id, history_entry, reply_text, MAX_HISTORY_TURNS)

            await _send_long(message, reply_text, reply_markup=quick_actions_keyboard())
        except ai.AIError as e:
            # The request already cost a message/credit above — an error
            # means the user got nothing for it, so give it back rather than
            # making them pay again to retry.
            db.refund_consumed_message(user_id, status["consumed"])
            await message.answer(e.user_message)
            return
        except Exception:
            db.refund_consumed_message(user_id, status["consumed"])
            raise

        # Referral bonus accounting is secondary to the reply above — a bug
        # here must never eat the response the user already paid a message for.
        try:
            await _credit_referral_progress(message.bot, user_id)
        except Exception:
            logger.exception("Referral credit progress failed for user_id=%s", user_id)


@router.message(F.photo)
async def handle_photo_message(message: Message, state: FSMContext) -> None:
    if not await _should_respond_in_group(message):
        return
    lock = _get_user_lock(message.from_user.id)
    if lock.locked():
        await message.answer(BUSY_TEXT)
        return
    async with lock:
        await _handle_photo_message_locked(message, state)


async def _handle_photo_message_locked(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    username = message.from_user.username

    allowed, status = db.try_consume_message(
        user_id, username, DAILY_FREE_MESSAGES, DAILY_FREE_PREMIUM_MESSAGES, PREMIUM_CREDIT_COST
    )
    if not allowed:
        await message.answer(quota_denied_text(status))
        return

    caption = await _strip_mention(message, message.caption) or (
        "Реши задание на фото. Если это не задание — опиши, что на фото."
    )
    notes = [content for _id, content in db.list_notes(user_id)]

    file_buf = await message.bot.download(message.photo[-1].file_id)
    try:
        image_bytes = _prepare_image(file_buf.read())
    except Exception:
        db.refund_consumed_message(user_id, status["consumed"])
        await message.answer("Не удалось обработать изображение. Попробуйте другое фото.")
        return
    image_b64 = base64.b64encode(image_bytes).decode()
    data_url = f"data:image/jpeg;base64,{image_b64}"

    history = (
        db.get_dialog_history(user_id, MAX_HISTORY_TURNS)
        if _caption_references_history(caption)
        else []
    )

    await message.bot.send_chat_action(message.chat.id, "typing")
    status_msg = await message.answer("🔍 <i>Распознаю задание...</i>")
    t0 = time.monotonic()

    # Stage 1 — pure perception: get an accurate, literal description of
    # what's on the photo (text/problem AND general visual content — the
    # PREMIUM_MODEL doing the solving in stage 2 has no vision at all, so
    # whatever isn't captured here is permanently lost to it). Kept separate
    # from solving so the vision model (weaker at multi-step reasoning)
    # isn't also responsible for getting the actual answer right — it only
    # has to accurately see. Keeping the instruction short and explicitly
    # "no analysis" measurably cut down on the model over-thinking and
    # running into the reasoning-token budget (verified via repeated
    # testing — an elaborate "think carefully and fully" instruction was
    # the actual cause of intermittent truncation).
    transcription_request = [
        {
            "type": "text",
            "text": (
                "Describe exactly what is in this image. If it contains text/a problem "
                "(numbers, formulas, questions), transcribe it verbatim. If it's a photo "
                "or scene with no text, describe what's visually shown instead (objects, "
                "people, animals, colors, setting) in enough detail to answer questions "
                "about it. Be direct and concise — no analysis, no commentary, no extra "
                "reasoning, just the transcription/description."
            ),
        },
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        vision_text = await ai.ask_ai([], transcription_request, VISION_MODEL, max_tokens=3000)
    except ai.AIError as e:
        # The request already cost a message/credit above — an error means
        # the user got nothing for it, so give it back rather than making
        # them pay again to retry.
        db.refund_consumed_message(user_id, status["consumed"])
        await status_msg.edit_text(e.user_message)
        return

    try:
        await status_msg.edit_text("🧠 <i>Решаю...</i>")
    except TelegramBadRequest:
        pass

    # Stage 2 — actual solving/answering, always handed to the strongest
    # reasoning model regardless of the user's fast/premium chat preference:
    # accuracy on homework is the core promise of this bot, worth the extra
    # seconds. It never sees the photo itself, only stage 1's description.
    solve_prompt = (
        f"Вот описание фото, которое прислал пользователь:\n\n{vision_text}\n\n---\n"
        f"Запрос пользователя: {caption}\n\n"
        f"Ответь точно и по существу. Если это учебное задание — реши по шагам, а в конце "
        f"добавь короткий раздел «✅ Проверка:» с быстрой самопроверкой результата (например, "
        f"подстановкой ответа обратно в условие или другим способом решения). Если это не "
        f"вычислительная/логическая задача, а просто вопрос о содержимом фото — раздел "
        f"«Проверка» не нужен."
    )
    try:
        reply_text, _ = await ai.ask_ai(
            history, solve_prompt, PREMIUM_MODEL, notes=notes, reasoning_effort="high", max_tokens=6144
        )
    except ai.AIError as e:
        db.refund_consumed_message(user_id, status["consumed"])
        await status_msg.edit_text(e.user_message)
        return

    try:
        await status_msg.delete()
    except TelegramBadRequest:
        pass

    try:
        db.log_message(user_id, username, "user", f"[фото] {caption}")
        db.log_message(user_id, username, "assistant", reply_text)

        # Store the actual description (not just the caption) so a follow-up
        # text question like "а второе задание?" still has something to
        # work with — vision_text is already short (stage 1 is tuned for
        # brevity), so it's safe to keep in full rather than just a placeholder.
        history_entry = f"[Фото] {caption}\n\nСодержимое фото: {vision_text}"
        db.append_dialog_turn(user_id, history_entry, reply_text, MAX_HISTORY_TURNS)

        elapsed = time.monotonic() - t0
        has_check = "✅ Проверка" in reply_text or "Проверка:" in reply_text
        stage_label = "распознано → решено → проверено" if has_check else "распознано → решено"
        footer = f"\n\n⚡ <i>Ответ за {elapsed:.1f} сек · 🔬 {stage_label}</i>"
        await _send_long(message, reply_text + footer, reply_markup=quick_actions_keyboard())
    except Exception:
        # Both AI stages already succeeded at this point, but the reply
        # never actually reached the user — still a wasted request from
        # their side, so it still gets refunded.
        db.refund_consumed_message(user_id, status["consumed"])
        raise

    # Referral bonus accounting is secondary to the reply above — a bug here
    # must never eat the response the user already paid a message for.
    try:
        await _credit_referral_progress(message.bot, user_id)
    except Exception:
        logger.exception("Referral credit progress failed for user_id=%s", user_id)
