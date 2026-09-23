"""本地元数据目录服务入口。

启动：uv run uvicorn main:app --reload
"""

from metadata_catalog.api import app

__all__ = ["app"]
