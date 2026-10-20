import hashlib
import sqlite3
import threading
import os
from pathlib import Path

try:
    import db_backend
except ImportError:  # package import (app.bot.database)
    from . import db_backend

DB_FILE = "wardogs_v2.db"
_local = threading.local()
_lock = threading.Lock()
_conns: set = set()

# site.db панели (рядом с app/bot/ → ../../../site.db)
_PANEL_DB = Path(__file__).resolve().parent.parent.parent / "site.db"
_panel_local = threading.local()


def get_panel_conn():
    if db_backend.panel_using_pg():
        return db_backend.get_panel_conn()
    conn = getattr(_panel_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_PANEL_DB), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        _panel_local.conn = conn
    return conn


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _time_ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def get_conn():
    if db_backend.using_pg():
        return db_backend.get_bot_conn()
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
    db_backend.close_all()


def db_file_exists() -> bool:
    if db_backend.using_pg():
        return db_backend.bot_ready()
    import os
    return os.path.isfile(DB_FILE) or os.path.isfile(DB_FILE + "-wal") or os.path.isfile(DB_FILE + "-shm")


def _insert_returning_id(conn, sql: str, params) -> int:
    """INSERT c получением id: sqlite lastrowid либо PG RETURNING id."""
    if getattr(conn, "is_pg", False):
        cur = conn.execute(sql + " RETURNING id", params)
        row = cur.fetchone()
        return row["id"] if row else 0
    cur = conn.execute(sql, params)
    return cur.lastrowid


