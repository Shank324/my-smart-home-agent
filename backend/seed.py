"""启动期建表与种子数据。"""

from __future__ import annotations

import json

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from backend.db import Base, SessionLocal, engine
from backend.models import (Device, Home, Member, Permission, Role, Scene, SceneAction,
                            User, role_permissions, user_roles)
from backend.security import password_hash

SEED_DEVICES = (
    {"name": "客厅中央空调", "room": "客厅", "type": "air_conditioner", "online": True,
     "active": True, "value": "24°C", "power_consumption": 1500.0,
     "attributes": {"mode": "cool", "temperature": 24}},
    {"name": "客厅氛围灯", "room": "客厅", "type": "light", "online": True,
     "active": False, "value": "40%", "power_consumption": 18.0,
     "attributes": {"brightness": 40}},
    {"name": "客厅电视", "room": "客厅", "type": "tv", "online": True,
     "active": False, "value": "HDMI 1", "power_consumption": 120.0,
     "attributes": {"volume": 35, "channel": 6}},
    {"name": "玄关智能灯", "room": "玄关", "type": "light", "online": True,
     "active": False, "value": "0%", "power_consumption": 12.0,
     "attributes": {"brightness": 0}},
    {"name": "主卧空气传感器", "room": "主卧", "type": "sensor", "online": True,
     "active": True, "value": "42 AQI", "power_consumption": 3.0, "attributes": {}},
    {"name": "主卧台灯", "room": "主卧", "type": "light", "online": True,
     "active": True, "value": "30%", "power_consumption": 9.0,
     "attributes": {"brightness": 30}},
    {"name": "厨房烟雾传感器", "room": "厨房", "type": "sensor", "online": False,
     "active": False, "value": "--", "power_consumption": 2.0, "attributes": {}},
)

PERMISSIONS = ("device:read", "device:write", "scene:read", "scene:write",
               "work_order:read", "work_order:write")

DEMO_ACCOUNTS = (
    ("admin_home", "home123", "管理员家居用户", "home_user"),
    ("repair_staff", "repair123", "演示维修人员", "repair_staff"),
    ("user1", "home123", "家庭用户", "home_user"),
    ("repair1", "repair123", "维修人员", "repair_staff"),
)


def _normalize_scene_actions(db: Session) -> None:
    """把旧的 scenes.actions(JSON) 迁移进 scene_actions 关联表，然后删掉旧列。

    迁移完就 drop，避免出现「同一份数据两个来源」。
    """
    if "actions" not in {column["name"] for column in inspect(engine).get_columns("scenes")}:
        return
    valid_devices = set(db.scalars(select(Device.id)))
    for scene_id, raw in db.execute(text("SELECT id, actions FROM scenes")).fetchall():
        actions = raw
        if isinstance(raw, str):
            try:
                actions = json.loads(raw)
            except json.JSONDecodeError:
                actions = []
        if not isinstance(actions, list) or not actions:
            continue
        existing = set(db.scalars(
            select(SceneAction.device_id).where(SceneAction.scene_id == scene_id)))
        for item in actions:
            if not isinstance(item, dict):
                continue
            device_id = item.get("device_id")
            if not isinstance(device_id, int) or device_id not in valid_devices:
                continue
            if device_id in existing:
                continue
            db.add(SceneAction(scene_id=scene_id, device_id=device_id,
                               action="on" if item.get("action") == "on" else "off"))
            existing.add(device_id)
    db.flush()
    db.execute(text("ALTER TABLE scenes DROP COLUMN actions"))


def _migrate() -> None:
    """轻量补列：让老库能跟上新模型（生产应改用 Alembic）。"""
    columns = {
        "work_orders": {c["name"] for c in inspect(engine).get_columns("work_orders")},
        "devices": {c["name"] for c in inspect(engine).get_columns("devices")},
        "scenes": {c["name"] for c in inspect(engine).get_columns("scenes")},
    }
    with SessionLocal() as db:
        if "assigned_at" not in columns["work_orders"]:
            db.execute(text("ALTER TABLE work_orders ADD COLUMN assigned_at DATETIME NULL"))
        if "updated_at" not in columns["work_orders"]:
            db.execute(text("ALTER TABLE work_orders ADD COLUMN updated_at DATETIME NULL"))
            db.execute(text("UPDATE work_orders SET updated_at = created_at WHERE updated_at IS NULL"))
        if "home_id" not in columns["devices"]:
            db.execute(text("ALTER TABLE devices ADD COLUMN home_id INTEGER NULL"))
        if "home_id" not in columns["scenes"]:
            db.execute(text("ALTER TABLE scenes ADD COLUMN home_id INTEGER NULL"))
        if "power_consumption" not in columns["devices"]:
            db.execute(text("ALTER TABLE devices ADD COLUMN power_consumption FLOAT NOT NULL DEFAULT 0"))
        if "attributes" not in columns["devices"]:
            db.execute(text("ALTER TABLE devices ADD COLUMN attributes JSON NULL"))
        user_columns = {c["name"] for c in inspect(engine).get_columns("users")}
        for column, ddl in (
            ("phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(20) NULL"),
            ("specialty", "ALTER TABLE users ADD COLUMN specialty VARCHAR(100) NULL"),
            ("experience_years", "ALTER TABLE users ADD COLUMN experience_years INTEGER NULL"),
            ("rating", "ALTER TABLE users ADD COLUMN rating FLOAT NULL DEFAULT 5.0"),
            ("available", "ALTER TABLE users ADD COLUMN available BOOLEAN NULL DEFAULT 1"),
        ):
            if column not in user_columns:
                db.execute(text(ddl))
        if "device_id" not in columns["work_orders"]:
            db.execute(text("ALTER TABLE work_orders ADD COLUMN device_id INTEGER NULL"))
            db.execute(text("ALTER TABLE work_orders ADD CONSTRAINT fk_work_orders_device "
                            "FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE SET NULL"))
        scene_action_columns = {c["name"] for c in inspect(engine).get_columns("scene_actions")}
        if "attributes" not in scene_action_columns:
            db.execute(text("ALTER TABLE scene_actions ADD COLUMN attributes JSON NULL"))
        for column, ddl in (
            ("priority", "ALTER TABLE work_orders ADD COLUMN priority VARCHAR(20) NOT NULL DEFAULT 'medium'"),
            ("cost", "ALTER TABLE work_orders ADD COLUMN cost FLOAT NULL"),
            ("duration_minutes", "ALTER TABLE work_orders ADD COLUMN duration_minutes INTEGER NULL"),
            ("resolved_at", "ALTER TABLE work_orders ADD COLUMN resolved_at DATETIME NULL"),
        ):
            if column not in columns["work_orders"]:
                db.execute(text(ddl))
        _normalize_scene_actions(db)
        db.commit()


