# 序策 · Operations Agent

面向电商运营的 Plan-and-Execute Agent。用户给出目标，模型制定计划、选择工具、查看实际结果并调整后续行动，交付有证据的分析、库存建议、活动方案和商品文案。

**没有店铺实体或店铺维度。经营数据只读，Agent 不改价、不发布商品、不下采购单、不执行投放。**

## 当前能力

- 模型驱动的 Planner / Executor / Tools / Evaluator 显式 LangGraph 节点，没有按电商意图拼装计划的代码分支。
- Executor 可在一次结构化决策中选择最多 6 个相互独立的只读工具并行执行；写入成果和记忆候选仍保持单次、可审计执行。
- 计划版本、步骤完成记录、工具证据、运行预算、用户干预及任务恢复。
- 订单、商品、退款、库存、分批履约、渠道、广告、活动与知识资料工具。
- 可复现、可对账的模拟数据；默认 180 天、200 个 SKU、约 5 万订单。
- React 工作台：经营概览、任务进度、计划与证据、资料上传、报告版本及 Markdown/CSV 下载。
- 会话可永久删除，并在一个事务中清理运行记录、证据、成果、任务记忆、后台作业及该会话产生的记忆候选和已确认长期记忆。
- 用户会话/任务/私有资料隔离；经营库与应用库分离，PostgreSQL 独立只读角色。
- 结构化/语义分块、版本化后台索引、PostgreSQL 全文与向量召回、RRF 和真实 BGE 精排。
- 当前任务摘要与历史召回；长期记忆经用户确认后生效，支持修改、到期、停用和忘记。
- 管理员公共资料和用户个人资料；公共资料由每个用户独立启用，不共享个人任务或记忆。
- 32 个真实模型评测场景；协议、权限、数值与恢复测试使用明确的测试替身。

**未配置模型时可以查看数据和管理资料，任务会明确进入“待处理”。应用没有伪造分析的演示模型回退。**

需求见 [PRD.md](PRD.md)，实现说明见 [docs/TECH.md](docs/TECH.md)，验证与剩余验收见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 本地启动（Python + Node.js）

