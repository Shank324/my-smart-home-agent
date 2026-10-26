"""Agent 的意图路由与护栏。

三件事：
1. `classify_intent` —— 规则优先、模型兜底的四类意图分流
2. `check_input`    —— 领域外请求拦截（避免答非所问或越权话题）
3. `check_grounding`—— 答案落地校验：回答里的设备名与数值必须来自本轮工具结果

设计取向：护栏失败要「失败开放」而不是「失败关闭」——
模型分类挂了不该让用户问不了问题，宁可放过也不误拦。
"""

from __future__ import annotations

import re
from typing import Any, Optional

INTENTS = ("consult", "repair", "design", "control")

# 关键词命中数最多的意图胜出；平票按这里的顺序优先
# 注意 repair 只收「明确的报修动作」，症状描述和故障代码属于 consult：
# 「空调显示 E1 怎么办」是在问原因，不是要报修。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("design", ("设计", "方案", "模式", "场景", "布置", "帮我配", "怎么配", "搭配",
                "睡前", "入睡", "睡不好", "失眠", "回家", "观影", "看电影", "洗澡",
                "起床", "潮湿", "发黏", "发闷", "太干")),
    ("repair", ("报修", "维修", "帮我修", "修一下", "找人看", "上门", "叫师傅", "师傅",
                "提交工单", "建单", "派单", "找人修")),
    ("control", ("打开", "关闭", "关掉", "开启", "调到", "调成", "设为", "设置成",
                 "开一下", "关一下", "切到", "切换")),
)

# 出现这些词就认为属于智能家居领域，不必再让模型判断
_DOMAIN_WORDS = ("设备", "空调", "灯", "电视", "热水器", "传感器", "烟雾", "场景", "报修",
                 "工单", "温度", "湿度", "空气", "电量", "耗电", "费电", "房间", "客厅",
                 "卧室", "厨房", "卫生间", "玄关", "遥控", "网关", "网络", "wifi", "模式",
                 "冷", "热", "风", "滤网", "水", "睡", "报警", "离家", "回家", "维修")


def classify_intent(message: str, llm: Optional[Any] = None) -> tuple[str, str]:
    """返回 (intent, source)，source 为 rule / llm / default，便于统计命中率。"""
    text = (message or "").lower()
    scores = {intent: 0 for intent, _ in _RULES}
    for intent, keywords in _RULES:
        for keyword in keywords:
            if keyword in text:
                scores[intent] += 1
    best = max(scores, key=lambda item: (scores[item], -INTENTS.index(item)))
    if scores[best] > 0:
        return best, "rule"
    if llm is not None:
        try:
            reply = llm.chat([
                {"role": "system", "content":
                    "你是意图分类器。把用户的话归为四类之一，只输出一个英文单词：\n"
                    "consult=在问原因、怎么处理、怎么使用，没有要求你去修\n"
                    "repair=明确要求提交报修、找人上门维修\n"
                    "design=希望设计一套场景，或描述身体感受/生活目标（睡不好、空气潮湿）\n"
                    "control=要求立刻打开、关闭、调节某台设备"},
                {"role": "user", "content": message},
            ], temperature=0.0, max_tokens=12)
            label = (reply.get("content") or "").strip().lower()
            for intent in INTENTS:
                if intent in label:
                    return intent, "llm"
        except Exception:                                  # noqa: BLE001
            pass
    return "consult", "default"


def check_input(message: str, llm: Optional[Any] = None) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。失败开放：分类不可用时放行。"""
    text = (message or "").lower()
    if any(word in text for word in _DOMAIN_WORDS):
        return True, "domain_keyword"
    if len(text) <= 2:
        return True, "too_short_to_judge"
    if llm is None:
        return True, "no_classifier"
    try:
        reply = llm.chat([
            {"role": "system", "content":
                "判断用户这句话是否属于「智能家居」范畴：设备控制、家居场景、家电故障、"
                "居家舒适与安全、家庭报修。只输出 home 或 off_topic。"},
            {"role": "user", "content": message},
        ], temperature=0.0, max_tokens=8)
        label = (reply.get("content") or "").strip().lower()
        if "off_topic" in label or "off-topic" in label:
            return False, "off_topic"
        return True, "model_checked"
    except Exception:                                      # noqa: BLE001
        return True, "classifier_failed"


_NUMBER_WITH_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:°c|℃|%|w|kw|度|小时|分钟|kwh)", re.IGNORECASE)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def check_grounding(answer: str, evidence: str, user_message: str,
                    device_names: list[str]) -> dict[str, Any]:
    """校验回答里的设备名与带单位的数值是否都能在本轮证据里找到出处。

    这只覆盖「可硬校验」的部分：设备名和数值。
    主观表述（"建议""可能"）不参与判定。
    """
    answer_norm = _normalize(answer)
    evidence_norm = _normalize(evidence)
    user_norm = _normalize(user_message)
    warnings: list[str] = []

    for name in device_names:
        key = _normalize(name)
        if key and key in answer_norm and key not in evidence_norm and key not in user_norm:
            warnings.append(f"device:{name}")

    for raw in _NUMBER_WITH_UNIT.findall(answer or ""):
        token = _normalize(raw)
        if token and token not in evidence_norm and token not in user_norm:
            # 允许范围表述里的边界值，例如 24-26°C 拆出来的 26°C
            if token not in evidence_norm.replace("-", ""):
                warnings.append(f"value:{raw.strip()}")

    return {"grounded": not warnings, "warnings": warnings[:8],
            "checked_devices": len(device_names)}
