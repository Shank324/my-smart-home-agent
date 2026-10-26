"""设备类型目录与类型化参数校验。

设备的可调参数是**稀疏且随类型变化**的：空调有模式/温度，电视有音量/频道，
传感器什么都没有。这类属性既不适合建一堆稀疏列，也不适合拆表，
所以用 `devices.attributes` JSON 列存放，并在写入时按类型做白名单 + 范围校验。

关键点：**JSON 列如果不校验就是无底洞**。这里把「允许哪些键、取值范围、
非法值怎么处理」集中成唯一事实来源，前端也从同一个接口拿目录去渲染表单，
避免前后端各写一份枚举。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

# key -> (中文名, {属性名: 规则})
ATTRIBUTE_RULES: dict[str, dict[str, dict[str, Any]]] = {
    "air_conditioner": {
        "mode": {"type": "enum", "choices": ["cool", "heat", "dry", "fan"],
                 "default": "cool", "label": "模式"},
        "temperature": {"type": "int", "min": 16, "max": 30,
                        "default": 24, "label": "温度(°C)"},
    },
    "light": {
        "brightness": {"type": "int", "min": 0, "max": 100,
                       "default": 60, "label": "亮度(%)"},
    },
    "tv": {
        "volume": {"type": "int", "min": 0, "max": 100, "default": 50, "label": "音量"},
        "channel": {"type": "int", "min": 1, "max": 999, "default": 1, "label": "频道"},
    },
    "water_heater": {
        "temperature": {"type": "int", "min": 35, "max": 75,
                        "default": 50, "label": "水温(°C)"},
    },
    "sensor": {},
    "other": {},
}

TYPE_LABELS: dict[str, str] = {
    "air_conditioner": "空调",
    "light": "灯",
    "tv": "电视",
    "water_heater": "热水器",
    "sensor": "传感器",
    "other": "其他",
}

ENUM_LABELS: dict[str, dict[str, str]] = {
    "mode": {"cool": "制冷", "heat": "制热", "dry": "除湿", "fan": "送风"},
}

# 只有"单一主状态"的设备才从参数推导状态值；电视这类保持手填（输入源才是它的状态）
STATUS_TEMPLATE: dict[str, str] = {
    "air_conditioner": "{temperature}°C",
    "light": "{brightness}%",
    "water_heater": "{temperature}°C",
}

# 类型默认值：新建时预填，省得用户从 0 开始填
TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "air_conditioner": {"status": "24°C", "power": 1500.0},
    "light": {"status": "60%", "power": 12.0},
    "tv": {"status": "HDMI 1", "power": 120.0},
    "water_heater": {"status": "50°C", "power": 2000.0},
    "sensor": {"status": "--", "power": 3.0},
    "other": {"status": "--", "power": 0.0},
}


def normalize_attributes(device_type: str, raw: Any) -> dict[str, Any]:
    """按设备类型过滤并校验参数，未知键直接丢弃。"""
    rules = ATTRIBUTE_RULES.get(device_type, {})
    if not isinstance(raw, dict):
        raise HTTPException(422, "device_attributes_must_be_object")

    cleaned: dict[str, Any] = {}
    for key, rule in rules.items():
        value = raw.get(key)
        if value is None or value == "":
            cleaned[key] = rule["default"]
            continue
        if rule["type"] == "enum":
            if value not in rule["choices"]:
                raise HTTPException(422, f"invalid_attribute:{key}")
            cleaned[key] = value
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"invalid_attribute:{key}") from None
        if not rule["min"] <= number <= rule["max"]:
            raise HTTPException(422, f"invalid_attribute:{key}")
        cleaned[key] = number
    return cleaned


def default_attributes(device_type: str) -> dict[str, Any]:
    return {key: rule["default"] for key, rule in ATTRIBUTE_RULES.get(device_type, {}).items()}


def derive_status(device_type: str, attributes: dict[str, Any]) -> str | None:
    """按状态模板从参数推导展示值，例如空调 {"temperature": 26} -> "26°C"。"""
    template = STATUS_TEMPLATE.get(device_type)
    if not template or not attributes:
        return None

    def replace(match: re.Match[str]) -> str:
        value = attributes.get(match.group(1))
        return "--" if value is None else str(value)

    return re.sub(r"\{(\w+)\}", replace, template)


def catalog_payload() -> dict[str, Any]:
    """前端表单直接消费这份目录，保证前后端枚举永远一致。"""
    return {
        "types": [
            {"key": key, "label": label,
             "attributes": ATTRIBUTE_RULES.get(key, {}),
             "attribute_defaults": default_attributes(key),
             "status_default": TYPE_DEFAULTS.get(key, {}).get("status", "--"),
             "power_default": TYPE_DEFAULTS.get(key, {}).get("power", 0.0),
             # 有状态模板的设备，状态值由参数推导，避免两处各填一遍
             "status_template": STATUS_TEMPLATE.get(key),
             "status_from_attributes": key in STATUS_TEMPLATE}
            for key, label in TYPE_LABELS.items()
        ],
        "enum_labels": ENUM_LABELS,
    }
