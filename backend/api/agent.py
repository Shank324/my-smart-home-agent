"""Agent 会话路由。

相比无状态的 /api/assistant/chat，这里多了四件事：

1. **会话持久化**：对话落库，刷新/换设备/重启都不丢
2. **意图路由**：四条流（咨询 / 报修 / 方案 / 控制），规则优先、模型兜底
3. **可恢复中断**：Agent 提出方案后把待确认动作写进 `agent_sessions.pending_action`，
   状态置为 pending_confirm；用户确认时从库里恢复并执行。
   与 LangGraph 的 checkpointer 等价，区别是存数据库——跨实例、可审计、可统计。
4. **护栏**：输入侧拦领域外请求，输出侧做落地校验
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from app.services.agent.assistant import Assistant
from app.services.agent.llm import LLMError
from backend.agent_guard import check_grounding, check_input, classify_intent
from backend.api.assistant import build_registry, llm
from backend.db import SessionLocal
from backend.models import AgentMessage, AgentSession, Device, User
from backend.security import current_user, home_for_user, role_name
from backend.services import create_scene_from_actions, create_work_order_from_proposal, pending_payload

router = APIRouter(prefix="/api/assistant/sessions", tags=["agent"])

MAX_HISTORY_MESSAGES = 10
REFUSAL_TEXT = ("我只处理智能家居相关的问题：设备怎么用、场景怎么配、报修怎么提交。"
                "要不要换个和家里设备有关的问题？")


class SessionCreate(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _session_or_404(db, session_id: int, user: User) -> AgentSession:
    conversation = db.get(AgentSession, session_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(404, "session_not_found")
    return conversation


def _history(db, session_id: int) -> list[dict[str, str]]:
    rows = list(db.scalars(select(AgentMessage).where(AgentMessage.session_id == session_id)
                           .order_by(AgentMessage.id.desc()).limit(MAX_HISTORY_MESSAGES)))
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


def _device_names(db, user: User) -> list[str]:
    try:
        home_id = home_for_user(db, user)
    except HTTPException:
        return []
    return [device.name for device in db.scalars(
        select(Device).where(Device.home_id == home_id))]


def _session_payload(conversation: AgentSession) -> dict[str, Any]:
    return {"id": conversation.id, "title": conversation.title, "status": conversation.status,
            "pending_action": pending_payload(conversation.pending_action) or None,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat()}


def _message_payload(row: AgentMessage) -> dict[str, Any]:
    return {"id": row.id, "role": row.role, "content": row.content, "intent": row.intent,
            "proposal": row.proposal, "citations": row.citations or [],
            "tool_calls": row.tool_calls or [], "grounding": row.grounding,
            "created_at": row.created_at.isoformat()}


@router.post("", status_code=201)
def create_session(request: SessionCreate, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        conversation = AgentSession(user_id=user.id,
                                    title=(request.title or "新对话")[:120])
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return _session_payload(conversation)


@router.get("")
def list_sessions(user: User = Depends(current_user)) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = list(db.scalars(select(AgentSession).where(AgentSession.user_id == user.id)
                               .order_by(AgentSession.updated_at.desc()).limit(30)))
        out = []
        for conversation in rows:
            payload = _session_payload(conversation)
            payload["message_count"] = db.scalar(
                select(func.count()).select_from(AgentMessage)
                .where(AgentMessage.session_id == conversation.id)) or 0
            out.append(payload)
        return out


@router.get("/{session_id}")
def get_session(session_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        conversation = _session_or_404(db, session_id, user)
        messages = list(db.scalars(select(AgentMessage)
                                   .where(AgentMessage.session_id == conversation.id)
                                   .order_by(AgentMessage.id)))
        payload = _session_payload(conversation)
        payload["messages"] = [_message_payload(row) for row in messages]
        return payload


@router.delete("/{session_id}")
def delete_session(session_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        conversation = _session_or_404(db, session_id, user)
        db.execute(delete(AgentMessage).where(AgentMessage.session_id == conversation.id))
        db.delete(conversation)
        db.commit()
        return {"deleted": True, "session_id": session_id}


def _run_turn(session_id: int, message: str, user: User,
              emit: Optional[Callable[[str, dict[str, Any]], None]] = None) -> dict[str, Any]:
    """跑一轮对话：护栏 → 意图 → Agent → 落地校验 → 挂起。

    emit 用于流式推送（meta / delta），事件在 worker 线程里产生，
    调用方负责把它安全地送到事件循环。
    """
    with SessionLocal() as db:
        conversation = _session_or_404(db, session_id, user)
        history = _history(db, conversation.id)

        db.add(AgentMessage(session_id=conversation.id, role="user", content=message))

        # 1) 输入护栏
        allowed, guard_reason = check_input(message, llm)
        if not allowed:
            db.add(AgentMessage(session_id=conversation.id, role="assistant",
                                content=REFUSAL_TEXT,
                                grounding={"grounded": True, "warnings": [], "blocked": True}))
            db.commit()
            return {"session_id": conversation.id, "status": conversation.status,
                    "answer": REFUSAL_TEXT, "intent": None, "intent_source": None,
                    "blocked": True, "reason": guard_reason, "citations": [],
                    "proposal": None, "grounded": True, "warnings": [],
                    "pending_action": pending_payload(conversation.pending_action) or None}

        # 2) 意图路由
        intent, intent_source = classify_intent(message, llm)
        if emit is not None:
            emit("meta", {"intent": intent, "intent_source": intent_source})

        # 3) 跑一轮 Agent
        assistant = Assistant(llm, build_registry(user))
        def on_token(text: str) -> None:
            if emit is not None:
                emit("delta", {"text": text})

        try:
            result = assistant.chat(message, history,
                                    on_token=on_token if emit is not None else None)
        except LLMError as exc:
            raise HTTPException(503, str(exc)) from exc

        # 4) 输出护栏：落地校验
        grounding = check_grounding(result["answer"], result.get("evidence", ""), message,
                                    _device_names(db, user))

        # 5) 有待确认动作就挂起，等用户确认后再落库
        proposal = result.get("proposal")
        work_order = result.get("work_order")
        if proposal:
            conversation.pending_action = {"type": "scene", "payload": proposal,
                                           "summary": proposal.get("name")}
            conversation.status = "pending_confirm"
        elif work_order:
            conversation.pending_action = {"type": "work_order", "payload": work_order,
                                           "summary": work_order.get("title")}
            conversation.status = "pending_confirm"
        if conversation.title in ("", "新对话"):
            conversation.title = message[:40]

        db.add(AgentMessage(session_id=conversation.id, role="assistant",
                            content=result["answer"], intent=intent, proposal=proposal,
                            citations=result["citations"], tool_calls=result["tool_calls"],
                            grounding=grounding))
        db.commit()
        return {"session_id": conversation.id, "status": conversation.status,
                "answer": result["answer"], "intent": intent, "intent_source": intent_source,
                "blocked": False, "citations": result["citations"], "proposal": proposal,
                "work_order": work_order,
                "grounded": grounding["grounded"], "warnings": grounding["warnings"],
                "numeric_unverified": grounding.get("numeric_unverified", []),
                "steps": result.get("steps", 0),
                "pending_action": pending_payload(conversation.pending_action) or None}


@router.post("/{session_id}/messages")
def send_message(session_id: int, request: TurnRequest,
                 user: User = Depends(current_user)) -> dict[str, Any]:
    return _run_turn(session_id, request.message.strip(), user)


@router.post("/{session_id}/messages/stream")
async def stream_message(session_id: int, request: TurnRequest,
                         user: User = Depends(current_user)) -> StreamingResponse:
    """流式版本：先推 meta（意图），再逐段推 delta（正文），最后推 done（完整结果）。

    Agent 执行是同步阻塞的（urllib + 数据库），所以丢到线程里跑，
    事件用 `call_soon_threadsafe` 送回事件循环，避免卡住整个 worker。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Optional[tuple[str, dict[str, Any]]]] = asyncio.Queue()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def worker() -> None:
        try:
            payload = _run_turn(session_id, request.message.strip(), user, emit=emit)
            emit("done", payload)
        except HTTPException as exc:
            emit("error", {"detail": exc.detail, "status": exc.status_code})
        except Exception as exc:                            # noqa: BLE001
            emit("error", {"detail": str(exc)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    task = asyncio.create_task(asyncio.to_thread(worker))

    async def generate():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            await task

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      # 让 Nginx 不要缓冲，否则流式会变成一次性吐出
                                      "X-Accel-Buffering": "no"})


