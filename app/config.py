from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = (str(PROJECT_ROOT / ".env"), str(PROJECT_ROOT / "app" / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    azure_subscription_id: str = Field(validation_alias="AZURE_SUBSCRIPTION_ID", min_length=1)
    azure_auth_mode: str = Field(default="cli", validation_alias="AZURE_AUTH_MODE")
    azure_tenant_id: str | None = Field(default=None, validation_alias="AZURE_TENANT_ID")
    azure_client_id: str | None = Field(default=None, validation_alias="AZURE_CLIENT_ID")
    azure_client_secret: str | None = Field(default=None, validation_alias="AZURE_CLIENT_SECRET")
    azure_default_resource_group: str | None = Field(default=None, validation_alias="AZURE_DEFAULT_RESOURCE_GROUP")
    azure_openai_endpoint: str | None = Field(default=None, validation_alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: str | None = Field(default=None, validation_alias="AZURE_OPENAI_API_KEY")
    azure_openai_api_version: str = Field(default="preview", validation_alias="AZURE_OPENAI_API_VERSION")
    azure_openai_deployment: str | None = Field(default=None, validation_alias="AZURE_OPENAI_DEPLOYMENT")
    azure_openai_advice_deployment: str | None = Field(default=None, validation_alias="AZURE_OPENAI_ADVICE_DEPLOYMENT")
    feishu_app_id: str | None = Field(default=None, validation_alias="FEISHU_APP_ID")
    feishu_app_secret: str | None = Field(default=None, validation_alias="FEISHU_APP_SECRET")
    feishu_base_url: str = Field(default="https://open.feishu.cn", validation_alias="FEISHU_BASE_URL")
    feishu_ack_reaction: str = Field(default="Get", validation_alias="FEISHU_ACK_REACTION")
    app_log_level: str = Field(default="INFO", validation_alias="APP_LOG_LEVEL")
    session_idle_ttl_sec: int = Field(default=1200, validation_alias="SESSION_IDLE_TTL_SEC")
    session_max_turns: int = Field(default=6, validation_alias="SESSION_MAX_TURNS")

    @property
    def sub_id_tail(self) -> str:
        """只暴露订阅 ID 末 6 位，避免在 /health 等接口泄露完整 ID。"""
        sid = (self.azure_subscription_id or "").strip()
        return sid[-6:] if sid else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        settings = Settings()
        return settings
    except ValidationError as exc:
        raise

