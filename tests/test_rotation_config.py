from app.api import _rotation_config_text, _rotation_entry_line

LIVE_DOC = (
    "[/Script/WDRCON.WDRCONSettings]\r\n"
    "bEnabled=true\r\n"
    "Port=7839\r\n"
    "\r\n"
    "[/Script/WDGame.WDServerMapRotationSettings]\r\n"
    "bEnabled=True\r\n"
    "RotationMode=Random\r\n"
    "!RotationEntries=ClearArray\r\n"
    '.RotationEntries=(Map="NorthAmerica", Experiences="Detroit_KOTH_01", Lighting="DayStartClear", ZoneAlternator="ZoneAlternator.Zestafona.WaterTreatment.Circle")\r\n'
    '.RotationEntries=(Map="Kavkazi", Experiences="Bakurani_KOTH_01", Lighting="DayStartClear", ZoneAlternator="ZoneAlternator.Bakurani.River.Circle")\r\n'
)

ENTRIES = [
    {"map": "Europe", "experiences": ["Madrid_KOTH_01"], "lighting": "DayLateGrayFog", "zoneAlternator": ""},
    {"map": "Kavkazi", "experiences": ["A_KOTH_01", "B_InfantryOnly"], "lighting": "DayClear", "zoneAlternator": "Z.Default.Circle"},
]


def test_entry_line_joins_experiences_and_defaults_zone():
    line = _rotation_entry_line(ENTRIES[0])
    assert line == '.RotationEntries=(Map="Europe", Experiences="Madrid_KOTH_01", Lighting="DayLateGrayFog", ZoneAlternator="None")'
    line = _rotation_entry_line(ENTRIES[1])
    assert 'Experiences="A_KOTH_01+B_InfantryOnly"' in line


def test_config_text_replaces_rotation_section_only():
    updated = _rotation_config_text(LIVE_DOC, ENTRIES, mode="ordered")
    # Секция RCON сохранена целиком.
    assert "[/Script/WDRCON.WDRCONSettings]\r\nbEnabled=true\r\nPort=7839" in updated
    # Заголовок ротации, bEnabled и режим обновлены.
    section = updated.split("[/Script/WDGame.WDServerMapRotationSettings]")[1]
    assert "bEnabled=True" in section
    assert "RotationMode=Ordered" in section
    # Старые записи вычищены, новые записаны после ClearArray.
    assert "NorthAmerica" not in updated
    assert "WaterTreatment" not in updated
    assert section.index("!RotationEntries=ClearArray") < section.index('.RotationEntries=(Map="Europe"')
    # Только одна секция ротации.
    assert updated.count("[/Script/WDGame.WDServerMapRotationSettings]") == 1


def test_config_text_appends_section_when_missing():
    doc = "[/Script/WDGame.WDGameSession]\nServerName=x\n"
    updated = _rotation_config_text(doc, ENTRIES, mode=None)
    assert updated.startswith(doc.rstrip("\r\n"))
    assert updated.count("[/Script/WDGame.WDServerMapRotationSettings]") == 1
    assert "RotationMode=Ordered" in updated
    assert updated.count(".RotationEntries=") == 2


def test_config_text_keeps_unknown_keys_in_section():
    doc = LIVE_DOC.replace(
        "RotationMode=Random",
        "RotationMode=Random\r\nSomeFutureKey=7",
    )
    updated = _rotation_config_text(doc, [], mode=None)
    assert "SomeFutureKey=7" in updated
    assert "RotationMode=Random" in updated
    assert ".RotationEntries=" not in updated


def test_config_text_preserves_line_endings():
    updated = _rotation_config_text(LIVE_DOC, ENTRIES, mode="random")
    assert "\r\n" in updated
    assert updated.count("\n") == updated.count("\r\n")