def _ensure_column(conn, table: str, column: str, ddl: str):
    if db_backend.using_pg():
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        except Exception:
            pass
        return
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

        CREATE TABLE IF NOT EXISTS donations_orders (
            id TEXT PRIMARY KEY,
            buyer_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            product_id TEXT,
            amount_kopecks INTEGER NOT NULL,
            recipients TEXT DEFAULT '[]',
            public_thanks INTEGER DEFAULT 1,
            source TEXT DEFAULT 'bot',
            created_at INTEGER,
            expires_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS donations_carts (
            id TEXT PRIMARY KEY,
            buyer_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            recipients TEXT DEFAULT '[]',
            expires_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS donations_donors (
            user_id INTEGER PRIMARY KEY,
            total INTEGER DEFAULT 0,
            public INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS donations_payments (
            operation_id TEXT PRIMARY KEY,
            order_id TEXT,
            buyer_id INTEGER,
            amount INTEGER,
            kind TEXT,
            product_id TEXT,
            timestamp_ms INTEGER,
            total_after INTEGER,
            previous_role_id INTEGER,
            new_role_id INTEGER,
            role_done INTEGER DEFAULT 0,
            thanks_done INTEGER DEFAULT 0,
            vip_done INTEGER DEFAULT 0,
            source TEXT DEFAULT 'feed',
            admin_id INTEGER,
            reason TEXT,
            recorded_at INTEGER,
            request_id TEXT
        );

        CREATE TABLE IF NOT EXISTS donations_reviews (
            operation_id TEXT PRIMARY KEY,
            event_json TEXT,
            reason TEXT
        );

        CREATE TABLE IF NOT EXISTS donations_vip (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            buyer_id INTEGER,
            product_id TEXT,
            starts_at INTEGER,
            ends_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS donations_adjustments (
            id TEXT PRIMARY KEY,
            buyer_id INTEGER,
            amount INTEGER,
            admin_id INTEGER,
            reason TEXT,
            recorded_at INTEGER,
            total_before INTEGER,
            total_after INTEGER,
            previous_role_id INTEGER,
            new_role_id INTEGER,
            role_done INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS donations_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS seed_users (
            id TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL UNIQUE,
            steam_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT 'Игрок',
            balance INTEGER NOT NULL DEFAULT 0,
            total_seconds INTEGER NOT NULL DEFAULT 0,
            lifetime_points INTEGER NOT NULL DEFAULT 0,
            linked INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_link_challenges (
            discord_id TEXT PRIMARY KEY,
            steam_id TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT 'Игрок',
            code_hash TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'seeding',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_donor_links (
            id TEXT PRIMARY KEY,
            discord_id TEXT NOT NULL UNIQUE,
            steam_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT 'Игрок',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_weekly_stats (
            user_id TEXT NOT NULL,
            week_key TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            manual_seconds INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, week_key)
        );

        CREATE TABLE IF NOT EXISTS seed_active_sessions (
            user_id TEXT PRIMARY KEY,
            continuous_seconds INTEGER NOT NULL DEFAULT 0,
            last_eligible_at INTEGER NOT NULL,
            ineligible_since INTEGER
        );

        CREATE TABLE IF NOT EXISTS seed_point_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            ref TEXT NOT NULL UNIQUE,
            delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '{}',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_vip_memberships (
            user_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'inactive',
            queued_seconds INTEGER NOT NULL DEFAULT 0,
            active_until INTEGER,
            managed_by_bot INTEGER,
            pending_since INTEGER,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_vip_orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            sku TEXT NOT NULL,
            cost INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_weekly_reports (
            week_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL DEFAULT '{}',
            published_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seed_system_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sw_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT NOT NULL,
            steam_id TEXT NOT NULL,
            source_faction TEXT NOT NULL,
            target_faction TEXT NOT NULL,
            diff INTEGER NOT NULL,
            player_count INTEGER NOT NULL,
            faction_counts TEXT NOT NULL,
            is_vip INTEGER NOT NULL DEFAULT 0,
            actor_discord_id TEXT,
            map TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ticket_types (
            type_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            emoji TEXT NOT NULL DEFAULT '',
            color INTEGER NOT NULL DEFAULT 5793266,
            position INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        );
    """)
    conn.commit()
    # Связь с тикетами панели: сайт-тикет + флаг транскрипта.
    _ensure_column(conn, "tickets", "site_ticket_id", "TEXT")
    _ensure_column(conn, "tickets", "transcript_saved", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    _clan_form = (
        "# ЗАЯВКА НА РЕГИСТРАЦИЮ КЛАНА\n\n"
        "1. Название клана.\n2. Тег|Роль клана.\n3. Ник лидера.\n4. Количество участников.\n"
        "5. Ссылка на Discord клана.\n6. Кратко о клане.\n7. Цели и задачи клана.\n\n"
        "> Ознакомлен с Положением о регистрации и деятельности кланов WARDOGS | RU "
        "и согласен соблюдать установленные правила."
    )
    for row in (
        ("complaint", "Жалоба", "Отправьте жалобу — администрация разберётся в ближайшее время.", "📢", 9109504, 0),
        ("suggestion", "Предложение", "Отправьте ваше предложение — спасибо за вклад в развитие сервера!", "💡", 5763719, 1),
        ("vip_bonuses", "Покупка VIP за бонусы",
         "Откройте канал магазина сидинга и нажмите кнопку нужного срока VIP. "
         "Баланс баллов и статус: команда /seed status.", "👑", 14727003, 2),
        ("clan", "Зарегистрировать клан", _clan_form, "📝", 16766720, 10),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO ticket_types (type_id, label, description, emoji, color, position) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()


# Ticket types (Discord-панель тикетов)
def list_ticket_types(include_disabled: bool = False):
    conn = get_conn()
    sql = "SELECT * FROM ticket_types"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY position ASC, label ASC"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_ticket_type(type_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM ticket_types WHERE type_id = ?", (type_id,)).fetchone()
    return dict(row) if row else None


def upsert_ticket_type(type_id, label, description="", emoji="", color=5793266, position=0, enabled=1):
    conn = get_conn()
    conn.execute(
        "INSERT INTO ticket_types (type_id, label, description, emoji, color, position, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (type_id) DO UPDATE SET label = EXCLUDED.label, description = EXCLUDED.description, "
        "emoji = EXCLUDED.emoji, color = EXCLUDED.color, position = EXCLUDED.position, enabled = EXCLUDED.enabled",
        (type_id, label, description, emoji, color, position, 1 if enabled else 0),
    )
    conn.commit()


def delete_ticket_type(type_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM ticket_types WHERE type_id = ?", (type_id,))
    conn.commit()
    return bool(getattr(cur, "rowcount", 0))


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
    return _insert_returning_id(
        conn,
        "INSERT INTO warnings (user_id, guild_id, reason, moderator_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, guild_id, reason, moderator_id, _utcnow()),
    )


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
    Возвращает путь к бэкапу или None. Старые копии сверх max_kept удаляются.
    В PG-режиме файловых бэкапов нет — дампы делает хостинг/pg_dump."""
    if db_backend.using_pg():
        return None
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
    return _insert_returning_id(
        conn,
        "INSERT INTO giveaways (channel_id, guild_id, prize, ends_at, winners, created_by) VALUES (?, ?, ?, ?, ?, ?)",
        (channel_id, guild_id, prize, ends_at, winners, created_by),
    )


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
    return _insert_returning_id(
        conn,
        "INSERT INTO giveaways (channel_id, guild_id, prize, ends_at, winners, created_by, message_id, done) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (channel_id, guild_id, prize, ends_at, winners, created_by, message_id),
    )


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


def upsert_members_batch(rows: list[tuple]) -> None:
    """Пакетный upsert участников одним executemany (server_sync).
    Каждая строка: (user_id, guild_id, username, display_name, joined_at_joined_iso,
    roles, is_bot_0_1, last_seen, last_seen_fallback_now)."""
    if not rows:
        return
    conn = get_conn()
    conn.executemany(
        "INSERT INTO server_members (user_id, guild_id, username, display_name, joined_at, roles, is_bot, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, ?)) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
        "username=excluded.username, display_name=excluded.display_name, roles=excluded.roles, last_seen=COALESCE(excluded.last_seen, server_members.last_seen)",
        rows,
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
    return _insert_returning_id(
        conn,
        "INSERT INTO server_events (guild_id, event_type, target_id, actor_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, event_type, target_id, actor_id, details, _utcnow()),
    )


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

# ---- Donations ----

def _row_to_dict(row):
    return dict(row) if row else None


def get_donation_meta(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM donations_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_donation_meta(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO donations_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def save_donation_order(order):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_orders"
        "(id,buyer_id,kind,product_id,amount_kopecks,recipients,public_thanks,source,created_at,expires_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (order["id"], order["buyerId"], order["kind"], order.get("productId"),
         order["amountKopecks"], __import__("json").dumps(order.get("recipients", [])),
         1 if order.get("publicThanks", True) else 0, order.get("source", "bot"),
         order["createdAt"], order["expiresAt"]),
    )


def get_donation_order(order_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM donations_orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["recipients"] = __import__("json").dumps and __import__("json").loads(d.get("recipients") or "[]")
    d["publicThanks"] = bool(d.pop("public_thanks", 1))
    d["buyerId"] = d.pop("buyer_id")
    d["amountKopecks"] = d.pop("amount_kopecks")
    d["productId"] = d.pop("product_id")
    d["createdAt"] = d.pop("created_at")
    d["expiresAt"] = d.pop("expires_at")
    return d


def save_donation_cart(cart):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_carts(id,buyer_id,product_id,recipients,expires_at)"
        " VALUES(?,?,?,?,?)",
        (cart["id"], cart["buyerId"], cart["productId"],
         __import__("json").dumps(cart.get("recipients", [])), cart["expiresAt"]),
    )


def get_donation_cart(cart_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM donations_carts WHERE id=?", (cart_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["recipients"] = __import__("json").loads(d.get("recipients") or "[]")
    d["buyerId"] = d.pop("buyer_id")
    d["productId"] = d.pop("product_id")
    d["expiresAt"] = d.pop("expires_at")
    return d


def delete_donation_cart(cart_id):
    get_conn().execute("DELETE FROM donations_carts WHERE id=?", (cart_id,))


def get_donor_total(user_id):
    conn = get_conn()
    row = conn.execute("SELECT total FROM donations_donors WHERE user_id=?", (user_id,)).fetchone()
    return row["total"] if row else 0


def set_donor_total(user_id, total, public=True):
    conn = get_conn()
    conn.execute(
        "INSERT INTO donations_donors(user_id,total,public) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET total=excluded.total, public=excluded.public",
        (user_id, total, 1 if public else 0),
    )


def list_donors():
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT user_id, total, public FROM donations_donors WHERE total > 0 ORDER BY total DESC LIMIT 10"
    ).fetchall()]


def save_donation_payment(p):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_payments"
        "(operation_id,order_id,buyer_id,amount,kind,product_id,timestamp_ms,total_after,"
        "previous_role_id,new_role_id,role_done,thanks_done,vip_done,source,admin_id,reason,recorded_at,request_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p["operationId"], p.get("orderId"), p.get("buyerId"), p.get("amount"),
         p.get("kind"), p.get("productId"), p.get("timestamp"), p.get("totalAfter"),
         p.get("previousRoleId"), p.get("newRoleId"),
         1 if p.get("roleDone") else 0, 1 if p.get("thanksDone") else 0, 1 if p.get("vipDone") else 0,
         p.get("source", "feed"), p.get("adminId"), p.get("reason"),
         p.get("recordedAt"), str(p.get("requestId", ""))),
    )


def get_donation_payment(op_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM donations_payments WHERE operation_id=?", (op_id,)).fetchone()
    return _row_to_dict(row)


def mark_payment_field(op_id, field):
    conn = get_conn()
    conn.execute(f"UPDATE donations_payments SET {field}=1 WHERE operation_id=?", (op_id,))


def list_pending_payments():
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM donations_payments WHERE role_done=0 OR thanks_done=0"
    ).fetchall()]


def save_donation_review(op_id, event_json, reason):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_reviews(operation_id,event_json,reason) VALUES(?,?,?)",
        (op_id, event_json, reason),
    )


def delete_donation_review(op_id):
    get_conn().execute("DELETE FROM donations_reviews WHERE operation_id=?", (op_id,))


def list_donation_reviews():
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM donations_reviews LIMIT 50").fetchall()]


def save_donation_vip(v):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_vip(id,user_id,buyer_id,product_id,starts_at,ends_at)"
        " VALUES(?,?,?,?,?,?)",
        (v["id"], v["userId"], v["buyerId"], v["productId"], v["startsAt"], v["endsAt"]),
    )


def list_active_vip(user_id, now_ms):
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM donations_vip WHERE user_id=? AND ends_at > ?", (user_id, now_ms)
    ).fetchall()]


def save_donation_adjustment(a):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO donations_adjustments"
        "(id,buyer_id,amount,admin_id,reason,recorded_at,total_before,total_after,previous_role_id,new_role_id,role_done)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (a["id"], a["buyerId"], a["amount"], a["adminId"], a["reason"],
         a["recordedAt"], a["totalBefore"], a["totalAfter"],
         a.get("previousRoleId"), a.get("newRoleId"), 1 if a.get("roleDone") else 0),
    )


