# AI 客服系统（起步版）

一套基于大语言模型的多渠道智能客服系统。单租户、可自托管、端到端跑通：可嵌入的 Web 对话窗口 + 企业微信渠道、Agent 核心（对话 / 上下文管理 / 工具调用 / 知识库 RAG）、运营管理后台、轻量转人工兜底，以及限流、成本熔断、可观测、异步任务等生产基本面。

> 实现自《AI客服系统需求文档_起步版_v2.1》。数据模型保持「**租户就绪**」，未来可增量演进到多租户 SaaS。

---

## ✨ 能力一览

| 模块 | 实现 |
|------|------|
| **Web 对话窗口** | 可嵌入 `<script>`（右下角弹窗）+ 独立页面；WebSocket 流式打字机；亮/暗主题、Markdown、移动端适配；历史恢复、断线重连补齐、👍/👎 反馈；**域名白名单 + 多维限流 + 每日成本熔断 + 空闲超时** |
| **企业微信渠道** | 回调验签 / 解密（WXBizMsgCrypt）、先 ACK 异步处理再主动推送、access_token 缓存 |
| **Agent 核心** | 原生调用 LLM（无 LangChain）；上下文管理（摘要压缩 + 截断）；**带边界的工具循环**；工具失败交回模型；优雅降级 |
| **工具** | `search_knowledge`（RAG）、`escalate_to_human`（转人工）、`get_order`（业务工具示例）；可扩展 |
| **知识库 RAG** | 分块（带重叠）→ 异步生成向量 → 全文索引；**混合检索（向量 + 关键词）+ RRF 融合 + 可选重排**；检索失败降级纯 LLM；**Embedding 迁移：全量重建 + 进度 + 降级** |
| **LLM 抽象** | OpenAI 兼容统一接口，配置即可切换 OpenAI / DeepSeek / 通义 / GLM / 本地模型等 |
| **运营后台** | 登录（JWT + RBAC）、概览 Dashboard、知识库 CRUD + 版本/回滚 + 批量导入 + 待审核、对话记录（工具调用/引用/trace）、AI 配置、渠道配置、转人工工单、账号权限 + 操作日志 |
| **转人工兜底** | 建工单 + 企微/邮件通知 + 告知客户 + 后台跟进 |
| **生产基本面** | 限流、成本熔断、内容安全（可开关）、语义缓存（可开关）、结构化日志 + trace_id、Prometheus 指标、健康检查、异步任务队列（ARQ）、数据留存清理、可选 Langfuse |

---

## 🏗 架构

```
   终端用户渠道                     后端 (FastAPI)
 ┌──────────┐   WS/HTTP   ┌───────────────────────────────────────┐
 │ Web 窗口  │◀──────────▶│ Channel 适配层 → ConversationService    │
 │ (Preact)  │            │        → AgentRunner（工具循环）         │
 ├──────────┤   Webhook   │   ┌────────┬────────┬────────┬───────┐  │
 │ 企业微信  │◀──────────▶│   ▼上下文  ▼ToolUse ▼RAG混合 ▼LLM抽象 │  │
 ├──────────┤            │            转人工(通知)                  │
 │ 飞书/钉钉 │(适配器扩展) │   管理后台 API（JWT + RBAC）             │
 └──────────┘            └───────┬──────────────┬──────────────────┘
   React 后台 (Antd) ◀───────────┘              │
   ARQ Worker ───▶ PostgreSQL(pgvector) · Redis · MinIO · (Langfuse)
```

**技术栈**：Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · Redis · ARQ · MinIO · React + TS + Ant Design · Preact（widget）· Alembic · JWT/bcrypt。

---

## 🚀 快速开始（本地开发）

前置：Docker、Python 3.12+、Node 18+ 与 pnpm。

```bash
# 1. 安装依赖 + 生成 .env
make setup

# 2. 在 .env 中填入 LLM 配置（必填才能拿到真实回答）
#    LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#    EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL（启用向量检索）
#    —— 不填也能跑通：无 Key 时会优雅降级提示，关键词检索仍可用

# 3. 拉起依赖（postgres+pgvector、redis、minio）
make deps-up

# 4. 迁移 + 初始化（默认管理员 admin@example.com / admin12345）
make migrate
make bootstrap

# 5. 分别在不同终端启动
make dev-backend   # API + WebSocket  → http://localhost:8000
make dev-worker    # 异步任务 worker
make dev-admin     # 运营后台          → http://localhost:5173
```

- 对话窗口（独立页）：<http://localhost:8000/chat>
- API 文档（Swagger）：<http://localhost:8000/docs>
- 运营后台：<http://localhost:5173>（默认账号见上）
- 指标：<http://localhost:8000/metrics> · 健康：<http://localhost:8000/health>

### 一键全栈（Docker）

```bash
cp .env.example .env   # 按需修改
make stack-up          # 构建并启动 backend + worker + admin + 依赖
# 后台 → http://localhost:8080   对话页 → http://localhost:8000/chat
```

---

## 🔌 在你的网站嵌入对话窗口

在「渠道配置 → Web」里填好品牌信息与**域名白名单**，然后把下面这行粘贴到目标网站：

```html
<script src="http://<你的后端域名>/embed/widget.js"></script>
```

右下角即出现客服按钮。也可用独立页 `/chat` 或 `<iframe>` 接入。

---

## ⚙️ 配置说明

