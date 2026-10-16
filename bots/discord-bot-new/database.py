import hashlib
import sqlite3
import threading

DB_FILE = "wardogs_v2.db"
_local = threading.local()
_lock = threading.Lock()
_conns: set[sqlite3.Connection] = set()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _time_ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_FILE, check_same_thread=False, isolation_level=None)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        with _lock:
            _conns.add(_local.conn)
    return _local.conn


def close_all_connections():
    """Закрывает все открытые соединения к БД (нужно перед заменой файла БД)."""
    global _conns
    with _lock:
        for conn in list(_conns):
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        _conns = set()
    _local.conn = None


def db_file_exists() -> bool:
    import os
    return os.path.isfile(DB_FILE) or os.path.isfile(DB_FILE + "-wal") or os.path.isfile(DB_FILE + "-shm")


def _ensure_column(conn, table: str, column: str, ddl: str):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS member_stats (
            user_id INTEGER PRIMARY KEY,
            messages INTEGER DEFAULT 0,
            voice_seconds INTEGER DEFAULT 0,
            voice_joins INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_xp (
            user_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            last_message_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_xp_guild ON user_xp(guild_id);

        CREATE TABLE IF NOT EXISTS tickets (
            number INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER,
            ticket_type TEXT DEFAULT 'complaint',
            status TEXT DEFAULT 'open',
            created_at TEXT,
            closed_at TEXT,
            closed_by INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
        CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);

        CREATE TABLE IF NOT EXISTS streamers (
            login TEXT PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            is_live INTEGER DEFAULT 0,
            live_since TEXT
        );

        CREATE TABLE IF NOT EXISTS mirrors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_guild_id INTEGER NOT NULL,
            source_channel_id INTEGER NOT NULL,
            dest_channels TEXT NOT NULL,
            title TEXT DEFAULT '',
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_guild_id, source_channel_id)
        );

        CREATE TABLE IF NOT EXISTS activity_roles_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            granted INTEGER DEFAULT 1,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS backgrounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            added_by INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            cx INTEGER DEFAULT 50,
            cy INTEGER DEFAULT 60,
            radius INTEGER DEFAULT 75
        );

        CREATE TABLE IF NOT EXISTS log_channels (
            log_type TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS log_sent (
            fingerprint TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_lease (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            run_id TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            reason TEXT DEFAULT 'Не указана',
            moderator_id INTEGER NOT NULL,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(user_id, guild_id);

        CREATE TABLE IF NOT EXISTS automod_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS automod_badwords (
            word TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS automod_whitelist (
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            PRIMARY KEY (target_type, target_id)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            winners INTEGER DEFAULT 1,
            created_by INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS giveaway_participants (
            giveaway_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT,
            PRIMARY KEY (giveaway_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS server_members (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT,
            joined_at TEXT,
            roles TEXT DEFAULT '',
            is_bot INTEGER DEFAULT 0,
            last_seen TEXT,
            PRIMARY KEY (user_id, guild_id)
        );
        CREATE INDEX IF NOT EXISTS idx_server_members_guild ON server_members(guild_id);

        CREATE TABLE IF NOT EXISTS server_roles (
            role_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT,
            color INTEGER,
            position INTEGER DEFAULT 0,
            permissions INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_server_roles_guild ON server_roles(guild_id);

        CREATE TABLE IF NOT EXISTS server_channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT,
            type TEXT DEFAULT 'text',
            category_id INTEGER,
            position INTEGER DEFAULT 0,
            topic TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_server_channels_guild ON server_channels(guild_id);

        CREATE TABLE IF NOT EXISTS server_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            target_id INTEGER,
            actor_id INTEGER,
            details TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_server_events_guild ON server_events(guild_id, created_at);

        CREATE TABLE IF NOT EXISTS announce_pending (
            user_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trap_channels (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            created_at TEXT
        );
    """)
    conn.commit()


# Streamers
def get_streamers():
    conn = get_conn()
    return [r["login"] for r in conn.execute("SELECT login FROM streamers ORDER BY login").fetchall()]


def add_streamer(login: str, added_by: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO streamers (login, added_by, added_at) VALUES (?, ?, ?)",
        (login, added_by, _utcnow()),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_streamer(login: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM streamers WHERE login = ?", (login,))
    conn.commit()
    return cur.rowcount > 0


def set_streamer_live(login: str, live: bool):
    conn = get_conn()
    if live:
        conn.execute(
            "UPDATE streamers SET is_live = 1, live_since = COALESCE(live_since, ?) WHERE login = ?",
            (_utcnow(), login),
        )
    else:
        conn.execute("UPDATE streamers SET is_live = 0, live_since = NULL WHERE login = ?", (login,))
    conn.commit()


def streamer_live_state(login: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT is_live FROM streamers WHERE login = ?", (login,)).fetchone()
    return bool(row and row["is_live"])


def get_live_streamers() -> list[str]:
    conn = get_conn()
    return [r["login"] for r in conn.execute("SELECT login FROM streamers WHERE is_live = 1").fetchall()]


# Mirrors
def get_mirrors():
    conn = get_conn()
    return conn.execute("SELECT * FROM mirrors ORDER BY id").fetchall()


def add_mirror(source_guild_id: int, source_channel_id: int, dest_channels: list[int], title: str, created_by: int) -> bool:
    conn = get_conn()
    dest_str = ",".join(str(x) for x in dest_channels)
    cur = conn.execute(
        "INSERT OR IGNORE INTO mirrors (source_guild_id, source_channel_id, dest_channels, title, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_guild_id, source_channel_id, dest_str, title, created_by, _utcnow()),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_mirror(mirror_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM mirrors WHERE id = ?", (mirror_id,))
    conn.commit()
    return cur.rowcount > 0


def get_mirror_dests(source_guild_id: int, source_channel_id: int) -> list[int]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT dest_channels FROM mirrors WHERE source_guild_id = ? AND source_channel_id = ?",
        (source_guild_id, source_channel_id),
    ).fetchall()
    out = []
    for r in rows:
        for x in (r["dest_channels"] or "").split(","):
            if x.strip().isdigit():
                out.append(int(x))
    return out


# --- Backgrounds (фоны rank-карточки из БД, удобно на хостинге) ---

def add_background(path: str, added_by: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO backgrounds (path, added_by, added_at) VALUES (?, ?, ?)",
        (path, added_by, _utcnow()),
    )
    conn.commit()
    return cur.rowcount > 0


def get_backgrounds() -> list[str]:
    conn = get_conn()
    return [r["path"] for r in conn.execute("SELECT path FROM backgrounds ORDER BY id").fetchall()]


def get_random_bg() -> dict | None:
    """Случайный фон с позицией аватара. Возвращает {path, cx, cy, radius} или None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT path, cx, cy, radius FROM backgrounds ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def set_bg_pos(rid: int, cx: int, cy: int, radius: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE backgrounds SET cx=?, cy=?, radius=? WHERE id=?",
        (cx, cy, radius, rid),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_background(rid: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM backgrounds WHERE id = ?", (rid,))
    conn.commit()
    return cur.rowcount > 0


def clear_backgrounds() -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM backgrounds")
    conn.commit()
    return cur.rowcount


def xp_needed_for_level(level: int) -> int:
    """Суммарный XP, необходимый для достижения уровня level."""
    total = 0
    for l in range(1, level):
        total += 100 + 50 * (l - 1)
    return total


def level_from_xp(xp: int) -> int:
    """Текущий уровень по суммарному XP."""
    level = 1
    while xp >= xp_needed_for_level(level + 1):
        level += 1
    return level


def xp_progress(xp: int, level: int) -> tuple[int, int]:
    """(Прогресс уровня, нужно для следующего уровня)."""
    current = xp_needed_for_level(level)
    nxt = xp_needed_for_level(level + 1)
    return xp - current, nxt - current


def ensure_user_xp(user_id: int, guild_id: int):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO user_xp (user_id, guild_id) VALUES (?, ?)",
        (user_id, guild_id),
    )
    conn.commit()


def add_xp(user_id: int, guild_id: int, amount: int) -> tuple[int, int] | None:
    """Добавить XP. Возвращает (было_уровней, стало_уровней) или None при ошибке."""
    conn = get_conn()
    ensure_user_xp(user_id, guild_id)
    row = conn.execute(
        "SELECT xp, level FROM user_xp WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    old_level = row["level"]
    new_xp = row["xp"] + amount
    new_level = level_from_xp(new_xp)
    conn.execute(
        "UPDATE user_xp SET xp = ?, level = ? WHERE user_id = ? AND guild_id = ?",
        (new_xp, new_level, user_id, guild_id),
    )
    conn.commit()
    return old_level, new_level


def get_user_xp(user_id: int, guild_id: int) -> tuple[int, int] | None:
    """(xp, level) или None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT xp, level FROM user_xp WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    if not row:
        return None
    return row["xp"], row["level"]


def set_last_message(user_id: int, guild_id: int):
    conn = get_conn()
    ensure_user_xp(user_id, guild_id)
    conn.execute(
        "UPDATE user_xp SET last_message_at = ? WHERE user_id = ? AND guild_id = ?",
        (_utcnow(), user_id, guild_id),
    )
    conn.commit()


def can_get_message_xp(user_id: int, guild_id: int, cooldown_seconds: int = 60) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT last_message_at FROM user_xp WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    if not row or not row["last_message_at"]:
        return True
    try:
        from datetime import datetime, timezone
        last = datetime.fromisoformat(row["last_message_at"])
        now = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() >= cooldown_seconds
    except Exception:
        return True


def get_leaderboard(guild_id: int, limit: int = 15) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, xp, level FROM user_xp WHERE guild_id = ? "
        "ORDER BY xp DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_xp_rank(user_id: int, guild_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM user_xp WHERE guild_id = ? AND xp > "
        "(SELECT COALESCE(xp,0) FROM user_xp WHERE user_id = ? AND guild_id = ?)",
        (guild_id, user_id, guild_id),
    ).fetchone()
    return (row["c"] if row else 0) + 1


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек."
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours == 0:
        return f"{minutes} мин."
    return f"{hours} ч. {minutes} мин."


# --- Log channels ---

def set_log_channel(log_type: str, channel_id: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO log_channels (log_type, channel_id) VALUES (?, ?) "
        "ON CONFLICT(log_type) DO UPDATE SET channel_id = excluded.channel_id",
        (log_type, channel_id),
    )
    conn.commit()


def get_log_channel(log_type: str) -> int | None:
    conn = get_conn()
    row = conn.execute("SELECT channel_id FROM log_channels WHERE log_type = ?", (log_type,)).fetchone()
    return row["channel_id"] if row else None


def get_all_log_channels() -> dict[str, int]:
    conn = get_conn()
    return {r["log_type"]: r["channel_id"] for r in conn.execute("SELECT * FROM log_channels").fetchall()}


def remove_log_channel(log_type: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM log_channels WHERE log_type = ?", (log_type,))
    conn.commit()
    return cur.rowcount > 0


# --- Anti-duplicate for log embeds ---

# In-memory кеш заявок (первый барьер). Даже при одном процессе защищает от
# сдвоенных слушателей/когов; при нескольких процессах дубли отсекает БД.
_claim_memory: dict[str, float] = {}

import time as _time


def make_log_fingerprint(log_type: str, embed) -> str:
    """Стабильный отпечаток эмбеда лога без учёта времени/цвета.
    Используется, чтобы один и тот же лог не уходил дважды (два инстанса
    бота, двойная обработка события и т.п.)."""
    parts = [log_type, embed.title or ""]
    if embed.description:
        parts.append(embed.description)
    for f in embed.fields:
        parts.append(f"{f.name}\x00{f.value}")
    raw = "\x01".join(parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def try_claim_log(fingerprint: str, ttl_seconds: int = 6) -> bool:
    """Пытается зарегистрировать лог как отправленный.
    Возвращает True, если это первая отправка за окно ttl_seconds,
    иначе False (дубль — отправлять не нужно)."""
    now = _time.monotonic()
    prev = _claim_memory.get(fingerprint)
    if prev is not None and now - prev < ttl_seconds:
        return False
    # Умеряем рост кеша — чистим записи старше TTL*10.
    if len(_claim_memory) > 2048:
        for k in [k for k, v in _claim_memory.items() if now - v > ttl_seconds * 10]:
            _claim_memory.pop(k, None)
    _claim_memory[fingerprint] = now

    conn = get_conn()
    cutoff = _time_ago(ttl_seconds)
    conn.execute("DELETE FROM log_sent WHERE sent_at < ?", (cutoff,))
    try:
        conn.execute(
            "INSERT INTO log_sent (fingerprint, sent_at) VALUES (?, ?)",
            (fingerprint, _utcnow()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# --- Warnings ---

def add_warning(user_id: int, guild_id: int, reason: str, moderator_id: int) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO warnings (user_id, guild_id, reason, moderator_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, guild_id, reason, moderator_id, _utcnow()),
    )
    conn.commit()
    return cur.lastrowid


def get_warnings(user_id: int, guild_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, reason, moderator_id, created_at FROM warnings WHERE user_id = ? AND guild_id = ? ORDER BY id",
        (user_id, guild_id),
    ).fetchall()
    return [dict(r) for r in rows]


def get_warning_count(user_id: int, guild_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM warnings WHERE user_id = ? AND guild_id = ?",
        (user_id, guild_id),
    ).fetchone()
    return row["c"] if row else 0


def clear_warnings(user_id: int, guild_id: int) -> int:
    conn = get_conn()
    cur = conn.execute("DELETE FROM warnings WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    conn.commit()
    return cur.rowcount


# --- Automod config ---

_AUTOMOD_DEFAULTS = {
    "spam_msgs": "5",
    "spam_secs": "5",
    "spam_mute_mins": "5",
    "links_allowed": "0",
    "caps_percent": "70",
    "caps_min_len": "10",
    "warns_to_mute": "3",
    "warn_mute_mins": "10",
}


def get_automod_config(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM automod_config WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return _AUTOMOD_DEFAULTS.get(key, "0")


def set_automod_config(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO automod_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_all_automod_config() -> dict[str, str]:
    conn = get_conn()
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM automod_config").fetchall()}
    for k, v in _AUTOMOD_DEFAULTS.items():
        rows.setdefault(k, v)
    return rows


# --- Automod badwords ---

def add_badword(word: str) -> bool:
    conn = get_conn()
    cur = conn.execute("INSERT OR IGNORE INTO automod_badwords (word) VALUES (?)", (word.lower().strip(),))
    conn.commit()
    return cur.rowcount > 0


def remove_badword(word: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM automod_badwords WHERE word = ?", (word.lower().strip(),))
    conn.commit()
    return cur.rowcount > 0


def get_badwords() -> list[str]:
    conn = get_conn()
    return [r["word"] for r in conn.execute("SELECT word FROM automod_badwords ORDER BY word").fetchall()]


# --- Automod whitelist ---

def add_whitelist(target_type: str, target_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO automod_whitelist (target_type, target_id) VALUES (?, ?)",
        (target_type, target_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_whitelist(target_type: str, target_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM automod_whitelist WHERE target_type = ? AND target_id = ?",
        (target_type, target_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_whitelist() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM automod_whitelist").fetchall()]


def is_whitelisted(user_id: int, role_ids: list[int]) -> bool:
    conn = get_conn()
    rows = conn.execute("SELECT target_type, target_id FROM automod_whitelist").fetchall()
    for r in rows:
        if r["target_type"] == "user" and r["target_id"] == user_id:
            return True
        if r["target_type"] == "role" and r["target_id"] in role_ids:
            return True
    return False


# --- Single-instance lease ---

LEASE_TTL_SECONDS = 30


def _lease_expired() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=LEASE_TTL_SECONDS)).isoformat()


def acquire_lease(run_id: str) -> bool:
    """Пытается занять единственную лицензию на бота.
    Возвращает True, если лицензия получена (либо удержана прежним живым
    инстансом, который сам решает, выйти ли). False — если лицензию держит
    другой процесс с иным run_id."""
    conn = get_conn()
    cutoff = _lease_expired()
    row = conn.execute(
        "SELECT run_id, expires_at FROM bot_lease WHERE id = 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO bot_lease (id, run_id, expires_at) VALUES (1, ?, ?)",
            (run_id, _utcnow()),
        )
        conn.commit()
        check = conn.execute("SELECT run_id FROM bot_lease WHERE id = 1").fetchone()
        return check and check["run_id"] == run_id
    if row["run_id"] == run_id:
        return True
    if row["expires_at"] < cutoff:
        conn.execute(
            "UPDATE bot_lease SET run_id = ?, expires_at = ? WHERE id = 1",
            (run_id, _utcnow()),
        )
        conn.commit()
        check = conn.execute("SELECT run_id FROM bot_lease WHERE id = 1").fetchone()
        return check and check["run_id"] == run_id
    return False


def renew_lease(run_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE bot_lease SET expires_at = ? WHERE id = 1 AND run_id = ?",
        (_utcnow(), run_id),
    )
    conn.commit()


def release_lease(run_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM bot_lease WHERE id = 1 AND run_id = ?", (run_id,))
    conn.commit()


# --- Автоматический бэкап БД при старте ---

def automatic_backup(max_kept: int = 30) -> str | None:
    """Создаёт копию текущей БД (с учётом WAL) в папке backups/.
    Возвращает путь к бэкапу или None. Старые копии сверх max_kept удаляются."""
    import os
    import shutil
    from datetime import datetime, timezone
    try:
        bak_dir = os.path.join(os.getcwd(), "backups")
        os.makedirs(bak_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(bak_dir, f"wardogs_v2.{stamp}.db")

        src = get_conn()
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()

        # Очистка старых бэкапов (по времени создания имени).
        files = sorted(
            f for f in os.listdir(bak_dir)
            if f.startswith("wardogs_v2.") and f.endswith(".db")
        )
        for f in files[:-max_kept]:
            try:
                os.remove(os.path.join(bak_dir, f))
            except OSError:
                pass
        return dest
    except Exception:
        return None


# --- Giveaways ---

def create_giveaway(channel_id: int, guild_id: int, prize: str, ends_at: str, winners: int, created_by: int) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO giveaways (channel_id, guild_id, prize, ends_at, winners, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (channel_id, guild_id, prize, ends_at, winners, created_by),
    )
    conn.commit()
    return cur.lastrowid


def get_active_giveaways() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM giveaways WHERE done = 0").fetchall()]


def get_giveaway(giveaway_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,)).fetchone()
    return dict(row) if row else None


def get_giveaway_by_message(channel_id: int, message_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM giveaways WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return dict(row) if row else None


def insert_recovered_giveaway(channel_id: int, guild_id: int, prize: str, ends_at: str,
                              winners: int, created_by: int, message_id: int) -> int:
    """Восстанавливает розыгрыш, когда он пропал из БД, но сообщение на сервере ещё есть.
    Сохраняет message_id для персистентной кнопки. Возвращает новый id."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO giveaways (channel_id, guild_id, prize, ends_at, winners, created_by, message_id, done) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (channel_id, guild_id, prize, ends_at, winners, created_by, message_id),
    )
    conn.commit()
    return cur.lastrowid


def set_giveaway_done(giveaway_id: int):
    conn = get_conn()
    conn.execute("UPDATE giveaways SET done = 1, finished_at = ? WHERE id = ?", (_utcnow(), giveaway_id))
    conn.commit()


def set_giveaway_message_id(giveaway_id: int, message_id: int):
    conn = get_conn()
    conn.execute("UPDATE giveaways SET message_id = ? WHERE id = ?", (message_id, giveaway_id))
    conn.commit()


def add_participant(giveaway_id: int, user_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO giveaway_participants (giveaway_id, user_id) VALUES (?, ?)",
        (giveaway_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_participant(giveaway_id: int, user_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM giveaway_participants WHERE giveaway_id = ? AND user_id = ?",
        (giveaway_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_participants(giveaway_id: int) -> list[int]:
    conn = get_conn()
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM giveaway_participants WHERE giveaway_id = ? ORDER BY joined_at",
        (giveaway_id,),
    ).fetchall()]


def get_participant_count(giveaway_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM giveaway_participants WHERE giveaway_id = ?",
        (giveaway_id,),
    ).fetchone()
    return row["c"] if row else 0


def migrate_giveaways_from(path: str) -> int:
    """Переносит активные розыгрыши (и их участников) из БД v1 (wardogs.db)
    в текущую БД v2, сохраняя id, message_id и channel_id — чтобы идущий
    розыгрыш с персистентной кнопкой gwa_join:{id} не отвалился при переезде."""
    import os
    if not os.path.isfile(path):
        return 0
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        tabs = {r["name"] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "giveaways" not in tabs:
            return 0
    except sqlite3.DatabaseError:
        src.close()
        return 0

    conn = get_conn()
    moved = 0
    for g in src.execute("SELECT * FROM giveaways WHERE done = 0").fetchall():
        d = dict(g)
        conn.execute(
            "INSERT OR REPLACE INTO giveaways (id, channel_id, guild_id, prize, ends_at, winners, created_by, message_id, done, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d.get("id"), d.get("channel_id"), d.get("guild_id"), d.get("prize"),
             d.get("ends_at"), d.get("winners"), d.get("created_by"),
             d.get("message_id", 0) or 0, 0, d.get("finished_at")),
        )
        for p in src.execute("SELECT user_id, joined_at FROM giveaway_participants WHERE giveaway_id = ?", (d.get("id"),)).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO giveaway_participants (giveaway_id, user_id, joined_at) VALUES (?, ?, ?)",
                (d.get("id"), p["user_id"], p["joined_at"]),
            )
        moved += 1
    conn.commit()
    src.close()
    return moved


# --- Server sync ---

def sf_joined_at(member_dt) -> str | None:
    from datetime import datetime, timezone
    if member_dt is None:
        return None
    if isinstance(member_dt, datetime):
        if member_dt.tzinfo is None:
            member_dt = member_dt.replace(tzinfo=timezone.utc)
        return member_dt.isoformat()
    return str(member_dt)


def upsert_member(user_id: int, guild_id: int, username: str | None, display_name: str | None,
                  joined_at: str | None, roles: str, is_bot: bool, last_seen: str | None = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO server_members (user_id, guild_id, username, display_name, joined_at, roles, is_bot, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, ?)) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
        "username=excluded.username, display_name=excluded.display_name, roles=excluded.roles, last_seen=COALESCE(excluded.last_seen, server_members.last_seen)",
        (user_id, guild_id, username, display_name, sf_joined_at(joined_at), roles, 1 if is_bot else 0, last_seen, _utcnow()),
    )
    conn.commit()


def remove_member(user_id: int, guild_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM server_members WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    conn.commit()


def get_members(guild_id: int, limit: int = 1000) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM server_members WHERE guild_id = ? ORDER BY last_seen DESC, user_id LIMIT ?",
        (guild_id, limit),
    ).fetchall()]


def get_member_count(guild_id: int) -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM server_members WHERE guild_id = ?", (guild_id,)).fetchone()
    return row["c"] if row else 0


def upsert_role(role_id: int, guild_id: int, name: str, color: int, position: int, permissions: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO server_roles (role_id, guild_id, name, color, position, permissions) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(role_id) DO UPDATE SET name=excluded.name, color=excluded.color, position=excluded.position, permissions=excluded.permissions",
        (role_id, guild_id, name, color, position, permissions),
    )
    conn.commit()


def remove_role(role_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM server_roles WHERE role_id = ?", (role_id,))
    conn.commit()


def get_roles(guild_id: int) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM server_roles WHERE guild_id = ? ORDER BY position DESC", (guild_id,)).fetchall()]


def upsert_channel(channel_id: int, guild_id: int, name: str, ctype: str, category_id: int | None, position: int, topic: str | None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO server_channels (channel_id, guild_id, name, type, category_id, position, topic) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET name=excluded.name, category_id=excluded.category_id, position=excluded.position, topic=excluded.topic",
        (channel_id, guild_id, name, ctype, category_id, position, topic),
    )
    conn.commit()


def remove_channel(channel_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM server_channels WHERE channel_id = ?", (channel_id,))
    conn.commit()


def get_channels(guild_id: int) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM server_channels WHERE guild_id = ? ORDER BY position", (guild_id,)).fetchall()]


def log_server_event(guild_id: int, event_type: str, target_id: int | None, actor_id: int | None, details: str = "") -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO server_events (guild_id, event_type, target_id, actor_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, event_type, target_id, actor_id, details, _utcnow()),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_events(guild_id: int, limit: int = 50) -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM server_events WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()]


# --- Announce (ЛС-панель: выбор канала для persistent-кнопки «Отправить») ---

def set_announce_channel(user_id: int, channel_id: int, guild_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO announce_pending (user_id, channel_id, guild_id, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET channel_id = excluded.channel_id, guild_id = excluded.guild_id, updated_at = excluded.updated_at",
        (user_id, channel_id, guild_id, _utcnow()),
    )
    conn.commit()


def get_announce_channel(user_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT channel_id, guild_id FROM announce_pending WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def clear_announce_channel(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM announce_pending WHERE user_id = ?", (user_id,))
    conn.commit()


# --- Каналы-ловушки (сообщение в канале = кик) ---

def set_trap_channel(channel_id: int, guild_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO trap_channels (channel_id, guild_id, created_at) VALUES (?, ?, ?)",
        (channel_id, guild_id, _utcnow()),
    )
    conn.commit()


def remove_trap_channel(channel_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM trap_channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    return cur.rowcount > 0


def get_trap_channels() -> list[int]:
    conn = get_conn()
    return [
        r["channel_id"]
        for r in conn.execute("SELECT channel_id FROM trap_channels").fetchall()
    ]