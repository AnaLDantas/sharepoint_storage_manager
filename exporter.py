from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from database import format_size

LOGGER = logging.getLogger(__name__)


INVENTORY_SQL = """
SELECT
    s.host_name AS tenant_site,
    s.name AS site_name,
    s.web_url AS site_url,
    d.name AS biblioteca,
    i.parent_path AS caminho_completo_da_pasta,
    i.name AS nome_arquivo_ou_pasta,
    i.item_type AS tipo_item,
    i.extension AS extensao,
    i.mime_type AS tipo_mime,
    i.size_bytes AS tamanho_bytes,
    i.formatted_size AS tamanho_formatado,
    i.created_at AS data_criacao,
    i.modified_at AS data_modificacao,
    i.last_accessed_at AS data_ultimo_uso_acesso,
    i.file_count AS quantidade_arquivos_dentro_da_pasta,
    i.folder_total_size_bytes AS volume_total_pasta_bytes,
    i.folder_total_size_formatted AS volume_total_pasta_formatado,
    i.id AS id_item,
    i.drive_id AS id_drive,
    i.status AS status_leitura,
    i.collected_at AS data_hora_coleta,
    i.full_path AS caminho_item
FROM items i
JOIN sites s ON s.id = i.site_id
JOIN drives d ON d.id = i.drive_id
WHERE i.status!='deleted'
ORDER BY s.web_url, d.name, i.full_path
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_table(rows: list[sqlite3.Row], columns: list[str]):
    import pyarrow as pa

    int_columns = {"tamanho_bytes", "quantidade_arquivos_dentro_da_pasta", "volume_total_pasta_bytes"}
    batch_data = {}
    for column in columns:
        values = [row[column] for row in rows]
        value_type = pa.int64() if column in int_columns else pa.string()
        batch_data[column] = pa.array(values, type=value_type)
    schema = pa.schema([(column, pa.int64() if column in int_columns else pa.string()) for column in columns])
    return pa.Table.from_arrays([batch_data[column] for column in columns], schema=schema)


def _file_bytes(rows: list[sqlite3.Row]) -> int:
    return sum(int(row["tamanho_bytes"] or 0) for row in rows if row["tipo_item"] == "file")


def export_parquet_parts(
    db_path: Path,
    output_dir: Path,
    rows_per_file: int = 1_000_000,
    chunk_size: int = 10000,
) -> dict[str, Any]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Exportacao Parquet requer pyarrow instalado: pip install pyarrow") from exc

    if rows_per_file < chunk_size:
        rows_per_file = chunk_size

    conn = _connect(db_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_dir = output_dir / "inventory_parquet"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    try:
        for stale_file in dataset_dir.glob("part-*.parquet"):
            stale_file.unlink()
    except PermissionError:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_dir = output_dir / f"inventory_parquet_{timestamp}"
        dataset_dir.mkdir(parents=True, exist_ok=False)

    writer: pq.ParquetWriter | None = None
    part_index = 0
    rows_in_part = 0
    total = 0
    total_file_bytes = 0
    part_files: list[str] = []
    try:
        cur = conn.execute(INVENTORY_SQL)
        columns = [desc[0] for desc in cur.description]
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            table = _rows_to_table(rows, columns)
            if writer is None or rows_in_part >= rows_per_file:
                if writer:
                    writer.close()
                part_file = dataset_dir / f"part-{part_index:05d}.parquet"
                writer = pq.ParquetWriter(part_file, table.schema, compression="snappy")
                part_files.append(str(part_file))
                part_index += 1
                rows_in_part = 0
            writer.write_table(table)
            rows_in_part += len(rows)
            total += len(rows)
            total_file_bytes += _file_bytes(rows)
        return {
            "inventory_rows": total,
            "file_total_bytes": total_file_bytes,
            "file_total_formatted": format_size(total_file_bytes),
            "dataset_dir": str(dataset_dir),
            "part_files": len(part_files),
            "rows_per_file": rows_per_file,
        }
    finally:
        if writer:
            writer.close()
        conn.close()


def export_parquet_parts(
    db_path: Path,
    output_dir: Path,
    rows_per_file: int = 1_000_000,
    chunk_size: int = 10000,
) -> dict[str, Any]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Exportacao Parquet requer pyarrow instalado: pip install pyarrow") from exc

    if rows_per_file < chunk_size:
        rows_per_file = chunk_size

    conn = _connect(db_path)
    dataset_dir = output_dir / "inventory_parquet"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for stale_file in dataset_dir.glob("part-*.parquet"):
        stale_file.unlink()

    writer: pq.ParquetWriter | None = None
    part_index = 0
    rows_in_part = 0
    total = 0
    part_files: list[str] = []
    try:
        cur = conn.execute(INVENTORY_SQL)
        columns = [desc[0] for desc in cur.description]
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            table = _rows_to_table(rows, columns)
            if writer is None or rows_in_part >= rows_per_file:
                if writer:
                    writer.close()
                part_file = dataset_dir / f"part-{part_index:05d}.parquet"
                writer = pq.ParquetWriter(part_file, table.schema, compression="snappy")
                part_files.append(str(part_file))
                part_index += 1
                rows_in_part = 0
            writer.write_table(table)
            rows_in_part += len(rows)
            total += len(rows)
        return {
            "inventory_rows": total,
            "dataset_dir": str(dataset_dir),
            "part_files": len(part_files),
            "rows_per_file": rows_per_file,
        }
    finally:
        if writer:
            writer.close()
        conn.close()


def print_summary(db_path: Path) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        totals = dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM sites) sites,
                    (SELECT COUNT(*) FROM drives) drives,
                    (SELECT COUNT(*) FROM items WHERE item_type='folder' AND status!='deleted') folders,
                    (SELECT COUNT(*) FROM items WHERE item_type='file' AND status!='deleted') files,
                    (SELECT COALESCE(SUM(size_bytes), 0) FROM items WHERE item_type='file' AND status!='deleted') total_bytes,
                    (SELECT COUNT(*) FROM errors WHERE resolved=0) open_errors
                """
            ).fetchone()
        )
        totals["total_formatted"] = format_size(totals["total_bytes"])
        LOGGER.info("Resumo: %s", totals)
        return totals
    finally:
        conn.close()


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def audit_sites(db_path: Path, site_priority_csv: Path, output_dir: Path, warn_ratio: float = 0.8) -> dict[str, Any]:
    if not site_priority_csv.exists():
        raise FileNotFoundError(f"Arquivo de prioridade nao encontrado: {site_priority_csv}")

    expected_sites: list[dict[str, Any]] = []
    with site_priority_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            expected_bytes = int(float((row.get("storage_used_bytes") or "0").strip() or 0))
            expected_sites.append(
                {
                    "site_id": row.get("site_id") or "",
                    "site_url": row.get("site_url") or "",
                    "site_name": row.get("site_name") or row.get("owner") or "",
                    "expected_storage_bytes": expected_bytes,
                    "expected_storage_formatted": format_size(expected_bytes),
                }
            )

    conn = _connect(db_path)
    try:
        collected_rows = conn.execute(
            """
            SELECT
                s.id AS site_id,
                s.name AS site_name,
                s.web_url AS site_url,
                COUNT(DISTINCT d.id) AS drives,
                COUNT(CASE WHEN i.item_type='file' AND i.status!='deleted' THEN 1 END) AS files,
                COALESCE(SUM(CASE WHEN i.item_type='file' AND i.status!='deleted' THEN i.size_bytes ELSE 0 END), 0) AS collected_storage_bytes
            FROM sites s
            LEFT JOIN drives d ON d.site_id = s.id
            LEFT JOIN items i ON i.site_id = s.id
            GROUP BY s.id, s.name, s.web_url
            """
        ).fetchall()
    finally:
        conn.close()

    collected_by_url = {_normalize_url(row["site_url"]): dict(row) for row in collected_rows if row["site_url"]}
    audit_rows: list[dict[str, Any]] = []
    for expected in expected_sites:
        collected = collected_by_url.get(_normalize_url(expected["site_url"]))
        collected_bytes = int(collected["collected_storage_bytes"]) if collected else 0
        expected_bytes = int(expected["expected_storage_bytes"])
        ratio = (collected_bytes / expected_bytes) if expected_bytes else 1.0
        if not collected:
            status = "missing"
        elif expected_bytes and ratio < warn_ratio:
            status = "under_collected"
        else:
            status = "ok"
        audit_rows.append(
            {
                **expected,
                "status": status,
                "collected_site_id": collected["site_id"] if collected else "",
                "collected_site_name": collected["site_name"] if collected else "",
                "drives": collected["drives"] if collected else 0,
                "files": collected["files"] if collected else 0,
                "collected_storage_bytes": collected_bytes,
                "collected_storage_formatted": format_size(collected_bytes),
                "collected_vs_expected_ratio": round(ratio, 4),
                "missing_storage_bytes": max(expected_bytes - collected_bytes, 0),
                "missing_storage_formatted": format_size(max(expected_bytes - collected_bytes, 0)),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "site_collection_audit.csv"
    with output_file.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(audit_rows[0].keys()) if audit_rows else [
            "site_id",
            "site_url",
            "status",
            "expected_storage_bytes",
            "collected_storage_bytes",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit_rows)

    return {
        "expected_sites": len(expected_sites),
        "missing_sites": sum(1 for row in audit_rows if row["status"] == "missing"),
        "under_collected_sites": sum(1 for row in audit_rows if row["status"] == "under_collected"),
        "ok_sites": sum(1 for row in audit_rows if row["status"] == "ok"),
        "audit_file": str(output_file),
    }
