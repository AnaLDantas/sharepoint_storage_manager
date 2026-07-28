from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from models import DriveRecord, ItemRecord, SiteRecord

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
    """SQLite local usado como catalogo e checkpoint delta."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Tuning de ingestao para runs longos com milhoes de linhas.
        self.conn.execute("PRAGMA cache_size=-262144")   # ~256 MB de page cache
        self.conn.execute("PRAGMA mmap_size=268435456")  # 256 MB mapeados em memoria
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA wal_autocheckpoint=2000")

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

                CREATE TABLE IF NOT EXISTS drive_sync_state (
                    drive_id TEXT PRIMARY KEY,
                    site_id TEXT NOT NULL,
                    library_name TEXT,
                    mode TEXT NOT NULL DEFAULT 'delta',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_link TEXT,
                    delta_link TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_started_at TEXT,
                    last_completed_at TEXT,
                    FOREIGN KEY(site_id) REFERENCES sites(id),
                    FOREIGN KEY(drive_id) REFERENCES drives(id)
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

                CREATE INDEX IF NOT EXISTS idx_drive_sync_status ON drive_sync_state(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_errors_retry ON errors(resolved, retryable);
                -- Indice parcial minusculo: serve drive_root_path() sem varrer items inteira.
                CREATE INDEX IF NOT EXISTS idx_items_drive_root
                    ON items(drive_id)
                    WHERE parent_id IS NULL AND item_type='folder';
                """
            )

    def create_analytics_indexes(self) -> None:
        """Cria os indices pesados de items usados por export/summary/agregados.

        Sao criados sob demanda (depois do crawl) para nao pagar manutencao de
        indice a cada insert durante a coleta, que e o gargalo de escrita.
        """
        with self.transaction() as conn:
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_items_parent ON items(drive_id, parent_id);
                CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type);
                CREATE INDEX IF NOT EXISTS idx_items_site_drive ON items(site_id, drive_id);
                """
            )

    def wal_checkpoint(self) -> None:
        """Trunca o WAL para evitar que o arquivo cresca indefinidamente no run."""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError as exc:
                LOGGER.debug("wal_checkpoint ignorado: %s", exc)

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
            conn.execute(
                """
                INSERT INTO drive_sync_state(drive_id, site_id, library_name, status, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(drive_id) DO UPDATE SET
                    site_id=excluded.site_id,
                    library_name=excluded.library_name,
                    updated_at=excluded.updated_at
                """,
                (drive.id, drive.site_id, drive.name, utc_now(), utc_now()),
            )

    def upsert_item(self, item: ItemRecord, status: str = "ok") -> None:
        self.upsert_items_batch([item], status=status)

    def _item_values(self, item: ItemRecord, status: str, collected_at: str) -> tuple[Any, ...]:
        full_path = f"{item.parent_path.rstrip('/')}/{item.name}" if item.parent_path else item.name
        return (
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
            collected_at,
        )

    def upsert_items_batch(self, items: list[ItemRecord], status: str = "ok") -> None:
        if not items:
            return
        with self.transaction() as conn:
            collected_at = utc_now()
            conn.executemany(
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
                [self._item_values(item, status, collected_at) for item in items],
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

    def _site_filter(self, site_ids: set[str] | None, column: str = "site_id") -> tuple[str, list[str]]:
        if site_ids is None:
            return "", []
        if not site_ids:
            return " AND 1=0", []
        placeholders = ",".join("?" for _ in site_ids)
        return f" AND {column} IN ({placeholders})", list(site_ids)

    def prepare_delta_sync(self, reset_completed: bool = False, site_ids: set[str] | None = None) -> None:
        with self.transaction() as conn:
            now = utc_now()
            site_filter, site_params = self._site_filter(site_ids, "d.site_id")
            conn.execute(
                f"""
                INSERT INTO drive_sync_state(drive_id, site_id, library_name, status, created_at, updated_at)
                SELECT d.id, d.site_id, d.name, 'pending', ?, ?
                FROM drives d
                WHERE NOT EXISTS (
                    SELECT 1 FROM drive_sync_state s WHERE s.drive_id=d.id
                )
                {site_filter}
                """,
                [now, now, *site_params],
            )
            state_filter, state_params = self._site_filter(site_ids)
            conn.execute(
                f"""
                UPDATE drive_sync_state
                SET status='pending', updated_at=?, last_error=COALESCE(last_error, 'Retomado apos interrupcao')
                WHERE status='in_progress'
                {state_filter}
                """,
                [now, *state_params],
            )
            if reset_completed:
                conn.execute(
                    f"""
                    UPDATE drive_sync_state
                    SET status='pending', updated_at=?, last_error=NULL
                    WHERE status='done'
                    {state_filter}
                    """,
                    [now, *state_params],
                )

    def reset_delta_sync(self, site_ids: set[str] | None = None, clear_items: bool = False) -> None:
        with self.transaction() as conn:
            now = utc_now()
            state_filter, state_params = self._site_filter(site_ids)
            conn.execute(
                f"""
                UPDATE drive_sync_state
                SET status='pending',
                    next_link=NULL,
                    delta_link=NULL,
                    last_error=NULL,
                    updated_at=?
                WHERE 1=1
                {state_filter}
                """,
                [now, *state_params],
            )
            if clear_items:
                item_filter, item_params = self._site_filter(site_ids)

                conn.execute(f"DELETE FROM items WHERE 1=1 {item_filter}", item_params)

    def claim_delta_drive(self, site_ids: set[str] | None = None) -> sqlite3.Row | None:
        with self.transaction() as conn:
            state_filter, state_params = self._site_filter(site_ids)
            row = conn.execute(
                f"""
                SELECT * FROM drive_sync_state
                WHERE status='pending'
                {state_filter}
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                state_params,
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE drive_sync_state
                SET status='in_progress', attempts=attempts+1, updated_at=?, last_started_at=?
                WHERE drive_id=? AND status='pending'
                """,
                (utc_now(), utc_now(), row["drive_id"]),
            )
            return row

    def complete_delta_page(
        self,
        drive_id: str,
        items: list[ItemRecord],
        deleted_item_ids: list[str],
        next_link: str | None,
        delta_link: str | None,
    ) -> None:
        with self.transaction() as conn:
            collected_at = utc_now()
            if items:
                conn.executemany(
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
                    [self._item_values(item, "ok", collected_at) for item in items],
                )
            if deleted_item_ids:
                conn.executemany(
                    """
                    UPDATE items
                    SET status='deleted', collected_at=?, raw_json=COALESCE(raw_json, '{}')
                    WHERE drive_id=? AND id=?
                    """,
                    [(collected_at, drive_id, item_id) for item_id in deleted_item_ids],
                )
            if delta_link:
                conn.execute(
                    """
                    UPDATE drive_sync_state
                    SET status='done', next_link=NULL, delta_link=?, updated_at=?, last_completed_at=?, last_error=NULL
                    WHERE drive_id=?
                    """,
                    (delta_link, collected_at, collected_at, drive_id),
                )
                conn.execute("UPDATE drives SET processed_at=? WHERE id=?", (collected_at, drive_id))
            else:
                conn.execute(
                    """
                    UPDATE drive_sync_state
                    SET status='in_progress', next_link=?, updated_at=?
                    WHERE drive_id=?
                    """,
                    (next_link, collected_at, drive_id),
                )

    def fail_delta_drive(self, drive_id: str, message: str, retryable: bool = True) -> None:
        status = "pending" if retryable else "failed"
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE drive_sync_state
                SET status=?, updated_at=?, last_error=?
                WHERE drive_id=?
                """,
                (status, utc_now(), message[:2000], drive_id),
            )

    def reset_delta_drive(self, drive_id: str, message: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE drive_sync_state
                SET status='pending', next_link=NULL, delta_link=NULL, updated_at=?, last_error=?
                WHERE drive_id=?
                """,
                (utc_now(), message[:2000], drive_id),
            )

    def reset_site_inventory(self, site_id: str) -> dict[str, int]:
        """Remove dados/checkpoints de um site para permitir nova coleta completa."""
        site_id = site_id.strip()
        if not site_id:
            raise ValueError("site_id nao pode ser vazio")

        with self.transaction() as conn:
            drive_rows = conn.execute("SELECT id FROM drives WHERE site_id=?", (site_id,)).fetchall()
            drive_ids = [row["id"] for row in drive_rows]
            now = utc_now()

            counts = {
                "sites": int(conn.execute("SELECT COUNT(*) FROM sites WHERE id=?", (site_id,)).fetchone()[0]),
                "drives": len(drive_ids),
                "items": int(conn.execute("SELECT COUNT(*) FROM items WHERE site_id=?", (site_id,)).fetchone()[0]),
                "drive_sync_state": int(conn.execute("SELECT COUNT(*) FROM drive_sync_state WHERE site_id=?", (site_id,)).fetchone()[0]),
                "errors": int(conn.execute("SELECT COUNT(*) FROM errors WHERE site_id=?", (site_id,)).fetchone()[0]),
            }

            conn.execute("DELETE FROM items WHERE site_id=?", (site_id,))
            conn.execute("DELETE FROM errors WHERE site_id=?", (site_id,))
            conn.execute(
                """
                UPDATE drive_sync_state
                SET status='pending',
                    attempts=0,
                    next_link=NULL,
                    delta_link=NULL,
                    last_error=NULL,
                    updated_at=?,
                    last_started_at=NULL,
                    last_completed_at=NULL
                WHERE site_id=?
                """,
                (now, site_id),
            )
            conn.execute("UPDATE drives SET processed_at=NULL WHERE site_id=?", (site_id,))
            conn.execute("UPDATE sites SET processed_at=NULL WHERE id=?", (site_id,))

            return counts

    def delta_pending_count(self, site_ids: set[str] | None = None) -> int:
        with self._lock:
            state_filter, state_params = self._site_filter(site_ids)
            return int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM drive_sync_state WHERE status='pending' {state_filter}",
                    state_params,
                ).fetchone()[0]
            )

    def delta_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute("SELECT status, COUNT(*) total FROM drive_sync_state GROUP BY status").fetchall()
            return {row["status"]: row["total"] for row in rows}

    def drive_root_path(self, drive_id: str, fallback: str) -> str:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT full_path
                FROM items
                WHERE drive_id=? AND parent_id IS NULL AND item_type='folder' AND status!='deleted'
                ORDER BY collected_at DESC
                LIMIT 1
                """,
                (drive_id,),
            ).fetchone()
            return row["full_path"] if row and row["full_path"] else fallback

    def stats(self, include_inventory_totals: bool = True) -> dict[str, Any]:
        with self._lock:
            if include_inventory_totals:
                row = self.conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM sites) sites,
                        (SELECT COUNT(*) FROM drives) drives,
                        (SELECT COUNT(*) FROM items WHERE item_type='folder' AND status!='deleted') folders,
                        (SELECT COUNT(*) FROM items WHERE item_type='file' AND status!='deleted') files,
                        (SELECT COALESCE(SUM(size_bytes), 0) FROM items WHERE item_type='file' AND status!='deleted') total_bytes,
                        (SELECT COUNT(*) FROM errors WHERE resolved=0 AND COALESCE(status_code, 0) != 423) open_errors,
                        (SELECT COUNT(DISTINCT site_id) FROM errors WHERE resolved=0 AND status_code=423) blocked_sites,
                        (SELECT MAX(updated_at) FROM drive_sync_state) last_delta_checkpoint
                    """
                ).fetchone()
            else:
                row = self.conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM sites) sites,
                        (SELECT COUNT(*) FROM drives) drives,
                        0 folders,
                        0 files,
                        0 total_bytes,
                        (SELECT COUNT(*) FROM errors WHERE resolved=0) open_errors,
                        (SELECT MAX(updated_at) FROM drive_sync_state) last_delta_checkpoint
                    """
                ).fetchone()
            data = dict(row)
            data["total_formatted"] = format_size(data["total_bytes"])
            data["delta"] = self.delta_counts()
            data["last_checkpoint"] = data.get("last_delta_checkpoint")
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
                WHERE item_type='folder' AND status!='deleted'
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
                    WHERE drive_id=? AND parent_id=? AND status!='deleted'
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

