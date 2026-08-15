import datetime
import secrets
import sqlite3
import string
import threading
from zoneinfo import ZoneInfo

from bot.config import (
    DB_PATH,
    PROMO_BONUS_DAILY_MESSAGES,
    PROMO_BONUS_DAILY_PREMIUM_MESSAGES,
    QUOTA_TZ,
    SUBSCRIPTION_DAILY_MESSAGES,
    SUBSCRIPTION_DAILY_PREMIUM_MESSAGES,
)

_QUOTA_TZINFO = ZoneInfo(QUOTA_TZ)

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
# WAL lets readers and the writer avoid blocking each other; busy_timeout
# makes SQLite retry for up to 5s instead of raising "database is locked"
# immediately on contention; synchronous=NORMAL is the standard safe
# pairing with WAL (still durable across app crashes, just not against an
# OS-level power loss mid-write, which is an acceptable tradeoff here).
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA busy_timeout=5000")
_conn.execute("PRAGMA synchronous=NORMAL")
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        quota_date TEXT NOT NULL,
        messages_used_today INTEGER NOT NULL DEFAULT 0,
        bonus_credits INTEGER NOT NULL DEFAULT 0,
        model_pref TEXT NOT NULL DEFAULT 'fast'
    )
    """
)
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS chat_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        kind TEXT NOT NULL,
        amount_stars INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
# Conversation context fed to the model on each turn (distinct from chat_log,
# which is an admin-facing audit trail of everything ever sent/received and
# is never trimmed or read back into a prompt).
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS dialog_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_dialog_history_user_id ON dialog_history (user_id)"
)
# Tracks the deferred-payout state machine for referrals, separate from
# players.referred_by (which just remembers who invited whom, forever, for
# idempotency/anti-self-referral). One row per referred user: status starts
# 'pending' and only flips to 'paid' once message_count clears
# REFERRAL_MIN_MESSAGES *and* the referrer is still under REFERRAL_DAILY_CAP
# for the day — see try_credit_referral_message().
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        message_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        paid_at TEXT
    )
    """
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_referrals_referrer_id ON referrals (referrer_id)"
)
# Funnel analytics — see log_event()/get_funnel_stats(). Deliberately just
# (user_id, event, created_at): no extra metadata columns, so the funnel
# query stays a simple COUNT(DISTINCT user_id) per event per time window.
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        event TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_events_event_created_at ON events (event, created_at)"
)
# Blogger/partner promo codes — parallel to (and independent of) the
# referral program above. owner_user_id starts NULL: the admin creates the
# code first, the partner claims it later via /promo <code>. A code has no
# expiry and no auto-disable of any kind — it stays active until an admin
# runs /promo_off, however long that takes.
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY,
        owner_user_id INTEGER,
        title TEXT NOT NULL,
        bonus_minutes INTEGER NOT NULL,
        revenue_share INTEGER NOT NULL,
        window_days INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )
    """
)
# One row per user, forever — first promo code a user arrives through wins
# and is never overwritten, same "first wins" rule as players.referred_by
# for the referral program. joined_at anchors that user's own
# window_days-long attribution window (not the code's lifetime).
_conn.execute(
    """
    CREATE TABLE IF NOT EXISTS promo_visits (
        user_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        joined_at TEXT NOT NULL
    )
    """
)
_conn.execute("CREATE INDEX IF NOT EXISTS idx_promo_visits_code ON promo_visits (code)")
_conn.commit()

MAX_NOTES_PER_USER = 30


def _ensure_column(table: str, column: str, coldef: str) -> None:
    cur = _conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        _conn.commit()


# Excludes visually-similar characters (0/O/o, 1/l/I) so a partner reading
# the word off a phone screen or hearing it dictated doesn't mistype it.
_CLAIM_TOKEN_ALPHABET = "".join(
    c for c in (string.digits + string.ascii_letters) if c not in "0Oo1lI"
)


def _generate_claim_token() -> str:
    """8-character secret word gating who may claim a promo code's
    ownership — secrets.choice (not random), so it isn't guessable."""
    return "".join(secrets.choice(_CLAIM_TOKEN_ALPHABET) for _ in range(8))


_ensure_column("players", "premium_messages_used_today", "INTEGER NOT NULL DEFAULT 0")
_ensure_column("players", "last_active_at", "TEXT")
_ensure_column("players", "unlimited_until", "TEXT")
_ensure_column("players", "referred_by", "INTEGER")
# Which specific engine to use within the chosen tier (model_pref stays
# 'fast'/'premium' purely for quota billing — model_choice picks the actual
# Groq model, e.g. 'gptoss' vs 'llama', independent of that billing tier).
_ensure_column("players", "model_choice", "TEXT NOT NULL DEFAULT 'gptoss'")

# Monthly Stars subscription (separate from the one-off unlimited_until time
# passes above): subscription_until is the same "active until" idea but
# tracked independently since a subscription auto-renews and can be
# canceled; subscription_status distinguishes a live-and-renewing
# subscription from one the user canceled (still active until the period
# ends, just won't renew) from never-subscribed. subscription_charge_id is
# the most recent renewal's charge id, required by Telegram's
# editUserStarSubscription to act on the subscription.
_ensure_column("players", "subscription_until", "TEXT")
_ensure_column("players", "subscription_status", "TEXT NOT NULL DEFAULT 'none'")
_ensure_column("players", "subscription_charge_id", "TEXT")

# Opt-in daily "come back" reminder — off by default, only ever sent if the
# user explicitly enabled it and picked an hour themselves (see
# handlers.py's reminder menu). reminder_last_sent_date guards against
# sending twice in the same local day if the background check runs more
# than once during that hour.
_ensure_column("players", "reminder_enabled", "INTEGER NOT NULL DEFAULT 0")
_ensure_column("players", "reminder_hour", "INTEGER")
_ensure_column("players", "reminder_last_sent_date", "TEXT")

# telegram_payment_charge_id is what makes crediting idempotent: Telegram can
# redeliver a successful_payment update (e.g. bot restarted mid-delivery), and
# without this we'd credit the same purchase twice. It's also required to
# issue a refund later. status distinguishes a refunded payment from a live
# one so its credit isn't counted twice in stats or reversible twice.
_ensure_column("payments", "telegram_payment_charge_id", "TEXT")
_ensure_column("payments", "status", "TEXT NOT NULL DEFAULT 'paid'")
# credited_amount is the messages/minutes actually granted for this payment
# (independent of PACKAGES possibly changing later), so a refund can reverse
# exactly what was given regardless of subsequent price changes.
_ensure_column("payments", "credited_amount", "INTEGER")
# Which promo code (if any) this payment counts toward for partner revenue
# share — set at credit time in record_payment_and_credit() based on
# promo_visits + the code's window_days, never touched afterward (so a
# later window_days edit or the code going inactive doesn't retroactively
# change which past payments counted).
_ensure_column("payments", "promo_code", "TEXT")
_conn.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_charge_id "
    "ON payments (telegram_payment_charge_id)"
)

