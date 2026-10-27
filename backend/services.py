"""会话层的业务动作：把 Agent 提出的待确认动作落成真实数据。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from itertools import groupby
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Device, DeviceEvent, Scene, SceneAction, WorkOrder


def naive_utc() -> datetime:
    """数据库里存的是 naive UTC，比较时也要用 naive。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


LOCAL_TZ = timezone(timedelta(hours=8))
PERIODS = ("today", "yesterday")


def _period_window(period: str) -> tuple[datetime, datetime]:
    """把「今天 / 昨天」换算成 UTC 时间窗。

    库里存的是 naive UTC，而用户说的「昨天」是按本地自然日算的，
    这里两边的转换必须显式做，否则跨时区就差 8 小时。
    """
    now_local = datetime.now(LOCAL_TZ)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start_local, end_local = midnight, now_local
    else:
        start_local, end_local = midnight - timedelta(days=1), midnight
    return (start_local.astimezone(timezone.utc).replace(tzinfo=None),
            end_local.astimezone(timezone.utc).replace(tzinfo=None))


def energy_report(db: Session, home_id: int, days: int = 7,
                  room: Optional[str] = None,
                  period: Optional[str] = None) -> dict[str, Any]:
    """按设备状态历史积分出用电量。

    算法：每台设备的事件按时间排序，相邻两条之间若处于通电状态，
    则 用电量 += 额定功率 × 时长。最后一条事件到窗口结束时刻也计入。

    period 支持 today / yesterday（自然日）；不传则按最近 days 天滚动窗口。
    """
    if period in PERIODS:
        since, now = _period_window(period)
        window_label = period
    else:
        window = max(1, min(int(days or 7), 30))
        now = naive_utc()
        since = now - timedelta(days=window)
        window_label = f"last_{window}_days"
    devices = {device.id: device for device in
               db.scalars(select(Device).where(Device.home_id == home_id))}
    events = list(db.scalars(select(DeviceEvent)
                             .where(DeviceEvent.home_id == home_id,
                                    DeviceEvent.created_at >= since,
                                    DeviceEvent.created_at <= now)
                             .order_by(DeviceEvent.device_id, DeviceEvent.created_at,
                                       DeviceEvent.id)))
    per_device: list[dict[str, Any]] = []
    for device_id, group in groupby(events, key=lambda event: event.device_id):
        rows = list(group)
        device = devices.get(device_id)
        if device is None:
            continue
        if room and room not in device.room:
            continue
        kwh = 0.0
        seconds = 0.0
        for index, event in enumerate(rows):
            if not event.powered:
                continue
            end = rows[index + 1].created_at if index + 1 < len(rows) else now
            span = max((end - event.created_at).total_seconds(), 0.0)
            seconds += span
            kwh += event.power_w * span / 3600 / 1000
        if kwh <= 0 and seconds <= 0:
            continue
        per_device.append({"device_id": device_id, "name": device.name, "room": device.room,
                           "kwh": round(kwh, 3), "hours": round(seconds / 3600, 2)})
    per_device.sort(key=lambda item: item["kwh"], reverse=True)
    per_room: dict[str, float] = {}
    for item in per_device:
        per_room[item["room"]] = round(per_room.get(item["room"], 0.0) + item["kwh"], 3)
    label = None
    if period in PERIODS:
        # 直接给出日期，别让模型自己推算——它算不准
        label = (since + timedelta(hours=8)).strftime("%Y-%m-%d")
    return {"window": window_label, "period_label": label, "room_filter": room or None,
            "from": since.isoformat(), "to": now.isoformat(),
            "total_kwh": round(sum(item["kwh"] for item in per_device), 3),
            "total_hours": round(sum(item["hours"] for item in per_device), 2),
            "by_room": [{"room": key, "kwh": value}
                        for key, value in sorted(per_room.items(), key=lambda kv: -kv[1])],
            "by_device": per_device[:10],
            "has_history": bool(events)}


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


def pending_payload(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}
