"""Очередь повторов для неудавшихся записей на игровые серверы (баны, снятие банов)."""

import json
import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


async def enqueue_remote_op(kind, server_id, steam_id, payload=None):
    """Ставит операцию в очередь. Для пары (сервер, игрок) остаётся последнее намерение."""
    now = _now()
    body = json.dumps(payload or {}, ensure_ascii=False)
    async with _conn() as db:
        await db.execute(
            "INSERT INTO remote_ops (id, kind, server_id, steam_id, payload, attempts, next_try, last_error, created_utc, updated_utc) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, '', ?, ?) "
            "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
            "kind=excluded.kind, payload=excluded.payload, attempts=0, next_try=excluded.next_try, "
            "last_error='', updated_utc=excluded.updated_utc",
            (secrets.token_hex(12), kind, server_id, steam_id, body, now, now, now),
        )
        await db.commit()


async def list_due_remote_ops(limit=50):
    now = _now()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, kind, server_id, steam_id, payload, attempts, last_error FROM remote_ops "
            "WHERE next_try <= ? ORDER BY next_try LIMIT ?",
            (now, limit),
        )
        rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r[4] or "{}")
        except ValueError:
            payload = {}
        out.append({
            "id": r[0], "kind": r[1], "server_id": r[2], "steam_id": r[3],
            "payload": payload, "attempts": r[5], "last_error": r[6] or "",
        })
    return out


async def delete_remote_op(op_id):
    async with _conn() as db:
        await db.execute("DELETE FROM remote_ops WHERE id = ?", (op_id,))
        await db.commit()


async def reschedule_remote_op(op_id, attempts, error, delay_seconds):
    now = _now()
    async with _conn() as db:
        await db.execute(
            "UPDATE remote_ops SET attempts = ?, last_error = ?, next_try = ?, updated_utc = ? WHERE id = ?",
            (attempts, (error or "")[:500], now + delay_seconds, now, op_id),
        )
        await db.commit()
