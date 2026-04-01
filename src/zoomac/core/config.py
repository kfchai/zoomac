"""Zoomac configuration via environment variables and settings files."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class ZoomacSettings(BaseSettings):
    """Global settings — loaded from env vars (ZOOMAC_ prefix) or config file."""

    model_config = {"env_prefix": "ZOOMAC_"}

    # LLM
    model: str = Field(default="claude-sonnet-4-20250514", description="Anthropic model identifier")

    # Paths
    project_dir: Path = Field(default_factory=lambda: Path.cwd(), description="Project root directory")
    memgate_db: Path = Field(default=Path(".memgate.db"), description="MemGate database path (relative to project_dir)")

    # Memory
    memory_max_tokens: int = Field(default=2000, description="Max tokens for memory context injection")
    memory_top_k: int = Field(default=10, description="Number of memories to retrieve per query")

    # Autonomy
    autonomy_config: Path = Field(default=Path("config/autonomy.yaml"), description="Autonomy rules config path")

    # Agent
    max_sub_agents: int = Field(default=5, description="Maximum concurrent sub-agents")
    confidence_threshold: float = Field(default=0.7, description="Below this, trigger verification")

    # Security
    secret_key: str | None = Field(default=None, description="Encryption key for credential vault")

    # WebSocket server
    ws_host: str = Field(default="0.0.0.0", description="WebSocket server bind address")
    ws_port: int = Field(default=8765, description="WebSocket server port")

    @property
    def memgate_db_path(self) -> Path:
        if self.memgate_db.is_absolute():
            return self.memgate_db
        return self.project_dir / self.memgate_db


def load_settings() -> ZoomacSettings:
    return ZoomacSettings()