# Promo-code time bonus (see activate_promo_bonus) — deliberately its own
# column, separate from unlimited_until (the genuinely-unlimited purchased
# hourly pass, whose logic this must never touch).
_ensure_column("players", "promo_bonus_until", "TEXT")
# Snapshot taken the moment the bonus is granted: 1 if the user had no
# active subscription yet (their daily limits later stack with a
# subscription bought during the bonus window — see promo_effective_limits),
# 0 if they were already subscribed (limits don't stack — an existing
# subscriber can't double their allowance just by clicking a promo link).
_ensure_column("players", "promo_bonus_stacks", "INTEGER NOT NULL DEFAULT 0")

# Secret word gating who may claim a promo code's ownership via /promo
# <code> <word> — the code itself is public (it's in the shareable link),
# so ownership can't be "whoever asks first" the way it used to be.
_ensure_column("promo_codes", "claim_token", "TEXT")
# One-time backfill for codes created before claim_token existed — without
# this they'd be permanently unclaimable (nobody has a word for them).
_backfill_rows = _conn.execute(
    "SELECT code FROM promo_codes WHERE claim_token IS NULL OR claim_token = ''"
).fetchall()
for (_backfill_code,) in _backfill_rows:
    _conn.execute(
        "UPDATE promo_codes SET claim_token = ? WHERE code = ?",
        (_generate_claim_token(), _backfill_code),
    )
if _backfill_rows:
    _conn.commit()

_conn.commit()


