"""可区分的拒绝原因：每种错误对应一个独立异常类型。"""


class CatalogError(Exception):
    """所有目录错误的基类。"""


class AssetNotFoundError(CatalogError):
    """引用的资产不存在（悬空引用）。"""


class DanglingReferenceError(CatalogError):
    """血缘引用了未登记的资产或列。"""


class CycleDetectedError(CatalogError):
    """登记该血缘会形成环。"""


class DuplicateColumnError(CatalogError):
    """同一资产内出现重复列定义。"""


class ConcurrentUpdateError(CatalogError):
    """乐观并发冲突：客户端持有的版本已过期。"""
