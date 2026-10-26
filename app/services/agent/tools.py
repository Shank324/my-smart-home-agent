"""通用工具注册表。

框架无关：这里不认识 FastAPI、SQLAlchemy，只负责把「函数」
包装成模型可调用的 tool schema，并安全地分发调用。
业务侧在 backend 里注册具体工具。
"""

from __future__ import annotations

import json
from typing import Any, Callable


class ToolError(RuntimeError):
    """工具执行失败，错误信息会回传给模型让它自行纠正。"""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, description: str, parameters: dict[str, Any]):
        """装饰器：import 时把函数登记为工具。"""
        def decorator(func: Callable[..., Any]):
            self._specs[name] = {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
            self._handlers[name] = func
            return func
        return decorator

    def specs(self) -> list[dict[str, Any]]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def invoke(self, name: str, arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError(f"未知工具: {name}")
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError(f"工具参数不是合法 JSON: {arguments[:120]}") from exc
        else:
            args = arguments or {}
        if not isinstance(args, dict):
            raise ToolError("工具参数必须是对象")
        try:
            result = handler(**args)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"工具参数不匹配: {exc}") from exc
        except Exception as exc:                     # noqa: BLE001 —— 工具内部异常不应打断对话
            raise ToolError(f"工具执行出错: {exc}") from exc
        return result if isinstance(result, dict) else {"result": result}
