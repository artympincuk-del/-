import datetime
import sqlite3
import threading

from bot.config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
_conn.commit()

MAX_NOTES_PER_USER = 30


def _ensure_column(table: str, column: str, coldef: str) -> None:
    cur = _conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        _conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
        _conn.commit()


_ensure_column("players", "premium_messages_used_today", "INTEGER NOT NULL DEFAULT 0")
_ensure_column("players", "last_active_at", "TEXT")
_ensure_column("players", "unlimited_until", "TEXT")
_ensure_column("players", "referred_by", "INTEGER")
# Which specific engine to use within the chosen tier (model_pref stays
# 'fast'/'premium' purely for quota billing — model_choice picks the actual
# Groq model, e.g. 'gptoss' vs 'llama', independent of that billing tier).
_ensure_column("players", "model_choice", "TEXT NOT NULL DEFAULT 'gptoss'")


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


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
    return raw if expiry > datetime.datetime.utcnow() else None


def get_status(user_id: int, username: str | None) -> dict:
    with _lock:
        _ensure_player(user_id, username)
        _reset_if_new_day(user_id)
        cur = _conn.execute(
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, "
            "model_pref, unlimited_until, model_choice FROM players WHERE user_id = ?",
            (user_id,),
        )
        used, premium_used, bonus, model_pref, unlimited_until, model_choice = cur.fetchone()
        return {
            "used_today": used,
            "premium_used_today": premium_used,
            "bonus_credits": bonus,
            "model_pref": model_pref,
            "unlimited_until": _active_unlimited_until(unlimited_until),
            "model_choice": model_choice,
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
) -> tuple[bool, dict]:
    """Atomically consumes quota for one message, based on the player's model_pref.

    'fast' model: free daily quota first, then 1 bonus credit.
    'premium' model: free daily premium quota first, then `premium_cost` bonus credits.

    Returns (allowed, status) where status mirrors get_status's shape after the attempt.
    """
    with _lock:
        _ensure_player(user_id, username)
        _reset_if_new_day(user_id)
        cur = _conn.execute(
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, "
            "model_pref, unlimited_until, model_choice FROM players WHERE user_id = ?",
            (user_id,),
        )
        used, premium_used, bonus, model_pref, unlimited_raw, model_choice = cur.fetchone()
        unlimited_until = _active_unlimited_until(unlimited_raw)

        def status(used=used, premium_used=premium_used, bonus=bonus) -> dict:
            return {
                "used_today": used,
                "premium_used_today": premium_used,
                "bonus_credits": bonus,
                "model_pref": model_pref,
                "unlimited_until": unlimited_until,
                "model_choice": model_choice,
            }

        if unlimited_until:
            # Active time pass: no quota/credits touched at all.
            return True, status()

        if model_pref == "premium":
            if premium_used < daily_premium_limit:
                _conn.execute(
                    "UPDATE players SET premium_messages_used_today = premium_messages_used_today + 1 "
                    "WHERE user_id = ?",
                    (user_id,),
                )
                _conn.commit()
                return True, status(premium_used=premium_used + 1)

            if bonus < premium_cost:
                return False, status()
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - ? WHERE user_id = ?",
                (premium_cost, user_id),
            )
            _conn.commit()
            return True, status(bonus=bonus - premium_cost)

        if used < daily_limit:
            _conn.execute(
                "UPDATE players SET messages_used_today = messages_used_today + 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, status(used=used + 1)

        if bonus > 0:
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, status(bonus=bonus - 1)

        return False, status()


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
        now = datetime.datetime.utcnow()
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


def log_payment(user_id: int, username: str | None, kind: str, amount_stars: int) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO payments (user_id, username, kind, amount_stars, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, kind, amount_stars, _now()),
        )
        _conn.commit()


def get_admin_stats() -> dict:
    with _lock:
        today = _today()
        week_ago = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()

        cur = _conn.execute("SELECT COUNT(*) FROM players")
        (total_users,) = cur.fetchone()

        cur = _conn.execute(
            "SELECT COUNT(*) FROM players WHERE last_active_at >= ?", (today,)
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

        revenue_today, payments_today = revenue_since(today)
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


def set_referrer(user_id: int, referrer_id: int) -> bool:
    """Records who referred this user, but only the first time and never for
    self-referrals. Returns True iff this call actually set it (i.e. the
    referral bonus should be paid out); False means already set/invalid."""
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
