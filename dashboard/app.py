from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st


ROOT_DIR = Path(__file__).resolve().parents[1]
EXPORTS_DIR = ROOT_DIR / "exports"
INVENTORY_PARQUET = EXPORTS_DIR / "inventory.parquet"
SITE_PRIORITY_CSV = EXPORTS_DIR / "site_priority.csv"
BYTES_IN_GB = 1024**3


st.set_page_config(
    page_title="SharePoint Storage Dashboard",
    page_icon=":bar_chart:",
    layout="wide",
)


def format_gb(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "0.00 GB"
    return f"{float(value) / BYTES_IN_GB:,.2f} GB"


def format_number(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "0"
    return f"{int(value):,}".replace(",", ".")


@st.cache_data(show_spinner=False)
def load_site_priority() -> pd.DataFrame:
    if not SITE_PRIORITY_CSV.exists():
        return pd.DataFrame()

    df = pd.read_csv(SITE_PRIORITY_CSV, encoding="utf-8-sig")
    if "last_activity_date" in df.columns:
        df["last_activity_date"] = pd.to_datetime(df["last_activity_date"], errors="coerce")
    if "storage_used_bytes" in df.columns:
        df["storage_used_gb"] = df["storage_used_bytes"] / BYTES_IN_GB
    return df


@st.cache_data(show_spinner=False)
def load_site_names() -> list[str]:
    if not INVENTORY_PARQUET.exists():
        return []

    query = f"""
        SELECT DISTINCT site_name
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE site_name IS NOT NULL AND site_name <> ''
        ORDER BY site_name
    """
    return duckdb.sql(query).df()["site_name"].tolist()


def site_filter_clause(selected_sites: list[str]) -> str:
    if not selected_sites:
        return ""
    escaped = ", ".join("'" + site.replace("'", "''") + "'" for site in selected_sites)
    return f"AND site_name IN ({escaped})"


@st.cache_data(show_spinner=False)
def load_overview(selected_sites: tuple[str, ...]) -> dict[str, int]:
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            COUNT(DISTINCT site_url) AS sites,
            COUNT(DISTINCT biblioteca) AS libraries,
            COUNT_IF(tipo_item = 'file') AS files,
            COUNT_IF(tipo_item = 'folder') AS folders,
            COALESCE(SUM(CASE WHEN tipo_item = 'file' THEN tamanho_bytes ELSE 0 END), 0) AS total_bytes
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE 1=1
        {where_sites}
    """
    return duckdb.sql(query).df().iloc[0].to_dict()


@st.cache_data(show_spinner=False)
def load_top_sites(selected_sites: tuple[str, ...]) -> pd.DataFrame:
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            site_name,
            site_url,
            COUNT_IF(tipo_item = 'file') AS file_count,
            COALESCE(SUM(CASE WHEN tipo_item = 'file' THEN tamanho_bytes ELSE 0 END), 0) AS storage_bytes
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE 1=1
        {where_sites}
        GROUP BY site_name, site_url
        ORDER BY storage_bytes DESC
        LIMIT 25
    """
    df = duckdb.sql(query).df()
    df["storage_gb"] = df["storage_bytes"] / BYTES_IN_GB
    return df


@st.cache_data(show_spinner=False)
def load_top_extensions(selected_sites: tuple[str, ...]) -> pd.DataFrame:
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS extension,
            COUNT(*) AS file_count,
            COALESCE(SUM(tamanho_bytes), 0) AS storage_bytes
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE tipo_item = 'file'
        {where_sites}
        GROUP BY extension
        ORDER BY storage_bytes DESC
        LIMIT 20
    """
    df = duckdb.sql(query).df()
    df["storage_gb"] = df["storage_bytes"] / BYTES_IN_GB
    return df


@st.cache_data(show_spinner=False)
def load_unused_files(selected_sites: tuple[str, ...], cutoff: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    where_sites = site_filter_clause(list(selected_sites))
    cutoff_text = cutoff.isoformat()
    base_where = f"""
        tipo_item = 'file'
        AND TRY_CAST(data_modificacao AS TIMESTAMP) < TIMESTAMP '{cutoff_text}'
        {where_sites}
    """

    by_site_query = f"""
        SELECT
            site_name,
            site_url,
            COUNT(*) AS old_file_count,
            COALESCE(SUM(tamanho_bytes), 0) AS storage_bytes
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE {base_where}
        GROUP BY site_name, site_url
        ORDER BY storage_bytes DESC
        LIMIT 25
    """
    sample_query = f"""
        SELECT
            site_name,
            biblioteca,
            nome_arquivo_ou_pasta,
            extensao,
            tamanho_bytes,
            data_modificacao,
            caminho_item
        FROM read_parquet('{INVENTORY_PARQUET.as_posix()}')
        WHERE {base_where}
        ORDER BY tamanho_bytes DESC
        LIMIT 100
    """
    by_site = duckdb.sql(by_site_query).df()
    sample = duckdb.sql(sample_query).df()
    by_site["storage_gb"] = by_site["storage_bytes"] / BYTES_IN_GB
    sample["storage_gb"] = sample["tamanho_bytes"] / BYTES_IN_GB
    return by_site, sample


def classify_activity(last_activity: pd.Timestamp | pd.NaT) -> str:
    if pd.isna(last_activity):
        return "Sem atividade registrada"
    days = (pd.Timestamp.today().normalize() - last_activity).days
    if days > 180:
        return "Inativo"
    if days > 90:
        return "Pouco ativo"
    return "Ativo"


def main() -> None:
    st.title("SharePoint Storage Dashboard")
    st.caption("Analise de armazenamento, arquivos antigos, extensoes e sites inativos.")

    if not INVENTORY_PARQUET.exists():
        st.error(f"Arquivo nao encontrado: {INVENTORY_PARQUET}")
        st.stop()

    site_names = load_site_names()
    selected_sites = st.sidebar.multiselect(
        "Filtrar sites",
        options=site_names,
        placeholder="Todos os sites",
    )
    selected_sites_tuple = tuple(selected_sites)
    cutoff = (pd.Timestamp.today().normalize() - pd.DateOffset(months=6)).date()

    overview = load_overview(selected_sites_tuple)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Armazenamento analisado", format_gb(overview["total_bytes"]))
    col2.metric("Sites", format_number(overview["sites"]))
    col3.metric("Arquivos", format_number(overview["files"]))
    col4.metric("Bibliotecas", format_number(overview["libraries"]))

    st.divider()

    left, right = st.columns((1.15, 0.85))
    top_sites = load_top_sites(selected_sites_tuple)
    with left:
        st.subheader("Sites por armazenamento")
        fig = px.bar(
            top_sites.head(15).sort_values("storage_gb"),
            x="storage_gb",
            y="site_name",
            orientation="h",
            labels={"storage_gb": "GB", "site_name": "Site"},
            text_auto=".1f",
        )
        fig.update_layout(height=520, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Top sites")
        st.dataframe(
            top_sites.assign(storage_gb=top_sites["storage_gb"].round(2))[
                ["site_name", "storage_gb", "file_count", "site_url"]
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.divider()

    extensions = load_top_extensions(selected_sites_tuple)
    col_ext_chart, col_ext_table = st.columns((1, 1))
    with col_ext_chart:
        st.subheader("Extensoes que mais ocupam espaco")
        fig = px.treemap(
            extensions,
            path=["extension"],
            values="storage_bytes",
            color="file_count",
            color_continuous_scale="Tealrose",
        )
        fig.update_layout(height=460, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with col_ext_table:
        st.subheader("Ranking por extensao")
        st.dataframe(
            extensions.assign(storage_gb=extensions["storage_gb"].round(2))[
                ["extension", "storage_gb", "file_count"]
            ],
            hide_index=True,
            use_container_width=True,
        )

    st.divider()

    unused_by_site, unused_sample = load_unused_files(selected_sites_tuple, cutoff)
    st.subheader("Arquivos em desuso")
    st.caption(f"Criterio: arquivos sem modificacao desde antes de {cutoff.strftime('%d/%m/%Y')}.")

    old_col1, old_col2 = st.columns((1, 1))
    with old_col1:
        fig = px.bar(
            unused_by_site.head(15).sort_values("storage_gb"),
            x="storage_gb",
            y="site_name",
            orientation="h",
            labels={"storage_gb": "GB", "site_name": "Site"},
            text_auto=".1f",
        )
        fig.update_layout(height=460, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with old_col2:
        st.dataframe(
            unused_by_site.assign(storage_gb=unused_by_site["storage_gb"].round(2))[
                ["site_name", "storage_gb", "old_file_count", "site_url"]
            ],
            hide_index=True,
            use_container_width=True,
        )

    with st.expander("Ver maiores arquivos antigos"):
        st.dataframe(
            unused_sample.assign(storage_gb=unused_sample["storage_gb"].round(2))[
                [
                    "site_name",
                    "biblioteca",
                    "nome_arquivo_ou_pasta",
                    "extensao",
                    "storage_gb",
                    "data_modificacao",
                    "caminho_item",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )

    priority = load_site_priority()
    if not priority.empty:
        st.divider()
        st.subheader("Sites inativos")
        inactive = priority.copy()
        inactive["activity_status"] = inactive["last_activity_date"].apply(classify_activity)
        inactive = inactive.sort_values(["activity_status", "storage_used_bytes"], ascending=[False, False])
        st.dataframe(
            inactive[
                [
                    "rank",
                    "owner",
                    "site_url",
                    "last_activity_date",
                    "activity_status",
                    "storage_used_formatted",
                    "file_count",
                    "active_file_count",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Para exibir sites inativos, gere o arquivo exports/site_priority.csv com: python main.py prioritize-sites --period D180")


if __name__ == "__main__":
    main()
