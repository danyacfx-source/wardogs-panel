"""Данные карточки игрока: активность по дням, сессии, игры, убийства и история банов."""

import json
import secrets
from datetime import datetime, timedelta, timezone

try:
    from .core import _conn, _now
except ImportError:
    from core import _conn, _now

CHART_DAYS = 35
LIST_LIMIT = 30
START_RATING = 1500.0
RATING_K = 32.0
RATING_BANDS = ((10, "Низкий"), (40, "Ниже среднего"), (60, "Средний"), (85, "Выше среднего"), (101, "Высокий"))


def rating_changes(ratings, winners, losers):
    """Изменение рейтинга по схеме Эло: команды сравниваются по среднему рейтингу.

    Победа над более сильной командой даёт больше очков, чем над слабой.
    """
    winners_average = sum(ratings[steam_id] for steam_id in winners) / len(winners)
    losers_average = sum(ratings[steam_id] for steam_id in losers) / len(losers)
    expected = 1 / (1 + 10 ** ((losers_average - winners_average) / 400))
    gain = RATING_K * (1 - expected)
    return {**{steam_id: gain for steam_id in winners}, **{steam_id: -gain for steam_id in losers}}


def rating_band(percentile):
    return next(label for limit, label in RATING_BANDS if percentile < limit)


async def add_punishment_details(kind, steam_id, server_id, reason, expires_utc=None, created_by=None):
    """Причина и срок наказания: в журнале аудита хранится только SteamID."""
    async with _conn() as db:
        await db.execute(
            "INSERT INTO punishment_details "
            "(id, kind, steam_id, server_id, reason, expires_utc, created_by, created_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), kind, steam_id, server_id, reason, expires_utc, created_by, _now()),
        )
        await db.commit()


async def list_punishment_details(steam_id, limit=300):
    async with _conn() as db:
        cur = await db.execute(
            "SELECT kind, server_id, reason, expires_utc, created_utc FROM punishment_details "
            "WHERE steam_id = ? ORDER BY created_utc DESC LIMIT ?",
            (steam_id, limit),
        )
        rows = await cur.fetchall()
    return [
        {"kind": row[0], "server_id": row[1], "reason": row[2] or "", "expires_utc": row[3], "created_utc": row[4]}
        for row in rows
    ]


async def record_match_result(server_id, result, players):
    """Итог матча и сторона каждого игрока на момент конца. Повторная доставка ничего не меняет."""
    winner = result["winning_faction"].casefold()
    async with _conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM match_results WHERE server_id = ? AND event_id = ?",
            (server_id, result["event_id"]),
        )
        if await cur.fetchone():
            return False
        cur = await db.execute("SELECT started_utc FROM server_match_state WHERE server_id = ?", (server_id,))
        started = await cur.fetchone()
        await db.execute("DELETE FROM server_match_state WHERE server_id = ?", (server_id,))
        await db.execute(
            "INSERT INTO match_results "
            "(server_id, event_id, match_id, map, started_utc, ended_utc, winning_faction, faction_scores, players_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(server_id, event_id) DO NOTHING",
            (
                server_id, result["event_id"], result["match_id"], result["map"],
                started[0] if started else None, result["ended_utc"], result["winning_faction"],
                json.dumps(result["faction_scores"], ensure_ascii=False),
                result.get("players_count") if result.get("players_count") is not None else (len(players) or None),
            ),
        )
        for steam_id, faction in players.items():
            # Ничья или неизвестный победитель не идут в винрейт: won остаётся пустым.
            won = (1 if faction.casefold() == winner else 0) if winner and faction else None
            await db.execute(
                "INSERT INTO player_match_results "
                "(server_id, event_id, steam_id, match_id, map, faction, won, ended_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(server_id, event_id, steam_id) DO NOTHING",
                (server_id, result["event_id"], steam_id, result["match_id"], result["map"], faction, won, result["ended_utc"]),
            )
        await _update_ratings(db, players, winner)
        await db.commit()
    return True


