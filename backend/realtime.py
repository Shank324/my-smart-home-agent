"""实时通道：本实例连接表 + Redis Pub/Sub 跨实例广播。

单实例时行为和直接发消息完全一样；一旦配了 REDIS_URL 且连得上，
每条事件先 publish 到频道，各实例的订阅任务收到后再推给自己这边的连接。
Redis 不可用时**自动退回本地广播**——实时推送是增强功能，
它挂掉不应该让业务请求失败。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 连接 -> 归属家庭；维修人员用 -1 表示「收全部工单事件」
CLIENTS: dict[Any, int] = {}
CHANNEL = os.getenv("REALTIME_CHANNEL", "smarthome:events")

_publisher: Optional[Any] = None
_listener: Optional[asyncio.Task[None]] = None


def visible(client_home_id: int, event: str, home_id: int) -> bool:
    """广播层也做一次可见性判断，不能只依赖接口层的权限校验。"""
    if event.startswith("work_order"):
        return client_home_id in (home_id, -1)
    return client_home_id == home_id


async def fanout(event: str, data: Any, home_id: int) -> None:
    """把事件推给本实例上所有该看到的连接。"""
    stale = []
    for client, client_home_id in list(CLIENTS.items()):
        if not visible(client_home_id, event, home_id):
            continue
        try:
            await client.send_json({"event": event, "data": data})
        except Exception:                                  # noqa: BLE001
            stale.append(client)
    for client in stale:
        CLIENTS.pop(client, None)


async def broadcast(event: str, data: Any, home_id: int) -> None:
    if _publisher is not None:
        try:
            await _publisher.publish(
                CHANNEL, json.dumps({"event": event, "home_id": home_id, "data": data},
                                    ensure_ascii=False, default=str))
            return
        except Exception as exc:                           # noqa: BLE001
            logger.warning("Redis 广播失败，退回本地广播：%s", exc)
    await fanout(event, data, home_id)


async def _listen(client: Any) -> None:
    pubsub = client.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                body = json.loads(message["data"])
                await fanout(str(body.get("event", "")), body.get("data"),
                             int(body.get("home_id", 0)))
            except (ValueError, TypeError):                # 单条坏消息不该打断订阅
                logger.warning("收到无法解析的广播消息")
    except asyncio.CancelledError:
        raise
    except Exception as exc:                               # noqa: BLE001
        logger.warning("Redis 订阅中断：%s", exc)
    finally:
        try:
            await pubsub.close()
        except Exception:                                  # noqa: BLE001
            pass


async def start(redis_url: Optional[str] = None) -> bool:
    """尝试启用 Redis 广播，成功返回 True；失败则保持单实例模式。"""
    global _publisher, _listener
    url = redis_url or os.getenv("REDIS_URL")
    if not url:
        return False
    try:
        from redis.asyncio import Redis
    except ImportError:
        logger.warning("未安装 redis 客户端，实时广播降级为单实例")
        return False
    try:
        client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=None,
                                decode_responses=True)
        await client.ping()
    except Exception as exc:                               # noqa: BLE001
        logger.warning("Redis 不可用，实时广播降级为单实例：%s", exc)
        return False
    _publisher = client
    _listener = asyncio.create_task(_listen(client))
    logger.info("实时广播已启用 Redis Pub/Sub（频道 %s）", CHANNEL)
    return True


async def stop() -> None:
    global _publisher, _listener
    if _listener is not None:
        _listener.cancel()
        try:
            await _listener
        except (asyncio.CancelledError, Exception):        # noqa: BLE001
            pass
        _listener = None
    if _publisher is not None:
        try:
            await _publisher.aclose()
        except Exception:                                  # noqa: BLE001
            pass
        _publisher = None
