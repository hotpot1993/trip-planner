#!/bin/sh
# 容器启动前的补料与检查。
#
# 补的是什么：车站名表。它按设计放在数据目录里（`config.DATA_DIR /
# "rail_stations.json"`，由 `lushu/adapters/rail.py` 读取），可一旦给
# 数据目录挂上卷，那个目录就是空的 —— 卷把镜像里的内容盖住了。
#
# 缺了它不报错：适配器会去 12306 现拉一份（约 200 KB）再写回缓存。
# 但那把一个本来离线的活变成了必须联网，而这份表镜像里本来就有，
# 所以它在镜像里被放在挂载点之外：/app/lushu/seed/。
#
# 只补缺的，不覆盖已有的：容器里那份缓存可能已经比镜像里的新。
set -e

DATA_DIR="${LUSHU_DATA_DIR:-/app/data}"
SEED_DIR="/app/lushu/seed"
mkdir -p "$DATA_DIR"

# 挂 .env.local 有个坑：宿主机的文件要是还没建，Docker 不报错，它会照挂，
# 只是在容器里把那个路径建成一个**目录**。而 python-dotenv 碰到目录既不说
# 也不加载（实测返回 False，不抛异常）—— 于是服务照常起来、一个 Key 都没有，
# 界面上只有一行「缺少配置」。所以这里直接拒绝启动，把原因说清楚。
if [ -d /app/.env.local ]; then
    echo "[路书] /app/.env.local 是个目录，不是文件，容器不启动。" >&2
    echo "        多半是宿主机上那个 .env.local 还没建出来，Docker 就按目录挂进来了。" >&2
    echo "        在宿主机上建好它（内容照 .env.example），或者去掉 compose 里那条挂载。" >&2
    exit 1
fi

for name in rail_stations.json; do
    if [ ! -e "$DATA_DIR/$name" ] && [ -e "$SEED_DIR/$name" ]; then
        cp "$SEED_DIR/$name" "$DATA_DIR/$name"
        echo "[路书] 数据目录里没有 $name，已从镜像补上"
    fi
done

exec "$@"
