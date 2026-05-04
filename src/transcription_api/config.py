"""Settings loaded from environment variables.

Source of truth for the env vars: .env.example (in the project root).
Reference docs: wiki/05_modelo_datos.md, wiki/ADR/ADR-008.md, wiki/ADR/ADR-009.md.
"""
from pathlib import Path

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Pipeline IA -------------------------------------------------------
    hf_token: SecretStr = Field(default=SecretStr(""), alias="HF_TOKEN")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    default_language: str = Field(default="es", alias="DEFAULT_LANGUAGE")
    compute_type: str = Field(default="float16", alias="COMPUTE_TYPE")
    max_upload_mb: int = Field(default=500, alias="MAX_UPLOAD_MB", gt=0)
    max_image_upload_mb: int = Field(default=25, alias="MAX_IMAGE_UPLOAD_MB", gt=0)
    pipeline_timeout_seconds: int = Field(
        default=1800, alias="PIPELINE_TIMEOUT_SECONDS", gt=0
    )

    # --- Persistencia (Postgres) -------------------------------------------
    postgres_user: str = Field(default="transcription", alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(
        default=SecretStr("change-me"), alias="POSTGRES_PASSWORD"
    )
    postgres_db: str = Field(default="transcription_api", alias="POSTGRES_DB")
    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT", gt=0)
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE", gt=0)
    db_pool_max_overflow: int = Field(default=5, alias="DB_POOL_MAX_OVERFLOW", ge=0)
    database_url_override: str | None = Field(default=None, alias="DATABASE_URL")

    # --- Auth (Microsoft Entra) --------------------------------------------
    ms_tenant_id: str = Field(default="", alias="MS_TENANT_ID")
    ms_client_id: str = Field(default="", alias="MS_CLIENT_ID")
    ms_client_secret: SecretStr = Field(
        default=SecretStr(""), alias="MS_CLIENT_SECRET"
    )
    ms_redirect_uri: str = Field(
        default="http://localhost:8000/auth/callback", alias="MS_REDIRECT_URI"
    )
    oauth_token_enc_key: SecretStr = Field(
        default=SecretStr(""), alias="OAUTH_TOKEN_ENC_KEY"
    )
    jwt_secret: SecretStr = Field(default=SecretStr(""), alias="JWT_SECRET")
    session_ttl_seconds: int = Field(
        default=86400, alias="SESSION_TTL_SECONDS", gt=0
    )

    # --- Caché filesystem y cleanup ----------------------------------------
    cache_ttl_seconds: int = Field(default=86400, alias="CACHE_TTL_SECONDS", gt=0)
    cache_cleanup_interval_seconds: int = Field(
        default=3600, alias="CACHE_CLEANUP_INTERVAL_SECONDS", gt=0
    )
    upload_session_grace_seconds: int = Field(
        default=300, alias="UPLOAD_SESSION_GRACE_SECONDS", ge=0
    )

    # --- Concurrencia (ADR-005) --------------------------------------------
    lock_wait_seconds: float = Field(default=5.0, alias="LOCK_WAIT_SECONDS", gt=0)
    lock_retry_after_seconds: int = Field(
        default=600, alias="LOCK_RETRY_AFTER_SECONDS", gt=0
    )

    # --- Observabilidad -----------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- Computed paths -----------------------------------------------------
    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    # --- Computed database URL ---------------------------------------------
    @computed_field
    @property
    def database_url(self) -> str:
        """SQLAlchemy + asyncpg URL.

        Uses DATABASE_URL env var if explicitly set; otherwise composes one
        from the discrete POSTGRES_* env vars.
        """
        if self.database_url_override:
            return self.database_url_override
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
