from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

LOGGER = logging.getLogger(__name__)

BYTES_IN_KB = 1024
BYTES_IN_MB = 1024**2
BYTES_IN_GB = 1024**3

BRONZE_TABLES = ("sites", "drives", "files")
SILVER_TABLES = ("sites", "drives", "files")
GOLD_TABLES = (
    "storage_kpis",
    "top_sites",
    "top_extensions",
    "inactive_sites",
    "archive_candidates",
    "storage_savings",
)

GOLD_SCHEMAS = {
    "storage_kpis": ["site_name", "total_files", "storage_gb", "avg_file_size_mb", "archive_candidate_gb", "archive_candidate_pct"],
    "top_sites": ["site_name", "storage_gb", "file_count"],
    "top_extensions": ["extension", "extension_category", "storage_gb", "file_count"],
    "inactive_sites": ["site_name", "days_since_last_activity", "storage_gb"],
    "archive_candidates": ["site_name", "file_name", "extension", "size_gb", "days_since_modified"],
    "storage_savings": ["site_name", "current_storage_gb", "archive_candidate_gb", "estimated_saving_pct"],
}

OFFICE_EXTENSIONS = {
    "doc",
    "docm",
    "docx",
    "dot",
    "dotm",
    "dotx",
    "odp",
    "ods",
    "odt",
    "one",
    "pdf",
    "pot",
    "potm",
    "potx",
    "pps",
    "ppsm",
    "ppsx",
    "ppt",
    "pptm",
    "pptx",
    "rtf",
    "xls",
    "xlsb",
    "xlsm",
    "xlsx",
}
IMAGE_EXTENSIONS = {"ai", "bmp", "gif", "heic", "heif", "ico", "jpeg", "jpg", "png", "psd", "svg", "tif", "tiff", "webp"}
VIDEO_EXTENSIONS = {"avi", "flv", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "ts", "webm", "wmv"}
ARCHIVE_EXTENSIONS = {"7z", "bz2", "gz", "rar", "tar", "tgz", "xz", "zip"}
EXECUTABLE_EXTENSIONS = {"app", "bat", "bin", "cmd", "com", "dll", "dmg", "exe", "msi", "ps1", "sh"}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _raw_json_frame(df: pd.DataFrame) -> pd.DataFrame:
    if "raw_json" not in df.columns:
        return df
    raw = pd.json_normalize(df["raw_json"].map(_parse_json_object), sep="_")
    raw = raw.add_prefix("raw_")
    return pd.concat([df.reset_index(drop=True), raw.reset_index(drop=True)], axis=1)


def _read_sql(db_path: Path, query: str) -> pd.DataFrame:
    conn = _connect(db_path)
    try:
        return pd.read_sql_query(query, conn)
    finally:
        conn.close()


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = _parquet_safe_frame(df)
    df.to_parquet(path, index=False, engine="pyarrow", compression="snappy")


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path, engine="pyarrow")


