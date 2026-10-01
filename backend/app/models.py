import time
import uuid

from sqlalchemy import JSON, Column, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase


def uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(String(32), primary_key=True, default=uid)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    is_admin = Column(Integer, default=0, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class Session(Base):
    __tablename__ = "sessions"
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    expires_at = Column(Float, nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    goal = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="queued", index=True)
    state = Column(JSON, nullable=False, default=dict)
    revision = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(32), nullable=True)
    lease_until = Column(Float, nullable=False, default=0)
    created_at = Column(Float, nullable=False, default=time.time)
    updated_at = Column(Float, nullable=False, default=time.time)


class Run(Base):
    __tablename__ = "runs"
    id = Column(String(32), primary_key=True, default=uid)
    task_id = Column(ForeignKey("tasks.id"), nullable=False, index=True)
    started_at = Column(Float, nullable=False, default=time.time)
    ended_at = Column(Float)
    status = Column(String(24), nullable=False, default="running")
    configuration = Column(JSON, nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "seq"),)
    id = Column(String(32), primary_key=True, default=uid)
    task_id = Column(ForeignKey("tasks.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    kind = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class Evidence(Base):
    __tablename__ = "evidence"
    id = Column(String(40), primary_key=True)
    task_id = Column(ForeignKey("tasks.id"), nullable=False, index=True)
    tool = Column(String(80), nullable=False)
    arguments = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False)
    created_at = Column(Float, default=time.time, nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    id = Column(String(40), primary_key=True)
    task_id = Column(ForeignKey("tasks.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    format = Column(String(16), nullable=False, default="markdown")
    content = Column(Text, nullable=False)
    evidence_ids = Column(JSON, nullable=False, default=list)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, default=time.time, nullable=False)


class Document(Base):
    __tablename__ = "documents"
    id = Column(String(32), primary_key=True, default=uid)
    user_id = Column(ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    enabled = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(Float, default=time.time, nullable=False)


# 扩展表复用同一份元数据，兼容既有数据库的增量迁移。
from app import extension_models as extension_models  # noqa: E402  扩展表必须在基础元数据定义后加载。
