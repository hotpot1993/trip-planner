# 路书

本地优先的旅行攻略规划工具。它解决三个问题：

1. **网友攻略里的信息不可信也不可用**——把散落的自然语言变成可溯源、有置信度、能挂到具体景点上的结论
2. **需要预约的景点会让人白跑一趟**——故宫、陕西历史博物馆这类景点的预约规则必须准确且醒目
3. **一次旅行常常跨多座城市**——而大多数工具默认单城市

第一项与第二项是挂在第三项上的信息层，**多城市规划是骨架**。

---

## 当前状态

**M0 底座已完成**，正在进入 M1 单城市闭环。

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 底座 | 仓库结构、引擎隔离、表结构、前后端骨架、架构边界测试 | **完成** |
| M1 单城市闭环 | 对话层与排程流水线、行程编辑器、路线总览图 | 下一步 |
| M2 多城市 | 城市停留序列、城际转移、预算分栏、多城市天气 | 未开始 |
| M3 数据链路 | 导入、提纯、独立来源归组、实体对齐、数据工作台、金标准评测 | 未开始 |
| M4 预约子系统 | 规则种子库、复核、卡片标注、预约清单、日历导出 | 未开始 |
| M5 候选池接入 | 攻略知识库优先加高德补全 | 未开始 |
| M6 路书导出 | 单文件 HTML、导航深链、契约校验 | 未开始 |

M0 已落地的具体内容：

- **引擎隔离**：`third_party/floattrip/` 从上游提交 `ec911f7` 拷入 48 个引擎文件（约 7,900 行），剔除 `api/`、`main.py` 与 `core/auth.py`，import 前缀机械改写 107 行。出处、剔除理由、改动摘要与 48 个文件的上游哈希记录在该目录的 `README.md` 与 `MANIFEST.txt`
- **表结构**：本项目七域共 17 张表（实体、素材、知识、预约、行程、人工介入），与引擎自带的 13 张表（会话、运行、检查点）共存于同一个 SQLite 文件。坐标字段一律带 `_gcj02` 后缀
- **领域规则**：不含任何框架的三组规则——总天数与转移落点、置信度与复验分级、复核门禁与预约清单
- **架构边界**：`tests/test_architecture_boundaries.py` 拦截 Web 框架渗入领域层、绕过适配层直引 vendored 代码、以及反向依赖。已实测注入违规会失败
- **前后端**：`python -m lushu` 起本地服务并托管构建后的前端；152 项测试全绿，`ruff` 无告警

---

## 快速开始

```powershell
# 1. 建立虚拟环境并安装依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 2. 配置环境变量（高德 Key 与 LLM Key 必填）
Copy-Item .env.example .env.local
# 然后编辑 .env.local

# 3. 启动本地服务
.\.venv\Scripts\python.exe -m lushu
# 浏览器打开 http://127.0.0.1:8756
```

前端单独开发时：

```powershell
cd web
pnpm install
pnpm dev
```

---

## 目录结构

```
lushu/                    自有后端代码（接口层 → 应用服务 → 领域 → 适配器）
├── api/                  接口层，唯一允许依赖 FastAPI 的地方
├── services/             应用服务
├── domain/               领域实体与规则，不依赖任何框架
├── adapters/             外部适配器（高德、天气、12306）
├── store/                SQLite 连接与表结构
├── engine/               薄适配层，唯一允许触碰 third_party 的地方
└── cli.py                数据管线命令行

third_party/floattrip/    从 FloatTrip 隔离拷贝的引擎（见该目录 README）
web/                      前端（Vite + React + TypeScript）
tests/                    测试，含架构边界测试
docs/                     设计文档与决策记录
data/                     本地数据（不进版本库）
```

依赖方向固定为 `api → services → domain → adapters → store`，反向依赖由 `tests/test_architecture_boundaries.py` 拦截。

---

## 文档

- [`CONTEXT.md`](CONTEXT.md) —— 术语表，任何与代码冲突的用词以此为准
- [`docs/DESIGN.md`](docs/DESIGN.md) —— 设计文档：架构、数据模型、数据链路、排程、实施顺序、风险
- [`docs/adr/`](docs/adr/) —— 架构决策记录
