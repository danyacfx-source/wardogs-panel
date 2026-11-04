"""Отчёты панели: статистика игроков за период, история ников, матчи и поиск по журналу."""

import json
from datetime import datetime, timedelta, timezone

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now


def _day_start(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _like(value):
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def _conditions(clauses):
    return f" WHERE {' AND '.join(clauses)}" if clauses else ""


async def player_period_stats(first_day=None, last_day=None, server_id=None):
    """Показатели игроков за период (границы включительно, даты в UTC): {steam_id: строка}."""
    since = _day_start(first_day) if first_day else None
    until = _day_start(last_day) + 86400 if last_day else None
    rows = {}

    def row(steam_id):
        return rows.setdefault(steam_id, {
            "steam_id": steam_id, "name": "", "clan_tag": "", "online_seconds": 0, "seed_seconds": 0,
            "kills": 0, "deaths": 0, "teamkills": 0, "cash": 0, "wins": 0, "losses": 0, "matches": 0,
        })

    def filters(time_column, first, last, server_column="server_id"):
        clauses, params = [], []
        if first is not None:
            clauses.append(f"{time_column} >= ?")
            params.append(first)
        if last is not None:
            clauses.append(f"{time_column} {'<' if time_column != 'day' else '<='} ?")
            params.append(last)
        if server_id:
            clauses.append(f"{server_column} = ?")
            params.append(server_id)
        return clauses, params

    async with _conn() as db:
        clauses, params = filters("day", first_day, last_day)
        cur = await db.execute(
            "SELECT steam_id, SUM(play_seconds), SUM(seed_seconds) FROM player_daily_activity"
            f"{_conditions(clauses)} GROUP BY steam_id",
            tuple(params),
        )
        for steam_id, online, seed in await cur.fetchall():
            item = row(steam_id)
            item["online_seconds"] = round(online or 0)
            item["seed_seconds"] = round(seed or 0)

        clauses, params = filters("event_time", since, until)
        cur = await db.execute(
            "SELECT killer_id, "
            "SUM(CASE WHEN suicide = 0 AND teamkill = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN teamkill = 1 THEN 1 ELSE 0 END) FROM killfeed_events"
            f"{_conditions(clauses + ['killer_id IS NOT NULL'])} GROUP BY killer_id",
            tuple(params),
        )
        for steam_id, kills, teamkills in await cur.fetchall():
            item = row(steam_id)
            item["kills"] = int(kills or 0)
            item["teamkills"] = int(teamkills or 0)
        cur = await db.execute(
            f"SELECT victim_id, COUNT(*) FROM killfeed_events{_conditions(clauses)} GROUP BY victim_id",
            tuple(params),
        )
        for steam_id, deaths in await cur.fetchall():
            row(steam_id)["deaths"] = int(deaths or 0)

        clauses, params = filters("ended_utc", since, until)
        cur = await db.execute(
            "SELECT steam_id, COALESCE(SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END), 0), COUNT(*) FROM player_match_results"
            f"{_conditions(clauses)} GROUP BY steam_id",
            tuple(params),
        )
        for steam_id, wins, losses, matches in await cur.fetchall():
            item = row(steam_id)
            item.update(wins=int(wins or 0), losses=int(losses or 0), matches=int(matches or 0))

        # Имена и деньги нужны только тем, кто попал в период: деньги — последнее значение с сервера.
        clauses, params = filters("seen_utc", None, None)
        cur = await db.execute(
            f"SELECT steam_id, name, cash FROM player_activity{_conditions(clauses)} ORDER BY seen_utc",
            tuple(params),
        )
        for steam_id, name, cash in await cur.fetchall():
            if steam_id in rows:
                rows[steam_id]["name"] = name or rows[steam_id]["name"]
                rows[steam_id]["cash"] += int(cash or 0)
        cur = await db.execute("SELECT steam_id, persona FROM users WHERE persona IS NOT NULL AND persona <> ''")
        for steam_id, persona in await cur.fetchall():
            if steam_id in rows and not rows[steam_id]["name"]:
                rows[steam_id]["name"] = persona
        cur = await db.execute(
            "SELECT m.steam_id, c.tag FROM clan_members m JOIN clans c ON c.id = m.clan_id"
        )
        for steam_id, tag in await cur.fetchall():
            if steam_id in rows:
                rows[steam_id]["clan_tag"] = tag or ""
    for item in rows.values():
        item["kd"] = round(item["kills"] / max(1, item["deaths"]), 2)
    return rows


async def list_player_names(query="", limit=50, offset=0):
    """Все ники игроков, в том числе прежние: по ним находят сменившего имя."""
    clauses, params = [], []
    if query:
        clauses.append("(LOWER(name) LIKE ? ESCAPE '\\' OR steam_id LIKE ? ESCAPE '\\')")
        params += [_like(query), _like(query)]
    where = _conditions(clauses)
    async with _conn() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM player_names{where}", tuple(params))
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            f"SELECT steam_id, name, first_seen_utc, last_seen_utc FROM player_names{where} "
            "ORDER BY last_seen_utc DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
    return {
        "total": int(total or 0),
        "items": [
            {"steam_id": row[0], "name": row[1], "first_seen_utc": row[2], "last_seen_utc": row[3]}
            for row in rows
        ],
    }


async def names_of_player(steam_id, limit=20):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT name, first_seen_utc, last_seen_utc FROM player_names WHERE steam_id = ? "
            "ORDER BY last_seen_utc DESC LIMIT ?",
            (steam_id, limit),
        )
        rows = await cur.fetchall()
    return [{"name": row[0], "first_seen_utc": row[1], "last_seen_utc": row[2]} for row in rows]


