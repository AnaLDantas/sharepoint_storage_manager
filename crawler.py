from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import PurePosixPath
from typing import Any

from database import InventoryDatabase
from graph_client import GraphClient, GraphError
from models import DriveRecord, ItemRecord, QueueFolder, SiteRecord

LOGGER = logging.getLogger(__name__)


def _site_from_graph(row: dict[str, Any]) -> SiteRecord:
    return SiteRecord(
        id=row["id"],
        name=row.get("displayName") or row.get("name") or row["id"],
        web_url=row.get("webUrl", ""),
        host_name=(row.get("siteCollection") or {}).get("hostname", ""),
        raw=row,
    )


def _drive_from_graph(site_id: str, row: dict[str, Any]) -> DriveRecord:
    return DriveRecord(
        id=row["id"],
        site_id=site_id,
        name=row.get("name") or row["id"],
        drive_type=row.get("driveType"),
        web_url=row.get("webUrl"),
        raw=row,
    )


def _extension(name: str, item_type: str) -> str | None:
    if item_type != "file":
        return None
    suffix = PurePosixPath(name).suffix
    return suffix.lower().lstrip(".") if suffix else None


def _item_from_graph(
    site_id: str,
    drive_id: str,
    library_name: str,
    parent_id: str | None,
    parent_path: str,
    row: dict[str, Any],
) -> ItemRecord:
    item_type = "folder" if "folder" in row or "package" in row else "file"
    file_info = row.get("file") or {}
    return ItemRecord(
        id=row["id"],
        drive_id=drive_id,
        site_id=site_id,
        library_name=library_name,
        parent_id=parent_id,
        parent_path=parent_path,
        name=row.get("name") or row["id"],
        item_type=item_type,
        extension=_extension(row.get("name") or "", item_type),
        mime_type=file_info.get("mimeType"),
        size_bytes=int(row.get("size") or 0),
        created_at=row.get("createdDateTime"),
        modified_at=row.get("lastModifiedDateTime"),
        last_accessed_at=row.get("lastAccessedDateTime"),
        web_url=row.get("webUrl"),
        raw=row,
    )