- **运行时配置存数据库**（`ai_configs` / `channel_configs`），在后台「AI 配置 / 渠道配置」里改即时生效；`.env` 仅用于首次启动与密钥兜底。
- **切换 LLM / 模型**：后台 AI 配置改 Provider/BaseURL/Key/Model 即可，业务代码无感。
- **切换 Embedding 模型/维度**：后台保存后自动触发**全量向量重建**，重建期间检索自动降级，后台显示进度。
- **国产模型**：填对应 OpenAI 兼容端点即可（DeepSeek、通义 DashScope-compat、智谱 GLM、Moonshot…）。
- 关键安全项：`APP_SECRET_KEY`、`JWT_SECRET` 生产务必改成强随机；第三方密钥（企微 Secret、SMTP 密码）在库中加密存储。

### Embedding（向量）来源

对话模型与 Embedding 模型**相互独立**，可任意混搭。两条路：

1. **本地中文向量（推荐，免费/离线/数据不出本机）**：内置 `scripts/local_embedding_server.py`，用 `BAAI/bge-small-zh-v1.5`（90MB，512 维，纯 ONNX 无 PyTorch）起一个 OpenAI 兼容的 `/v1/embeddings`：
   ```bash
   make dev-embed          # 启动本地向量服务（:8100）
   ```
   AI 配置里 Embedding 指向 `http://localhost:8100/v1`、模型 `bge-small-zh-v1.5`、维度 `512`。

   > ⚠️ **改 Embedding 维度的正确姿势**（dev 手动启动时）：① 后台「AI 配置」改模型/维度 → 自动触发全量重建；② 同步 `.env` 的 `EMBEDDING_DIM`（它驱动 ORM 的向量列维度）；③ **干净重启** backend + worker —— 务必先 `pkill -f "arq app.tasks"; pkill -f "uvicorn app.main"` 杀掉**所有**旧进程再起，残留的旧 worker 会带着旧维度，写入时报 `expected N dimensions`。生产用 `docker compose up -d` 整体重建容器，无此坑。
2. **云 Embedding API**：通义 `text-embedding-v3`、智谱 `embedding-3` 等官方 OpenAI 兼容端点，填进 AI 配置即可。

> **注意**：逆向 ChatGPT 网页版的「中转/反代」通常**只支持对话、不支持 `/embeddings`**（网页端本就没有该接口）。这类中转只配做对话模型，向量请用本地 BGE 或官方 Embedding API。

完整变量见 [.env.example](.env.example)。

---

## 🧪 测试与评估

```bash
make test    # 单元 + 集成测试（LLM 抽象、渠道适配器、限流、工具循环、企微加解密…）
make eval    # RAG 检索评估集（recall@k / MRR）
make lint    # ruff
```

> RAG 评估在**仅关键词**模式下，精确词（型号、错误码）召回好，语义改写召回有限——这正是需要**混合检索 + 向量**的原因；配置 Embedding Key 后向量路径生效，召回显著提升。中文全文检索可选装 `zhparser` / `pg_jieba` 进一步增强（检索层已预留，改 tsvector 配置即可）。

---

## 📁 目录结构

```
ai-customer-service/
├── backend/                  # FastAPI 后端 + ARQ worker
│   ├── app/
│   │   ├── agent/            # AgentRunner、工具、上下文管理
│   │   ├── channels/         # 渠道适配器（web / wechat + 加解密）
│   │   ├── llm/              # LLM Provider 抽象、embedding、rerank、pricing
│   │   ├── rag/              # 分块、混合检索
│   │   ├── services/         # 业务服务（会话、知识、转人工、用量、配置…）
│   │   ├── api/              # chat（WS/HTTP/企微）+ admin（8 个子路由）
│   │   ├── tasks/            # ARQ 队列 + worker
│   │   ├── models/           # SQLAlchemy 模型（租户就绪）
│   │   └── core/             # 配置、安全、日志、限流、存储、指标、健康
│   ├── alembic/              # 迁移（pgvector + HNSW + 全文索引）
│   ├── scripts/              # bootstrap、rag_eval
│   └── tests/
├── widget/                   # 可嵌入对话窗口（Preact）
├── admin/                    # 运营后台（React + TS + Antd）
├── deploy/                   # postgres 初始化、nginx 示例
├── docker-compose.yml        # 依赖 + 全栈（--profile app）
└── Makefile
```

---

## 🛡 生产部署要点（§16）

- 反向代理启用 **HTTPS/WSS**；`backend` 多进程、`worker` 独立水平扩展。
- 数据库定期备份；密钥经环境/密钥服务注入，不入库明文。
- 监控接入 Prometheus 抓 `/metrics`，Grafana 看板；告警覆盖成本熔断、错误率、队列积压、依赖不可用。
- 配置 `DATA_RETENTION_DAYS` 后，worker 每日清理过期会话（PIPL）。

---

## 🧭 演进到 SaaS（以后再做）

数据模型已带 `tenant_id`（单一隐式租户），渠道/Provider/配置均按租户可扩展。未来增量加：租户解析 + PostgreSQL RLS、平台后台、计费订阅、完整坐席工作台、白标。详见需求文档第十九章。

---

## 📌 实现说明 / 假设

- 知识富文本编辑采用 Markdown 文本域（够用且零额外依赖）；如需所见即所得可替换为富文本组件。
- 中文全文检索默认 `simple` 分词 + 三元组（trigram）兜底精确匹配；语义召回靠向量。可插拔中文分词扩展。
- 企业微信实现自建应用单聊回调全链路；群聊 @ 触发点已预留在适配器。
- Langfuse 为可选增强（未装也可）；每轮对话的 token/成本/耗时/工具/引用/trace_id 始终落库可查。
```
