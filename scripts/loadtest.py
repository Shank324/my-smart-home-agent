"""极简压测：固定并发打一个接口，输出 QPS 与 P50/P95/P99。

为什么不用 locust/wrk：只需要一个端点的数字，标准库的线程池足够，
也免得多装一个依赖。它的局限要先说清楚——客户端和被压服务在**同一台机器**上，
CPU 是共享的，所以数字只能说明「量级」，不能当成生产容量。

用法：
    python scripts/loadtest.py --path /api/health --concurrency 20 --seconds 10
    python scripts/loadtest.py --path /api/devices --concurrency 20 --seconds 10 --login user1:home123
    python scripts/loadtest.py --path /api/knowledge/search --method POST \
        --body '{"query":"灯一直闪","top_k":3}' --concurrency 4 --seconds 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request


def login(base: str, credentials: str) -> str:
    username, _, password = credentials.partition(":")
    payload = json.dumps({"username": username, "password": password}).encode()
    request = urllib.request.Request(f"{base}/api/auth/login", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())["access_token"]


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--path", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="", help="从文件读请求体，避免命令行转义问题")
    parser.add_argument("--login", default="", help="user:password，自动取 token")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    token = login(args.base, args.login) if args.login else ""
    raw_body = args.body
    if args.body_file:
        with open(args.body_file, encoding="utf-8") as handle:
            raw_body = handle.read()
    data = raw_body.encode("utf-8") if raw_body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + args.seconds

    def worker() -> None:
        local: list[float] = []
        local_errors = 0
        while time.perf_counter() < stop_at:
            request = urllib.request.Request(f"{args.base}{args.path}", data=data,
                                             headers=headers, method=args.method)
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read()
                    if response.status >= 400:
                        local_errors += 1
            except (urllib.error.URLError, TimeoutError, OSError):
                local_errors += 1
            local.append((time.perf_counter() - started) * 1000)
        with lock:
            latencies.extend(local)
            if local_errors:
                errors.append(f"{local_errors} 次失败")

    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    started_at = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started_at

    total = len(latencies)
    failed = sum(int(item.split()[0]) for item in errors)
    summary = {
        "label": args.label or f"{args.method} {args.path}",
        "concurrency": args.concurrency,
        "seconds": round(elapsed, 1),
        "requests": total,
        "failed": failed,
        "qps": round(total / elapsed, 1),
        "p50_ms": round(percentile(latencies, 0.50), 1),
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "p99_ms": round(percentile(latencies, 0.99), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
