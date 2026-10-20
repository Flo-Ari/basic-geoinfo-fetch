"""Download CEMS product archives and retain their static PDF maps."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from geofetch.progress import report as report_progress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse

from .client import CemsApiError, CemsClient, DETAIL_ENDPOINT, LIST_ENDPOINT


@dataclass
class DownloadSummary:
    events_selected: int = 0
    events_completed: int = 0
    products_downloaded: int = 0
    products_skipped: int = 0
    pdfs_downloaded: int = 0
    pdfs_existing: int = 0
    failures: list[str] = field(default_factory=list)


class CemsPdfDownloader:
    """Orchestrate activation selection, ZIP downloads and PDF-only extraction."""

    def __init__(
        self,
        client: CemsClient,
        output_dir: Path,
        *,
        keep_archives: bool = False,
        overwrite: bool = False,
    ) -> None:
        self.client = client
        self.output_dir = output_dir.resolve()
        self.keep_archives = keep_archives
        self.overwrite = overwrite

    def run(
        self,
        *,
        limit: int = 3,
        codes: Iterable[str] | None = None,
        page_size: int = 100,
    ) -> DownloadSummary:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        requested_codes = [code.strip().upper() for code in (codes or []) if code.strip()]
        if requested_codes:
            activation_refs = [{"code": code} for code in dict.fromkeys(requested_codes)]
            selection_mode = "codes"
        else:
            activation_refs = self.client.list_activations(limit=limit, page_size=page_size)
            selection_mode = "all" if limit == 0 else "latest"

        summary = DownloadSummary(events_selected=len(activation_refs))
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "activation_page": "https://mapping.emergency.copernicus.eu/activations/",
                "list_api": LIST_ENDPOINT,
                "detail_api": DETAIL_ENDPOINT,
            },
            "selection": {
                "mode": selection_mode,
                "limit": limit,
                "codes": requested_codes,
            },
            "keep_archives": self.keep_archives,
            "events": [],
        }

        with tempfile.TemporaryDirectory(prefix="cems_pdf_") as temporary:
            temporary_dir = Path(temporary)
            report_progress("提取事件", 0, len(activation_refs), "事件", force=True)
            for index, activation_ref in enumerate(activation_refs, start=1):
                code = str(activation_ref.get("code", "")).upper()
                print(f"\n[{index}/{len(activation_refs)}] 处理事件 {code}")
                try:
                    activation = self.client.get_activation(code)
                    event_record = self._download_activation(activation, temporary_dir, summary)
                    manifest["events"].append(event_record)
                    summary.events_completed += 1
                except (CemsApiError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    message = f"{code}: {exc}"
                    print(f"  失败：{message}")
                    summary.failures.append(message)
                    manifest["events"].append({"code": code, "status": "failed", "error": str(exc)})
                self._write_manifest(manifest, summary)
                report_progress("提取事件", index, len(activation_refs), "事件", force=True)

        self._write_manifest(manifest, summary)
        return summary

    def _download_activation(
        self,
        activation: dict[str, Any],
        temporary_dir: Path,
        summary: DownloadSummary,
    ) -> dict[str, Any]:
        code = str(activation.get("code", "unknown"))
        name = str(activation.get("name", "unnamed"))
        event_dir = self.output_dir / f"{safe_name(code)}_{safe_name(name)}"
        event_dir.mkdir(parents=True, exist_ok=True)

        event_record: dict[str, Any] = {
            "code": code,
            "name": name,
            "category": activation.get("category"),
            "sub_category": activation.get("subCategory"),
            "event_time": activation.get("eventTime"),
            "activation_time": activation.get("activationTime"),
            "countries": _country_names(activation.get("countries")),
            "status": "completed",
            "products": [],
        }
        product_failures: list[str] = []
        products_with_downloads = 0
        products_expected = 0

        for aoi in activation.get("aois") or []:
            if not isinstance(aoi, dict):
                continue
            for product in aoi.get("products") or []:
                if not isinstance(product, dict):
                    continue
                if str(product.get("downloadPath") or "").strip():
                    products_with_downloads += 1
                version = product.get("version") if isinstance(product.get("version"), dict) else {}
                if int(product.get("mapsCount") or 0) > 0 or version.get("statusCode") == "F":
                    products_expected += 1
                try:
                    product_record = self._download_product(
                        code=code,
                        event_dir=event_dir,
                        aoi=aoi,
                        product=product,
                        temporary_dir=temporary_dir,
                        summary=summary,
                    )
                except (CemsApiError, OSError, ValueError, zipfile.BadZipFile) as exc:
                    product_label = _product_label(str(product.get("type") or "MAP"), product)
                    aoi_number = int(aoi.get("number") or product.get("aoiNumber") or 0)
                    message = f"{code}/AOI{aoi_number:02d}/{product_label}: {exc}"
                    print(f"  产品失败，继续下一个：{message}")
                    summary.failures.append(message)
                    product_failures.append(message)
                    product_record = {
                        "aoi_number": aoi_number,
                        "aoi_name": aoi.get("name") or product.get("aoiName"),
                        "product_label": product_label,
                        "status": "failed",
                        "error": str(exc),
                    }
                event_record["products"].append(product_record)

        activation_archive_url = str(activation.get("productsPath") or "").strip()
        if products_with_downloads == 0 and products_expected > 0 and activation_archive_url:
            try:
                fallback_record = self._download_activation_archive(
                    code=code,
                    event_dir=event_dir,
                    download_url=activation_archive_url,
                    temporary_dir=temporary_dir,
                    summary=summary,
                )
            except (CemsApiError, OSError, ValueError, zipfile.BadZipFile) as exc:
                message = f"{code}/ALL_PRODUCTS: {exc}"
                print(f"  事件总包失败：{message}")
                summary.failures.append(message)
                product_failures.append(message)
                fallback_record = {
                    "product_label": "ALL_PRODUCTS",
                    "status": "failed",
                    "error": str(exc),
                }
            event_record["products"].append(fallback_record)

        if not event_record["products"]:
            event_record["warnings"] = ["该事件没有公开的产品记录"]
        elif products_expected == 0 and not product_failures:
            event_record["status"] = "no_static_map"
            event_record["warnings"] = [
                "官方产品均未生成静态地图（例如状态N/W或mapsCount=0）"
            ]
        if product_failures:
            event_record["status"] = "partial"
            event_record["product_failures"] = product_failures
        return event_record

    def _download_activation_archive(
        self,
        *,
        code: str,
        event_dir: Path,
        download_url: str,
        temporary_dir: Path,
        summary: DownloadSummary,
    ) -> dict[str, Any]:
        """Fallback for activations exposing only one archive containing all products."""
        product_dir = event_dir / "ALL_PRODUCTS"
        marker_path = product_dir / "_download.json"
        existing = _load_completed_marker(marker_path, download_url, self.keep_archives)
        if existing and not self.overwrite:
            summary.products_skipped += 1
            summary.pdfs_existing += len(existing.get("pdf_files") or [])
            print("  事件级总包已存在，跳过下载")
            return {
                "product_label": "ALL_PRODUCTS",
                "status": "existing",
                **existing,
            }

        archive_name = safe_name(PurePosixPath(urlparse(download_url).path).name or f"{code}_products.zip")
        if not archive_name.lower().endswith(".zip"):
            archive_name += ".zip"
        if self.keep_archives:
            archive_path = event_dir / "_archives" / archive_name
        else:
            archive_path = temporary_dir / archive_name

        print("  单产品地址不可用，回退下载事件级总包")
        bytes_downloaded = self.client.download_file(
            download_url,
            archive_path,
            progress=_progress_printer(),
        )
        print()
        summary.products_downloaded += 1
        extraction = extract_pdf_files(archive_path, product_dir, overwrite=self.overwrite)
        summary.pdfs_downloaded += extraction["downloaded"]
        summary.pdfs_existing += extraction["existing"]
        pdf_files = [str(path.relative_to(self.output_dir)).replace("\\", "/") for path in extraction["files"]]
        archive_file = (
            str(archive_path.relative_to(self.output_dir)).replace("\\", "/")
            if self.keep_archives
            else None
        )
        marker = {
            "download_url": download_url,
            "archive_file": archive_file,
            "pdf_files": pdf_files,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(marker_path, marker)
        print(f"    从事件总包提取PDF：{len(pdf_files)} 个")
        return {
            "product_label": "ALL_PRODUCTS",
            "status": "downloaded",
            "download_url": download_url,
            "archive_bytes": bytes_downloaded,
            "archive_file": archive_file,
            "pdf_files": pdf_files,
        }

    def _download_product(
        self,
        *,
        code: str,
        event_dir: Path,
        aoi: dict[str, Any],
        product: dict[str, Any],
        temporary_dir: Path,
        summary: DownloadSummary,
    ) -> dict[str, Any]:
        aoi_number = int(aoi.get("number") or product.get("aoiNumber") or 0)
        aoi_name = str(aoi.get("name") or product.get("aoiName") or "unnamed")
        product_type = str(product.get("type") or "MAP").upper()
        version = product.get("version") if isinstance(product.get("version"), dict) else {}
        version_number = int(version.get("number") or 0)
        product_label = _product_label(product_type, product)
        product_dir = (
            event_dir
            / f"AOI{aoi_number:02d}_{safe_name(aoi_name)}"
            / f"{product_label}_v{version_number}"
        )
        download_url = str(product.get("downloadPath") or "").strip()
        record: dict[str, Any] = {
            "aoi_number": aoi_number,
            "aoi_name": aoi_name,
            "product_type": product_type,
            "product_label": product_label,
            "version": version_number,
            "status_code": version.get("statusCode"),
            "maps_count": product.get("mapsCount"),
            "download_url": download_url or None,
            "pdf_files": [],
        }

        if not download_url:
            record["status"] = "skipped"
            record["reason"] = "产品尚无公开下载地址"
            summary.products_skipped += 1
            print(f"  跳过 AOI{aoi_number:02d} {product_label}：无下载地址")
            return record

        marker_path = product_dir / "_download.json"
        existing = _load_completed_marker(marker_path, download_url, self.keep_archives)
        if existing and not self.overwrite:
            record.update(existing)
            record["status"] = "existing"
            summary.products_skipped += 1
            summary.pdfs_existing += len(existing.get("pdf_files") or [])
            print(f"  已存在 AOI{aoi_number:02d} {product_label}，跳过下载")
            return record

        archive_name = safe_name(PurePosixPath(urlparse(download_url).path).name or "product.zip")
        if not archive_name.lower().endswith(".zip"):
            archive_name += ".zip"
        if self.keep_archives:
            archive_path = event_dir / "_archives" / archive_name
        else:
            archive_path = temporary_dir / f"{code}_{aoi_number:02d}_{product_label}_v{version_number}.zip"

        print(f"  下载 AOI{aoi_number:02d} {aoi_name} / {product_label}")
        bytes_downloaded = self.client.download_file(
            download_url,
            archive_path,
            progress=_progress_printer(),
        )
        print()
        summary.products_downloaded += 1

        extraction = extract_pdf_files(archive_path, product_dir, overwrite=self.overwrite)
        summary.pdfs_downloaded += extraction["downloaded"]
        summary.pdfs_existing += extraction["existing"]
        pdf_files = [str(path.relative_to(self.output_dir)).replace("\\", "/") for path in extraction["files"]]
        record.update(
            {
                "status": "downloaded",
                "archive_bytes": bytes_downloaded,
                "archive_file": (
                    str(archive_path.relative_to(self.output_dir)).replace("\\", "/")
                    if self.keep_archives
                    else None
                ),
                "pdf_files": pdf_files,
            }
        )

        marker = {
            "download_url": download_url,
            "archive_file": record["archive_file"],
            "pdf_files": pdf_files,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(marker_path, marker)
        print(f"    提取PDF：{len(pdf_files)} 个")
        return record

    def _write_manifest(self, manifest: dict[str, Any], summary: DownloadSummary) -> None:
        manifest["summary"] = {
            "events_selected": summary.events_selected,
            "events_completed": summary.events_completed,
            "products_downloaded": summary.products_downloaded,
            "products_skipped": summary.products_skipped,
            "pdfs_downloaded": summary.pdfs_downloaded,
            "pdfs_existing": summary.pdfs_existing,
            "failures": summary.failures,
        }
        _write_json_atomic(self.output_dir / "manifest.json", manifest)


def extract_pdf_files(archive_path: Path, output_dir: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Safely extract PDF members only; never unpack GIS files from the ZIP."""
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    downloaded = 0
    existing = 0
    used_names: set[str] = set()

    with zipfile.ZipFile(archive_path) as archive:
        members = [
            member
            for member in archive.infolist()
            if not member.is_dir() and member.filename.lower().endswith(".pdf")
        ]
        from geofetch.progress import track
        for member in track(members, len(members), "提取 PDF", "文件"):
            raw_name = PurePosixPath(member.filename.replace("\\", "/")).name
            filename = _unique_name(safe_name(raw_name), used_names)
            destination = output_dir / filename
            files.append(destination)
            if destination.exists() and not overwrite:
                existing += 1
                continue

            partial = destination.with_name(destination.name + ".part")
            with archive.open(member) as source, partial.open("wb") as target:
                header = source.read(5)
                if header != b"%PDF-":
                    partial.unlink(missing_ok=True)
                    raise CemsApiError(f"ZIP中的文件不是有效PDF：{member.filename}")
                target.write(header)
                shutil.copyfileobj(source, target, length=1024 * 1024)
            os.replace(partial, destination)
            downloaded += 1

    if not files:
        raise CemsApiError(f"产品包中没有PDF地图：{archive_path.name}")
    return {"files": files, "downloaded": downloaded, "existing": existing}


