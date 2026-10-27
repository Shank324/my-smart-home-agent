"""JWT 与实时广播的单元测试。

JWT 的价值在于「不依赖进程内状态」：所以这里重点测签名、过期、篡改，
以及服务端在多实例下对同一 token 的判断必须一致。
"""

from __future__ import annotations

import pytest

from backend import realtime
from backend.tokens import TokenError, create_token, decode_token, user_id_from_token


def test_token_roundtrip():
    token = create_token(7)
    assert user_id_from_token(token) == 7
    assert decode_token(token)["sub"] == "7"


def test_tampered_token_rejected():
    token = create_token(7)
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload}.{signature[:-2]}AB"
    assert user_id_from_token(forged) is None


def test_expired_token_rejected():
    assert user_id_from_token(create_token(7, ttl_seconds=-1)) is None


def test_garbage_token_rejected():
    assert user_id_from_token("not.a.token") is None
    assert user_id_from_token("") is None
    assert user_id_from_token(None) is None


def test_decode_raises_for_bad_signature():
    with pytest.raises(TokenError):
        decode_token("a.b.c")


def test_api_rejects_expired_token(client):
    """服务端必须真的验签，而不是只解 payload。"""
    expired = create_token(1, ttl_seconds=-5)
    response = client.get("/api/devices", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_api_rejects_token_signed_with_other_secret(client, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "another-secret")
    foreign = create_token(1)
    monkeypatch.delenv("JWT_SECRET")
    response = client.get("/api/devices", headers={"Authorization": f"Bearer {foreign}"})
    assert response.status_code == 401


def test_visibility_rules():
    """设备事件只给同家庭；工单事件还要给维修人员（-1）。"""
    assert realtime.visible(2, "device_updated", 2) is True
    assert realtime.visible(3, "device_updated", 2) is False
    assert realtime.visible(-1, "device_updated", 2) is False
    assert realtime.visible(-1, "work_order_updated", 2) is True
    assert realtime.visible(3, "work_order_updated", 2) is False


def test_broadcast_falls_back_to_local(monkeypatch):
    """Redis 不可用时 broadcast 必须退化成本地 fanout，而不是抛异常。"""
    calls = []

    async def fake_fanout(event, data, home_id):
        calls.append((event, data, home_id))

    monkeypatch.setattr(realtime, "fanout", fake_fanout)
    monkeypatch.setattr(realtime, "_publisher", None)
    import asyncio
    asyncio.run(realtime.broadcast("device_updated", {"id": 1}, 2))
    assert calls == [("device_updated", {"id": 1}, 2)]
