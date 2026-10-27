"""Agent 会话：提案 → 挂起 → 确认落库 / 取消 的完整链路。

这里不打真实模型：用一个按脚本返回的假 LLM，验证「模型说要做什么」之后
系统自己的行为——挂起状态、pending_action 持久化、确认后的副作用。
"""

from __future__ import annotations

import json

import pytest

from backend.api import agent as agent_module


class ScriptedLLM:
    """按顺序吐出预设回复，用来模拟工具调用循环。"""

    configured = True
    model = "scripted"

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs):               # noqa: ARG002
        self.calls.append(messages)
        if self.script:
            return self.script.pop(0)
        return {"content": "好的"}


def _tool_call(name: str, arguments: dict) -> dict:
    return {"content": "", "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }]}


@pytest.fixture()
def use_llm(monkeypatch):
    def _install(script: list[dict]) -> ScriptedLLM:
        fake = ScriptedLLM(script)
        monkeypatch.setattr(agent_module.llm, "chat", fake.chat)
        # configured 是只读属性，测试环境没有 key，这里整体替换成「已配置」
        monkeypatch.setattr(type(agent_module.llm), "configured", property(lambda self: True))
        monkeypatch.setattr(agent_module.llm, "model", "scripted", raising=False)
        return fake
    return _install


def test_scene_proposal_is_pending_until_confirmed(client, home_headers, device, use_llm):
    use_llm([
        _tool_call("propose_scene", {
            "name": "睡前助眠测试",
            "description": "调暗灯光",
            "rationale": "测试用",
            "actions": [{"device": device["name"], "action": "on",
                         "attributes": {"brightness": 15}}],
        }),
        {"content": "方案已生成，确认后布设。"},
    ])

    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    reply = client.post(f"/api/assistant/sessions/{session['id']}/messages",
                        headers=home_headers, json={"message": "帮我设计一个回家洗澡的模式"})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["status"] == "pending_confirm", body
    assert body["proposal"]["name"] == "睡前助眠测试"

    # 挂起阶段不能有副作用
    scene_names = {scene["name"] for scene in client.get("/api/scenes", headers=home_headers).json()}
    assert "睡前助眠测试" not in scene_names

    # 会话详情里能恢复出待确认动作（等价于 checkpointer）
    detail = client.get(f"/api/assistant/sessions/{session['id']}", headers=home_headers).json()
    assert detail["pending_action"]["type"] == "scene"

    confirmed = client.post(f"/api/assistant/sessions/{session['id']}/confirm",
                            headers=home_headers, json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["kind"] == "scene"

    scene_names = {scene["name"] for scene in client.get("/api/scenes", headers=home_headers).json()}
    assert "睡前助眠测试" in scene_names
    assert client.get(f"/api/assistant/sessions/{session['id']}",
                      headers=home_headers).json()["status"] == "active"


def test_cancel_leaves_no_side_effect(client, home_headers, device, use_llm):
    use_llm([
        _tool_call("propose_scene", {
            "name": "会被取消的场景", "description": "", "rationale": "",
            "actions": [{"device": device["name"], "action": "on", "attributes": {}}],
        }),
        {"content": "方案已生成。"},
    ])
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    client.post(f"/api/assistant/sessions/{session['id']}/messages",
                headers=home_headers, json={"message": "帮我设计一个晚上观影的模式"})

    cancelled = client.post(f"/api/assistant/sessions/{session['id']}/cancel",
                            headers=home_headers, json={})
    assert cancelled.status_code == 200
    scene_names = {scene["name"] for scene in client.get("/api/scenes", headers=home_headers).json()}
    assert "会被取消的场景" not in scene_names


def test_work_order_draft_then_confirm(client, home_headers, device, use_llm):
    use_llm([
        _tool_call("propose_work_order", {
            "title": "测试灯具闪烁", "symptom": "灯一闪一闪",
            "device": device["name"], "priority": "medium",
        }),
        {"content": "已整理成报修草稿。"},
    ])
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    body = client.post(f"/api/assistant/sessions/{session['id']}/messages",
                       headers=home_headers, json={"message": "我要报修，测试灯一直闪"}).json()
    assert body["status"] == "pending_confirm"
    assert body["work_order"]["title"] == "测试灯具闪烁"

    before = {order["id"] for order in client.get("/api/work-orders", headers=home_headers).json()}
    confirmed = client.post(f"/api/assistant/sessions/{session['id']}/confirm",
                            headers=home_headers, json={}).json()
    assert confirmed["kind"] == "work_order"
    after = {order["id"] for order in client.get("/api/work-orders", headers=home_headers).json()}
    assert len(after - before) == 1


def test_off_topic_message_is_blocked(client, home_headers, use_llm):
    use_llm([{"content": "off_topic"}])
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    body = client.post(f"/api/assistant/sessions/{session['id']}/messages",
                       headers=home_headers, json={"message": "帮我写一首关于春天的诗"}).json()
    assert body["blocked"] is True
    assert body["intent"] is None


def test_confirm_without_pending_returns_409(client, home_headers):
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    assert client.post(f"/api/assistant/sessions/{session['id']}/confirm",
                       headers=home_headers, json={}).status_code == 409


def test_session_list_and_delete(client, home_headers):
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    listed = client.get("/api/assistant/sessions", headers=home_headers).json()
    assert session["id"] in {item["id"] for item in listed}
    assert client.delete(f"/api/assistant/sessions/{session['id']}",
                         headers=home_headers).status_code == 200
    listed = client.get("/api/assistant/sessions", headers=home_headers).json()
    assert session["id"] not in {item["id"] for item in listed}


def test_other_user_cannot_read_session(client, home_headers, other_home_headers):
    session = client.post("/api/assistant/sessions", headers=home_headers, json={}).json()
    response = client.get(f"/api/assistant/sessions/{session['id']}", headers=other_home_headers)
    assert response.status_code in (403, 404)
