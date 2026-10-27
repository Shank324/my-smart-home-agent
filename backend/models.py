"""SQLAlchemy ORM 模型。

所有表集中在这里定义，业务代码只依赖本模块，不依赖 `backend.main`。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db import Base

role_permissions = Table(
    "role_permissions", Base.metadata,
    Column("role_id", ForeignKey("roles.id"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id"), primary_key=True),
)
user_roles = Table(
    "user_roles", Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("role_id", ForeignKey("roles.id"), primary_key=True),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True)
    name = Column(String(40), unique=True, nullable=False)


class Permission(Base):
    __tablename__ = "permissions"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    display_name = Column(String(80), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    # 维修人员档案（家庭用户留空）
    phone = Column(String(20), nullable=True)
    specialty = Column(String(100), nullable=True)
    experience_years = Column(Integer, nullable=True)
    rating = Column(Float, default=5.0, nullable=True)
    available = Column(Boolean, default=True, nullable=True)


class Home(Base):
    __tablename__ = "homes"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)


class Member(Base):
    __tablename__ = "members"
    user_id = Column(ForeignKey("users.id"), primary_key=True)
    home_id = Column(ForeignKey("homes.id"), primary_key=True)


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    home_id = Column(ForeignKey("homes.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    room = Column(String(40), nullable=False)
    type = Column(String(40), nullable=False)
    online = Column(Boolean, default=True, nullable=False)
    active = Column(Boolean, default=False, nullable=False)
    value = Column(String(40), default="0%", nullable=False)
    # 额定功耗（瓦），用于能耗统计视图
    power_consumption = Column(Float, default=0.0, nullable=False)
    # 类型化参数：空调存 mode/temperature，电视存 volume/channel……
    # 稀疏、多态、从不参与 JOIN，所以用 JSON；写入前由 backend.device_types 校验
    attributes = Column(JSON, nullable=True)


class Scene(Base):
    __tablename__ = "scenes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    home_id = Column(ForeignKey("homes.id"), nullable=False, index=True)
    name = Column(String(40), nullable=False)
    description = Column(String(120), default="", nullable=False)
    builtin = Column(Boolean, default=False, nullable=False)
    # actions 原本是 JSON 列，无法用 SQL 聚合、也无法约束设备必须存在，
    # 现规范化为 scene_actions 关联表（见 SceneAction）。
    action_rows = relationship(
        "SceneAction", back_populates="scene", lazy="selectin",
        cascade="all, delete-orphan", order_by="SceneAction.id")


class SceneAction(Base):
    """场景动作：场景与设备的多对多关联，附带目标状态。"""

    __tablename__ = "scene_actions"
    __table_args__ = (
        UniqueConstraint("scene_id", "device_id", name="uq_scene_actions_scene_device"),
        Index("idx_scene_actions_device", "device_id"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    scene_id = Column(ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id = Column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(10), nullable=False)
    # 目标参数，如 {"mode": "dry", "temperature": 26}；为空表示只切换开关
    attributes = Column(JSON, nullable=True)

    scene = relationship("Scene", back_populates="action_rows")


class WorkOrder(Base):
    __tablename__ = "work_orders"
    __table_args__ = (
        Index("idx_work_orders_home_status", "home_id", "status"),
        Index("idx_work_orders_assigned_to", "assigned_to"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    home_id = Column(ForeignKey("homes.id"), nullable=False, index=True)
    created_by = Column(ForeignKey("users.id"), nullable=False)
    assigned_to = Column(ForeignKey("users.id"), nullable=True)
    # 关联到具体设备（可选）：报修时能指明是哪台设备出问题
    device_id = Column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    device = relationship("Device", lazy="joined")
    title = Column(String(120), nullable=False)
    description = Column(String(500), default="", nullable=False)
    status = Column(String(30), default="open", nullable=False)
    priority = Column(String(20), default="medium", nullable=False)
    cost = Column(Float, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    assigned_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class WorkOrderEvent(Base):
    """工单状态流转日志。

    由数据库触发器在状态变化时写入，保证「无论从哪条代码路径改状态都会留痕」。
    """

    __tablename__ = "work_order_events"
    __table_args__ = (Index("idx_work_order_events_order", "work_order_id", "id"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    work_order_id = Column(ForeignKey("work_orders.id"), nullable=False)
    from_status = Column(String(30), nullable=False)
    to_status = Column(String(30), nullable=False)
    actor_id = Column(ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class WorkOrderMessage(Base):
    __tablename__ = "work_order_messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    work_order_id = Column(ForeignKey("work_orders.id"), nullable=False, index=True)
    sender_id = Column(ForeignKey("users.id"), nullable=False)
    message = Column(String(2000), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class DeviceEvent(Base):
    """设备状态变化历史，由数据库触发器写入。

    智能家居只存「当前状态」是设计缺陷：没有时间序列就无法回答
    「昨天客厅空调开了多久、耗了多少电」。触发器保证任何一条 UPDATE 都留痕，
    应用层不需要（也无法）记得每条修改路径。
    """

    __tablename__ = "device_events"
    __table_args__ = (Index("idx_device_events_device", "device_id", "id"),
                      Index("idx_device_events_home_time", "home_id", "created_at"))
    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    home_id = Column(Integer, nullable=False)
    powered = Column(Boolean, default=False, nullable=False)
    value = Column(String(40), default="", nullable=False)
    power_w = Column(Float, default=0.0, nullable=False)
    attributes = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AgentSession(Base):
    """一次 Agent 对话会话。

    会话状态落库而不是落文件：跨实例、可审计、可统计（例如确认放弃率）。
    """

    __tablename__ = "agent_sessions"
    __table_args__ = (Index("idx_agent_sessions_user", "user_id", "updated_at"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(ForeignKey("users.id"), nullable=False)
    title = Column(String(120), default="新对话", nullable=False)
    # active | pending_confirm | closed
    status = Column(String(20), default="active", nullable=False)
    # 待用户确认的动作：{"type": "scene", "payload": {...}, "summary": "..."}
    pending_action = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (Index("idx_agent_messages_session", "session_id", "id"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)          # user | assistant
    content = Column(String(4000), nullable=False)
    intent = Column(String(20), nullable=True)
    proposal = Column(JSON, nullable=True)
    citations = Column(JSON, nullable=True)
    tool_calls = Column(JSON, nullable=True)
    grounding = Column(JSON, nullable=True)            # {"grounded": bool, "warnings": [...]}
    created_at = Column(DateTime, default=utcnow, nullable=False)