需要 Python 3.12、Node.js 22.12+。Node 版本要求与当前使用的 Vite 工具链一致，参见 [Vite 官方说明](https://vite.dev/guide/)。SQLite 仅作为本地开发适配，Docker 使用 PostgreSQL/pgvector。

在项目根目录创建配置文件：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中设置模型。支持兼容接口、Anthropic 或 Ollama，配置值不会通过前端返回：

```dotenv
LLM_PROVIDER=openai
LLM_MODEL=你的模型名称
LLM_API_KEY=你的密钥
LLM_BASE_URL=你的兼容接口地址
```

`LLM_BASE_URL` 为可选项；Ollama 使用 `LLM_PROVIDER=ollama`，设置本地模型名称，可不填密钥。模型必须支持工具调用和结构化输出。更改配置后重启后端。

启动后端：

```powershell
Set-Location backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-retrieval.txt
.\.venv\Scripts\python.exe -m app.download_models
.\.venv\Scripts\python.exe -m app.bootstrap
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`app.bootstrap` 只在数据集不存在时生成数据，不覆盖已有数据。首次生成包含对账验证，结果打印为 JSON。数据库位于 `backend/data/`，已加入忽略规则。请使用项目独立虚拟环境，避免系统已有的 Transformers/Hugging Face 版本冲突。本地默认在 API 进程启动维护 Worker；Docker 使用独立 Worker。

另开终端启动前端：

```powershell
Set-Location frontend
npm ci
npm run dev
```

打开 <http://localhost:5173>，注册本地账号即可使用。默认管理员账号为 `admin`，密码为 `adminadmin`。浏览器使用 HttpOnly Cookie，开发模式通过 Vite 代理访问 API，无需把登录令牌保存在 localStorage。

## Docker Compose 启动

在根目录 `.env` 中填写四个独立的数据库密码，使用随机字母数字，避免未经 URL 编码的特殊字符影响连接串：

- `POSTGRES_PASSWORD`：初始化管理员，仅数据库容器使用。
- `APP_DB_PASSWORD`：任务、身份、报告和知识资料库。
- `COMMERCE_OWNER_PASSWORD`：模拟数据初始化，只提供给 seed 容器。
- `COMMERCE_READER_PASSWORD`：API 的经营数据库只读账号。

```powershell
docker compose --profile setup run --rm --no-deps model-download
docker compose up -d --build
docker compose ps
```

访问 <http://localhost:5173>。启动顺序为数据库初始化 → 模拟数据生成/对账 → Alembic 迁移与 API → Nginx 前端及独立维护 Worker。模型位于根目录 `models/` 并挂载到容器；若已有完整权重，可跳过下载命令。数据库不暴露宿主端口；应用默认仅绑定本机。

宿主端口冲突时，在 `.env` 中修改 `API_PORT`、`FRONTEND_PORT`，并同步 `CORS_ORIGINS`。后台任务独立于浏览器连接；刷新或断线不会取消任务。

```powershell
docker compose logs --tail 100 api
docker compose logs --tail 100 seed
docker compose logs --tail 100 worker
docker compose stop
```

数据保存在命名卷。保留卷即可保留账号、任务与成果；此项目不会自动删除或重建已有经营数据。

## 模拟数据与情境

需要一个独立的小数据集时，在 `backend/` 执行：

```powershell
python -m app.seed --days 60 --skus 32 --orders 2500 --scenario stockout --url sqlite+aiosqlite:///data/stockout.db --report data/stockout-report.json
```

情境：`baseline`、`traffic_drop`、`stockout`、`refund_wave`、`promotion_margin`、`supply_delay`、`mixed`。情境标签只供生成器/评测器使用，不写入 Agent 可读取的资料和能力目录。

切换本地只读数据源时，在 `.env` 使用绝对路径，例如：

```dotenv
COMMERCE_DATABASE_URL=sqlite+aiosqlite:///file:D:/project/operations-agent/backend/data/stockout.db?mode=ro&uri=true
```

数值由订单、明细、付款和流水推导。访客按周期去重；支付商品 GMV 不含运费；退款以到账日统计；库存区分实物、预占、可用与在途。完整字段和边界见 [数据字典](docs/DATA.md)。

## 资料检索

上传 Markdown、TXT、CSV、PDF、DOCX，最大 8MB，文本上限 30 万字符。扫描 PDF 需要先 OCR。上传返回后由后台完成结构化解析、语义分块、中文词面索引和向量编码；界面显示进度、错误和重建入口。新版本完成后原子切换，不覆盖已有任务或经营数据。

完整部署使用 `bge-small-zh-v1.5`（512 维）与 `bge-reranker-base`，下载脚本固定模型版本。PostgreSQL 内执行全文与向量候选召回，使用 GIN/HNSW 索引、RRF 融合及 Cross-Encoder 精排；SQLite 保留隔离测试适配。引用可定位标题、片段与 PDF 页码。模型不可用时明确显示词面或融合排序降级，不能将降级状态当作完整能力验收通过。

本地模型路径在 `.env` 中配置，相对路径以 `backend/` 为基准；Docker 固定挂载 `/models`。模型只从本地加载，业务请求中不会自动下载。更换向量模型后须重建索引；更换向量维度需先迁移数据库。

系统启动时会根据 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 创建或校正管理员账号。管理员上传的文件始终作为管理员资料，不提供个人资料范围；普通用户只能上传自己的个人资料。管理员资料默认未加入任何用户的检索来源，每名用户自行启用；资料停用或删除后，检索、原文及相关证据读取都会重新校验权限。

## 记忆

- **任务内记忆**：完整保存每条用户/Agent 消息，结构化有效约束单独维护版本；模型默认读取最近 12 条原文，更早对话进入滚动摘要并可通过当前任务历史按需召回。工具、缓存和数据库查询均禁止读取其他任务，即使属于同一用户。
- **长期记忆**：工作背景、回答偏好、分析习惯、近期关注和稳定约束。用户说“记住”或模型提取的内容都先进入候选，确认前不作为长期记忆使用。编辑也需要再次确认；可以停用、删除、设置有效期，或明确输入“忘记：完整内容”“清除所有记忆”。
- 已确认的个人长期偏好可跨任务使用，但不会展开原任务历史；当前明确要求优先，旧库存、价格和销量不能当作当前事实。
- 每次模型调用保存约束、摘要、近期消息、长期记忆、证据和历史单元的采用 ID/版本，便于复现上下文范围，不重复保存完整提示词。
- 侧栏“长期记忆”管理候选与记录；任务页可查看当前有效约束、近期原文窗口、摘要状态和采用偏好。未配置对话模型时，显式候选确认及资料管理仍可用，自动摘要/候选提取显示等待模型。

摘要和候选提取使用每任务最多 6 次的独立维护预算，并继续计入累计 Token 和成本；它们不占用用户新一轮追问的交互预算。查询扩展属于当前交互轮次，计入该轮模型预算。

运行图按 `guard → plan / execute / tools / evaluate` 显式路由。中间计划步骤完成后直接进入下一个满足依赖的步骤，只在计划结束、关键失败、重规划或预算收尾时执行全局评估，避免每个小步骤额外消耗一次模型调用。数据库任务状态仍是唯一持久化执行来源，防止与框架 checkpoint 形成双重真相。

测试、RAG 评测和真实模型评测均使用独立 PostgreSQL/pgvector 容器，不使用 SQLite 作为验收环境：

```powershell
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -f docker-compose.test.yml down -v
```

RAG 评测使用独立的 `operations_test`、`commerce_test` 数据库和真实本地向量、精排模型：

```powershell
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from rag-evaluation rag-evaluation
docker compose -f docker-compose.test.yml down -v
```

报告包含逐题排名、Recall@5、MRR、BM25 与 `ts_rank_cd` 基线、无答案误命中、阶段耗时及当前任务历史隔离检查。

## 测试与评测

后端测试必须在独立 PostgreSQL/pgvector 测试容器中运行：

```powershell
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -f docker-compose.test.yml down -v
Set-Location backend
python -m ruff check app tests
python -m ruff format --check app tests migrations
```

前端：

```powershell
Set-Location frontend
npm run build
```

真实模型评测同样只使用独立 PostgreSQL 测试库，需要已经配置可用模型，会产生实际模型调用费用：

```powershell
docker compose -f docker-compose.test.yml run --rm agent-evaluation --list
docker compose -f docker-compose.test.yml run --rm agent-evaluation --case D01,D02,D03 --repeat 5
docker compose -f docker-compose.test.yml run --rm agent-evaluation --repeat 3
```

每次评测会清空并重新创建独立 PostgreSQL 测试库中的应用表和模拟经营表，输出到 `evaluation-reports/<UTC时间>/`；不会使用或修改正在运行的应用数据。报告保存计划、工具观察、预算和答案。模型声称完成仅是机械指标，质量通过率必须在人工核对数值、证据和目标覆盖后统计。

## 运行约束

- 每轮用户问题默认 30 次模型调用、20 次工具调用、5 次重规划、300 秒活跃执行时间，每步骤最多 6 次工具调用。
- 用户补充新问题时开启新的交互轮次并重置该轮执行额度；累计调用、Token、成本和历史证据继续保留。暂停后恢复同一轮不会重置额度。
- 补充条件产生约束版本，使旧结果无法冒充新条件下的证据。
- 暂停/取消会阻止新动作及旧调用提交；已经开始的只读请求可能等待超时，但不会写入经营数据。
- 当前每个 API 进程有一个后台执行器；多个实例通过数据库租约竞争任务。首版主要面向本地使用。
- 前端“完成”不等于真实经营效果已经改善，也不等于通过完整模型质量评测。
