"""Ставки сидинга: одно место для правил начисления.

Начисление считается по накопленным секундам непрерывной сессии. Этим же
модулем пользуется база (начисление баллов) и карточка сервера (текст наград),
поэтому менять ставки или момент их перехода достаточно здесь.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateStage:
    # Сколько секунд сессии действует этап; None — до конца сессии.
    until_seconds: int | None
    # Один балл за столько секунд.
    seconds_per_point: int


RATE_STAGES: tuple[RateStage, ...] = (
    RateStage(until_seconds=3600, seconds_per_point=240),  # первый час: 15 баллов в час
    RateStage(until_seconds=None, seconds_per_point=180),  # дальше: 20 баллов в час
)


# Пассивная игра вне окна сидинга: 10 баллов в первый час, затем 15 в час.
PASSIVE_STAGES: tuple[RateStage, ...] = (
    RateStage(until_seconds=3600, seconds_per_point=360),
    RateStage(until_seconds=None, seconds_per_point=240),
)


START_BONUS_POINTS = 40
LEAVER_BONUS_POINTS = 25
LEAVER_WINDOW_SECONDS = 600


def start_bonus_recipients(present: set[str], previous: dict[str, int], now: int) -> dict[str, int]:
    """Кому и сколько баллов начислить при старте матча.
    present — SteamID на сервере сейчас; previous — SteamID -> время последнего тика на сервере."""
    window_ms = LEAVER_WINDOW_SECONDS * 1000
    recipients = {steam: START_BONUS_POINTS for steam in present}
    for steam, seen in previous.items():
        if steam not in present and now - seen <= window_ms:
            recipients.setdefault(steam, LEAVER_BONUS_POINTS)
    return recipients


def bonus_lines() -> list[str]:
    """Строки бонусов для карточки."""
    return [
        f"Старт сервера: +{START_BONUS_POINTS} всем, кто сидел",
        f"+{LEAVER_BONUS_POINTS} вышедшим за {LEAVER_WINDOW_SECONDS // 60} минут",
    ]


def points_for_seconds(seconds: int, stages: tuple[RateStage, ...] = RATE_STAGES) -> int:
    """Всего баллов за непрерывную сессию длиной `seconds`."""
    safe = max(0, int(seconds))
    total = 0
    start = 0
    for stage in stages:
        end = safe if stage.until_seconds is None else min(safe, stage.until_seconds)
        if end > start:
            total += (end - start) // stage.seconds_per_point
        if stage.until_seconds is None or safe <= stage.until_seconds:
            break
        start = stage.until_seconds
    return total


def incremental_points(before: int, after: int) -> int:
    """Баллы, набранные при переходе сессии с `before` до `after` секунд."""
    return points_for_seconds(after) - points_for_seconds(before)


def rate_lines() -> list[str]:
    """Человекочитаемые строки ставок для карточки и правил."""
    lines: list[str] = []
    for stage in RATE_STAGES:
        per_hour = 3600 // stage.seconds_per_point
        if stage.until_seconds is None:
            lines.append(f"Дальше — {per_hour} баллов/час")
        else:
            hours = stage.until_seconds // 3600
            span = "первый час" if hours == 1 else f"первые {hours} ч"
            lines.append(f"{span.capitalize()} — {per_hour} баллов/час")
    return lines
