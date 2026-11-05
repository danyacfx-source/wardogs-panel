"""Карточки «Зовём сидеров» и ставки сидинга."""

import io

from PIL import Image

from app.bot import seed_rates
from app.bot.seed_card import CardData, format_seeders, render_server_card


def _old_points(seconds: int) -> int:
    """Прежняя формула из database.py — ставки не должны поменяться."""
    safe = max(0, int(seconds))
    return min(safe, 3600) // 240 + max(0, safe - 3600) // 180


def test_points_match_previous_formula_on_many_sessions():
    for seconds in list(range(0, 7300, 17)) + [3599, 3600, 3601, 7200, 86400]:
        assert seed_rates.points_for_seconds(seconds) == _old_points(seconds), seconds


def test_incremental_points_sum_to_total():
    steps = [0, 30, 359, 360, 3599, 3600, 3700, 5000]
    total = sum(seed_rates.incremental_points(a, b) for a, b in zip(steps, steps[1:]))
    assert total == seed_rates.points_for_seconds(steps[-1])


def test_rate_lines_describe_both_stages():
    assert seed_rates.rate_lines() == [
        "Первый час — 15 баллов/час",
        "Дальше — 20 баллов/час",
    ]


def test_format_seeders_limits_names():
    assert format_seeders(()) == []
    assert format_seeders(("a", "b")) == ["a", "b"]
    assert format_seeders(("a", "b", "c", "d", "e")) == ["a", "b", "c", "и ещё 2"]


def _png_size(data: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG"
    return img.size


def test_every_state_renders_png():
    rates = tuple(seed_rates.rate_lines())
    for state in ("active", "limit", "closed", "offline"):
        card = CardData(
            server_label="Сервер 2",
            state=state,
            population=3,
            capacity=20,
            seeder_names=("[STRB] E39", "[STRB] BIBA", "il'sor", "Игрок"),
            window_text="06:00–22:00",
            rates=rates,
        )
        assert _png_size(render_server_card(card)) == (1756, 896), state


def test_cyrillic_and_long_names_do_not_break_render():
    card = CardData(
        server_label="Сервер 3",
        state="active",
        population=19,
        capacity=20,
        seeder_names=("Очень_длинный_ник_который_не_должен_вылезать_за_рамку_карточки_вообще",),
        window_text="06:00–22:00",
        rates=tuple(seed_rates.rate_lines()),
    )
    assert _png_size(render_server_card(card)) == (1756, 896)


def test_card_data_is_hashable_for_change_detection():
    a = CardData(server_label="Сервер 1", state="active", population=5, capacity=20)
    b = CardData(server_label="Сервер 1", state="active", population=5, capacity=20)
    c = CardData(server_label="Сервер 1", state="active", population=6, capacity=20)
    assert hash(a) == hash(b)
    assert a != c
