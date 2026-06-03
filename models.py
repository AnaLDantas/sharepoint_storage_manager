from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SiteRecord:
    id: str
    name: str
    web_url: str
    host_name: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class DriveRecord:
    id: str
    site_id: str
    name: str
    drive_type: Optional[str]
    web_url: Optional[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class QueueFolder:
    drive_id: str
    item_id: str
    site_id: str
    library_name: str
    path: str
    name: str
    depth: int


@dataclass(frozen=True)
class ItemRecord:
    id: str
    drive_id: str
    site_id: str
    library_name: str
    parent_id: Optional[str]
    parent_path: str
    name: str
    item_type: str
    extension: Optional[str]
    mime_type: Optional[str]
    size_bytes: int
    created_at: Optional[str]
    modified_at: Optional[str]
    last_accessed_at: Optional[str]
    web_url: Optional[str]
    raw: dict[str, Any]
