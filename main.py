from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from config import _bool, configure_logging, load_settings

LOGGER = logging.getLogger(__name__)


def _has_open_failures(stats: dict) -> bool:
    queue = stats.get("queue") or {}
    delta = stats.get("delta") or {}
    return bool(
        stats.get("open_errors")
        or queue.get("failed")
        or queue.get("in_progress")
        or delta.get("failed")
        or delta.get("in_progress")
    )


def _load_lines(path) -> list[str]:
    if not path:
        return []
    if not path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventario Microsoft 365 SharePoint/OneDrive via Microsoft Graph")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("crawl", help="Descobre sites/drives e sincroniza via delta com checkpoint por pagina")
    sub.add_parser("resume", help="Continua sincronizacao delta usando nextLink/deltaLink salvos")
    reset_site = sub.add_parser("reset-site", help="Apaga dados/checkpoints locais de um ou mais sites para recoletar")
    reset_site.add_argument("--site-id", action="append", default=[], help="Site ID a resetar. Pode ser usado mais de uma vez")
    reset_site.add_argument("--site-ids-file", help="Arquivo com um Site ID por linha")
    export = sub.add_parser("export", help="Exporta dataset Parquet particionado e Lakehouse")
    export.add_argument("--parquet-rows-per-file", type=int, default=1_000_000, help="Linhas por arquivo no dataset Parquet particionado")
    export.add_argument("--recalculate-folders", action="store_true", help="Recalcula agregados de pastas antes da exportacao")
    lakehouse = sub.add_parser("lakehouse", help="Constroi as camadas Bronze, Silver e Gold em Parquet")
    lakehouse.add_argument("--layer", choices=["bronze", "silver", "gold", "all"], default="all")
    lakehouse.add_argument("--data-dir", default="./data", help="Diretorio raiz das camadas Lakehouse")
    priority = sub.add_parser("prioritize-sites", help="Gera ranking de sites por armazenamento usado")
    priority.add_argument("--period", choices=["D7", "D30", "D90", "D180"], default="D7")
    summary_parser = sub.add_parser("summary", help="Gera resumo consolidado no log/terminal")
    summary_parser.add_argument("--recalculate-folders", action="store_true", help="Recalcula agregados de pastas antes do resumo")
    return parser


def crawl(command: str) -> int:
    from crawler import InventoryCrawler, install_signal_handlers
    from database import InventoryDatabase
    from graph_client import GraphClient

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
    graph = GraphClient(settings)
    crawler = InventoryCrawler(graph, db, settings.max_workers, settings.progress_log_interval_seconds)
    install_signal_handlers(crawler)
    run_id = db.start_run(command)
    start = time.monotonic()
    status = "success"
    try:
        if command == "crawl":
            full_resync = _bool(os.environ.get("FULL_RESYNC"), False)
            site_ids = list(settings.site_ids) + _load_lines(settings.site_ids_file)
            discovered_site_ids: list[str] | None = None
            if site_ids:
                LOGGER.info("Coleta delta limitada a %s Site IDs", len(site_ids))
                discovered_site_ids = crawler.discover_sharepoint_by_site_ids(site_ids)
            else:
                crawler.discover_sharepoint(settings.site_search_query)
            if settings.enable_user_onedrive:
                user_site_ids = crawler.discover_user_onedrives()
                if discovered_site_ids is not None:
                    discovered_site_ids.extend(user_site_ids)
            if full_resync:
                target_site_ids = set(discovered_site_ids) if discovered_site_ids is not None else None
                if target_site_ids is None:
                    LOGGER.warning("Full resync sem SITE_IDS/SITE_IDS_FILE: todos os sites descobertos serao reenumerados.")
                else:
                    LOGGER.info("Full resync solicitado para %s Site IDs", len(target_site_ids))
                db.reset_delta_sync(site_ids=target_site_ids, clear_items=True)
            crawler.process_delta_drives(
                reset_completed=True,
                site_ids=set(discovered_site_ids) if discovered_site_ids is not None else None,
            )
        else:
            crawler.process_delta_drives(reset_completed=False)
        # Agregados de pastas nao rodam mais ao fim de todo crawl: sao caros
        # (uma transacao por pasta) e nao sao usados pelo dashboard. Rode quando
        # precisar via: python main.py export --recalculate-folders
        return 0
    except Exception:
        status = "failed"
        LOGGER.exception("Execucao delta falhou")
        return 1
    finally:
        stats = db.stats(include_inventory_totals=False)
        stats.update(
            {
                "duration_seconds": round(time.monotonic() - start, 2),
                "graph_429": graph.throttle_count,
                "graph_retries": graph.retry_count,
            }
        )
        if status == "success" and _has_open_failures(stats):
            status = "completed_with_errors"
        db.finish_run(run_id, status, stats)
        LOGGER.info("Estatisticas finais: %s", stats)


