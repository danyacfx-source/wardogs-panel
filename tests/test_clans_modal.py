import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"))

from cogs.clans import ClansCog, modal_values  # noqa: E402


def _submit_payload(rows):
    return {
        "custom_id": "clan:apply:first",
        "components": [
            {"type": 1, "components": [{"type": 4, "custom_id": cid, "value": val}]}
            for cid, val in rows
        ],
    }


def test_modal_values_reads_nested_action_rows():
    fake = SimpleNamespace(data=_submit_payload([
        ("name", "Волки"),
        ("tag", "WLF"),
        ("leader", "Ivan"),
        ("members", "15"),
    ]))
    assert modal_values(fake) == {"name": "Волки", "tag": "WLF", "leader": "Ivan", "members": "15"}


def test_modal_values_handles_empty_and_missing_data():
    assert modal_values(SimpleNamespace(data=None)) == {}
    assert modal_values(SimpleNamespace(data={})) == {}


def _ids(modal):
    return [item.custom_id for item in modal.children]


def test_application_modals_use_parser_keys():
    cog = ClansCog.__new__(ClansCog)
    assert _ids(cog._first_application_modal()) == ["name", "tag", "leader", "members"]
    assert _ids(cog._second_application_modal(1)) == [
        "discord", "description", "goals", "logo", "role-icon",
    ]


def test_join_and_recruitment_modals_use_parser_keys():
    cog = ClansCog.__new__(ClansCog)
    clan = {"id": "123", "tag": "WLF", "name": "Волки"}
    assert _ids(cog._join_application_modal(clan)) == ["nickname", "age", "experience", "motivation"]
    assert _ids(cog._recruitment_modal(clan, "create")) == [
        "looking", "requirements", "schedule", "contact", "image",
    ]
