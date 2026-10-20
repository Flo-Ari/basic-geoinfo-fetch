"""HTTP client for the public Copernicus EMS Rapid Mapping API."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LIST_ENDPOINT = "https://mapping.emergency.copernicus.eu/activations/api/activations/"
DETAIL_ENDPOINT = (
    "https://rapidmapping.emergency.copernicus.eu/"
    "backend/dashboard-api/public-activations/"
)
ALLOWED_API_HOSTS = {
    "mapping.emergency.copernicus.eu",
    "rapidmapping.emergency.copernicus.eu",
}
ALLOWED_DOWNLOAD_HOSTS = {"rapidmapping.emergency.copernicus.eu"}
DEFAULT_USER_AGENT = "emergency-mapping-agent-cems-pdf-fetcher/1.0"


class CemsApiError(RuntimeError):
    """Raised when the public CEMS API returns unusable data."""


class CemsClient:
    """Small, retrying client for public EMSR activation metadata and files."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 120,
        max_retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_retries < 0:
            raise ValueError("max_retries 不能小于 0")

        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})

        if session is None:
            retry = Retry(
                total=max_retries,
                connect=max_retries,
                read=max_retries,
                status=max_retries,
                backoff_factor=1.0,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)

    def list_activations(self, limit: int = 3, page_size: int = 100) -> list[dict[str, Any]]:
        """Return newest public EMSR activations; limit=0 follows all API pages."""
        if limit < 0:
            raise ValueError("limit 不能小于 0；使用 0 表示全部")
        if page_size <= 0:
            raise ValueError("page_size 必须大于 0")

        collected: list[dict[str, Any]] = []
        url: str | None = LIST_ENDPOINT
        params: dict[str, int] | None = {
            "limit": min(page_size, max(limit, 20)) if limit else page_size,
            "offset": 0,
        }

        while url:
            payload = self._get_json(url, params=params)
            results = payload.get("results")
            if not isinstance(results, list):
                raise CemsApiError("CEMS激活列表缺少 results 数组")
            collected.extend(
                item
                for item in results
                if isinstance(item, dict)
                and str(item.get("code", "")).upper().startswith("EMSR")
                and str(item.get("drmPhase") or "response").lower() == "response"
            )

            if limit and len(collected) >= limit:
                return collected[:limit]

            next_url = payload.get("next")
            if not next_url:
                break
            url = self._normalize_api_url(str(next_url))
            params = None

        return collected

    def get_activation(self, code: str) -> dict[str, Any]:
        normalized = code.strip().upper()
        if not re.fullmatch(r"EMSR\d+", normalized):
            raise ValueError(f"无效的Rapid Mapping激活编号：{code}")

        payload = self._get_json(DETAIL_ENDPOINT, params={"code": normalized})
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            raise CemsApiError(f"未找到公开激活事件：{normalized}")
        activation = results[0]
        if not isinstance(activation, dict):
            raise CemsApiError(f"激活事件 {normalized} 的详情格式无效")
        return activation

    def download_file(
        self,
        url: str,
        destination: Path,
        *,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> int:
        """Stream one trusted public product ZIP to an atomic local file."""
        self._validate_download_url(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        downloaded = 0

        try:
            with self.session.get(
                url,
                stream=True,
                timeout=self.timeout_seconds,
                headers={"Accept": "*/*"},
            ) as response:
                response.raise_for_status()
                raw_total = response.headers.get("Content-Length")
                total = int(raw_total) if raw_total and raw_total.isdigit() else None
                with partial.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(downloaded, total)
            partial.replace(destination)
            return downloaded
        except (OSError, requests.RequestException) as exc:
            partial.unlink(missing_ok=True)
            raise CemsApiError(f"下载产品包失败：{url}：{exc}") from exc

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._validate_api_url(url)
        try:
            response = self.session.get(url, params=params, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise CemsApiError(f"请求CEMS API失败：{url}：{exc}") from exc
        if not isinstance(payload, dict):
            raise CemsApiError(f"CEMS API没有返回JSON对象：{url}")
        return payload

    @staticmethod
    def _validate_api_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_API_HOSTS:
            raise CemsApiError(f"拒绝访问非CEMS官方API地址：{url}")

    @staticmethod
    def _normalize_api_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.hostname not in ALLOWED_API_HOSTS:
            raise CemsApiError(f"拒绝访问非CEMS官方API地址：{url}")
        if parsed.scheme == "http":
            return parsed._replace(scheme="https").geturl()
        CemsClient._validate_api_url(url)
        return url

    @staticmethod
    def _validate_download_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
            raise CemsApiError(f"拒绝下载非CEMS官方产品地址：{url}")
