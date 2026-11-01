"""Общий кулдаун свапа «1 раз за игру» для панели и Discord-бота.

Бот и панель работают в одном процессе, поэтому достаточно одного словаря в памяти.
Ключ — (server_id, steam_id), значение — карта (или "__unknown__", если карта не читается).
"""

SW_COOLDOWN: dict[tuple[str, str], str] = {}
SW_COOLDOWN_LIMIT = 20000


def remember(key: tuple[str, str], game_key: str) -> None:
    """Записывает свап и вытесняет самые старые записи (dict хранит порядок вставки)."""
    SW_COOLDOWN[key] = game_key
    while len(SW_COOLDOWN) > SW_COOLDOWN_LIMIT:
        SW_COOLDOWN.pop(next(iter(SW_COOLDOWN)), None)
