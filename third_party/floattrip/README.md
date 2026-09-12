# third_party/floattrip —— 隔离拷贝的引擎

本目录是从 FloatTrip 拷贝进来的代码，**不是本项目的自有代码**。它被视为可丢弃的：一旦本项目需要开源或分享，替换或重写本目录即可（见 [`docs/adr/0005-vendored-floattrip-code-is-isolated.md`](../../docs/adr/0005-vendored-floattrip-code-is-isolated.md)）。

**业务层不得直接 import 本目录。** 唯一允许触碰它的是 `lushu/engine/`，那一层薄适配负责路径、数据库与配置的对接。

---

## 来源

| 项 | 值 |
|---|---|
| 上游仓库 | `https://github.com/shouzhuoshouzhuo/FloatTrip` |
| 提交哈希 | `ec911f7d59e84535535cbcff2d09915336c4d08e` |
| 提交时间 | 2026-08-13 |
| 拷贝日期 | 2026-09-12 |
| 拷贝方式 | 从完整克隆逐文件复制，再作下述 import 改写 |

项目最初引用的 `https://github.com/hotpot1993/Trip-planner` 经核实是该上游仓库的 fork，且 fork 侧零自研提交、内容与上游完全同步。**因此以真正的上游为准。**

## 许可状况（务必知悉）

**上游仓库没有任何 LICENSE 文件。** 它的 README 里写着 MIT 徽章与 `## 📄 License [MIT](LICENSE)` 链接，但 `LICENSE` 文件并不存在——该链接指向 404。

这意味着在法律上默认「保留所有权利」，README 里的 MIT 声明不生效。本项目为自用工具，实际风险为零；但**本目录的存在是将来开源前必须处理的唯一障碍**。

## 拷贝范围

`core/`、`llm/`、`providers/`、`planning/`、`chat/`、`runtime/` 六个子包，共 48 个 `.py` 文件。

选择依据是依赖闭包分析：这六个子包彼此自洽，且**对 `fastapi` 与 `starlette` 零命中**——只有上游的 `app/api/` 依赖 Web 框架。因此它们可以被整体搬走而不拖入 Web 层。

## 未拷贝的内容及理由

| 未拷贝 | 理由 |
|---|---|
| `app/api/`（7 文件 1,264 行） | 接口层由 `lushu/api/` 自己写；且它是唯一依赖 FastAPI 的部分 |
| `app/main.py` | 应用装配由 `lushu/main.py` 自己写 |
| `core/auth.py` | 本项目无账号体系（自用）。已核实：除 `api/*` 与 `main.py` 外无人引用它 |
| `data/langgraph-checkpoints.db` 等 | 上游把一个 5.4MB 的二进制检查点库提交进了版本控制，且 `.gitignore` 里明明列了它（忽略规则失效）。状态库必须本地生成 |
| `mobile-app/android/app/debug.keystore` | 调试签名密钥入库，绝不复用 |
| `mobile-app/`、`mobile-prototype/` | 本项目只做响应式 Web，不做原生 App |
| `xiaohongshu-floattrip/`、`launch-page.html`、`app-pages-preview.html` | 推广素材与营销页面 |
| 约 75 张 PNG、`static/`、`openspec/`、`.codex/` | 与本项目无关的仓库包袱 |

## 对源码做的改动

### 改动一：import 前缀改写（107 行，纯机械）

把 import 语句中的 `app.` 前缀改为 `third_party.floattrip.`。

改写**严格限定在 import 语句行**（`^\s*from\s+app\.` 与 `^\s*import\s+app\.`），因为上游存在 3 处非 import 的 `app.` 会被误伤：

- `planning/graph.py` 的 `app.astream_events(...)`——这里的 `app` 是局部变量，指编译后的图
- `core/database.py` 的 `"data" / "app.db"`——字符串里的库文件名
- `runtime/observability.py` 的 `logging.getLogger("app.runtime")`——日志器名称

### 改动二：让高德 POI id 流到最终计划（3 处，纯增量）

**动机**：ADR-0002 规定高德 POI id 是全系统的实体主键，攻略知识库以它为挂载点。但上游在 `poi_to_spot()` 里把这个字段丢掉了——它返回的字典有 name、rating、location、address，唯独没有 `id`；`_finalize_impl()` 又只按白名单复制字段，于是 id 在整条流水线上彻底消失。

后果是：行程里的景点只能靠**名字**与知识库关联。而名字是会被改写的（「故宫」与「故宫博物院」），用它做关联等于放弃了实体对齐，还要在每次规划后拿名字去反查高德。用一个字段的损失换一次反查，不划算。

改动内容：

| 文件 | 改动 |
|---|---|
| `providers/amap/poi.py` | `poi_to_spot()` 的返回字典增加 `amap_poi_id` |
| `planning/nodes.py` | `_finalize_impl()` 的 timeline 项增加 `amap_poi_id` |
| `planning/nodes.py` | `_finalize_impl()` 的 `candidate_spots` 白名单增加 `amap_poi_id` |

三处都是**新增字段**，不改变任何既有字段、不改变任何控制流。上游代码即使不认识这个字段也不受影响。

### 除此之外没有改动任何逻辑

以上两处之外，本目录与上游逐字一致（哈希见 `MANIFEST.txt`，记录的是上游原始文件，便于核对）。

## 已知适配项（M1 必须处理）

这些不是缺陷，是换位置之后必然出现的水土不服：

1. **`core/env.py` 的 `.env.local` 路径失效。** 它按 `Path(__file__).resolve().parents[2]` 定位，原本指向项目根；现在指向 `third_party/floattrip/`。处理方式是让 `lushu/config.py` 先加载环境变量，使它的 `load_local_env()` 变成无害的空操作。
2. **`core/database.py` 的默认库路径失效。** 它按 `parents[3] / "data" / "app.db"` 定位，现在会指向 `third_party/data/app.db`。处理方式是在 `lushu/engine/bootstrap.py` 里调用它自带的 `configure_database()`——上游留了这个接缝，正是为这类场景准备的。
3. **表结构冲突。** `core/database.py` 的 `init_db()` 会建立上游自己的 13 张表（`users`、`itineraries`、`conversations`…），而本项目有自己的七域表结构。两者可以共存于同一个 SQLite 文件，但**行程数据以谁为准必须有明确决定**——这是 M1 的第一个设计任务，不能含糊过去。
4. **隐式用户。** 上游的表普遍带 `user_id NOT NULL REFERENCES users(id)`，而本项目没有账号体系。需要一条固定的本地用户记录来满足外键。

## 与上游再同步

改写的规则是确定性的，因此再同步可以脚本化：重新克隆上游 → 复制同样六个子包 → 套用同一条 import 改写 → 用 `MANIFEST.txt` 里的哈希核对有无改动。`MANIFEST.txt` 记录的是**上游原始文件**的 SHA256，不是改写后的文件，正是为了方便这个比对。

## 校验

`MANIFEST.txt` 内含上游仓库地址、提交哈希、拷贝日期，以及 48 个文件各自在上游的 SHA256。

导入可用性已实测：19 个代表模块在 Python 3.14.7 与 langgraph 1.2.11、langchain-openai 1.6.2 上全部导入成功。
