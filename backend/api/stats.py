"""统计接口：演示四类数据库能力。

1. 多表连接聚合（Home ⨝ Device）
2. 嵌套子查询（HAVING 里再聚合）
3. 自连接（同房间不同类型设备）
4. 视图查询（MySQL 有视图时走视图，SQLite 自动降级为等价 ORM 查询）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.orm import Session, aliased

from backend.db import SessionLocal, engine
from backend.models import Device, Home, Scene, SceneAction, User, WorkOrder
from backend.security import current_user, home_for_user, require_role, role_name

router = APIRouter(prefix="/api/stats", tags=["stats"])

HAS_VIEWS = engine.dialect.name == "mysql"


def _scalar_row(db: Session, home_id: int) -> dict[str, Any]:
    """1. 多表连接 + 条件聚合：一次查出设备总数/在线数/开启数/功耗。"""
    row = db.execute(
        select(
            Home.name.label("home_name"),
            func.count(Device.id).label("device_total"),
            func.coalesce(func.sum(case((Device.online.is_(True), 1), else_=0)), 0).label("device_online"),
            func.coalesce(func.sum(case((Device.active.is_(True), 1), else_=0)), 0).label("device_active"),
            func.coalesce(func.sum(Device.power_consumption), 0).label("power_total"),
            func.coalesce(func.sum(case((Device.active.is_(True), Device.power_consumption), else_=0)), 0)
            .label("power_active"),
        )
        .join(Device, Device.home_id == Home.id)
        .where(Home.id == home_id)
        .group_by(Home.id, Home.name)
    ).mappings().first()
    return dict(row) if row else {"home_name": "", "device_total": 0, "device_online": 0,
                                  "device_active": 0, "power_total": 0, "power_active": 0}


def _rooms_above_average(db: Session, home_id: int) -> list[dict[str, Any]]:
    """2. 嵌套子查询：设备数高于本家庭「房间均值」的房间。"""
    counts = (select(func.count(Device.id).label("cnt"))
              .where(Device.home_id == home_id)
              .group_by(Device.room)
              .subquery())
    average = select(func.coalesce(func.avg(counts.c.cnt), 0)).scalar_subquery()
    rows = db.execute(
        select(Device.room, func.count(Device.id).label("device_count"))
        .where(Device.home_id == home_id)
        .group_by(Device.room)
        .having(func.count(Device.id) > average)
        .order_by(func.count(Device.id).desc())
    ).mappings().all()
    return [dict(row) for row in rows]


def _cross_type_rooms(db: Session, home_id: int) -> list[dict[str, Any]]:
    """3. 自连接：同一个房间内存在不同类型设备，且类型对只出现一次。"""
    left = aliased(Device)
    right = aliased(Device)
    rows = db.execute(
        select(left.room, left.name.label("device_a"), left.type.label("type_a"),
               right.name.label("device_b"), right.type.label("type_b"))
        .join(right, and_(left.room == right.room, left.type < right.type))
        .where(left.home_id == home_id, right.home_id == home_id)
        .order_by(left.room)
    ).mappings().all()
    return [dict(row) for row in rows]


def _home_view(db: Session, home_id: int) -> dict[str, Any]:
    """4. 视图查询：v_home_device_stats。"""
    row = db.execute(text("SELECT * FROM v_home_device_stats WHERE home_id = :h"),
                     {"h": home_id}).mappings().first()
    return dict(row) if row else {}


def _top_scene(db: Session, home_id: int) -> dict[str, Any] | None:
    """聚合查询：动作数最多的场景（关联表规范化后可以直接用 SQL 排序）。"""
    row = db.execute(
        select(Scene.id, Scene.name, Scene.description,
               func.count(SceneAction.id).label("action_count"))
        .join(SceneAction, SceneAction.scene_id == Scene.id)
        .where(Scene.home_id == home_id)
        .group_by(Scene.id, Scene.name, Scene.description)
        .order_by(func.count(SceneAction.id).desc(), Scene.id)
        .limit(1)
    ).mappings().first()
    return dict(row) if row else None


@router.get("/overview")
def overview(user: User = Depends(current_user)) -> dict[str, Any]:
    require_role(user, {"home_user"})
    with SessionLocal() as db:
        home_id = home_for_user(db, user)
        return {
            "device_stats": _scalar_row(db, home_id),
            "rooms_above_average": _rooms_above_average(db, home_id),
            "cross_type_rooms": _cross_type_rooms(db, home_id),
            "top_scene": _top_scene(db, home_id),
            "view": _home_view(db, home_id) if HAS_VIEWS else None,
            "view_source": "database_view" if HAS_VIEWS else "orm_fallback",
        }


@router.get("/technicians")
def technician_ranking(user: User = Depends(current_user)) -> dict[str, Any]:
    """视图 v_technician_stats：维修人员的接单量/完成量/平均耗时。"""
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        if HAS_VIEWS:
            rows = db.execute(text(
                "SELECT * FROM v_technician_stats ORDER BY assigned_total DESC"
            )).mappings().all()
            return {"source": "database_view", "rows": [dict(row) for row in rows]}
        rows = db.execute(
            select(User.username, User.display_name,
                   func.count(WorkOrder.id).label("assigned_total"),
                   func.coalesce(func.sum(case((WorkOrder.status == "resolved", 1), else_=0)), 0)
                   .label("resolved_total"))
            .outerjoin(WorkOrder, WorkOrder.assigned_to == User.id)
            .where(User.id == user.id)
            .group_by(User.id, User.username, User.display_name)
        ).mappings().all()
        return {"source": "orm_fallback", "rows": [dict(row) for row in rows]}
