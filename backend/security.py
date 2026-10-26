"""鉴权、角色与数据归属校验。

三层校验：
1. 身份层 —— bearer token 是否有效（current_user）
2. 角色层 —— 是否属于允许的角色（require_role）
3. 数据层 —— 是否拥有该家庭的数据（home_for_user / find_device / find_scene）
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import SessionLocal
from backend.models import Device, Member, Role, Scene, User, user_roles

# 演示用：进程内 token 表。生产应换成 JWT 或 Redis 存储（见 README Roadmap）
TOKENS: dict[str, int] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def password_hash(password: str) -> str:
    """演示实现：无盐 SHA-256。生产必须换成 bcrypt/argon2。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def current_user(authorization: Optional[str] = Header(default=None)) -> User:
    with SessionLocal() as db:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "authentication_required")
        user_id = TOKENS.get(authorization[7:].strip())
        user = db.get(User, user_id) if user_id else None
        if user is None or not user.active:
            raise HTTPException(401, "invalid_token")
        return user


def role_name(db: Session, user: User) -> Optional[str]:
    return db.scalar(
        select(Role.name).join(user_roles, Role.id == user_roles.c.role_id)
        .where(user_roles.c.user_id == user.id)
    )


def require_role(user: User, allowed: set[str]) -> tuple[User, str]:
    with SessionLocal() as db:
        role = role_name(db, user)
    if role not in allowed:
        raise HTTPException(403, "permission_denied")
    return user, role


def home_for_user(db: Session, user: User) -> int:
    home_id = db.scalar(select(Member.home_id).where(Member.user_id == user.id))
    if home_id is None:
        raise HTTPException(403, "home_access_denied")
    return home_id


def find_device(db: Session, device_id: int, user: User) -> Device:
    require_role(user, {"home_user"})
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(404, "device_not_found")
    if device.home_id != home_for_user(db, user):
        raise HTTPException(403, "home_access_denied")
    return device


def find_scene(db: Session, user: User, scene_id: Optional[int] = None,
               name: Optional[str] = None) -> Scene:
    require_role(user, {"home_user"})
    home_id = home_for_user(db, user)
    scene = db.scalar(select(Scene).where(
        Scene.home_id == home_id,
        Scene.id == scene_id if scene_id is not None else Scene.name == name,
    ))
    if scene is None:
        raise HTTPException(404, "scene_not_found")
    return scene
