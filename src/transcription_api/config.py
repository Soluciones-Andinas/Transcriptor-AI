"""Settings loaded from environment variables.

See wiki/05_modelo_datos.md and .env.example for the full list of variables.
"""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    hf_token: str = Field(default="", alias="HF_TOKEN")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    default_language: str = Field(default="es", alias="DEFAULT_LANGUAGE")
    compute_type: str = Field(default="float16", alias="COMPUTE_TYPE")
    max_upload_mb: int = Field(default=500, alias="MAX_UPLOAD_MB", gt=0)
    pipeline_timeout_seconds: int = Field(
        default=1800, alias="PIPELINE_TIMEOUT_SECONDS", gt=0
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cache_ttl_seconds: int = Field(default=86400, alias="CACHE_TTL_SECONDS", gt=0)
    cache_cleanup_interval_seconds: int = Field(
        default=3600, alias="CACHE_CLEANUP_INTERVAL_SECONDS", gt=0
    )

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


settings = Settings()
