"""检查 .env.local 里哪些配置已经填好。只报有没有，不打印任何值。"""

from __future__ import annotations

from dotenv import dotenv_values

WATCHED = (
    "AMAP_API_KEY",
    "AMAP_JS_KEY",
    "AMAP_JS_SECURITY_CODE",
    "DEEPSEEK_API_KEY",
    "LLM_PROVIDER",
)


def main() -> None:
    values = dotenv_values(".env.local")
    for name in WATCHED:
        raw = (values.get(name) or "").strip()
        state = f"已设置（{len(raw)} 字符）" if raw else "空"
        print(f"  {name:<24} {state}")


if __name__ == "__main__":
    main()
