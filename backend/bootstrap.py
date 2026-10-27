"""一次性初始化：建表 + 种子数据 + 视图/触发器。

生产环境不在应用启动时做这些事：
N 个 worker 同时启动会并发执行 DDL 和种子写入，轻则报错重则数据重复。
所以拆成独立步骤，由部署脚本在任何 worker 起来之前跑一次（可重复执行）。

    python -m backend.bootstrap
"""

from __future__ import annotations

import logging

from backend.db_objects import ensure_db_objects
from backend.seed import seed_database

logger = logging.getLogger(__name__)


def bootstrap() -> dict[str, object]:
    seed_database()
    created = ensure_db_objects()
    logger.info("bootstrap 完成: %s", created)
    return created


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    created = bootstrap()
    print(f"bootstrap ok: {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
