from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRICEWATCH_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    database_url: str | None = None
    scheduler_enabled: bool = True
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'pricewatch.db'}"
