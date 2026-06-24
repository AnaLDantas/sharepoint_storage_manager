from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from database import format_size
from graph_client import GraphClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SitePriority:
    rank: int
    site_id: str
    site_url: str
    owner: str
    last_activity_date: str
    file_count: int
    active_file_count: int
    storage_used_bytes: int
    storage_allocated_bytes: int
    root_web_template: str
    report_refresh_date: str
    report_period: str

    @property
    def storage_used_formatted(self) -> str:
        return format_size(self.storage_used_bytes)

    @property
    def storage_allocated_formatted(self) -> str:
        return format_size(self.storage_allocated_bytes)

    def as_row(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "site_id": self.site_id,
            "site_url": self.site_url,
            "owner": self.owner,
            "last_activity_date": self.last_activity_date,
            "file_count": self.file_count,
            "active_file_count": self.active_file_count,
            "storage_used_bytes": self.storage_used_bytes,
            "storage_used_formatted": self.storage_used_formatted,
            "storage_allocated_bytes": self.storage_allocated_bytes,
            "storage_allocated_formatted": self.storage_allocated_formatted,
            "root_web_template": self.root_web_template,
            "report_refresh_date": self.report_refresh_date,
            "report_period": self.report_period,
        }


def _int_value(value: str | None) -> int:
    if not value:
        return 0
    cleaned = value.strip().replace(",", "")
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def _field(row: dict[str, str], name: str) -> str:
    return (row.get(name) or "").strip()


def _normalize_header(value: str) -> str:
    return value.strip().lstrip("\ufeff").lower().replace(" ", "").replace("_", "")


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {_normalize_header(key): value for key, value in row.items() if key is not None}


