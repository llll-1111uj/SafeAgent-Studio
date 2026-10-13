#!/usr/bin/env python3
"""打印 FastAPI 的版本号。

优先读取沙箱内已安装的 fastapi 包版本；若未安装（沙箱断网、无 fastapi），
则回退到脚本顶部硬编码的常量 FASTAPI_LATEST_VERSION。
"""

# 由 web_search 结果填充；若搜索未能给出确切版本，保持为 "unknown"。
FASTAPI_LATEST_VERSION = "unknown"


def main() -> None:
    try:
        import fastapi  # type: ignore

        version = getattr(fastapi, "__version__", "unknown")
        print(f"沙箱内已安装的 FastAPI 版本: {version}")
    except ModuleNotFoundError:
        print("沙箱内未安装 fastapi 包（断网，无法 pip install）。")
        print(f"记录的最新版本号: {FASTAPI_LATEST_VERSION}")


if __name__ == "__main__":
    main()
