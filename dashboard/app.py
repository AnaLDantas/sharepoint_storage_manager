from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st


ROOT_DIR = Path(__file__).resolve().parents[1]
CLIENTS_DIR = ROOT_DIR / "clients"
BYTES_IN_GB = 1024**3
TOP_SITE_CHART_LIMIT = 10
SITE_LABEL_MAX_CHARS = 32


st.set_page_config(
    page_title="SharePoint Storage Dashboard",
    page_icon=":bar_chart:",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@dataclass(frozen=True)
class ClientWorkspace:
    name: str
    root_dir: Path
    inventory_parquet: Path
    gold_dir: Path
    site_priority_csv: Path
    sqlite_db: Path


def workspace_from_root(name: str, root_dir: Path) -> ClientWorkspace:
    exports_dir = root_dir / "exports"
    parquet_datasets = sorted(
        (path for path in exports_dir.glob("inventory_parquet*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    parquet_dataset = parquet_datasets[0] if parquet_datasets else exports_dir / "inventory_parquet"
    return ClientWorkspace(
        name=name,
        root_dir=root_dir,
        inventory_parquet=parquet_dataset,
        gold_dir=root_dir / "data" / "gold",
        site_priority_csv=root_dir / "exports" / "site_priority.csv",
        sqlite_db=root_dir / "inventory" / "sharepoint_inventory.sqlite3",
    )


def discover_client_workspaces() -> list[ClientWorkspace]:
    workspaces = [workspace_from_root("Cliente atual", ROOT_DIR)]
    if CLIENTS_DIR.exists():
        for client_dir in sorted(path for path in CLIENTS_DIR.iterdir() if path.is_dir()):
            workspace = workspace_from_root(client_dir.name, client_dir)
            has_data = (
                workspace.inventory_parquet.exists()
                or (workspace.gold_dir / "storage_kpis.parquet").exists()
                or workspace.site_priority_csv.exists()
                or workspace.sqlite_db.exists()
            )
            if has_data:
                workspaces.append(workspace)
    return workspaces


def apply_styles() -> None:
    st.markdown(
        """
        <style>
            .block-container {
                padding-top: 2.2rem;
                padding-bottom: 3rem;
            }

            [data-testid="stMetric"] {
                background: rgba(255, 255, 255, 0.035);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 1rem 1.1rem;
            }

            [data-testid="stMetricLabel"] {
                color: rgba(250, 250, 250, 0.72);
            }

            [data-testid="stSidebar"] {
                border-right: 1px solid rgba(255, 255, 255, 0.08);
            }

            div[data-testid="stPopover"] button {
                border-radius: 999px;
            }

            h1, h2, h3 {
                letter-spacing: 0;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def format_gb(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "0.00 GB"
    return f"{float(value) / BYTES_IN_GB:,.2f} GB"


def format_number(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "0"
    return f"{int(value):,}".replace(",", ".")


def shorten_label(value: str | None, max_chars: int = SITE_LABEL_MAX_CHARS) -> str:
    if not value or pd.isna(value):
        return "(sem nome)"
    text = str(value).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}..."


def display_site_name(site_name: str | None, site_url: str | None = None) -> str:
    name = "" if not site_name or pd.isna(site_name) else str(site_name).strip()
    url = "" if not site_url or pd.isna(site_url) else str(site_url).strip()
    if name and "," not in name:
        return name
    if url:
        parsed = urlparse(url)
        path_name = parsed.path.rstrip("/").split("/")[-1]
        return path_name or parsed.netloc or name or "(sem nome)"
    return name or "(sem nome)"


def parquet_scan_path(inventory_parquet: str) -> str:
    parquet_path = Path(inventory_parquet)
    return (parquet_path / "*.parquet").as_posix()


@st.cache_data(show_spinner=False)
def load_gold_table(gold_dir: str, table_name: str) -> pd.DataFrame:
    path = Path(gold_dir) / f"{table_name}.parquet"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def render_gold_dashboard(workspace: ClientWorkspace) -> None:
    st.title("SharePoint Storage Dashboard")
    st.caption("Camada Gold Lakehouse")

    storage_kpis = load_gold_table(str(workspace.gold_dir), "storage_kpis")
    top_sites = load_gold_table(str(workspace.gold_dir), "top_sites")
    top_extensions = load_gold_table(str(workspace.gold_dir), "top_extensions")
    savings = load_gold_table(str(workspace.gold_dir), "storage_savings")

    if storage_kpis.empty:
        st.error(f"Camada Gold nao encontrada em: {workspace.gold_dir}")
        st.stop()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Armazenamento analisado", f"{storage_kpis['storage_gb'].sum():,.2f} GB")
    col2.metric("Sites", format_number(storage_kpis["site_name"].nunique()))
    col3.metric("Arquivos", format_number(storage_kpis["total_files"].sum()))
    col4.metric("Potencial de arquivo", f"{storage_kpis['archive_candidate_gb'].sum():,.2f} GB")

    st.divider()

    left, right = st.columns((1.15, 0.85))
    with left:
        st.subheader("Top sites por armazenamento")
        fig = px.bar(
            top_sites.head(10).sort_values("storage_gb"),
            x="storage_gb",
            y="site_name",
            orientation="h",
            labels={"storage_gb": "GB", "site_name": ""},
            text_auto=".1f",
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=20, b=20), yaxis=dict(tickfont=dict(size=12)))
        st.plotly_chart(fig, width="stretch")

    with right:
        st.subheader("Economia estimada")
        st.dataframe(
            savings.head(25).round({"current_storage_gb": 2, "archive_candidate_gb": 2, "estimated_saving_pct": 2}),
            hide_index=True,
            width="stretch",
        )

    st.divider()
    st.subheader("Top extensoes")
    st.dataframe(
        top_extensions.head(50).round({"storage_gb": 2}),
        hide_index=True,
        width="stretch",
    )


def prepare_site_chart_data(df: pd.DataFrame, value_column: str, limit: int = TOP_SITE_CHART_LIMIT) -> pd.DataFrame:
    chart = df.sort_values(value_column, ascending=False).head(limit).copy()
    chart["rank"] = range(1, len(chart) + 1)
    chart["site_display_name"] = chart.apply(
        lambda row: display_site_name(row.get("site_name"), row.get("site_url")),
        axis=1,
    )
    chart["site_label"] = chart.apply(
        lambda row: f"{int(row['rank']):02d}. {shorten_label(row['site_display_name'])}",
        axis=1,
    )
    return chart.sort_values(value_column)


def site_id_variants(site_id: str | None) -> list[str]:
    if not site_id or pd.isna(site_id):
        return []
    value = str(site_id).strip().lower()
    if not value:
        return []
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    return list(dict.fromkeys([value, *parts]))


@st.cache_data(show_spinner=False)
def load_site_url_lookup(sqlite_db: str) -> pd.DataFrame:
    db_path = Path(sqlite_db)
    if not db_path.exists():
        return pd.DataFrame(columns=["site_id", "site_name", "site_url"])

    conn = sqlite3.connect(db_path)
    try:
        sites = pd.read_sql_query("SELECT id, name, web_url FROM sites", conn)
    finally:
        conn.close()

    rows: list[dict[str, str]] = []
    for row in sites.itertuples(index=False):
        for variant in site_id_variants(row.id):
            rows.append(
                {
                    "site_id": variant,
                    "site_name": row.name or "",
                    "site_url": row.web_url or "",
                }
            )
    return pd.DataFrame(rows).drop_duplicates("site_id")


@st.cache_data(show_spinner=False)
def load_storage_metrics(sqlite_db: str) -> pd.DataFrame:
    """Le site_storage_metrics (Get-SPOSite): storage total, versoes e tamanho."""
    cols = ["site_id", "site_name", "site_url", "storage_used_bytes", "version_count", "version_size_bytes"]
    db_path = Path(sqlite_db)
    if not db_path.exists():
        return pd.DataFrame(columns=cols)
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='site_storage_metrics'"
        ).fetchone()
        if not exists:
            return pd.DataFrame(columns=cols)
        df = pd.read_sql_query(
            """
            SELECT m.site_id, COALESCE(s.name, m.site_url) AS site_name, m.site_url,
                   m.storage_used_bytes, m.version_count, m.version_size_bytes
            FROM site_storage_metrics m
            LEFT JOIN sites s ON s.id = m.site_id
            WHERE m.status = 'ok'
            """,
            conn,
        )
    finally:
        conn.close()
    for column in ["storage_used_bytes", "version_count", "version_size_bytes"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def render_versioning_section(workspace: ClientWorkspace, selected_sites: list[str]) -> None:
    metrics = load_storage_metrics(str(workspace.sqlite_db))
    st.subheader("Versionamento e uso real (tenant)")
    st.caption(
        "Storage Used vem do Get-SPOSite (inclui versoes, metadados e lixeira) e bate com o admin center. "
        "O 'Armazenamento analisado' no topo e a soma dos streams da versao atual coletados pelo crawler."
    )
    if metrics.empty:
        st.info("Rode `python main.py storage --collect` para coletar StorageUsageCurrent, VersionCount e VersionSize por site.")
        return
    if selected_sites:
        metrics = metrics[metrics["site_name"].isin(selected_sites)]
    if metrics.empty:
        st.info("Nenhuma metrica de versao para os sites filtrados.")
        return

    total_storage = float(metrics["storage_used_bytes"].sum())
    total_versions = float(metrics["version_count"].sum())
    total_version_bytes = float(metrics["version_size_bytes"].sum())
    pct = (total_version_bytes / total_storage * 100) if total_storage else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Storage Used (tenant)", format_gb(total_storage))
    c2.metric("Espaco de versoes", format_gb(total_version_bytes))
    c3.metric("Versoes armazenadas", format_number(total_versions))
    c4.metric("% em versoes", f"{pct:.1f}%")

    left, right = st.columns((1.15, 0.85))
    with left:
        st.markdown("**Top sites por espaco de versoes**")
        top = metrics.sort_values("version_size_bytes", ascending=False).head(TOP_SITE_CHART_LIMIT).copy()
        top["version_gb"] = top["version_size_bytes"] / BYTES_IN_GB
        top["site_label"] = top["site_name"].map(shorten_label)
        fig = px.bar(
            top,
            x="version_gb",
            y="site_label",
            orientation="h",
            labels={"version_gb": "GB", "site_label": ""},
            text_auto=".1f",
            custom_data=["site_name", "version_count", "site_url"],
        )
        fig.update_traces(
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Versoes: %{customdata[1]:,}<br>"
                "Espaco: %{x:.2f} GB<br>"
                "%{customdata[2]}<extra></extra>"
            )
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=20, b=20), yaxis=dict(tickfont=dict(size=12)))
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, use_container_width=True)

    with right:
        st.markdown("**Detalhe por site**")
        storage_b = metrics["storage_used_bytes"].astype("float64")
        version_b = metrics["version_size_bytes"].astype("float64")
        table = metrics.assign(
            storage_gb=(storage_b / BYTES_IN_GB).round(2),
            versoes_gb=(version_b / BYTES_IN_GB).round(2),
            pct_versoes=((version_b / storage_b.where(storage_b > 0)) * 100).round(1),
        ).sort_values("versoes_gb", ascending=False)[
            ["site_name", "storage_gb", "versoes_gb", "version_count", "pct_versoes", "site_url"]
        ]
        st.dataframe(table, hide_index=True, use_container_width=True)


@st.cache_data(show_spinner=False)
def load_site_priority(site_priority_csv: str, sqlite_db: str) -> pd.DataFrame:
    priority_path = Path(site_priority_csv)
    if not priority_path.exists():
        return pd.DataFrame()

    df = pd.read_csv(priority_path, encoding="utf-8-sig")
    df["site_id_lookup"] = df["site_id"].astype(str).str.strip().str.lower()

    lookup = load_site_url_lookup(sqlite_db)
    if not lookup.empty:
        lookup = lookup.rename(columns={"site_id": "site_id_key"})
        df = df.merge(lookup, how="left", left_on="site_id_lookup", right_on="site_id_key", suffixes=("", "_lookup"))
        df["site_url"] = df["site_url"].fillna("").astype(str)
        df["site_url"] = df["site_url"].mask(df["site_url"].isin(["", "nan", "None"]), df["site_url_lookup"].fillna(""))
        df["site_name"] = df.get("site_name", pd.Series(dtype="object")).fillna("")
    else:
        df["site_name"] = ""

    if "last_activity_date" in df.columns:
        df["last_activity_date"] = pd.to_datetime(df["last_activity_date"], errors="coerce")
    if "storage_used_bytes" in df.columns:
        df["storage_used_gb"] = df["storage_used_bytes"] / BYTES_IN_GB
    df = df.drop(columns=[column for column in ["site_id_lookup", "site_id_key", "site_url_lookup"] if column in df.columns])
    return df


@st.cache_data(show_spinner=False)
def load_site_names(inventory_parquet: str) -> list[str]:
    parquet_path = Path(inventory_parquet)
    if not parquet_path.exists():
        return []
    parquet_scan = parquet_scan_path(inventory_parquet)

    query = f"""
        SELECT DISTINCT site_name
        FROM read_parquet('{parquet_scan}')
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
def load_overview(selected_sites: tuple[str, ...], inventory_parquet: str) -> dict[str, int]:
    parquet_scan = parquet_scan_path(inventory_parquet)
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            COUNT(DISTINCT site_url) AS sites,
            COUNT(DISTINCT biblioteca) AS libraries,
            COUNT_IF(tipo_item = 'file') AS files,
            COUNT_IF(tipo_item = 'folder') AS folders,
            COALESCE(SUM(CASE WHEN tipo_item = 'file' THEN tamanho_bytes ELSE 0 END), 0) AS total_bytes
        FROM read_parquet('{parquet_scan}')
        WHERE 1=1
        {where_sites}
    """
    return duckdb.sql(query).df().iloc[0].to_dict()


@st.cache_data(show_spinner=False)
def load_top_sites(selected_sites: tuple[str, ...], inventory_parquet: str) -> pd.DataFrame:
    parquet_scan = parquet_scan_path(inventory_parquet)
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            site_name,
            site_url,
            COUNT_IF(tipo_item = 'file') AS file_count,
            COALESCE(SUM(CASE WHEN tipo_item = 'file' THEN tamanho_bytes ELSE 0 END), 0) AS storage_bytes
        FROM read_parquet('{parquet_scan}')
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
def load_top_extensions(selected_sites: tuple[str, ...], inventory_parquet: str) -> pd.DataFrame:
    parquet_scan = parquet_scan_path(inventory_parquet)
    where_sites = site_filter_clause(list(selected_sites))
    query = f"""
        SELECT
            COALESCE(NULLIF(extensao, ''), '(sem extensao)') AS extension,
            COUNT(*) AS file_count,
            COALESCE(SUM(tamanho_bytes), 0) AS storage_bytes
        FROM read_parquet('{parquet_scan}')
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
def load_unused_files(selected_sites: tuple[str, ...], cutoff: date, inventory_parquet: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    parquet_scan = parquet_scan_path(inventory_parquet)
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
        FROM read_parquet('{parquet_scan}')
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
        FROM read_parquet('{parquet_scan}')
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


def classify_storage_limit(allocated_bytes: int | float | None) -> str:
    if allocated_bytes is None or pd.isna(allocated_bytes) or float(allocated_bytes) <= 0:
        return "Sem limite reportado"
    if float(allocated_bytes) >= 25 * 1024**4:
        return "Limite alto/padrao reportado"
    return "Limite especifico aparente"


def prepare_storage_limits_table(priority: pd.DataFrame) -> pd.DataFrame:
    table = priority.copy()
    table["site_display_name"] = table.apply(
        lambda row: display_site_name(row.get("site_name"), row.get("site_url")),
        axis=1,
    )
    table["storage_used_gb"] = table["storage_used_bytes"] / BYTES_IN_GB
    table["storage_allocated_gb"] = table["storage_allocated_bytes"] / BYTES_IN_GB
    table["quota_usage_percent"] = (
        table["storage_used_bytes"] / table["storage_allocated_bytes"].replace(0, pd.NA) * 100
    ).fillna(0)
    table["limit_status"] = table["storage_allocated_bytes"].apply(classify_storage_limit)
    return table.sort_values(["limit_status", "storage_allocated_bytes", "storage_used_bytes"], ascending=[True, False, False])


def main() -> None:
    apply_styles()

    workspaces = discover_client_workspaces()
    workspace_by_name = {workspace.name: workspace for workspace in workspaces}
    st.sidebar.header("Clientes")
    selected_client_name = st.sidebar.selectbox(
        "Base de dados",
        options=list(workspace_by_name),
        index=0,
    )
    workspace = workspace_by_name[selected_client_name]

    with st.sidebar.expander("Arquivos da base"):
        st.code(str(workspace.root_dir))

    if not workspace.inventory_parquet.exists():
        if (workspace.gold_dir / "storage_kpis.parquet").exists():
            render_gold_dashboard(workspace)
            return
        st.error(f"Arquivo nao encontrado: {workspace.inventory_parquet}")
        st.stop()

    site_names = load_site_names(str(workspace.inventory_parquet))
    header_left, header_right = st.columns((0.78, 0.22), vertical_alignment="top")
    with header_left:
        st.title("SharePoint Storage Dashboard")
        st.caption("Analise de armazenamento, arquivos antigos, extensoes e sites inativos.")

    with header_right:
        st.write("")
        with st.popover("Filtros", icon=":material/filter_list:", width="stretch"):
            selected_sites = st.multiselect(
                "Sites",
                options=site_names,
                placeholder="Todos os sites",
            )

    selected_sites_tuple = tuple(selected_sites)
    cutoff = (pd.Timestamp.today().normalize() - pd.DateOffset(years=1)).date()

    overview = load_overview(selected_sites_tuple, str(workspace.inventory_parquet))
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Armazenamento analisado", format_gb(overview["total_bytes"]))
    col2.metric("Sites", format_number(overview["sites"]))
    col3.metric("Arquivos", format_number(overview["files"]))
    col4.metric("Bibliotecas", format_number(overview["libraries"]))

    st.divider()

    left, right = st.columns((1.15, 0.85))
    top_sites = load_top_sites(selected_sites_tuple, str(workspace.inventory_parquet))
    with left:
        st.subheader("Top 10 sites por armazenamento")
        top_sites_chart = prepare_site_chart_data(top_sites, "storage_gb")
        fig = px.bar(
            top_sites_chart,
            x="storage_gb",
            y="site_label",
            orientation="h",
            labels={"storage_gb": "GB", "site_label": ""},
            text_auto=".1f",
            custom_data=["site_name", "site_url", "file_count"],
        )
        fig.update_traces(
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Armazenamento: %{x:.2f} GB<br>"
                "Arquivos: %{customdata[2]:,}<br>"
                "URL: %{customdata[1]}<extra></extra>"
            )
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=20, b=20), yaxis=dict(tickfont=dict(size=12)))
        st.plotly_chart(fig, width="stretch")

    with right:
        st.subheader("Top sites")
        st.dataframe(
            top_sites.assign(storage_gb=top_sites["storage_gb"].round(2))[
                ["site_name", "storage_gb", "file_count", "site_url"]
            ],
            hide_index=True,
            width="stretch",
        )

    st.divider()

    render_versioning_section(workspace, selected_sites)
    st.divider()

    priority = load_site_priority(str(workspace.site_priority_csv), str(workspace.sqlite_db))
    if not priority.empty:
        storage_limits = prepare_storage_limits_table(priority)
        st.subheader("Limites de armazenamento dos sites")
        st.caption("Baseado no campo Storage Allocated do relatorio de uso do Microsoft 365.")
        limit_col1, limit_col2, limit_col3 = st.columns(3)
        limit_col1.metric("Sites com limite reportado", format_number((storage_limits["storage_allocated_bytes"] > 0).sum()))
        limit_col2.metric("Sites sem limite reportado", format_number((storage_limits["storage_allocated_bytes"] <= 0).sum()))
        limit_col3.metric(
            "Sites com limite especifico aparente",
            format_number((storage_limits["limit_status"] == "Limite especifico aparente").sum()),
        )
        st.dataframe(
            storage_limits[
                [
                    "site_display_name",
                    "owner",
                    "storage_used_gb",
                    "storage_allocated_gb",
                    "quota_usage_percent",
                    "limit_status",
                    "site_url",
                ]
            ].round(
                {
                    "storage_used_gb": 2,
                    "storage_allocated_gb": 2,
                    "quota_usage_percent": 2,
                }
            ),
            column_config={
                "site_display_name": "Site",
                "owner": "Owner",
                "storage_used_gb": "Usado GB",
                "storage_allocated_gb": "Limite GB",
                "quota_usage_percent": "% usado",
                "limit_status": "Status do limite",
                "site_url": "URL",
            },
            hide_index=True,
            width="stretch",
        )
        st.divider()
    else:
        st.info("Para exibir limites de armazenamento, gere o arquivo exports/site_priority.csv com: python main.py prioritize-sites --period D180")
        st.divider()

    extensions = load_top_extensions(selected_sites_tuple, str(workspace.inventory_parquet))
    st.subheader("Analise por extensao")
    ext_storage_chart, ext_count_chart = st.columns((1, 1))
    with ext_storage_chart:
        fig = px.bar(
            extensions.sort_values("storage_gb", ascending=False).head(10).sort_values("storage_gb"),
            x="storage_gb",
            y="extension",
            orientation="h",
            labels={"storage_gb": "GB", "extension": ""},
            text_auto=".1f",
            custom_data=["file_count"],
            title="Extensoes que mais ocupam espaco",
        )
        fig.update_traces(
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Armazenamento: %{x:.2f} GB<br>"
                "Arquivos: %{customdata[0]:,}<extra></extra>"
            )
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=48, b=20), yaxis=dict(tickfont=dict(size=12)))
        st.plotly_chart(fig, width="stretch")

    with ext_count_chart:
        fig = px.bar(
            extensions.sort_values("file_count", ascending=False).head(10).sort_values("file_count"),
            x="file_count",
            y="extension",
            orientation="h",
            labels={"file_count": "Arquivos", "extension": ""},
            text_auto=True,
            custom_data=["storage_gb"],
            title="Extensoes por quantidade de arquivos",
        )
        fig.update_traces(
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Arquivos: %{x:,}<br>"
                "Armazenamento: %{customdata[0]:.2f} GB<extra></extra>"
            )
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=48, b=20), yaxis=dict(tickfont=dict(size=12)))
        st.plotly_chart(fig, width="stretch")

    st.markdown("#### Tabela por extensao")
    extension_table = extensions.copy()
    total_extension_bytes = extension_table["storage_bytes"].sum()
    extension_table["storage_gb"] = extension_table["storage_gb"].round(2)
    extension_table["avg_file_size_mb"] = (
        extension_table["storage_bytes"] / extension_table["file_count"].replace(0, pd.NA) / 1024**2
    ).fillna(0).round(2)
    extension_table["storage_percent"] = (
        extension_table["storage_bytes"] / total_extension_bytes * 100 if total_extension_bytes else 0
    ).round(2)
    st.dataframe(
        extension_table[
            ["extension", "storage_gb", "storage_percent", "file_count", "avg_file_size_mb"]
        ],
        column_config={
            "extension": "Extensao",
            "storage_gb": "GB",
            "storage_percent": "% do total",
            "file_count": "Arquivos",
            "avg_file_size_mb": "Media MB/arquivo",
        },
        hide_index=True,
        width="stretch",
    )

    st.divider()

    unused_by_site, unused_sample = load_unused_files(selected_sites_tuple, cutoff, str(workspace.inventory_parquet))
    st.subheader("Arquivos em desuso")
    st.caption(f"Criterio: arquivos sem modificacao ha mais de 1 ano, desde antes de {cutoff.strftime('%d/%m/%Y')}.")

    unused_total_bytes = unused_by_site["storage_bytes"].sum()
    unused_total_files = unused_by_site["old_file_count"].sum()
    unused_storage_percent = (unused_total_bytes / overview["total_bytes"] * 100) if overview["total_bytes"] else 0
    unused_metric_col1, unused_metric_col2, unused_metric_col3 = st.columns(3)
    unused_metric_col1.metric("Armazenamento em desuso", format_gb(unused_total_bytes))
    unused_metric_col2.metric("Arquivos em desuso", format_number(unused_total_files))
    unused_metric_col3.metric("% do armazenamento em desuso", f"{unused_storage_percent:.2f}%")

    old_col1, old_col2 = st.columns((1, 1))
    with old_col1:
        unused_chart = prepare_site_chart_data(unused_by_site, "storage_gb")
        fig = px.bar(
            unused_chart,
            x="storage_gb",
            y="site_label",
            orientation="h",
            labels={"storage_gb": "GB", "site_label": ""},
            text_auto=".1f",
            custom_data=["site_name", "site_url", "old_file_count"],
        )
        fig.update_traces(
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>"
                "Arquivos antigos: %{customdata[2]:,}<br>"
                "Armazenamento: %{x:.2f} GB<br>"
                "URL: %{customdata[1]}<extra></extra>"
            )
        )
        fig.update_layout(height=420, margin=dict(l=8, r=16, t=20, b=20), yaxis=dict(tickfont=dict(size=12)))
        st.plotly_chart(fig, width="stretch")

    with old_col2:
        st.dataframe(
            unused_by_site.assign(storage_gb=unused_by_site["storage_gb"].round(2))[
                ["site_name", "storage_gb", "old_file_count", "site_url"]
            ],
            hide_index=True,
            width="stretch",
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
            width="stretch",
        )

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
                    "site_name",
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
            width="stretch",
        )
    else:
        st.info("Para exibir sites inativos, gere o arquivo exports/site_priority.csv com: python main.py prioritize-sites --period D180")


if __name__ == "__main__":
    main()
