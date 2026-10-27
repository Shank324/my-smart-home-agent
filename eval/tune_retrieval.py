"""检索通道配比消融：同一个评测集，跑不同权重，输出对比表。

用法（宿主机，需要 api 已经在跑）：
    py eval/tune_retrieval.py
它会依次改环境变量重启容器、重跑评测，最后打印一张表。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "retrieval_questions.json"
BASE = "http://127.0.0.1:8000"

CONFIGS = [
    ("baseline v1.0/b1.0", {"KNOWLEDGE_VECTOR_WEIGHT": "1.0", "KNOWLEDGE_BM25_WEIGHT": "1.0",
                            "KNOWLEDGE_QUERY_REWRITE": "0"}),
    ("bm25 1.2", {"KNOWLEDGE_VECTOR_WEIGHT": "1.0", "KNOWLEDGE_BM25_WEIGHT": "1.2",
                  "KNOWLEDGE_QUERY_REWRITE": "0"}),
    ("bm25 1.5", {"KNOWLEDGE_VECTOR_WEIGHT": "1.0", "KNOWLEDGE_BM25_WEIGHT": "1.5",
                  "KNOWLEDGE_QUERY_REWRITE": "0"}),
]

env = dict(os.environ)


def restart(overrides: dict[str, str]) -> None:
    merged = dict(env)
    merged.update(overrides)
    subprocess.run(["docker", "compose", "up", "-d", "api"], cwd=ROOT, env=merged,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{BASE}/api/health", timeout=2)
            return
        except Exception:                                     # noqa: BLE001
            time.sleep(1)
    raise SystemExit("api 没有起来")


def search(query: str, top_k: int = 5) -> list[dict]:
    payload = json.dumps({"query": query, "top_k": top_k}, ensure_ascii=False).encode()
    request = urllib.request.Request(f"{BASE}/api/knowledge/search", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())["results"]


def matches(hit: dict, gold: list[list[str]]) -> bool:
    doc, section = hit.get("source_doc", ""), hit.get("section", "")
    return any(doc == gd and (not gs or gs in section or section == gs) for gd, gs in gold)


def evaluate(top_k: int = 5) -> dict:
    items = json.loads(QUESTIONS.read_text(encoding="utf-8"))["items"]
    hit1 = hitk = hit3 = 0
    reciprocal = []
    for item in items:
        results = search(item["q"], top_k)
        ranks = [i for i, hit in enumerate(results, 1) if matches(hit, item["gold"])]
        if ranks:
            hitk += 1
            hit1 += ranks[0] == 1
            hit3 += ranks[0] <= 3
            reciprocal.append(1.0 / ranks[0])
        else:
            reciprocal.append(0.0)
    total = len(items)
    return {"recall_at_1": round(hit1 / total, 4), "recall_at_3": round(hit3 / total, 4),
            "recall_at_5": round(hitk / total, 4),
            "mrr": round(sum(reciprocal) / total, 4)}


def main() -> int:
    rows = []
    for label, overrides in CONFIGS:
        restart(overrides)
        result = evaluate()
        rows.append((label, result))
        print(f"{label:22s} R@1 {result['recall_at_1']:.3f}  R@3 {result['recall_at_3']:.3f}  "
              f"R@5 {result['recall_at_5']:.3f}  MRR {result['mrr']:.4f}", flush=True)
    best = max(rows, key=lambda row: (row[1]["recall_at_1"], row[1]["mrr"]))
    print(f"\n最优: {best[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
