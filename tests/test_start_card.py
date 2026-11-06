"""Карточка «Сервер запущен»: список сидящих, форматирование и рендер."""
from app.bot import start_card
from app.bot.start_card import StartCardData, format_duration, format_points, render_start_card, sitter_cells


def test_sitter_cells_limit_and_overflow_cell():
    assert sitter_cells(("a", "b")) == ["a", "b"]
    names = tuple(f"n{i}" for i in range(12))
    cells = sitter_cells(names)
    assert cells[:8] == list(names[:8])
    assert cells[8] == "и ещё 4"


def test_duration_and_points_formatting():
    assert format_duration(2 * 3600 + 19 * 60) == "2 ч 19 мин"
    assert format_duration(19 * 60) == "19 мин"
    assert format_points(1224) == "1 224"


def test_render_returns_png_of_background_size():
    card = StartCardData(
        server_label="Сервер 2",
        duration_seconds=8340,
        population=20,
        capacity=20,
        sitter_names=("Ares", "Noob", "[NWO] Tess"),
        sitter_total=49,
        points=1224,
        first_name="Ares",
        longest_name="Noob",
        longest_seconds=45300,
    )
    png = render_start_card(card)
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(png))
    assert img.format == "PNG"
    assert img.size == start_card._background().size


def test_render_handles_no_sitters():
    card = StartCardData("Сервер 1", 60, 20, 20, (), 0, 0, "", "", 0)
    assert render_start_card(card)
