from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator
from urllib.parse import quote

import msal
import requests

from config import Settings

LOGGER = logging.getLogger(__name__)


class GraphError(RuntimeError):
    def __init__(self, status_code: int, message: str, url: str):
        super().__init__(f"Graph HTTP {status_code}: {message} ({url})")
        self.status_code = status_code
        self.url = url
        self.message = message


class GraphClient:
    """Cliente Microsoft Graph com retry, paginacao e protecao contra 429.

    Controle de 429:
    - Quando o Graph retorna Retry-After, o cliente dorme exatamente esse tempo.
    - Quando nao ha Retry-After, aplica exponential backoff com jitter.
    - 401/403/404 nao sao repetidos porque normalmente indicam permissao, escopo ou item removido.
    """

    RETRYABLE = {408, 429, 500, 502, 503, 504}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.retry_count = 0
        self.throttle_count = 0
        self._app = msal.ConfidentialClientApplication(
            client_id=settings.client_id,
            client_credential=settings.client_secret,
            authority=settings.authority,
        )

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Falha ao autenticar no Azure AD: {result}")
        self._token = result["access_token"]
        self._token_expires_at = time.time() + int(result.get("expires_in", 3600))
        return self._token

    def get(self, url_or_path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = url_or_path if url_or_path.startswith("http") else f"{self.settings.graph_base_url}{url_or_path}"
        attempt = 0
        while True:
            token = self._access_token()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.settings.request_timeout,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
            except requests.Timeout as exc:
                response = None
                status_code = 408
                message = str(exc)
            except requests.RequestException as exc:
                response = None
                status_code = 503
                message = str(exc)
            else:
                status_code = response.status_code
                message = response.text[:1000]

            if response is not None and 200 <= response.status_code < 300:
                if not response.content:
                    return {}
                return response.json()

            if status_code in {401, 403, 404}:
                raise GraphError(status_code, message, url)

            if status_code in self.RETRYABLE and attempt < 8:
                attempt += 1
                self.retry_count += 1
                if status_code == 429:
                    self.throttle_count += 1
                retry_after = response.headers.get("Retry-After") if response is not None else None
                delay = self._retry_delay(attempt, retry_after)
                LOGGER.warning("Graph %s em %s. Retry em %.1fs", status_code, url, delay)
                time.sleep(delay)
                continue

            raise GraphError(status_code, message, url)

    def download_report_csv(self, report_path: str) -> str:
        """Baixa CSV de Microsoft 365 Reports.

        Endpoints de reports normalmente retornam 302 com uma URL preautenticada
        temporaria no header Location. Essa segunda URL nao precisa do token Graph.
        """
        url = report_path if report_path.startswith("http") else f"{self.settings.graph_base_url}{report_path}"
        attempt = 0
        while True:
            token = self._access_token()
            try:
                response = self.session.get(
                    url,
                    timeout=self.settings.request_timeout,
                    headers={"Authorization": f"Bearer {token}", "Accept": "text/csv"},
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                response = None
                status_code = 408
                message = str(exc)
            except requests.RequestException as exc:
                response = None
                status_code = 503
                message = str(exc)
            else:
                status_code = response.status_code
                message = response.text[:1000]

            if response is not None and response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise GraphError(response.status_code, "Redirect sem header Location", url)
                download = self.session.get(location, timeout=self.settings.request_timeout)
                if 200 <= download.status_code < 300:
                    download.encoding = download.encoding or "utf-8-sig"
                    return download.text
                raise GraphError(download.status_code, download.text[:1000], location)

            if response is not None and 200 <= response.status_code < 300:
                response.encoding = response.encoding or "utf-8-sig"
                return response.text

            if status_code in {401, 403, 404}:
                raise GraphError(status_code, message, url)

            if status_code in self.RETRYABLE and attempt < 8:
                attempt += 1
                self.retry_count += 1
                if status_code == 429:
                    self.throttle_count += 1
                retry_after = response.headers.get("Retry-After") if response is not None else None
                delay = self._retry_delay(attempt, retry_after)
                LOGGER.warning("Graph report %s em %s. Retry em %.1fs", status_code, url, delay)
                time.sleep(delay)
                continue

            raise GraphError(status_code, message, url)

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        base = min(120.0, 2.0**attempt)
        return base + random.uniform(0.0, min(3.0, base / 4.0))

    def paged_get(self, url_or_path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = url_or_path
        next_params = params
        while next_url:
            page = self.get(next_url, params=next_params)
            for row in page.get("value", []):
                yield row
            next_url = page.get("@odata.nextLink")
            next_params = None

    def list_sites(self, search_query: str = "*") -> Iterator[dict[str, Any]]:
        yield from self.paged_get("/sites", params={"search": search_query})

    def get_site(self, site_id: str) -> dict[str, Any]:
        return self.get(f"/sites/{quote(site_id, safe='')}")

    def list_site_drives(self, site_id: str) -> Iterator[dict[str, Any]]:
        yield from self.paged_get(f"/sites/{quote(site_id, safe='')}/drives")

    def get_drive_root(self, drive_id: str) -> dict[str, Any]:
        return self.get(f"/drives/{quote(drive_id, safe='')}/root")

    def list_children(self, drive_id: str, item_id: str) -> Iterator[dict[str, Any]]:
        select = "id,name,size,folder,file,package,parentReference,createdDateTime,lastModifiedDateTime,lastAccessedDateTime,webUrl"
        path = f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/children"
        yield from self.paged_get(path, params={"$select": select, "$top": 999})

    def drive_delta_page(self, drive_id: str, next_url: str | None = None, delta_url: str | None = None) -> dict[str, Any]:
        select = "id,name,size,folder,file,package,root,parentReference,createdDateTime,lastModifiedDateTime,lastAccessedDateTime,webUrl,deleted"
        if next_url:
            return self.get(next_url)
        if delta_url:
            return self.get(delta_url)
        path = f"/drives/{quote(drive_id, safe='')}/root/delta"
        return self.get(path, params={"$select": select, "$top": 999})

    def list_users(self) -> Iterator[dict[str, Any]]:
        yield from self.paged_get("/users", params={"$select": "id,displayName,userPrincipalName", "$top": 999})

    def get_user_drive(self, user_id: str) -> dict[str, Any]:
        return self.get(f"/users/{quote(user_id, safe='')}/drive")

    def get_sharepoint_site_usage_detail_csv(self, period: str = "D7") -> str:
        return self.download_report_csv(f"/reports/getSharePointSiteUsageDetail(period='{period}')")
