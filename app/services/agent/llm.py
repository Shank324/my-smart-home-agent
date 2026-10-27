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

    def chat_stream(self, messages: list[dict[str, Any]],
                    tools: Optional[list[dict[str, Any]]] = None,
                    temperature: float = 0.2, max_tokens: int = 1024):
        """流式对话：逐段产出 ("delta", 文本)，结束时产出 ("final", 完整 message)。

        工具调用在 SSE 里是**分片**过来的（同一 index 的 arguments 需要拼接），
        所以这里要按 index 累积，最后组装成和非流式一样的 message 结构。
        """
        if not self.api_key:
            raise LLMError("DEEPSEEK_API_KEY 未配置")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta") or {}
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield "delta", text
                    for piece in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            int(piece.get("index", 0)),
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}})
                        if piece.get("id"):
                            slot["id"] = piece["id"]
                        function = piece.get("function") or {}
                        if function.get("name"):
                            slot["function"]["name"] += function["name"]
                        if function.get("arguments"):
                            slot["function"]["arguments"] += function["arguments"]
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"大模型返回 {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMError(f"大模型请求失败: {exc}") from exc

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        yield "final", message
