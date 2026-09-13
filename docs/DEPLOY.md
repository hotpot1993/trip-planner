# 部署：Docker 镜像与飞牛 NAS

这份文档说清三件事：镜像怎么构建、怎么推到 Docker Hub、飞牛 NAS 上怎么拉起来。

目前定的路子是 **GitHub Actions 构建并推送**，NAS 只负责拉取与运行。
命令除非另说，都在仓库根目录执行。

> **写这份文档的机器上没有 Docker**（也没有 WSL），所以镜像是 GitHub Actions
> 构建的，不是本机构建的。构建前能查的都查了，构建之后 CI 又把镜像拉下来
> 真跑了一遍；验过什么、没验什么，列在文末「验证过什么」一节 ——
> 请连没验的一起看。

---

## 一、镜像里有什么

| 内容 | 说明 |
|---|---|
| `lushu/` 全套 Python 代码 | 含 `lushu/seed/booking_rules.json`（预约规则种子）与 `lushu/seed/rail_stations.json`（车站名表），两者都走版本库 |
| `third_party/floattrip/` | 上游引擎，逐字拷贝，随源码进镜像 |
| `web/dist/` | 前端产物，**在镜像构建时现场编译**，不用本地那份 |
| Python 3.14 + pyproject 里声明的依赖 | 官方 `python:3.14-slim-trixie` |

**镜像里没有**：`.env.local`（真 Key）、`data/`（真实库、素材底档、导出的路书）、
`_raw/`、`tests/`、`scripts/`。都写在 `.dockerignore` 里。

### 四个不显眼但会让部署出错的点

**1. 镜像里没有安装 `lushu` 这个包。** 代码是直接从 `/app` 跑的。

`lushu/config.py` 用 `__file__` 的上一级推项目根：

```python
ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIST_DIR = ROOT_DIR / "web" / "dist"
DATA_DIR = _env_path("LUSHU_DATA_DIR", ROOT_DIR / "data")
```

装进 `site-packages` 之后，`ROOT_DIR` 会推成 `site-packages`，于是前端挂不上
（根路径返回「前端尚未构建」，服务本身却是健康的）、库会建在 `site-packages`
里（不在卷上，删容器就没），而且**两件事都不报错**。所以 Dockerfile 只装依赖：

```dockerfile
RUN python -c "...从 pyproject.toml 抽依赖写 /tmp/requirements.txt..." \
    && pip install -r /tmp/requirements.txt
```

容器里因此没有 `ls` 这个命令，要用完整的：`python -m lushu <子命令>`
（等价于 `ls <子命令>`，见 `lushu/__main__.py`）。

**2. `TZ=Asia/Shanghai` 是必需的，不是顺手写的。** 预约提醒按 `date.today()`
算「今天」（`lushu/services/booking_store.py`、`lushu/api/trip_routes.py`）。
容器默认 UTC，北京时间 0 点到 8 点之间它算出来的是昨天 —— **抢票日整整差一天，
而界面上一个字都不会提**。官方 `python:slim` 自带 tzdata，所以改环境变量就够。

**3. 车站名表在 `/app/lushu/seed/`，不在 `/app/data/`。** 因为一旦给
`/app/data` 挂上卷，镜像里那份就被盖住了。`deploy/entrypoint.sh` 在启动时把它
补进数据目录，只补缺的、不覆盖已有的。

缺了它其实不报错：适配器会去 12306 现拉一份再写回缓存。但那把一个本来离线的
活变成必须联网，而这份表镜像里本来就有。

**4. 构建要用的文件必须在版本库里。** GitHub Actions 从 clone 出来的仓库构建，
工作区里只有被跟踪的文件。车站名表原先放在 `data/`（被 `.gitignore` 挡着），
本机构建一切正常、CI 上却会以「文件不存在」失败 —— 现在它挪到了
`lushu/seed/`，与 `booking_rules.json` 放在一起。
`tests/test_deploy_artifacts.py` 会拿着 Dockerfile 里每一条 `COPY` 去问
`git check-ignore`，防止再冒出第二个这样的文件。

---

## 二、构建镜像（GitHub Actions）

