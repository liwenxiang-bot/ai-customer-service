#!/usr/bin/env bash
# 回滚到上一次 `make release` 之前的版本（docker 全栈）。
#
# 用法（在服务器仓库目录）：
#   bash deploy/rollback.sh               # 默认：先用「当前（新）代码」把数据库结构降级到发版前版本，
#                                         #       再回退代码并重建。数据保留。
#   bash deploy/rollback.sh --restore-db  # 用发版前的备份整库恢复，再回退代码。
#                                         #       发版之后产生的新数据会丢失——只在迁移把数据弄坏时使用。
#
# 为什么不能只 git reset：新版本若带数据库迁移，旧代码的 migrate 容器不认识库里的新 revision，
# `alembic upgrade head` 直接失败 → backend/worker 起不来 → 停机。
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose --profile app"
MODE="downgrade"
[ "${1:-}" = "--restore-db" ] && MODE="restore"

[ -f .last_release_commit ] || { echo "找不到 .last_release_commit（没有通过 make release 发过版）" >&2; exit 1; }
OLD_COMMIT="$(cat .last_release_commit)"
OLD_REV="$(cat .last_release_db_rev 2>/dev/null || true)"
BACKUP="$(cat .last_release_backup 2>/dev/null || true)"
CUR_REV="$(docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select version_num from alembic_version"' | tr -d '[:space:]')"

echo "回滚计划："
echo "  代码：$(git rev-parse --short HEAD) → ${OLD_COMMIT:0:7}"
if [ "$MODE" = "restore" ]; then
  [ -n "$BACKUP" ] && [ -f "$BACKUP" ] || { echo "找不到发版前备份（.last_release_backup）" >&2; exit 1; }
  echo "  数据库：用 $BACKUP 整库恢复（发版后的新数据会丢失）"
else
  echo "  数据库结构：${CUR_REV:-?} → ${OLD_REV:-（未记录，跳过降级）}（数据保留）"
fi
read -r -p "确认执行？输入 yes 继续：" ans
[ "$ans" = "yes" ] || { echo "已取消"; exit 1; }

if [ "$MODE" = "restore" ]; then
  gunzip -t "$BACKUP"
  SAFETY="backups/acs_before_rollback_$(date +%F_%H%M%S).sql.gz"
  echo "==> 先备份当前库（以防回滚本身需要撤销）：$SAFETY"
  docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$SAFETY"
  gunzip -t "$SAFETY"
  echo "==> 停止 backend / worker"
  $COMPOSE stop backend worker
  echo "==> 清空并恢复数据库"
  docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"'
  gunzip -c "$BACKUP" | docker compose exec -T postgres sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null
elif [ -n "$OLD_REV" ] && [ "$CUR_REV" != "$OLD_REV" ]; then
  echo "==> 降级数据库结构 $CUR_REV → $OLD_REV（使用当前镜像里的 downgrade）"
  $COMPOSE run --rm --no-deps migrate alembic downgrade "$OLD_REV"
fi

echo "==> 回退代码并重建"
git reset --hard "$OLD_COMMIT"
$COMPOSE up -d --build

ok=""
for _ in $(seq 1 30); do
  if $COMPOSE exec -T backend curl -fsS http://localhost:8000/health/live >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || { echo "!! backend 未在 60s 内就绪：$COMPOSE logs --tail=50 backend migrate" >&2; exit 1; }
echo "✅ 已回滚到 ${OLD_COMMIT:0:7}"
