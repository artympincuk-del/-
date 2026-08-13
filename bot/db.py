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


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.date.today().isoformat()


def _ensure_player(user_id: int, username: str | None) -> None:
    _conn.execute(
        """
        INSERT INTO players (user_id, username, quota_date, messages_used_today, bonus_credits, model_pref, last_active_at)
        VALUES (?, ?, ?, 0, 0, 'fast', ?)
        ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, last_active_at = excluded.last_active_at
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


def get_status(user_id: int, username: str | None) -> dict:
    with _lock:
        _ensure_player(user_id, username)
        _reset_if_new_day(user_id)
        cur = _conn.execute(
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, model_pref "
            "FROM players WHERE user_id = ?",
            (user_id,),
        )
        used, premium_used, bonus, model_pref = cur.fetchone()
        return {
            "used_today": used,
            "premium_used_today": premium_used,
            "bonus_credits": bonus,
            "model_pref": model_pref,
        }


def set_model_pref(user_id: int, username: str | None, model_pref: str) -> None:
    with _lock:
        _ensure_player(user_id, username)
        _conn.execute(
            "UPDATE players SET model_pref = ? WHERE user_id = ?", (model_pref, user_id)
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
            "SELECT messages_used_today, premium_messages_used_today, bonus_credits, model_pref "
            "FROM players WHERE user_id = ?",
            (user_id,),
        )
        used, premium_used, bonus, model_pref = cur.fetchone()

        if model_pref == "premium":
            if premium_used < daily_premium_limit:
                _conn.execute(
                    "UPDATE players SET premium_messages_used_today = premium_messages_used_today + 1 "
                    "WHERE user_id = ?",
                    (user_id,),
                )
                _conn.commit()
                return True, {
                    "used_today": used,
                    "premium_used_today": premium_used + 1,
                    "bonus_credits": bonus,
                    "model_pref": model_pref,
                }

            if bonus < premium_cost:
                return False, {
                    "used_today": used,
                    "premium_used_today": premium_used,
                    "bonus_credits": bonus,
                    "model_pref": model_pref,
                }
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - ? WHERE user_id = ?",
                (premium_cost, user_id),
            )
            _conn.commit()
            return True, {
                "used_today": used,
                "premium_used_today": premium_used,
                "bonus_credits": bonus - premium_cost,
                "model_pref": model_pref,
            }

        if used < daily_limit:
            _conn.execute(
                "UPDATE players SET messages_used_today = messages_used_today + 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, {
                "used_today": used + 1,
                "premium_used_today": premium_used,
                "bonus_credits": bonus,
                "model_pref": model_pref,
            }

        if bonus > 0:
            _conn.execute(
                "UPDATE players SET bonus_credits = bonus_credits - 1 WHERE user_id = ?",
                (user_id,),
            )
            _conn.commit()
            return True, {
                "used_today": used,
                "premium_used_today": premium_used,
                "bonus_credits": bonus - 1,
                "model_pref": model_pref,
            }

        return False, {
            "used_today": used,
            "premium_used_today": premium_used,
            "bonus_credits": bonus,
            "model_pref": model_pref,
        }


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


def list_users(limit: int = 30) -> list[tuple]:
    with _lock:
        cur = _conn.execute(
            "SELECT user_id, username, messages_used_today, premium_messages_used_today, "
            "bonus_credits, model_pref, last_active_at "
            "FROM players ORDER BY last_active_at DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()


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