async def note_match_started(server_id, started_utc):
    """Начало матча из события фида. Повтор того же состояния не сдвигает время начала."""
    async with _conn() as db:
        await db.execute(
            "INSERT INTO server_match_state (server_id, started_utc) VALUES (?, ?) "
            "ON CONFLICT(server_id) DO NOTHING",
            (server_id, started_utc),
        )
        await db.commit()


async def current_match_starts():
    async with _conn() as db:
        cur = await db.execute("SELECT server_id, started_utc FROM server_match_state")
        return {row[0]: row[1] for row in await cur.fetchall()}


async def list_matches(server_id=None, player_query="", limit=50, offset=0):
    clauses, params = [], []
    if server_id:
        clauses.append("m.server_id = ?")
        params.append(server_id)
    if player_query:
        clauses.append(
            "EXISTS (SELECT 1 FROM player_match_results p "
            "LEFT JOIN player_names n ON n.steam_id = p.steam_id "
            "WHERE p.server_id = m.server_id AND p.event_id = m.event_id "
            "AND (p.steam_id LIKE ? ESCAPE '\\' OR LOWER(n.name) LIKE ? ESCAPE '\\'))"
        )
        params += [_like(player_query), _like(player_query)]
    where = _conditions(clauses)
    async with _conn() as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM match_results m{where}", tuple(params))
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT m.server_id, m.event_id, m.map, m.started_utc, m.ended_utc, m.winning_faction, "
            f"m.faction_scores, m.players_count FROM match_results m{where} "
            "ORDER BY m.ended_utc DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
    items = []
    for row in rows:
        try:
            scores = json.loads(row[6] or "[]")
        except ValueError:
            scores = []
        items.append({
            "server_id": row[0], "id": row[1], "map": row[2] or "", "started_utc": row[3], "ended_utc": row[4],
            "winning_faction": row[5] or "", "scores": scores if isinstance(scores, list) else [],
            "players": row[7], "in_progress": False,
        })
    return {"total": int(total or 0), "items": items}


AUDIT_SECTIONS = {
    "bans": ("ban.",),
    "players": ("rcon.", "player."),
    "vip": ("vip.", "reserved."),
    "clans": ("clan.",),
    "tickets": ("ticket",),
    "match": ("rcon.change_map", "rcon.match_", "rcon.change_lighting", "rotation.", "broadcast"),
    "settings": ("config.", "roles.", "discord.", "dev."),
}


