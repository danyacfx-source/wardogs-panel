"""РњРѕРЅРёС‚РѕСЂРёРЅРі РѕРЅР»Р°Р№РЅР°, Р°РєС‚РёРІРЅРѕСЃС‚СЊ РёРіСЂРѕРєРѕРІ, Р»РёРґРµСЂР±РѕСЂРґ."""

try:
    from .. import config
except ImportError:
    import config

try:
    from . import core
except ImportError:
    import core


async def record_monitoring(server_id, current, maximum, players):
    now = core._now()
    bucket = int(now // 60) * 60
    async with core._conn() as db:
        await db.execute(
            "INSERT INTO population_samples (server_id, bucket_utc, players, max_players) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(server_id, bucket_utc) DO UPDATE SET players=excluded.players, max_players=excluded.max_players",
            (server_id, bucket, int(current or 0), int(maximum or 0)),
        )
        presence_cur = await db.execute(
            "SELECT steam_id, total_seconds, session_count, current_session_started_utc, "
            "last_seen_utc, online FROM player_presence WHERE server_id = ?",
            (server_id,),
        )
        previous_presence = {
            str(row[0]): {
                "total_seconds": float(row[1] or 0),
                "session_count": int(row[2] or 0),
                "session_started": row[3],
                "last_seen": float(row[4] or 0),
                "online": bool(row[5]),
            }
            for row in await presence_cur.fetchall()
        }
        await db.execute(
            "UPDATE player_presence SET online = 0 WHERE server_id = ?",
            (server_id,),
        )
        for player in (players or [])[:1000]:
            if not isinstance(player, dict):
                continue
            steam_id = str(player.get("steamId") or "").strip()
            if not steam_id:
                continue
            previous = previous_presence.get(steam_id)
            continuous = bool(
                previous
                and previous["online"]
                and 0 <= now - previous["last_seen"] <= 180
            )
            total_seconds = previous["total_seconds"] if previous else 0.0
            session_count = previous["session_count"] if previous else 0
            session_started = previous["session_started"] if continuous else now
            if continuous:
                total_seconds += min(120.0, max(0.0, now - previous["last_seen"]))
            else:
                session_count += 1
            await db.execute(
                "INSERT INTO player_presence "
                "(server_id, steam_id, total_seconds, session_count, "
                "current_session_started_utc, last_seen_utc, online) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
                "total_seconds=excluded.total_seconds, session_count=excluded.session_count, "
                "current_session_started_utc=excluded.current_session_started_utc, "
                "last_seen_utc=excluded.last_seen_utc, online=1",
                (
                    server_id, steam_id, total_seconds, session_count,
                    session_started, now,
                ),
            )
            await db.execute(
                "INSERT INTO player_activity "
                "(server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, first_seen_utc, last_seen_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(server_id, steam_id) DO UPDATE SET "
                "name=excluded.name, kills=excluded.kills, deaths=excluded.deaths, cash=excluded.cash, "
                "ping_ms=excluded.ping_ms, faction=excluded.faction, seen_utc=excluded.seen_utc, "
                "last_seen_utc=excluded.last_seen_utc",
                (
                    server_id, steam_id, str(player.get("name") or "РРіСЂРѕРє")[:80],
                    int(player.get("kills") or 0), int(player.get("deaths") or 0),
                    int(player.get("cash") or 0), int(player.get("pingMs") or 0),
                    str(player.get("faction") or "")[:40], now, now, now,
                ),
            )
        await db.execute("DELETE FROM population_samples WHERE bucket_utc < ?", (int(now - 31 * 86400),))
        await db.execute(
            "DELETE FROM player_activity WHERE seen_utc < ?",
            (int(now - config.PLAYER_ACTIVITY_RETENTION_DAYS * 86400),),
        )
        await db.commit()


async def get_population(hours=24):
    since = int(core._now() - max(1, min(int(hours), 24 * 31)) * 3600)
    async with core._conn() as db:
        cur = await db.execute(
            "SELECT server_id, bucket_utc, players, max_players FROM population_samples "
            "WHERE bucket_utc >= ? ORDER BY bucket_utc", (since,)
        )
        rows = await cur.fetchall()
    return [
        {"server_id": r[0], "ts": r[1], "players": r[2], "max_players": r[3]}
        for r in rows
    ]


async def get_player_activity(limit=100):
    async with core._conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc "
            "FROM player_activity ORDER BY kills DESC, deaths ASC, seen_utc DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        )
        rows = await cur.fetchall()
    return [
        {"server_id": r[0], "steam_id": r[1], "name": r[2], "kills": r[3], "deaths": r[4],
         "cash": r[5], "ping_ms": r[6], "faction": r[7], "seen_utc": r[8],
         "first_seen_utc": r[9], "last_seen_utc": r[10]}
        for r in rows
    ]


async def get_player_leaderboard(limit=100, sort="kills"):
    limit = max(1, min(int(limit or 100), 200))
    sort = str(sort or "kills").lower()
    if sort not in {"kills", "kd", "playtime", "sessions", "recent"}:
        sort = "kills"
    async with core._conn() as database:
        activity_cur = await database.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity "
            "ORDER BY last_seen_utc DESC LIMIT 10000"
        )
        activity_rows = await activity_cur.fetchall()
        presence_cur = await database.execute(
            "SELECT steam_id, total_seconds, session_count, last_seen_utc, online "
            "FROM player_presence ORDER BY last_seen_utc DESC"
        )
        presence_rows = await presence_cur.fetchall()

    profiles = _aggregate_player_rows(activity_rows)
    now = core._now()
    presence = {}
    for row in presence_rows:
        steam_id = str(row[0] or "")
        if not steam_id:
            continue
        item = presence.setdefault(
            steam_id,
            {"total_seconds": 0.0, "session_count": 0, "online": False},
        )
        item["total_seconds"] += float(row[1] or 0)
        item["session_count"] += int(row[2] or 0)
        item["online"] = item["online"] or (
            bool(row[4]) and 0 <= now - float(row[3] or 0) <= 180
        )

    result = []
    for steam_id, profile in profiles.items():
        tracked = presence.get(steam_id, {})
        profile["total_seconds"] = round(float(tracked.get("total_seconds", 0)))
        profile["session_count"] = int(tracked.get("session_count", 0))
        profile["online"] = bool(tracked.get("online", False))
        result.append(profile)

    def kd_value(item):
        deaths = int(item.get("deaths") or 0)
        kills = int(item.get("kills") or 0)
        return kills / deaths if deaths else None

    sorters = {
        "kills": lambda item: (
            -int(item.get("kills") or 0),
            int(item.get("deaths") or 0),
            -float(item.get("last_seen_utc") or 0),
        ),
        "kd": lambda item: (
            1 if kd_value(item) is None else 0,
            -(kd_value(item) or 0),
            -int(item.get("kills") or 0),
            -float(item.get("last_seen_utc") or 0),
        ),
        "playtime": lambda item: (
            -int(item.get("total_seconds") or 0),
            -int(item.get("kills") or 0),
        ),
        "sessions": lambda item: (
            -int(item.get("session_count") or 0),
            -int(item.get("total_seconds") or 0),
        ),
        "recent": lambda item: (
            -float(item.get("last_seen_utc") or 0),
            str(item.get("name") or "").casefold(),
        ),
    }
    result.sort(key=sorters[sort])
    return result[:limit]