def _nfield(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(_normalize_header(name))
        if value:
            return value.strip()
    return ""


def parse_usage_report(csv_text: str) -> list[SitePriority]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows: list[SitePriority] = []
    for raw_row in reader:
        row = _normalize_row(raw_row)
        site_url = _nfield(row, "Site URL", "Site Url", "URL do Site")
        is_deleted = _nfield(row, "Is Deleted", "Deleted").lower() == "true"
        site_id = _nfield(row, "Site Id", "Site ID")
        if (not site_url and not site_id) or is_deleted:
            continue
        rows.append(
            SitePriority(
                rank=0,
                site_id=site_id,
                site_url=site_url,
                owner=_nfield(row, "Owner Display Name") or _nfield(row, "Owner Principal Name"),
                last_activity_date=_nfield(row, "Last Activity Date"),
                file_count=_int_value(_nfield(row, "File Count")),
                active_file_count=_int_value(_nfield(row, "Active File Count")),
                storage_used_bytes=_int_value(_nfield(row, "Storage Used (Byte)", "Storage Used Bytes")),
                storage_allocated_bytes=_int_value(_nfield(row, "Storage Allocated (Byte)", "Storage Allocated Bytes")),
                root_web_template=_nfield(row, "Root Web Template"),
                report_refresh_date=_nfield(row, "Report Refresh Date"),
                report_period=_nfield(row, "Report Period"),
            )
        )

    ordered = sorted(rows, key=lambda item: item.storage_used_bytes, reverse=True)
    return [
        SitePriority(
            rank=index,
            site_id=item.site_id,
            site_url=item.site_url,
            owner=item.owner,
            last_activity_date=item.last_activity_date,
            file_count=item.file_count,
            active_file_count=item.active_file_count,
            storage_used_bytes=item.storage_used_bytes,
            storage_allocated_bytes=item.storage_allocated_bytes,
            root_web_template=item.root_web_template,
            report_refresh_date=item.report_refresh_date,
            report_period=item.report_period,
        )
        for index, item in enumerate(ordered, start=1)
    ]


def _csv_fields() -> list[str]:
    return [
        "rank",
        "site_id",
        "site_url",
        "owner",
        "last_activity_date",
        "file_count",
        "active_file_count",
        "storage_used_bytes",
        "storage_used_formatted",
        "storage_allocated_bytes",
        "storage_allocated_formatted",
        "root_web_template",
        "report_refresh_date",
        "report_period",
    ]


def _write_priority_csv(sites: list[SitePriority], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=_csv_fields())
        writer.writeheader()
        for site in sites:
            writer.writerow(site.as_row())


def _write_ids(sites: list[SitePriority], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as handle:
        for site in sites:
            if site.site_id:
                handle.write(f"{site.site_id}\n")


def _write_json(data: dict[str, Any] | list[dict[str, Any]], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_raw_report(csv_text: str, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(csv_text, encoding="utf-8-sig")


def _group_summary(name: str, label: str, sites: list[SitePriority]) -> dict[str, Any]:
    total_bytes = sum(site.storage_used_bytes for site in sites)
    return {
        "name": name,
        "label": label,
        "site_count": len(sites),
        "storage_used_bytes": total_bytes,
        "storage_used_formatted": format_size(total_bytes),
        "sites": [site.as_row() for site in sites],
    }


def _site_url_map_from_graph(graph: GraphClient) -> dict[str, str]:
    url_by_id: dict[str, str] = {}
    for site in graph.list_sites("*"):
        web_url = site.get("webUrl") or ""
        if not web_url:
            continue
        graph_site_id = site.get("id") or ""
        if graph_site_id:
            url_by_id[graph_site_id.lower()] = web_url
            parts = graph_site_id.split(",")
            if parts:
                url_by_id[parts[-1].lower()] = web_url
        sharepoint_ids = site.get("sharepointIds") or {}
        for key in ("siteId", "siteCollectionId"):
            value = sharepoint_ids.get(key)
            if value:
                url_by_id[str(value).lower()] = web_url
    return url_by_id


def _enrich_missing_urls(graph: GraphClient, sites: list[SitePriority]) -> list[SitePriority]:
    if not any(site.site_id and not site.site_url for site in sites):
        return sites
    LOGGER.info("Relatorio veio sem Site URL para alguns sites. Tentando mapear URLs via /sites?search=*")
    url_by_id = _site_url_map_from_graph(graph)
    enriched: list[SitePriority] = []
    for site in sites:
        site_url = site.site_url or url_by_id.get(site.site_id.lower(), "")
        enriched.append(
            SitePriority(
                rank=site.rank,
                site_id=site.site_id,
                site_url=site_url,
                owner=site.owner,
                last_activity_date=site.last_activity_date,
                file_count=site.file_count,
                active_file_count=site.active_file_count,
                storage_used_bytes=site.storage_used_bytes,
                storage_allocated_bytes=site.storage_allocated_bytes,
                root_web_template=site.root_web_template,
                report_refresh_date=site.report_refresh_date,
                report_period=site.report_period,
            )
        )
    return enriched


def generate_site_priority(
    graph: GraphClient,
    output_dir: Path,
    period: str = "D7",
    over_1tb_threshold_bytes: int = 1024**4,
    over_500gb_threshold_bytes: int = 500 * 1024**3,
    over_100gb_threshold_bytes: int = 100 * 1024**3,
) -> dict[str, Any]:
    LOGGER.info("Baixando relatorio SharePoint Site Usage Detail (%s)", period)
    csv_text = graph.get_sharepoint_site_usage_detail_csv(period)
    _write_raw_report(csv_text, output_dir / "sharepoint_site_usage_detail_raw.csv")
    raw_reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    raw_rows = list(raw_reader)
    raw_headers = raw_reader.fieldnames or []
    sites = _enrich_missing_urls(graph, parse_usage_report(csv_text))

    over_1tb = [site for site in sites if site.storage_used_bytes >= over_1tb_threshold_bytes]
    over_500gb = [
        site
        for site in sites
        if over_500gb_threshold_bytes <= site.storage_used_bytes < over_1tb_threshold_bytes
    ]
    over_100gb = [
        site
        for site in sites
        if over_100gb_threshold_bytes <= site.storage_used_bytes < over_500gb_threshold_bytes
    ]
    under_100gb = [site for site in sites if site.storage_used_bytes < over_100gb_threshold_bytes]

    groups = {
        "over_1tb": _group_summary("over_1tb", ">= 1 TB", over_1tb),
        "over_500gb_to_1tb": _group_summary("over_500gb_to_1tb", ">= 500 GB e < 1 TB", over_500gb),
        "over_100gb_to_500gb": _group_summary("over_100gb_to_500gb", ">= 100 GB e < 500 GB", over_100gb),
        "under_100gb": _group_summary("under_100gb", "< 100 GB", under_100gb),
    }

    _write_priority_csv(sites, output_dir / "site_priority.csv")
    _write_ids(sites, output_dir / "site_ids_priority_order.txt")
    _write_ids(over_1tb, output_dir / "site_ids_over_1tb.txt")
    _write_ids(over_500gb, output_dir / "site_ids_500gb_to_1tb.txt")
    _write_ids(over_100gb, output_dir / "site_ids_100gb_to_500gb.txt")
    _write_ids(under_100gb, output_dir / "site_ids_under_100gb.txt")

    _write_json([site.as_row() for site in sites], output_dir / "site_priority.json")
    _write_json(groups, output_dir / "site_priority_groups.json")
    for group_name, group in groups.items():
        _write_json(group, output_dir / f"{group_name}.json")

    total_bytes = sum(site.storage_used_bytes for site in sites)
    result = {
        "period": period,
        "raw_report_rows": len(raw_rows),
        "raw_report_headers": raw_headers,
        "sites": len(sites),
        "sites_with_url": sum(1 for site in sites if site.site_url),
        "sites_without_url": sum(1 for site in sites if not site.site_url),
        "over_1tb_sites": len(over_1tb),
        "over_500gb_to_1tb_sites": len(over_500gb),
        "over_100gb_to_500gb_sites": len(over_100gb),
        "under_100gb_sites": len(under_100gb),
        "total_storage_used_bytes": total_bytes,
        "total_storage_used_formatted": format_size(total_bytes),
        "output_dir": str(output_dir),
    }
    LOGGER.info("Priorizacao concluida: %s", result)
    return result
