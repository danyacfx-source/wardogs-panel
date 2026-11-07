"""Принудительная выгрузка базы: таблицы PostgreSQL в CSV внутри zip, либо файл site.db для SQLite.

Если выгрузка больше лимита вложения Discord, она делится на несколько частей:
- PostgreSQL: несколько zip-архивов; таблицы, которые не влезают целиком, режутся по строкам CSV
  (заголовок повторяется в каждой части).
- SQLite: site.db режется по байтам на части `.partNNN`; собрать обратно: `copy /b` или `cat`.
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

MAX_ARCHIVE_BYTES = 24 * 1024 * 1024  # лимит вложения Discord для бота без буста
SITE_SCHEMAS = ("public", "bot")
_MANIFEST_RESERVE = 64 * 1024  # запас на manifest.json и служебные поля zip


class ExportError(RuntimeError):
    pass


def site_dsn() -> str:
    return (os.environ.get("WARDOGS_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()


def bot_dsn() -> str:
    return (os.environ.get("BOT_DATABASE_URL") or "").strip()


def _budget() -> int:
    return MAX_ARCHIVE_BYTES - _MANIFEST_RESERVE


def _zip_bytes(files: dict[str, bytes], part: int, total: int) -> bytes:
    buf = io.BytesIO()
    manifest = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "part": part,
        "parts": total,
        "files": sorted(files),
    }
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


def export_due(last_ts: float, now: float, hours: int) -> bool:
    """Пора ли делать автовыгрузку: hours <= 0 — выключено."""
    return hours > 0 and now - float(last_ts or 0) >= hours * 3600


def pack_archive(files: dict[str, bytes], part: int = 1, total: int = 1) -> bytes:
    """Упаковывает файлы в один zip. Больше лимита Discord — ошибка, а не молчаливая обрезка."""
    data = _zip_bytes(files, part, total)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ExportError(f"Архив {len(data) // (1024 * 1024)} МБ больше лимита Discord (24 МБ).")
    return data


def _compressed_size(name: str, data: bytes) -> int:
    return len(_zip_bytes({name: data}, 1, 1))


def _split_csv(name: str, data: bytes) -> list[tuple[str, bytes]]:
    """Режет CSV по строкам пополам, пока каждая часть не влезет в бюджет. Заголовок повторяется."""
    lines = data.splitlines(keepends=True)
    if len(lines) < 2:
        raise ExportError(f"Таблица {name} не влезает в лимит Discord даже одной строкой.")
    header, rows = lines[0], lines[1:]
    stem = name[:-4] if name.endswith(".csv") else name
    out: list[tuple[str, bytes]] = []

    def walk(chunk: list[bytes]) -> None:
        blob = header + b"".join(chunk)
        if _compressed_size(name, blob) <= _budget() or len(chunk) <= 1:
            if _compressed_size(name, blob) > _budget():
                raise ExportError(f"Строка таблицы {name} не влезает в лимит Discord.")
            out.append((f"{stem}.part{len(out) + 1:03d}.csv", blob))
            return
        middle = len(chunk) // 2
        walk(chunk[:middle])
        walk(chunk[middle:])

    walk(rows)
    return out


def split_into_archives(files: dict[str, bytes]) -> list[dict[str, bytes]]:
    """Делит набор файлов на части, каждая из которых укладывается в лимит zip."""
    parts: list[dict[str, bytes]] = []
    current: dict[str, bytes] = {}
    current_size = 0
    for name, data in sorted(files.items()):
        pieces = [(name, data)] if _compressed_size(name, data) <= _budget() else _split_csv(name, data)
        for piece_name, piece in pieces:
            size = _compressed_size(piece_name, piece)
            if current and current_size + size > _budget():
                parts.append(current)
                current, current_size = {}, 0
            current[piece_name] = piece
            current_size += size
    if current:
        parts.append(current)
    return parts


def export_postgres(dsn: str, schemas: tuple[str, ...]) -> dict[str, bytes]:
    """Выгружает все базовые таблицы указанных схем в CSV (с заголовком)."""
    import psycopg
    from psycopg import sql

    files: dict[str, bytes] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE' ORDER BY 1, 2",
            (list(schemas),),
        ).fetchall()
        for schema, table in tables:
            buf = io.BytesIO()
            query = sql.SQL("COPY (SELECT * FROM {}) TO STDOUT WITH CSV HEADER").format(sql.Identifier(schema, table))
            with conn.cursor().copy(query) as copy:
                for chunk in copy:
                    buf.write(bytes(chunk))
            files[f"{schema}.{table}.csv"] = buf.getvalue()
    return files


def build_archives() -> list[tuple[str, bytes]]:
    """Возвращает список (имя файла, содержимое) для отправки. Одна часть — без суффикса."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dsn = site_dsn()
    if dsn:
        files = {f"site/{name}": data for name, data in export_postgres(dsn, SITE_SCHEMAS).items()}
        bot = bot_dsn()
        if bot and bot != dsn:
            files.update({f"bot/{name}": data for name, data in export_postgres(bot, ("bot",)).items()})
        groups = split_into_archives(files)
        total = len(groups)
        result = []
        for index, group in enumerate(groups, start=1):
            suffix = "" if total == 1 else f"_part{index:02d}of{total:02d}"
            result.append((f"wardogs_db_{stamp}{suffix}.zip", pack_archive(group, index, total)))
        return result
    from app import config as site_config

    path = Path(site_config.DB_PATH)
    if not path.exists():
        raise ExportError(f"Файл базы не найден: {path}")
    data = path.read_bytes()
    if len(data) <= MAX_ARCHIVE_BYTES:
        return [(f"site_{stamp}.db", data)]
    size = MAX_ARCHIVE_BYTES - _MANIFEST_RESERVE
    chunks = [data[offset:offset + size] for offset in range(0, len(data), size)]
    return [(f"site_{stamp}.db.part{index:03d}", chunk) for index, chunk in enumerate(chunks, start=1)]