def _aggregate_player_rows(rows):
    profiles = {}
    for row in rows:
        steam_id = str(row[1] or "")
        if not steam_id:
            continue
        profile = profiles.setdefault(
            steam_id,
            {
                "steam_id": steam_id,
                "name": str(row[2] or "РРіСЂРѕРє"),
                "kills": 0,
                "deaths": 0,
                "servers": [],
                "first_seen_utc": None,
                "last_seen_utc": None,
            },
        )
        if row[2]:
            profile["name"] = str(row[2])
        profile["kills"] += int(row[3] or 0)
        profile["deaths"] += int(row[4] or 0)
        first_seen = row[9] if row[9] is not None else row[8]
        last_seen = row[10] if row[10] is not None else row[8]
        if first_seen is not None:
            profile["first_seen_utc"] = (
                first_seen
                if profile["first_seen_utc"] is None
                else min(profile["first_seen_utc"], first_seen)
            )
        if last_seen is not None:
            profile["last_seen_utc"] = (
                last_seen
                if profile["last_seen_utc"] is None
                else max(profile["last_seen_utc"], last_seen)
            )
        profile["servers"].append(
            {
                "server_id": row[0],
                "kills": int(row[3] or 0),
                "deaths": int(row[4] or 0),
                "cash": int(row[5] or 0),
                "ping_ms": int(row[6] or 0),
                "faction": str(row[7] or ""),
                "seen_utc": row[8],
            }
        )
    for profile in profiles.values():
        profile["server_count"] = len(profile["servers"])
        profile["kd"] = round(profile["kills"] / max(1, profile["deaths"]), 2)
    return profiles


