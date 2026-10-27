"""鉴权与角色边界。"""

from __future__ import annotations

import hashlib
import uuid

from conftest import login
from sqlalchemy import select


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


def test_password_hash_is_bcrypt(client, home_headers):
    """口令不能是裸 SHA-256，必须是带盐的 bcrypt。"""
    from backend.db import SessionLocal
    from backend.models import User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "user1"))
        stored = user.password_hash
    assert stored.startswith(("$2a$", "$2b$", "$2y$"))
    assert len(stored) == 60


def test_legacy_sha256_hash_upgrades_on_login(client):
    """历史数据是无盐 SHA-256：老用户仍能登录，且登录后自动升级成 bcrypt。"""
    from backend.db import SessionLocal
    from backend.models import Home, Member, Role, User, user_roles

    def make_legacy_user(username: str) -> None:
        with SessionLocal() as db:
            home = db.scalar(select(Home).limit(1))
            user = User(username=username,
                        password_hash=hashlib.sha256(b"oldpass123").hexdigest(),
                        display_name="历史用户")
            db.add(user)
            db.flush()
            role = db.scalar(select(Role).where(Role.name == "home_user"))
            db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
            db.add(Member(user_id=user.id, home_id=home.id))
            db.commit()

    username = f"legacy_{uuid.uuid4().hex[:6]}"
    make_legacy_user(username)
    assert login(client, username, "oldpass123")

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        assert user.password_hash.startswith("$2")

    # 升级后仍可用同一个口令登录
    assert login(client, username, "oldpass123")
    assert client.post("/api/auth/login",
                       json={"username": username, "password": "wrong"}).status_code == 401


def test_verify_password_rejects_empty_hash():
    from backend.security import verify_password

    assert verify_password("anything", "") is False
