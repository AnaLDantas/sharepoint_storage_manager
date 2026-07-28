from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Permite exibir --help antes de instalar requirements.
    load_dotenv = None


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "sim"}


@dataclass(frozen=True)
class Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    max_workers: int
    request_timeout: int
    sqlite_db_path: Path
    export_path: Path
    log_level: str
    enable_user_onedrive: bool
    site_search_query: str
    site_ids_file: Path | None
    site_ids: tuple[str, ...]
    progress_log_interval_seconds: int
    user_agent: str
    rate_limit_min_remaining: int
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    authority_host: str = "https://login.microsoftonline.com"

    @property
    def authority(self) -> str:
        return f"{self.authority_host}/{self.tenant_id}"


def load_settings() -> Settings:
    if load_dotenv:
        load_dotenv()
    site_ids_file_value = os.environ.get("SITE_IDS_FILE", "").strip()
    site_ids_value = os.environ.get("SITE_IDS", "").strip()
    settings = Settings(
        tenant_id=os.environ.get("TENANT_ID", "").strip(),
        client_id=os.environ.get("CLIENT_ID", "").strip(),
        client_secret=os.environ.get("CLIENT_SECRET", "").strip(),
        max_workers=max(1, int(os.environ.get("MAX_WORKERS", "4"))),
        request_timeout=max(5, int(os.environ.get("REQUEST_TIMEOUT", "60"))),
        sqlite_db_path=Path(os.environ.get("SQLITE_DB_PATH", "./sharepoint_inventory.sqlite3")),
        export_path=Path(os.environ.get("EXPORT_PATH", "./exports")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        enable_user_onedrive=_bool(os.environ.get("ENABLE_USER_ONEDRIVE"), False),
        site_search_query=os.environ.get("SITE_SEARCH_QUERY", "*"),
        site_ids_file=Path(site_ids_file_value) if site_ids_file_value else None,
        site_ids=tuple(item.strip() for item in site_ids_value.split(",") if item.strip()),
        progress_log_interval_seconds=max(10, int(os.environ.get("PROGRESS_LOG_INTERVAL_SECONDS", "60"))),
        user_agent=(
            os.environ.get("GRAPH_USER_AGENT", "").strip()
            or "NONISV|SharePointStorageManager|Inventory/1.0"
        ),
        rate_limit_min_remaining=max(0, int(os.environ.get("RATE_LIMIT_MIN_REMAINING", "20"))),
    )
    missing = [
        name
        for name, value in {
            "TENANT_ID": settings.tenant_id,
            "CLIENT_ID": settings.client_id,
            "CLIENT_SECRET": settings.client_secret,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Variaveis obrigatorias ausentes no .env: {', '.join(missing)}")
    return settings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
