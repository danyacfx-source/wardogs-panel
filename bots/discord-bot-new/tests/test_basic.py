import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_config_loads():
    os.environ.setdefault("DISCORD_TOKEN", "dummy")
    import config

    assert hasattr(config, "TOKEN")
    assert hasattr(config, "BACKUP_CHANNEL_ID")
    assert hasattr(config, "GUILD_ID")


def test_database_init_creates_file():
    import database

    with tempfile.TemporaryDirectory() as tmp:
        old_file = database.DB_FILE
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            database.DB_FILE = "test_wardogs.db"
            database.init_db()
            assert Path(database.DB_FILE).exists()
        finally:
            database.close_all_connections()
            database.DB_FILE = old_file
            os.chdir(old_cwd)


def test_hash_code_is_stable():
    import database

    h1 = database.seed_hash_code("12345", "ABCDEF")
    h2 = database.seed_hash_code("12345", "ABCDEF")
    assert h1 == h2
    assert len(h1) >= 16
    h3 = database.seed_hash_code("12345", "DIFFERENT")
    assert h1 != h3


def test_xp_math():
    import database

    assert database.xp_needed_for_level(1) == 0
    assert database.xp_needed_for_level(2) > 0
    assert database.level_from_xp(0) == 1
    need = database.xp_needed_for_level(2)
    assert database.level_from_xp(need) >= 2
    current, needed = database.xp_progress(0, 1)
    assert current == 0
    assert needed > 0
