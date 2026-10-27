"""能耗积分：给定开关时间点，算出的电量必须对得上。"""

from __future__ import annotations

from datetime import timedelta, timezone

from backend.db import SessionLocal
from backend.models import DeviceEvent
from backend.services import _period_window, energy_report, naive_utc


def _home_id(client, headers) -> int:
    return client.get("/api/me", headers=headers).json()["home_id"]


def test_energy_integrates_power_over_time(client, home_headers, device):
    home_id = _home_id(client, home_headers)
    now = naive_utc()
    with SessionLocal() as db:
        # 2 小时前开机（1000W），1 小时前关机 → 恰好 1 度电、1 小时
        db.add(DeviceEvent(device_id=device["id"], home_id=home_id, powered=True,
                           value="on", power_w=1000, created_at=now - timedelta(hours=2)))
        db.add(DeviceEvent(device_id=device["id"], home_id=home_id, powered=False,
                           value="off", power_w=1000, created_at=now - timedelta(hours=1)))
        db.commit()
        report = energy_report(db, home_id, days=1)

    row = next(item for item in report["by_device"] if item["device_id"] == device["id"])
    assert 0.9 <= row["kwh"] <= 1.1
    assert 0.9 <= row["hours"] <= 1.1


def test_energy_skips_powered_off_spans(client, home_headers, device):
    home_id = _home_id(client, home_headers)
    now = naive_utc()
    with SessionLocal() as db:
        # 开机 3 小时前，1 分钟后就关了，之后一直关着 → 只有 1 分钟的电
        db.add(DeviceEvent(device_id=device["id"], home_id=home_id, powered=True,
                           value="on", power_w=2000, created_at=now - timedelta(hours=3)))
        db.add(DeviceEvent(device_id=device["id"], home_id=home_id, powered=False,
                           value="off", power_w=2000,
                           created_at=now - timedelta(hours=3) + timedelta(minutes=1)))
        db.commit()
        report = energy_report(db, home_id, days=1)

    row = next(item for item in report["by_device"] if item["device_id"] == device["id"])
    assert row["kwh"] < 0.1


def test_energy_room_filter(client, home_headers, device):
    home_id = _home_id(client, home_headers)
    now = naive_utc()
    with SessionLocal() as db:
        db.add(DeviceEvent(device_id=device["id"], home_id=home_id, powered=True,
                           value="on", power_w=600, created_at=now - timedelta(hours=1)))
        db.commit()
        matched = energy_report(db, home_id, days=1, room=device["room"])
        missing = energy_report(db, home_id, days=1, room="不存在的房间")

    assert any(item["device_id"] == device["id"] for item in matched["by_device"])
    assert missing["by_device"] == []


def test_period_window_is_local_calendar_day():
    start, end = _period_window("yesterday")
    # 窗口长度必须正好 24 小时（自然日），且结束于本地今天 0 点
    assert (end - start) == timedelta(days=1)
    local_end = end.replace(tzinfo=timezone.utc) + timedelta(hours=8)
    assert local_end.hour == 0 and local_end.minute == 0


def test_energy_api_endpoint(client, home_headers):
    response = client.get("/api/stats/energy?days=7", headers=home_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "last_7_days"
    assert "total_kwh" in body and "by_room" in body


def test_energy_api_rejects_repair_staff(client, staff_headers):
    assert client.get("/api/stats/energy", headers=staff_headers).status_code == 403
