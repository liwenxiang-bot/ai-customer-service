# 部署与发版手册

docker 全栈自托管。架构:终端用户 → Caddy/nginx(HTTPS/WSS)→ backend(`:8000`)/ admin(`:8080`);
应用端口仅绑 `127.0.0.1`,公网流量统一走反向代理。依赖为 postgres(pgvector)+ redis + minio,
数据在 docker volume。

---

## 一、首次部署(从零)

前提:Linux 服务器(≥2C4G)、已装 Docker、域名解析到公网 IP、放行 `80/443`(国内域名需备案)。

1. **拉代码 + 配置 env**
   ```bash
   git clone <repo-url> && cd ai-customer-service
   cp .env.production .env        # compose 读的是 .env
   ```
   编辑 `.env`,必改项:
   - `APP_SECRET_KEY` / `JWT_SECRET` → 各跑一次 `openssl rand -hex 32`
   - `POSTGRES_PASSWORD`、MinIO/S3 密钥、`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`
   - `APP_BASE_URL` / `ADMIN_BASE_URL` / `ADMIN_CORS_ORIGINS` → 你的域名
   - `LLM_*`、`EMBEDDING_*` —— ⚠️ `EMBEDDING_DIM` 必须与模型维度一致(text-embedding-v4 = **1024**),
     且要在**第一次启动前**设好(向量列建表时按它定)。

2. **起全栈**(migrate 容器自动 `alembic upgrade head` + bootstrap)
   ```bash
   make stack-up                          # = docker compose --profile app up -d --build
   docker compose --profile app ps        # 各服务 healthy
   ```

3. **反向代理 + HTTPS**:用 `deploy/Caddyfile`(改域名,admin 端口对齐 `ADMIN_PORT`,默认 8080)→
   `sudo systemctl reload caddy`。或用 `deploy/nginx.conf` + `certbot`(文件头部有命令)。

   > ⚠️ **chat 和 admin 两个域名都必须配 WebSocket 升级**(`proxy_http_version 1.1` +
   > `Upgrade`/`Connection "upgrade"`,见 `deploy/nginx.conf`)。漏在 admin 域名上时,后台
   > 实时会**静默退化成轮询**:坐席工作台消息不实时、「客服/客户正在输入」提示失效、
   > `ws/admin` 每 ~200ms 断一次疯狂重连(F12 → Network → WS 看到一堆 Finished)。
   > **certbot 重写或手动精简 nginx 配置时最容易把 admin 块这段弄丢**——nginx 用户尤其留意
   > (Caddy 自动透传 WSS,无此坑)。排查:`curl` 看 widget 没问题但后台不实时,基本就是这里。

4. **后台初始化**:打开 admin 域名 → 用 `BOOTSTRAP_ADMIN_*` 登录 → 立刻改密 →「AI 配置」确认
   对话/Embedding 模型与 Key、把 `vector_min_sim` 设为 **0.5**、打开「上下文扩展」→「渠道配置」加 widget 域名白名单。

---

## 二、日常更新(增量发版)

### 首次更新(线上还没有 `make release`)

`make release` 这套机制在新代码里,首次需手动拉一次拿到它:

```bash
cd <项目目录>
git rev-parse HEAD        # ① 复制保存这串旧版本号(首次回滚用)
git pull                  # ② 拉到最新 main
make release              # ③ 一键发版
```
完成后到后台把 `vector_min_sim` 设为 **0.5**(存数据库,只需设这一次)。

### 以后每次更新

```bash
make release
```
内部自带 `git pull`,无需先手动拉。

---

## 三、`make release` 做了什么

1. 记录当前版本到 `.last_release_commit`(回滚锚点)
2. `pg_dump` 备份数据库到 `backups/`
3. `git pull --ff-only`
4. `docker compose --profile app up -d --build`(migrate 容器自动跑 alembic 迁移)
5. 轮询 backend `/health/live` 直到健康(~60s 超时则报错并提示看日志)
6. `resegment_chunks` 回填存量知识库中文分词索引(幂等)

任一步失败即停下,不会带着半成品继续。

---

## 四、回滚

```bash
# 首次那次:用 ① 记下的旧版本号
git reset --hard <旧版本号> && make stack-up

# 以后:直接用锚点文件
git reset --hard $(cat .last_release_commit) && make stack-up
```
迁移 `0003` 向后兼容,**代码回滚无需动数据库**。仅当数据真出问题才用备份恢复:
```bash
gunzip -c backups/acs_YYYY-MM-DD_HHMMSS.sql.gz \
  | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

---

## 五、运维

- **备份**:`make release` 每次自动备份到 `backups/`;手动备份同第三节第 2 步。建议再挂 cron 定期备份并异地保存。
- **健康检查**:`docker compose --profile app ps`;内网 `curl http://127.0.0.1:8000/health/live`。
- **检索校准**:`EMBEDDING_DIM` 必须与后台 embedding 模型维度一致;`vector_min_sim` 按模型调
  (text-embedding-v4 ≈ **0.5–0.6**),改完可 `make eval` 看 recall / 正确拒答率。
- **检索参数**:数据库(后台)是唯一真相源,后台改即时生效(无需重启);`.env` 的 `RAG_*` 仅首次启动种子。
- **监控**:Prometheus 抓 `/metrics`(反代对外 deny,内网抓);`DATA_RETENTION_DAYS` 设好后 worker 每日清理过期会话(PIPL)。

---

## 六、前提与注意

- 线上仓库需在 **main 分支**且无本地改动(`make release` 用 `git pull --ff-only`,否则报错停下,不会乱合并)。
- 服务器要能访问 GitHub(私有库配 deploy key 或凭证)。
- `.env` 在 `.gitignore` 中,更新不会覆盖你的密钥 / 配置。
- postgres / redis / minio 端口仅绑 `127.0.0.1`,切勿对公网开放。