def get_donation_adjustment(adj_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM donations_adjustments WHERE id=?", (adj_id,)).fetchone()
    return _row_to_dict(row)


def mark_adjustment_done(adj_id):
    get_conn().execute("UPDATE donations_adjustments SET role_done=1 WHERE id=?", (adj_id,))


def list_pending_adjustments():
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM donations_adjustments WHERE role_done=0"
    ).fetchall()]


def get_all_donor_ids():
    conn = get_conn()
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM donations_donors WHERE total > 0").fetchall()]


def get_feed_offset():
    return int(get_donation_meta("feed_offset", "0") or "0")


def set_feed_offset(offset):
    set_donation_meta("feed_offset", str(offset))


def is_top_dirty():
    return get_donation_meta("top_dirty", "0") == "1"


def set_top_dirty(val):
    set_donation_meta("top_dirty", "1" if val else "0")


# ---- Seeding (порт seeding-bot, plain SQLite) ----

import json as _json
import time as _time
import uuid as _uuid


def _now_ms() -> int:
    return int(_time.time() * 1000)


class SeedDomainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def seed_get_user_by_discord(discord_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM seed_users WHERE linked = 1 AND discord_id = ?", (str(discord_id),)
    ).fetchone()
    return dict(row) if row else None


def seed_get_user_by_steam(steam_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM seed_users WHERE linked = 1 AND steam_id = ?", (str(steam_id),)
    ).fetchone()
    return dict(row) if row else None


def seed_get_user_by_id(user_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM seed_users WHERE id = ?", (str(user_id),)).fetchone()
    return dict(row) if row else None


def seed_list_users() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM seed_users WHERE linked = 1 ORDER BY created_at"
    ).fetchall()]


