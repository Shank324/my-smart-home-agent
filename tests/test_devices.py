"""设备：类型化参数校验、状态派生、指令下发。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.device_types import derive_status, normalize_attributes


def test_device_types_catalog(client):
    body = client.get("/api/device-types").json()
    types = {item["key"]: item for item in body["types"]}
    assert types["light"]["label"] == "灯"
    assert types["air_conditioner"]["attributes"]["temperature"]["max"] == 30
    assert body["enum_labels"]["mode"]["dry"] == "除湿"


def test_create_device_with_attributes(client, home_headers):
    response = client.post("/api/devices", headers=home_headers, json={
        "name": "书房空调", "room": "书房", "type": "air_conditioner", "value": "24°C",
        "power_consumption": 900, "attributes": {"temperature": 24, "mode": "cool"},
    })
    assert response.status_code == 201, response.text
    device = response.json()
    assert device["attributes"]["temperature"] == 24
    assert device["attributes"]["mode"] == "cool"


def test_create_device_keeps_explicit_attributes(client, home_headers):
    device = client.post("/api/devices", headers=home_headers, json={
        "name": "默认值灯", "room": "书房", "type": "light", "attributes": {"brightness": 20},
    }).json()
    assert device["attributes"] == {"brightness": 20}


def test_create_device_rejects_out_of_range(client, home_headers):
    response = client.post("/api/devices", headers=home_headers, json={
        "name": "异常温度空调", "room": "书房", "type": "air_conditioner",
        "attributes": {"temperature": 80},
    })
    assert response.status_code == 422


def test_create_device_rejects_enum_outside_choices(client, home_headers):
    response = client.post("/api/devices", headers=home_headers, json={
        "name": "异常模式空调", "room": "书房", "type": "air_conditioner",
        "attributes": {"mode": "turbo"},
    })
    assert response.status_code == 422


def test_command_on_off_toggles_state(client, home_headers, device):
    turned_on = client.post(f"/api/devices/{device['id']}/commands",
                            headers=home_headers, json={"action": "on"}).json()
    assert turned_on["accepted"] is True
    assert turned_on["device"]["active"] is True

    turned_off = client.post(f"/api/devices/{device['id']}/commands",
                             headers=home_headers, json={"action": "off"}).json()
    assert turned_off["device"]["active"] is False


def test_command_rejects_unknown_action(client, home_headers, device):
    response = client.post(f"/api/devices/{device['id']}/commands",
                           headers=home_headers, json={"action": "toggle"})
    assert response.status_code == 422


def test_update_device_attributes(client, home_headers, device):
    response = client.put(f"/api/devices/{device['id']}", headers=home_headers, json={
        "name": device["name"], "room": device["room"], "type": device["type"],
        "online": True, "active": True, "power_consumption": 12,
        "attributes": {"brightness": 80},
    })
    assert response.status_code == 200, response.text
    assert response.json()["attributes"]["brightness"] == 80


def test_delete_device(client, home_headers):
    created = client.post("/api/devices", headers=home_headers, json={
        "name": "待删除设备", "room": "储物间", "type": "light", "value": "10%",
    }).json()
    assert client.delete(f"/api/devices/{created['id']}", headers=home_headers).status_code == 200
    assert created["id"] not in {d["id"] for d in client.get("/api/devices", headers=home_headers).json()}


def test_normalize_attributes_fills_defaults():
    assert normalize_attributes("light", {}) == {"brightness": 60}
    assert normalize_attributes("air_conditioner", {"temperature": 26}) == {
        "mode": "cool", "temperature": 26}


def test_normalize_attributes_rejects_bad_value():
    with pytest.raises(HTTPException) as excinfo:
        normalize_attributes("light", {"brightness": 500})
    assert excinfo.value.status_code == 422


def test_normalize_attributes_drops_unknown_keys():
    """未知键直接丢弃，不允许往 JSON 列里塞任意字段。"""
    assert normalize_attributes("sensor", {"anything": 1}) == {}


def test_derive_status_formats_value():
    assert derive_status("air_conditioner", {"temperature": 24}) == "24°C"
    assert derive_status("tv", {"volume": 10}) is None      # 电视不按参数推导状态