def _utcnow_naive() -> datetime.datetime:
    """datetime.utcnow() is deprecated (timezone-naive, easy to misuse) —
    this gets the same value via the timezone-aware API and immediately
    strips the tzinfo back off. Every timestamp stored/compared in this
    file has always been a naive UTC ISO string; switching to storing
    offset-aware strings would silently break comparisons against every
    row written before this change (mixing naive and aware datetimes
    raises TypeError), so the on-disk format stays exactly as it was."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _now() -> str:
    return _utcnow_naive().isoformat(timespec="seconds")


def _today() -> str:
    """Calendar date in QUOTA_TZ, not UTC/system time — the daily message
    quota rolls over at local midnight instead of e.g. 3am Moscow time
    (which is what UTC midnight was silently doing before)."""
    return datetime.datetime.now(_QUOTA_TZINFO).date().isoformat()


def _today_start_utc() -> str:
    """The UTC instant of today's midnight in QUOTA_TZ, as a naive-UTC ISO
    string. Every timestamp column in this file (created_at, last_active_at,
    paid_at, ...) is naive UTC — bounding a "since today" query with the
    bare _today() date string would compare a QUOTA_TZ-local calendar day
    against UTC-local timestamps and drift by the timezone offset near
    midnight. This converts local midnight to the matching UTC instant so
    "today" means the same day as _today() everywhere it's used."""
    local_midnight = datetime.datetime.now(_QUOTA_TZINFO).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (
        local_midnight.astimezone(datetime.timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _ensure_player(user_id: int, username: str | None) -> None:
    # COALESCE keeps the previously known username when this call passes None
    # (e.g. admin actions that only have a user_id) instead of blanking it out.
    _conn.execute(
        """
        INSERT INTO players (user_id, username, quota_date, messages_used_today, bonus_credits, model_pref, last_active_at)
        VALUES (?, ?, ?, 0, 0, 'fast', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = COALESCE(excluded.username, players.username),
            last_active_at = excluded.last_active_at
        """,
        (user_id, username, _today(), _now()),
    )
    _conn.commit()


def _reset_if_new_day(user_id: int) -> None:
    today = _today()
    cur = _conn.execute("SELECT quota_date FROM players WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row and row[0] != today:
        _conn.execute(
            "UPDATE players SET quota_date = ?, messages_used_today = 0, premium_messages_used_today = 0 "
            "WHERE user_id = ?",
            (today, user_id),
        )
        _conn.commit()


def _active_unlimited_until(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        expiry = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return raw if expiry > _utcnow_naive() else None


def get_status(user_id: int, username: str | None) -> dict:
    with _lock:
        _ensure_player(user_id, username)
        _reset_if_new_day(user_id)
        cur = _conn.execute(
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, "
            "model_pref, unlimited_until, model_choice, subscription_until, subscription_status, "
            "promo_bonus_until, promo_bonus_stacks "
            "FROM players WHERE user_id = ?",
            (user_id,),
        )
        (
            used, premium_used, bonus, model_pref, unlimited_until, model_choice,
            subscription_until, subscription_status, promo_bonus_until, promo_bonus_stacks,
        ) = cur.fetchone()
        return {
            "used_today": used,
            "premium_used_today": premium_used,
            "bonus_credits": bonus,
            "model_pref": model_pref,
            "unlimited_until": _active_unlimited_until(unlimited_until),
            "model_choice": model_choice,
            "subscription_until": _active_unlimited_until(subscription_until),
            "subscription_status": subscription_status,
            "promo_bonus_until": _active_unlimited_until(promo_bonus_until),
            "promo_bonus_stacks": bool(promo_bonus_stacks),
        }


def set_model_pref(user_id: int, username: str | None, model_pref: str, model_choice: str) -> None:
    with _lock:
        _ensure_player(user_id, username)
        _conn.execute(
            "UPDATE players SET model_pref = ?, model_choice = ? WHERE user_id = ?",
            (model_pref, model_choice, user_id),
        )
        _conn.commit()


def try_consume_message(
    user_id: int,
    username: str | None,
    daily_limit: int,
    daily_premium_limit: int,
    premium_cost: int,
    force_premium: bool = False,
) -> tuple[bool, dict]:
    """Atomically consumes quota for one message, based on the player's
    model_pref — unless force_premium is set, which bills against the
    premium bucket regardless of model_pref (for features that are always
    premium-tier value regardless of the user's chat model choice, e.g.
    image generation).

    Checked in this order:
      1. Active time-based unlimited pass (bought by the hour) — genuinely
         unlimited, nothing touched, since it's already bounded by time.
      2. Active promo-code bonus (time-boxed, from a partner's link) — draws
         from the same counters as the free tier, but compared against a
         daily cap of its own (see promo_effective_limits) that can stack
         with an active subscription's cap or not, depending on
         promo_bonus_stacks. Checked before the subscription on its own
         (i.e. a promo bonus is never silently ignored just because a
         subscription also exists) because it expires and the subscription
         doesn't.
      3. Active subscription — draws from the same counters as the free
         tier, but compared against SUBSCRIPTION_DAILY_MESSAGES /
         SUBSCRIPTION_DAILY_PREMIUM_MESSAGES instead of `daily_limit` /
         `daily_premium_limit` (a subscription is a bigger daily allowance,
         not true unlimited — one heavy user shouldn't eat the whole
         margin on a flat monthly price). Falls back to bonus_credits once
         that's exhausted, same as the free tier does.
      4. Free tier — free daily quota first (`daily_limit`/
         `daily_premium_limit`), then bonus_credits.

    Returns (allowed, status) where status mirrors get_status's shape after
    the attempt, plus "consumed" (what was taken, for refund_consumed_message)
    and "limit_source" ("promo", "subscription" or "free" — which daily
    allowance was actually being checked, for quota_denied_text).
    """
    with _lock:
        _ensure_player(user_id, username)
        _reset_if_new_day(user_id)
        cur = _conn.execute(
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, "
            "model_pref, unlimited_until, model_choice, subscription_until, subscription_status, "
            "promo_bonus_until, promo_bonus_stacks "
            "FROM players WHERE user_id = ?",
            (user_id,),
        )
        (
            used, premium_used, bonus, model_pref, unlimited_raw, model_choice,
            subscription_raw, subscription_status, promo_bonus_raw, promo_bonus_stacks,
        ) = cur.fetchone()
        unlimited_until = _active_unlimited_until(unlimited_raw)
        subscription_until = _active_unlimited_until(subscription_raw)
        promo_bonus_until = _active_unlimited_until(promo_bonus_raw)

        def status(
            used=used, premium_used=premium_used, bonus=bonus, consumed=None, limit_source="free"
        ) -> dict:
            return {
                "used_today": used,
                "premium_used_today": premium_used,
                "bonus_credits": bonus,
                "model_pref": model_pref,
                "unlimited_until": unlimited_until,
                "model_choice": model_choice,
                "subscription_until": subscription_until,
                "subscription_status": subscription_status,
                "promo_bonus_until": promo_bonus_until,
                "promo_bonus_stacks": bool(promo_bonus_stacks),
                # What this call actually took, so a failed request (no
                # answer produced) can be refunded via refund_consumed_message
                # instead of silently costing the user a wasted message.
                "consumed": consumed,
                # Which daily allowance was actually checked ("promo",
                # "subscription" or "free") — quota_denied_text needs this
                # to explain the right thing when denied.
                "limit_source": limit_source,
            }

        if unlimited_until:
            # Active time pass: no quota/credits touched at all.
            return True, status()

        if promo_bonus_until:
            effective_daily_limit, effective_daily_premium_limit = promo_effective_limits(
                True, bool(promo_bonus_stacks), subscription_until is not None
            )
            limit_source = "promo"
        elif subscription_until:
            effective_daily_limit = SUBSCRIPTION_DAILY_MESSAGES
            effective_daily_premium_limit = SUBSCRIPTION_DAILY_PREMIUM_MESSAGES
            limit_source = "subscription"
        else:
            effective_daily_limit = daily_limit
            effective_daily_premium_limit = daily_premium_limit
            limit_source = "free"

        if force_premium or model_pref == "premium":
            if premium_used < effective_daily_premium_limit:
                _conn.execute(
                    "UPDATE players SET premium_messages_used_today = premium_messages_used_today + 1 "
                    "WHERE user_id = ?",
                    (user_id,),
                )
                _conn.commit()
                return True, status(
                    premium_used=premium_used + 1,
                    consumed={"bucket": "premium_quota", "amount": 1},
                    limit_source=limit_source,
                )

            if bonus < premium_cost:
                return False, status(limit_source=limit_source)
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - ? WHERE user_id = ?",
                (premium_cost, user_id),
            )
            _conn.commit()
            return True, status(
                bonus=bonus - premium_cost,
                consumed={"bucket": "bonus_credits", "amount": premium_cost},
                limit_source=limit_source,
            )

        if used < effective_daily_limit:
            _conn.execute(
                "UPDATE players SET messages_used_today = messages_used_today + 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, status(
                used=used + 1,
                consumed={"bucket": "fast_quota", "amount": 1},
                limit_source=limit_source,
            )

        if bonus > 0:
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, status(
                bonus=bonus - 1,
                consumed={"bucket": "bonus_credits", "amount": 1},
                limit_source=limit_source,
            )

        return False, status(limit_source=limit_source)


def refund_consumed_message(user_id: int, consumed: dict | None) -> None:
    """Reverses exactly what a try_consume_message() call took, for a
    request that consumed quota/credits but failed before producing an
    answer — so a Groq error, a bug, or any other failure doesn't cost the
    user a message they got nothing for. No-op if nothing was actually
    consumed (e.g. the user was on an active unlimited pass/subscription)."""
    if not consumed:
        return
    bucket, amount = consumed["bucket"], consumed["amount"]
    with _lock:
        if bucket == "fast_quota":
            _conn.execute(
                "UPDATE players SET messages_used_today = MAX(0, messages_used_today - ?) "
                "WHERE user_id = ?",
                (amount, user_id),
            )
        elif bucket == "premium_quota":
            _conn.execute(
                "UPDATE players SET premium_messages_used_today = "
                "MAX(0, premium_messages_used_today - ?) WHERE user_id = ?",
                (amount, user_id),
            )
        elif bucket == "bonus_credits":
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
                (amount, user_id),
            )
        _conn.commit()


def add_bonus_credits(user_id: int, username: str | None, amount: int) -> int:
    with _lock:
        _ensure_player(user_id, username)
        _conn.execute(
            "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
            (amount, user_id),
        )
        _conn.commit()
        cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (user_id,))
        return cur.fetchone()[0]


def admin_add_bonus_credits(user_id: int, amount: int) -> int | None:
    """Grants/deducts bonus credits for an admin action: unlike
    add_bonus_credits, doesn't touch last_active_at (this isn't the user
    being active) and doesn't create a new player row for an unknown id
    (returns None instead, so the admin gets a clear "not found")."""
    with _lock:
        cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None:
            return None
        _conn.execute(
            "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
            (amount, user_id),
        )
        _conn.commit()
        cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (user_id,))
        return cur.fetchone()[0]


def try_consume_bonus_credits(user_id: int, username: str | None, cost: int) -> tuple[bool, int]:
    """Spends `cost` bonus credits if available (used for paid-only extras like
    image generation, which don't draw from the daily free quota)."""
    with _lock:
        _ensure_player(user_id, username)
        cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (user_id,))
        (bonus,) = cur.fetchone()
        if bonus < cost:
            return False, bonus
        _conn.execute(
            "UPDATE players SET bonus_credits = bonus_credits - ? WHERE user_id = ?",
            (cost, user_id),
        )
        _conn.commit()
        return True, bonus - cost


def add_note(user_id: int, content: str) -> int:
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,))
        (count,) = cur.fetchone()
        if count >= MAX_NOTES_PER_USER:
            _conn.execute(
                "DELETE FROM notes WHERE id = ("
                "  SELECT id FROM notes WHERE user_id = ? ORDER BY id LIMIT 1"
                ")",
                (user_id,),
            )
        cur = _conn.execute(
            "INSERT INTO notes (user_id, content, created_at) VALUES (?, ?, ?)",
            (user_id, content, _today()),
        )
        _conn.commit()
        return cur.lastrowid


