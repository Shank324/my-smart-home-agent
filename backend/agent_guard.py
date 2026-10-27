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
    # 注意别把「场景」单独作为 design 关键词：
    # 「场景执行了但设备没反应」是排查咨询，不是要设计场景
    ("design", ("设计", "方案", "模式", "布置", "帮我配", "怎么配", "搭配",
                "睡前", "入睡", "睡不好", "失眠", "回家", "观影", "看电影", "洗澡",
                "起床", "潮湿", "发黏", "发闷", "太干")),
    ("repair", ("报修", "维修", "帮我修", "修一下", "找人看", "上门", "叫师傅", "师傅",
                "提交工单", "建单", "派单", "找人修")),
    ("control", ("打开", "关闭", "关掉", "开启", "调到", "调成", "设为", "设置成",
                 "开一下", "关一下", "切到", "切换")),
)

# 出现这些词就认为属于智能家居领域，不必再让模型判断。
# 这个表踩过坑：早期只收录「电量/耗电/费电」，用户说「昨天用电多少」被误判成领域外，
# 所以按「设备品类 / 场景功能 / 能耗 / 环境 / 服务 / 空间」分类收全一点。
_DOMAIN_WORDS = (
    # 设备品类
    "设备", "家电", "空调", "灯", "电视", "热水器", "传感器", "烟雾", "门锁", "冰箱",
    "窗帘", "净化", "新风", "暖气", "加湿", "除湿", "滤网", "遥控", "网关", "插座",
    # 场景与功能
    "场景", "模式", "联动", "定时", "离家", "回家", "睡眠", "观影",
    # 能耗（用电/电费是高频问法，必须覆盖）
    "用电", "电量", "耗电", "费电", "电费", "度电", "能耗", "功耗", "省电", "千瓦时",
    # 环境
    "温度", "湿度", "空气", "室温", "水温", "冷", "热", "风",
    # 服务
    "报修", "工单", "维修", "上门", "师傅",
    # 空间
    "房间", "客厅", "卧室", "厨房", "卫生间", "玄关", "阳台", "书房",
    # 其它
    "网络", "wifi", "报警", "水压", "漏水",
)


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
                "判断用户这句话是否属于「智能家居」范畴，只输出 home 或 off_topic。\n"
                "属于 home 的范围：\n"
                "- 家电设备（空调/灯/电视/热水器/传感器/门锁等）的开关、参数、使用\n"
                "- 家居场景与自动化方案的配置\n"
                "- 家电故障排查、家庭报修\n"
                "- 居家舒适与安全（温湿度、空气、睡眠、烟雾燃气）\n"
                "- **家庭用电、电费、能耗统计**（例如「昨天用电多少」「这个月电费怎么这么高」）\n"
                "注意：只要和「自己家的居住环境、家电、用电、报修」有关就算 home，"
                "不要因为句子短或没有出现具体设备名就判成 off_topic。\n"
                "只有明确无关的（写诗、股票、旅游、情感咨询等）才输出 off_topic。"},
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
    """校验回答的落地性，分两级：

    - **硬校验（决定 grounded）**：回答里出现的设备名，必须在本轮工具结果或用户原话里出现过。
      设备名编错是最典型的幻觉（把「客厅空调」说成「主卧空调」）。
    - **软校验（仅统计）**：带单位的数值是否在证据里出现过。
      模型会把工具结果做除法、取整（31.7 小时 / 7 天 = 4.5 小时），
      这类派生值不应算幻觉，所以单独列出来供人工看，不参与 grounded 判定。
    """
    answer_norm = _normalize(answer)
    evidence_norm = _normalize(evidence)
    user_norm = _normalize(user_message)

    device_violations: list[str] = []
    for name in device_names:
        key = _normalize(name)
        if key and key in answer_norm and key not in evidence_norm and key not in user_norm:
            device_violations.append(name)

    numeric_unverified: list[str] = []
    evidence_numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", evidence or "")]

    def numeric_found(token: str) -> bool:
        core = re.match(r"\d+(?:\.\d+)?", token)
        if not core:
            return True
        literal = core.group()
        if literal in evidence_norm:
            return True
        value = float(literal)
        # 允许四舍五入：31.67 -> 31.7
        return any(abs(value - candidate) <= max(abs(value) * 0.01, 0.05)
                   for candidate in evidence_numbers)

    for raw in _NUMBER_WITH_UNIT.findall(answer or ""):
        token = _normalize(raw)
        if not token or token in evidence_norm or token in user_norm:
            continue
        # 百分比通常是比例计算的结果，不参与判定
        if token.endswith("%"):
            continue
        if numeric_found(token):
            continue
        numeric_unverified.append(raw.strip())

    return {"grounded": not device_violations,
            "device_violations": device_violations,
            "numeric_unverified": numeric_unverified[:8],
            "warnings": device_violations[:8],
            "checked_devices": len(device_names)}
