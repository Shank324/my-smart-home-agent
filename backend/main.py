"""FastAPI 应用入口：装配路由与实时通道。

分层：
- backend.db / models / security / seed —— 基础设施与横切关注点
- backend.api.* —— 各个路由模块
- app.services.agent.* —— 框架无关的 Agent 与检索能力
本文件只负责装配，并保留核心业务路由。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.api.agent import router as agent_router
from backend.api.assistant import router as assistant_router
from backend.api.knowledge import router as knowledge_router
from backend.api.stats import router as stats_router
from backend.db import SessionLocal, engine
from backend.db_objects import ensure_db_objects
from backend.device_types import catalog_payload, derive_status, normalize_attributes
from backend.models import (
    Device,
    Home,
    Member,
    Role,
    Scene,
    User,
    WorkOrder,
    WorkOrderEvent,
    WorkOrderMessage,
    user_roles,
)

# 与请求体里的 Pydantic SceneAction 区分，避免同名覆盖
from backend.models import SceneAction as SceneActionRow
from backend.realtime import CLIENTS, broadcast
from backend.realtime import start as realtime_start
from backend.realtime import stop as realtime_stop
from backend.security import (
    current_user,
    find_device,
    find_scene,
    home_for_user,
    now,
    password_hash,
    require_role,
    role_name,
)
from backend.seed import seed_database
from backend.tokens import create_token, user_id_from_token


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #
class DeviceState(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    room: str
    type: str
    online: bool
    active: bool
    value: str
    power_consumption: float = 0.0
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes", mode="before")
    @classmethod
    def _empty_when_null(cls, value: Any) -> Any:
        return value or {}


class DeviceInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    room: str = Field(min_length=1, max_length=40)
    type: str = Field(min_length=1, max_length=40)
    online: bool = True
    active: bool = False
    value: str = Field(default="0%", max_length=40)
    power_consumption: float = Field(default=0.0, ge=0, le=100000)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CommandRequest(BaseModel):
    action: Literal["on", "off"]


class SceneAction(BaseModel):
    device_id: int
    action: Literal["on", "off"]
    attributes: dict[str, Any] = Field(default_factory=dict)


class SceneInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=120)
    actions: list[SceneAction] = Field(min_length=1)


class SceneResponse(SceneInput):
    id: int
    builtin: bool = False


class SceneExecuteRequest(BaseModel):
    scene_id: Optional[int] = None
    scene: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    home_name: str = Field(default="我的家庭", min_length=1, max_length=80)
    role: Literal["home_user", "repair_staff"] = "home_user"
    # 维修人员档案（role=repair_staff 时使用）
    phone: Optional[str] = Field(default=None, max_length=20)
    specialty: Optional[str] = Field(default=None, max_length=100)
    experience_years: Optional[int] = Field(default=None, ge=0, le=60)


class WorkOrderInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    home_id: Optional[int] = None
    device_id: Optional[int] = None
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class WorkOrderComplete(BaseModel):
    cost: Optional[float] = Field(default=None, ge=0)
    duration_minutes: Optional[int] = Field(default=None, ge=0)


class WorkOrderStatus(BaseModel):
    status: Literal["open", "assigned", "in_progress", "resolved", "cancelled"]


class WorkOrderMessageInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- #
# 序列化与广播
# --------------------------------------------------------------------------- #
def device_payload(device: Device) -> dict[str, Any]:
    return DeviceState.model_validate(device).model_dump()


def scene_payload(scene: Scene) -> dict[str, Any]:
    return SceneResponse(
        id=scene.id, name=scene.name, description=scene.description, builtin=scene.builtin,
        actions=[{"device_id": row.device_id, "action": row.action,
                  "attributes": row.attributes or {}} for row in scene.action_rows],
    ).model_dump()


def apply_target_attributes(device: Device, attributes: dict[str, Any]) -> None:
    """把场景里的目标参数写到设备上，并同步展示用的状态值。"""
    if not attributes:
        return
    try:
        cleaned = normalize_attributes(device.type, {**(device.attributes or {}), **attributes})
    except HTTPException:
        return  # 非法参数不应让整个场景执行失败
    device.attributes = cleaned
    derived = derive_status(device.type, cleaned)
    if derived:
        device.value = derived


@asynccontextmanager
async def lifespan(_: FastAPI):
    seed_database()
    ensure_db_objects()
    # Redis 可用就启用跨实例广播；不可用会退回单实例，不影响启动
    await realtime_start()
    try:
        yield
    finally:
        await realtime_stop()


app = FastAPI(title="SmartHome Ops Platform", version="0.5.0", lifespan=lifespan)
app.include_router(knowledge_router)
app.include_router(assistant_router)
app.include_router(agent_router)
app.include_router(stats_router)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# 健康检查与认证
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "smart-home-ops", "time": now()}


@app.post("/api/auth/login")
def login(request: LoginRequest) -> dict[str, Any]:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == request.username))
        if user is None or user.password_hash != password_hash(request.password) or not user.active:
            raise HTTPException(401, "invalid_credentials")
        token = create_token(user.id)
        return {"access_token": token, "token_type": "bearer",
                "user": {"id": user.id, "username": user.username,
                         "display_name": user.display_name, "role": role_name(db, user),
                         "home_id": db.scalar(select(Member.home_id)
                                              .where(Member.user_id == user.id))}}


@app.post("/api/auth/register", status_code=201)
def register(request: RegisterRequest) -> dict[str, Any]:
    """注册家庭用户或维修人员。维修人员不创建家庭。"""
    with SessionLocal() as db:
        if db.scalar(select(User).where(User.username == request.username)):
            raise HTTPException(409, "username_exists")
        role = db.scalar(select(Role).where(Role.name == request.role))
        if role is None:
            raise HTTPException(500, "role_missing")
        user = User(username=request.username, password_hash=password_hash(request.password),
                    display_name=request.display_name,
                    phone=request.phone, specialty=request.specialty,
                    experience_years=request.experience_years)
        home_id: Optional[int] = None
        try:
            db.add(user)
            db.flush()
            db.execute(user_roles.insert().values(user_id=user.id, role_id=role.id))
            if request.role == "home_user":
                home = Home(name=request.home_name)
                db.add(home)
                db.flush()
                db.add(Member(user_id=user.id, home_id=home.id))
                home_id = home.id
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "username_exists") from None
        return {"id": user.id, "username": user.username, "display_name": user.display_name,
                "role": request.role, "home_id": home_id}


@app.get("/api/technician/me")
def technician_profile(user: User = Depends(current_user)) -> dict[str, Any]:
    """维修人员档案 + 工作台统计。"""
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        orders = list(db.scalars(select(WorkOrder).where(WorkOrder.assigned_to == user.id)))
        available = db.scalar(select(func.count()).select_from(WorkOrder)
                              .where(WorkOrder.assigned_to.is_(None))) or 0
        return {
            "id": user.id, "username": user.username, "display_name": user.display_name,
            "phone": user.phone, "specialty": user.specialty,
            "experience_years": user.experience_years, "rating": user.rating,
            "available": user.available,
            "stats": {
                "total": len(orders),
                "assigned": sum(1 for order in orders if order.status == "assigned"),
                "in_progress": sum(1 for order in orders if order.status == "in_progress"),
                "resolved": sum(1 for order in orders if order.status == "resolved"),
            },
            "available_orders": available,
        }


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        return {"id": user.id, "username": user.username, "display_name": user.display_name,
                "role": role_name(db, user),
                "home_id": db.scalar(select(Member.home_id).where(Member.user_id == user.id))}


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
@app.get("/api/device-types")
def device_type_catalog() -> dict[str, Any]:
    """设备类型目录：前端表单据此渲染枚举与取值区间，避免前后端各写一份。"""
    return catalog_payload()


@app.get("/api/devices", response_model=list[DeviceState])
def list_devices(user: User = Depends(current_user)) -> list[DeviceState]:
    require_role(user, {"home_user"})
    with SessionLocal() as db:
        home_id = home_for_user(db, user)
        return [DeviceState.model_validate(item) for item in db.scalars(
            select(Device).where(Device.home_id == home_id).order_by(Device.id))]


@app.post("/api/devices", response_model=DeviceState, status_code=201)
async def create_device(request: DeviceInput, user: User = Depends(current_user)) -> DeviceState:
    require_role(user, {"home_user"})
    with SessionLocal() as db:
        data = request.model_dump()
        data["attributes"] = normalize_attributes(data["type"], data["attributes"])
        device = Device(home_id=home_for_user(db, user), **data)
        db.add(device)
        db.commit()
        db.refresh(device)
        result = DeviceState.model_validate(device)
        await broadcast("device_updated", result.model_dump(), device.home_id)
        return result


@app.put("/api/devices/{device_id}", response_model=DeviceState)
async def update_device(device_id: int, request: DeviceInput,
                        user: User = Depends(current_user)) -> DeviceState:
    with SessionLocal() as db:
        device = find_device(db, device_id, user)
        payload = request.model_dump()
        payload["attributes"] = normalize_attributes(payload["type"], payload["attributes"])
        for key, value in payload.items():
            setattr(device, key, value)
        db.commit()
        result = DeviceState.model_validate(device)
        await broadcast("device_updated", result.model_dump(), device.home_id)
        return result


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: int, user: User = Depends(current_user)) -> dict[str, object]:
    with SessionLocal() as db:
        device = find_device(db, device_id, user)
        db.delete(device)
        # 显式清理关联动作：SQLite 默认不强制外键，不能只依赖 ON DELETE CASCADE
        db.execute(delete(SceneActionRow).where(SceneActionRow.device_id == device_id))
        db.execute(update(WorkOrder).where(WorkOrder.device_id == device_id).values(device_id=None))
        db.commit()
        return {"deleted": True, "device_id": device_id}


@app.post("/api/devices/{device_id}/commands")
async def command_device(device_id: int, request: CommandRequest,
                         user: User = Depends(current_user)) -> dict[str, object]:
    with SessionLocal() as db:
        device = find_device(db, device_id, user)
        if not device.online:
            raise HTTPException(409, "device_offline")
        device.active = request.action == "on"
        db.commit()
        payload = device_payload(device)
        await broadcast("device_updated", payload, device.home_id)
        return {"accepted": True, "device": payload, "acknowledged_at": now()}


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
@app.get("/api/scenes", response_model=list[SceneResponse])
def list_scenes(user: User = Depends(current_user)) -> list[SceneResponse]:
    require_role(user, {"home_user"})
    with SessionLocal() as db:
        home_id = home_for_user(db, user)
        return [SceneResponse.model_validate(scene_payload(x)) for x in db.scalars(
            select(Scene).where(Scene.home_id == home_id).order_by(Scene.id))]


def validate_scene(db: Session, home_id: int, request: SceneInput) -> None:
    ids = set(db.scalars(select(Device.id).where(Device.home_id == home_id)))
    if any(action.device_id not in ids for action in request.actions):
        raise HTTPException(422, "scene_contains_unknown_device")


@app.post("/api/scenes", response_model=SceneResponse, status_code=201)
def create_scene(request: SceneInput, user: User = Depends(current_user)) -> SceneResponse:
    require_role(user, {"home_user"})
    with SessionLocal() as db:
        home_id = home_for_user(db, user)
        validate_scene(db, home_id, request)
        if db.scalar(select(Scene).where(Scene.home_id == home_id, Scene.name == request.name)):
            raise HTTPException(409, "scene_name_exists")
        scene = Scene(home_id=home_id, name=request.name, description=request.description,
                      action_rows=[SceneActionRow(device_id=action.device_id, action=action.action,
                                                  attributes=action.attributes or {})
                                   for action in request.actions])
        db.add(scene)
        db.commit()
        db.refresh(scene)
        return SceneResponse.model_validate(scene_payload(scene))


@app.put("/api/scenes/{scene_id}", response_model=SceneResponse)
def update_scene(scene_id: int, request: SceneInput,
                 user: User = Depends(current_user)) -> SceneResponse:
    with SessionLocal() as db:
        scene = find_scene(db, user, scene_id)
        validate_scene(db, scene.home_id, request)
        if db.scalar(select(Scene).where(Scene.home_id == scene.home_id,
                                         Scene.name == request.name, Scene.id != scene_id)):
            raise HTTPException(409, "scene_name_exists")
        scene.name = request.name
        scene.description = request.description
        # 差量更新：直接整体替换会撞 (scene_id, device_id) 唯一约束
        desired = {action.device_id: action for action in request.actions}
        current = {row.device_id: row for row in scene.action_rows}
        for device_id, action in desired.items():
            if device_id in current:
                current[device_id].action = action.action
                current[device_id].attributes = action.attributes or {}
            else:
                scene.action_rows.append(SceneActionRow(device_id=device_id, action=action.action,
                                                        attributes=action.attributes or {}))
        for device_id, row in current.items():
            if device_id not in desired:
                scene.action_rows.remove(row)
        db.commit()
        return SceneResponse.model_validate(scene_payload(scene))


@app.delete("/api/scenes/{scene_id}")
def delete_scene(scene_id: int, user: User = Depends(current_user)) -> dict[str, object]:
    with SessionLocal() as db:
        scene = find_scene(db, user, scene_id)
        if scene.builtin:
            raise HTTPException(409, "builtin_scene_cannot_be_deleted")
        db.delete(scene)
        db.commit()
        return {"deleted": True, "scene_id": scene_id}


@app.post("/api/scenes/execute")
async def execute_scene(request: SceneExecuteRequest,
                        user: User = Depends(current_user)) -> dict[str, object]:
    with SessionLocal() as db:
        scene = find_scene(db, user, request.scene_id, request.scene)
        for row in scene.action_rows:
            device = find_device(db, row.device_id, user)
            if device.online and device.type != "sensor":
                device.active = row.action == "on"
                apply_target_attributes(device, row.attributes or {})
        db.commit()
        devices = [device_payload(x) for x in db.scalars(
            select(Device).where(Device.home_id == scene.home_id).order_by(Device.id))]
        return {"accepted": True, "scene": scene_payload(scene),
                "devices": devices, "executed_at": now()}


# --------------------------------------------------------------------------- #
# 工单
# --------------------------------------------------------------------------- #
def work_order_payload(order: WorkOrder) -> dict[str, Any]:
    message_count = getattr(order, "_message_count", None)
    return {"id": order.id, "home_id": order.home_id, "created_by": order.created_by,
            "assigned_to": order.assigned_to, "title": order.title,
            "description": order.description, "status": order.status,
            "priority": order.priority, "cost": order.cost,
            "duration_minutes": order.duration_minutes,
            "device_id": order.device_id,
            "device_name": order.device.name if order.device else None,
            "created_at": order.created_at.isoformat(),
            "assigned_at": order.assigned_at.isoformat() if order.assigned_at else None,
            "resolved_at": order.resolved_at.isoformat() if order.resolved_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "message_count": message_count if message_count is not None else 0}


def work_order_access(db: Session, order: WorkOrder, user: User) -> str:
    role = role_name(db, user)
    if role == "home_user":
        if order.home_id != home_for_user(db, user):
            raise HTTPException(403, "home_access_denied")
        return role
    if role == "repair_staff":
        if order.assigned_to != user.id:
            raise HTTPException(403, "work_order_not_assigned")
        return role
    raise HTTPException(403, "permission_denied")


@app.get("/api/work-orders")
def list_work_orders(user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        role = role_name(db, user)
        query = select(WorkOrder).order_by(WorkOrder.id.desc())
        if role == "home_user":
            query = query.where(WorkOrder.home_id == home_for_user(db, user))
        elif role != "repair_staff":
            raise HTTPException(403, "permission_denied")
        orders = list(db.scalars(query))
        counts = dict(db.execute(
            select(WorkOrderMessage.work_order_id, text("count(*)"))
            .where(WorkOrderMessage.work_order_id.in_([x.id for x in orders]))
            .group_by(WorkOrderMessage.work_order_id)
        ).all()) if orders else {}
        for order in orders:
            order._message_count = counts.get(order.id, 0)
        return [work_order_payload(x) for x in orders]


@app.post("/api/work-orders", status_code=201)
def create_work_order(request: WorkOrderInput,
                      user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        role = role_name(db, user)
        if role == "repair_staff":
            raise HTTPException(403, "home_user_only")
        if role != "home_user":
            raise HTTPException(403, "permission_denied")
        home_id = home_for_user(db, user)
        if request.home_id is not None and request.home_id != home_id:
            raise HTTPException(403, "home_access_denied")
        if request.device_id is not None:
            device = db.get(Device, request.device_id)
            if device is None or device.home_id != home_id:
                raise HTTPException(422, "device_not_in_home")
        order = WorkOrder(home_id=home_id, created_by=user.id,
                          title=request.title, description=request.description,
                          device_id=request.device_id, priority=request.priority)
        db.add(order)
        db.commit()
        db.refresh(order)
        return work_order_payload(order)


@app.post("/api/work-orders/{order_id}/accept")
async def accept_work_order(order_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        if order.assigned_to is not None and order.assigned_to != user.id:
            raise HTTPException(409, "work_order_already_assigned")
        if order.status not in {"open", "assigned"}:
            raise HTTPException(409, "work_order_not_open")
        order.assigned_to = user.id
        order.status = "assigned"
        order.assigned_at = datetime.now(timezone.utc)
        order.updated_at = order.assigned_at
        db.commit()
        payload = work_order_payload(order)
        home_id = order.home_id
    await broadcast("work_order_updated", payload, home_id)
    return payload


@app.put("/api/work-orders/{order_id}/status")
async def update_work_order_status(order_id: int, request: WorkOrderStatus,
                                   user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        if order.assigned_to != user.id:
            raise HTTPException(403, "work_order_not_assigned")
        order.status = request.status
        order.updated_at = datetime.now(timezone.utc)
        db.commit()
        payload = work_order_payload(order)
        home_id = order.home_id
    await broadcast("work_order_updated", payload, home_id)
    return payload


@app.put("/api/work-orders/{order_id}")
async def update_work_order(order_id: int, request: WorkOrderStatus,
                            user: User = Depends(current_user)) -> dict[str, Any]:
    return await update_work_order_status(order_id, request, user)


@app.put("/api/work-orders/{order_id}/completion")
async def complete_work_order(order_id: int, request: WorkOrderComplete,
                              user: User = Depends(current_user)) -> dict[str, Any]:
    """完工登记：记录实际费用与工时，并把工单置为 resolved。"""
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        if order.assigned_to != user.id:
            raise HTTPException(403, "work_order_not_assigned")
        if request.cost is not None:
            order.cost = request.cost
        if request.duration_minutes is not None:
            order.duration_minutes = request.duration_minutes
        order.status = "resolved"
        order.updated_at = datetime.now(timezone.utc)
        # MySQL 下由触发器统一写入 resolved_at，这里兜底无触发器的环境（SQLite）
        if order.resolved_at is None:
            order.resolved_at = datetime.now(timezone.utc)
        db.commit()
        payload = work_order_payload(order)
        home_id = order.home_id
    await broadcast("work_order_updated", payload, home_id)
    return payload


@app.get("/api/work-orders/{order_id}/timeline")
def work_order_timeline(order_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    """工单状态流转时间线——记录由数据库触发器写入，应用层不负责记日志。"""
    with SessionLocal() as db:
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        role = role_name(db, user)
        if role == "home_user":
            if order.home_id != home_for_user(db, user):
                raise HTTPException(403, "home_access_denied")
        elif role == "repair_staff":
            if order.assigned_to != user.id:
                raise HTTPException(403, "work_order_not_assigned")
        else:
            raise HTTPException(403, "permission_denied")
        events = db.execute(
            select(WorkOrderEvent).where(WorkOrderEvent.work_order_id == order_id)
            .order_by(WorkOrderEvent.id)
        ).scalars().all()
        return {"order_id": order_id, "current_status": order.status,
                "trigger_backed": engine.dialect.name == "mysql",
                "events": [{"from": event.from_status, "to": event.to_status,
                            "actor_id": event.actor_id,
                            "at": event.created_at.isoformat()} for event in events]}


@app.get("/api/work-orders/{order_id}/messages")
def list_work_order_messages(order_id: int,
                             user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        work_order_access(db, order, user)
        return [{"id": item.id, "work_order_id": item.work_order_id,
                 "sender_id": item.sender_id, "message": item.message,
                 "created_at": item.created_at.isoformat()}
                for item in db.scalars(select(WorkOrderMessage)
                                       .where(WorkOrderMessage.work_order_id == order_id)
                                       .order_by(WorkOrderMessage.id))]


@app.post("/api/work-orders/{order_id}/messages", status_code=201)
async def create_work_order_message(order_id: int, request: WorkOrderMessageInput,
                                    user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        work_order_access(db, order, user)
        item = WorkOrderMessage(work_order_id=order.id, sender_id=user.id,
                                message=request.message.strip())
        if not item.message:
            raise HTTPException(422, "message_required")
        db.add(item)
        order.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(item)
        payload = {"id": item.id, "work_order_id": item.work_order_id,
                   "sender_id": item.sender_id, "message": item.message,
                   "created_at": item.created_at.isoformat()}
        home_id = order.home_id
    await broadcast("work_order_message", payload, home_id)
    return payload


@app.delete("/api/work-orders/{order_id}")
def delete_work_order(order_id: int, user: User = Depends(current_user)) -> dict[str, object]:
    with SessionLocal() as db:
        if role_name(db, user) != "repair_staff":
            raise HTTPException(403, "repair_staff_only")
        order = db.get(WorkOrder, order_id)
        if order is None:
            raise HTTPException(404, "work_order_not_found")
        # 只能删自己接的单：否则任何维修人员都能删掉别人的工单，甚至删掉还没被接的单
        if order.assigned_to != user.id:
            raise HTTPException(403, "work_order_not_assigned")
        db.delete(order)
        db.commit()
        return {"deleted": True, "work_order_id": order_id}


# --------------------------------------------------------------------------- #
# 实时通道
# --------------------------------------------------------------------------- #
@app.websocket("/ws/events")
async def events(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    with SessionLocal() as db:
        user_id = user_id_from_token(token)
        user = db.get(User, user_id) if user_id else None
        role = role_name(db, user) if user else None
        if user is not None and not user.active:
            user = None
        if user is not None and role == "home_user":
            home_id = db.scalar(select(Member.home_id).where(Member.user_id == user.id))
        elif user is not None and role == "repair_staff":
            home_id = -1
        else:
            home_id = None
    if home_id is None:
        # 在 accept 之前关闭，客户端能拿到确定的握手失败，而不是莫名断开
        await websocket.close(code=1008)
        return
    await websocket.accept()
    CLIENTS[websocket] = home_id
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        CLIENTS.pop(websocket, None)
