# 序策 · Operations Agent

面向电商运营的 Plan-and-Execute Agent。用户提出经营目标，系统规划调查步骤、调用受控工具、核验证据并调整后续行动，交付可追溯的经营分析、库存建议、活动方案和商品文案。

经营数据只读。系统不改价、不发布商品、不创建采购单、不执行投放或发送消息；不包含店铺实体、店铺维度或多店铺能力。

需求见 PRD.md

## 核心能力

- **Plan-and-Execute 编排**：基于 LangGraph 显式推进 Guard、Plan、Execute、Tools、Evaluate；支持动态计划、重规划、暂停、恢复、取消和部分交付。
- **多 Agent 调查**：主 Agent 可按需委派渠道、商品库存、知识资料和通用调查子任务，并行完成独立只读调查后统一核验交付。
- **证据驱动交付**：工具原始结果持久化为 Evidence，模型仅接收压缩后的 Observation；长表和长文档可按 Evidence ID 定向回读。
- **经营分析工具**：支持指标、周期贡献、商品、订单退款、库存、活动成本库存快照、渠道活动与受控计算。
- **运营知识库**：支持 Markdown、TXT、CSV、PDF、DOCX 的上传、解析、版本管理、检索、精排和引用定位。
- **记忆与上下文**：近期对话、滚动摘要、当前任务历史召回和确认型长期偏好；任务历史不跨任务读取。
- **Tavily 联网搜索**：可选查询公开网页，返回标题、链接、摘要和发布日期，并独立保存为 Evidence。
- **审计与评测**：展示计划、工具、证据、子 Agent、模型调用与异常原因；提供 RAG、记忆和 Agent 的版本化离线评测。

## 架构概览

~~~
用户问题
  → Guard：身份、权限、预算、约束检查
  → Plan：成功标准与步骤
  → Execute：选择工具、批量只读查询或委派子 Agent
  → Tools：经营数据 / 知识库 / Tavily / 计算
  → Evidence：保存原始依据
  → Observation：压缩后反馈模型
  → Evaluate：继续、重规划、追问、完成或部分交付
~~~

主 Agent 负责计划和最终交付；临时子 Agent 只能在授权范围内调查，不可保存成果、修改记忆、委派其他子任务或读取其他任务历史。

## 技术栈

- 后端：Python 3.12、FastAPI、SQLAlchemy、Alembic
- Agent：LangChain、LangGraph、Pydantic 结构化输出
- 数据：PostgreSQL、pgvector、GIN 全文索引、HNSW 向量索引
- 检索：BAAI/bge-small-zh-v1.5、BAAI/bge-reranker-base、ts_rank_cd、RRF
- 前端：React、TypeScript、Vite、Ant Design
- 交付与验证：Docker Compose、Pytest、Ruff

## 快速启动

### 1. 准备配置

在项目根目录创建 .env：

~~~
Copy-Item .env.example .env
~~~

至少填写数据库密码和模型配置：

~~~
POSTGRES_PASSWORD=你的数据库管理员密码
APP_DB_PASSWORD=你的应用数据库密码
COMMERCE_OWNER_PASSWORD=你的经营数据库所有者密码
COMMERCE_READER_PASSWORD=你的经营数据库只读密码

LLM_PROVIDER=openai
LLM_MODEL=你的模型名称
LLM_API_KEY=你的模型密钥
LLM_BASE_URL=你的兼容接口地址
~~~

模型必须支持工具调用和结构化输出。若未配置模型，系统仍可查看数据、管理资料和查看审计记录，但不能完成 Agent 推理任务。

管理员默认账号：

~~~
ADMIN_USERNAME=admin
ADMIN_PASSWORD=adminadmin
~~~

部署到非本地环境前应更换管理员密码，并使用独立的高强度数据库密码。

### 2. 配置可选 Tavily 联网搜索

~~~
TAVILY_API_KEY=你的 Tavily API 密钥
TAVILY_TIMEOUT_SECONDS=12
TAVILY_SEARCH_DEPTH=basic
~~~

未配置 Tavily 密钥时，联网搜索工具不会出现在 Agent 工具目录。配置后，单次搜索默认返回 3 条，可在任务中指定 1 至 5 条结果。

### 3. 下载本地检索模型

首次使用时下载向量和精排模型：

~~~
docker compose --profile setup run --rm --no-deps model-download
~~~

模型保存于根目录 models。业务请求不会在运行时自动下载模型。

### 4. 启动服务

~~~
docker compose up -d --build
docker compose ps
~~~

访问地址：

- 工作台：http://127.0.0.1:5173
- API 健康检查：http://127.0.0.1:8000/health
- 数据库查看页：http://127.0.0.1:8081

数据库查看页仅用于本地开发和排查。业务数据位于 commerce 数据库，应用数据位于 operations 数据库。

### 5. 本地开发启动

需要 Python 3.12 和 Node.js 22.12+。

后端：

~~~
Set-Location backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-retrieval.txt
.\.venv\Scripts\python.exe -m app.download_models
.\.venv\Scripts\python.exe -m app.bootstrap
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
~~~

前端：