@router.post("/{session_id}/confirm")
def confirm_pending(session_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    """恢复挂起的动作并执行——这就是本项目版本的 HITL。"""
    with SessionLocal() as db:
        conversation = _session_or_404(db, session_id, user)
        pending = pending_payload(conversation.pending_action)
        if not pending:
            raise HTTPException(409, "nothing_to_confirm")
        kind = pending.get("type")
        payload = pending.get("payload") or {}
        if role_name(db, user) != "home_user":
            raise HTTPException(403, "home_user_only")
        home_id = home_for_user(db, user)

        data: dict[str, Any] = {}
        if kind == "scene":
            try:
                scene = create_scene_from_actions(db, home_id, payload)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            text = (f"已布设场景「{scene.name}」，共 {len(scene.action_rows)} 个动作，"
                    f"可在「自动化场景」里执行或修改。")
            data = {"id": scene.id, "name": scene.name,
                    "action_count": len(scene.action_rows)}
        elif kind == "work_order":
            order = create_work_order_from_proposal(db, home_id, user.id, payload)
            priority_text = {"low": "低", "medium": "普通", "high": "高", "urgent": "紧急"}
            text = (f"已提交报修工单 #{order.id}「{order.title}」，"
                    f"优先级为{priority_text.get(order.priority, order.priority)}。"
                    f"维修人员会在工单池看到它，接单后会在这里联系你。")
            data = {"id": order.id, "title": order.title,
                    "status": order.status, "priority": order.priority}
        else:
            raise HTTPException(422, "unsupported_pending_action")

        conversation.pending_action = None
        conversation.status = "active"
        db.add(AgentMessage(session_id=conversation.id, role="assistant", content=text,
                            grounding={"grounded": True, "warnings": []}))
        db.commit()
        return {"session_id": conversation.id, "status": conversation.status,
                "message": text, "kind": kind, "result": data}


@router.post("/{session_id}/cancel")
def cancel_pending(session_id: int, user: User = Depends(current_user)) -> dict[str, Any]:
    with SessionLocal() as db:
        conversation = _session_or_404(db, session_id, user)
        pending = pending_payload(conversation.pending_action)
        if not pending:
            raise HTTPException(409, "nothing_to_cancel")
        name = pending.get("summary") or "该方案"
        conversation.pending_action = None
        conversation.status = "active"
        text = f"已取消「{name}」，没有做任何改动。"
        db.add(AgentMessage(session_id=conversation.id, role="assistant", content=text,
                            grounding={"grounded": True, "warnings": []}))
        db.commit()
        return {"session_id": conversation.id, "status": conversation.status, "message": text}
