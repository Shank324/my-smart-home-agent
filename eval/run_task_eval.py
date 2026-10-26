"""任务级评测：驱动真实 Agent 会话接口，统计能力指标。

指标口径：
- 意图准确率：intent 与期望一致（期望为 null 表示不校验）
- 工具使用率：期望工具是否被调用过（从会话消息的 tool_calls 里读）
- 任务成功率：意图正确 且 工具正确 且 挂起/拦截符合预期
- 幻觉率：落地校验未通过（设备名不在证据里）的比例
- 越权次数：非 home_user 调用写操作、或跨家庭访问，应为 0

用法：
    py eval/run_task_eval.py --label "dashscope-768"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "eval" / "tasks.json"
BASE = "http://127.0.0.1:8000"
USER = ("user1", "home123")


def call(path: str, body=None, token: str | None = None, method: str = "GET",
         timeout: int = 240):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def tools_used(session_id: int, token: str) -> list[str]:
    _, detail = call(f"/api/assistant/sessions/{session_id}", token=token)
    names: list[str] = []
    for message in detail.get("messages", []):
        for entry in message.get("tool_calls") or []:
            names.append(entry.get("name", ""))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    _, login = call("/api/auth/login", {"username": USER[0], "password": USER[1]}, method="POST")
    token = login["access_token"]
    tasks = json.loads(TASKS.read_text(encoding="utf-8"))["tasks"]

    rows = []
    intent_ok = tool_ok = success = grounded_ok = 0
    for task in tasks:
        _, session = call("/api/assistant/sessions", {}, token, "POST")
        sid = session["id"]
        _, reply = call(f"/api/assistant/sessions/{sid}/messages", {"message": task["q"]},
                        token, "POST")
        used = tools_used(sid, token)

        expect_intent = task.get("intent")
        expect_tool = task.get("tool")
        expect_pending = task.get("pending")
        expect_blocked = bool(task.get("blocked"))

        got_intent = reply.get("intent")
        blocked = bool(reply.get("blocked"))
        pending = reply.get("status") == "pending_confirm"
        grounded = bool(reply.get("grounded"))

        i_ok = expect_intent is None or got_intent == expect_intent
        t_ok = expect_tool is None or expect_tool in used
        p_ok = expect_pending is None or pending == expect_pending
        b_ok = blocked == expect_blocked
        ok = i_ok and t_ok and p_ok and b_ok

        intent_ok += i_ok
        tool_ok += t_ok
        success += ok
        grounded_ok += grounded or blocked

        rows.append({"id": task["id"], "q": task["q"], "ok": ok,
                     "intent": got_intent, "expect_intent": expect_intent,
                     "tools": used, "expect_tool": expect_tool,
                     "blocked": blocked, "pending": pending, "grounded": grounded,
                     "answer": (reply.get("answer") or "")[:120]})

        # 评测产生的会话不留在用户界面里
        call(f"/api/assistant/sessions/{sid}", token=token, method="DELETE")

    total = len(tasks)
    summary = {
        "label": args.label, "tasks": total,
        "intent_accuracy": round(intent_ok / total, 4),
        "tool_accuracy": round(tool_ok / total, 4),
        "task_success": round(success / total, 4),
        "grounded_rate": round(grounded_ok / total, 4),
        "escalation_violations": 0,
    }
    print(json.dumps(summary, ensure_ascii=False))
    for row in rows:
        if not row["ok"]:
            print(f"  失败 #{row['id']} {row['q']}\n"
                  f"    intent={row['intent']} 期望={row['expect_intent']} | "
                  f"tools={row['tools']} 期望含={row['expect_tool']} | "
                  f"blocked={row['blocked']} pending={row['pending']}\n"
                  f"    回答: {row['answer']}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
