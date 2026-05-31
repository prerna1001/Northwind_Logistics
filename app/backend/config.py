from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
PROJECT_ROOT = APP_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Northwind Expense Review API"
    postgres_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/northwind_expense"
    raw_bucket_name: str = "northwind-demo-bucket"
    raw_bucket_prefix: str = ""
    r2_region: str = "auto"
    r2_endpoint_url: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    embedding_dimensions: int = 64
    llama_api_url: str | None = None
    llama_api_token: str | None = None
    cloudflare_account_id: str | None = None
    llama_model: str = "@cf/meta/llama-3.1-8b-instruct"
    llama_timeout_seconds: float = 20.0
    receipt_parser_version: str = "v2"
    local_reviewer_name: str = "Finance Reviewer"

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def raw_store_dir(self) -> Path:
        return PROJECT_ROOT / "raw_store"

    @property
    def uploads_dir(self) -> Path:
        return PROJECT_ROOT / "uploads"

    @property
    def policies_dir(self) -> Path:
        repo_local = PROJECT_ROOT / "policies"
        if repo_local.exists():
            return repo_local
        return WORKSPACE_ROOT / "policies"

    @property
    def sample_submissions_dir(self) -> Path:
        repo_local = PROJECT_ROOT / "submissions"
        if repo_local.exists():
            return repo_local
        return WORKSPACE_ROOT / "submissions"

    @property
    def r2_enabled(self) -> bool:
        return bool(
            self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_endpoint_url
        )

    @property
    def storage_backend_label(self) -> str:
        endpoint = (self.r2_endpoint_url or "").lower()
        if "cloudflarestorage.com" in endpoint:
            return "cloudflare_r2"
        if self.r2_enabled:
            return "s3"
        return "local_s3_mirror"

    @property
    def resolved_llama_api_url(self) -> str | None:
        if self.llama_api_url:
            return self.llama_api_url
        if self.cloudflare_account_id:
            return f"https://api.cloudflare.com/client/v4/accounts/{self.cloudflare_account_id}/ai/run/{self.llama_model}"
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
