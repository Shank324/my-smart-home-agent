"""检索评测：对 /api/knowledge/search 打 50 条问题，算 Recall@1 / Recall@5 / MRR。

用法：
    py eval/run_retrieval_eval.py --label "dashscope-768"
    py eval/run_retrieval_eval.py --label baseline --top-k 5 --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "eval" / "retrieval_questions.json"
BASE = "http://127.0.0.1:8000"


def search(query: str, top_k: int) -> list[dict]:
    payload = json.dumps({"query": query, "top_k": top_k}, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{BASE}/api/knowledge/search", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read().decode())["results"]


def matches(hit: dict, gold: list[list[str]]) -> bool:
    doc = (hit.get("source_doc") or "").strip()
    section = (hit.get("section") or "").strip()
    for gold_doc, gold_section in gold:
        if doc != gold_doc:
            continue
        if not gold_section or section == gold_section or gold_section in section:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    items = data["items"]
    hit1 = hit5 = 0
    reciprocal: list[float] = []
    misses: list[dict] = []

    for item in items:
        results = search(item["q"], args.top_k)
        ranks = [index for index, hit in enumerate(results, 1) if matches(hit, item["gold"])]
        if ranks:
            best = ranks[0]
            reciprocal.append(1.0 / best)
            hit5 += 1
            hit1 += best == 1
        else:
            reciprocal.append(0.0)
            misses.append({"id": item["id"], "q": item["q"],
                           "got": [f"{h['source_doc']}#{h['section']}" for h in results[:3]],
                           "want": [f"{d}#{s}" for d, s in item["gold"]]})

    total = len(items)
    summary = {
        "label": args.label,
        "top_k": args.top_k,
        "questions": total,
        "recall_at_1": round(hit1 / total, 4),
        "recall_at_k": round(hit5 / total, 4),
        "mrr": round(statistics.mean(reciprocal), 4),
        "misses": len(misses),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.quiet:
        for miss in misses:
            print(f"  漏召 #{miss['id']} {miss['q']}\n    期望 {miss['want']}\n    实得 {miss['got']}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "misses": misses},
                                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
