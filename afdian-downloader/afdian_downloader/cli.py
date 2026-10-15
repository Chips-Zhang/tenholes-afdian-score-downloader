"""Command line interface."""

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Callable, List, Mapping, Optional, Sequence

from . import __version__
from .client import AfdianClient
from .config import AlbumConfig, DEFAULT_ALBUMS
from .downloader import ImageDownloader
from .errors import AfdianDownloaderError, AuthenticationError
from .workflow import AlbumRunResult, AlbumRunner


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能是负数")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afdian-downloader",
        description="下载默认爱发电专辑中当前账号有权查看的曲谱图片。",
    )
    parser.add_argument(
        "--album",
        choices=("all",) + tuple(album.key for album in DEFAULT_ALBUMS),
        default="all",
        help="选择专辑；默认 all（全部默认专辑）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="输出根目录（默认：项目内 output）",
    )
    parser.add_argument("--workers", type=_positive_int, default=4, help="并发下载数（默认：4）")
    parser.add_argument("--retries", type=_nonnegative_int, default=3, help="失败重试次数（默认：3）")
    parser.add_argument("--timeout", type=_positive_float, default=60.0, help="单次请求超时秒数（默认：60）")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="只取清单并统计，不创建目录或下载文件")
    modes.add_argument("--list", action="store_true", help="只列出曲目和状态，不创建目录或下载文件")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def resolve_token(
    environment: Optional[Mapping[str, str]] = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    env = os.environ if environment is None else environment
    token = str(env.get("AFDIAN_AUTH_TOKEN", "")).strip()
    if token:
        return token
    try:
        token = prompt("Afdian auth_token（输入不会显示）: ").strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise AuthenticationError(
            "无法安全读取 token；请设置环境变量 AFDIAN_AUTH_TOKEN 后重试"
        ) from error
    if not token:
        raise AuthenticationError("没有提供 Afdian auth_token")
    return token


def _selected_albums(key: str) -> List[AlbumConfig]:
    if key == "all":
        return list(DEFAULT_ALBUMS)
    return [album for album in DEFAULT_ALBUMS if album.key == key]


def _print_records(result: AlbumRunResult) -> None:
    print("\n{}（{}）".format(result.album.title, result.album.album_id))
    print("rank\tstatus\timages\ttitle")
    for post in result.manifest["posts"]:
        print(
            "{}\t{}\t{}\t{}".format(
                post["rank"], post["status"], len(post["images"]), post["title"]
            )
        )


def _print_summary(result: AlbumRunResult, mode: str) -> None:
    summary = result.summary
    if mode == "list":
        _print_records(result)
        return
    if mode == "dry-run":
        print(
            "{}：曲目 {}，计划图片 {}，锁定 {}，无图 {}，非图片 {}（未写入文件）".format(
                result.album.title,
                summary["posts_total"],
                summary["images_total"],
                summary["posts_locked"],
                summary["posts_no_pics"],
                summary["posts_non_pic"],
            )
        )
        return
    print(
        "{}：曲目 {}，新下载 {}，已存在 {}，失败 {}，锁定 {}，无图 {}，非图片 {}".format(
            result.album.title,
            summary["posts_total"],
            summary["images_downloaded"],
            summary["images_existing"],
            summary["images_failed"],
            summary["posts_locked"],
            summary["posts_no_pics"],
            summary["posts_non_pic"],
        )
    )
    print("报告目录：{}".format(result.output_directory))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "list" if args.list else "dry-run" if args.dry_run else "download"
    try:
        token = resolve_token()
        client = AfdianClient(token, timeout=args.timeout, retries=args.retries)
        client.validate_session()
    except (AfdianDownloaderError, ValueError) as error:
        print("错误：{}".format(error), file=sys.stderr)
        return 2

    downloader = ImageDownloader(timeout=args.timeout, retries=args.retries)
    runner = AlbumRunner(client, downloader, workers=args.workers)
    failed = False
    for album in _selected_albums(args.album):
        print("正在读取专辑：{}".format(album.title))
        try:
            result = runner.run(album, args.output.expanduser().resolve(), mode)
            _print_summary(result, mode)
            if result.summary["images_failed"]:
                failed = True
        except (AfdianDownloaderError, OSError, ValueError) as error:
            failed = True
            print("{} 失败：{}".format(album.title, error), file=sys.stderr)

    return 1 if failed else 0

