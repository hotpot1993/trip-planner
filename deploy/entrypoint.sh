#!/bin/sh
# 容器启动前的补料。
#
# 补的是什么：车站名表。它按设计放在数据目录里（`config.DATA_DIR /
# "rail_stations.json"`，由 `lushu/adapters/rail.py` 读取），可一旦给
# 数据目录挂上卷，镜像里那份就被盖住了 —— 卷是空的，文件就不在。
#
# 缺了它不报错：适配器会去 12306 现拉一份（约 200 KB）再写回缓存。
# 但那把一个本来离线的活变成了必须联网，而这份表镜像里本来就有。
# 所以放在挂载点之外的 /app/seed，启动时补进数据目录。
#
# 只补缺的，不覆盖已有的：容器里那份缓存可能已经比镜像里的新。
set -e

DATA_DIR="${LUSHU_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"

for name in rail_stations.json; do
    if [ ! -e "$DATA_DIR/$name" ] && [ -e "/app/seed/$name" ]; then
        cp "/app/seed/$name" "$DATA_DIR/$name"
        echo "[路书] 数据目录里没有 $name，已从镜像补上"
    fi
done

exec "$@"
