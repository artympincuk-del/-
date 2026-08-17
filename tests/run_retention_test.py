import asyncio
import datetime
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, REPO_ROOT)

os.makedirs(os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests"), exist_ok=True)
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "klyaksa_bot_tests", "retention_test.db")
os.environ["BOT_TOKEN"] = "dummy"
os.environ["GROQ_API_KEY"] = "dummy"
os.environ["ADMIN_IDS"] = "9999"
for ext in ("", "-wal", "-shm"):
    p = os.environ["DB_PATH"] + ext
    if os.path.exists(p):
        os.remove(p)

from bot import db  # noqa: E402
from bot import handlers  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def days_ago_ts(n):
    return (
        datetime.datetime.now(db._QUOTA_TZINFO).replace(hour=12, minute=0, second=0, microsecond=0)
        - datetime.timedelta(days=n)
    ).astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


async def run():
    # ------------------------------------------------------------------
    # first_seen_at is set once at creation and never touched again.
    # ------------------------------------------------------------------
    u = 60001
    db._ensure_player(u, "u")
    first_seen_1 = db._conn.execute("SELECT first_seen_at FROM players WHERE user_id = ?", (u,)).fetchone()[0]
    check("first_seen_at: set on first _ensure_player call", first_seen_1 is not None)
    db._ensure_player(u, "u")  # a later call (simulates a later message)
    first_seen_2 = db._conn.execute("SELECT first_seen_at FROM players WHERE user_id = ?", (u,)).fetchone()[0]
    check("first_seen_at: unchanged by a later _ensure_player call", first_seen_1 == first_seen_2)

    # ------------------------------------------------------------------
    # The exact spec scenario: three users first_seen yesterday, one of
    # them active again today -> 1 of 3, 33%.
    # ------------------------------------------------------------------
    y1, y2, y3 = 60002, 60003, 60004
    for uid in (y1, y2, y3):
        db._ensure_player(uid, f"y{uid}")
    with db._lock:
        for uid in (y1, y2, y3):
            db._conn.execute(
                "UPDATE players SET first_seen_at = ?, last_active_at = ? WHERE user_id = ?",
                (days_ago_ts(1), days_ago_ts(1), uid),
            )
        # y1 came back today.
        db._conn.execute(
            "UPDATE players SET last_active_at = ? WHERE user_id = ?", (db._now(), y1)
        )
        db._conn.commit()

    stats = db.get_retention_stats()
    check("retention: exactly 3 users first-seen yesterday", stats["new_yesterday"] == 3)
    check("retention: exactly 1 of them active again today", stats["returned_today"] == 1)
    check("retention: percentage rounds to 33%", stats["returned_today_pct"] == 33)

    # ------------------------------------------------------------------
    # A user first-seen TODAY doesn't count as "yesterday"'s cohort.
    # ------------------------------------------------------------------
    t1 = 60005
    db._ensure_player(t1, "today_user")
    stats2 = db.get_retention_stats()
    check("retention: a brand-new today user doesn't inflate 'new_yesterday'", stats2["new_yesterday"] == 3)

    # ------------------------------------------------------------------
    # A user first-seen 10 days ago is outside the 7-day window.
    # ------------------------------------------------------------------
    old = 60006
    db._ensure_player(old, "old_user")
    with db._lock:
        db._conn.execute(
            "UPDATE players SET first_seen_at = ?, last_active_at = ? WHERE user_id = ?",
            (days_ago_ts(10), days_ago_ts(10), old),
        )
        db._conn.commit()
    stats3 = db.get_retention_stats()
    check(
        "retention: new_7d excludes a user first-seen 10 days ago",
        # u (60001, today) + y1/y2/y3 (yesterday) + t1 (today) = 5, old_user excluded
        stats3["new_7d"] == 5,
    )

    # A user first-seen 5 days ago, active 1 day ago -> counts in both
    # new_7d and the "active within 3 days" bucket.
    recent = 60007
    db._ensure_player(recent, "recent_user")
    with db._lock:
        db._conn.execute(
            "UPDATE players SET first_seen_at = ?, last_active_at = ? WHERE user_id = ?",
            (days_ago_ts(5), days_ago_ts(1), recent),
        )
        db._conn.commit()
    stats4 = db.get_retention_stats()
    check("retention: new_7d includes the 5-days-ago user", stats4["new_7d"] == 6)
    check(
        "retention: that user (active 1 day ago) counts as active within 3 days",
        stats4["new_7d_active_3d"] >= 1,
    )

    # ------------------------------------------------------------------
    # Zero new-yesterday users -> 0%, not a division-by-zero crash.
    # ------------------------------------------------------------------
    with db._lock:
        db._conn.execute("DELETE FROM players")
        db._conn.commit()
    stats_empty = db.get_retention_stats()
    check("retention: empty DB gives 0 new_yesterday", stats_empty["new_yesterday"] == 0)
    check("retention: empty DB gives 0% (no crash)", stats_empty["returned_today_pct"] == 0)

    # ------------------------------------------------------------------
    # Admin stats text includes the retention block.
    # ------------------------------------------------------------------
    db._ensure_player(70001, "someone")
    stats_text = handlers._admin_stats_text()
    check("admin stats: mentions 'Пришли вчера'", "Пришли вчера" in stats_text)
    check("admin stats: mentions 'вернулись сегодня'", "вернулись сегодня" in stats_text)
    check("admin stats: mentions 'Пришли за 7 дней'", "Пришли за 7 дней" in stats_text)


asyncio.run(run())

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f" - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
