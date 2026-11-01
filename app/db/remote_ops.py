"""Очередь повторов для неудавшихся записей на игровые серверы (баны, снятие банов)."""

import json
import secrets

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


async def enqueue_remote_op(kind, server_id, steam_id, payload=None):
    """Ставит операцию в очередь. Для пары (сервер, игрок) остаётся последнее намерение.

    Возвращает updated_utc записи: это токен для compare-and-set в delete/reschedule.
    Запись сохраняет id исходной строки при конфликте, поэтому токен и нужен,
    чтобы отличить старое намерение от нового.
    """
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
    return now


async def list_due_remote_ops(limit=50):
    now = _now()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT id, kind, server_id, steam_id, payload, attempts, last_error, updated_utc FROM remote_ops "
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
            "updated_utc": r[7],
        })
    return out


async def delete_remote_op(op_id, expected_updated_utc=None):
    """Удаляет операцию. С expected_updated_utc удаляет только если запись не менялась
    (compare-and-set): свежее намерение, поставленное во время выполнения, не теряется.
    Возвращает True, если строка удалена."""
    async with _conn() as db:
        if expected_updated_utc is None:
            cur = await db.execute("DELETE FROM remote_ops WHERE id = ?", (op_id,))
        else:
            cur = await db.execute(
                "DELETE FROM remote_ops WHERE id = ? AND updated_utc = ?",
                (op_id, expected_updated_utc),
            )
        removed = cur.rowcount > 0
        await db.commit()
    return removed


async def reschedule_remote_op(op_id, attempts, error, delay_seconds, expected_updated_utc=None):
    """Откладывает повтор. С expected_updated_utc обновляет только неизменённую запись
    (compare-and-set). Возвращает True, если строка обновлена."""
    now = _now()
    async with _conn() as db:
        if expected_updated_utc is None:
            cur = await db.execute(
                "UPDATE remote_ops SET attempts = ?, last_error = ?, next_try = ?, updated_utc = ? WHERE id = ?",
                (attempts, (error or "")[:500], now + delay_seconds, now, op_id),
            )
        else:
            cur = await db.execute(
                "UPDATE remote_ops SET attempts = ?, last_error = ?, next_try = ?, updated_utc = ? "
                "WHERE id = ? AND updated_utc = ?",
                (attempts, (error or "")[:500], now + delay_seconds, now, op_id, expected_updated_utc),
            )
        changed = cur.rowcount > 0
        await db.commit()
    return changed
