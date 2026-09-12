"""`python -m lushu` —— 启动本地服务。

等价于 `ls serve`。数据管线子命令走 `ls <子命令>`。
"""

from __future__ import annotations

from lushu.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["serve"]))
