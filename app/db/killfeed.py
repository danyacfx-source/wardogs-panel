"""Килфид: приём убийств от игрового сервера и чтение ленты."""

import json

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


async def record_kill_events(server_id, events):
    """Вставить новые убийства (dedup по event_id). Возвращает число записанных."""
    if not events:
        return 0
    now = _now()
    ids = list(dict.fromkeys(e["event_id"] for e in events))
    written = 0
    async with _conn() as db:
        placeholders = ", ".join("?" for _ in ids)
        cur = await db.execute(
            f"SELECT event_id FROM killfeed_events "
            f"WHERE server_id = ? AND event_id IN ({placeholders})",
            (server_id, *ids),
        )
        seen = {row[0] for row in await cur.fetchall()}
        for e in events:
            if e["event_id"] in seen:
                continue
            seen.add(e["event_id"])
            await db.execute(
                "INSERT INTO killfeed_events "
                "(server_id, event_id, received_utc, match_id, event_time, map, "
                "killer_id, killer_name, killer_faction, victim_id, victim_name, victim_faction, "
                "cause, distance_m, headshot, suicide, teamkill, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(server_id, event_id) DO NOTHING",
                (
                    server_id, e["event_id"], now, e.get("match_id") or "",
                    e.get("event_time") or now, e.get("map") or "",
                    e.get("killer_id"), e.get("killer_name"), e.get("killer_faction"),
                    e["victim_id"], e.get("victim_name") or "", e.get("victim_faction"),
                    e.get("cause") or "", e.get("distance_m"),
                    1 if e.get("headshot") else 0, 1 if e.get("suicide") else 0,
                    1 if e.get("teamkill") else 0,
                    json.dumps(e.get("tags") or [], ensure_ascii=False),
                ),
            )
            written += 1
        await db.commit()
    return written


async def get_known_factions(server_id, steam_ids):
    """Последние известные фракции игроков (из player_activity)."""
    ids = [sid for sid in steam_ids if sid]
    if not ids:
        return {}
    async with _conn() as db:
        placeholders = ", ".join("?" for _ in ids)
        cur = await db.execute(
            f"SELECT steam_id, faction FROM player_activity "
            f"WHERE server_id = ? AND steam_id IN ({placeholders})",
            (server_id, *ids),
        )
        rows = await cur.fetchall()
    return {row[0]: (row[1] or None) for row in rows}


async def recent_kills(server_id, limit=200, kind="all"):
    """Новые убийства сервера; kind: all | teamkills | headshots | suicides | environment."""
    limit = max(1, min(int(limit or 200), 1000))
    where = "server_id = ?"
    params = [server_id]
    if kind == "teamkills":
        where += " AND teamkill = 1"
    elif kind == "headshots":
        where += " AND headshot = 1"
    elif kind == "suicides":
        where += " AND suicide = 1"
    elif kind == "environment":
        where += " AND killer_id IS NULL AND suicide = 0"
    async with _conn() as db:
        cur = await db.execute(
            "SELECT event_id, received_utc, match_id, event_time, map, killer_id, killer_name, "
            "killer_faction, victim_id, victim_name, victim_faction, cause, distance_m, "
            "headshot, suicide, teamkill, tags "
            f"FROM killfeed_events WHERE {where} ORDER BY received_utc DESC, event_time DESC LIMIT ?",
            (*params, limit),
        )
        rows = await cur.fetchall()
    out = []
    for r in rows:
        try:
            tags = json.loads(r[16] or "[]")
        except ValueError:
            tags = []
        out.append({
            "event_id": r[0],
            "received_utc": r[1],
            "match_id": r[2] or "",
            "event_time": r[3],
            "map": r[4] or "",
            "killer_id": r[5],
            "killer_name": r[6],
            "killer_faction": r[7],
            "victim_id": r[8],
            "victim_name": r[9],
            "victim_faction": r[10],
            "cause": r[11] or "",
            "distance_m": r[12],
            "headshot": bool(r[13]),
            "suicide": bool(r[14]),
            "teamkill": bool(r[15]),
            "tags": tags if isinstance(tags, list) else [],
        })
    return out


async def killfeed_stats(server_id):
    """Счётчики ленты сервера: всего, союзных, hs, последний пакет."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(teamkill), 0), COALESCE(SUM(headshot), 0), "
            "COALESCE(SUM(suicide), 0), MAX(received_utc) "
            "FROM killfeed_events WHERE server_id = ?",
            (server_id,),
        )
        row = await cur.fetchone()
    return {
        "total": int(row[0] or 0),
        "teamkills": int(row[1] or 0),
        "headshots": int(row[2] or 0),
        "suicides": int(row[3] or 0),
        "last_received_utc": row[4],
    }
