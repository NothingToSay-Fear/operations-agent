"""环境配置及其中央化校验。"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT.parent / ".env", ROOT / ".env"), extra="ignore")

    app_database_url: str = f"sqlite+aiosqlite:///{(DATA / 'app.db').as_posix()}"
    commerce_database_url: str = (
        f"sqlite+aiosqlite:///file:{(DATA / 'commerce.db').as_posix()}?mode=ro&uri=true"
    )
    commerce_admin_url: str = f"sqlite+aiosqlite:///{(DATA / 'commerce.db').as_posix()}"
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_timeout: int = 90
    llm_temperature: float = 0.2
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    admin_username: str = "admin"
    admin_password: str = "adminadmin"
    session_days: int = 7
    cookie_secure: bool = False
    worker_poll_seconds: float = 1
    lease_seconds: int = 120
    max_model_calls: int = 30
    max_tool_calls: int = 30
    max_replans: int = 5
    max_active_seconds: int = 300
    max_step_tools: int = 6
    max_subtasks: int = 3
    subtask_max_model_calls: int = 4
    subtask_max_tool_calls: int = 3
    subtask_context_char_budget: int = 8000
    main_context_observation_limit: int = 10
    main_context_observation_char_budget: int = 7000
    embedding_model_path: str = ""
    reranker_model_path: str = ""
    embedding_model_id: str = "BAAI/bge-small-zh-v1.5"
    reranker_model_id: str = "BAAI/bge-reranker-base"
    retrieval_min_score: float = 0.35
    query_expansion_limit: int = 2
    memory_context_char_budget: int = 24000
    memory_compact_threshold: int = 18000
    memory_recent_turn_limit: int = 12
    memory_turn_char_limit: int = 2000
    maintenance_max_model_calls: int = 6
    maintenance_max_seconds: int = 180
    background_lease_seconds: int = 180
    background_max_attempts: int = 3
    background_in_api: bool = True
    seed_days: int = 180
    seed_skus: int = 200
    seed_orders: int = 50000
    seed_scenario: str = "mixed"
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    @field_validator("embedding_model_path", "reranker_model_path")
    @classmethod
    def local_model_path(cls, value):
        if not value:
            return ""
        path = Path(value)
        return str(path if path.is_absolute() else (ROOT / path).resolve())

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_model and (self.llm_api_key or self.llm_provider == "ollama"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