def reset_site(site_ids: list[str], site_ids_file: str | None = None) -> int:
    from pathlib import Path

    from database import InventoryDatabase

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()

    requested_site_ids = [site_id.strip() for site_id in site_ids if site_id.strip()]
    requested_site_ids.extend(_load_lines(Path(site_ids_file)) if site_ids_file else [])
    requested_site_ids = list(dict.fromkeys(requested_site_ids))
    if not requested_site_ids:
        LOGGER.error("Informe pelo menos um --site-id ou --site-ids-file")
        return 2

    run_id = db.start_run("reset-site")
    start = time.monotonic()
    status = "success"
    totals = {"sites": 0, "drives": 0, "items": 0, "drive_sync_state": 0, "errors": 0}
    try:
        for site_id in requested_site_ids:
            counts = db.reset_site_inventory(site_id)
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
            LOGGER.info("Site resetado: %s removidos/resetados=%s", site_id, counts)
        print({"site_ids": len(requested_site_ids), **totals})
        return 0
    except Exception:
        status = "failed"
        LOGGER.exception("Falha ao resetar site")
        return 1
    finally:
        totals["duration_seconds"] = round(time.monotonic() - start, 2)
        db.finish_run(run_id, status, totals)


def export(parquet_rows_per_file: int = 1_000_000, recalculate_folders: bool = False) -> int:
    from database import InventoryDatabase
    from exporter import export_parquet_parts
    from lakehouse import build_lakehouse

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
    db.create_analytics_indexes()
    if recalculate_folders:
        LOGGER.info("Recalculando agregados de pastas antes da exportacao")
        db.recalculate_folder_aggregates()
    LOGGER.info(
        "Dataset Parquet exportado: %s",
        export_parquet_parts(
            settings.sqlite_db_path,
            settings.export_path,
            rows_per_file=parquet_rows_per_file,
        ),
    )
    data_dir = settings.export_path.parent / "data"
    LOGGER.info("Lakehouse exportado: %s", build_lakehouse(settings.sqlite_db_path, data_dir, layer="all"))
    return 0


def lakehouse(layer: str = "all", data_dir: str = "./data") -> int:
    from pathlib import Path

    from lakehouse import build_lakehouse

    settings = load_settings()
    configure_logging(settings.log_level)
    result = build_lakehouse(settings.sqlite_db_path, Path(data_dir), layer=layer)
    LOGGER.info("Lakehouse construido: %s", result)
    print(result)
    return 0


def summary(recalculate_folders: bool = False) -> int:
    from database import InventoryDatabase
    from exporter import print_summary

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
    db.create_analytics_indexes()
    if recalculate_folders:
        db.recalculate_folder_aggregates()
    stats = print_summary(settings.sqlite_db_path)
    print(stats)
    return 0


def prioritize_sites(period: str) -> int:
    from graph_client import GraphClient
    from priority import generate_site_priority

    settings = load_settings()
    configure_logging(settings.log_level)
    graph = GraphClient(settings)
    result = generate_site_priority(
        graph=graph,
        output_dir=settings.export_path,
        period=period,
    )
    print(result)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command in {"crawl", "resume"}:
        return crawl(args.command)
    if args.command == "reset-site":
        return reset_site(args.site_id, args.site_ids_file)
    if args.command == "export":
        return export(args.parquet_rows_per_file, args.recalculate_folders)
    if args.command == "lakehouse":
        return lakehouse(args.layer, args.data_dir)
    if args.command == "summary":
        return summary(args.recalculate_folders)
    if args.command == "prioritize-sites":
        return prioritize_sites(args.period)
    return 2


if __name__ == "__main__":
    sys.exit(main())
