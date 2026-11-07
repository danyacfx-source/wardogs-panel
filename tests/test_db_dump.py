"""Принудительная выгрузка базы: архив с manifest, деление на части, лимит Discord, SQLite-файл."""
import io
import os
import zipfile

import pytest

from app import config as site_config
from app.bot import db_dump


def test_pack_archive_contains_files_and_manifest():
    data = db_dump.pack_archive({"site/public.users.csv": b"id\n1\n"})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "site/public.users.csv"}
        assert archive.read("site/public.users.csv") == b"id\n1\n"


def test_pack_archive_refuses_oversize(monkeypatch):
    monkeypatch.setattr(db_dump, "MAX_ARCHIVE_BYTES", 1024)
    # Случайные байты не сжимаются, иначе архив уложится в лимит.
    with pytest.raises(db_dump.ExportError):
        db_dump.pack_archive({"big.bin": os.urandom(100_000)})


def test_split_puts_files_into_several_archives_within_limit(monkeypatch):
    monkeypatch.setattr(db_dump, "MAX_ARCHIVE_BYTES", 200 * 1024)
    monkeypatch.setattr(db_dump, "_MANIFEST_RESERVE", 1024)
    files = {f"site/t{i}.csv": os.urandom(80 * 1024) for i in range(5)}

    groups = db_dump.split_into_archives(files)

    assert len(groups) >= 2
    assert sorted(name for group in groups for name in group) == sorted(files)
    for group in groups:
        assert len(db_dump._zip_bytes(group, 1, 1)) <= db_dump.MAX_ARCHIVE_BYTES


def test_large_csv_is_split_by_rows_with_header_repeated(monkeypatch):
    monkeypatch.setattr(db_dump, "MAX_ARCHIVE_BYTES", 20 * 1024)
    monkeypatch.setattr(db_dump, "_MANIFEST_RESERVE", 1024)
    rows = [f"{i},{os.urandom(16).hex()}\n".encode() for i in range(3000)]
    blob = b"id,value\n" + b"".join(rows)

    groups = db_dump.split_into_archives({"site/public.big.csv": blob})

    parts = [data for group in groups for data in group.values()]
    assert len(parts) >= 2
    recovered = []
    for part in parts:
        lines = part.splitlines(keepends=True)
        assert lines[0] == b"id,value\n"
        recovered.extend(lines[1:])
        assert len(db_dump._zip_bytes({"x": part}, 1, 1)) <= db_dump.MAX_ARCHIVE_BYTES
    assert recovered == rows


def test_sqlite_export_returns_database_file(monkeypatch, tmp_path):
    db_file = tmp_path / "site.db"
    db_file.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    monkeypatch.delenv("WARDOGS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(site_config, "DB_PATH", db_file)

    archives = db_dump.build_archives()

    assert len(archives) == 1
    filename, data = archives[0]
    assert filename.startswith("site_") and filename.endswith(".db")
    assert data == db_file.read_bytes()


def test_sqlite_export_splits_large_file_into_parts(monkeypatch, tmp_path):
    db_file = tmp_path / "site.db"
    db_file.write_bytes(os.urandom(50 * 1024))
    monkeypatch.delenv("WARDOGS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(site_config, "DB_PATH", db_file)
    monkeypatch.setattr(db_dump, "MAX_ARCHIVE_BYTES", 20 * 1024)
    monkeypatch.setattr(db_dump, "_MANIFEST_RESERVE", 1024)

    archives = db_dump.build_archives()

    assert len(archives) >= 3
    assert all(len(data) <= db_dump.MAX_ARCHIVE_BYTES for _, data in archives)
    assert b"".join(data for _, data in archives) == db_file.read_bytes()


def test_sqlite_export_reports_missing_file(monkeypatch, tmp_path):
    monkeypatch.delenv("WARDOGS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(site_config, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(db_dump.ExportError):
        db_dump.build_archives()


def test_export_due_respects_interval_and_disabled_flag():
    hour = 3600
    assert db_dump.export_due(0, 25 * hour, 24) is True
    assert db_dump.export_due(0, 10 * hour, 0) is False
    assert db_dump.export_due(9 * hour, 10 * hour, 24) is False
    assert db_dump.export_due(10 * hour - 24 * hour, 10 * hour, 24) is True
