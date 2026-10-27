"""AI 客服 Agent 路由。

工具实现放在这一层，是因为它们需要数据库和当前登录用户；
通用的编排循环、LLM 客户端、工具协议都在 app/services/agent 下。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.device_types import ENUM_LABELS, TYPE_LABELS, normalize_attributes
from app.services.agent.assistant import Assistant
from app.services.agent.llm import DeepSeekClient, LLMError
from app.services.agent.tools import ToolError, ToolRegistry
from backend.api.knowledge import retriever
from backend.db import SessionLocal
from backend.models import Device, User, WorkOrder
from backend.security import current_user, home_for_user, role_name
from backend.services import energy_report

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

llm = DeepSeekClient()


class HistoryItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[HistoryItem] = Field(default_factory=list)
    # 上一轮提出的方案，让模型能在用户说「温度再低点」时基于它修改，而不是从零重来
    last_proposal: Optional[dict[str, Any]] = None


class Citation(BaseModel):
    source: str
    section: str = ""


class ToolCallTrace(BaseModel):
    name: str
    arguments: str
    ok: bool
    result_preview: str = ""


class ProposalAction(BaseModel):
    device_id: int
    device_name: str
    room: str = ""
    action: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    label: str = ""


class SceneProposal(BaseModel):
    name: str
    description: str = ""
    rationale: str = ""
    actions: list[ProposalAction] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    proposal: Optional[SceneProposal] = None
    steps: int = 0
    model: str = ""


def _device_payload(device: Device) -> dict[str, Any]:
    return {"id": device.id, "name": device.name, "room": device.room, "type": device.type,
            "online": device.online, "active": device.active, "value": device.value,
            "power_consumption": device.power_consumption,
            "attributes": device.attributes or {}}


def _order_payload(order: WorkOrder) -> dict[str, Any]:
    return {"id": order.id, "title": order.title, "description": order.description,
            "status": order.status, "priority": order.priority,
            "created_at": order.created_at.isoformat()}


def _match_device(text: str, devices: list[Device]) -> Optional[Device]:
    keyword = (text or "").strip()
    if not keyword:
        return None
    for device in devices:
        if device.name == keyword:
            return device
    for device in devices:
        if keyword in device.name or device.name in keyword:
            return device
    for device in devices:
        label = TYPE_LABELS.get(device.type, "")
        if device.room and device.room in keyword and label and label in keyword:
            return device
    return None


def _action_label(device: Device, act: str, attributes: dict[str, Any]) -> str:
    if act == "off":
        return "关闭"
    parts: list[str] = []
    mode = attributes.get("mode")
    if mode:
        parts.append(ENUM_LABELS.get("mode", {}).get(str(mode), str(mode)))
    if "temperature" in attributes:
        parts.append(f"{attributes['temperature']}°C")
    if "brightness" in attributes:
        parts.append(f"亮度 {attributes['brightness']}%")
    if "volume" in attributes:
        parts.append(f"音量 {attributes['volume']}")
    if "channel" in attributes:
        parts.append(f"频道 {attributes['channel']}")
    return " · ".join(parts) if parts else "开启"


def build_registry(user: User) -> ToolRegistry:
    """按当前登录用户构造工具集——所有查询都只作用于该用户所属的家。"""
    registry = ToolRegistry()

    @registry.register(
        "search_knowledge",
        "在设备手册与常见问题知识库中检索。回答设备故障、使用方法、安全注意事项前必须先调用。",
        {"type": "object",
         "properties": {
             "query": {"type": "string", "description": "检索query，用用户的问题原文或关键词"},
             "top_k": {"type": "integer", "description": "返回条数，默认 3", "default": 3}},
         "required": ["query"]})
    def search_knowledge(query: str, top_k: int = 3) -> dict[str, Any]:
        hits = retriever.search(query, max(1, min(int(top_k or 3), 5)))
        return {"results": [{"content": hit.content, "source": hit.source_doc,
                             "section": hit.section, "score": round(hit.score, 4)} for hit in hits]}

    @registry.register(
        "list_devices",
        "列出当前用户家里的全部设备及其在线／开关状态。",
        {"type": "object", "properties": {}})
    def list_devices() -> dict[str, Any]:
        with SessionLocal() as db:
            home_id = home_for_user(db, user)
            devices = db.scalars(select(Device).where(Device.home_id == home_id).order_by(Device.id))
            return {"devices": [_device_payload(device) for device in devices]}

    @registry.register(
        "get_device_status",
        "按名称或房间查找某个设备的详细状态，例如「客厅空调」「厨房烟雾传感器」。",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "设备名称或房间名"}},
         "required": ["name"]})
    def get_device_status(name: str) -> dict[str, Any]:
        keyword = (name or "").strip().lower()
        with SessionLocal() as db:
            home_id = home_for_user(db, user)
            devices = list(db.scalars(select(Device).where(Device.home_id == home_id)))
        matched = [device for device in devices
                   if keyword and (keyword in device.name.lower() or keyword in device.room.lower())]
        if not matched:
            return {"found": False, "hint": "没有匹配的设备",
                    "available": [device.name for device in devices]}
        return {"found": True, "devices": [_device_payload(device) for device in matched]}

    @registry.register(
        "propose_scene",
        "把方案整理成一套可一键布设的场景（本工具不会立即改变设备）。"
        "用户描述生活困扰（睡不好、空气潮湿黏腻、回家想洗澡、想看电影）或要求你设计一个模式时，"
        "**必须**调用本工具产出结构化方案，不能只在文字里描述。"
        "先调用 list_devices 看清设备，再想清楚动哪些设备、各自调到什么参数。只支持家庭用户。",
        {"type": "object",
         "properties": {
             "name": {"type": "string", "description": "场景名称，4-10 字，如「睡前除湿」"},
             "description": {"type": "string", "description": "一句话说明适用场景"},
             "rationale": {"type": "string", "description": "为什么这样配，一两句给用户看的解释"},
             "actions": {
                 "type": "array",
                 "description": "要执行的动作列表",
                 "items": {
                     "type": "object",
                     "properties": {
                         "device": {"type": "string", "description": "设备名称，必须来自 list_devices 的返回"},
                         "action": {"type": "string", "enum": ["on", "off"]},
                         "attributes": {
                             "type": "object",
                             "description": '目标参数，例如 {"mode":"dry","temperature":26}、'
                                            '{"brightness":20}、{"volume":15}；留空表示只切换开关',
                         },
                     },
                     "required": ["device", "action"],
                 },
             },
         },
         "required": ["name", "description", "actions"]})
    def propose_scene(name: str, description: str, actions: list[dict[str, Any]],
                      rationale: str = "") -> dict[str, Any]:
        with SessionLocal() as db:
            home_id = home_for_user(db, user)
            devices = list(db.scalars(select(Device).where(Device.home_id == home_id)))
        if not devices:
            raise ToolError("当前家庭还没有任何设备，无法设计场景")

        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        for item in actions or []:
            if not isinstance(item, dict):
                continue
            device = _match_device(str(item.get("device", "")), devices)
            if device is None:
                missing.append(str(item.get("device", "")).strip() or "(未填写)")
                continue
            act = "off" if item.get("action") == "off" else "on"
            raw = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            target: dict[str, Any] = {}
            if raw:
                try:
                    cleaned = normalize_attributes(device.type,
                                                   {**(device.attributes or {}), **raw})
                except HTTPException:
                    raise ToolError(f"{device.name} 的参数不合法：{raw}") from None
                # 只保留本次真正要改的键，不要把默认值全写进场景
                target = {key: value for key, value in cleaned.items() if key in raw}
            resolved.append({"device_id": device.id, "device_name": device.name,
                             "room": device.room, "action": act, "attributes": target,
                             "label": _action_label(device, act, target)})

        if not resolved:
            raise ToolError("没有匹配到任何设备，可用设备：" + "、".join(d.name for d in devices))
        return {"proposal": {"name": (name or "AI 方案")[:40],
                             "description": (description or "")[:120],
                             "rationale": (rationale or "")[:200],
                             "actions": resolved, "missing": missing}}

    @registry.register(
        "analyze_energy",
        "统计家庭用电量与设备运行时长（数据来自设备状态历史）。"
        "用户问「电费为什么高」「昨天用电多少」「空调开了多久」「哪个房间最费电」时必须调用。"
        "「今天/昨天」用 period 参数，其它情况用 days。",
        {"type": "object",
         "properties": {
             "period": {"type": "string", "enum": ["today", "yesterday"],
                        "description": "按自然日统计：今天或昨天。用户说「昨天」时必须用这个"},
             "days": {"type": "integer", "description": "最近 N 天，默认 7，范围 1-30"},
             "room": {"type": "string", "description": "只统计某个房间，如 客厅；留空表示全屋"},
         }})
    def analyze_energy(period: str = "", days: int = 7, room: str = "") -> dict[str, Any]:
        with SessionLocal() as db:
            home_id = home_for_user(db, user)
            report = energy_report(db, home_id, days=days, room=room or None,
                                   period=period or None)
        report["note"] = "回答时日期请直接使用 period_label（如果非空），不要自己推算"
        if not report["has_history"]:
            report["hint"] = "这个时间窗内没有设备状态变化记录，无法统计；需要设备被开关过才有数据"
        return report

    @registry.register(
        "list_work_orders",
        "列出当前用户的维修工单及状态。",
        {"type": "object", "properties": {}})
    def list_work_orders() -> dict[str, Any]:
        with SessionLocal() as db:
            role = role_name(db, user)
            query = select(WorkOrder).order_by(WorkOrder.id.desc()).limit(10)
            if role == "home_user":
                query = query.where(WorkOrder.home_id == home_for_user(db, user))
            orders = list(db.scalars(query))
            return {"orders": [_order_payload(order) for order in orders]}

    @registry.register(
        "create_work_order",
        "替当前用户提交一条维修工单。只有当用户明确要求报修、且已说明故障现象时才调用。",
        {"type": "object",
         "properties": {
             "title": {"type": "string", "description": "工单标题，不超过 120 字"},
             "description": {"type": "string", "description": "故障描述，包含房间、设备、现象"},
             "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"],
                          "description": "紧急程度，涉及漏电/漏气/冒烟等安全问题用 urgent"}},
         "required": ["title", "description"]})
    def create_work_order(title: str, description: str, priority: str = "medium") -> dict[str, Any]:
        with SessionLocal() as db:
            if role_name(db, user) != "home_user":
                raise ToolError("当前账号不是家庭用户，无法提交报修")
            home_id = home_for_user(db, user)
            order = WorkOrder(home_id=home_id, created_by=user.id,
                              title=title.strip()[:120], description=description.strip()[:500],
                              priority=priority if priority in {"low", "medium", "high", "urgent"} else "medium")
            db.add(order)
            db.commit()
            db.refresh(order)
            return {"created": True, "order": _order_payload(order)}

    return registry


TOOL_NAMES = ("search_knowledge", "list_devices", "get_device_status", "analyze_energy",
              "list_work_orders", "create_work_order", "propose_scene")


@router.get("/status")
def assistant_status() -> dict[str, Any]:
    return {"configured": llm.configured, "model": llm.model,
            "knowledge_ready": bool(retriever.chunks), "tools": list(TOOL_NAMES)}


@router.post("/chat", response_model=ChatResponse)
def assistant_chat(request: ChatRequest, user: User = Depends(current_user)) -> ChatResponse:
    if not llm.configured:
        raise HTTPException(503, "llm_not_configured")
    assistant = Assistant(llm, build_registry(user))
    history = [item.model_dump() for item in request.history]
    if request.last_proposal:
        # 让模型看到上一版方案，用户说「再低一点」时才能做增量修改
        history.append({"role": "assistant",
                        "content": "我上一轮给出的方案（可以在此基础上修改）："
                                   + json.dumps(request.last_proposal, ensure_ascii=False)})
    try:
        result = assistant.chat(request.message, history)
    except LLMError as exc:
        raise HTTPException(503, str(exc)) from exc
    proposal = result.get("proposal")
    return ChatResponse(answer=result["answer"],
                        citations=[Citation(**item) for item in result["citations"]],
                        tool_calls=[ToolCallTrace(**item) for item in result["tool_calls"]],
                        proposal=SceneProposal(**proposal) if proposal else None,
                        steps=result["steps"], model=llm.model)
