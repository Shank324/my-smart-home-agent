#!/usr/bin/env bash
# 生产部署：构建前端 → 起容器 → 初始化数据库与索引 → 自检
#
#   ./scripts/deploy.sh              # 完整部署
#   ./scripts/deploy.sh --skip-build # 只重启服务（前端没改时）
#
# 前置：仓库根目录有 .env（从 .env.example 复制并填值）
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.prod.yml"
WEB_PORT="${WEB_PORT:-8080}"

if [ ! -f .env ]; then
  echo "缺少 .env：先执行 cp .env.example .env 并填写 JWT_SECRET / MYSQL_ROOT_PASSWORD / API Key"
  exit 1
fi

for key in JWT_SECRET MYSQL_ROOT_PASSWORD; do
  if ! grep -q "^${key}=.\+" .env || grep -q "^${key}=CHANGE_ME" .env; then
    echo ".env 里的 ${key} 还没填（不能是默认值）"
    exit 1
  fi
done

if [ "${1:-}" != "--skip-build" ]; then
  echo "==> 构建前端（同源模式，产物在 dist/）"
  npm ci
  npm run build
fi

echo "==> 启动数据库与缓存"
$COMPOSE up -d mysql redis

echo "==> 初始化数据库结构与种子数据（可重复执行）"
$COMPOSE run --rm --no-deps api python -m backend.bootstrap

echo "==> 启动 API 与 Nginx"
$COMPOSE up -d --build api web

echo "==> 等待健康检查"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${WEB_PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo "==> 初始化知识库索引"
curl -fsS -X POST "http://127.0.0.1:${WEB_PORT}/api/knowledge/init" || true
echo

echo "==> 状态"
curl -fsS "http://127.0.0.1:${WEB_PORT}/api/health" && echo
$COMPOSE ps

echo
echo "部署完成： http://<服务器地址>:${WEB_PORT}"
echo "首次登录： user1 / home123 —— 上线后请立刻改口令并删除演示账号"
