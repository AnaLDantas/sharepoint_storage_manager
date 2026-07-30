"""Coleta rapida de storage + versoes por site via Get-SPOSite.

Diferente do report job (assincrono, dias), o Get-SPOSite devolve na hora as
propriedades mantidas pelo servico: StorageUsageCurrent, VersionCount e
VersionSize. Percorre todos os sites com o seu login; sites sem acesso ficam
com valores null e status "failed", sem interromper a coleta.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from config import Settings
from database import InventoryDatabase

LOGGER = logging.getLogger(__name__)

_SCRIPT = Path(__file__).resolve().parent / "scripts" / "spo_storage_metrics.ps1"
_MB = 1024 * 1024


def _derive_admin_url(settings: Settings, sample_site_url: str) -> str:
    """Usa SPO_ADMIN_URL se definido; senao deriva de um site (contoso -> contoso-admin)."""
    if settings.spo_admin_url:
        return settings.spo_admin_url.rstrip("/")
    host = urlparse(sample_site_url).netloc
    if host.endswith(".sharepoint.com") and "-admin.sharepoint.com" not in host:
        admin_host = host.replace(".sharepoint.com", "-admin.sharepoint.com", 1)
        return f"https://{admin_host}"
    raise ValueError(
        "Nao foi possivel derivar a URL de admin do SharePoint. Defina SPO_ADMIN_URL no .env "
        "(ex.: https://suaempresa-admin.sharepoint.com)."
    )


def _to_bytes_from_mb(value) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * _MB))
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def collect_storage_metrics(settings: Settings, db: InventoryDatabase) -> dict:
    """Coleta StorageUsageCurrent/VersionCount/VersionSize de todos os sites."""
    if not _SCRIPT.exists():
        raise FileNotFoundError(f"Script nao encontrado: {_SCRIPT}")

    sites = db.all_sites()
    if not sites:
        LOGGER.info("Nenhum site descoberto ainda. Rode o crawl antes.")
        return {"ok": 0, "failed": 0, "total": 0}

    admin_url = _derive_admin_url(settings, sites[0]["web_url"])
    payload = [dict(site_id=r["id"], site_url=r["web_url"]) for r in sites]

    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "in.json"
        out_path = Path(tmp) / "out.json"
        in_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cmd = [
            settings.pwsh_path, "-NoProfile", "-File", str(_SCRIPT),
            "-InputPath", str(in_path),
            "-OutputPath", str(out_path),
            "-AdminUrl", admin_url,
        ]
        LOGGER.info("Coletando storage/versoes de %s site(s) via Get-SPOSite (admin: %s)...", len(payload), admin_url)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            LOGGER.error("pwsh retornou %s. stderr:\n%s", proc.returncode, proc.stderr.strip())
            raise RuntimeError(f"Falha ao executar o script SPO (codigo {proc.returncode}).")
        if not out_path.exists():
            raise RuntimeError(f"Script SPO nao gerou saida. stdout:\n{proc.stdout.strip()}")

        raw = json.loads(out_path.read_text(encoding="utf-8-sig"))
        results = raw if isinstance(raw, list) else [raw]

    ok = failed = 0
    for res in results:
        status = res.get("status", "failed")
        db.upsert_site_storage_metric(
            site_id=res.get("site_id"),
            site_url=res.get("site_url"),
            storage_used_bytes=_to_bytes_from_mb(res.get("storage_used_mb")),
            version_count=_to_int(res.get("version_count")),
            version_size_bytes=_to_int(res.get("version_size_bytes")),
            status=status,
            message=res.get("message"),
        )
        if status == "ok":
            ok += 1
        else:
            failed += 1
    LOGGER.info("Storage/versoes coletados -> ok: %s | falhas (sem acesso/erro): %s", ok, failed)
    return {"ok": ok, "failed": failed, "total": len(results)}