def seed_database() -> None:
    Base.metadata.create_all(engine)
    _migrate()
    with SessionLocal() as db:
        roles = {}
        for name in ("repair_staff", "home_user"):
            role = db.scalar(select(Role).where(Role.name == name))
            if not role:
                role = Role(name=name)
                db.add(role)
            roles[name] = role
        db.flush()

        permissions = {}
        for name in PERMISSIONS:
            permission = db.scalar(select(Permission).where(Permission.name == name))
            if permission is None:
                permission = Permission(name=name)
                db.add(permission)
                db.flush()
            permissions[name] = permission

        grants = {
            "home_user": PERMISSIONS,
            "repair_staff": ("work_order:read", "work_order:write"),
        }
        for role_key, names in grants.items():
            existing = set(db.scalars(
                select(role_permissions.c.permission_id)
                .where(role_permissions.c.role_id == roles[role_key].id)))
            for name in names:
                if permissions[name].id not in existing:
                    db.execute(role_permissions.insert().values(
                        role_id=roles[role_key].id, permission_id=permissions[name].id))

        home = db.scalar(select(Home).limit(1))
        if not home:
            home = Home(name="演示家庭")
            db.add(home)
            db.flush()
        db.execute(text("UPDATE devices SET home_id = :home_id WHERE home_id IS NULL"), {"home_id": home.id})
        db.execute(text("UPDATE scenes SET home_id = :home_id WHERE home_id IS NULL"), {"home_id": home.id})

        def ensure_user(username: str, password: str, display_name: str, role_key: str) -> User:
            user = db.scalar(select(User).where(User.username == username))
            if user is None:
                user = User(username=username, password_hash=password_hash(password),
                            display_name=display_name,
                            specialty="空调 / 热水器" if role_key == "repair_staff" else None,
                            experience_years=5 if role_key == "repair_staff" else None,
                            phone="13800000000" if role_key == "repair_staff" else None)
                db.add(user)
                db.flush()
            linked = db.scalar(select(user_roles.c.user_id).where(
                user_roles.c.user_id == user.id, user_roles.c.role_id == roles[role_key].id))
            if linked is None:
                db.execute(user_roles.insert().values(user_id=user.id, role_id=roles[role_key].id))
            return user

        for username, password, display_name, role_key in DEMO_ACCOUNTS:
            user = ensure_user(username, password, display_name, role_key)
            if role_key == "home_user" and db.scalar(
                    select(Member.user_id).where(Member.user_id == user.id)) is None:
                db.add(Member(user_id=user.id, home_id=home.id))

        # 幂等补齐演示设备：已存在的按名称复用，缺失的补上，功耗为空的历史数据回填
        existing = {device.name: device for device in
                    db.scalars(select(Device).where(Device.home_id == home.id))}
        for spec in SEED_DEVICES:
            device = existing.get(spec["name"])
            if device is None:
                device = Device(home_id=home.id, **spec)
                db.add(device)
                existing[spec["name"]] = device
                continue
            if not device.power_consumption:
                device.power_consumption = spec["power_consumption"]
            if device.attributes is None:
                device.attributes = spec.get("attributes") or {}
        db.flush()

        if db.scalar(select(Scene.id).limit(1)) is None:
            air_conditioner = existing["客厅中央空调"]
            entry_light = existing["玄关智能灯"]
            db.add_all([
                Scene(home_id=home.id, name="回家", description="开启灯光 · 调节温度", builtin=True,
                      action_rows=[SceneAction(device_id=air_conditioner.id, action="on"),
                                   SceneAction(device_id=entry_light.id, action="on")]),
                Scene(home_id=home.id, name="离家", description="关闭全部设备", builtin=True,
                      action_rows=[SceneAction(device_id=air_conditioner.id, action="off"),
                                   SceneAction(device_id=entry_light.id, action="off")]),
                Scene(home_id=home.id, name="晚安", description="安静模式 · 安全检查", builtin=True,
                      action_rows=[SceneAction(device_id=air_conditioner.id, action="off"),
                                   SceneAction(device_id=entry_light.id, action="off")]),
            ])
        db.commit()
