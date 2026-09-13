# 路书镜像。
#
# 两段构建：Node 只用来产出前端静态文件，运行镜像里没有 Node 也没有 npm。
#
# 平时不用手工构建 —— 推到 master 之后 .github/workflows/docker.yml 会构建
# 并推送到 hotpot1993/trip-planner。手工构建的话在仓库根目录执行：
#   docker build -t hotpot1993/trip-planner:latest .
#
# 构建参数是为国内网络准备的，默认值就是官方源：
#   docker build \
#     --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
#     -t hotpot1993/trip-planner:latest .


# ─── 第一段：构建前端 ────────────────────────────────────────────
FROM node:24-trixie-slim AS web

ARG NPM_REGISTRY=https://registry.npmjs.org

WORKDIR /build

# pnpm 钉在开发机那个版本上，免得「本机建得出来、镜像里建不出来」
RUN npm install -g pnpm@11.22.0 --registry="${NPM_REGISTRY}"

# 先只拷清单：依赖没动的时候这一层能命中缓存
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --registry="${NPM_REGISTRY}"

COPY web/ ./
# package.json 里的 build 是 `tsc --noEmit && vite build`：
# 类型错误会让镜像构建失败，而不是把一份坏前端打进镜像。
RUN pnpm build


# ─── 第二段：运行 ────────────────────────────────────────────────
FROM python:3.14-slim-trixie AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple

# TZ 不是装饰。预约提醒按 date.today() 算「今天」，容器默认 UTC，
# 北京时间 0 点到 8 点之间它算出来的是昨天 —— 抢票日会整整差一天，
# 而界面上一个字都不会提。官方 python:slim 自带 tzdata，改这个变量就够。
#
# LUSHU_HOST 必须是 0.0.0.0：默认值是 127.0.0.1，容器自己起得来、
# 日志也好看，但外面一个请求都进不来。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    LUSHU_HOST=0.0.0.0 \
    LUSHU_PORT=8756 \
    TZ=Asia/Shanghai

WORKDIR /app

# 只装 pyproject.toml 里声明的依赖，不装 lushu 这个包本身。
#
# 理由是 config.py 用 __file__ 的上一级推项目根（ROOT_DIR）：装进
# site-packages 之后它会推成 site-packages，于是 data/ 与 web/dist
# 全部找错地方 —— 前端不挂载、库建在 site-packages 里，而且都不报错。
# 代码直接从 /app 跑，ROOT_DIR 才是 /app。
#
# 拼行用 chr(10)：Dockerfile 里反斜杠是转义字符，能不写就不写。
COPY pyproject.toml ./
RUN python -c "import pathlib,tomllib;pathlib.Path('/tmp/requirements.txt').write_text(chr(10).join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY lushu/ ./lushu/
COPY third_party/ ./third_party/

# 前端产物来自第一段。app.py 在导入期就检查 web/dist/index.html，
# 它不存在的话根路径会返回一句「前端尚未构建」——服务是活的，页面是空的。
COPY --from=web /build/dist ./web/dist

# 车站名表跟着 `COPY lushu/` 一起进来，落在 /app/lushu/seed/rail_stations.json。
# 它刻意不在 /app/data 下：挂上卷之后那个目录会被盖住，放那儿等于没放。
# 首次启动时由 entrypoint 把它补进数据目录。
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8756

# 探的是 /api/health，一个真能被读到的状态，不是「进程还在不在」。
# 端口跟着 LUSHU_PORT 走，改了端口不用改这里。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('LUSHU_PORT','8756')+'/api/health',timeout=4)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "lushu", "serve"]