~~~
Set-Location frontend
npm ci
npm run dev
~~~

## Agent、工具与证据

当前主 Agent 注册 19 个工具：

| 分类 | 工具 |
| --- | --- |
| 数据范围与口径 | 数据能力、指标定义、指标定义检索 |
| 经营分析 | 指标查询、周期比较、商品、订单退款、库存、活动成本库存快照、渠道活动 |
| 资料与公开来源 | 资料检索、资料原文读取、Tavily 联网搜索 |
| 证据与计算 | Evidence 读取、四则计算 |
| 记忆与历史 | 当前任务历史检索、历史片段读取、长期记忆候选 |
| 成果 | Markdown 报告与 CSV 保存 |

一次 Execute 决策最多并行执行 6 个互不依赖的只读工具。成果保存和长期记忆候选属于写入动作，必须单独执行。

工具调用结果分为两部分：

- **Evidence** 保存原始数据、参数、状态和来源，供前端查看和后续定向读取。
- **Observation** 是受字符预算限制的摘要，供模型继续决策。

工具失败或查询为空会反馈给模型；模型可改参数、换工具、追问用户或停止并交付已验证的部分结果。相同参数的工具调用最多执行两次，任务还受模型调用、工具调用、重规划和活跃执行时间预算约束。

## RAG 知识库

上传支持 Markdown、TXT、CSV、PDF、DOCX，单文件最大 8MB，文本上限 30 万字符；扫描 PDF 需要先 OCR。

~~~
文档上传
  → 结构化解析与分块
  → 向量编码与版本发布
  → PostgreSQL 全文候选 + 向量候选
  → RRF 融合
  → BGE Cross-Encoder 精排
  → 片段、文档标题与 PDF 页码引用
~~~

资料按用户权限与启用范围检索。管理员上传的资料归管理员所有；普通用户上传的资料仅属于自己。每个用户独立决定是否启用管理员资料。资料被停用、删除或更新后，后续 Evidence 回读会重新校验权限与版本。

## 记忆与上下文

- **短期记忆**：默认保留最近 12 条原文；较早消息进入滚动摘要。
- **任务历史**：完整消息以 HistoryUnit 保存，只在当前任务内通过全文、向量、RRF 与精排按需召回。
- **长期记忆**：稳定偏好、工作背景和约束先作为候选，必须由用户确认后才能跨任务使用。
- **上下文优先级**：当前指令 → 有效约束 → 当前证据 → 近期原文 → 任务摘要 → 相关任务历史 → 已确认长期记忆。

每次模型调用记录实际采用的约束、消息、摘要、Evidence、HistoryUnit 与长期记忆版本，便于审计上下文范围。用户删除会话后，相关消息、运行、证据、成果、任务记忆、后台作业和该会话确认的长期记忆会一并永久删除。

## 可观测性

运行记录涵盖：

- 计划版本、步骤状态、重规划原因和预算。
- 模型调用次数、Token、耗时、结构化输出错误和模型服务异常。
- 工具与子 Agent 调用、Evidence、检索阶段耗时、空结果和失败原因。
- 最终回答、成果版本、成功标准与引用证据。

前端可以查看执行计划、证据详情、任务记忆、成果和模型调用次数。证据引用会在弹窗中打开，而不是跳转到外部页面。

## 测试与评测

所有测试和评测使用隔离 PostgreSQL/pgvector 环境，不使用正在运行的应用数据。

基础测试：

~~~
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -f docker-compose.test.yml down -v
~~~

RAG 评测：

~~~
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from rag-evaluation rag-evaluation
docker compose -f docker-compose.test.yml down -v
~~~

RAG 评测集位于 backend/evaluation/rag/v1，当前包含 60 份资料和 110 条问题，覆盖 Markdown、TXT、CSV、PDF、DOCX、长文层级、表格和多页 PDF。报告包含文档 Recall@5、文本锚点 Recall@5、MRR、ts_rank_cd 基线、无答案误命中和 P95 检索耗时。

记忆评测：

~~~
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from memory-evaluation memory-evaluation
docker compose -f docker-compose.test.yml down -v
~~~

记忆评测集位于 backend/evaluation/memory/v1，当前包含 50 条场景，覆盖约束、任务历史隔离、摘要和长期记忆生命周期。

Plan-and-Execute 评测：

~~~
docker compose -f docker-compose.test.yml run --rm agent-evaluation python -m app.evaluation --list
docker compose -f docker-compose.test.yml run --rm agent-evaluation python -m app.evaluation --repeat 1
~~~

Agent 评测集位于 backend/evaluation/agent/v1，当前包含 32 条场景。自动评分检查任务状态、证据覆盖、成果保存、工具路由、子 Agent 路由和禁用工具违规；数值结论与业务解释保留人工复核字段。

## 常用运维命令

~~~
docker compose ps
docker compose logs --tail 100 api
docker compose logs --tail 100 worker
docker compose up -d --build
docker compose stop
~~~

修改 .env 后执行 docker compose up -d 以重新创建受影响的服务。不要使用 docker compose down -v，除非明确需要删除本地数据卷。
