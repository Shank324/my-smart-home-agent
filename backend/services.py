"""会话层的业务动作：把 Agent 提出的待确认动作落成真实数据。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device, Scene, SceneAction, WorkOrder


def unique_scene_name(db: Session, home_id: int, name: str) -> str:
    base = (name or "AI 场景").strip()[:36] or "AI 场景"
    candidate = base
    index = 2
    while db.scalar(select(Scene.id).where(Scene.home_id == home_id, Scene.name == candidate)):
        candidate = f"{base} {index}"[:40]
        index += 1
    return candidate


def create_scene_from_actions(db: Session, home_id: int, payload: dict[str, Any]) -> Scene:
    """把草案落成场景。动作里的 device_id 必须属于该家庭。"""
    actions = payload.get("actions") or []
    valid_ids = set(db.scalars(select(Device.id).where(Device.home_id == home_id)))
    rows = []
    for item in actions:
        device_id = item.get("device_id")
        if device_id not in valid_ids:
            continue
        rows.append(SceneAction(device_id=int(device_id),
                                action="off" if item.get("action") == "off" else "on",
                                attributes=item.get("attributes") or {}))
    if not rows:
        raise ValueError("场景里没有可用设备")
    scene = Scene(home_id=home_id,
                  name=unique_scene_name(db, home_id, payload.get("name") or "AI 场景"),
                  description=(payload.get("description") or payload.get("rationale") or "")[:120],
                  builtin=False, action_rows=rows)
    db.add(scene)
    db.commit()
    db.refresh(scene)
    return scene


def create_work_order_from_proposal(db: Session, home_id: int, user_id: int,
                                    payload: dict[str, Any]) -> WorkOrder:
    order = WorkOrder(home_id=home_id, created_by=user_id,
                      title=(payload.get("title") or "AI 代建工单")[:120],
                      description=(payload.get("description") or "")[:500],
                      priority=payload.get("priority") if payload.get("priority") in
                      {"low", "medium", "high", "urgent"} else "medium",
                      device_id=payload.get("device_id"))
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def scene_summary(scene: Scene) -> dict[str, Any]:
    return {"id": scene.id, "name": scene.name, "description": scene.description,
            "actions": [{"device_id": row.device_id, "action": row.action,
                         "attributes": row.attributes or {}} for row in scene.action_rows]}


def pending_payload(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}
