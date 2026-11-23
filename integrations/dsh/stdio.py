"""通过 DSH stdio 入口启动 Atlas 工具。"""

import os
import runpy


if __name__ == "__main__":
    # Node 会在创建子进程时追加模型存储的写保护参数。Playwright 驱动沿用启动前的选项。
    if os.environ.pop("ATLAS_RESTORE_DRIVER_OPTIONS", None) == "1":
        inherited = os.environ.pop("ATLAS_ORIGINAL_NODE_OPTIONS", None)
        if inherited is None:
            os.environ.pop("NODE_OPTIONS", None)
        else:
            os.environ["NODE_OPTIONS"] = inherited
    runpy.run_module("webtest_atlas", run_name="__main__")
