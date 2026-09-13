# 部署：Docker 镜像与飞牛 NAS

这份文档说清三件事：镜像怎么构建、怎么推到 Docker Hub、飞牛 NAS 上怎么拉起来。
命令都按「在仓库根目录执行」写。

> **本文写的时候，这台开发机上没有装 Docker**（也没有 WSL），所以镜像本身
> 没有在本地构建过。构建前能验证的部分都验证了，验证了什么、没验证什么，
> 列在文末「构建前验证过什么」一节 —— 请连没验证的一起看。

---

## 一、镜像里有什么

| 内容 | 说明 |
|---|---|
| `lushu/` 全套 Python 代码 | 含 `lushu/seed/booking_rules.json`（预约规则种子，走版本库） |
| `third_party/floattrip/` | 上游引擎，逐字拷贝，随源码进镜像 |
| `web/dist/` | 前端产物，**在镜像构建时现场编译**，不用本地那份 |
| `data/rail_stations.json` | 车站名表，放在 `/app/seed/` 而不是 `/app/data`（下面解释） |
| Python 3.14 + pyproject 里声明的依赖 | 官方 `python:3.14-slim-trixie` |

**镜像里没有**：`.env.local`（真 Key）、`data/lushu.db`（真实库）、素材底档、
导出的路书、`_raw/`、`tests/`、`scripts/`。都写在 `.dockerignore` 里。

### 三个不显眼但会让部署出错的点

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
RUN python -c "...tomllib...写 /tmp/requirements.txt..." \
    && pip install -r /tmp/requirements.txt
```

容器里因此没有 `ls` 这个命令，要用完整的：`python -m lushu <子命令>`。
（等价于 `ls <子命令>`，见 `lushu/__main__.py`。）

**2. 车站名表放在 `/app/seed/`，不在 `/app/data/`。** 因为一旦给 `/app/data`
挂上卷，镜像里那份就被盖住了。`deploy/entrypoint.sh` 在启动时把它补进数据
目录，只补缺的、不覆盖已有的。

缺了它其实不报错：适配器会去 12306 现拉一份再写回缓存。但那把一个本来离线的
活变成必须联网，而这份表镜像里本来就有。

**3. `TZ=Asia/Shanghai` 是必需的，不是顺手写的。** 预约提醒按 `date.today()`
算「今天」（`lushu/services/booking_store.py`、`lushu/api/trip_routes.py`）。
容器默认 UTC，北京时间 0 点到 8 点之间它算出来的是昨天 —— **抢票日整整差一天，
而界面上一个字都不会提**。官方 `python:slim` 自带 tzdata，所以改环境变量就够。

---

## 二、构建镜像

### 在哪构建：三条路

| 路径 | 适合 | 代价 |
|---|---|---|
| **A. 本机装 Docker Desktop** | 想让我全程自动跑完构建与推送；能在推之前先起容器自测一遍 | 要装 Docker Desktop + WSL2，可能要重启一次 |
| **B. 在 NAS 上构建** | 本机不想装任何东西；架构天然对得上 | 需要开 NAS 的 SSH；NAS 上编译前端比 PC 慢；要先把源码送过去 |
| **C. GitHub Actions** | 以后每次 push 自动出新镜像 | 要先把代码推到 GitHub，再配 Docker Hub 的 secrets |

构建本身是同一条命令，三条路只差「在哪台机器上敲」。

### 命令

```bash
cd <仓库根目录>

# 单架构（构建机的架构）
docker build -t <账号>/lushu:0.1.0 -t <账号>/lushu:latest .

# 双架构（NAS 是 ARM 但你在 x86 上构建，或反过来）
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t <账号>/lushu:0.1.0 -t <账号>/lushu:latest \
  --push .
```

`--push` 与 `--load` 不能同时用：多架构构建的产物是一个 manifest 列表，
只能直接推进 registry，不能装进本地 `docker images`。

国内网络下构建可以换源（两个参数默认就是官方源）：

```bash
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t <账号>/lushu:0.1.0 -t <账号>/lushu:latest .
```

### ⚠️ 在 NAS 上构建（路径 B）之前必须知道的一件事

`data/rail_stations.json` **不在版本库里**（`.gitignore` 第 13 行 `data/`）。
`git clone` 下来的仓库里没有这个文件，而 Dockerfile 要 `COPY` 它 ——
构建会以「文件找不到」失败。

两个办法：

```bash
# 办法一：从开发机把它传过去
scp "data/rail_stations.json" <用户>@<NAS>:/path/to/lushu/data/

# 办法二：在 NAS 上让适配器重新拉一份（需要能连 12306），
#         再把内容整理成 {"fetched_at": "...", "stations": [...]} 的格式
```

失败是响的（构建中断、报文件不存在），不会给你一个缺料的镜像。

---

## 三、推到 Docker Hub

```bash
docker login -u <账号>