async def _update_ratings(db, players, winner):
    """Без победителя или без одной из сторон матч ничего не говорит о силе игроков."""
    if not winner:
        return
    winners = [steam_id for steam_id, faction in players.items() if faction and faction.casefold() == winner]
    losers = [steam_id for steam_id, faction in players.items() if faction and faction.casefold() != winner]
    if not winners or not losers:
        return
    ids = winners + losers
    placeholders = ", ".join("?" for _ in ids)
    cur = await db.execute(f"SELECT steam_id, rating FROM player_ratings WHERE steam_id IN ({placeholders})", tuple(ids))
    known = {row[0]: float(row[1]) for row in await cur.fetchall()}
    ratings = {steam_id: known.get(steam_id, START_RATING) for steam_id in ids}
    now = _now()
    for steam_id, change in rating_changes(ratings, winners, losers).items():
        await db.execute(
            "INSERT INTO player_ratings (steam_id, rating, games, updated_utc) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(steam_id) DO UPDATE SET rating=excluded.rating, "
            "games=player_ratings.games+1, updated_utc=excluded.updated_utc",
            (steam_id, ratings[steam_id] + change, now),
        )


async def _player_rating(db, steam_id):
    cur = await db.execute("SELECT rating, games FROM player_ratings WHERE steam_id = ?", (steam_id,))
    row = await cur.fetchone()
    if not row:
        return None
    rating = float(row[0])
    cur = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN rating < ? THEN 1 ELSE 0 END), 0) FROM player_ratings",
        (rating,),
    )
    total, below = await cur.fetchone()
    # Сам игрок в сравнении не участвует: единственный игрок в таблице лучше 0%.
    others = max(0, int(total) - 1)
    percentile = round(int(below) * 100 / others) if others else 0
    return {
        "value": round(rating),
        "games": int(row[1]),
        "percentile": percentile,
        "band": rating_band(percentile),
    }


async def recent_server_factions(server_id, max_age_seconds=180):
    """Запасной источник сторон, если список игроков нельзя получить в момент конца матча."""
    async with _conn() as db:
        cur = await db.execute(
            "SELECT steam_id, faction FROM player_activity WHERE server_id = ? AND seen_utc >= ?",
            (server_id, _now() - max_age_seconds),
        )
        rows = await cur.fetchall()
    return {row[0]: row[1] or "" for row in rows}


