from fastapi.testclient import TestClient

from app.api import _swap_allowed
from app.main import app


def test_swap_balance_rules():
    assert _swap_allowed(2, has_vip=False) is True   # штатный баланс
    assert _swap_allowed(3, has_vip=False) is False  # больше +2 — отказ
    assert _swap_allowed(4, has_vip=True) is True    # VIP: ещё +2
    assert _swap_allowed(5, has_vip=True) is False   # и с VIP больше +4 нельзя


def test_swap_endpoint_requires_permission():
    with TestClient(app) as client:
        response = client.post(
            "/api/server/ru1/players/76561190000000001/swap",
            json={"faction": "Lonestar"},
        )
    assert response.status_code == 401
