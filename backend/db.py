"""数据库连接与会话工厂。

把 engine / SessionLocal / Base 独立出来，让 models、路由、Agent 工具层都能安全引用，
避免 `backend.main` 变成所有模块的循环依赖中心。
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://root:324215@mysql:3306/smarthome")

# SQLite 需要放开线程检查；MySQL 不需要这个参数
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# 连接池参数：默认 pool_size=5 / max_overflow=10 只够 15 个并发连接，
# 压测下第 16 个请求开始排队，P95 会被拖高。生产按 worker 数与并发调整。
_pool_options = {}
if not DATABASE_URL.startswith("sqlite"):
    _pool_options = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
        # MySQL 默认 8 小时断开空闲连接，回收要早于它，否则会拿到已失效的连接
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "1800")),
    }

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args,
                       **_pool_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
