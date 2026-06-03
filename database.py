from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models import DriveRecord, ItemRecord, QueueFolder, SiteRecord

LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if value < 1024 or unit == "PB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} PB"


class InventoryDatabase:
    """SQLite local usado como catalogo e checkpoint.

    Checkpoint/restart:
    - Cada pasta a ler entra em folders_queue como pending.
    - Um worker muda a pasta para in_progress dentro de uma transacao.
    - A pasta so vira done depois que todos os filhos da pagina Graph foram gravados.
    - Se o processo cair, pastas in_progress voltam para pending no proximo resume.
      Como items usa chave unica por drive/item, reler a mesma pasta apenas atualiza registros.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    stats_json TEXT
                );

                CREATE TABLE IF NOT EXISTS sites (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    web_url TEXT,
                    host_name TEXT,
                    raw_json TEXT,
                    discovered_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS drives (
                    id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    name TEXT,
                    drive_type TEXT,
                    web_url TEXT,
                    raw_json TEXT,
                    discovered_at TEXT NOT NULL,
                    processed_at TEXT,
                    FOREIGN KEY(site_id) REFERENCES sites(id)
                );

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT NOT NULL,
                    drive_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    library_name TEXT,
                    parent_id TEXT,
                    parent_path TEXT,
                    full_path TEXT,
                    name TEXT,
                    item_type TEXT NOT NULL,
                    extension TEXT,
                    mime_type TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    formatted_size TEXT,
                    created_at TEXT,
                    modified_at TEXT,
                    last_accessed_at TEXT,
                    file_count INTEGER DEFAULT 0,
                    folder_total_size_bytes INTEGER DEFAULT 0,
                    folder_total_size_formatted TEXT,
                    web_url TEXT,
                    raw_json TEXT,
                    status TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY (drive_id, id),
                    FOREIGN KEY(site_id) REFERENCES sites(id),
                    FOREIGN KEY(drive_id) REFERENCES drives(id)
                );

                CREATE TABLE IF NOT EXISTS folders_queue (
                    drive_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    library_name TEXT,
                    path TEXT NOT NULL,
                    name TEXT,
                    depth INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (drive_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS processing_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    drive_id TEXT,
                    site_id TEXT,
                    operation TEXT NOT NULL,
                    status_code INTEGER,
                    message TEXT,
                    retryable INTEGER NOT NULL DEFAULT 1,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_queue_status ON folders_queue(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_items_parent ON items(drive_id, parent_id);
                CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type);
                CREATE INDEX IF NOT EXISTS idx_items_site_drive ON items(site_id, drive_id);
                CREATE INDEX IF NOT EXISTS idx_errors_retry ON errors(resolved, retryable);
                """
            )

    def start_run(self, command: str) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO runs(command, started_at, status) VALUES (?, ?, 'running')",
                (command, utc_now()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE id=?",
                (utc_now(), status, json.dumps(stats, ensure_ascii=False), run_id),
            )

    def reset_interrupted_work(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE folders_queue
                SET status='pending', updated_at=?, last_error=COALESCE(last_error, 'Retomado apos interrupcao')
                WHERE status='in_progress'
                """,
                (utc_now(),),
            )

    def upsert_site(self, site: SiteRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sites(id, name, web_url, host_name, raw_json, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, web_url=excluded.web_url, host_name=excluded.host_name,
                    raw_json=excluded.raw_json
                """,
                (site.id, site.name, site.web_url, site.host_name, json.dumps(site.raw), utc_now()),
            )

    def mark_site_processed(self, site_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE sites SET processed_at=? WHERE id=?", (utc_now(), site_id))

    def upsert_drive(self, drive: DriveRecord) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO drives(id, site_id, name, drive_type, web_url, raw_json, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    site_id=excluded.site_id, name=excluded.name, drive_type=excluded.drive_type,
                    web_url=excluded.web_url, raw_json=excluded.raw_json
                """,
                (drive.id, drive.site_id, drive.name, drive.drive_type, drive.web_url, json.dumps(drive.raw), utc_now()),
            )

    def mark_drive_processed(self, drive_id: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE drives SET processed_at=? WHERE id=?", (utc_now(), drive_id))

    def enqueue_folder(self, folder: QueueFolder) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO folders_queue(drive_id, item_id, site_id, library_name, path, name, depth, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(drive_id, item_id) DO UPDATE SET
                    site_id=excluded.site_id,
                    library_name=excluded.library_name,
                    path=excluded.path,
                    name=excluded.name,
                    depth=excluded.depth,
                    updated_at=excluded.updated_at
                """,
                (folder.drive_id, folder.item_id, folder.site_id, folder.library_name, folder.path, folder.name, folder.depth, utc_now(), utc_now()),
            )

    def claim_folder(self) -> QueueFolder | None:
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM folders_queue
                WHERE status='pending'
                ORDER BY depth ASC, updated_at ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE folders_queue
                SET status='in_progress', attempts=attempts+1, updated_at=?
                WHERE drive_id=? AND item_id=? AND status='pending'
                """,
                (utc_now(), row["drive_id"], row["item_id"]),
            )
            return QueueFolder(
                drive_id=row["drive_id"],
                item_id=row["item_id"],
                site_id=row["site_id"],
                library_name=row["library_name"],
                path=row["path"],
                name=row["name"],
                depth=row["depth"],
            )

    def complete_folder(self, drive_id: str, item_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE folders_queue SET status='done', updated_at=?, last_error=NULL WHERE drive_id=? AND item_id=?",
                (utc_now(), drive_id, item_id),
            )

    def fail_folder(self, drive_id: str, item_id: str, message: str, retryable: bool = True) -> None:
        status = "pending" if retryable else "failed"
        with self.transaction() as conn:
            conn.execute(
                "UPDATE folders_queue SET status=?, updated_at=?, last_error=? WHERE drive_id=? AND item_id=?",
                (status, utc_now(), message[:2000], drive_id, item_id),
            )

    def upsert_item(self, item: ItemRecord, status: str = "ok") -> None:
        full_path = f"{item.parent_path.rstrip('/')}/{item.name}" if item.parent_path else item.name
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO items(
                    id, drive_id, site_id, library_name, parent_id, parent_path, full_path, name,
                    item_type, extension, mime_type, size_bytes, formatted_size, created_at,
                    modified_at, last_accessed_at, web_url, raw_json, status, collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(drive_id, id) DO UPDATE SET
                    site_id=excluded.site_id,
                    library_name=excluded.library_name,
                    parent_id=excluded.parent_id,
                    parent_path=excluded.parent_path,
                    full_path=excluded.full_path,
                    name=excluded.name,
                    item_type=excluded.item_type,
                    extension=excluded.extension,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    formatted_size=excluded.formatted_size,
                    created_at=excluded.created_at,
                    modified_at=excluded.modified_at,
                    last_accessed_at=excluded.last_accessed_at,
                    web_url=excluded.web_url,
                    raw_json=excluded.raw_json,
                    status=excluded.status,
                    collected_at=excluded.collected_at
                """,
                (
                    item.id,
                    item.drive_id,
                    item.site_id,
                    item.library_name,
                    item.parent_id,
                    item.parent_path,
                    full_path,
                    item.name,
                    item.item_type,
                    item.extension,
                    item.mime_type,
                    item.size_bytes,
                    format_size(item.size_bytes),
                    item.created_at,
                    item.modified_at,
                    item.last_accessed_at,
                    item.web_url,
                    json.dumps(item.raw),
                    status,
                    utc_now(),
                ),
            )

    def record_error(
        self,
        entity_type: str,
        operation: str,
        message: str,
        entity_id: str | None = None,
        drive_id: str | None = None,
        site_id: str | None = None,
        status_code: int | None = None,
        retryable: bool = True,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO errors(entity_type, entity_id, drive_id, site_id, operation, status_code, message, retryable, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entity_type, entity_id, drive_id, site_id, operation, status_code, message[:4000], int(retryable), utc_now(), utc_now()),
            )

    def pending_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM folders_queue WHERE status='pending'").fetchone()[0])

    def queue_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute("SELECT status, COUNT(*) total FROM folders_queue GROUP BY status").fetchall()
            return {row["status"]: row["total"] for row in rows}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sites) sites,
                    (SELECT COUNT(*) FROM drives) drives,
                    (SELECT COUNT(*) FROM items WHERE item_type='folder') folders,
                    (SELECT COUNT(*) FROM items WHERE item_type='file') files,
                    (SELECT COALESCE(SUM(size_bytes), 0) FROM items WHERE item_type='file') total_bytes,
                    (SELECT COUNT(*) FROM errors WHERE resolved=0) open_errors,
                    (SELECT MAX(updated_at) FROM folders_queue) last_checkpoint
                """
            ).fetchone()
            data = dict(row)
            data["total_formatted"] = format_size(data["total_bytes"])
            data["queue"] = self.queue_counts()
            return data

    def recalculate_folder_aggregates(self) -> None:
        """Calcula volume e quantidade de arquivos das pastas sem chamar a API.

        O tamanho total de diretorios e derivado dos filhos ja catalogados. Processamos
        das pastas mais profundas para a raiz, somando arquivos diretos e agregados das
        subpastas. Isso evita recalcular arvores completas repetidamente durante o crawl.
        """
        with self._lock:
            folders = self.conn.execute(
                """
                SELECT drive_id, id, full_path
                FROM items
                WHERE item_type='folder'
                ORDER BY LENGTH(full_path) - LENGTH(REPLACE(full_path, '/', '')) DESC
                """
            ).fetchall()
        for folder in folders:
            with self.transaction() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN item_type='file' THEN 1 ELSE file_count END), 0) file_count,
                        COALESCE(SUM(CASE WHEN item_type='file' THEN size_bytes ELSE folder_total_size_bytes END), 0) total_size
                    FROM items
                    WHERE drive_id=? AND parent_id=?
                    """,
                    (folder["drive_id"], folder["id"]),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE items
                    SET file_count=?, folder_total_size_bytes=?, folder_total_size_formatted=?
                    WHERE drive_id=? AND id=?
                    """,
                    (row["file_count"], row["total_size"], format_size(row["total_size"]), folder["drive_id"], folder["id"]),
                )

    def unresolved_retryable_errors(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM errors WHERE resolved=0 AND retryable=1 ORDER BY created_at ASC"
            ).fetchall()

    def mark_error_resolved(self, error_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE errors SET resolved=1, updated_at=? WHERE id=?", (utc_now(), error_id))
