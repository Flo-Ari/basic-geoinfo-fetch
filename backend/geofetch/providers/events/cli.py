"""Command-line interface for the standalone CEMS PDF fetcher."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .client import CemsApiError, CemsClient
from .downloader import CemsPdfDownloader


ROOT_DIR = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从Copernicus EMS Rapid Mapping公开API下载产品并提取静态地图PDF"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="下载最新多少个EMSR事件；默认为3，0表示API公开的全部事件",
    )
    parser.add_argument(
        "--code",
        action="append",
        default=[],
        metavar="EMSR927",
        help="只下载指定激活编号，可重复使用；指定后忽略 --limit",
    )
    parser.add_argument("--output", type=Path, default=Path("results"), help="输出目录")
    parser.add_argument(
        "--keep-archives",
        action="store_true",
        help="保留包含GIS数据的原始产品ZIP；默认提取PDF后删除临时ZIP",
    )
    parser.add_argument("--overwrite", action="store_true", help="重新下载并覆盖已有PDF")
    parser.add_argument("--page-size", type=int, default=100, help="全量模式下每页事件数")
    parser.add_argument("--timeout", type=int, default=180, help="单个HTTP请求超时秒数")
    parser.add_argument("--max-retries", type=int, default=3, help="网络失败最大重试次数")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 不能小于0")
    if args.page_size <= 0:
        parser.error("--page-size 必须大于0")

    output_dir = args.output if args.output.is_absolute() else ROOT_DIR / args.output
    try:
        client = CemsClient(timeout_seconds=args.timeout, max_retries=args.max_retries)
        downloader = CemsPdfDownloader(
            client,
            output_dir,
            keep_archives=args.keep_archives,
            overwrite=args.overwrite,
        )
        summary = downloader.run(
            limit=args.limit,
            codes=args.code,
            page_size=args.page_size,
        )
    except (CemsApiError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("\n下载结束")
    print(f"事件：{summary.events_completed}/{summary.events_selected}")
    print(f"新下载产品包：{summary.products_downloaded}")
    print(f"新提取PDF：{summary.pdfs_downloaded}")
    print(f"已存在PDF：{summary.pdfs_existing}")
    print(f"输出目录：{output_dir.resolve()}")
    if summary.failures:
        print(f"失败：{len(summary.failures)}，详见 manifest.json", file=sys.stderr)
        return 1
    return 0


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except OSError:
                pass