def _parquet_safe_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _parquet_safe_frame(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    object_columns = result.select_dtypes(include=["object"]).columns
    for column in object_columns:
        result[column] = result[column].map(_parquet_safe_value)
    return result


def save_bronze(db_path: Path, data_dir: Path = Path("data")) -> dict[str, Any]:
    """Materializa dados brutos do SQLite em Parquet na camada Bronze."""
    bronze_dir = data_dir / "bronze"
    sites = _read_sql(
        db_path,
        """
        SELECT
            id AS site_id,
            name AS site_name,
            web_url AS site_url,
            host_name,
            discovered_at,
            processed_at,
            raw_json
        FROM sites
        """,
    )
    drives = _read_sql(
        db_path,
        """
        SELECT
            id AS drive_id,
            site_id,
            name AS drive_name,
            drive_type,
            web_url AS drive_url,
            discovered_at,
            processed_at,
            raw_json
        FROM drives
        """,
    )
    files = _read_sql(
        db_path,
        """
        SELECT
            id AS file_id,
            drive_id,
            site_id,
            library_name,
            parent_id,
            parent_path,
            full_path,
            name AS file_name,
            item_type,
            extension,
            mime_type,
            size_bytes,
            formatted_size,
            created_at,
            modified_at,
            last_accessed_at,
            file_count,
            folder_total_size_bytes,
            folder_total_size_formatted,
            web_url,
            status,
            collected_at,
            raw_json
        FROM items
        WHERE item_type='file'
        """,
    )

    datasets = {
        "sites": _raw_json_frame(sites),
        "drives": _raw_json_frame(drives),
        "files": _raw_json_frame(files),
    }
    result: dict[str, Any] = {}
    for name, df in datasets.items():
        path = bronze_dir / f"{name}.parquet"
        _write_parquet(df, path)
        result[name] = {"rows": int(len(df)), "file": str(path)}
    return result


def load_bronze(data_dir: Path = Path("data")) -> dict[str, pd.DataFrame]:
    bronze_dir = data_dir / "bronze"
    return {name: _read_parquet(bronze_dir / f"{name}.parquet") for name in BRONZE_TABLES}


def _extension_category(extension: Any) -> str:
    ext = "" if extension is None or pd.isna(extension) else str(extension).lower().strip().lstrip(".")
    if ext in OFFICE_EXTENSIONS:
        return "Office Documents"
    if ext in IMAGE_EXTENSIONS:
        return "Images"
    if ext in VIDEO_EXTENSIONS:
        return "Videos"
    if ext in ARCHIVE_EXTENSIONS:
        return "Archives"
    if ext in EXECUTABLE_EXTENSIONS:
        return "Executables"
    return "Other"


def _usage_status(days_since_modified: Any) -> str:
    if pd.isna(days_since_modified):
        return "Unknown"
    days = int(days_since_modified)
    if days <= 90:
        return "Active"
    if days <= 180:
        return "Low Usage"
    if days <= 365:
        return "Unused"
    return "Archive Candidate"


def _drop_raw_columns(df: pd.DataFrame) -> pd.DataFrame:
    raw_columns = [column for column in df.columns if column == "raw_json" or column.startswith("raw_")]
    return df.drop(columns=raw_columns, errors="ignore")


def _clean_text_series(series: pd.Series, default: str = "") -> pd.Series:
    return series.fillna(default).astype(str).str.strip()


def _select_existing(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return df[[column for column in columns if column in df.columns]].copy()


def _series_or_default(df: pd.DataFrame, column: str, default: Any = "") -> pd.Series:
    if column in df.columns:
        return df[column]
    return pd.Series(default, index=df.index)


def _build_silver_sites(sites: pd.DataFrame) -> pd.DataFrame:
    sites = _drop_raw_columns(sites.copy())
    if sites.empty:
        return _select_existing(
            sites,
            ["site_id", "site_name", "site_url", "host_name", "discovered_at", "processed_at", "is_processed"],
        )

    for column in ("discovered_at", "processed_at"):
        if column in sites.columns:
            sites[column] = pd.to_datetime(sites[column], errors="coerce", utc=True)
    if "site_name" in sites.columns:
        sites["site_name"] = _clean_text_series(sites["site_name"], "(sem nome)")
    if "site_url" in sites.columns:
        sites["site_url"] = _clean_text_series(sites["site_url"])
    if "host_name" in sites.columns:
        sites["host_name"] = _clean_text_series(sites["host_name"])
    if "processed_at" in sites.columns:
        sites["is_processed"] = sites["processed_at"].notna()
    return _select_existing(
        sites,
        ["site_id", "site_name", "site_url", "host_name", "discovered_at", "processed_at", "is_processed"],
    ).drop_duplicates("site_id")


def _build_silver_drives(drives: pd.DataFrame, sites: pd.DataFrame) -> pd.DataFrame:
    drives = _drop_raw_columns(drives.copy())
    if drives.empty:
        return _select_existing(
            drives,
            ["drive_id", "site_id", "site_name", "drive_name", "drive_type", "drive_url", "discovered_at", "processed_at", "is_processed"],
        )

    for column in ("discovered_at", "processed_at"):
        if column in drives.columns:
            drives[column] = pd.to_datetime(drives[column], errors="coerce", utc=True)
    if "drive_name" in drives.columns:
        drives["drive_name"] = _clean_text_series(drives["drive_name"], "(sem nome)")
    if "drive_type" in drives.columns:
        drives["drive_type"] = _clean_text_series(drives["drive_type"], "unknown").str.lower()
    if "drive_url" in drives.columns:
        drives["drive_url"] = _clean_text_series(drives["drive_url"])
    if "processed_at" in drives.columns:
        drives["is_processed"] = drives["processed_at"].notna()

    site_lookup = _select_existing(sites, ["site_id", "site_name"]).drop_duplicates("site_id")
    if not site_lookup.empty:
        drives = drives.merge(site_lookup, how="left", on="site_id")
    return _select_existing(
        drives,
        ["drive_id", "site_id", "site_name", "drive_name", "drive_type", "drive_url", "discovered_at", "processed_at", "is_processed"],
    ).drop_duplicates("drive_id")


def _build_silver_files(files: pd.DataFrame, sites: pd.DataFrame, drives: pd.DataFrame) -> pd.DataFrame:
    files = _drop_raw_columns(files.copy())
    if files.empty:
        return pd.DataFrame(
            columns=[
                "file_id",
                "drive_id",
                "site_id",
                "site_name",
                "site_url",
                "drive_name",
                "drive_type",
                "library_name",
                "parent_id",
                "parent_path",
                "full_path",
                "file_name",
                "extension",
                "extension_category",
                "mime_type",
                "size_bytes",
                "size_kb",
                "size_mb",
                "size_gb",
                "created_at",
                "created_date",
                "modified_at",
                "modified_date",
                "last_accessed_at",
                "days_since_modified",
                "usage_status",
                "web_url",
                "status",
                "collected_at",
            ]
        )

    for column in ("created_at", "modified_at", "last_accessed_at", "collected_at"):
        if column in files.columns:
            files[column] = pd.to_datetime(files[column], errors="coerce", utc=True)

    files["size_bytes"] = pd.to_numeric(files.get("size_bytes", 0), errors="coerce").fillna(0).astype("int64")
    files["size_kb"] = files["size_bytes"] / BYTES_IN_KB
    files["size_mb"] = files["size_bytes"] / BYTES_IN_MB
    files["size_gb"] = files["size_bytes"] / BYTES_IN_GB
    files["created_date"] = files["created_at"].dt.date if "created_at" in files.columns else pd.NaT
    files["modified_date"] = files["modified_at"].dt.date if "modified_at" in files.columns else pd.NaT

    today = pd.Timestamp.now(tz="UTC").normalize()
    files["days_since_modified"] = (today - files["modified_at"].dt.normalize()).dt.days if "modified_at" in files.columns else pd.NA
    files["usage_status"] = files["days_since_modified"].apply(_usage_status)
    files["extension"] = _series_or_default(files, "extension").fillna("").astype(str).str.lower().str.strip().str.lstrip(".")
    files["extension_category"] = files["extension"].apply(_extension_category)
    files["file_name"] = _clean_text_series(_series_or_default(files, "file_name"), "(sem nome)")
    files["library_name"] = _clean_text_series(_series_or_default(files, "library_name"), "(sem biblioteca)")

    if "status" in files.columns:
        files = files[files["status"].fillna("") != "deleted"].copy()

    site_lookup = _select_existing(sites, ["site_id", "site_name", "site_url"]).drop_duplicates("site_id")
    drive_lookup = _select_existing(drives, ["drive_id", "drive_name", "drive_type"]).drop_duplicates("drive_id")
    if not site_lookup.empty:
        files = files.merge(site_lookup, how="left", on="site_id")
    if not drive_lookup.empty:
        files = files.merge(drive_lookup, how="left", on="drive_id")

    return _select_existing(
        files,
        [
            "file_id",
            "drive_id",
            "site_id",
            "site_name",
            "site_url",
            "drive_name",
            "drive_type",
            "library_name",
            "parent_id",
            "parent_path",
            "full_path",
            "file_name",
            "extension",
            "extension_category",
            "mime_type",
            "size_bytes",
            "size_kb",
            "size_mb",
            "size_gb",
            "created_at",
            "created_date",
            "modified_at",
            "modified_date",
            "last_accessed_at",
            "days_since_modified",
            "usage_status",
            "web_url",
            "status",
            "collected_at",
        ],
    )


def build_silver(data_dir: Path = Path("data")) -> dict[str, Any]:
    bronze = load_bronze(data_dir)
    silver_dir = data_dir / "silver"

    sites = _build_silver_sites(bronze["sites"])
    drives = _build_silver_drives(bronze["drives"], sites)
    files = _build_silver_files(bronze["files"], sites, drives)

    datasets = {"sites": sites, "drives": drives, "files": files}
    result: dict[str, Any] = {}
    for name, df in datasets.items():
        path = silver_dir / f"{name}.parquet"
        _write_parquet(df, path)
        result[name] = {"rows": int(len(df)), "file": str(path)}
    return result


def load_silver(data_dir: Path = Path("data")) -> dict[str, pd.DataFrame]:
    silver_dir = data_dir / "silver"
    return {name: _read_parquet(silver_dir / f"{name}.parquet") for name in SILVER_TABLES}


def build_gold(data_dir: Path = Path("data")) -> dict[str, Any]:
    silver = load_silver(data_dir)
    gold_dir = data_dir / "gold"
    files = silver["files"].copy()

    if files.empty:
        datasets = {name: pd.DataFrame(columns=columns) for name, columns in GOLD_SCHEMAS.items()}
    else:
        files["site_name"] = files["site_name"].fillna("(sem nome)")
        files["extension"] = files["extension"].replace("", "(sem extensao)")
        files["size_gb"] = pd.to_numeric(files["size_gb"], errors="coerce").fillna(0)
        files["size_mb"] = pd.to_numeric(files["size_mb"], errors="coerce").fillna(0)
        files["days_since_modified"] = pd.to_numeric(files["days_since_modified"], errors="coerce")

        by_site = files.groupby("site_name", dropna=False)
        total = by_site.agg(total_files=("file_id", "count"), storage_gb=("size_gb", "sum"), avg_file_size_mb=("size_mb", "mean")).reset_index()
        archive = (
            files[files["usage_status"] == "Archive Candidate"]
            .groupby("site_name", dropna=False)
            .agg(archive_candidate_gb=("size_gb", "sum"))
            .reset_index()
        )
        storage_kpis = total.merge(archive, how="left", on="site_name")
        storage_kpis["archive_candidate_gb"] = storage_kpis["archive_candidate_gb"].fillna(0)
        storage_kpis["archive_candidate_pct"] = (
            storage_kpis["archive_candidate_gb"] / storage_kpis["storage_gb"].replace(0, pd.NA) * 100
        ).fillna(0)
        storage_kpis = storage_kpis[
            ["site_name", "total_files", "storage_gb", "avg_file_size_mb", "archive_candidate_gb", "archive_candidate_pct"]
        ].sort_values("storage_gb", ascending=False)

        top_sites = storage_kpis.rename(columns={"total_files": "file_count"})[["site_name", "storage_gb", "file_count"]].sort_values(
            "storage_gb", ascending=False
        )

        top_extensions = (
            files.groupby(["extension", "extension_category"], dropna=False)
            .agg(storage_gb=("size_gb", "sum"), file_count=("file_id", "count"))
            .reset_index()
            .sort_values("storage_gb", ascending=False)
        )

        inactive_sites = (
            by_site.agg(days_since_last_activity=("days_since_modified", "min"), storage_gb=("size_gb", "sum"))
            .reset_index()
            .query("days_since_last_activity > 180")
            .sort_values(["days_since_last_activity", "storage_gb"], ascending=[False, False])
        )[["site_name", "days_since_last_activity", "storage_gb"]]

        archive_candidates = files[files["usage_status"] == "Archive Candidate"][
            ["site_name", "file_name", "extension", "size_gb", "days_since_modified"]
        ].sort_values("size_gb", ascending=False)

        storage_savings = storage_kpis.rename(
            columns={"storage_gb": "current_storage_gb", "archive_candidate_pct": "estimated_saving_pct"}
        )[["site_name", "current_storage_gb", "archive_candidate_gb", "estimated_saving_pct"]].sort_values(
            "archive_candidate_gb", ascending=False
        )

        datasets = {
            "storage_kpis": storage_kpis,
            "top_sites": top_sites,
            "top_extensions": top_extensions,
            "inactive_sites": inactive_sites,
            "archive_candidates": archive_candidates,
            "storage_savings": storage_savings,
        }

    result: dict[str, Any] = {}
    for name, df in datasets.items():
        path = gold_dir / f"{name}.parquet"
        _write_parquet(df, path)
        result[name] = {"rows": int(len(df)), "file": str(path)}
    return result


def build_lakehouse(db_path: Path, data_dir: Path = Path("data"), layer: str = "all") -> dict[str, Any]:
    layer = layer.lower()
    if layer not in {"bronze", "silver", "gold", "all"}:
        raise ValueError("layer deve ser bronze, silver, gold ou all")

    result: dict[str, Any] = {}
    if layer in {"bronze", "all"}:
        result["bronze"] = save_bronze(db_path, data_dir)
    if layer in {"silver", "all"}:
        result["silver"] = build_silver(data_dir)
    if layer in {"gold", "all"}:
        result["gold"] = build_gold(data_dir)
    return result
