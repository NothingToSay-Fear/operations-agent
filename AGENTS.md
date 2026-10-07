# Operations Agent 开发规范

本文件用于指导本仓库的代码开发与维护。产品范围见 [PRD.md](PRD.md)，使用和部署说明见 [README.md](README.md)。

## 开发原则

1. 修改前阅读相关实现、测试、接口契约和相邻代码，优先复用既有能力。
2. 以最小、聚焦的改动完成任务；不做与需求无关的重构。
3. 保留工作区中与当前任务无关的用户改动。除非用户明确要求，不执行提交、重置、丢弃、覆盖、强制推送或清理操作。
4. 行为、协议、权限或数据结构变化时，同步更新调用端、测试和必要文档。
5. 不新增依赖，除非现有能力无法满足需求；较大依赖变更需先说明影响。
6. 交付时说明改动文件、验证结果和未执行的验证项。

## 代码约定

- 所有新增或修改的代码注释、Python 文档字符串、测试说明和开发文档使用中文。
- 标识符、数据库字段、JSON 协议字段、环境变量、工具名和第三方 API 保持既有英文名称，不为翻译破坏契约。
- 遵循相邻代码的格式、命名和分层；优先清晰直接的实现，避免不必要的抽象。
- Python 使用类型标注、Pydantic Schema 和 SQLAlchemy 异步会话；Ruff 行宽为 110。
- 前端使用 React、TypeScript、Ant Design。页面容器负责请求和状态，复杂展示逻辑拆入 `frontend/src/components/`。
- 修改 API 响应时同步更新 `frontend/src/api.ts` 的类型和对应组件。
- 面向运营人员的文案使用业务语言，不展示内部表名、字段名、模型参数、构造参数或技术实现细节。

## 业务与安全边界

- 经营数据只读。不得实现改价、发布商品、创建采购单、调整库存、投放广告、发送消息或其他业务数据写入。
- 不增加店铺实体、店铺切换、多店铺分析或店铺级权限。
- 不暴露任意 SQL、任意 Shell、任意文件访问或浏览器自动化接口。
- 不在代码、日志、错误响应、测试快照或前端中泄漏 `.env`、密码、Cookie、API 密钥和数据库连接串。
- 应用库与经营库必须保持独立连接；经营查询只能通过 `commerce_reader` 只读账号。
- 所有会话、任务、Evidence、成果、资料、历史和个人记忆访问必须校验当前用户与资源归属。
- 新增与会话关联的数据表时，必须同步更新会话级联删除逻辑及测试。
- 修改 SQLAlchemy 模型时新增 Alembic 迁移，放入 `backend/migrations/versions/`；不要用启动时临时建表替代迁移。

## Agent 修改规则

- 保持主链路 `Guard → Plan → Execute → Tools / Subtasks → Evaluate`。不要通过问题关键词硬编码业务路由或固定结论。
- `Plan` 只定义目标、成功标准和步骤；`Action` 决定当前步骤的具体动作；`Decision` 决定继续、重规划、追问、完成或停止。模型输出必须经过 Pydantic Schema 校验。
- `Task.state` 是任务运行状态的唯一事实来源；不要引入与其竞争的状态或 checkpoint 存储。
- 模型调用、工具调用、计划版本、预算、Evidence 和失败结果必须可审计、可恢复。
- 工具的原始结果持久化为 Evidence，进入模型的是受字符预算限制的 Observation。不得将大表、长文档或完整历史直接放入上下文。
- 工具失败和查询为空必须形成结构化 Observation，允许模型修正参数、改用其他工具、追问或交付部分结论；空结果不能充当业务事实证据。
- 一次 Execute 最多并行 6 个互不依赖的只读工具。保存成果和提出长期记忆候选必须单独执行。
- 新增调用不得绕过模型、工具、重规划和活跃执行时间预算；相同工具和参数最多执行两次。
- 子 Agent 仅用于独立只读调查，不能保存成果、写入长期记忆、读取其他任务历史或再次委派。

