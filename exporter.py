from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

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
ORDER BY s.web_url, d.name, i.full_path
"""


SUMMARY_SQL = """
SELECT
    s.name AS site_name,
    s.web_url AS site_url,
    d.name AS biblioteca,
    COALESCE(NULLIF(i.extension, ''), '(sem extensao/pasta)') AS extensao,
    i.item_type AS tipo_item,
    COUNT(*) AS quantidade,
    COALESCE(SUM(i.size_bytes), 0) AS tamanho_bytes
FROM items i
JOIN sites s ON s.id = i.site_id
JOIN drives d ON d.id = i.drive_id
GROUP BY s.name, s.web_url, d.name, extensao, i.item_type
ORDER BY s.web_url, d.name, i.item_type, extensao
"""


FOLDER_SUMMARY_SQL = """
SELECT
    s.name AS site_name,
    s.web_url AS site_url,
    d.name AS biblioteca,
    i.full_path AS pasta,
    i.file_count AS quantidade_arquivos,
    i.folder_total_size_bytes AS volume_total_bytes,
    i.folder_total_size_formatted AS volume_total_formatado,
    i.modified_at AS data_modificacao
FROM items i
JOIN sites s ON s.id = i.site_id
JOIN drives d ON d.id = i.drive_id
WHERE i.item_type='folder'
ORDER BY s.web_url, d.name, i.full_path
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _write_csv(rows: Iterable[sqlite3.Row], output_file: Path) -> int:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer: csv.DictWriter[str] | None = None
        for row in rows:
            data = dict(row)
            if "tamanho_bytes" in data:
                data.setdefault("tamanho_formatado_calculado", format_size(data["tamanho_bytes"]))
            if writer is None:
                writer = csv.DictWriter(handle, fieldnames=list(data.keys()))
                writer.writeheader()
            writer.writerow(data)
            count += 1
    return count


def export_csv(db_path: Path, output_dir: Path) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        inventory_count = _write_csv(conn.execute(INVENTORY_SQL), output_dir / "inventory.csv")
        summary_count = _write_csv(conn.execute(SUMMARY_SQL), output_dir / "summary_by_extension.csv")
        folder_count = _write_csv(conn.execute(FOLDER_SUMMARY_SQL), output_dir / "summary_by_folder.csv")
        return {"inventory_rows": inventory_count, "summary_rows": summary_count, "folder_summary_rows": folder_count}
    finally:
        conn.close()


def export_parquet(db_path: Path, output_dir: Path, chunk_size: int = 10000) -> dict[str, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Exportacao Parquet requer pyarrow instalado: pip install pyarrow") from exc

    conn = _connect(db_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "inventory.parquet"
    writer: pq.ParquetWriter | None = None
    total = 0
    try:
        cur = conn.execute(INVENTORY_SQL)
        columns = [desc[0] for desc in cur.description]
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            batch_data = {column: [row[column] for row in rows] for column in columns}
            table = pa.Table.from_pydict(batch_data)
            if writer is None:
                writer = pq.ParquetWriter(output_file, table.schema, compression="snappy")
            writer.write_table(table)
            total += len(rows)
        return {"inventory_rows": total, "file": str(output_file)}
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
                    (SELECT COUNT(*) FROM items WHERE item_type='folder') folders,
                    (SELECT COUNT(*) FROM items WHERE item_type='file') files,
                    (SELECT COALESCE(SUM(size_bytes), 0) FROM items WHERE item_type='file') total_bytes,
                    (SELECT COUNT(*) FROM errors WHERE resolved=0) open_errors
                """
            ).fetchone()
        )
        totals["total_formatted"] = format_size(totals["total_bytes"])
        LOGGER.info("Resumo: %s", totals)
        return totals
    finally:
        conn.close()