async def search_players(query="", limit=50, offset=0):
    query = str(query or "").strip()[:120]
    limit = max(1, min(int(limit), 100))
    offset = max(0, min(int(offset), 10000))
    pattern = f"%{query}%"
    async with core._conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity "
            "WHERE ? = '' OR steam_id LIKE ? OR name LIKE ? "
            "ORDER BY last_seen_utc DESC, name ASC LIMIT ? OFFSET ?",
            (query, pattern, pattern, limit * 8, offset),
        )
        rows = await cur.fetchall()
    profiles = list(_aggregate_player_rows(rows).values())
    profiles.sort(key=lambda item: (item["last_seen_utc"] is None, -(item["last_seen_utc"] or 0)))
    return profiles[:limit]


async def get_player_profile(steam_id):
    steam_id = str(steam_id or "").strip()
    async with core._conn() as db:
        cur = await db.execute(
            "SELECT server_id, steam_id, name, kills, deaths, cash, ping_ms, faction, seen_utc, "
            "first_seen_utc, last_seen_utc FROM player_activity WHERE steam_id = ? "
            "ORDER BY last_seen_utc DESC",
            (steam_id,),
        )
        rows = await cur.fetchall()
    profiles = _aggregate_player_rows(rows)
    profile = profiles.get(steam_id)
    if not profile:
        return None
    presence = await get_player_presence(steam_id)
    profile.update(
        {
            "total_seconds": presence["total_seconds"],
            "session_count": presence["session_count"],
            "current_session_seconds": presence["current_session_seconds"],
        }
    )
    by_server = {item["server_id"]: item for item in presence["servers"]}
    for server in profile["servers"]:
        server_presence = by_server.get(server["server_id"], {})
        server["total_seconds"] = server_presence.get("total_seconds", 0)
        server["session_count"] = server_presence.get("session_count", 0)
        server["online"] = server_presence.get("online", False)
    return profile


async def get_player_presence(steam_id):
    now = core._now()
    async with core._conn() as db:
        cur = await db.execute(
            "SELECT server_id, total_seconds, session_count, current_session_started_utc, "
            "last_seen_utc, online FROM player_presence WHERE steam_id = ? "
            "ORDER BY last_seen_utc DESC",
            (str(steam_id or "").strip(),),
        )
        rows = await cur.fetchall()
    servers = []
    total_seconds = 0.0
    session_count = 0
    current_session_seconds = 0.0
    for row in rows:
        recent_online = bool(row[5]) and 0 <= now - float(row[4] or 0) <= 180
        server_current = (
            max(0.0, now - float(row[3]))
            if recent_online and row[3] is not None
            else 0.0
        )
        stored_total = float(row[1] or 0)
        total_seconds += stored_total
        session_count += int(row[2] or 0)
        current_session_seconds += server_current
        servers.append(
            {
                "server_id": row[0],
                "total_seconds": round(stored_total),
                "session_count": int(row[2] or 0),
                "current_session_seconds": round(server_current),
                "last_seen_utc": row[4],
                "online": recent_online,
            }
        )
    return {
        "total_seconds": round(total_seconds),
        "session_count": session_count,
        "current_session_seconds": round(current_session_seconds),
        "servers": servers,
    }

