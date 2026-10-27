"""测试夹具：用一个独立的 SQLite 库跑完整应用，不依赖 MySQL / Redis / 外部 API。

关键点是**先设环境变量再 import backend**：`backend.db` 在导入时就按 DATABASE_URL
建 engine，晚一步设就跑到真库上去了。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_FILE = ROOT / ".test_smarthome.db"
STORE_FILE = ROOT / ".test_knowledge.json"

for path in (DB_FILE, Path(f"{DB_FILE}-wal"), Path(f"{DB_FILE}-shm"), STORE_FILE):
    if path.exists():
        path.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["KNOWLEDGE_EMBEDDER"] = "hash"          # 不调外部 embedding
os.environ["KNOWLEDGE_VECTOR_BACKEND"] = "json"
os.environ["KNOWLEDGE_STORE_PATH"] = str(STORE_FILE)
os.environ["KNOWLEDGE_QUERY_REWRITE"] = "0"        # 改写需要 LLM，测试里关掉
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # 故意指向不可用端口，验证降级
os.environ["BCRYPT_ROUNDS"] = "4"                  # 测试里不需要 200ms 一次的强度

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:               # 触发 lifespan：建表 + 种子数据
        yield test_client


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture(scope="session")
def home_headers(client):
    return {"Authorization": f"Bearer {login(client, 'user1', 'home123')}"}


@pytest.fixture(scope="session")
def staff_headers(client):
    return {"Authorization": f"Bearer {login(client, 'repair1', 'repair123')}"}


@pytest.fixture(scope="session")
def other_home_headers(client):
    """第二个家庭：用来验证跨家庭访问会被拦住。"""
    from backend.db import SessionLocal
    from backend.models import Home, Member, Role, User, user_roles
    from backend.security import password_hash

    with SessionLocal() as db:
        home = Home(name="另一个家庭")
        db.add(home)
        db.flush()
        user = User(username="outsider", password_hash=password_hash("home123"),
                    display_name="外部用户")
        db.add(user)
        db.flush()
        role = db.scalar(select(Role).where(Role.name == "home_user"))
        db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
        db.add(Member(user_id=user.id, home_id=home.id))
        db.commit()
    return {"Authorization": f"Bearer {login(client, 'outsider', 'home123')}"}


@pytest.fixture()
def device(client, home_headers):
    """每个用例造一台新设备，避免用例之间互相影响。"""
    response = client.post("/api/devices", headers=home_headers, json={
        "name": "测试灯", "room": "测试房", "type": "light", "active": False,
        "value": "40%", "power_consumption": 12, "attributes": {"brightness": 40},
    })
    assert response.status_code == 201, response.text
    return response.json()