async def search_site_audit(query="", section="", server_id="", limit=50, offset=0):
    clauses, params = [], []
    if section in AUDIT_SECTIONS:
        prefixes = AUDIT_SECTIONS[section]
        clauses.append("(" + " OR ".join("a.event LIKE ?" for _ in prefixes) + ")")
        params += [f"{prefix}%" for prefix in prefixes]
    if server_id:
        clauses.append("a.server_id = ?")
        params.append(server_id)
    if query:
        pattern = _like(query)
        clauses.append(
            "(LOWER(COALESCE(actor.persona, '')) LIKE ? ESCAPE '\\' OR a.actor_steam_id LIKE ? ESCAPE '\\' "
            "OR LOWER(a.detail) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(target.persona, '')) LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM player_names n WHERE n.steam_id = a.detail AND LOWER(n.name) LIKE ? ESCAPE '\\'))"
        )
        params += [pattern] * 5
    source = (
        "FROM site_audit a LEFT JOIN users actor ON actor.steam_id = a.actor_steam_id "
        "LEFT JOIN users target ON target.steam_id = a.detail"
    )
    where = _conditions(clauses)
    async with _conn() as db:
        cur = await db.execute(f"SELECT COUNT(*) {source}{where}", tuple(params))
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT a.id, a.timestamp_utc, a.server_id, a.actor_steam_id, COALESCE(actor.persona, ''), "
            "a.event, a.detail, COALESCE(target.persona, ''), "
            "(SELECT n.name FROM player_names n WHERE n.steam_id = a.detail ORDER BY n.last_seen_utc DESC LIMIT 1) "
            f"{source}{where} ORDER BY a.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        rows = await cur.fetchall()
    return {
        "total": int(total or 0),
        "items": [
            {
                "id": row[0], "timestamp_utc": row[1], "server_id": row[2], "actor_steam_id": row[3],
                "actor_name": row[4], "event": row[5], "detail": row[6], "target_name": row[7] or row[8] or "",
            }
            for row in rows
        ],
    }


def period_default_days(now=None):
    """Текущая неделя с понедельника: период по умолчанию."""
    today = datetime.fromtimestamp(now if now is not None else _now(), timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat(), today.isoformat()


async def live_player_meta(steam_ids):
    """Часы на наших серверах, клан и VIP для игроков онлайн: {steam_id: данные}."""
    ids = [str(steam_id) for steam_id in steam_ids][:1000]
    if not ids:
        return {}
    meta = {steam_id: {"play_seconds": 0, "clan_tag": "", "vip": False} for steam_id in ids}
    marks = ", ".join("?" for _ in ids)
    async with _conn() as db:
        cur = await db.execute(
            f"SELECT steam_id, SUM(total_seconds) FROM player_presence WHERE steam_id IN ({marks}) GROUP BY steam_id",
            tuple(ids),
        )
        for steam_id, seconds in await cur.fetchall():
            meta[steam_id]["play_seconds"] = round(seconds or 0)
        cur = await db.execute(
            "SELECT m.steam_id, c.tag FROM clan_members m JOIN clans c ON c.id = m.clan_id "
            f"WHERE m.steam_id IN ({marks})",
            tuple(ids),
        )
        for steam_id, tag in await cur.fetchall():
            meta[steam_id]["clan_tag"] = tag or ""
        cur = await db.execute(
            f"SELECT DISTINCT steam_id FROM vip_slots WHERE steam_id IN ({marks}) "
            "AND (expires_utc IS NULL OR expires_utc > ?)",
            (*ids, _now()),
        )
        for (steam_id,) in await cur.fetchall():
            meta[steam_id]["vip"] = True
    return meta


async def active_vip_steam_ids():
    async with _conn() as db:
        cur = await db.execute(
            "SELECT DISTINCT steam_id FROM vip_slots WHERE expires_utc IS NULL OR expires_utc > ?",
            (_now(),),
        )
        return {row[0] for row in await cur.fetchall()}