class InventoryCrawler:
    """Orquestra descoberta de sites/drives e consumo da fila de pastas."""

    def __init__(
        self,
        graph: GraphClient,
        db: InventoryDatabase,
        max_workers: int,
        progress_log_interval_seconds: int = 60,
    ):
        self.graph = graph
        self.db = db
        self.max_workers = max(1, max_workers)
        self.progress_log_interval_seconds = max(10, progress_log_interval_seconds)
        self._stop = threading.Event()

    def discover_sharepoint_by_site_ids(self, site_ids: list[str]) -> None:
        """Descobre sites especificos por ID, preservando a ordem recebida.

        O report de uso pode trazer apenas o GUID do SharePoint, enquanto o Graph
        geralmente usa um ID composto. Por isso tentamos acesso direto primeiro e,
        se necessario, criamos um de/para com /sites?search=*.
        """
        site_map: dict[str, dict[str, Any]] | None = None
        for site_id in site_ids:
            try:
                site_row = self.graph.get_site(site_id)
            except GraphError as exc:
                if exc.status_code != 404:
                    self.db.record_error("site", "get_site", exc.message, site_id, site_id=site_id, status_code=exc.status_code, retryable=exc.status_code not in {401, 403, 404})
                    LOGGER.exception("Erro ao buscar site por ID %s", site_id)
                    continue
                if site_map is None:
                    site_map = self._build_site_id_map()
                site_row = site_map.get(site_id.lower())
                if not site_row:
                    self.db.record_error("site", "resolve_site_id", f"Site ID nao encontrado no Graph: {site_id}", site_id, site_id=site_id, status_code=404, retryable=False)
                    LOGGER.warning("Site ID nao encontrado no Graph: %s", site_id)
                    continue
            self._discover_single_site(site_row)

    def discover_sharepoint(self, search_query: str = "*") -> None:
        for site_row in self.graph.list_sites(search_query):
            self._discover_single_site(site_row)

    def _discover_single_site(self, site_row: dict[str, Any]) -> None:
        site = _site_from_graph(site_row)
        self.db.upsert_site(site)
        LOGGER.info("Site descoberto: %s", site.web_url or site.id)
        try:
            for drive_row in self.graph.list_site_drives(site.id):
                drive = _drive_from_graph(site.id, drive_row)
                self.db.upsert_drive(drive)
                self._enqueue_drive_root(site, drive)
            self.db.mark_site_processed(site.id)
        except GraphError as exc:
            self.db.record_error("site", "list_site_drives", exc.message, site.id, site_id=site.id, status_code=exc.status_code, retryable=exc.status_code != 403)
            LOGGER.exception("Erro ao listar drives do site %s", site.id)

    def _build_site_id_map(self) -> dict[str, dict[str, Any]]:
        LOGGER.info("Criando de/para de Site ID via /sites?search=*")
        site_map: dict[str, dict[str, Any]] = {}
        for site_row in self.graph.list_sites("*"):
            graph_id = site_row.get("id") or ""
            if graph_id:
                site_map[graph_id.lower()] = site_row
                for part in graph_id.split(","):
                    if part:
                        site_map[part.lower()] = site_row
            sharepoint_ids = site_row.get("sharepointIds") or {}
            for key in ("siteId", "siteCollectionId", "webId"):
                value = sharepoint_ids.get(key)
                if value:
                    site_map[str(value).lower()] = site_row
        return site_map

    def discover_user_onedrives(self) -> None:
        for user in self.graph.list_users():
            site = SiteRecord(
                id=f"user:{user['id']}",
                name=user.get("displayName") or user.get("userPrincipalName") or user["id"],
                web_url=user.get("userPrincipalName", ""),
                host_name="OneDrive for Business",
                raw=user,
            )
            self.db.upsert_site(site)
            try:
                drive_row = self.graph.get_user_drive(user["id"])
                drive = _drive_from_graph(site.id, drive_row)
                self.db.upsert_drive(drive)
                self._enqueue_drive_root(site, drive)
                self.db.mark_site_processed(site.id)
            except GraphError as exc:
                self.db.record_error("user", "get_user_drive", exc.message, user["id"], site_id=site.id, status_code=exc.status_code, retryable=exc.status_code not in {403, 404})

    def _enqueue_drive_root(self, site: SiteRecord, drive: DriveRecord) -> None:
        try:
            root = self.graph.get_drive_root(drive.id)
            root_name = root.get("name") or "root"
            root_item = _item_from_graph(site.id, drive.id, drive.name, None, "", root)
            self.db.upsert_item(root_item)
            self.db.enqueue_folder(
                QueueFolder(
                    drive_id=drive.id,
                    item_id=root["id"],
                    site_id=site.id,
                    library_name=drive.name,
                    path=root_name,
                    name=root_name,
                    depth=0,
                )
            )
            LOGGER.info("Drive enfileirado: %s / %s", site.name, drive.name)
        except GraphError as exc:
            self.db.record_error("drive", "get_drive_root", exc.message, drive.id, drive_id=drive.id, site_id=site.id, status_code=exc.status_code, retryable=exc.status_code != 403)

    def process_queue(self) -> None:
        """Consome folders_queue com concorrencia limitada.

        A fila de pastas e a unidade de checkpoint: cada pasta e listada pagina a
        pagina via @odata.nextLink no GraphClient, seus filhos sao salvos e novas
        subpastas entram na fila. O limite de workers evita chamadas paralelas demais.
        """
        started_at = time.monotonic()
        last_progress_at = started_at
        last_files = 0
        last_folders = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = set()
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_progress_at >= self.progress_log_interval_seconds:
                    last_files, last_folders = self._log_progress(started_at, last_progress_at, last_files, last_folders)
                    last_progress_at = now
                while len(futures) < self.max_workers:
                    folder = self.db.claim_folder()
                    if not folder:
                        break
                    futures.add(executor.submit(self._process_folder, folder))
                if not futures:
                    if self.db.pending_count() == 0:
                        break
                    time.sleep(1)
                    continue
                done, futures = wait(futures, timeout=2, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
        self._log_progress(started_at, last_progress_at, last_files, last_folders, final=True)

    def _log_progress(
        self,
        started_at: float,
        previous_log_at: float,
        previous_files: int,
        previous_folders: int,
        final: bool = False,
    ) -> tuple[int, int]:
        stats = self.db.stats()
        now = time.monotonic()
        elapsed_minutes = max((now - started_at) / 60, 0.001)
        interval_minutes = max((now - previous_log_at) / 60, 0.001)
        files = int(stats.get("files") or 0)
        folders = int(stats.get("folders") or 0)
        queue = stats.get("queue") or {}
        files_per_minute = (files - previous_files) / interval_minutes
        folders_per_minute = (folders - previous_folders) / interval_minutes
        LOGGER.info(
            "%sprogresso: sites=%s drives=%s arquivos=%s pastas=%s pendentes=%s em_andamento=%s concluidas=%s "
            "volume=%s erros=%s checkpoint=%s tempo=%.1fmin arquivos/min=%.1f pastas/min=%.1f 429=%s retries=%s",
            "final " if final else "",
            stats.get("sites"),
            stats.get("drives"),
            files,
            folders,
            queue.get("pending", 0),
            queue.get("in_progress", 0),
            queue.get("done", 0),
            stats.get("total_formatted"),
            stats.get("open_errors"),
            stats.get("last_checkpoint"),
            elapsed_minutes,
            files_per_minute,
            folders_per_minute,
            self.graph.throttle_count,
            self.graph.retry_count,
        )
        return files, folders

    def _process_folder(self, folder: QueueFolder) -> None:
        LOGGER.info("Lendo pasta: drive=%s path=%s", folder.drive_id, folder.path)
        try:
            child_count = 0
            for child in self.graph.list_children(folder.drive_id, folder.item_id):
                item = _item_from_graph(
                    site_id=folder.site_id,
                    drive_id=folder.drive_id,
                    library_name=folder.library_name,
                    parent_id=folder.item_id,
                    parent_path=folder.path,
                    row=child,
                )
                self.db.upsert_item(item)
                child_count += 1
                if item.item_type == "folder":
                    full_path = f"{folder.path.rstrip('/')}/{item.name}"
                    self.db.enqueue_folder(
                        QueueFolder(
                            drive_id=folder.drive_id,
                            item_id=item.id,
                            site_id=folder.site_id,
                            library_name=folder.library_name,
                            path=full_path,
                            name=item.name,
                            depth=folder.depth + 1,
                        )
                    )
            self.db.complete_folder(folder.drive_id, folder.item_id)
            LOGGER.info("Pasta concluida: %s (%s filhos)", folder.path, child_count)
        except GraphError as exc:
            retryable = exc.status_code not in {401, 403, 404}
            self.db.fail_folder(folder.drive_id, folder.item_id, exc.message, retryable=retryable)
            self.db.record_error(
                "folder",
                "list_children",
                exc.message,
                entity_id=folder.item_id,
                drive_id=folder.drive_id,
                site_id=folder.site_id,
                status_code=exc.status_code,
                retryable=retryable,
            )
            LOGGER.exception("Erro lendo pasta %s", folder.path)
        except Exception as exc:
            self.db.fail_folder(folder.drive_id, folder.item_id, str(exc), retryable=True)
            self.db.record_error("folder", "list_children", str(exc), folder.item_id, folder.drive_id, folder.site_id, retryable=True)
            LOGGER.exception("Falha inesperada lendo pasta %s", folder.path)

    def retry_errors(self) -> None:
        for error in self.db.unresolved_retryable_errors():
            if error["entity_type"] == "folder" and error["drive_id"] and error["entity_id"]:
                self.db.fail_folder(error["drive_id"], error["entity_id"], "Reenfileirado por retry-errors", retryable=True)
                self.db.mark_error_resolved(error["id"])
        self.process_queue()


def install_signal_handlers(crawler: InventoryCrawler) -> None:
    # Em Windows/servicos alguns sinais podem nao existir; signal e importado sob demanda.
    import signal

    def stop(_signum: int, _frame: object) -> None:
        LOGGER.warning("Interrupcao recebida. Workers atuais terminarao a pasta em andamento.")
        crawler._stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
