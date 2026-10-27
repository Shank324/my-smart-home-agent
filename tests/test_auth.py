"""鉴权与角色边界。"""

from __future__ import annotations

import uuid

from conftest import login


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_returns_token_and_user(client):
    response = client.post("/api/auth/login", json={"username": "user1", "password": "home123"})
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["username"] == "user1"
    assert body["user"]["role"] == "home_user"


def test_login_wrong_password(client):
    response = client.post("/api/auth/login", json={"username": "user1", "password": "wrong"})
    assert response.status_code == 401


def test_missing_token_rejected(client):
    assert client.get("/api/devices").status_code == 401


def test_garbage_token_rejected(client):
    response = client.get("/api/devices", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


def test_register_then_login(client):
    username = f"t_{uuid.uuid4().hex[:8]}"
    response = client.post("/api/auth/register", json={
        "username": username, "password": "pass1234", "display_name": "测试用户",
        "home_name": "测试家庭", "role": "home_user",
    })
    assert response.status_code == 201, response.text
    assert login(client, username, "pass1234")


def test_register_duplicate_username(client):
    response = client.post("/api/auth/register", json={
        "username": "user1", "password": "pass1234", "display_name": "重复用户",
    })
    assert response.status_code in (400, 409)


def test_technician_endpoint_requires_staff_role(client, home_headers):
    assert client.get("/api/technician/me", headers=home_headers).status_code == 403


def test_technician_profile(client, staff_headers):
    body = client.get("/api/technician/me", headers=staff_headers).json()
    assert body["username"] == "repair1"
    assert "stats" in body and "available_orders" in body


def test_me_returns_role_and_home(client, home_headers):
    body = client.get("/api/me", headers=home_headers).json()
    assert body["username"] == "user1"
    assert body["role"] == "home_user"
    assert body["home_id"] is not None
