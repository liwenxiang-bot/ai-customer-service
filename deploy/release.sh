#!/usr/bin/env bash
# 线上增量发版（docker 全栈）：备份 → 拉代码 → 重建+迁移 → 回填 → 健康检查
#
# 用法（在服务器上，仓库任意位置）：
#   make release            # 推荐
#   bash deploy/release.sh
#
# 适用于「已部署、增量更新」。首次部署见 README 生产部署要点。
# 回滚：bash deploy/rollback.sh（代码 + 数据库结构一起回退，见脚本头部说明）
#
# 可选环境变量：
#   KEEP_BACKUPS=14          保留最近 N 份备份（默认 14）
#   BACKUP_UPLOAD_CMD="..."  备份完成后执行的异地上传命令，备份文件路径作为最后一个参数，
#                            例：BACKUP_UPLOAD_CMD="ossutil cp -f" 或 "rclone copy --no-traverse"
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="docker compose --profile app"
KEEP_BACKUPS="${KEEP_BACKUPS:-14}"

psql_q() {
  docker compose exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\""
}

echo "==> [1/6] 记录回滚锚点（代码 → .last_release_commit，库结构 → .last_release_db_rev）"
git rev-parse HEAD | tee .last_release_commit
psql_q "select version_num from alembic_version" | tr -d '[:space:]' | tee .last_release_db_rev
echo

echo "==> [2/6] 备份数据库到 backups/ 并校验"
mkdir -p backups
BACKUP="backups/acs_$(date +%F_%H%M%S).sql.gz"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$BACKUP"
# 不完整的备份 = 没有备份：校验 gzip 完整性与 pg_dump 结束标记，不通过就中止发版。
DUMP_TAIL="$(gunzip -c "$BACKUP" 2>/dev/null | tail -n 20 || true)"
if ! gunzip -t "$BACKUP" || ! grep -q "PostgreSQL database dump complete" <<<"$DUMP_TAIL"; then
  echo "    !! 备份校验失败：$BACKUP 不完整，已中止发版（代码未更新）" >&2
  exit 1
fi
echo "$BACKUP" > .last_release_backup
echo "    备份完成并已校验：$BACKUP（$(du -h "$BACKUP" | cut -f1)）"
if [ -n "${BACKUP_UPLOAD_CMD:-}" ]; then
  # shellcheck disable=SC2086 # 允许命令带参数
  $BACKUP_UPLOAD_CMD "$BACKUP" && echo "    已上传到异地" || echo "    !! 异地上传失败（本地备份仍在）" >&2
else
  echo "    提示：未配置 BACKUP_UPLOAD_CMD，备份只在本机——磁盘损坏会一起丢失"
fi
# 轮转：只保留最近 KEEP_BACKUPS 份
ls -1t backups/acs_*.sql.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | while read -r old; do
  rm -f -- "$old"
done

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
echo "   · 回滚：bash deploy/rollback.sh（默认降级库结构并保留数据；--restore-db 用发版前备份整库恢复）"
