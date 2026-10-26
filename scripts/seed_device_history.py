"""生成演示用的设备状态历史。

真实的历史来自触发器（每次开关都会留痕）。但新项目跑起来时历史是空的，
能耗分析就无从演示，所以这里按「典型家庭作息」补一段历史。

⚠️ 这是**演示数据**，不是真实采集记录。生产环境不要跑这个脚本。

用法：
    py scripts/seed_device_history.py --home 1 --days 7
    py scripts/seed_device_history.py --home 2 --days 7 --reset
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "mysql+pymysql://root:324215@127.0.0.1:3308/smarthome")

from sqlalchemy import delete, select  # noqa: E402

from backend.db import SessionLocal  # noqa: E402
from backend.models import Device, DeviceEvent  # noqa: E402

# 每种设备一天里的典型运行时段（本地时间，24 小时制）
SCHEDULE: dict[str, list[tuple[float, float]]] = {
    "air_conditioner": [(19.0, 23.5)],
    "light": [(7.0, 8.0), (18.5, 23.0)],
    "tv": [(20.0, 22.5)],
    "water_heater": [(6.5, 7.5), (20.5, 21.5)],
    "other": [(0.0, 24.0)],          # 冰箱这类常开设备
    "sensor": [],
}

LOCAL_OFFSET = timezone(timedelta(hours=8))


def local_to_utc_naive(day: datetime, hour: float) -> datetime:
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    moment = base + timedelta(hours=hour)
    return (moment - timedelta(hours=8))  # 本地 -> UTC naive


def build_events(device: Device, days: int, now_local: datetime) -> list[DeviceEvent]:
    events: list[DeviceEvent] = []
    slots = SCHEDULE.get(device.type, [])
    if not slots:
        return events
    for offset in range(days, 0, -1):
        day = now_local - timedelta(days=offset)
        for start, end in slots:
            # 加一点随机抖动，避免每天完全一样
            start_jitter = start + random.uniform(-0.3, 0.3)
            end_jitter = end + random.uniform(-0.3, 0.3)
            if end - start >= 24:
                events.append(DeviceEvent(
                    device_id=device.id, home_id=device.home_id, powered=True,
                    value=device.value, power_w=device.power_consumption,
                    attributes=device.attributes,
                    created_at=local_to_utc_naive(day, 0.0)))
                continue
            events.append(DeviceEvent(
                device_id=device.id, home_id=device.home_id, powered=True,
                value=device.value, power_w=device.power_consumption,
                attributes=device.attributes,
                created_at=local_to_utc_naive(day, max(start_jitter, 0))))
            events.append(DeviceEvent(
                device_id=device.id, home_id=device.home_id, powered=False,
                value=device.value, power_w=device.power_consumption,
                attributes=device.attributes,
                created_at=local_to_utc_naive(day, min(end_jitter, 23.98))))
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=int, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--reset", action="store_true", help="先清空该家庭已有历史")
    args = parser.parse_args()

    now_local = datetime.now(LOCAL_OFFSET).replace(tzinfo=None)
    with SessionLocal() as db:
        devices = list(db.scalars(select(Device).where(Device.home_id == args.home)))
        if not devices:
            print(f"home {args.home} 没有设备")
            return 1
        if args.reset:
            removed = db.execute(delete(DeviceEvent).where(DeviceEvent.home_id == args.home))
            print(f"已清空历史：{removed.rowcount} 条")
        total = 0
        for device in devices:
            events = build_events(device, args.days, now_local)
            db.add_all(events)
            total += len(events)
        db.commit()
        print(f"home {args.home}: 为 {len(devices)} 台设备生成 {total} 条演示历史")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