def safe_name(value: str, max_length: int = 120) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", normalized)
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip(" ._")
    normalized = normalized or "unnamed"
    stem = normalized.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        normalized = f"_{normalized}"
    return normalized[:max_length].rstrip(" .")


def _product_label(product_type: str, product: dict[str, Any]) -> str:
    if product.get("monitoring"):
        number = int(product.get("monitoringNumber") or 0)
        return f"{product_type}_MONIT{number:02d}"
    return f"{product_type}_PRODUCT"


def _country_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _unique_name(filename: str, used_names: set[str]) -> str:
    candidate = filename
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    counter = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def _load_completed_marker(
    marker_path: Path,
    download_url: str,
    keep_archives: bool,
) -> dict[str, Any] | None:
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if marker.get("download_url") != download_url:
        return None
    root = marker_path.parents[2]
    pdf_files = marker.get("pdf_files") or []
    if not pdf_files or not all((root.parent / path).is_file() for path in pdf_files):
        return None
    if keep_archives:
        archive_file = marker.get("archive_file")
        if not archive_file or not (root.parent / archive_file).is_file():
            return None
    return {
        "archive_file": marker.get("archive_file"),
        "pdf_files": pdf_files,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)


def _progress_printer():
    last_bucket = {-1}

    def report(downloaded: int, total: int | None) -> None:
        report_progress("下载", downloaded, total, "字节")
        if total:
            percent = int(downloaded * 100 / total)
            bucket = percent // 10
            if bucket not in last_bucket:
                last_bucket.add(bucket)
                print(f"\r    ZIP下载进度：{percent:3d}%", end="", flush=True)
        else:
            mib = downloaded / (1024 * 1024)
            bucket = int(mib // 10)
            if bucket not in last_bucket:
                last_bucket.add(bucket)
                print(f"\r    已下载：{mib:.1f} MiB", end="", flush=True)

    return report
