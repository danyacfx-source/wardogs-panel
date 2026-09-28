import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api, config, db


def test_draft_orders_persist_without_granting_vip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'orders.db')
    monkeypatch.setattr(config, 'DATABASE_URL', '')
    async def allow(*args):
        return {'steam_id': '76561190000000000'}
    monkeypatch.setattr(api, '_require_perm', allow)
    monkeypatch.setattr(api, '_require_any_perm', allow)
    sid = next(iter(api.POOL))
    async def run():
        await db.init()
        payload = {'plan_id': 'personal-3', 'buyer_steam_id': '76561190000000001', 'server_id': sid, 'amount': 1}
        async def body():
            return payload
        request = SimpleNamespace(json=body)
        result = await api.vip_order_create(request)
        item = result['item']
        assert item['amount'] == 1200  # The browser cannot override the tariff.
        assert item['months'] == 3
        assert item['recipients'] == ['76561190000000001']
        assert item['status'] == 'awaiting_payment'
        assert await db.list_vip_slots(sid) == []
        await db.init()  # Restart/migration does not erase orders.
        assert (await db.list_vip_orders())[0]['id'] == item['id']
        payload['recipients'] = ['bad-id']
        with pytest.raises(HTTPException) as error:
            await api.vip_order_create(request)
        assert error.value.status_code == 400
        payload.update(plan_id='clan-10', recipients=['76561190000000001'] * 2)
        with pytest.raises(HTTPException):
            await api.vip_order_create(request)
        await api.vip_order_cancel(item['id'], request)
        assert (await db.list_vip_orders())[0]['status'] == 'cancelled'
        with pytest.raises(HTTPException):
            await api.vip_order_cancel(item['id'], request)
    asyncio.run(run())
