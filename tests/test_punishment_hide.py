import asyncio

from app import config, db


def test_hidden_punishments_keep_audit_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hide.db")
    target = "76561190000000001"
    staff = "76561190000000002"

    async def run():
        await db.init()
        await db.log_site_audit(staff, "ru1", "ban.add", target)
        await db.log_site_audit(staff, "ru1", "rcon.kick", target)
        await db.log_site_audit(staff, "ru2", "ban.remove", target)
        await db.log_site_audit(staff, "ru2", "ban.add", "76561190000000009")
        rows = await db.list_player_punishments(target)
        assert len(rows) == 3
        other_player_row = (await db.list_player_punishments("76561190000000009"))[0]

        # выборочное скрытие: только выбранные записи этого игрока
        hidden = await db.hide_selected_player_punishments(
            target, [rows[0]["id"], rows[1]["id"]], staff
        )
        assert sorted(hidden) == sorted([rows[0]["id"], rows[1]["id"]])
        assert [row["id"] for row in await db.list_player_punishments(target)] == [rows[2]["id"]]

        # чужая запись и несуществующий id не скрываются
        assert await db.hide_selected_player_punishments(target, [other_player_row["id"], 999999], staff) == []
        assert len(await db.list_player_punishments("76561190000000009")) == 1

        # повторное скрытие уже скрытой записи не ломает результат
        assert await db.hide_selected_player_punishments(target, [rows[0]["id"]], staff) == [rows[0]["id"]]

        await db.hide_all_player_punishments(target, staff)
        assert await db.list_player_punishments(target) == []

        # строки журнала на месте, цепочка хешей не сломана
        assert (await db.verify_site_audit_chain(force=True))["ok"] is True

    asyncio.run(run())


def test_punishment_hide_endpoints_require_permission():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        selected = client.post(
            "/api/players/76561190000000001/punishments/hide", json={"ids": [1, 2]}
        )
        clear = client.delete("/api/players/76561190000000001/punishments")
    assert selected.status_code == 401
    assert clear.status_code == 401
