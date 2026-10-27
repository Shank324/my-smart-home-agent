"""多租户隔离：跨家庭读写必须被拦住。"""

from __future__ import annotations

SAMPLE_DEVICE = {
    "name": "隔离测试灯", "room": "样板间", "type": "light",
    "value": "30%", "power_consumption": 9, "attributes": {"brightness": 30},
}


def test_outsider_only_sees_own_home(client, home_headers, other_home_headers):
    created = client.post("/api/devices", headers=home_headers, json=SAMPLE_DEVICE).json()
    mine = client.get("/api/devices", headers=home_headers).json()
    theirs = client.get("/api/devices", headers=other_home_headers).json()
    assert created["id"] in {device["id"] for device in mine}
    assert created["id"] not in {device["id"] for device in theirs}


def test_outsider_cannot_read_device(client, home_headers, other_home_headers):
    created = client.post("/api/devices", headers=home_headers, json=SAMPLE_DEVICE).json()
    response = client.get(f"/api/devices/{created['id']}", headers=other_home_headers)
    assert response.status_code in (403, 404, 405)


def test_outsider_cannot_command_device(client, home_headers, other_home_headers):
    created = client.post("/api/devices", headers=home_headers, json=SAMPLE_DEVICE).json()
    response = client.post(f"/api/devices/{created['id']}/commands",
                           headers=other_home_headers, json={"action": "on"})
    assert response.status_code == 403


def test_outsider_cannot_update_device(client, home_headers, other_home_headers):
    created = client.post("/api/devices", headers=home_headers, json=SAMPLE_DEVICE).json()
    response = client.put(f"/api/devices/{created['id']}",
                          headers=other_home_headers, json={**SAMPLE_DEVICE, "name": "被改名"})
    assert response.status_code == 403


def test_outsider_cannot_see_scene(client, home_headers, other_home_headers):
    scenes = client.get("/api/scenes", headers=other_home_headers).json()
    assert scenes == [] or all(scene.get("id") for scene in scenes)
    home_scenes = client.get("/api/scenes", headers=home_headers).json()
    home_ids = {scene["id"] for scene in home_scenes}
    other_ids = {scene["id"] for scene in scenes}
    assert home_ids.isdisjoint(other_ids)


def test_technician_only_sees_orders_they_can_work_on(client, home_headers, staff_headers):
    """维修人员能看到工单池，但没接的单不能聊天、不能改状态。"""
    order = client.post("/api/work-orders", headers=home_headers, json={
        "title": "隔离用例-卫生间漏水", "description": "地面有水", "priority": "high",
    }).json()
    pool = client.get("/api/work-orders", headers=staff_headers).json()
    assert order["id"] in {item["id"] for item in pool}

    assert client.get(f"/api/work-orders/{order['id']}/messages",
                      headers=staff_headers).status_code == 403
    assert client.put(f"/api/work-orders/{order['id']}/status",
                      headers=staff_headers, json={"status": "in_progress"}).status_code == 403