def list_notes(user_id: int) -> list[tuple[int, str]]:
    with _lock:
        cur = _conn.execute(
            "SELECT id, content FROM notes WHERE user_id = ? ORDER BY id", (user_id,)
        )
        return cur.fetchall()


def delete_note(user_id: int, note_id: int) -> bool:
    with _lock:
        cur = _conn.execute(
            "DELETE FROM notes WHERE user_id = ? AND id = ?", (user_id, note_id)
        )
        _conn.commit()
        return cur.rowcount > 0


def log_message(user_id: int, username: str | None, role: str, content: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO chat_log (user_id, username, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, role, content, _now()),
        )
        _conn.commit()


def get_recent_chat(user_id: int, limit: int = 20) -> list[tuple[str, str, str]]:
    with _lock:
        cur = _conn.execute(
            "SELECT role, content, created_at FROM chat_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = cur.fetchall()
        return list(reversed(rows))


def delete_old_chat_log(retention_days: int) -> int:
    """Deletes chat_log rows older than `retention_days`. Unlike
    dialog_history (already capped at MAX_HISTORY_TURNS per user), chat_log
    is the admin support/audit trail (/chatlog) and has no other size cap,
    so without this it grows unboundedly on disk forever. Returns how many
    rows were deleted, for startup/daily-task logging. Admin access to
    what's left is untouched — this only prunes the tail, not the feature."""
    with _lock:
        cutoff = (_utcnow_naive() - datetime.timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        cur = _conn.execute("DELETE FROM chat_log WHERE created_at < ?", (cutoff,))
        _conn.commit()
        return cur.rowcount


def get_dialog_history(user_id: int, max_turns: int) -> list[dict]:
    """Returns this user's conversation context (oldest first, as
    {"role", "content"} dicts ready to feed straight into ask_ai), capped to
    the most recent `max_turns` user+assistant turns. Persisted in the
    dialog_history table rather than FSM memory, so it survives restarts —
    unlike chat_log (admin audit trail), this is what actually gets replayed
    into the model's context."""
    with _lock:
        cur = _conn.execute(
            "SELECT role, content FROM dialog_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, max_turns * 2),
        )
        rows = cur.fetchall()
        return [{"role": role, "content": content} for role, content in reversed(rows)]


def append_dialog_turn(
    user_id: int, user_content: str, assistant_content: str, max_turns: int
) -> None:
    """Appends one user+assistant turn and prunes anything older than the
    most recent `max_turns` turns for this user, so the table doesn't grow
    unboundedly per user the way the old in-memory history would have (it
    just lost everything on restart instead; this keeps the same cap but
    persists it)."""
    with _lock:
        now = _now()
        _conn.execute(
            "INSERT INTO dialog_history (user_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (user_id, user_content, now),
        )
        _conn.execute(
            "INSERT INTO dialog_history (user_id, role, content, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (user_id, assistant_content, now),
        )
        _conn.execute(
            "DELETE FROM dialog_history WHERE user_id = ? AND id NOT IN ("
            "  SELECT id FROM dialog_history WHERE user_id = ? ORDER BY id DESC LIMIT ?"
            ")",
            (user_id, user_id, max_turns * 2),
        )
        _conn.commit()


def clear_dialog_history(user_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM dialog_history WHERE user_id = ?", (user_id,))
        _conn.commit()


def list_users(limit: int = 30, offset: int = 0) -> list[tuple]:
    with _lock:
        cur = _conn.execute(
            "SELECT user_id, username, messages_used_today, premium_messages_used_today, "
            "bonus_credits, model_pref, last_active_at "
            "FROM players ORDER BY last_active_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return cur.fetchall()


def count_users() -> int:
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) FROM players")
        return cur.fetchone()[0]


def get_player(user_id: int) -> dict | None:
    """Read-only lookup for admin display — unlike get_status, doesn't
    upsert a row or touch last_active_at/username."""
    with _lock:
        cur = _conn.execute(
            "SELECT user_id, username, messages_used_today, premium_messages_used_today, "
            "bonus_credits, model_pref, model_choice, unlimited_until, last_active_at "
            "FROM players WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        uid, uname, used, premium_used, bonus, pref, choice, unlimited_raw, last_active = row
        return {
            "user_id": uid,
            "username": uname,
            "used_today": used,
            "premium_used_today": premium_used,
            "bonus_credits": bonus,
            "model_pref": pref,
            "model_choice": choice,
            "unlimited_until": _active_unlimited_until(unlimited_raw),
            "last_active_at": last_active,
        }


def find_user_id_by_username(username: str) -> int | None:
    """Look up a user_id by @username (case-insensitive). Only works for
    users who have messaged the bot at least once, since that's the only
    way we learn their username."""
    with _lock:
        cur = _conn.execute(
            "SELECT user_id FROM players WHERE username = ? COLLATE NOCASE",
            (username,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def activate_unlimited(user_id: int, username: str | None, minutes: int) -> str:
    """Grants (or extends) unrestricted access for `minutes`. Stacks on top of
    an already-active window instead of overwriting it, so buying another pass
    before the current one expires adds to the remaining time."""
    with _lock:
        _ensure_player(user_id, username)
        cur = _conn.execute("SELECT unlimited_until FROM players WHERE user_id = ?", (user_id,))
        (current_raw,) = cur.fetchone()
        now = _utcnow_naive()
        base = now
        active = _active_unlimited_until(current_raw)
        if active:
            base = datetime.datetime.fromisoformat(active)
        new_expiry = (base + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        _conn.execute(
            "UPDATE players SET unlimited_until = ? WHERE user_id = ?", (new_expiry, user_id)
        )
        _conn.commit()
        return new_expiry


def promo_effective_limits(
    promo_bonus_active: bool, promo_bonus_stacks: bool, subscription_active: bool
) -> tuple[int, int] | None:
    """Pure (no DB access) — the daily (fast, premium) limits an active
    promo bonus grants. Called from both try_consume_message (spending) and
    handlers.py (display), so the two can never disagree on the number.

    - No promo bonus active: None.
    - Promo bonus active, no subscription active right now: just the
      bonus's own daily cap.
    - Both active, promo_bonus_stacks (user had no subscription when they
      joined via the promo link): the two caps ADD UP — a reward for
      converting to a paying subscriber during the bonus window.
    - Both active, not promo_bonus_stacks (user was already subscribed when
      they joined via the link): the LARGER of the two caps applies, not
      the sum — an existing subscriber can't double their daily allowance
      just by clicking a public promo link.
    """
    if not promo_bonus_active:
        return None
    if not subscription_active:
        return (PROMO_BONUS_DAILY_MESSAGES, PROMO_BONUS_DAILY_PREMIUM_MESSAGES)
    if promo_bonus_stacks:
        return (
            PROMO_BONUS_DAILY_MESSAGES + SUBSCRIPTION_DAILY_MESSAGES,
            PROMO_BONUS_DAILY_PREMIUM_MESSAGES + SUBSCRIPTION_DAILY_PREMIUM_MESSAGES,
        )
    return (
        max(PROMO_BONUS_DAILY_MESSAGES, SUBSCRIPTION_DAILY_MESSAGES),
        max(PROMO_BONUS_DAILY_PREMIUM_MESSAGES, SUBSCRIPTION_DAILY_PREMIUM_MESSAGES),
    )


def activate_promo_bonus(user_id: int, username: str | None, minutes: int) -> str:
    """Like activate_unlimited, but for the promo-code time bonus: writes
    promo_bonus_until (a capped daily allowance — see promo_effective_limits
    and try_consume_message) instead of unlimited_until (genuinely
    unlimited, reserved for purchased hourly passes — never touched here).
    Also snapshots, at the moment the bonus is granted, whether the user
    already had an active subscription — see promo_bonus_stacks."""
    with _lock:
        _ensure_player(user_id, username)
        cur = _conn.execute(
            "SELECT promo_bonus_until, subscription_until FROM players WHERE user_id = ?",
            (user_id,),
        )
        current_raw, subscription_raw = cur.fetchone()
        base = _utcnow_naive()
        active = _active_unlimited_until(current_raw)
        if active:
            base = datetime.datetime.fromisoformat(active)
        new_expiry = (base + datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        stacks = 0 if _active_unlimited_until(subscription_raw) else 1
        _conn.execute(
            "UPDATE players SET promo_bonus_until = ?, promo_bonus_stacks = ? WHERE user_id = ?",
            (new_expiry, stacks, user_id),
        )
        _conn.commit()
        return new_expiry


def record_payment_and_credit(
    user_id: int,
    username: str | None,
    kind: str,
    amount_stars: int,
    charge_id: str,
    amount: int,
) -> tuple[str, str | int | None]:
    """Records a Stars payment and applies its credit as a single unit: if
    granting the credit fails after the payment row is inserted, the whole
    thing rolls back rather than leaving a payment marked 'paid' with no
    credit applied. Guarded by telegram_payment_charge_id's unique index, so
    a redelivered successful_payment update (e.g. Telegram retrying after a
    restart) can never double-credit — it just reports "duplicate".

    `kind` is 'messages', 'unlimited' or 'subscription'; `amount` is the
    messages count / minutes / days to grant, matching `kind` (also stored
    as credited_amount so a later refund can reverse exactly this, even if
    prices change in the meantime).

    Each subscription renewal arrives as a genuinely new charge_id (Telegram
    mints one per period), so it's never treated as a duplicate — it just
    stacks another `amount` days onto subscription_until, same as buying a
    second time pass before the first expires, and (re)marks the
    subscription 'active' with this charge_id (needed later to cancel it).

    Returns (outcome, result):
      "duplicate" -> charge_id already processed, nothing changed, result None
      "credited"  -> result is the new bonus_credits balance (messages), the
                     new unlimited_until timestamp (unlimited), or the new
                     subscription_until timestamp (subscription)
    """
    with _lock:
        _ensure_player(user_id, username)
        promo_code = _resolve_promo_for_payment(user_id)
        try:
            _conn.execute(
                "INSERT INTO payments (user_id, username, kind, amount_stars, created_at, "
                "telegram_payment_charge_id, credited_amount, status, promo_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'paid', ?)",
                (user_id, username, kind, amount_stars, _now(), charge_id, amount, promo_code),
            )
        except sqlite3.IntegrityError:
            _conn.rollback()
            return "duplicate", None

        try:
            if kind == "unlimited":
                cur = _conn.execute(
                    "SELECT unlimited_until FROM players WHERE user_id = ?", (user_id,)
                )
                (current_raw,) = cur.fetchone()
                base = _utcnow_naive()
                active = _active_unlimited_until(current_raw)
                if active:
                    base = datetime.datetime.fromisoformat(active)
                new_expiry = (base + datetime.timedelta(minutes=amount)).isoformat(timespec="seconds")
                _conn.execute(
                    "UPDATE players SET unlimited_until = ? WHERE user_id = ?", (new_expiry, user_id)
                )
                _conn.commit()
                return "credited", new_expiry

            if kind == "subscription":
                cur = _conn.execute(
                    "SELECT subscription_until FROM players WHERE user_id = ?", (user_id,)
                )
                (current_raw,) = cur.fetchone()
                base = _utcnow_naive()
                active = _active_unlimited_until(current_raw)
                if active:
                    base = datetime.datetime.fromisoformat(active)
                new_expiry = (base + datetime.timedelta(days=amount)).isoformat(timespec="seconds")
                _conn.execute(
                    "UPDATE players SET subscription_until = ?, subscription_status = 'active', "
                    "subscription_charge_id = ? WHERE user_id = ?",
                    (new_expiry, charge_id, user_id),
                )
                _conn.commit()
                return "credited", new_expiry

            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
                (amount, user_id),
            )
            _conn.commit()
            cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (user_id,))
            return "credited", cur.fetchone()[0]
        except Exception:
            _conn.rollback()
            raise


def get_active_subscription_charge_id(user_id: int) -> str | None:
    """The charge_id needed to call Telegram's editUserStarSubscription —
    only returned while the subscription is still within its paid period
    (canceled-but-not-yet-expired counts; fully lapsed doesn't, since
    Telegram would reject acting on a subscription that's already over)."""
    with _lock:
        cur = _conn.execute(
            "SELECT subscription_until, subscription_charge_id FROM players WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        until_raw, charge_id = row
        if not charge_id or not _active_unlimited_until(until_raw):
            return None
        return charge_id


def set_subscription_status(user_id: int, status: str) -> None:
    """Persists 'active'/'canceled' after a successful
    editUserStarSubscription call — doesn't touch subscription_until
    (cancellation stops renewal, it doesn't cut the current period short)."""
    with _lock:
        _conn.execute(
            "UPDATE players SET subscription_status = ? WHERE user_id = ?", (status, user_id)
        )
        _conn.commit()


def get_payment(payment_id: int) -> dict | None:
    with _lock:
        cur = _conn.execute(
            "SELECT id, user_id, username, kind, amount_stars, credited_amount, "
            "telegram_payment_charge_id, status, created_at, promo_code FROM payments WHERE id = ?",
            (payment_id,),
        )
        row = cur.fetchone()
        return _payment_row_to_dict(row)


def get_payment_by_charge_id(charge_id: str) -> dict | None:
    with _lock:
        cur = _conn.execute(
            "SELECT id, user_id, username, kind, amount_stars, credited_amount, "
            "telegram_payment_charge_id, status, created_at, promo_code FROM payments "
            "WHERE telegram_payment_charge_id = ?",
            (charge_id,),
        )
        row = cur.fetchone()
        return _payment_row_to_dict(row)


def _payment_row_to_dict(row: tuple | None) -> dict | None:
    if row is None:
        return None
    pid, uid, uname, kind, stars, credited_amount, charge_id, status, created_at, promo_code = row
    return {
        "id": pid,
        "user_id": uid,
        "username": uname,
        "kind": kind,
        "amount_stars": stars,
        "credited_amount": credited_amount,
        "charge_id": charge_id,
        "status": status,
        "created_at": created_at,
        "promo_code": promo_code,
    }


def list_recent_payments(user_id: int, limit: int = 5) -> list[tuple]:
    with _lock:
        cur = _conn.execute(
            "SELECT id, kind, amount_stars, credited_amount, telegram_payment_charge_id, "
            "status, created_at FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return cur.fetchall()


def refund_payment(charge_id: str) -> dict | None:
    """Marks a 'paid' payment 'refunded' and reverses its credit: bonus
    credits are clamped at 0 (never taken negative — if the user already
    spent them, the rest just isn't clawed back), and unlimited-time minutes
    are subtracted from unlimited_until the same way activate_unlimited adds
    them, which can naturally push it into the past (i.e. no longer active).
    Returns what was reversed, or None if the charge_id is unknown or the
    payment isn't in 'paid' status (already refunded, so calling again is a
    no-op rather than double-reversing)."""
    with _lock:
        cur = _conn.execute(
            "SELECT user_id, kind, credited_amount, status FROM payments "
            "WHERE telegram_payment_charge_id = ?",
            (charge_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        user_id, kind, credited_amount, status = row
        if status != "paid":
            return None

        _conn.execute(
            "UPDATE payments SET status = 'refunded' WHERE telegram_payment_charge_id = ?",
            (charge_id,),
        )

        if credited_amount:
            if kind == "messages":
                _conn.execute(
                    "UPDATE players SET bonus_credits = MAX(0, bonus_credits - ?) WHERE user_id = ?",
                    (credited_amount, user_id),
                )
            elif kind == "unlimited":
                cur = _conn.execute(
                    "SELECT unlimited_until FROM players WHERE user_id = ?", (user_id,)
                )
                current_row = cur.fetchone()
                if current_row and current_row[0]:
                    try:
                        current_dt = datetime.datetime.fromisoformat(current_row[0])
                        new_dt = current_dt - datetime.timedelta(minutes=credited_amount)
                        _conn.execute(
                            "UPDATE players SET unlimited_until = ? WHERE user_id = ?",
                            (new_dt.isoformat(timespec="seconds"), user_id),
                        )
                    except ValueError:
                        pass

        _conn.commit()
        return {"user_id": user_id, "kind": kind, "credited_amount": credited_amount}


def get_admin_stats() -> dict:
    with _lock:
        today_start = _today_start_utc()
        week_ago = (_utcnow_naive() - datetime.timedelta(days=7)).isoformat(timespec="seconds")

        cur = _conn.execute("SELECT COUNT(*) FROM players")
        (total_users,) = cur.fetchone()

        cur = _conn.execute(
            "SELECT COUNT(*) FROM players WHERE last_active_at >= ?", (today_start,)
        )
        (active_today,) = cur.fetchone()

        cur = _conn.execute("SELECT COALESCE(SUM(bonus_credits), 0) FROM players")
        (bonus_outstanding,) = cur.fetchone()

        def revenue_since(since: str) -> tuple[int, int]:
            cur = _conn.execute(
                "SELECT COALESCE(SUM(amount_stars), 0), COUNT(*) FROM payments WHERE created_at >= ?",
                (since,),
            )
            return cur.fetchone()

        revenue_today, payments_today = revenue_since(today_start)
        revenue_7d, payments_7d = revenue_since(week_ago)
        revenue_all, payments_all = revenue_since("")

        return {
            "total_users": total_users,
            "active_today": active_today,
            "bonus_outstanding": bonus_outstanding,
            "revenue_today": revenue_today,
            "payments_today": payments_today,
            "revenue_7d": revenue_7d,
            "payments_7d": payments_7d,
            "revenue_all": revenue_all,
            "payments_all": payments_all,
        }


FUNNEL_EVENTS = ("buy_opened", "invoice_sent", "paid", "promo_join")


def log_event(user_id: int, event: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO events (user_id, event, created_at) VALUES (?, ?, ?)",
            (user_id, event, _now()),
        )
        _conn.commit()


def get_funnel_stats() -> dict:
    """Purchase funnel — how many distinct users hit each stage, for today
    and the last 7 days. Pricing was otherwise being tuned blind."""
    with _lock:
        today_start = _today_start_utc()
        week_ago = (_utcnow_naive() - datetime.timedelta(days=7)).isoformat(timespec="seconds")

        def counts_since(since: str) -> dict:
            result = {}
            for event in FUNNEL_EVENTS:
                cur = _conn.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM events WHERE event = ? AND created_at >= ?",
                    (event, since),
                )
                result[event] = cur.fetchone()[0]
            return result

        return {"today": counts_since(today_start), "week": counts_since(week_ago)}


def set_referrer(user_id: int, referrer_id: int) -> bool:
    """Records who referred this user, but only the first time and never for
    self-referrals. Returns True iff this call actually set it (i.e. a
    pending referral should now be registered via register_referral());
    False means already set/invalid. Doesn't pay anything by itself — see
    register_referral() and try_credit_referral_message()."""
    if referrer_id == user_id:
        return False
    with _lock:
        cur = _conn.execute("SELECT referred_by FROM players WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row is None or row[0] is not None:
            return False
        _conn.execute(
            "UPDATE players SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id)
        )
        _conn.commit()
        return True


def register_referral(user_id: int, referrer_id: int) -> None:
    """Opens the deferred-payout state for a referral, right after
    set_referrer() succeeds. Pays nothing — a fake account that never sends
    a real message never earns anything for its referrer."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO referrals (user_id, referrer_id, status, message_count, created_at) "
            "VALUES (?, ?, 'pending', 0, ?)",
            (user_id, referrer_id, _now()),
        )
        _conn.commit()


def try_credit_referral_message(
    user_id: int, min_messages: int, daily_cap: int, bonus_messages: int
) -> dict | None:
    """Call after each real (quota-approved) message a user sends. If this
    user has a pending referral, bumps its message counter; once it reaches
    `min_messages`, pays `bonus_messages` to both the referee and the
    referrer — but only if the referrer hasn't already had `daily_cap`
    referrals paid out today (a farm of fake accounts can register any
    number of pending referrals, but can only cash in `daily_cap` of them
    per day no matter how many accounts it controls). If the cap is full,
    the referral stays pending and is simply retried on this user's next
    message — so it pays out naturally once the day rolls over or the
    referrer's cap frees up, no background job needed.

    Returns a dict describing the payout (for notifying the referrer) if
    one just happened, else None.
    """
    with _lock:
        cur = _conn.execute(
            "SELECT referrer_id, message_count FROM referrals WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        referrer_id, message_count = row
        message_count += 1
        _conn.execute(
            "UPDATE referrals SET message_count = ? WHERE user_id = ?", (message_count, user_id)
        )

        if message_count < min_messages:
            _conn.commit()
            return None

        today_start = _today_start_utc()
        cur = _conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'paid' AND paid_at >= ?",
            (referrer_id, today_start),
        )
        (paid_today,) = cur.fetchone()
        if paid_today >= daily_cap:
            _conn.commit()  # keep the counter bump; still pending, retried next message
            return None

        now = _now()
        _conn.execute(
            "UPDATE referrals SET status = 'paid', paid_at = ? WHERE user_id = ?", (now, user_id)
        )
        # Both sides are guaranteed to already have a players row in
        # practice (the referee via the get_status() call in _apply_referral
        # before set_referrer(); the referrer because they had to have
        # started the bot themselves to get an /invite link). Ensured here
        # defensively too, but via a plain INSERT-if-missing rather than
        # _ensure_player() — this is a system-triggered credit, not
        # something either user just did, so it must not bump their
        # last_active_at the way _ensure_player() would (that would pollute
        # the "active today" admin stat for the referrer in particular).
        for uid in (user_id, referrer_id):
            _conn.execute(
                "INSERT INTO players (user_id, username, quota_date, messages_used_today, "
                "bonus_credits, model_pref, last_active_at) VALUES (?, NULL, ?, 0, 0, 'fast', ?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (uid, _today(), now),
            )
        _conn.execute(
            "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
            (bonus_messages, user_id),
        )
        _conn.execute(
            "UPDATE players SET bonus_credits = bonus_credits + ? WHERE user_id = ?",
            (bonus_messages, referrer_id),
        )
        _conn.commit()
        cur = _conn.execute("SELECT bonus_credits FROM players WHERE user_id = ?", (referrer_id,))
        referrer_balance = cur.fetchone()[0]
        return {
            "referrer_id": referrer_id,
            "referee_id": user_id,
            "referrer_balance": referrer_balance,
        }


def get_reminder_status(user_id: int) -> dict:
    with _lock:
        cur = _conn.execute(
            "SELECT reminder_enabled, reminder_hour FROM players WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        if row is None:
            return {"enabled": False, "hour": None}
        enabled, hour = row
        return {"enabled": bool(enabled), "hour": hour}


def set_reminder(user_id: int, username: str | None, enabled: bool, hour: int | None) -> None:
    """User-driven only — called from the reminder menu, never on the bot's
    own initiative. Turning it off clears reminder_hour too, so re-enabling
    later always requires picking a time again rather than silently
    resuming an old one."""
    with _lock:
        _ensure_player(user_id, username)
        _conn.execute(
            "UPDATE players SET reminder_enabled = ?, reminder_hour = ? WHERE user_id = ?",
            (1 if enabled else 0, hour if enabled else None, user_id),
        )
        _conn.commit()


def get_users_due_for_reminder(hour: int, today: str) -> list[int]:
    """Users who opted in, picked this hour (in QUOTA_TZ), and haven't
    already gotten today's reminder — checked by the background loop."""
    with _lock:
        cur = _conn.execute(
            "SELECT user_id FROM players WHERE reminder_enabled = 1 AND reminder_hour = ? "
            "AND (reminder_last_sent_date IS NULL OR reminder_last_sent_date != ?)",
            (hour, today),
        )
        return [row[0] for row in cur.fetchall()]


def mark_reminder_sent(user_id: int, today: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE players SET reminder_last_sent_date = ? WHERE user_id = ?", (today, user_id)
        )
        _conn.commit()


def create_promo_code(
    code: str, title: str, bonus_minutes: int, revenue_share: int, window_days: int
) -> str | None:
    """Creates a promo code, active and owner-less, with a freshly generated
    claim token (see claim_promo_code — the code itself is public, in the
    shareable link, so the token is what actually gates ownership). Returns
    the token, or None if the code already exists, so the caller can show a
    friendly "code taken" message rather than a traceback."""
    with _lock:
        token = _generate_claim_token()
        try:
            _conn.execute(
                "INSERT INTO promo_codes (code, title, bonus_minutes, revenue_share, "
                "window_days, created_at, active, claim_token) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (code, title, bonus_minutes, revenue_share, window_days, _now(), token),
            )
        except sqlite3.IntegrityError:
            _conn.rollback()
            return None
        _conn.commit()
        return token


def _promo_code_row_to_dict(row: tuple | None) -> dict | None:
    if row is None:
        return None
    code, owner_user_id, title, bonus_minutes, revenue_share, window_days, created_at, active = row
    return {
        "code": code,
        "owner_user_id": owner_user_id,
        "title": title,
        "bonus_minutes": bonus_minutes,
        "revenue_share": revenue_share,
        "window_days": window_days,
        "created_at": created_at,
        "active": bool(active),
    }


def get_promo_code(code: str) -> dict | None:
    with _lock:
        cur = _conn.execute(
            "SELECT code, owner_user_id, title, bonus_minutes, revenue_share, window_days, "
            "created_at, active FROM promo_codes WHERE code = ?",
            (code,),
        )
        return _promo_code_row_to_dict(cur.fetchone())


def get_promo_code_by_owner(user_id: int) -> dict | None:
    with _lock:
        cur = _conn.execute(
            "SELECT code, owner_user_id, title, bonus_minutes, revenue_share, window_days, "
            "created_at, active FROM promo_codes WHERE owner_user_id = ?",
            (user_id,),
        )
        return _promo_code_row_to_dict(cur.fetchone())


def claim_promo_code(code: str, token: str, user_id: int) -> str:
    """Partner-facing claim via /promo <code> <word> — requires the secret
    claim_token, not just the (public, link-visible) code. Returns:
      "invalid" — code doesn't exist OR the token is wrong. Deliberately
                  the same result for both, so the response can't be used
                  to probe which codes exist.
      "already_owned" — code + token are right, but it's owned by someone
                  else already (not reassigned even with the correct word —
                  use admin_set_promo_owner to force that).
      "ok" — claimed (a fresh claim, or the same owner re-running /promo).
    """
    with _lock:
        cur = _conn.execute(
            "SELECT owner_user_id, claim_token FROM promo_codes WHERE code = ?", (code,)
        )
        row = cur.fetchone()
        if row is None:
            return "invalid"
        current_owner, real_token = row
        if not real_token or not secrets.compare_digest(token, real_token):
            return "invalid"
        if current_owner is not None and current_owner != user_id:
            return "already_owned"
        _conn.execute(
            "UPDATE promo_codes SET owner_user_id = ? WHERE code = ?", (user_id, code)
        )
        _conn.commit()
        return "ok"


def admin_set_promo_owner(code: str, user_id: int | None) -> str:
    """Admin-only forced (re)assignment via /promo_owner — bypasses the
    claim token entirely and, unlike claim_promo_code, can also clear an
    existing owner (user_id=None) or hand the code to someone else outright.
    For when the real owner lost access to their account, or a code was
    claimed by the wrong person before this whole token system existed.
    Returns "not_found" or "ok"."""
    with _lock:
        cur = _conn.execute("SELECT code FROM promo_codes WHERE code = ?", (code,))
        if cur.fetchone() is None:
            return "not_found"
        _conn.execute(
            "UPDATE promo_codes SET owner_user_id = ? WHERE code = ?", (user_id, code)
        )
        _conn.commit()
        return "ok"


def get_promo_claim_token(code: str) -> str | None:
    """For /promo_token <code> — None if the code doesn't exist."""
    with _lock:
        cur = _conn.execute("SELECT claim_token FROM promo_codes WHERE code = ?", (code,))
        row = cur.fetchone()
        return row[0] if row else None


def regenerate_promo_claim_token(code: str) -> str | None:
    """Invalidates the old claim word and issues a fresh one — for
    /promo_token <code> new, when a word has leaked. Returns the new word,
    or None if the code doesn't exist."""
    with _lock:
        cur = _conn.execute("SELECT code FROM promo_codes WHERE code = ?", (code,))
        if cur.fetchone() is None:
            return None
        token = _generate_claim_token()
        _conn.execute("UPDATE promo_codes SET claim_token = ? WHERE code = ?", (token, code))
        _conn.commit()
        return token


def disable_promo_code(code: str) -> str:
    """Sets active = 0. Deliberately doesn't touch promo_visits or existing
    payments' promo_code — users already attributed to this code keep their
    attribution window, only new visits stop getting the signup bonus.
    Returns "not_found", "already_off", or "ok"."""
    with _lock:
        cur = _conn.execute("SELECT active FROM promo_codes WHERE code = ?", (code,))
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if not row[0]:
            return "already_off"
        _conn.execute("UPDATE promo_codes SET active = 0 WHERE code = ?", (code,))
        _conn.commit()
        return "ok"


def get_promo_visit(user_id: int) -> dict | None:
    with _lock:
        cur = _conn.execute(
            "SELECT code, joined_at FROM promo_visits WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"code": row[0], "joined_at": row[1]}


def record_promo_visit(user_id: int, code: str) -> bool:
    """Attributes a user to a promo code, but only the first time ever —
    one user is permanently tied to whichever code (if any) they visited
    first, mirroring set_referrer's "first wins" rule for the referral
    program. Returns True iff this call is the one that set it."""
    with _lock:
        cur = _conn.execute(
            "INSERT INTO promo_visits (user_id, code, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, code, _now()),
        )
        _conn.commit()
        return cur.rowcount > 0


def _resolve_promo_for_payment(user_id: int) -> str | None:
    """Lock-free helper — only ever called from inside record_payment_and_credit,
    which already holds _lock. Attributes a payment to this user's promo code
    iff they're within that code's window_days of their own joined_at (the
    window is per-user, anchored to when THEY joined, not to when the code
    was created or when it might later be disabled)."""
    cur = _conn.execute("SELECT code, joined_at FROM promo_visits WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    code, joined_at = row
    cur = _conn.execute("SELECT window_days FROM promo_codes WHERE code = ?", (code,))
    code_row = cur.fetchone()
    if code_row is None:
        return None
    (window_days,) = code_row
    try:
        joined_dt = datetime.datetime.fromisoformat(joined_at)
    except ValueError:
        return None
    deadline = joined_dt + datetime.timedelta(days=window_days)
    return code if _utcnow_naive() <= deadline else None


def _promo_stats(code: str) -> dict:
    """Lock-free helper — only ever called from inside a function that
    already holds _lock. Aggregates everything /mypromo and /promo_stat
    show: visit counts, how many visitors have actually used the bot at
    least once (a chat_log row — /start alone doesn't write one, so this
    excludes people who only clicked the link and never sent a message),
    payment count/revenue attributed to the code, the partner's share of
    that revenue (floored), and how many visitors are still inside their
    own attribution window right now."""
    cur = _conn.execute("SELECT COUNT(*) FROM promo_visits WHERE code = ?", (code,))
    (total_visits,) = cur.fetchone()

    week_ago = (_utcnow_naive() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
    cur = _conn.execute(
        "SELECT COUNT(*) FROM promo_visits WHERE code = ? AND joined_at >= ?", (code, week_ago)
    )
    (visits_7d,) = cur.fetchone()

    cur = _conn.execute(
        "SELECT COUNT(DISTINCT pv.user_id) FROM promo_visits pv "
        "WHERE pv.code = ? AND EXISTS ("
        "  SELECT 1 FROM chat_log cl WHERE cl.user_id = pv.user_id AND cl.role = 'user'"
        ")",
        (code,),
    )
    (active_users,) = cur.fetchone()

    cur = _conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_stars), 0) FROM payments "
        "WHERE promo_code = ? AND status = 'paid'",
        (code,),
    )
    payments_count, revenue_stars = cur.fetchone()

    cur = _conn.execute("SELECT window_days FROM promo_codes WHERE code = ?", (code,))
    (window_days,) = cur.fetchone()
    cur = _conn.execute("SELECT joined_at FROM promo_visits WHERE code = ?", (code,))
    now = _utcnow_naive()
    in_window = 0
    for (joined_at,) in cur.fetchall():
        try:
            joined_dt = datetime.datetime.fromisoformat(joined_at)
        except ValueError:
            continue
        if now <= joined_dt + datetime.timedelta(days=window_days):
            in_window += 1

    cur = _conn.execute(
        "SELECT revenue_share FROM promo_codes WHERE code = ?", (code,)
    )
    (revenue_share,) = cur.fetchone()

    return {
        "total_visits": total_visits,
        "visits_7d": visits_7d,
        "active_users": active_users,
        "payments_count": payments_count,
        "revenue_stars": revenue_stars,
        "partner_share_stars": revenue_stars * revenue_share // 100,
        "in_window": in_window,
    }


def get_promo_stats(code: str) -> dict | None:
    """Public entry point: promo code details merged with _promo_stats()'s
    aggregates, or None if the code doesn't exist."""
    with _lock:
        cur = _conn.execute(
            "SELECT code, owner_user_id, title, bonus_minutes, revenue_share, window_days, "
            "created_at, active FROM promo_codes WHERE code = ?",
            (code,),
        )
        promo = _promo_code_row_to_dict(cur.fetchone())
        if promo is None:
            return None
        return {**promo, **_promo_stats(code)}


def list_promo_codes() -> list[dict]:
    """All promo codes (active and disabled) with their stats, newest first."""
    with _lock:
        cur = _conn.execute(
            "SELECT code, owner_user_id, title, bonus_minutes, revenue_share, window_days, "
            "created_at, active FROM promo_codes ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
        return [{**_promo_code_row_to_dict(row), **_promo_stats(row[0])} for row in rows]


def close() -> None:
    """Flushes and closes the SQLite connection — call on graceful shutdown
    so WAL checkpoint data isn't left stranded and the file lock is
    released cleanly instead of relying on process teardown."""
    with _lock:
        _conn.commit()
        _conn.close()
