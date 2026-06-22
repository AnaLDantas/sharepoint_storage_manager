from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT_DIR / "exports" / "inventory.parquet"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "exports" / "client_report"
BYTES_IN_MB = 1024**2
BYTES_IN_GB = 1024**3


def copy_csv(con: duckdb.DuckDBPyConnection, query: str, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"""
        COPY ({query})
        TO '{output_file.as_posix()}'
        WITH (
            HEADER true,
            DELIMITER ';',
            QUOTE '"',
            ESCAPE '"',
            ENCODING 'utf-8'
        )
        """
    )


def build_client_report(input_file: Path, output_dir: Path, old_file_days: int = 365) -> None:
    if not input_file.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {input_file}")

    con = duckdb.connect()
    parquet = input_file.as_posix().replace("'", "''")

    common_from = f"FROM read_parquet('{parquet}')"

    files_query = f"""
        SELECT
            site_name AS "Site",
            site_url AS "URL do site",
            biblioteca AS "Biblioteca",
            caminho_completo_da_pasta AS "Pasta",
            nome_arquivo_ou_pasta AS "Arquivo",
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS "Extensao",
            tipo_mime AS "Tipo MIME",
            tamanho_formatado AS "Tamanho",
            ROUND(tamanho_bytes / {BYTES_IN_MB}, 2) AS "Tamanho MB",
            ROUND(tamanho_bytes / {BYTES_IN_GB}, 4) AS "Tamanho GB",
            data_criacao AS "Criado em",
            data_modificacao AS "Modificado em",
            data_ultimo_uso_acesso AS "Ultimo acesso/uso",
            caminho_item AS "Caminho completo",
            status_leitura AS "Status da leitura"
        {common_from}
        WHERE tipo_item = 'file'
        ORDER BY site_name, biblioteca, caminho_item
    """

    sites_query = f"""
        SELECT
            site_name AS "Site",
            site_url AS "URL do site",
            COUNT(DISTINCT biblioteca) AS "Bibliotecas",
            COUNT(*) FILTER (WHERE tipo_item = 'file') AS "Arquivos",
            COUNT(*) FILTER (WHERE tipo_item = 'folder') AS "Pastas",
            ROUND(SUM(CASE WHEN tipo_item = 'file' THEN tamanho_bytes ELSE 0 END) / {BYTES_IN_GB}, 2) AS "Armazenamento GB",
            MAX(data_modificacao) AS "Ultima modificacao identificada"
        {common_from}
        GROUP BY site_name, site_url
        ORDER BY "Armazenamento GB" DESC
    """

    extensions_query = f"""
        SELECT
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS "Extensao",
            COUNT(*) AS "Arquivos",
            ROUND(SUM(tamanho_bytes) / {BYTES_IN_GB}, 2) AS "Armazenamento GB",
            ROUND(AVG(tamanho_bytes) / {BYTES_IN_MB}, 2) AS "Tamanho medio MB",
            ROUND(MAX(tamanho_bytes) / {BYTES_IN_GB}, 4) AS "Maior arquivo GB"
        {common_from}
        WHERE tipo_item = 'file'
        GROUP BY "Extensao"
        ORDER BY "Armazenamento GB" DESC, "Arquivos" DESC
    """

    old_files_query = f"""
        SELECT
            site_name AS "Site",
            site_url AS "URL do site",
            biblioteca AS "Biblioteca",
            nome_arquivo_ou_pasta AS "Arquivo",
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS "Extensao",
            tamanho_formatado AS "Tamanho",
            ROUND(tamanho_bytes / {BYTES_IN_GB}, 4) AS "Tamanho GB",
            data_modificacao AS "Modificado em",
            caminho_item AS "Caminho completo"
        {common_from}
        WHERE tipo_item = 'file'
          AND TRY_CAST(data_modificacao AS TIMESTAMP) < current_date - INTERVAL {old_file_days} DAY
        ORDER BY tamanho_bytes DESC
        LIMIT 1000
    """

    largest_files_query = f"""
        SELECT
            site_name AS "Site",
            site_url AS "URL do site",
            biblioteca AS "Biblioteca",
            nome_arquivo_ou_pasta AS "Arquivo",
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS "Extensao",
            tamanho_formatado AS "Tamanho",
            ROUND(tamanho_bytes / {BYTES_IN_GB}, 4) AS "Tamanho GB",
            data_modificacao AS "Modificado em",
            caminho_item AS "Caminho completo"
        {common_from}
        WHERE tipo_item = 'file'
        ORDER BY tamanho_bytes DESC
        LIMIT 1000
    """

    copy_csv(con, sites_query, output_dir / "01_resumo_por_site.csv")
    copy_csv(con, extensions_query, output_dir / "02_resumo_por_extensao.csv")
    copy_csv(con, largest_files_query, output_dir / "03_top_1000_maiores_arquivos.csv")
    copy_csv(con, old_files_query, output_dir / "04_top_1000_arquivos_antigos.csv")
    copy_csv(con, files_query, output_dir / "05_inventario_arquivos_cliente.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera CSVs amigaveis para envio ao cliente.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Arquivo inventory.parquet de origem.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Pasta de saida dos CSVs.")
    parser.add_argument("--old-file-days", type=int, default=365, help="Idade minima em dias para arquivos antigos.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build_client_report(args.input, args.output_dir, args.old_file_days)
    print(f"Relatorio gerado em: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
