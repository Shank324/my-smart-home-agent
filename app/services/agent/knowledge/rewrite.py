"""查询改写：把口语化的问题变成更适合检索的检索式。

为什么需要：用户问「点了开关但是设备没反应」，手册里写的是「指令下发无响应」，
两者几乎没有共同词，纯词面检索必然召不回。改写一次补上书面表达就能救回来。

代价是每次检索多一次 LLM 调用，所以做了三件事控制成本：
- 结果按原句缓存，同一句话只改写一次
- 短句（< 4 字）不改写，没什么可改的
- 任何异常都退回原句，检索不能因为改写失败而不可用
"""

from __future__ import annotations

from typing import Any, Optional

PROMPT = (
    "你是检索式改写器。把用户的口语化问题改写成 {count} 条更利于关键词检索的短检索式。\n"
    "要求：\n"
    "- 每行一条，不要编号、不要解释、不要标点前缀\n"
    "- 保留设备名称、故障代码、房间名等专有词\n"
    "- 把口语换成手册里常见的书面表达（例如「没反应」→「无响应」，「一直闪」→「闪烁」）\n"
    "- 每条不超过 25 字"
)


class QueryRewriter:
    def __init__(self, llm: Optional[Any] = None, max_variants: int = 2) -> None:
        self.llm = llm
        self.max_variants = max_variants
        self._cache: dict[str, list[str]] = {}

    @property
    def available(self) -> bool:
        return self.llm is not None and getattr(self.llm, "configured", False)

    def expand(self, query: str) -> list[str]:
        text = (query or "").strip()
        if not text or len(text) < 4 or not self.available:
            return []
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        variants: list[str] = []
        try:
            reply = self.llm.chat([
                {"role": "system", "content": PROMPT.format(count=self.max_variants)},
                {"role": "user", "content": text},
            ])
            variants = self._parse(reply.get("content") or "", text)
        except Exception:                                       # noqa: BLE001
            variants = []
        self._cache[text] = variants
        return variants

    def _parse(self, content: str, original: str) -> list[str]:
        seen = {original}
        result: list[str] = []
        for line in content.splitlines():
            item = line.strip().lstrip("-*0123456789.、) ").strip()
            if not item or len(item) > 50 or item in seen:
                continue
            seen.add(item)
            result.append(item)
            if len(result) >= self.max_variants:
                break
        return result
