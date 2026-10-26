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

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