async def get_player_card_activity(steam_id):
    first_day = (datetime.now(timezone.utc) - timedelta(days=CHART_DAYS - 1)).strftime("%Y-%m-%d")
    async with _conn() as db:
        cur = await db.execute(
            "SELECT day, SUM(play_seconds), SUM(seed_seconds) FROM player_daily_activity "
            "WHERE steam_id = ? AND day >= ? GROUP BY day ORDER BY day",
            (steam_id, first_day),
        )
        daily = [
            {"day": row[0], "play_seconds": round(row[1] or 0), "seed_seconds": round(row[2] or 0)}
            for row in await cur.fetchall()
        ]
        cur = await db.execute(
            "SELECT COALESCE(SUM(seed_seconds), 0) FROM player_daily_activity WHERE steam_id = ?",
            (steam_id,),
        )
        seed_total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT server_id, started_utc, last_seen_utc, seed_seconds FROM player_sessions "
            "WHERE steam_id = ? ORDER BY started_utc DESC LIMIT ?",
            (steam_id, LIST_LIMIT),
        )
        sessions = [
            {
                "server_id": row[0],
                "started_utc": row[1],
                "last_seen_utc": row[2],
                "duration_seconds": round(max(0.0, float(row[2]) - float(row[1]))),
                "seed_seconds": round(row[3] or 0),
            }
            for row in await cur.fetchall()
        ]
        cur = await db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN killer_id = ? AND suicide = 0 AND teamkill = 0 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN victim_id = ? THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN killer_id = ? AND teamkill = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN killer_id = ? AND headshot = 1 AND teamkill = 0 THEN 1 ELSE 0 END), 0) "
            "FROM killfeed_events WHERE killer_id = ? OR victim_id = ?",
            (steam_id,) * 6,
        )
        totals = await cur.fetchone()
        cur = await db.execute(
            "SELECT server_id, match_id, MAX(map), MIN(event_time), MAX(event_time), "
            "SUM(CASE WHEN killer_id = ? AND suicide = 0 AND teamkill = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN victim_id = ? THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN killer_id = ? AND teamkill = 1 THEN 1 ELSE 0 END) "
            "FROM killfeed_events WHERE (killer_id = ? OR victim_id = ?) AND match_id <> '' "
            "GROUP BY server_id, match_id ORDER BY MAX(event_time) DESC LIMIT ?",
            (steam_id, steam_id, steam_id, steam_id, steam_id, LIST_LIMIT),
        )
        matches = [
            {
                "server_id": row[0], "match_id": row[1], "map": row[2] or "",
                "started_utc": row[3], "ended_utc": row[4],
                "kills": int(row[5] or 0), "deaths": int(row[6] or 0), "teamkills": int(row[7] or 0),
            }
            for row in await cur.fetchall()
        ]
        kill_columns = (
            "server_id, event_id, event_time, map, killer_id, killer_name, victim_id, victim_name, "
            "cause, distance_m, headshot, suicide, teamkill"
        )
        cur = await db.execute(
            f"SELECT {kill_columns} FROM killfeed_events WHERE killer_id = ? OR victim_id = ? "
            "ORDER BY event_time DESC LIMIT ?",
            (steam_id, steam_id, LIST_LIMIT),
        )
        kills = [_kill_row(row) for row in await cur.fetchall()]
        cur = await db.execute(
            "SELECT COALESCE(SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END), 0) "
            "FROM player_match_results WHERE steam_id = ?",
            (steam_id,),
        )
        record = await cur.fetchone()
        cur = await db.execute(
            "SELECT server_id, match_id, map, ended_utc, won, faction FROM player_match_results "
            "WHERE steam_id = ? ORDER BY ended_utc DESC LIMIT ?",
            (steam_id, LIST_LIMIT),
        )
        results = await cur.fetchall()
        rating = await _player_rating(db, steam_id)
        cur = await db.execute(
            f"SELECT {kill_columns} FROM killfeed_events WHERE killer_id = ? AND teamkill = 1 "
            "ORDER BY event_time DESC LIMIT ?",
            (steam_id, LIST_LIMIT),
        )
        teamkills = [_kill_row(row) for row in await cur.fetchall()]
    wins, losses = int(record[0] or 0), int(record[1] or 0)
    return {
        "daily": daily,
        "seed_seconds": round(seed_total or 0),
        "sessions": sessions,
        "matches": _merge_match_results(matches, results),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins * 100 / (wins + losses)) if wins + losses else None,
        "rating": rating,
        "kills": kills,
        "teamkills": teamkills,
        "stats": {
            "kills": int(totals[0] or 0),
            "deaths": int(totals[1] or 0),
            "teamkills": int(totals[2] or 0),
            "headshots": int(totals[3] or 0),
        },
    }


def _merge_match_results(matches, results):
    """Игры из килфида и итоги матчей: игрок мог доиграть матч и без единого убийства."""
    for match in matches:
        match["won"] = None
        match["faction"] = ""
    by_key = {(match["server_id"], match["match_id"]): match for match in matches}
    for row in results:
        match = by_key.get((row[0], row[1])) if row[1] else None
        if match is None:
            match = {
                "server_id": row[0], "match_id": row[1] or f"ended-{row[3]}", "map": row[2] or "",
                "started_utc": row[3], "ended_utc": row[3], "kills": 0, "deaths": 0, "teamkills": 0,
            }
            matches.append(match)
        match["won"] = None if row[4] is None else bool(row[4])
        match["faction"] = row[5] or ""
    matches.sort(key=lambda item: item["ended_utc"], reverse=True)
    return matches[:LIST_LIMIT]


def _kill_row(row):
    return {
        "server_id": row[0], "event_id": row[1], "event_time": row[2], "map": row[3] or "",
        "killer_id": row[4], "killer_name": row[5] or "", "victim_id": row[6], "victim_name": row[7] or "",
        "cause": row[8] or "", "distance_m": row[9],
        "headshot": bool(row[10]), "suicide": bool(row[11]), "teamkill": bool(row[12]),
    }