docker push <账号>/lushu:0.1.0
docker push <账号>/lushu:latest
```

多架构那次 `buildx --push` 已经推完了，不用再 push。

**公开还是私有，取决于你要不要让别人看到这个镜像。** 镜像里没有 Key、
没有行程数据，公开不会泄露隐私；但这个服务本身没有任何鉴权，谁拉下来
接上自己的 Key 都能跑。仓库设成私有的话，NAS 上 `docker login` 一次即可。

---

## 四、飞牛 NAS 上拉起来

飞牛的「容器」应用支持 Docker Compose 项目，直接把仓库根的
`docker-compose.yml` 粘进去，改三处：

```yaml
    image: <账号>/lushu:latest        # ← 你的 Docker Hub 用户名
    environment:
      AMAP_API_KEY: ""                # ← 高德 Web 服务 Key
      AMAP_JS_KEY: ""                 # ← 高德 JS API Key
      AMAP_JS_SECURITY_CODE: ""       # ← JS API 安全密钥
      DEEPSEEK_API_KEY: ""            # ← DeepSeek Key
    volumes:
      - /vol1/1000/docker/lushu/data:/app/data   # ← 换成 NAS 上的真实目录
```

`./data` 这种相对路径相对的是 Compose 项目所在目录，飞牛上建议写绝对路径。

### 密钥：两种给法，挑一种

**一是直接写在 compose 的 `environment` 里**（上面那种）。最省事，缺点是
Key 明文躺在 Compose 文件里，飞牛界面上也看得见。

**二是挂载 `.env.local`**。开发机上本来就有这个文件，传上去即可：

```yaml
    volumes:
      - ./data:/app/data
      - ./env.local:/app/.env.local:ro
```

`lushu/config.py` 会读 `/app/.env.local`（`load_dotenv(..., override=False)`，
已存在的环境变量优先）。要注意 `lushu/config.py` 里的路径是
`ROOT_DIR / ".env.local"`，也就是 `/app/.env.local`，不是别的目录。

两种都给也可以，环境变量赢。

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
`engine.available` 为 `true`。缺 Key 不会让它失败，只会在 `warnings` 里
列出来 —— 那是刻意的：缺 Key 也要能打开界面看到提示。

3 的期望：`lushu.db` 与 `rail_stations.json` 都在。后者是 entrypoint 补的，
没有它说明卷挂载点不是 `/app/data`，或者补料那一步没跑。

然后打开 `http://<NAS>:8756`，界面上**空库是正常的** —— 镜像不带任何行程。
新建一个行程试试：能建出来，说明高德与 LLM 的 Key 都对。

---

## 六、升级与回滚

```bash
docker compose pull && docker compose up -d
```

数据在卷里，容器换掉不影响。回滚同理，把 `image:` 换回旧 tag 再 `up -d`。

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
docker tag <账号>/lushu:latest registry.cn-hangzhou.aliyuncs.com/<命名空间>/lushu:latest
docker push registry.cn-hangzhou.aliyuncs.com/<命名空间>/lushu:latest
```

**3. 不走 registry，直接搬文件。** 在构建机上导出，拷到 NAS，再导入：

```bash
# 构建机
docker save <账号>/lushu:latest -o lushu-0.1.0.tar

# 拷到 NAS 之后
docker load -i lushu-0.1.0.tar
```

`docker save` 出来的 tar 是分架构的：在 x86 上导的包只能在 x86 的 NAS 上用。

---

## 八、构建前验证过什么

这台机器上没有 Docker，所以下面这些是**构建之外**能查的都查了：

| 查了什么 | 怎么查的 | 结果 |
|---|---|---|
| 依赖在 Linux 上有没有轮子 | 用 pip 的 `--platform/--python-version/--abi` 按 `manylinux cp314` 解析一遍，`--only-binary=:all:` 强制只用轮子 | amd64 与 arm64 **都能全部解析出轮子**，不需要在镜像里装编译器 |
| 基础镜像标签存不存在、支持哪些架构 | 查 Docker Hub 的 tags 接口 | `python:3.14-slim-trixie`、`node:24-trixie-slim` 都在，且都有 linux/amd64 与 linux/arm64 |
| 基础镜像带不带 tzdata | 查 docker-library/python 的 `3.14/slim-trixie/Dockerfile` | 带（`apt-get install` 里有 `tzdata`），所以 `ENV TZ` 直接生效 |
| 前端产物路径对不对 | 看 `web/dist/index.html` 与 `vite.config.ts` | 资源引用是绝对路径 `/assets/...`，而 `StaticFiles` 挂在 `/`，对得上 |
| 依赖清单是不是只有一处 | pip 解析出来的包与 `pyproject.toml` 声明的对得上，`sqlite-vec`、`httpx2` 等是传递依赖 | 是，`pyproject.toml` 一处，镜像不另存一份 |
| 车站缓存格式与新鲜度 | 读 `data/rail_stations.json` | `fetched_at: 2026-09-12`，3384 站；30 天内有效，之后会自动重拉 |

**没验证的**（要等真机构建）：镜像能不能构建出来、容器能不能起来、
健康检查过不过、飞牛上拉取顺不顺、挂载之后写不写得进去。
第一次构建如果出错，把完整输出贴出来即可。

**记下这批依赖的版本**：构建当天 `pip` 解析出的是
`fastapi 0.141.1 / uvicorn 0.52.4 / pydantic 2.13.5 / langgraph 1.2.11 /
langgraph-checkpoint-sqlite 3.1.1 / langchain-openai 1.6.2 / httpx 0.28.1 /
python-dotenv 1.2.3`，与开发机虚拟环境一致。`pyproject.toml` 写的是范围不是
精确版本，以后重建可能解析到更新的版本；真出了问题，先用上面这组版本对齐。
