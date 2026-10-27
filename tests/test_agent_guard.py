"""Agent 的确定性部分：意图规则、领域护栏、落地校验。

依赖 LLM 的编排循环不在这里测（会联网），只测「不接模型也必须正确」的逻辑。
"""

from __future__ import annotations

from backend.agent_guard import check_grounding, check_input, classify_intent


class FakeLLM:
    """把模型回答固定成某个标签，用来验证护栏的判定分支。"""

    configured = True

    def __init__(self, reply: str = "off_topic") -> None:
        self.reply = reply

    def chat(self, messages, tools=None, **kwargs):          # noqa: ARG002
        return {"content": self.reply}


def test_intent_repair():
    assert classify_intent("我要报修，卫生间热水器漏水", None)[0] == "repair"


def test_intent_design():
    assert classify_intent("帮我设计一个回家洗澡的模式", None)[0] == "design"


def test_intent_control():
    assert classify_intent("把客厅空调关掉", None)[0] == "control"


def test_intent_consult_is_default():
    assert classify_intent("空调显示 E1 怎么办", None)[0] == "consult"


def test_scene_question_is_not_design():
    """回归用例：「场景没反应」是排查咨询，不是要设计场景。"""
    assert classify_intent("场景执行了但设备没反应", None)[0] == "consult"


def test_energy_question_stays_in_domain():
    """回归用例：曾经因为词表里没有「用电」被误判成领域外。"""
    allowed, reason = check_input("昨天用电多少", FakeLLM("off_topic"))
    assert allowed is True
    assert reason == "domain_keyword"


def test_off_topic_is_blocked():
    allowed, reason = check_input("帮我写一首关于春天的诗", FakeLLM("off_topic"))
    assert allowed is False
    assert reason == "off_topic"


def test_fail_open_when_classifier_unavailable():
    """分类器挂了要放行，不能让人问不了问题。"""
    allowed, reason = check_input("帮我写一首关于春天的诗", None)
    assert allowed is True


def test_grounding_accepts_known_device():
    result = check_grounding("客厅空调已经关闭了", "客厅中央空调 状态 关闭", "把空调关掉",
                             ["客厅中央空调", "主卧台灯"])
    assert result["grounded"] is True


def test_grounding_flags_unknown_device():
    result = check_grounding("主卧台灯已经关闭", "", "把客厅空调关掉", ["客厅中央空调", "主卧台灯"])
    assert result["grounded"] is False
    assert result["device_violations"] == ["主卧台灯"]


def test_grounding_flags_unverified_number():
    result = check_grounding("温度已经调到 26°C 了", "当前温度 24°C", "调温度", [])
    assert "26°C" in result["numeric_unverified"]


def test_grounding_allows_rounded_derived_value():
    result = check_grounding("累计运行 31.7 小时", "累计运行时长 31.67 小时", "开了多久", [])
    assert result["numeric_unverified"] == []
