"""DeepSeek（OpenAI 兼容协议）对话客户端。

只用标准库 urllib，避免为一次 HTTP 调用引入额外依赖。
返回**原始 assistant message**，这样调用方可以把 tool_calls 原样回传给模型。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional


class LLMError(RuntimeError):
    """大模型不可用（未配置、网络失败、返回异常）。"""


class DeepSeekClient:
    default_model = "deepseek-chat"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[float] = None) -> None:
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_MODEL", self.default_model)
        self.timeout = float(timeout or os.getenv("DEEPSEEK_TIMEOUT", "60"))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY 未配置")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"大模型返回 {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMError(f"大模型请求失败: {exc}") from exc

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None,
             temperature: float = 0.2, max_tokens: int = 1024) -> dict[str, Any]:
        """返回 choices[0].message（含可能存在的 tool_calls）。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._request("/v1/chat/completions", payload)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("大模型返回结构异常") from exc

    def complete(self, system: str, user: str) -> str:
        message = self.chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}])
        return (message.get("content") or "").strip()