工作流在 [`.github/workflows/docker.yml`](../.github/workflows/docker.yml)：
推到 `master`、打 `v*` 标签、或手动触发时，构建并推送到
`hotpot1993/trip-planner`。构建的是 `linux/amd64`。

**只需要配一个 secret**（用户名已经写在 workflow 里了）：

1. 打开 Docker Hub → Account settings → Personal access tokens → **New Access Token**
2. 权限勾 **Read & Write**，名字随便（例如 `github-actions`）
3. 复制那一串 token（**关掉页面就再也看不到**）
4. 打开 GitHub 仓库 → Settings → Secrets and variables → Actions →
   **New repository secret**
5. Name 填 `DOCKERHUB_TOKEN`，Secret 填刚才那串

配好之后随便推一次代码，或者在 GitHub 的 Actions 页面点 **Run workflow**。
手动触发时有个 `mode` 可以选：默认 `push`（构建并推送），选 `build-only`
则只构建不推送 —— 改完 Dockerfile 想先验证一遍、又不想动已经发布的镜像时，
用这个。

**`latest` 是按分支名给的**（`github.ref == 'refs/heads/master'`），没有用
metadata-action 的 `is_default_branch`。原因：默认分支要是没设成 master，
`latest` 会**静默地**不打，而 compose 拉的正是 `latest` —— 那种失败到了 NAS 上
只表现为「拉不到镜像」，看不出原因。

### 推完之后还会自己跑一遍

workflow 里还有一个 `smoke` job：把刚推上去的那个 tag 拉下来，不挂卷、不给任何
Key（就是 NAS 上第一次起来的样子），然后核对

- `/api/health` 的 `status` / `database.exists` / `engine.available`
- 根路径回的是前端（有 `id="root"` 与 `/assets/`），不是那句「前端尚未构建」
- `/app/data` 下库与车站名表都在（后者靠 entrypoint 补）
- 容器自己的健康检查最后是不是 `healthy`

**注意顺序是先 push 再 smoke**：这一步要是红了，刚发布出去的那个 tag 就是坏的 ——
修好再推一次会覆盖它。构建成功不等于跑得起来，这一步就是为了那句区别。

产出的标签有三个：

| 标签 | 什么时候有 |
|---|---|
| `latest` | 默认分支（master）上的构建 |
| `sha-xxxxxxxx` | 每次构建都有，对应那一次提交 |
| `0.1.0`（跟着 tag 走） | 推了 `v0.1.0` 这样的标签时 |

**要出双架构**（比如以后换 ARM 的 NAS），把 workflow 里的
`platforms: linux/amd64` 改成 `linux/amd64,linux/arm64` 即可 ——
两种架构的依赖轮子都验过。

### 不在 GitHub 上构建的话

同样的 Dockerfile，在哪台装了 Docker 的机器上都是一条命令：

```bash
docker build -t hotpot1993/trip-planner:latest .
```

国内网络下可以换源（两个参数默认就是官方源）：

```bash
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t hotpot1993/trip-planner:latest .
```

多架构要用 `buildx`，注意 `--push` 与 `--load` 不能同时用：多架构构建的产物是
一个 manifest 列表，只能直接推进 registry，不能装进本地 `docker images`。

---

## 三、推到 Docker Hub

走 GitHub Actions 的话这一步是自动的，不用管。

手工推的话：

```bash
docker login -u hotpot1993
docker push hotpot1993/trip-planner:latest
```

**仓库现在是公开的**，谁都能拉。镜像里没有 Key、没有行程数据，公开不泄露
隐私；但这个服务本身没有任何鉴权，接了 Key 就能跑。想收回来就改成私有，
NAS 上 `docker login` 一次即可。

