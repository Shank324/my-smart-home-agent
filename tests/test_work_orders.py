"""工单：状态机、维修人员权限矩阵（含越权删除的回归用例）。"""

from __future__ import annotations

import uuid


def _order(client, headers, **overrides) -> dict:
    payload = {"title": f"测试工单-{uuid.uuid4().hex[:6]}",
               "description": "测试用描述", "priority": "medium"}
    payload.update(overrides)
    response = client.post("/api/work-orders", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_and_timeline(client, home_headers):
    order = _order(client, home_headers, priority="high")
    assert order["status"] == "open"
    timeline = client.get(f"/api/work-orders/{order['id']}/timeline", headers=home_headers).json()
    assert timeline["events"] == [] or isinstance(timeline["events"], list)


def test_technician_accept_flow(client, home_headers, staff_headers):
    order = _order(client, home_headers)
    accepted = client.post(f"/api/work-orders/{order['id']}/accept", headers=staff_headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "assigned"
    assert accepted.json()["assigned_to"] is not None

    progress = client.put(f"/api/work-orders/{order['id']}/status",
                          headers=staff_headers, json={"status": "in_progress"})
    assert progress.status_code == 200
    assert progress.json()["status"] == "in_progress"


def test_home_user_cannot_accept(client, home_headers):
    order = _order(client, home_headers)
    assert client.post(f"/api/work-orders/{order['id']}/accept",
                       headers=home_headers).status_code == 403


def test_cannot_change_status_before_accepting(client, home_headers, staff_headers):
    order = _order(client, home_headers)
    response = client.put(f"/api/work-orders/{order['id']}/status",
                          headers=staff_headers, json={"status": "in_progress"})
    assert response.status_code == 403


def test_cannot_delete_order_not_assigned_to_me(client, home_headers, staff_headers):
    """回归用例：曾经任何维修人员都能删掉未接单的工单。"""
    order = _order(client, home_headers)
    response = client.delete(f"/api/work-orders/{order['id']}", headers=staff_headers)
    assert response.status_code == 403
    # 工单还在
    still_there = {item["id"] for item in client.get("/api/work-orders", headers=home_headers).json()}
    assert order["id"] in still_there


def test_assigned_technician_can_delete_own_order(client, home_headers, staff_headers):
    order = _order(client, home_headers)
    client.post(f"/api/work-orders/{order['id']}/accept", headers=staff_headers)
    assert client.delete(f"/api/work-orders/{order['id']}", headers=staff_headers).status_code == 200


def test_completion_records_cost_and_resolves(client, home_headers, staff_headers):
    order = _order(client, home_headers)
    client.post(f"/api/work-orders/{order['id']}/accept", headers=staff_headers)
    client.put(f"/api/work-orders/{order['id']}/status", headers=staff_headers,
               json={"status": "in_progress"})
    done = client.put(f"/api/work-orders/{order['id']}/completion", headers=staff_headers,
                      json={"cost": 180, "duration_minutes": 45})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "resolved"
    assert done.json()["cost"] == 180
    assert done.json()["duration_minutes"] == 45


def test_messages_require_assignment(client, home_headers, staff_headers):
    order = _order(client, home_headers)
    assert client.post(f"/api/work-orders/{order['id']}/messages",
                       headers=staff_headers, json={"message": "我来了"}).status_code == 403

    client.post(f"/api/work-orders/{order['id']}/accept", headers=staff_headers)
    sent = client.post(f"/api/work-orders/{order['id']}/messages",
                       headers=staff_headers, json={"message": "我来了"})
    assert sent.status_code == 201

    messages = client.get(f"/api/work-orders/{order['id']}/messages", headers=home_headers).json()
    assert any("我来了" in item["message"] for item in messages)


def test_home_user_cannot_delete_order(client, home_headers):
    order = _order(client, home_headers)
    assert client.delete(f"/api/work-orders/{order['id']}", headers=home_headers).status_code == 403
