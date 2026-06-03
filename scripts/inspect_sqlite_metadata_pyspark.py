from __future__ import annotations

import argparse
from pathlib import Path


def format_size(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if value < 1024 or unit == "PB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} PB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspeciona metadados do SQLite de inventario usando PySpark."
    )
    parser.add_argument(
        "--db",
        default="inventory/sharepoint_inventory.sqlite3",
        help="Caminho do SQLite gerado pelo crawler.",
    )
    parser.add_argument(
        "--sqlite-jdbc-jar",
        default="",
        help="Caminho opcional para o jar sqlite-jdbc. Ex.: ./drivers/sqlite-jdbc.jar",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Quantidade de linhas nas amostras.",
    )
    return parser


def jdbc_options(db_path: Path, table_or_query: str) -> dict[str, str]:
    return {
        "url": f"jdbc:sqlite:{db_path.resolve()}",
        "driver": "org.sqlite.JDBC",
        "dbtable": table_or_query,
    }


def read_jdbc(spark, db_path: Path, table_or_query: str):
    return spark.read.format("jdbc").options(**jdbc_options(db_path, table_or_query)).load()


def show_title(title: str) -> None:
    print()
    print("=" * len(title))
    print(title)
    print("=" * len(title))


def main() -> int:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite nao encontrado: {db_path}")
    if args.sqlite_jdbc_jar and not Path(args.sqlite_jdbc_jar).exists():
        raise FileNotFoundError(f"Driver JDBC do SQLite nao encontrado: {args.sqlite_jdbc_jar}")

    try:
        from pyspark.sql import SparkSession
        from pyspark.sql import functions as F
    except ImportError as exc:
        raise RuntimeError("Instale o PySpark antes de executar: pip install pyspark") from exc

    builder = (
        SparkSession.builder.appName("inspect-sharepoint-sqlite-metadata")
        .master("local[*]")
        .config("spark.sql.session.timeZone", "UTC")
    )
    if args.sqlite_jdbc_jar:
        builder = builder.config("spark.jars", str(Path(args.sqlite_jdbc_jar).resolve()))

    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    try:
        show_title("Banco")
        print(db_path.resolve())

        inventory_query = """
        (
            SELECT
                s.host_name AS tenant_site,
                s.name AS site_name,
                s.web_url AS site_url,
                d.name AS biblioteca,
                i.parent_path AS caminho_pasta,
                i.full_path AS caminho_item,
                i.name AS nome,
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
                i.web_url AS url_item,
                i.status AS status_leitura,
                i.collected_at AS data_hora_coleta,
                i.id AS id_item,
                i.drive_id AS id_drive,
                i.site_id AS id_site
            FROM items i
            JOIN sites s ON s.id = i.site_id
            JOIN drives d ON d.id = i.drive_id
        ) metadata
        """

        tables = {
            "sites": read_jdbc(spark, db_path, "sites"),
            "drives": read_jdbc(spark, db_path, "drives"),
            "items": read_jdbc(spark, db_path, "items"),
            "folders_queue": read_jdbc(spark, db_path, "folders_queue"),
            "errors": read_jdbc(spark, db_path, "errors"),
        }
        metadata = read_jdbc(spark, db_path, inventory_query).cache()

        show_title("Contagens principais")
        table_counts = [(name, df.count()) for name, df in tables.items()]
        spark.createDataFrame(table_counts, ["tabela", "linhas"]).show(truncate=False)

        file_stats = metadata.where(F.col("tipo_item") == "file").agg(
            F.count("*").alias("arquivos"),
            F.coalesce(F.sum("tamanho_bytes"), F.lit(0)).alias("total_bytes"),
            F.countDistinct("extensao").alias("extensoes_distintas"),
            F.countDistinct("tipo_mime").alias("mimes_distintos"),
            F.min("data_modificacao").alias("menor_data_modificacao"),
            F.max("data_modificacao").alias("maior_data_modificacao"),
        ).first()
        print(
            f"Arquivos: {file_stats['arquivos']} | "
            f"Volume: {format_size(file_stats['total_bytes'])} | "
            f"Extensoes: {file_stats['extensoes_distintas']} | "
            f"MIME types: {file_stats['mimes_distintos']}"
        )
        print(
            "Datas de modificacao: "
            f"{file_stats['menor_data_modificacao']} ate {file_stats['maior_data_modificacao']}"
        )

        show_title("Schema dos metadados")
        metadata.printSchema()

        show_title("Itens por tipo e status")
        metadata.groupBy("tipo_item", "status_leitura").count().orderBy(
            "tipo_item", "status_leitura"
        ).show(args.limit, truncate=False)

        show_title("Top extensoes por quantidade")
        metadata.where(F.col("tipo_item") == "file").withColumn(
            "extensao_normalizada",
            F.when(
                F.col("extensao").isNull() | (F.col("extensao") == ""),
                F.lit("(sem extensao)"),
            ).otherwise(F.col("extensao")),
        ).groupBy("extensao_normalizada").agg(
            F.count("*").alias("arquivos"),
            F.coalesce(F.sum("tamanho_bytes"), F.lit(0)).alias("total_bytes"),
        ).orderBy(
            F.desc("arquivos")
        ).show(args.limit, truncate=False)

        show_title("Top bibliotecas por volume de arquivos")
        metadata.where(F.col("tipo_item") == "file").groupBy(
            "site_name", "site_url", "biblioteca"
        ).agg(
            F.count("*").alias("arquivos"),
            F.coalesce(F.sum("tamanho_bytes"), F.lit(0)).alias("total_bytes"),
        ).orderBy(
            F.desc("total_bytes")
        ).show(args.limit, truncate=False)

        show_title("Amostra de metadados de arquivos")
        metadata.where(F.col("tipo_item") == "file").select(
            "site_name",
            "biblioteca",
            "caminho_item",
            "nome",
            "extensao",
            "tipo_mime",
            "tamanho_bytes",
            "data_criacao",
            "data_modificacao",
            "data_ultimo_uso_acesso",
            "url_item",
        ).orderBy(F.desc("data_modificacao")).show(args.limit, truncate=80)

        show_title("Erros em aberto")
        tables["errors"].where(F.col("resolved") == 0).select(
            "entity_type",
            "operation",
            "status_code",
            "message",
            "retryable",
            "created_at",
        ).orderBy(F.desc("created_at")).show(args.limit, truncate=100)

        return 0
    finally:
        spark.stop()


if __name__ == "__main__":
    raise SystemExit(main())
