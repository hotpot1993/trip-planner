"""探测脚本的共用引导。

放在这里而不是让每个脚本各写一遍，是因为「先把项目根加进 sys.path、
再把 stdout 切到 UTF-8」这个动作必须在导入项目模块**之前**完成，
否则 ruff 会为每个脚本报一次 E402，而这些报错没有信息量。

用法（在脚本最前面）：

    from _bootstrap import setup  # noqa: E402

    setup()
    import lushu.config  # noqa: F401  必须最先导入，引擎会在 import 期读环境变量

`setup()` 之后才能导入 `lushu.*` 或 `third_party.*`。
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path


def setup() -> Path:
    """把项目根加进模块搜索路径、把标准输出切到 UTF-8，返回项目根。

    Windows 控制台默认 GBK，直接 print 中文与勾叉会抛 UnicodeEncodeError，
    探测脚本又必须能看到中文，所以这里统一切换而不是让每个脚本自己 try。
    """
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    # urllib3 在部分代理配置下会刷一大段无关警告，淹没探测输出
    warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
    return root
