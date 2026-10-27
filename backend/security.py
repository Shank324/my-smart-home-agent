"""鉴权、角色与数据归属校验。

四层校验：
1. 身份层 —— JWT 是否有效（current_user）
2. 角色层 —— 是否属于允许的角色（require_role）
3. 数据层 —— 是否拥有该家庭的数据（home_for_user / find_device / find_scene）
4. 推送层 —— 广播时再按家庭过滤一次（realtime.visible）

身份不再依赖进程内 token 表：JWT 自带签名和过期时间，多 worker、
多实例下天然一致，也不会因为重启把所有人踢下线。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Optional

import bcrypt
from fastapi import Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import SessionLocal
from backend.models import Device, Member, Role, Scene, User, user_roles
from backend.tokens import user_id_from_token

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def password_hash(password: str) -> str:
    """bcrypt + 每个用户独立随机盐。

    轮数默认 12（约 200ms/次），太重会拖慢登录，太轻扛不住离线爆破；
    测试里用 BCRYPT_ROUNDS=4 把 4 秒的种子时间压到几十毫秒。
    """
    rounds = max(4, int(os.getenv("BCRYPT_ROUNDS", "12")))
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(password: str, stored: str) -> bool:
    """同时兼容 bcrypt 和历史无盐 SHA-256，便于平滑升级。"""
    if not stored:
        return False
    if stored.startswith(_BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("ascii"))
        except ValueError:
            return False
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)


def needs_rehash(stored: str) -> bool:
    """旧格式（或轮数过低）的哈希应该在下次登录时重算。"""
    return not (stored or "").startswith(_BCRYPT_PREFIXES)


def current_user(authorization: Optional[str] = Header(default=None)) -> User:
    with SessionLocal() as db:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "authentication_required")
        user_id = user_id_from_token(authorization[7:].strip())
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
