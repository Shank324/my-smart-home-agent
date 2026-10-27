"""场景：动作规范化、唯一命名、执行副作用。"""

from __future__ import annotations

import uuid


def _scene_payload(device_id: int, name: str) -> dict:
    return {"name": name, "description": "测试用场景",
            "actions": [{"device_id": device_id, "action": "on",
                         "attributes": {"brightness": 70}}]}


def test_create_and_execute_scene(client, home_headers, device):
    name = f"测试场景-{uuid.uuid4().hex[:6]}"
    created = client.post("/api/scenes", headers=home_headers, json=_scene_payload(device["id"], name))
    assert created.status_code == 201, created.text
    scene = created.json()
    assert scene["name"] == name
    assert len(scene["actions"]) == 1

    executed = client.post("/api/scenes/execute", headers=home_headers, json={"scene_id": scene["id"]})
    assert executed.status_code == 200, executed.text

    devices = {item["id"]: item for item in client.get("/api/devices", headers=home_headers).json()}
    assert devices[device["id"]]["active"] is True


def test_duplicate_scene_name_rejected(client, home_headers, device):
    name = f"重名场景-{uuid.uuid4().hex[:6]}"
    assert client.post("/api/scenes", headers=home_headers,
                       json=_scene_payload(device["id"], name)).status_code == 201
    duplicate = client.post("/api/scenes", headers=home_headers,
                            json=_scene_payload(device["id"], name))
    assert duplicate.status_code == 409


def test_scene_requires_at_least_one_action(client, home_headers):
    response = client.post("/api/scenes", headers=home_headers,
                           json={"name": "空场景", "description": "", "actions": []})
    assert response.status_code == 422


def test_scene_with_unknown_device_rejected(client, home_headers):
    response = client.post("/api/scenes", headers=home_headers, json={
        "name": "幽灵设备场景", "actions": [{"device_id": 999999, "action": "on"}]})
    assert response.status_code in (404, 422)


def test_delete_scene(client, home_headers, device):
    name = f"待删场景-{uuid.uuid4().hex[:6]}"
    scene = client.post("/api/scenes", headers=home_headers,
                        json=_scene_payload(device["id"], name)).json()
    assert client.delete(f"/api/scenes/{scene['id']}", headers=home_headers).status_code == 200
    assert scene["id"] not in {item["id"] for item in client.get("/api/scenes", headers=home_headers).json()}


def test_builtin_scenes_are_seeded(client, home_headers):
    names = {scene["name"] for scene in client.get("/api/scenes", headers=home_headers).json()}
    assert {"回家", "离家", "晚安"} <= names
