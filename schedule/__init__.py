"""schedule 子包：纯函数时段判断 + 留言落库。

注意：本包目录名沿用需求里的 ``schedule``。为避免遮蔽 PyPI 上同名的
``schedule`` 第三方库，``main.py`` 优先使用相对导入（插件以
``data.plugins.<name>.main`` 包形式加载），只有相对导入失败时才把插件
目录追加到 ``sys.path`` 末尾做兜底。
"""

from __future__ import annotations

__all__ = ["clock", "store"]
