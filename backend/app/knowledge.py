"""兼容层：将历史调用转发到版本化知识库服务。"""

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
