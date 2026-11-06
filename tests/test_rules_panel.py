"""Правила: разбиение на эфемерные части без потери текста и без выхода за лимит embed."""
import pytest

from app.bot import rules_panel


@pytest.mark.parametrize("kind", ["discord", "game"])
def test_parts_are_lossless_and_within_embed_limit(kind):
    parts = rules_panel.load_parts(kind)
    source = rules_panel.SOURCES[kind][1].read_text(encoding="utf-8")
    assert "".join(parts) == source
    assert all(len(part) <= rules_panel.PART_LIMIT for part in parts)
    assert all(len(part) <= 4096 for part in parts)


@pytest.mark.parametrize("kind", ["discord", "game"])
def test_sections_are_not_cut_in_the_middle(kind):
    parts = rules_panel.load_parts(kind)
    for part in parts:
        # Каждая часть начинается с раздела или с начала файла, без разрыва заголовка.
        assert not part.startswith("\n")


def test_long_section_is_split_by_lines():
    text = "## Раздел\n" + "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
    parts = rules_panel.split_rules(text, limit=1000)
    assert "".join(parts) == text
    assert all(len(part) <= 1000 for part in parts)


def test_banner_asset_exists():
    assert rules_panel.BANNER.exists()
