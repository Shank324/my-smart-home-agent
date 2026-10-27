"""Agent 编排：模型 ↔ 工具的多轮循环。

职责边界很清楚：
- 本模块只负责「让模型决定下一步」和「把工具结果喂回去」；
- 具体能做什么，全部由外部注入的 ToolRegistry 决定；
- 所以同一套循环既能跑客服，也能跑运维助手。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from .llm import DeepSeekClient
from .tools import ToolError, ToolRegistry

DEFAULT_SYSTEM_PROMPT = """你是「智家管家」，一个智能家居平台的客服助手。

你的核心价值是：把用户的**生活困扰**翻译成可以直接落地的设备方案。

用户很少说「把空调调成除湿」，他们说的是：
- 「晚上睡不好，感觉空气很粘」
- 「下班回家想马上洗个热水澡」
- 「周末想在家看电影」

遇到这类描述，直接产出方案，不要先反问「要不要我帮你设计」：
1. 用 list_devices 看清家里有哪些设备、分别能调什么参数（拿不准可先 search_knowledge）
2. 想清楚动哪些设备、各自调到什么值
3. **必须调用 propose_scene** 输出结构化方案——界面上会出现「一键布设」按钮。
   只在文字里描述方案、不调用工具，是错误的做法。
4. 文字部分只写判断依据和需要提醒的点（安全、注意事项），不要逐条重复方案内容
5. 用户说「温度再低点」「灯再暗一些」时，在上一轮方案基础上修改，然后重新调用 propose_scene

只有事实性问题（设备在哪、工单状态、故障原因）才只回答不建方案。

其它工具：
- search_knowledge：查设备手册和常见问题，回答故障、操作方法前必须先查
- get_device_status：单个设备的详细状态
- analyze_energy：统计最近 N 天各设备/各房间的用电量与运行时长。
  用户问「电费为什么高」「昨天用电多少」「空调开了多久」时必须调用它，不要凭感觉答
- list_work_orders：查报修记录

报修怎么处理（很重要）：
用户说「报修 / 找人修 / 坏了要修」时，**不要直接建单，先补全信息**：
1. 先用 list_devices 找到是哪台设备，用 get_device_status 查它的在线状态、当前值、是否有故障码
2. 自动能查到的信息（设备名、房间、在线状态、当前数值）不要问用户
3. **只要「哪台设备 + 什么现象」齐了，就立刻调用 propose_work_order 出草稿**，
   不要为了少一次追问而卡住。缺的次要信息（何时开始、是否断电、具体哪盏灯）
   写进 pending_info，草稿里会显示成「待确认」，用户确认前还能补充
4. 只有连设备和现象都没有时（如「我家有东西坏了」），才用一两句话追问，不要连问一串
5. 涉及安全的（漏水、冒烟、漏电、焦糊味）优先级用 high 或 urgent。
   安全提示放在文字里说，**但不要因为要先给安全提示就不出草稿**
6. 出草稿后，文字里说明「已整理成草稿，确认后提交」以及还需要用户补充什么

回答要求：
1. 涉及设备故障、操作方法时，先用 search_knowledge 查证，不要凭记忆回答。
2. 参数必须落在设备支持的范围内，不确定就先查 list_devices。
3. 中文，简洁分点，不要暴露工具名和内部字段名。
4. 资料不足时如实说明，并建议提交报修工单。
5. 涉及漏电、燃气、烟雾等安全风险时，先强调断电、通风、联系专业人员。
"""

MAX_HISTORY = 8


class Assistant:
    def __init__(self, llm: DeepSeekClient, registry: ToolRegistry,
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT, max_steps: int = 6) -> None:
        self.llm = llm
        self.registry = registry
        self.system_prompt = system_prompt
        self.max_steps = max_steps

    def _prepare_history(self, history: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in (history or [])[-MAX_HISTORY:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                cleaned.append({"role": role, "content": content.strip()})
        return cleaned

    def _stream_once(self, messages: list[dict[str, Any]],
                     on_token: Callable[[str], None]) -> dict[str, Any]:
        """消费一次流式响应：文本实时回调，返回组装好的完整 message（含 tool_calls）。"""
        final: dict[str, Any] = {"role": "assistant", "content": ""}
        for kind, payload in self.llm.chat_stream(messages, tools=self.registry.specs()):
            if kind == "delta":
                on_token(payload)
            else:
                final = payload
        return final

    def chat(self, message: str, history: Optional[list[dict[str, Any]]] = None,
             on_token: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
        """跑一轮工具调用循环。

        on_token 不为空时走流式：模型每吐一段文本就回调一次，
        前端可以边生成边显示。注意工具调用轮次的 content 通常是空的，
        所以「先流式输出又发现要调工具」在真实响应里不会出现。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        messages += self._prepare_history(history)
        messages.append({"role": "user", "content": message})

        citations: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        evidence: list[str] = []
        proposal: Optional[dict[str, Any]] = None
        work_order: Optional[dict[str, Any]] = None
        answer = ""

        for step in range(1, self.max_steps + 1):
            reply = (self.llm.chat(messages, tools=self.registry.specs()) if on_token is None
                     else self._stream_once(messages, on_token))
            tool_calls = reply.get("tool_calls") or []
            answer = (reply.get("content") or "").strip()

            if not tool_calls:
                return {"answer": answer, "citations": citations, "tool_calls": trace,
                        "proposal": proposal, "work_order": work_order,
                        "evidence": "\n".join(evidence), "steps": step}

            # 把 assistant 的 tool_calls 原样回传，否则下一轮模型会丢上下文
            messages.append({"role": "assistant", "content": answer, "tool_calls": tool_calls})

            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                raw_arguments = function.get("arguments") or "{}"
                try:
                    result = self.registry.invoke(name, raw_arguments)
                    ok = True
                except ToolError as exc:
                    result = {"error": str(exc)}
                    ok = False

                if name == "search_knowledge":
                    for hit in result.get("results", []):
                        key = (hit.get("source"), hit.get("section"))
                        if key not in {(c["source"], c["section"]) for c in citations}:
                            citations.append({"source": hit.get("source", ""),
                                              "section": hit.get("section", "")})
                if name == "propose_scene" and isinstance(result.get("proposal"), dict):
                    proposal = result["proposal"]
                if name == "propose_work_order" and isinstance(result.get("work_order"), dict):
                    work_order = result["work_order"]

                evidence.append(json.dumps(result, ensure_ascii=False)[:4000])

                trace.append({"name": name, "arguments": raw_arguments, "ok": ok,
                              "result_preview": json.dumps(result, ensure_ascii=False)[:300]})
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": json.dumps(result, ensure_ascii=False)[:4000]})

        return {"answer": answer or "抱歉，这个问题我需要更多信息才能回答。",
                "citations": citations, "tool_calls": trace, "proposal": proposal,
                "work_order": work_order, "evidence": "\n".join(evidence),
                "steps": self.max_steps, "truncated": True}
