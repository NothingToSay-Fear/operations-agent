"""资料服务兼容入口，具体实现集中在版本化检索模块。"""

from app.knowledge_service import (
    parse_document,
    index_document,
    search,
    read,
    enqueue_version,
    prepare_index,
    publish_index,
)

__all__ = [
    "parse_document",
    "index_document",
    "search",
    "read",
    "enqueue_version",
    "prepare_index",
    "publish_index",
]
