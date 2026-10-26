"""数据库对象：视图、触发器、索引。

三点设计取舍：

1. **只在 MySQL 上创建**。SQLite 没有 `GROUP_CONCAT` 之外的等价语法，
   触发器写法也完全不同；本地开发用 SQLite 时自动跳过，统计接口会降级为 ORM 查询。
2. **用 information_schema 判断对象是否存在**，而不是靠匹配异常字符串
   （参考实现里用 `if "already exists" not in str(e)`，一旦错误信息本地化就会失效）。
3. **触发器只做一件事**：状态流转留痕 + 自动写 resolved_at。
   放在数据库层是因为状态可能从多条代码路径被修改，应用层写日志总会漏。
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.db import engine

logger = logging.getLogger("smarthome.dbobjects")

VIEWS: dict[str, str] = {
    "v_home_device_stats": """
        SELECT
            h.id                                   AS home_id,
            h.name                                 AS home_name,
            COUNT(d.id)                            AS device_total,
            COALESCE(SUM(d.online), 0)             AS device_online,
            COALESCE(SUM(d.active), 0)             AS device_active,
            COALESCE(SUM(d.power_consumption), 0)  AS power_total,
            COALESCE(SUM(CASE WHEN d.active = 1 THEN d.power_consumption ELSE 0 END), 0)
                                                   AS power_active
        FROM homes h
        LEFT JOIN devices d ON d.home_id = h.id
        GROUP BY h.id, h.name
    """,
    "v_technician_stats": """
        SELECT
            u.id                        AS technician_id,
            u.username                  AS username,
            u.display_name              AS display_name,
            COUNT(w.id)                 AS assigned_total,
            COALESCE(SUM(CASE WHEN w.status = 'resolved' THEN 1 ELSE 0 END), 0)
                                        AS resolved_total,
            ROUND(AVG(w.duration_minutes), 1) AS avg_duration_minutes,
            ROUND(AVG(w.cost), 2)             AS avg_cost
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.id
        JOIN roles r       ON r.id = ur.role_id AND r.name = 'repair_staff'
        LEFT JOIN work_orders w ON w.assigned_to = u.id
        GROUP BY u.id, u.username, u.display_name
    """,
    "v_home_repair_history": """
        SELECT
            h.id       AS home_id,
            h.name     AS home_name,
            COUNT(w.id) AS order_total,
            COALESCE(SUM(CASE WHEN w.status = 'resolved' THEN 1 ELSE 0 END), 0)
                        AS resolved_total,
            COALESCE(SUM(CASE WHEN w.status NOT IN ('resolved', 'cancelled') THEN 1 ELSE 0 END), 0)
                        AS pending_total,
            COALESCE(SUM(w.cost), 0) AS cost_total
        FROM homes h
        LEFT JOIN work_orders w ON w.home_id = h.id
        GROUP BY h.id, h.name
    """,
}

# 索引名 -> (表名, 列定义)
INDEXES: dict[str, tuple[str, str]] = {
    "idx_work_orders_home_status": ("work_orders", "home_id, status"),
    "idx_work_orders_assigned_to": ("work_orders", "assigned_to"),
    "idx_work_orders_priority": ("work_orders", "priority"),
    "idx_devices_home_type": ("devices", "home_id, type"),
    "idx_work_order_events_order": ("work_order_events", "work_order_id, id"),
    "idx_device_events_device": ("device_events", "device_id, id"),
    "idx_device_events_home_time": ("device_events", "home_id, created_at"),
}

# 触发器名 -> 建表语句
TRIGGERS: dict[str, str] = {
    "trg_work_order_status_change": """
        CREATE TRIGGER trg_work_order_status_change
        BEFORE UPDATE ON work_orders
        FOR EACH ROW
        BEGIN
            IF NEW.status <> OLD.status THEN
                IF NEW.status = 'resolved' THEN
                    SET NEW.resolved_at = UTC_TIMESTAMP();
                END IF;
                INSERT INTO work_order_events
                    (work_order_id, from_status, to_status, actor_id, created_at)
                VALUES
                    (NEW.id, OLD.status, NEW.status, NEW.assigned_to, UTC_TIMESTAMP());
            END IF;
        END
    """,
    # 设备状态变化留痕：开关 / 数值 / 在线状态任一变化都记一条
    "trg_device_state_change": """
        CREATE TRIGGER trg_device_state_change
        AFTER UPDATE ON devices
        FOR EACH ROW
        BEGIN
            IF NEW.active <> OLD.active OR NEW.value <> OLD.value OR NEW.online <> OLD.online THEN
                INSERT INTO device_events
                    (device_id, home_id, powered, value, power_w, attributes, created_at)
                VALUES
                    (NEW.id, NEW.home_id, NEW.active, NEW.value, NEW.power_consumption,
                     NEW.attributes, UTC_TIMESTAMP());
            END IF;
        END
    """,
    # 新设备建档时写入初始状态，历史才有起点
    "trg_device_created": """
        CREATE TRIGGER trg_device_created
        AFTER INSERT ON devices
        FOR EACH ROW
        INSERT INTO device_events
            (device_id, home_id, powered, value, power_w, attributes, created_at)
        VALUES
            (NEW.id, NEW.home_id, NEW.active, NEW.value, NEW.power_consumption,
             NEW.attributes, UTC_TIMESTAMP())
    """,
}


def _index_exists(conn, table: str, index_name: str) -> bool:
    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
    ), {"t": table, "i": index_name}).scalar())


def _trigger_exists(conn, trigger_name: str) -> bool:
    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.triggers "
        "WHERE trigger_schema = DATABASE() AND trigger_name = :n"
    ), {"n": trigger_name}).scalar())


def ensure_db_objects() -> dict[str, int]:
    """幂等地创建视图 / 索引 / 触发器。非 MySQL 直接跳过。"""
    if engine.dialect.name != "mysql":
        logger.info("跳过数据库对象创建：当前方言为 %s", engine.dialect.name)
        return {"views": 0, "indexes": 0, "triggers": 0, "skipped": 1}

    created = {"views": 0, "indexes": 0, "triggers": 0}
    with engine.begin() as conn:
        for name, body in VIEWS.items():
            conn.execute(text(f"CREATE OR REPLACE VIEW {name} AS {body}"))
            created["views"] += 1

        for name, (table, columns) in INDEXES.items():
            if _index_exists(conn, table, name):
                continue
            conn.execute(text(f"CREATE INDEX {name} ON {table} ({columns})"))
            created["indexes"] += 1

        for name, body in TRIGGERS.items():
            if _trigger_exists(conn, name):
                conn.execute(text(f"DROP TRIGGER {name}"))
            conn.execute(text(body))
            created["triggers"] += 1

    logger.info("数据库对象就绪: %s", created)
    return created
