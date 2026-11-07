"""При PostgreSQL site.db в Discord не отправляется и не восстанавливается: бэкапы ведёт хостинг."""
import asyncio

from app import backup, config


def _set(monkeypatch, database_url):
    monkeypatch.setattr(config, "DATABASE_URL", database_url)
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(config, "BACKUP_CHANNEL_ID", 123)


def test_postgres_disables_discord_backup(monkeypatch):
    _set(monkeypatch, "postgresql://user:pass@db.example/wardogs")
    assert backup._enabled() is False
    assert asyncio.run(backup.send_backup()) is False
    assert asyncio.run(backup.restore_backup()) is False


def test_sqlite_keeps_discord_backup_enabled(monkeypatch):
    _set(monkeypatch, "")
    assert backup._enabled() is True
