# 部署上线手册（网页版）

把「Web 对话窗口 + 运营后台」部署到一台云服务器。整套用 Docker，一条命令起全栈。

> 已在本地实测：`docker compose --profile app up -d --build` 可自动构建、迁移、初始化并运行（backend / worker / admin / postgres+pgvector / redis / minio 全部健康）。云服务器上是同一条命令。

---

## 0. 前置

- **云服务器**：国内业务用阿里云/腾讯云 ECS，起步 **2C4G**（向量用通义云接口，本机不跑模型，配置不高也行）；装好 **Docker + docker compose**。
- **域名**：一个域名（如 `chat.yourbiz.com` 给对话端，`admin.yourbiz.com` 给后台）。**国内服务器 + 国内域名必须先备案**，否则 80/443 会被拦。
- **放行端口**：安全组放行 `80`、`443`（对外）；`8000/8090` 只在本机用，不要对公网开放。

---

## 1. 拉代码 + 写生产 `.env`

```bash
git clone <你的仓库> ai-customer-service && cd ai-customer-service
cp .env.example .env
```

**编辑 `.env`，重点改这些**（⚠️ 安全相关）：

```ini
APP_ENV=production
APP_DEBUG=false
APP_BASE_URL=https://chat.yourbiz.com        # ⚠️ 公开对话域名；客户上传的图片/附件 URL 也用它拼（/api/chat/media/…），写错图片会裂

# ── 强随机密钥（生产必须改！）──
APP_SECRET_KEY=<openssl rand -hex 32 生成>    # ⚠️ 第三方密钥用它加密，首次启动前定好，之后别再改
JWT_SECRET=<openssl rand -hex 32 生成>
BOOTSTRAP_ADMIN_EMAIL=you@yourbiz.com
BOOTSTRAP_ADMIN_PASSWORD=<改成强密码>

# ── 数据库密码（生产改掉默认）──
POSTGRES_PASSWORD=<强密码>

# ── 模型（沿用你现在调通的）──
LLM_BASE_URL=https://api.9e.lv/v1
LLM_API_KEY=<你的中转 Key>
LLM_MODEL=gpt-5.5
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=<你的通义 Key>
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIM=1024
RERANK_ENABLED=true
RERANK_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_API_KEY=<你的通义 Key>
RERANK_MODEL=gte-rerank-v2

# ── 后台 CORS（同源走 nginx 代理则可留默认）──
ADMIN_CORS_ORIGINS=https://admin.yourbiz.com
# ADMIN_PORT=8090   # 若 8080 被占用
```

> **关于 `APP_SECRET_KEY`**：第三方密钥（中转/通义 Key 等）在库里是用它加密的。**首次启动前设好，之后不要改**——改了会导致已加密的密钥无法解密。全新部署没问题（首次启动从 `.env` 重新加密入库）。

---

## 2. 起全栈

```bash
docker compose --profile app up -d --build
```

它会自动：构建镜像（含 widget/admin 打包）→ 跑 `migrate`（建表 + pgvector/HNSW/全文索引 + 初始化默认管理员/AI配置/Web渠道）→ 起 backend / worker / admin。

验证：
```bash
docker compose --profile app ps                 # 全部 healthy/up
curl -s localhost:8000/health                   # {"status":"ok",...}
curl -s "localhost:8000/api/chat/config?channel_key=default"
```

---

## 3. 套 HTTPS（Caddy 自动证书，最省事）

```bash
# 安装 caddy（以 Debian/Ubuntu 为例，详见 caddyserver.com）
# 把 deploy/Caddyfile 改成你的域名，放到 /etc/caddy/Caddyfile
caddy reload --config /etc/caddy/Caddyfile
```

- `chat.yourbiz.com` → `127.0.0.1:8000`（对话端：widget.js / /chat / /api / WebSocket，WSS 自动透传）
- `admin.yourbiz.com` → `127.0.0.1:8090`（后台，建议加 IP 白名单）

Caddy 自动申请并续期证书，无需手动配证书。

---

## 4. 后台收尾配置

打开 `https://admin.yourbiz.com`，用 `.env` 里的管理员账号登录，然后：

1. **渠道配置 → Web**：填欢迎语/品牌/Logo；**「允许嵌入的域名白名单」填你自己网站的域名**（生产务必填，否则全网可嵌入刷你账单）；按需调限流。
2. **AI 配置**：确认 LLM / Embedding / Rerank 都是你要的（已从 `.env` 初始化）；可点「测试连接」。
3. **转人工通知**（可选）：填企微群机器人 webhook 或 SMTP，AI 答不上来时通知你。
4. **知识库**：批量导入你的真实 FAQ（可参考 `docs/sample_faq_zh.csv` 模板）。
5. **账号权限**：改掉默认管理员密码，按需加运营/只读账号。

---

## 5. 在你的网站嵌入对话窗口

把这行粘到目标网页（域名要在上面的白名单里）：

```html
<script src="https://chat.yourbiz.com/embed/widget.js"></script>
```

右下角即出现客服气泡。也可直接给用户独立页 `https://chat.yourbiz.com/chat`。

---

## 上线检查清单

- [ ] `APP_SECRET_KEY` / `JWT_SECRET` 已改强随机；默认管理员密码已改
- [ ] `POSTGRES_PASSWORD` 已改；`8000/8090` 未对公网暴露
- [ ] HTTPS/WSS 正常（`https://chat.yourbiz.com/chat` 能流式对话）
- [ ] Web 渠道**域名白名单**已填自己的站点
- [ ] 每日成本上限 `DAILY_COST_CAP_USD` 按预算设；通义「免费额度用完即停」保持**关闭**
- [ ] 数据库定期备份（`docker compose exec postgres pg_dump ...`）
- [ ] 数据留存 `DATA_RETENTION_DAYS` 按合规要求设（worker 每日自动清理）

## 常用运维

```bash
docker compose --profile app logs -f backend worker   # 看日志
docker compose --profile app up -d --build             # 更新代码后重新部署
docker compose --profile app down                      # 停（数据在卷里不丢）
docker compose exec postgres pg_dump -U acs acs > backup.sql   # 备份
```

> **提示**：对话模型现在走的是逆向中转，**不稳定**（会偶发 503/限流）。正式商用建议尽快换成官方 API（如 DeepSeek，后台 AI 配置一行切换即可），更稳更合规。