def seed_get_donor_by_discord(discord_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM seed_donor_links WHERE discord_id = ?", (str(discord_id),)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["linked"] = True
    d["purpose"] = "donation"
    return d


def seed_get_donor_by_steam(steam_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM seed_donor_links WHERE steam_id = ?", (str(steam_id),)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["linked"] = True
    d["purpose"] = "donation"
    return d


def seed_get_vip_identity_by_discord(discord_id: str) -> dict | None:
    return seed_get_user_by_discord(discord_id) or seed_get_donor_by_discord(discord_id)


def seed_get_vip_identity_by_id(user_id: str) -> dict | None:
    user = seed_get_user_by_id(user_id)
    if user:
        return user
    conn = get_conn()
    row = conn.execute("SELECT * FROM seed_donor_links WHERE id = ?", (str(user_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["linked"] = True
    return d


def seed_prepare_link_challenge(
    discord_id: str,
    steam_id: str,
    display_name: str,
    code_hash: str,
    expires_at: int,
    purpose: str = "seeding",
):
    conn = get_conn()
    now = _now_ms()
    if conn.execute(
        "SELECT id FROM seed_users WHERE linked = 1 AND discord_id = ?", (discord_id,)
    ).fetchone():
        if purpose == "donation":
            raise SeedDomainError(
                "already_linked",
                "Steam уже привязан для сидинга и подходит для оплаченного VIP. Новая привязка не нужна.",
            )
        raise SeedDomainError("already_linked", "Ваш Discord уже привязан к Steam.")
    if conn.execute(
        "SELECT id FROM seed_users WHERE linked = 1 AND steam_id = ?", (steam_id,)
    ).fetchone():
        raise SeedDomainError("steam_taken", "Этот Steam уже привязан к другому Discord.")
    donor_discord = seed_get_donor_by_discord(discord_id)
    donor_steam = seed_get_donor_by_steam(steam_id)
    if purpose == "donation" and donor_discord:
        raise SeedDomainError("already_linked", "Steam уже привязан для VIP.")
    if purpose == "donation" and donor_steam:
        raise SeedDomainError("steam_taken", "Discord или Steam уже привязан к другой паре аккаунтов.")
    if purpose == "seeding" and (
        (donor_discord and donor_discord["steam_id"] != steam_id)
        or (donor_steam and donor_steam["discord_id"] != discord_id)
    ):
        raise SeedDomainError("steam_taken", "Discord или Steam уже привязан к другой паре аккаунтов.")
    conn.execute(
        """
        INSERT INTO seed_link_challenges (discord_id, steam_id, display_name, code_hash, purpose, attempts, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            steam_id=excluded.steam_id,
            display_name=excluded.display_name,
            code_hash=excluded.code_hash,
            purpose=excluded.purpose,
            attempts=0,
            created_at=excluded.created_at,
            expires_at=excluded.expires_at
        """,
        (discord_id, steam_id, display_name[:80], code_hash, purpose, now, expires_at),
    )
    seed_audit("link_challenge_created", discord_id, {"steam_id": steam_id, "purpose": purpose})
    conn.commit()


def seed_cancel_link_challenge(discord_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (str(discord_id),))
    seed_audit("link_challenge_cancelled", discord_id, {})
    conn.commit()


def seed_verify_link_challenge(
    discord_id: str,
    code: str,
    max_attempts: int,
    purpose: str = "seeding",
) -> dict:
    import hmac as _hmac

    conn = get_conn()
    now = _now_ms()
    row = conn.execute(
        "SELECT * FROM seed_link_challenges WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    command = "/vip-link link" if purpose == "donation" else "/seed link"
    if not row:
        raise SeedDomainError("challenge_missing", f"Активный код не найден. Сначала выполните {command}.")
    if row["expires_at"] < now:
        conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
        seed_audit("link_challenge_expired", discord_id, {})
        conn.commit()
        raise SeedDomainError("challenge_expired", f"Код истёк. Запросите новый через {command}.")
    if (row["purpose"] or "seeding") != purpose:
        raise SeedDomainError(
            "wrong_link_purpose",
            f"Этот код создан для другой привязки. Подтвердите его нужной командой или начните заново через {command}.",
        )
    supplied_hash = _hash_code(discord_id, code)
    if not _hmac.compare_digest(str(row["code_hash"]), supplied_hash):
        attempts = int(row["attempts"]) + 1
        if attempts >= max_attempts:
            conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
            seed_audit("link_challenge_locked", discord_id, {})
            conn.commit()
            raise SeedDomainError("challenge_locked", "Слишком много попыток. Запросите новый код.")
        conn.execute(
            "UPDATE seed_link_challenges SET attempts = ? WHERE discord_id = ?",
            (attempts, discord_id),
        )
        conn.commit()
        raise SeedDomainError("bad_code", f"Неверный код. Осталось попыток: {max_attempts - attempts}.")

    steam_id = row["steam_id"]
    if conn.execute(
        "SELECT id FROM seed_users WHERE linked = 1 AND (discord_id = ? OR steam_id = ?)",
        (discord_id, steam_id),
    ).fetchone():
        raise SeedDomainError("identity_taken", "Discord или Steam уже привязан. Обратитесь к администратору.")

    donor_discord = seed_get_donor_by_discord(discord_id)
    donor_steam = seed_get_donor_by_steam(steam_id)
    if purpose == "donation" and (donor_discord or donor_steam):
        raise SeedDomainError("identity_taken", "Discord или Steam уже привязан к другой паре аккаунтов.")
    if purpose == "seeding" and (
        (donor_discord and donor_discord["steam_id"] != steam_id)
        or (donor_steam and donor_steam["discord_id"] != discord_id)
    ):
        raise SeedDomainError("identity_taken", "Discord или Steam уже привязан к другой паре аккаунтов.")

    archived = [
        dict(r)
        for r in conn.execute("SELECT * FROM seed_users WHERE linked = 0").fetchall()
        if r["discord_id"] == discord_id and r["steam_id"] == steam_id
    ]
    partial = conn.execute(
        "SELECT id FROM seed_users WHERE linked = 0 AND (discord_id = ? OR steam_id = ?)",
        (discord_id, steam_id),
    ).fetchone()
    if len(archived) > 1 or (not archived and partial):
        raise SeedDomainError(
            "identity_history_conflict",
            "Новая пара Discord↔Steam не совпадает с архивной. Автоматический перенос баллов заблокирован; обратитесь к администратору.",
        )

    if purpose == "donation":
        donor_id = _uuid.uuid4().hex
        conn.execute(
            "INSERT INTO seed_donor_links (id, discord_id, steam_id, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (donor_id, discord_id, steam_id, row["display_name"], now),
        )
        conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
        seed_audit("donation_identity_linked", discord_id, {"id": donor_id})
        conn.commit()
        return {
            "id": donor_id,
            "discord_id": discord_id,
            "steam_id": steam_id,
            "display_name": row["display_name"],
            "linked": True,
            "purpose": "donation",
        }

    if donor_discord:
        conn.execute("DELETE FROM seed_donor_links WHERE id = ?", (donor_discord["id"],))
        seed_audit("donation_identity_promoted", discord_id, {"donor_id": donor_discord["id"]})

    if archived:
        user = archived[0]
        conn.execute(
            "UPDATE seed_users SET discord_id = ?, steam_id = ?, display_name = ?, linked = 1, updated_at = ? WHERE id = ?",
            (discord_id, steam_id, row["display_name"], now, user["id"]),
        )
        seed_audit("identity_relinked", discord_id, {"user_id": user["id"]})
        user_id = user["id"]
        balance = user["balance"]
        total_seconds = user["total_seconds"]
        lifetime_points = user["lifetime_points"]
    else:
        user_id = _uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO seed_users (id, discord_id, steam_id, display_name, balance, total_seconds, lifetime_points, linked, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, 0, 0, 1, ?, ?)
            """,
            (user_id, discord_id, steam_id, row["display_name"], now, now),
        )
        seed_audit("identity_linked", discord_id, {"user_id": user_id})
        balance = total_seconds = lifetime_points = 0

    conn.execute(
        "INSERT OR IGNORE INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, 0, ?, ?, ?)",
        (user_id, f"link:{user_id}", balance, "account_linked", now),
    )
    conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
    conn.commit()
    return {
        "id": user_id,
        "discord_id": discord_id,
        "steam_id": steam_id,
        "display_name": row["display_name"],
        "balance": balance,
        "total_seconds": total_seconds,
        "lifetime_points": lifetime_points,
        "linked": True,
    }


def seed_hash_code(discord_id: str, code: str) -> str:
    return _hash_code(discord_id, code)


def _hash_code(discord_id: str, code: str) -> str:
    import hashlib as _hashlib
    import config as _config

    key = (_config.SEED_CHALLENGE_KEY or "wardogs-seeding-v1").encode()
    return _hashlib.sha256(key + b"\0" + str(discord_id).encode() + b"\0" + str(code).strip().upper().encode()).hexdigest()


def seed_unlink_discord(discord_id: str, actor_id: str | None = None, reason: str = "self_unlink"):
    conn = get_conn()
    now = _now_ms()
    user = seed_get_user_by_discord(discord_id)
    if not user:
        raise SeedDomainError("not_linked", "Steam не привязан к вашему Discord.")
    membership = seed_get_vip_membership(user["id"])
    if membership and membership["state"] != "inactive":
        raise SeedDomainError(
            "vip_active",
            "Нельзя отменить привязку, пока VIP активен или ожидает применения/снятия. Дождитесь завершения либо обратитесь к администратору.",
        )
    feed = seed_get_state("donation_feed", {"vip": {}})
    paid = (feed.get("vip") or {}).get(discord_id) or {}
    claims = seed_get_state("paid_slot_claims", {})
    if int(paid.get("endsAt") or 0) > now or claims.get(user["steam_id"]):
        raise SeedDomainError(
            "paid_vip_active",
            "Нельзя отменить привязку, пока оплаченный VIP действует или reserved slot ожидает снятия.",
        )
    tombstone = _uuid.uuid4().hex
    conn.execute(
        "UPDATE seed_users SET discord_id = ?, steam_id = ?, linked = 0, updated_at = ? WHERE id = ? AND linked = 1",
        (f"unlinked:{user['id']}:{tombstone}", f"unlinked:{tombstone}:{user['id']}", now, user["id"]),
    )
    conn.execute("DELETE FROM seed_active_sessions WHERE user_id = ?", (user["id"],))
    conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
    seed_audit("identity_unlinked", actor_id or discord_id, {"user_id": user["id"], "reason": reason[:100]})
    conn.commit()
    return {**user, "linked": False}


def seed_unlink_donor(discord_id: str):
    conn = get_conn()
    now = _now_ms()
    donor = seed_get_donor_by_discord(discord_id)
    if not donor:
        raise SeedDomainError("not_linked", "Отдельной VIP-привязки нет. Если вы сидер, используйте /seed unlink.")
    feed = seed_get_state("donation_feed", {"vip": {}})
    paid = (feed.get("vip") or {}).get(discord_id) or {}
    claims = seed_get_state("paid_slot_claims", {})
    if int(paid.get("endsAt") or 0) > now or claims.get(donor["steam_id"]):
        raise SeedDomainError("paid_vip_active", "Нельзя отменить привязку, пока оплаченный VIP действует или игровой слот ожидает снятия.")
    conn.execute("DELETE FROM seed_donor_links WHERE id = ?", (donor["id"],))
    conn.execute("DELETE FROM seed_link_challenges WHERE discord_id = ?", (discord_id,))
    seed_audit("donation_identity_unlinked", discord_id, {"donor_id": donor["id"]})
    conn.commit()
    return {**donor, "linked": False}


def seed_reset_sessions():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM seed_active_sessions").fetchone()["c"]
    if count:
        conn.execute("DELETE FROM seed_active_sessions")
        seed_audit("sessions_reset_on_startup", "", {"count": count})
    conn.commit()


def seed_apply_seeding_tick(eligible: list[dict], now_ms: int, week_key: str, grace_seconds: int, max_delta_seconds: int) -> list[dict]:
    eligible_map = {m["discord_id"]: m for m in eligible}
    conn = get_conn()
    awards: list[dict] = []
    users = seed_list_users()
    for user in users:
        member = eligible_map.get(user["discord_id"])
        sess_row = conn.execute(
            "SELECT * FROM seed_active_sessions WHERE user_id = ?", (user["id"],)
        ).fetchone()
        if not member:
            if not sess_row:
                continue
            ineligible_since = sess_row["ineligible_since"]
            if ineligible_since is None:
                conn.execute(
                    "UPDATE seed_active_sessions SET ineligible_since = ? WHERE user_id = ?",
                    (now_ms, user["id"]),
                )
                conn.commit()
                continue
            if now_ms - ineligible_since > grace_seconds * 1000:
                conn.execute("DELETE FROM seed_active_sessions WHERE user_id = ?", (user["id"],))
                conn.commit()
            continue

        display_name = member.get("display_name") or user["display_name"]
        display_name = str(display_name)[:80]
        if display_name != user["display_name"]:
            conn.execute(
                "UPDATE seed_users SET display_name = ?, updated_at = ? WHERE id = ?",
                (display_name, now_ms, user["id"]),
            )
            user["display_name"] = display_name

        if not sess_row:
            conn.execute(
                "INSERT INTO seed_active_sessions (user_id, continuous_seconds, last_eligible_at, ineligible_since) VALUES (?, 0, ?, NULL)",
                (user["id"], now_ms),
            )
            conn.commit()
            continue

        continuous = int(sess_row["continuous_seconds"])
        if sess_row["ineligible_since"] is not None:
            if now_ms - sess_row["ineligible_since"] > grace_seconds * 1000:
                continuous = 0
            conn.execute(
                "UPDATE seed_active_sessions SET continuous_seconds = ?, last_eligible_at = ?, ineligible_since = NULL WHERE user_id = ?",
                (continuous, now_ms, user["id"]),
            )
            conn.commit()
            continue

        raw_delta = int((now_ms - int(sess_row["last_eligible_at"])) / 1000)
        delta = max(0, min(raw_delta, max_delta_seconds))
        if not delta:
            continue
        before = continuous
        after = before + delta
        points = _incremental_points(before, after)
        conn.execute(
            "UPDATE seed_active_sessions SET continuous_seconds = ?, last_eligible_at = ? WHERE user_id = ?",
            (after, now_ms, user["id"]),
        )
        new_total_seconds = user["total_seconds"] + delta
        new_lifetime = user["lifetime_points"] + points
        new_balance = user["balance"] + points
        conn.execute(
            "UPDATE seed_users SET total_seconds = ?, lifetime_points = ?, balance = ?, updated_at = ? WHERE id = ?",
            (new_total_seconds, new_lifetime, new_balance, now_ms, user["id"]),
        )
        wr = conn.execute(
            "SELECT * FROM seed_weekly_stats WHERE user_id = ? AND week_key = ?",
            (user["id"], week_key),
        ).fetchone()
        if wr:
            conn.execute(
                "UPDATE seed_weekly_stats SET seconds = seconds + ?, points = points + ? WHERE user_id = ? AND week_key = ?",
                (delta, points, user["id"], week_key),
            )
        else:
            conn.execute(
                "INSERT INTO seed_weekly_stats (user_id, week_key, seconds, points) VALUES (?, ?, ?, ?)",
                (user["id"], week_key, delta, points),
            )
        if points > 0:
            ref = f"seed:{user['id']}:{now_ms}:{after}"
            try:
                conn.execute(
                    "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, ?, ?, 'seeding', ?)",
                    (user["id"], ref, points, new_balance, now_ms),
                )
            except Exception:
                pass
            awards.append({"discord_id": user["discord_id"], "points": points, "balance": new_balance})
    conn.commit()
    return awards


def _incremental_points(before: int, after: int) -> int:
    def for_seconds(seconds: int) -> int:
        safe = max(0, int(seconds))
        first = min(safe, 3600)
        later = max(0, safe - 3600)
        return first // 360 + later // 180

    return for_seconds(after) - for_seconds(before)


def seed_get_leaderboard(week_key: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT u.*, w.seconds AS weekly_seconds, w.points AS weekly_points
        FROM seed_weekly_stats w
        JOIN seed_users u ON u.id = w.user_id
        WHERE w.week_key = ? AND u.linked = 1 AND w.seconds > 0
        ORDER BY w.seconds DESC, u.display_name COLLATE NOCASE
        """,
        (week_key,),
    ).fetchall()
    return [dict(r) for r in rows]


def seed_list_seeders(week_key: str) -> list[dict]:
    weekly = {r["id"]: r for r in seed_get_leaderboard(week_key)}
    out = []
    for user in seed_list_users():
        w = weekly.get(user["id"], {})
        member = seed_get_vip_membership(user["id"])
        out.append({
            **user,
            "weekly_seconds": w.get("weekly_seconds", 0),
            "weekly_points": w.get("weekly_points", 0),
            "vip": member,
        })
    out.sort(
        key=lambda r: (
            -r["weekly_seconds"],
            -r["total_seconds"],
            r["display_name"].lower(),
        )
    )
    return out


def seed_adjust_points(discord_id: str, delta: int, reason: str, actor_id: str, idempotency_key: str) -> dict:
    if not isinstance(delta, int) or delta == 0 or abs(delta) > 1_000_000:
        raise SeedDomainError("bad_amount", "Сумма должна быть целым числом от -1000000 до 1000000, кроме нуля.")
    conn = get_conn()
    now = _now_ms()
    user = seed_get_user_by_discord(discord_id)
    if not user:
        raise SeedDomainError("not_linked", "У пользователя нет привязанного аккаунта.")
    ref = f"admin:{idempotency_key}"
    if conn.execute("SELECT id FROM seed_point_ledger WHERE ref = ?", (ref,)).fetchone():
        return user
    if user["balance"] + delta < 0:
        raise SeedDomainError("negative_balance", "Баланс не может стать отрицательным.")
    new_balance = user["balance"] + delta
    conn.execute("UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?", (new_balance, now, user["id"]))
    conn.execute(
        "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], ref, delta, new_balance, f"admin:{reason[:100]}", now),
    )
    seed_audit("points_adjusted", actor_id, {"user_id": user["id"], "delta": delta, "reason": reason[:100]})
    conn.commit()
    return {**user, "balance": new_balance}


def seed_grant_points_all(amount: int, reason: str, actor_id: str, idempotency_key: str) -> dict:
    if not isinstance(amount, int) or amount < 1 or amount > 1_000_000:
        raise SeedDomainError("bad_amount", "Начисление всем сидерам: от 1 до 1000000 баллов.")
    note = str(reason or "").replace("\n", " ").replace("\r", " ").strip()[:100]
    if len(note) < 3 or not idempotency_key or not actor_id:
        raise SeedDomainError("bad_reason", "Укажите причину не короче трёх символов.")
    conn = get_conn()
    now = _now_ms()
    marker = f"admin-bulk-points:{idempotency_key}"
    prev = seed_get_state(marker)
    if prev:
        if prev.get("kind") != "points":
            raise SeedDomainError("duplicate_command", "ID команды уже использован.")
        return {**prev, "applied": False}
    users = seed_list_users()
    for user in users:
        new_balance = user["balance"] + amount
        conn.execute("UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?", (new_balance, now, user["id"]))
        conn.execute(
            "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], f"admin-bulk-points:{idempotency_key}:{user['id']}", amount, new_balance, f"admin_all:{note}", now),
        )
    result = {"kind": "points", "count": len(users), "amount": amount, "total_points": len(users) * amount}
    seed_audit("points_granted_all", actor_id, {**result, "reason": note})
    seed_set_state(marker, result)
    conn.commit()
    return {**result, "applied": True}


def _add_time_for_user(user: dict, minutes: int, note: str, ref: str, week_key: str, now: int) -> int:
    conn = get_conn()
    wr = conn.execute(
        "SELECT * FROM seed_weekly_stats WHERE user_id = ? AND week_key = ?",
        (user["id"], week_key),
    ).fetchone()
    before_manual = int(wr["manual_seconds"]) if wr else 0
    seconds = minutes * 60
    points = _incremental_points(before_manual, before_manual + seconds)
    if wr:
        conn.execute(
            "UPDATE seed_weekly_stats SET manual_seconds = ?, seconds = seconds + ?, points = points + ? WHERE user_id = ? AND week_key = ?",
            (before_manual + seconds, seconds, points, user["id"], week_key),
        )
    else:
        conn.execute(
            "INSERT INTO seed_weekly_stats (user_id, week_key, seconds, points, manual_seconds) VALUES (?, ?, ?, ?, ?)",
            (user["id"], week_key, seconds, points, seconds),
        )
    new_total = user["total_seconds"] + seconds
    new_lifetime = user["lifetime_points"] + points
    new_balance = user["balance"] + points
    conn.execute(
        "UPDATE seed_users SET total_seconds = ?, lifetime_points = ?, balance = ?, updated_at = ? WHERE id = ?",
        (new_total, new_lifetime, new_balance, now, user["id"]),
    )
    try:
        conn.execute(
            "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], ref, points, new_balance, f"manual_time:{note}", now),
        )
    except Exception:
        pass
    return points


def seed_add_time(discord_id: str, minutes: int, reason: str, actor_id: str, idempotency_key: str) -> dict:
    if not isinstance(minutes, int) or minutes < 1 or minutes > 1440:
        raise SeedDomainError("bad_minutes", "Добавьте от 1 до 1440 минут за одну операцию.")
    note = str(reason or "").replace("\n", " ").replace("\r", " ").strip()[:100]
    if len(note) < 3 or not idempotency_key or not actor_id:
        raise SeedDomainError("bad_reason", "Укажите причину не короче трёх символов.")
    conn = get_conn()
    now = _now_ms()
    user = seed_get_user_by_discord(discord_id)
    if not user:
        raise SeedDomainError("not_linked", "У пользователя нет подтверждённой привязки сидера.")
    week_key = seed_week_key(now)
    ref = f"admin-time:{idempotency_key}"
    if conn.execute("SELECT id FROM seed_point_ledger WHERE ref = ?", (ref,)).fetchone():
        return {"applied": False, "user": user, "minutes": 0, "points": 0, "week_key": week_key}
    points = _add_time_for_user(user, minutes, note, ref, week_key, now)
    seed_audit("seeding_time_added", actor_id, {"user_id": user["id"], "minutes": minutes, "points": points, "week_key": week_key, "reason": note})
    conn.commit()
    return {"applied": True, "user": user, "minutes": minutes, "points": points, "week_key": week_key}


def seed_add_time_all(minutes: int, reason: str, actor_id: str, idempotency_key: str) -> dict:
    if not isinstance(minutes, int) or minutes < 1 or minutes > 1440:
        raise SeedDomainError("bad_minutes", "Добавьте от 1 до 1440 минут за одну операцию.")
    note = str(reason or "").replace("\n", " ").replace("\r", " ").strip()[:100]
    if len(note) < 3 or not idempotency_key or not actor_id:
        raise SeedDomainError("bad_reason", "Укажите причину не короче трёх символов.")
    conn = get_conn()
    now = _now_ms()
    marker = f"admin-bulk-time:{idempotency_key}"
    prev = seed_get_state(marker)
    if prev:
        if prev.get("kind") != "time":
            raise SeedDomainError("duplicate_command", "ID команды уже использован.")
        return {**prev, "applied": False}
    users = seed_list_users()
    week_key = seed_week_key(now)
    total_points = 0
    for user in users:
        total_points += _add_time_for_user(user, minutes, note, f"admin-bulk-time:{idempotency_key}:{user['id']}", week_key, now)
    result = {"kind": "time", "count": len(users), "minutes": minutes, "total_points": total_points, "week_key": week_key}
    seed_audit("seeding_time_added_all", actor_id, {**result, "reason": note})
    seed_set_state(marker, result)
    conn.commit()
    return {**result, "applied": True}


def seed_get_vip_membership(user_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM seed_vip_memberships WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def seed_list_vip_memberships() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM seed_vip_memberships").fetchall()]


def seed_purchase_vip(discord_id: str, sku: str, cost: int, duration_seconds: int, idempotency_key: str) -> dict:
    conn = get_conn()
    now = _now_ms()
    prior = conn.execute("SELECT * FROM seed_vip_orders WHERE id = ?", (f"vip:{idempotency_key}",)).fetchone()
    if prior:
        order = dict(prior)
        pu = seed_get_user_by_id(order["user_id"])
        order["balance"] = pu["balance"] if pu else 0
        return order
    user = seed_get_user_by_discord(discord_id)
    if not user:
        raise SeedDomainError("not_linked", "Сначала привяжите Steam через /seed link.")
    if user["balance"] < cost:
        raise SeedDomainError("insufficient_points", f"Недостаточно баллов: нужно {cost}, доступно {user['balance']}.")
    new_balance = user["balance"] - cost
    conn.execute("UPDATE seed_users SET balance = ?, updated_at = ? WHERE id = ?", (new_balance, now, user["id"]))
    order_id = _uuid.uuid4().hex
    conn.execute(
        "INSERT INTO seed_point_ledger (user_id, ref, delta, balance_after, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], f"vip:{order_id}", -cost, new_balance, f"vip_purchase:{sku}", now),
    )
    member = seed_get_vip_membership(user["id"])
    if not member:
        state = "pending_add"
        queued = duration_seconds
        active_until = None
        pending_since = now
    elif member["state"] == "active" and (member["active_until"] or 0) > now:
        state = "active"
        active_until = member["active_until"] + duration_seconds * 1000
        queued = 0
        pending_since = None
    elif member["state"] == "pending_remove":
        state = "pending_add"
        queued = (member["queued_seconds"] or 0) + duration_seconds
        active_until = None
        pending_since = now
    else:
        state = "pending_add"
        queued = (member["queued_seconds"] or 0) + duration_seconds
        active_until = member.get("active_until")
        pending_since = member.get("pending_since") or now
    conn.execute(
        """
        INSERT INTO seed_vip_memberships (user_id, state, queued_seconds, active_until, managed_by_bot, pending_since, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, '', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            state=excluded.state, queued_seconds=excluded.queued_seconds, active_until=excluded.active_until,
            pending_since=excluded.pending_since, last_error='', updated_at=excluded.updated_at
        """,
        (user["id"], state, queued, active_until, None, pending_since, now),
    )
    order_state = "active" if state == "active" else "queued"
    conn.execute(
        "INSERT INTO seed_vip_orders (id, user_id, sku, cost, duration_seconds, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"vip:{idempotency_key}", user["id"], sku, cost, duration_seconds, order_state, now),
    )
    seed_audit("vip_purchased", discord_id, {"order_id": order_id, "user_id": user["id"], "sku": sku, "cost": cost})
    conn.commit()
    return {
        "id": order_id,
        "user_id": user["id"],
        "sku": sku,
        "cost": cost,
        "duration_seconds": duration_seconds,
        "state": order_state,
        "balance": new_balance,
        "membership_state": state,
    }


def seed_set_vip_ownership(user_id: str, managed_by_bot: bool):
    conn = get_conn()
    now = _now_ms()
    conn.execute(
        "UPDATE seed_vip_memberships SET managed_by_bot = ?, last_error = '', updated_at = ? WHERE user_id = ?",
        (1 if managed_by_bot else 0, now, user_id),
    )
    seed_audit("vip_ownership_classified", "", {"user_id": user_id, "managed_by_bot": managed_by_bot})
    conn.commit()


def seed_activate_vip(user_id: str) -> dict | None:
    conn = get_conn()
    now = _now_ms()
    member = seed_get_vip_membership(user_id)
    if not member or member["state"] != "pending_add" or (member["queued_seconds"] or 0) <= 0:
        return member
    active_until = now + int(member["queued_seconds"]) * 1000
    conn.execute(
        "UPDATE seed_vip_memberships SET state = 'active', active_until = ?, queued_seconds = 0, pending_since = NULL, last_error = '', updated_at = ? WHERE user_id = ?",
        (active_until, now, user_id),
    )
    conn.execute(
        "UPDATE seed_vip_orders SET state = 'active' WHERE user_id = ? AND state = 'queued'",
        (user_id,),
    )
    seed_audit("vip_activated", "", {"user_id": user_id, "active_until": active_until})
    conn.commit()
    return {**member, "state": "active", "active_until": active_until, "queued_seconds": 0}


def seed_set_vip_state(user_id: str, state: str, error: str = "") -> dict | None:
    allowed = {"pending_add", "active", "pending_remove", "inactive"}
    if state not in allowed:
        raise ValueError("Invalid VIP state")
    conn = get_conn()
    now = _now_ms()
    member = seed_get_vip_membership(user_id)
    if not member:
        return None
    pending_since = (member.get("pending_since") or now) if state.startswith("pending_") else None
    active_until = None if state == "inactive" else member.get("active_until")
    managed = None if state == "inactive" else member.get("managed_by_bot")
    conn.execute(
        "UPDATE seed_vip_memberships SET state = ?, pending_since = ?, active_until = ?, managed_by_bot = ?, last_error = ?, updated_at = ? WHERE user_id = ?",
        (state, pending_since, active_until, managed, error[:160], now, user_id),
    )
    if state == "inactive":
        conn.execute("UPDATE seed_vip_orders SET state = 'expired' WHERE user_id = ? AND state = 'active'", (user_id,))
        seed_audit("vip_expired", "", {"user_id": user_id})
    conn.commit()
    return {**member, "state": state, "active_until": active_until, "managed_by_bot": managed, "last_error": error[:160]}


def seed_set_vip_error(user_id: str, error: str):
    conn = get_conn()
    now = _now_ms()
    conn.execute(
        "UPDATE seed_vip_memberships SET last_error = ?, updated_at = ? WHERE user_id = ?",
        (error[:160], now, user_id),
    )
    conn.commit()


def seed_report_published(week_key: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM seed_weekly_reports WHERE week_key = ?", (week_key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = _json.loads(d.get("payload") or "{}")
    return d


def seed_mark_report_published(week_key: str, message_ids: list):
    conn = get_conn()
    now = _now_ms()
    conn.execute(
        "INSERT INTO seed_weekly_reports (week_key, payload, published_at) VALUES (?, ?, ?) ON CONFLICT(week_key) DO UPDATE SET payload=excluded.payload, published_at=excluded.published_at",
        (week_key, _json.dumps({"message_ids": message_ids}), now),
    )
    seed_audit("weekly_report_published", "", {"week_key": week_key, "message_count": len(message_ids)})
    conn.commit()


def seed_get_state(key: str, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM seed_system_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return _json.loads(row["value"])
    except Exception:
        return default


def seed_set_state(key: str, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO seed_system_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, _json.dumps(value)),
    )
    conn.commit()


def seed_audit(kind: str, actor_id: str, details: dict):
    conn = get_conn()
    now = _now_ms()
    conn.execute(
        "INSERT INTO seed_audit_log (kind, actor_id, details, created_at) VALUES (?, ?, ?, ?)",
        (kind, str(actor_id or ""), _json.dumps(details, ensure_ascii=False, default=str), now),
    )


def seed_log_swap(server_id: str, steam_id: str, source_faction: str, target_faction: str,
                  diff: int, player_count: int, faction_counts: dict, is_vip: bool,
                  actor_discord_id: str = "", map_name: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO sw_log (server_id, steam_id, source_faction, target_faction, diff, player_count, faction_counts, is_vip, actor_discord_id, map, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (server_id, steam_id, source_faction, target_faction, diff, player_count,
         _json.dumps(faction_counts, ensure_ascii=False), 1 if is_vip else 0,
         actor_discord_id, map_name, int(_time.time() * 1000)),
    )


def seed_list_sw_logs(since_ms: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sw_log WHERE created_at >= ? ORDER BY created_at DESC", (since_ms,)).fetchall()
    return [dict(r) for r in rows]


def seed_week_key(now_ms: int) -> str:
    """Ключ недели (понедельник, МСК) для timestamp в миллисекундах."""
    from datetime import datetime, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    dt = datetime.fromtimestamp(now_ms / 1000, tz=msk)
    days_from_monday = (dt.weekday()) % 7
    monday = (dt - timedelta(days=days_from_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.strftime("%Y-%m-%d")


def seed_previous_week_key(now_ms: int) -> str:
    from datetime import datetime, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    dt = datetime.fromtimestamp(now_ms / 1000, tz=msk)
    days_from_monday = dt.weekday() % 7
    monday = (dt - timedelta(days=days_from_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
    prev = monday - timedelta(days=7)
    return prev.strftime("%Y-%m-%d")


def seed_is_inside_window(now_ms: int, start_minutes: int, end_minutes: int) -> bool:
    from datetime import datetime, timedelta, timezone

    msk = timezone(timedelta(hours=3))
    dt = datetime.fromtimestamp(now_ms / 1000, tz=msk)
    current = dt.hour * 60 + dt.minute
    return start_minutes <= current < end_minutes