## 工具与外部服务

新增或修改 Agent 工具时：

1. 在 `backend/app/agent/tools.py` 定义严格的 Pydantic 参数 Schema、中文说明和实现分支。
2. 明确工具是否只读、可否并行、子 Agent 是否可用，以及结果大小限制。
3. 将原始结果写入 Evidence，并提供受预算限制的 Observation；大结果应支持按 ID 或分页读取。
4. 更新 `backend/app/agent/model.py` 中必要的使用边界，避免模型虚构工具能力。
5. 覆盖正常结果、空结果、参数错误、权限错误和外部服务失败。
6. 外部服务未配置时从工具目录隐藏；失败时返回可恢复的结构化错误。

## RAG 与记忆规则

- 当前资料格式为 Markdown、TXT、CSV、PDF、DOCX。新增格式必须同时完善解析、分块、索引、权限过滤、引用展示、测试和评测资料。
- 分块需保留标题层级、来源位置、PDF 页码和表格上下文。表格切分时保留表头，不能让数据行失去列语义。
- 检索流程保持：`ts_rank_cd` 全文候选 + pgvector HNSW 向量候选 → RRF 融合 → Cross-Encoder 精排。变更任一环节时更新 RAG 评测和基线。
- 检索必须在用户、资料启停、来源选择和版本过滤后执行。引用必须指向真实命中的文档片段或页码。
- 用户要求“仅依据资料”时，禁止以经营库或网页信息补充结论；资料未覆盖时明确说明无法确认。
- 任务历史召回只能使用当前可信 `task_id`，严禁跨任务读取。长期记忆只有用户确认后才可跨任务使用。
- 上下文优先级固定为：当前指令 → 有效约束 → 当前有效证据 → 近期原文 → 摘要 → 当前任务历史 → 已确认长期记忆。

## 验证命令

在项目根目录执行。测试和评测应使用隔离环境，不得连接正在运行的应用数据。

```powershell
# 后端静态检查
Push-Location backend
python -m ruff check app tests
Pop-Location

# 前端构建
Push-Location frontend
npm.cmd run build
Pop-Location

# 隔离测试
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -f docker-compose.test.yml down -v
```

```powershell
# RAG 评测
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from rag-evaluation rag-evaluation
docker compose -f docker-compose.test.yml down -v

# 记忆评测
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from memory-evaluation memory-evaluation
docker compose -f docker-compose.test.yml down -v

# Agent 评测
docker compose -f docker-compose.test.yml run --rm agent-evaluation python -m app.evaluation --list
docker compose -f docker-compose.test.yml run --rm agent-evaluation python -m app.evaluation --repeat 1
docker compose -f docker-compose.test.yml down -v
```

- 变更模型、工具、提示词、计划协议、上下文策略、权限、分块或检索逻辑后，运行对应评测。
- 评测集位于 `backend/evaluation/{rag,memory,agent}/v1/`；变更评测数据时同步维护 `manifest.json`、`cases.json` 和基线。
- 修改 `.env` 后，用 `docker compose up -d` 重建受影响服务。不要执行 `docker compose down -v`，除非用户明确要求清除本地数据卷。

## 交付前检查

- 是否满足只读经营数据、用户隔离、当前任务历史隔离和长期记忆确认边界？
- 是否为新增模型、工具、检索或状态行为提供 Schema 校验、审计、Evidence/Observation 和失败处理？
- 是否更新了相关测试、评测、迁移、前端类型、环境变量或文档？
- 是否执行了与改动匹配的验证，并如实报告结果？
- 是否保留了其他工作区改动，且未提交生成文件、模型、报告或凭据？

## Git 规则

- 默认只提供提交信息，不执行 `git commit`。
- `data/`、`models/`、`evaluation-reports/`、构建产物、缓存、虚拟环境和 `.env` 为本地生成内容，不应提交。
- 提交信息使用中文 Conventional Commits，例如：`feat(rag): 改进表格分块与引用定位`。