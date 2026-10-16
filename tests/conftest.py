import os
import pathlib
import tempfile

# Keep API tests deterministic and independent from real RCON endpoints.
os.environ.setdefault("WARDOGS_DISABLE_BACKGROUND", "1")

# Тесты всегда идут в изолированный SQLite: боевой PostgreSQL
# (WARDOGS_DATABASE_URL) и реальная site.db не должны трогаться.
# Пустая строка, а не pop: config.py подхватывает .env через setdefault.
os.environ["WARDOGS_ENV"] = "development"
os.environ["WARDOGS_DATABASE_URL"] = ""
os.environ["DATABASE_URL"] = ""
os.environ["WARDOGS_DB"] = str(pathlib.Path(tempfile.mkdtemp(prefix="wardogs-test-")) / "test.db")
# HTTP-база, чтобы тестовые сессионные куки не были Secure и реально шли в TestClient.
os.environ["WARDOGS_PUBLIC_URL"] = "http://127.0.0.1:8236"
