"""资料版本、公共资料权限、任务内记忆及长期记忆的持久化对象。"""

import time

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint

from app.models import Base, uid


class UserScope(Base):
    """权限与记忆改变时递增，阻止在途模型提交过期上下文。"""

    __tablename__ = "user_scopes"
    user_id = Column(ForeignKey("users.id"), primary_key=True)
    revision = Column(Integer, default=0, nullable=False)


class DocumentScope(Base):
    __tablename__ = "document_scopes"
    document_id = Column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    shared = Column(Integer, default=0, nullable=False, index=True)
    active_version_id = Column(String(32))
    revision = Column(Integer, default=1, nullable=False)
    deleted = Column(Integer, default=0, nullable=False)


class SourceSetting(Base):
    __tablename__ = "source_settings"
    __table_args__ = (UniqueConstraint("user_id", "document_id"),)
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    document_id = Column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    enabled = Column(Integer, default=0, nullable=False)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "number"),)
    id = Column(String(32), primary_key=True, default=uid)
    document_id = Column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    number = Column(Integer, nullable=False)
    filename = Column(String(200), nullable=False)
    raw = Column(LargeBinary, nullable=False)
    content = Column(Text, default="", nullable=False)
    content_hash = Column(String(64), nullable=False)
    status = Column(String(24), default="queued", nullable=False)
    mode = Column(String(80), default="pending", nullable=False)
    model_id = Column(String(200), default="", nullable=False)
    dimension = Column(Integer, default=0, nullable=False)
    parser_version = Column(String(40), default="structure-2", nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class KnowledgeSegment(Base):
    __tablename__ = "knowledge_segments"
    id = Column(String(32), primary_key=True, default=uid)
    version_id = Column(ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    heading = Column(Text, default="", nullable=False)
    location = Column(JSON, default=dict, nullable=False)
    search_terms = Column(Text, nullable=False)
    embedding = Column(Vector(512).with_variant(JSON(), "sqlite"))


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    id = Column(String(32), primary_key=True, default=uid)
    key = Column(String(180), unique=True, nullable=False)
    kind = Column(String(24), nullable=False)
    target_id = Column(String(32), nullable=False)
    task_id = Column(ForeignKey("tasks.id"), index=True)
    status = Column(String(24), default="queued", nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    lease_token = Column(String(32))
    lease_until = Column(Float, default=0, nullable=False)
    progress = Column(Integer, default=0, nullable=False)
    error = Column(Text, default="", nullable=False)
    usage = Column(JSON, default=dict, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class TaskMemory(Base):
    __tablename__ = "task_memories"
    task_id = Column(ForeignKey("tasks.id"), primary_key=True)
    summary = Column(JSON, default=dict, nullable=False)
    through_seq = Column(Integer, default=0, nullable=False)
    version = Column(Integer, default=0, nullable=False)
    scope_revision = Column(Integer, default=0, nullable=False)


class HistoryUnit(Base):
    __tablename__ = "history_units"
    __table_args__ = (UniqueConstraint("task_id", "seq"),)
    id = Column(String(32), primary_key=True, default=uid)
    task_id = Column(ForeignKey("tasks.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    kind = Column(String(40), nullable=False)
    content = Column(Text, nullable=False)
    search_terms = Column(Text, nullable=False)
    evidence_ids = Column(JSON, default=list, nullable=False)
    embedding = Column(Vector(512).with_variant(JSON(), "sqlite"))
    model_id = Column(String(200), default="", nullable=False)
    scope_revision = Column(Integer, default=0, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    task_id = Column(ForeignKey("tasks.id"))
    kind = Column(String(40), nullable=False)
    content = Column(Text, nullable=False)
    source = Column(Text, default="用户填写", nullable=False)
    source_seq = Column(Integer, default=0, nullable=False)
    status = Column(String(24), default="pending", nullable=False)
    version = Column(Integer, default=1, nullable=False)
    replaces_id = Column(String(32))
    replaces_version = Column(Integer)
    expires_at = Column(Float)
    created_at = Column(Float, default=time.time, nullable=False)


class UserMemory(Base):
    __tablename__ = "user_memories"
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    content = Column(Text, nullable=False)
    source_candidate_id = Column(String(32), nullable=False)
    status = Column(String(24), default="active", nullable=False)
    version = Column(Integer, default=1, nullable=False)
    expires_at = Column(Float)
    created_at = Column(Float, default=time.time, nullable=False)


class MemoryEvent(Base):
    """审计不复制记忆正文，删除记忆后只保留生命周期信息。"""

    __tablename__ = "memory_events"
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    memory_id = Column(String(32), nullable=False)
    task_id = Column(ForeignKey("tasks.id"))
    action = Column(String(32), nullable=False)
    detail = Column(JSON, default=dict, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)
