"""Привязки, сессии, OAuth-state, идемпотентность, права ролей."""

import hashlib
import json
import secrets
from datetime import timedelta

try:
    from .. import config
except ImportError:
    import config

try:
    from .core import _conn, _now, _postgres_sql
except ImportError:
    from core import _conn, _now, _postgres_sql


async def list_staff_users():
    async with _conn() as db:
        cur = await db.execute(
            "SELECT u.steam_id, u.persona, u.last_seen_utc, "
            "b.discord_id, b.discord_name, b.roles, b.updated_utc "
            "FROM users u JOIN bindings b ON b.user_id = u.steam_id "
            "WHERE b.guild_member = 1 ORDER BY COALESCE(u.persona, ''), u.steam_id"
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            roles = json.loads(row[5] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            roles = []
        result.append(
            {
                "steam_id": row[0],
                "persona": row[1] or "",
                "last_seen_utc": row[2],
                "discord_id": row[3],
                "discord_name": row[4] or "",
                "role_ids": [str(role)[:64] for role in roles[:100] if str(role).strip()],
                "roles_updated_utc": row[6],
            }
        )
    return result


async def list_bindings():
    async with _conn() as db:
        cur = await db.execute(
            "SELECT b.user_id, COALESCE(u.persona, ''), b.discord_id, b.discord_name, "
            "b.guild_member, b.roles, b.updated_utc FROM bindings b "
            "LEFT JOIN users u ON u.steam_id = b.user_id ORDER BY b.updated_utc DESC"
        )
        rows = await cur.fetchall()
    result = []
    for row in rows:
        try:
            roles = json.loads(row[5] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            roles = []
        result.append({
            "steam_id": row[0], "persona": row[1] or "", "discord_id": row[2],
            "discord_name": row[3] or "", "guild_member": bool(row[4]),
            "role_ids": roles if isinstance(roles, list) else [], "updated_utc": row[6],
        })
    return result


async def upsert_binding(user_id, discord_id, discord_name, member_roles, guild_member):
    roles_json = json.dumps(
        [str(role)[:64] for role in (member_roles or [])[:100] if str(role).strip()],
        ensure_ascii=False,
    )
    async with _conn() as db:
        await db.execute(
            "INSERT INTO bindings (user_id, discord_id, discord_name, guild_member, roles, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET discord_id=excluded.discord_id, "
            "discord_name=excluded.discord_name, guild_member=excluded.guild_member, "
            "roles=excluded.roles, updated_utc=excluded.updated_utc",
            (user_id, discord_id, discord_name, 1 if guild_member else 0, roles_json, _now()),
        )
        await db.commit()


async def get_binding(user_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT user_id, discord_id, discord_name, guild_member, roles, updated_utc "
            "FROM bindings WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        roles = json.loads(row[4] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        roles = []
    if not isinstance(roles, list):
        roles = []
    return {
        "user_id": row[0],
        "discord_id": row[1],
        "discord_name": row[2],
        "guild_member": bool(row[3]),
        "roles": [str(role)[:64] for role in roles[:100] if str(role).strip()],
        "updated_utc": row[5],
    }


async def get_steam_by_discord(discord_id):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT user_id FROM bindings WHERE discord_id = ?", (discord_id,)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def delete_binding(user_id):
    async with _conn() as db:
        await db.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        await db.commit()


async def create_oauth_state(state, provider, user_id=None):
    now = _now()
    async with _conn() as db:
        await db.execute(
            "INSERT INTO oauth_states (state, provider, user_id, created_utc, expires_utc) VALUES (?, ?, ?, ?, ?)",
            (state, str(provider)[:32], user_id, now, now + 600),
        )
        await db.commit()


async def consume_oauth_state(state, provider, user_id=None):
    if not state or not provider:
        return False
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        lock_clause = " FOR UPDATE" if config.DATABASE_URL else ""
        cur = await db.execute(
            "SELECT user_id, expires_utc FROM oauth_states "
            "WHERE state = ? AND provider = ?" + lock_clause,
            (state, provider),
        )
        row = await cur.fetchone()
        await db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        await db.commit()
    if not row or row[1] < _now():
        return False
    return row[0] == user_id


async def claim_idempotency(actor_steam_id, idem_key, fingerprint, ttl=600):
    now = _now()
    async with _conn() as db:
        await db.execute("BEGIN IMMEDIATE")
        if config.DATABASE_URL:
            inserted = await db.connection.fetch(
                _postgres_sql(
                    "INSERT INTO idempotency_keys "
                    "(actor_steam_id, idem_key, fingerprint, created_utc, expires_utc) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(actor_steam_id, idem_key) DO NOTHING "
                    "RETURNING idem_key"
                ),
                actor_steam_id, idem_key, fingerprint, now, now + ttl,
            )
            if inserted:
                await db.commit()
                return {"kind": "claimed"}
            updated = await db.connection.execute(
                _postgres_sql(
                    "UPDATE idempotency_keys SET fingerprint = ?, created_utc = ?, "
                    "expires_utc = ?, status_code = NULL, response_json = NULL "
                    "WHERE actor_steam_id = ? AND idem_key = ? AND expires_utc < ?"
                ),
                fingerprint, now, now + ttl, actor_steam_id, idem_key, now,
            )
            if not str(updated).rstrip().endswith(" 0"):
                await db.commit()
                return {"kind": "claimed"}
            cur = await db.execute(
                "SELECT fingerprint, status_code, response_json, expires_utc "
                "FROM idempotency_keys WHERE actor_steam_id = ? AND idem_key = ?",
                (actor_steam_id, idem_key),
            )
            row = await cur.fetchone()
            await db.commit()
            if row and row[0] != fingerprint:
                return {"kind": "conflict"}
            if not row or row[2] is None:
                return {"kind": "in_progress"}
            try:
                response = json.loads(row[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"kind": "in_progress"}
            return {"kind": "replay", "status_code": int(row[1] or 200), "response": response}
        cur = await db.execute(
            "SELECT fingerprint, status_code, response_json, expires_utc "
            "FROM idempotency_keys WHERE actor_steam_id = ? AND idem_key = ?",
            (actor_steam_id, idem_key),
        )
        row = await cur.fetchone()
        if row and row[3] >= now:
            await db.commit()
            if row[0] != fingerprint:
                return {"kind": "conflict"}
            if row[2] is None:
                return {"kind": "in_progress"}
            try:
                response = json.loads(row[2])
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"kind": "in_progress"}
            return {"kind": "replay", "status_code": int(row[1] or 200), "response": response}
        await db.execute(
            "INSERT INTO idempotency_keys "
            "(actor_steam_id, idem_key, fingerprint, created_utc, expires_utc) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(actor_steam_id, idem_key) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, created_utc=excluded.created_utc, "
            "expires_utc=excluded.expires_utc, status_code=NULL, response_json=NULL",
            (actor_steam_id, idem_key, fingerprint, now, now + ttl),
        )
        await db.commit()
    return {"kind": "claimed"}


async def complete_idempotency(actor_steam_id, idem_key, status_code, response):
    try:
        serialized = json.dumps(response, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = json.dumps({"ok": True, "replayed": True}, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 256 * 1024:
        serialized = json.dumps(
            {"ok": True, "replayed": True, "response_truncated": True},
            ensure_ascii=False,
        )
    async with _conn() as db:
        await db.execute(
            "UPDATE idempotency_keys SET status_code = ?, response_json = ? "
            "WHERE actor_steam_id = ? AND idem_key = ?",
            (int(status_code), serialized, actor_steam_id, idem_key),
        )
        await db.commit()


async def abort_idempotency(actor_steam_id, idem_key):
    async with _conn() as db:
        await db.execute(
            "DELETE FROM idempotency_keys WHERE actor_steam_id = ? AND idem_key = ?",
            (actor_steam_id, idem_key),
        )
        await db.commit()


def new_session_token():
    return secrets.token_urlsafe(32)


def _session_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_session(steam_id):
    token = new_session_token()
    now = _now()
    expires = now + timedelta(days=config.SESSION_TTL_DAYS).total_seconds()
    async with _conn() as db:
        cur = await db.execute("SELECT session_version FROM users WHERE steam_id = ?", (steam_id,))
        row = await cur.fetchone()
        session_version = int(row[0] if row else 0)
        await db.execute(
            "INSERT INTO sessions (token, user_id, created_utc, expires_utc, session_version) VALUES (?, ?, ?, ?, ?)",
            (_session_key(token), steam_id, now, expires, session_version),
        )
        await db.commit()
    return token


async def get_session(token):
    if not token or len(token) > 512:
        return None
    async with _conn() as db:
        cur = await db.execute(
            "SELECT s.user_id, s.expires_utc, s.session_version, COALESCE(u.session_version, 0) "
            "FROM sessions s LEFT JOIN users u ON u.steam_id = s.user_id WHERE s.token = ?",
            (_session_key(token),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        user_id, expires, session_version, current_version = row
        if expires < _now():
            await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
            await db.commit()
            return None
        if int(session_version) != int(current_version):
            await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
            await db.commit()
            return None
    return user_id


async def delete_session(token):
    if not token:
        return
    async with _conn() as db:
        await db.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
        await db.commit()


async def revoke_user_sessions(user_id):
    async with _conn() as db:
        await db.execute(
            "UPDATE users SET session_version = session_version + 1 WHERE steam_id = ?",
            (user_id,),
        )
        await db.commit()


async def prune_sessions():
    async with _conn() as db:
        await db.execute("DELETE FROM sessions WHERE expires_utc < ?", (_now(),))
        await db.execute("DELETE FROM oauth_states WHERE expires_utc < ?", (_now(),))
        await db.execute("DELETE FROM idempotency_keys WHERE expires_utc < ?", (_now(),))
        await db.commit()


async def get_role_perms(role_ref):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT perms FROM role_permissions WHERE role_ref = ?", (role_ref,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


async def get_role_perms_all():
    async with _conn() as db:
        cur = await db.execute("SELECT role_ref, perms FROM role_permissions")
        rows = await cur.fetchall()
    result = {}
    for role_ref, raw_perms in rows:
        try:
            perms = json.loads(raw_perms or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            perms = []
        result[role_ref] = perms if isinstance(perms, list) else []
    return result


async def set_role_perms(role_ref, perms):
    async with _conn() as db:
        await db.execute(
            "INSERT INTO role_permissions (role_ref, perms, updated_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(role_ref) DO UPDATE SET perms = excluded.perms, updated_utc = excluded.updated_utc",
            (role_ref, json.dumps(perms or [], ensure_ascii=False), _now()),
        )
        await db.commit()
