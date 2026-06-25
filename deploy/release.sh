#!/usr/bin/env bash
# 线上增量发版（docker 全栈）：备份 → 拉代码 → 重建+迁移 → 回填 → 健康检查
#
# 用法（在服务器上，仓库任意位置）：
#   make release            # 推荐
#   bash deploy/release.sh
#
# 适用于「已部署、增量更新」。首次部署见 README 生产部署要点。
# 回滚：git reset --hard $(cat .last_release_commit) && make stack-up
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose --profile app"

echo "==> [1/6] 记录当前版本（回滚锚点 → .last_release_commit）"
git rev-parse HEAD | tee .last_release_commit

echo "==> [2/6] 备份数据库到 backups/"
mkdir -p backups
BACKUP="backups/acs_$(date +%F_%H%M%S).sql.gz"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$BACKUP"
echo "    备份完成：$BACKUP（$(du -h "$BACKUP" | cut -f1)）"

echo "==> [3/6] 拉取最新代码"
git pull --ff-only

echo "==> [4/6] 重建镜像 + 滚动重启（migrate 容器自动跑 alembic 迁移）"
$COMPOSE up -d --build

echo "==> [5/6] 等待 backend 健康（最长 ~60s）"
ok=""
for _ in $(seq 1 30); do
  if $COMPOSE exec -T backend curl -fsS http://localhost:8000/health/live >/dev/null 2>&1; then
    ok=1
    echo "    backend 健康 ✓"
    break
  fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo "    !! backend 未在 60s 内就绪。排查：$COMPOSE logs --tail=50 backend migrate" >&2
  exit 1
fi

echo "==> [6/6] 回填存量知识库中文分词索引（幂等，无数据时为空操作）"
$COMPOSE exec -T backend python -m scripts.resegment_chunks

echo ""
echo "✅ 发版完成（$(git rev-parse --short HEAD)）"
echo "   · 服务状态：$COMPOSE ps"
echo "   · 迁移日志：$COMPOSE logs --tail=20 migrate"
echo "   · 后台记得把 vector_min_sim 设为 0.5（text-embedding-v4 校准值）"
echo "   · 回滚：git reset --hard \$(cat .last_release_commit) && make stack-up"
