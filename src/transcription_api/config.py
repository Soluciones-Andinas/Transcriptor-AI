"""Settings loaded from environment variables.

Source of truth for the env vars: .env.example (in the project root).
Reference docs: wiki/05_modelo_datos.md, wiki/ADR/ADR-008.md, wiki/ADR/ADR-009.md.
"""
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
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
    compute_type: str = Field(default="int8_float16", alias="COMPUTE_TYPE")
    max_upload_mb: int = Field(default=500, alias="MAX_UPLOAD_MB", gt=0)
    max_image_upload_mb: int = Field(default=25, alias="MAX_IMAGE_UPLOAD_MB", gt=0)
    pipeline_timeout_seconds: int = Field(
        default=1800, alias="PIPELINE_TIMEOUT_SECONDS", gt=0
    )
    # Capa 3 — model identifiers consumed by the lifespan loaders. Keep the
    # batch-1 surface minimal: WHISPER_BATCH_SIZE / MAX_AUDIO_DURATION_SECONDS
    # (spec §7) are introduced in later batches when they become load-bearing.
    whisper_model: str = Field(default="large-v3", alias="WHISPER_MODEL")
    whisper_device: str = Field(default="cuda", alias="WHISPER_DEVICE")
    pyannote_model: str = Field(
        default="pyannote/speaker-diarization-3.1", alias="PYANNOTE_MODEL"
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

    # Public base URL (for /auth/me's `mcp_url` field). In Docker compose
    # this is the rig's address; locally it's http://localhost:8000.
    public_base_url: str = Field(
        default="http://localhost:8000", alias="PUBLIC_BASE_URL"
    )

    @field_validator("jwt_secret", mode="after")
    @classmethod
    def _validate_jwt_secret(cls, v: SecretStr) -> SecretStr:
        """Refuse to boot with a JWT_SECRET shorter than 32 chars.

        HS256 with a short secret enables offline brute-force forgery if any
        signed token leaks. Same secret is also used as the salt for the
        oauth_state cookie via itsdangerous, so the same constraint applies.
        """
        raw = v.get_secret_value()
        if len(raw) < 32:
            raise ValueError(
                f"JWT_SECRET must be at least 32 characters (got {len(raw)}). "
                "Generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return v

    @field_validator("oauth_token_enc_key", mode="after")
    @classmethod
    def _validate_oauth_token_enc_key(cls, v: SecretStr) -> SecretStr:
        """Validate OAUTH_TOKEN_ENC_KEY decodes to exactly 32 bytes (AES-256-GCM).

        The crypto module also validates at module-import time, but doing it
        here means a misconfigured deployment fails at the config-load
        boundary with a clear Pydantic error rather than a deep traceback
        later. Empty default is allowed (auth module unused in Capa 1).
        """
        import base64

        raw = v.get_secret_value()
        if not raw:
            return v  # empty allowed for non-auth deployments
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except Exception as exc:
            raise ValueError(
                f"OAUTH_TOKEN_ENC_KEY is not valid urlsafe-base64: {exc}"
            ) from exc
        if len(decoded) != 32:
            raise ValueError(
                f"OAUTH_TOKEN_ENC_KEY must decode to 32 bytes, got {len(decoded)}. "
                "Generate with: python -c 'import secrets, base64; "
                "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'"
            )
        return v

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

    # --- Database URL builder ----------------------------------------------
    # NOT a computed_field: doing so would put the cleartext password into
    # `settings.model_dump()` and `repr(settings)`, leaking via any diagnostic
    # logger or error handler. Construct the URL on demand only when handing
    # it to the engine factory.
    def build_database_url(self) -> str:
        """Return the SQLAlchemy + asyncpg URL.

        Honors `DATABASE_URL` override if set; otherwise composes from
        discrete `POSTGRES_*` settings. The cleartext password is only
        materialized inside this method, never stored as a public attribute.
        """
        if self.database_url_override:
            return self.database_url_override
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
