import pytest

from app import config


def test_loader_accepts_bom_and_whitespace():
    assert config._load_servers_json("\ufeff  {\"servers\": []}\n", "t") == {"servers": []}


def test_loader_rejects_empty_with_source_name():
    with pytest.raises(RuntimeError, match="пуста: host-file"):
        config._load_servers_json("  \n", "host-file")


def test_loader_rejects_invalid_json_with_source_name():
    with pytest.raises(RuntimeError, match="не является JSON: WARDOGS_SERVERS_JSON"):
        config._load_servers_json("not json", "WARDOGS_SERVERS_JSON")