> **上游许可这件事值得知道**：`third_party/floattrip` 是从
> [FloatTrip](https://github.com/shouzhuoshouzhuo/FloatTrip) 逐字拷贝的，
> 而那个仓库**没有 LICENSE 文件**（GitHub API 返回 `license: null`），
> 按默认规则是「保留所有权利」。公开分发含它代码的镜像，严格说是没有授权的。
> 风险不大但真实存在（通常是收到一封删除请求），要不要公开由你决定。

---

## 四、飞牛 NAS 上拉起来

飞牛的「容器」应用支持 Docker Compose 项目，直接把仓库根的
`docker-compose.yml` 粘进去，准备两样东西。

### 密钥：挂一个 `.env.local` 进去

compose 里**不写密钥**，密钥放在旁边的 `.env.local`，只读挂进容器。理由是
那份 compose 会进版本库，而这个仓库是公开的。

在 Compose 项目所在目录建一个 `.env.local`（内容照 `.env.example`）：

```ini
AMAP_API_KEY=...
AMAP_JS_KEY=...
AMAP_JS_SECURITY_CODE=...
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
```

开发机上本来就有这个文件，直接传上去即可。

**容器里的文件名必须是 `.env.local`。** 宿主机上叫什么都行，但挂载点的右边
得是 `/app/.env.local` —— `lushu/config.py` 里的路径是写死的
（`ROOT_DIR / ".env.local"`）。挂到 `/app/.env` 上的话，容器照样起得来，
只是一个 Key 都读不到。

> **⚠️ 这个坑值得单独说：文件必须先建出来。**
> Docker 挂一个**不存在**的宿主文件时不会报错，它会把容器里那个路径建成一个
> **目录**。而 `python-dotenv` 碰到目录既不报错也不加载（实测：返回 False，
> 不抛异常）—— 于是服务照常启动、所有 Key 都缺，界面上只有一行「缺少配置」，
> 很容易看半天看不出问题在哪。
>
> 所以 `deploy/entrypoint.sh` 里加了一道守卫：`/app/.env.local` 要是目录，
> 直接拒绝启动并说明原因。CI 的 smoke 步骤会把这两种情况都跑一遍 ——
> 正确挂载时 `warnings` 必须为空，挂成目录时容器必须以退出码 1 停下。

`TZ` 是**故意留在 compose 的 `environment:` 里**的，没放进 `.env.local`：
时区得在进程启动时就是对的，而不是等 Python 起来之后再去改环境变量。

想改回去用环境变量给密钥也行：`load_dotenv(..., override=False)`，已存在的
环境变量优先，所以在 compose 里写 `environment:` 会赢过文件。

### 数据目录

```yaml
    volumes:
      - /vol1/1000/docker/lushu/data:/app/data   # ← 换成 NAS 上的真实目录
```

`./data` 这种相对路径相对的是 Compose 项目所在目录，飞牛上建议写绝对路径。

### 目录与权限

容器默认以 **root** 运行。这是为了让挂载目录的权限问题一次都不出现：
Docker 自动创建的宿主目录属主是 root，容器里换个 uid 就会写不进去，
症状是启动时报 `unable to open database file`。

想收紧的话，在 compose 里加 `user: "1000:1000"`，并先在 NAS 上把数据目录
交给那个 uid：

```bash
mkdir -p /vol1/1000/docker/lushu/data
chown -R 1000:1000 /vol1/1000/docker/lushu
```

（飞牛上开 SSH：设置 → 远程访问 → 开启 SSH 服务。）

### 数据目录放哪

放 NAS 自己的卷上（ext4 / btrfs 都行）。**别放 SMB / NFS 挂载的网络盘**：
SQLite 靠文件锁保证一致性，网络文件系统的锁不可靠，损坏是迟早的事，
而且不是每次都立刻看得出来。

---

## 五、部署完先跑这几条

```bash
# 1. 起来了没有、三段状态对不对
curl -s http://<NAS>:8756/api/health

# 2. 容器自己的环境自检（数据目录、库版本、缺失配置）
docker compose exec lushu python -m lushu doctor

# 3. 数据目录里该有的东西在不在
docker compose exec lushu ls -l /app/data
```

`/api/health` 期望看到 `"status": "ok"`、`database.exists` 为 `true`、
`engine.available` 为 `true`，**并且 `warnings` 是空的**。

**`warnings` 那一项就是 `.env.local` 读没读到的判据。** 里面但凡出现
「缺少配置 AMAP_API_KEY」之类，就是密钥没进去 —— 先看那个文件建了没有、
挂载点右边是不是 `/app/.env.local`。缺 Key 不会让服务起不来（那是刻意的：
也要能打开界面看到提示），所以不看这一项的话，问题会以「界面能开但什么都
查不出来」的形式出现。

3 的期望：`lushu.db` 与 `rail_stations.json` 都在。后者是 entrypoint 补的，
没有它说明卷挂载点不是 `/app/data`，或者补料那一步没跑。

然后打开 `http://<NAS>:8756`，界面上**空库是正常的** —— 镜像不带任何行程。
新建一个行程试试：能建出来，说明高德与 LLM 的 Key 都对。

想把自己攒的行程也带过去的话，把开发机上的 `data/lushu.db` 拷进 NAS 那个
数据目录即可（停容器再拷，别在跑的时候拷）。

---

## 六、升级与回滚

```bash
docker compose pull && docker compose up -d
```

数据在卷里，容器换掉不影响。回滚同理，把 `image:` 换成某个 `sha-xxxxxxxx`
标签再 `up -d` —— 这也是每次构建都留一个 sha 标签的用处。

想留一份数据快照的话，直接拷数据目录：

```bash
cp -a /vol1/1000/docker/lushu/data /vol1/1000/docker/lushu/data.bak-$(date +%Y%m%d)
```

`lushu.db` 是主体，`rail_stations.json` 是缓存（丢了会自动重拉）。

---

## 七、Docker Hub 拉不动的时候

国内网络下拉 Docker Hub 经常超时或限速，这是这条路线最可能卡住的地方。
三条退路，按省事程度排：

**1. 给 NAS 配镜像加速器。** 飞牛的容器设置里能填 registry mirror。

**2. 同时推一份到国内 registry**（阿里云 ACR、腾讯云 CCR 等），NAS 从那边拉：

```bash
docker tag hotpot1993/trip-planner:latest registry.cn-hangzhou.aliyuncs.com/<命名空间>/trip-planner:latest
docker push registry.cn-hangzhou.aliyuncs.com/<命名空间>/trip-planner:latest
```

**3. 不走 registry，直接搬文件。** 在构建机上导出，拷到 NAS，再导入：

```bash
# 构建机
docker save hotpot1993/trip-planner:latest -o trip-planner.tar

# 拷到 NAS 之后
docker load -i trip-planner.tar
```

`docker save` 出来的 tar 是分架构的：在 x86 上导的包只能在 x86 的 NAS 上用。

---

## 八、验证过什么

这台机器上没有 Docker，所以下面这些是**构建之外**能查的都查了：

| 查了什么 | 怎么查的 | 结果 |
|---|---|---|
| 依赖在 Linux 上有没有轮子 | 用 pip 的 `--platform/--python-version/--abi` 按 `manylinux cp314` 解析一遍，`--only-binary=:all:` 强制只用轮子 | amd64 与 arm64 **都能全部解析出轮子**，不需要在镜像里装编译器 |
| 基础镜像标签存不存在、支持哪些架构 | 查 Docker Hub 的 tags 接口 | `python:3.14-slim-trixie`、`node:24-trixie-slim` 都在，且都有 linux/amd64 与 linux/arm64 |
| 基础镜像带不带 tzdata | 查 docker-library/python 的 `3.14/slim-trixie/Dockerfile` | 带（`apt-get install` 里有 `tzdata`），所以 `ENV TZ` 直接生效 |
| workflow 里的 action 版本存不存在 | 查各仓库的 latest release | `checkout@v7`、`setup-buildx-action@v4`、`login-action@v4`、`metadata-action@v6`、`build-push-action@v7`，都在 |
| 前端那一段到底建不建得出来 | 把版本库里 `web/` 下的 29 个文件拷进一个空目录（等于 CI checkout 出来的样子），按镜像第一段的命令走一遍 | `pnpm install --frozen-lockfile` 与 `pnpm build`（即 `tsc --noEmit && vite build`）都通过，产出的三个文件名与本地 `web/dist` **完全一致** —— 锁文件是满足的，构建是确定的 |
| 前端产物路径对不对 | 看 `web/dist/index.html` 与 `vite.config.ts` | 资源引用是绝对路径 `/assets/...`，而 `StaticFiles` 挂在 `/`，对得上 |
| Dockerfile 里抽取依赖那行 | 在本地原样执行那条 `python -c "...tomllib..."` | 写出的文件正好是 `pyproject.toml` 里那 8 条依赖，一行一条 |
| 依赖清单是不是只有一处 | pip 解析出来的包与 `pyproject.toml` 声明的对得上，`sqlite-vec`、`httpx2` 等是传递依赖 | 是，`pyproject.toml` 一处，镜像不另存一份 |
| 上线前会不会把密钥带出去 | 拿 `.env.local` 里四个真 Key 去搜整个 git 历史（71 次提交，`git log -S`） | 四个都**未命中**；历史里出现过的敏感路径只有 `.env.example` |
| 车站缓存格式与新鲜度 | 读 `lushu/seed/rail_stations.json` | `fetched_at: 2026-09-12`，3384 站；30 天内有效，之后会自动重拉 |

**还没验证的**：飞牛上拉不拉得动（国内网络）、挂载 NAS 目录之后写不写得进去、
那台机器的架构与端口有没有冲突。这三件只有到了那台机器上才知道。

### 构建之后，CI 把镜像真跑了一遍

上面那些是构建前查的。下面这些是同一份 Dockerfile 在 GitHub 的 Linux runner
上**真跑出来的结果**，不是推断 —— 见 workflow 里的 `smoke` job：

| 查了什么 | 结果 |
|---|---|
| 镜像构建 | 通过。两段都真的跑了：Node 段装依赖并 `pnpm build`，Python 段装依赖 |
| 推送到 Docker Hub | 通过。`latest` 与 `sha-xxxxxxxx` 都在，linux/amd64，75.5 MB，12 层 |
| 镜像里有没有密钥 | 没有。`Env` 里只有 PATH / PYTHON* / LUSHU_* / TZ；构建历史里没有 `.env.local`、`lushu.db` |
| 容器起不起得来 | 起得来，PID 1 就是服务本身（entrypoint 的 `exec` 生效了） |
| `/api/health` | `status: ok`、`database.exists: true`、`engine.available: true`（上游 `ec911f7`） |
| 库建在哪 | `/app/data/lushu.db` —— 说明 `ROOT_DIR` 推对了，没有装成 site-packages 里的包 |
| 前端挂上了没有 | 挂上了。根路径 200，回的是带 `id="root"` 与 `/assets/` 的页面 |
| entrypoint 补料 | 生效。日志里有「数据目录里没有 rail_stations.json，已从镜像补上」，随后该文件在 `/app/data` 下 |
| 容器自己的健康检查 | `starting` → `healthy` |
| 缺 Key 时的行为 | 警告「缺少配置 AMAP_API_KEY、DEEPSEEK_API_KEY」，服务照常提供 |
| **挂 `.env.local` 这条路** | 通。把一份装着假 Key 的 `.env.local` 挂到 `/app/.env.local:ro`，`warnings` 变成空数组 —— 证明挂载点是对的、键真的被读到了 |
| **挂成目录时会怎样** | 拒绝启动。宿主文件不存在时 Docker 会建出目录，这时容器以退出码 1 停下，日志里写明「是个目录，不是文件」 |

**记下这批依赖的版本**：构建当天 `pip` 解析出的是
`fastapi 0.141.1 / uvicorn 0.52.4 / pydantic 2.13.5 / langgraph 1.2.11 /
langgraph-checkpoint-sqlite 3.1.1 / langchain-openai 1.6.2 / httpx 0.28.1 /
python-dotenv 1.2.3`，与开发机虚拟环境一致。`pyproject.toml` 写的是范围不是
精确版本，以后重建可能解析到更新的版本；真出了问题，先用上面这组版本对齐。

---

## 九、暂时没做的两件事

**1. CI 里没跑那 1476 条单元测试。** 工作流只做了三件事：构建、推送、把镜像
拉起来冒烟。冒烟测的是「跑不跑得起来」，测不到业务逻辑。原因是这套测试只在
Windows 上跑过，没在 Linux 上验证过，先拿它挡在构建前面，有可能第一次就红在
某个与环境有关的用例上，反而把镜像卡住。等确认测试在 Linux 上也干净，再加一个
`test` job 让 `image` 依赖它。

**2. 没建 GitHub 仓库自己的 LICENSE。** 上游那份代码怎么处理还没定
（见第三节的说明），定了再补比较合适。
