from __future__ import annotations

import argparse
import logging
import sys
import time

from config import configure_logging, load_settings

LOGGER = logging.getLogger(__name__)


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
    sub.add_parser("crawl", help="Executa descoberta de sites/drives e processa fila completa")
    sub.add_parser("resume", help="Continua uma coleta interrompida usando checkpoint SQLite")
    sub.add_parser("retry-errors", help="Reprocessa erros retryable e volta a consumir a fila")
    export = sub.add_parser("export", help="Exporta inventario e relatorios")
    export.add_argument("--format", choices=["csv", "parquet", "all"], default="csv")
    priority = sub.add_parser("prioritize-sites", help="Gera ranking de sites por armazenamento usado")
    priority.add_argument("--period", choices=["D7", "D30", "D90", "D180"], default="D7")
    sub.add_parser("summary", help="Gera resumo consolidado no log/terminal")
    return parser


def _new_context() -> tuple[InventoryDatabase, GraphClient, InventoryCrawler]:
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
    return db, graph, crawler


def crawl(command: str) -> int:
    from crawler import InventoryCrawler, install_signal_handlers
    from database import InventoryDatabase
    from graph_client import GraphClient

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
    db.reset_interrupted_work()
    graph = GraphClient(settings)
    crawler = InventoryCrawler(graph, db, settings.max_workers, settings.progress_log_interval_seconds)
    install_signal_handlers(crawler)
    run_id = db.start_run(command)
    start = time.monotonic()
    status = "success"
    try:
        if command == "crawl":
            site_ids = list(settings.site_ids) + _load_lines(settings.site_ids_file)
            if site_ids:
                LOGGER.info("Coleta limitada a %s Site IDs", len(site_ids))
                crawler.discover_sharepoint_by_site_ids(site_ids)
            else:
                crawler.discover_sharepoint(settings.site_search_query)
            if settings.enable_user_onedrive:
                crawler.discover_user_onedrives()
        crawler.process_queue()
        db.recalculate_folder_aggregates()
        return 0
    except Exception:
        status = "failed"
        LOGGER.exception("Execucao falhou")
        return 1
    finally:
        stats = db.stats()
        stats.update(
            {
                "duration_seconds": round(time.monotonic() - start, 2),
                "graph_429": graph.throttle_count,
                "graph_retries": graph.retry_count,
            }
        )
        db.finish_run(run_id, status, stats)
        LOGGER.info("Estatisticas finais: %s", stats)


def retry_errors() -> int:
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
    run_id = db.start_run("retry-errors")
    start = time.monotonic()
    status = "success"
    try:
        crawler.retry_errors()
        db.recalculate_folder_aggregates()
        return 0
    except Exception:
        status = "failed"
        LOGGER.exception("Falha ao reprocessar erros")
        return 1
    finally:
        stats = db.stats()
        stats.update({"duration_seconds": round(time.monotonic() - start, 2), "graph_429": graph.throttle_count, "graph_retries": graph.retry_count})
        db.finish_run(run_id, status, stats)
        LOGGER.info("Estatisticas finais: %s", stats)


def export(format_name: str) -> int:
    from database import InventoryDatabase
    from exporter import export_csv, export_parquet

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
    LOGGER.info("Recalculando agregados de pastas antes da exportacao")
    db.recalculate_folder_aggregates()
    if format_name in {"csv", "all"}:
        LOGGER.info("CSV exportado: %s", export_csv(settings.sqlite_db_path, settings.export_path))
    if format_name in {"parquet", "all"}:
        LOGGER.info("Parquet exportado: %s", export_parquet(settings.sqlite_db_path, settings.export_path))
    return 0


def summary() -> int:
    from database import InventoryDatabase
    from exporter import print_summary

    settings = load_settings()
    configure_logging(settings.log_level)
    db = InventoryDatabase(settings.sqlite_db_path)
    db.init_schema()
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
    if args.command == "retry-errors":
        return retry_errors()
    if args.command == "export":
        return export(args.format)
    if args.command == "summary":
        return summary()
    if args.command == "prioritize-sites":
        return prioritize_sites(args.period)
    return 2


if __name__ == "__main__":
    sys.exit(main())
