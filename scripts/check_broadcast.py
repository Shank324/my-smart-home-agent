"""验证「跨实例实时广播」：连实例 B 的 WebSocket，然后在实例 A 上触发事件。

前提：两个 API 实例都连同一个 Redis 和同一个数据库。

用法：
    docker compose exec -d api uvicorn backend.main:app --host 0.0.0.0 --port 8001
    docker compose exec api python scripts/check_broadcast.py --listener http://127.0.0.1:8001
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

PRODUCER = "http://127.0.0.1:8000"


def login(base: str, username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password}).encode()
    request = urllib.request.Request(f"{base}/api/auth/login", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())["access_token"]


def pick_device(token: str) -> dict:
    request = urllib.request.Request(f"{PRODUCER}/api/devices",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())[0]


def send_command(token: str, device_id: int, action: str) -> None:
    payload = json.dumps({"action": action}).encode()
    request = urllib.request.Request(
        f"{PRODUCER}/api/devices/{device_id}/commands", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listener", default="http://127.0.0.1:8001",
                        help="接收端实例地址（另一个 API 进程）")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    import websockets

    token = login(PRODUCER, "user1", "home123")
    device = pick_device(token)
    ws_url = args.listener.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws/events?token={token}"

    async with websockets.connect(ws_url) as socket:
        print(f"已连接接收端 {ws_url.split('?')[0]}")
        await asyncio.sleep(0.5)

        action = "off" if device["active"] else "on"
        print(f"在发送端触发事件：{device['name']} -> {action}")
        send_command(token, device["id"], action)

        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print("❌ 超时未收到事件：说明广播没有跨实例")
            return 1

    message = json.loads(raw)
    ok = (message.get("event") == "device_updated"
          and message.get("data", {}).get("id") == device["id"])
    print(f"收到事件：{message.get('event')} device={message.get('data', {}).get('id')}")
    print("✅ 跨实例广播生效" if ok else "❌ 收到的内容不符合预期")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
