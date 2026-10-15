"""Album planning, concurrent downloads, and durable reports."""

import csv
import io
import json
import os
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .config import AlbumConfig
from .downloader import DownloadResult, ImageDownloader, image_relative_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_right(post: Mapping[str, Any]) -> bool:
    # Fail closed: a schema change must not turn preview covers into authorized files.
    value = post.get("has_right", 0)
    if isinstance(value, str):
        return value.strip() == "1"
    return value is True or (type(value) is int and value == 1)


def _urls_from_value(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
    return [text]


def score_image_urls(post: Mapping[str, Any]) -> List[str]:
    """Return cover + pics, deduplicated only inside this one post."""
    candidates = _urls_from_value(post.get("cover")) + _urls_from_value(post.get("pics"))
    result: List[str] = []
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _rank_value(post: Mapping[str, Any]) -> Any:
    rank = post.get("rank", "")
    return rank if isinstance(rank, (int, float, str)) else str(rank)


def build_manifest(album: AlbumConfig, posts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    records: List[Dict[str, Any]] = []
    for post in posts:
        post_id = str(post.get("post_id") or "").strip()
        if not post_id:
            continue
        title = str(post.get("title") or "未命名曲目").strip() or "未命名曲目"
        category = str(post.get("cate") or "").strip().lower()
        source_url = "https://afdian.com/album/{}/{}".format(album.album_id, post_id)
        sources = score_image_urls(post) if category == "pic" else []
        record: Dict[str, Any] = {
            "rank": _rank_value(post),
            "post_id": post_id,
            "title": title,
            "category": category or "unknown",
            "source_url": source_url,
            "status": "ready",
            "note": "",
            "images": [],
        }

        if category != "pic":
            record["status"] = "non_pic"
            record["note"] = "非图片条目，未下载"
        elif not _has_right(post):
            record["status"] = "locked"
            record["note"] = str(post.get("has_right_errMsg") or "当前 token 无权查看")
        elif not sources:
            record["status"] = "no_pics"
            record["note"] = "图片条目没有 cover 或 pics"
        else:
            for image_index, url in enumerate(sources, start=1):
                relative_path = image_relative_path(
                    post_id, image_index, url, record["rank"], record["title"]
                )
                record["images"].append(
                    {
                        "index": image_index,
                        "url": url,
                        "path": relative_path.as_posix(),
                        "status": "pending",
                        "bytes": 0,
                        "sha256": "",
                        "error": "",
                    }
                )
        records.append(record)

    return {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "album": {
            "key": album.key,
            "title": album.title,
            "album_id": album.album_id,
            "source_url": album.page_url,
            "output_directory": album.output_directory,
        },
        "posts": records,
    }


def _post_download_status(record: Mapping[str, Any]) -> str:
    statuses = [str(image["status"]) for image in record.get("images", [])]
    if any(status == "failed" for status in statuses):
        return "partial_failure"
    if any(status == "downloaded" for status in statuses):
        return "downloaded"
    if statuses and all(status == "existing" for status in statuses):
        return "existing"
    return str(record.get("status") or "ready")


def make_summary(manifest: Mapping[str, Any], mode: str) -> Dict[str, Any]:
    posts = list(manifest.get("posts", []))
    status_counts: Dict[str, int] = {}
    images: List[Mapping[str, Any]] = []
    for post in posts:
        status = str(post.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        images.extend(post.get("images", []))
    image_status_counts: Dict[str, int] = {}
    for image in images:
        status = str(image.get("status") or "unknown")
        image_status_counts[status] = image_status_counts.get(status, 0) + 1

    return {
        "completed_at": _utc_now(),
        "mode": mode,
        "posts_total": len(posts),
        "posts_locked": status_counts.get("locked", 0),
        "posts_no_pics": status_counts.get("no_pics", 0),
        "posts_non_pic": status_counts.get("non_pic", 0),
        "posts_failed": status_counts.get("partial_failure", 0),
        "images_total": len(images),
        "images_downloaded": image_status_counts.get("downloaded", 0),
        "images_existing": image_status_counts.get("existing", 0),
        "images_failed": image_status_counts.get("failed", 0),
        "post_statuses": status_counts,
        "image_statuses": image_status_counts,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_name(path.name + ".part")
    try:
        with part_path.open("w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(part_path), str(path))
    finally:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass


def _previous_images(output_directory: Path) -> Dict[str, Mapping[str, Any]]:
    """Read the last source/hash metadata; malformed reports are never trusted."""
    path = output_directory / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        posts = payload.get("posts", [])
        if not isinstance(posts, list):
            return {}
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return {}

    result: Dict[str, Mapping[str, Any]] = {}
    for post in posts:
        if not isinstance(post, dict) or not isinstance(post.get("images"), list):
            continue
        for image in post["images"]:
            if not isinstance(image, dict):
                continue
            relative_path = str(image.get("path") or "")
            source_url = str(image.get("url") or "")
            if relative_path and source_url:
                result[relative_path] = image
    return result


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _tsv(rows: Iterable[Sequence[Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue()


def write_reports(output_directory: Path, manifest: Dict[str, Any], summary: Dict[str, Any]) -> None:
    manifest["summary"] = summary
    _atomic_write_text(
        output_directory / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    index_rows: List[Sequence[Any]] = [
        (
            "rank",
            "post_id",
            "title",
            "category",
            "status",
            "image_count",
            "downloaded",
            "existing",
            "failed",
            "files",
            "source_url",
            "note",
        )
    ]
    failure_rows: List[Sequence[Any]] = [
        ("rank", "post_id", "title", "image_index", "url", "path", "error")
    ]

    for post in manifest["posts"]:
        images = post["images"]
        statuses = [image["status"] for image in images]
        index_rows.append(
            (
                post["rank"],
                post["post_id"],
                post["title"],
                post["category"],
                post["status"],
                len(images),
                statuses.count("downloaded"),
                statuses.count("existing"),
                statuses.count("failed"),
                "|".join(image["path"] for image in images),
                post["source_url"],
                post["note"],
            )
        )
        for image in images:
            if image["status"] == "failed":
                failure_rows.append(
                    (
                        post["rank"],
                        post["post_id"],
                        post["title"],
                        image["index"],
                        image["url"],
                        image["path"],
                        image["error"],
                    )
                )

    _atomic_write_text(output_directory / "index.tsv", _tsv(index_rows))
    _atomic_write_text(output_directory / "failures.tsv", _tsv(failure_rows))
    _atomic_write_text(
        output_directory / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


@dataclass
class AlbumRunResult:
    album: AlbumConfig
    output_directory: Path
    manifest: Dict[str, Any]
    summary: Dict[str, Any]


class AlbumRunner:
    def __init__(self, client: Any, downloader: ImageDownloader, workers: int = 4) -> None:
        if workers <= 0:
            raise ValueError("workers must be positive")
        self.client = client
        self.downloader = downloader
        self.workers = workers

    def run(self, album: AlbumConfig, output_root: Path, mode: str = "download") -> AlbumRunResult:
        posts = self.client.fetch_album_posts(album.album_id)
        return self.run_posts(album, posts, output_root, mode)

    def run_posts(
        self,
        album: AlbumConfig,
        posts: Sequence[Mapping[str, Any]],
        output_root: Path,
        mode: str = "download",
    ) -> AlbumRunResult:
        if mode not in ("download", "dry-run", "list"):
            raise ValueError("unsupported mode: {}".format(mode))
        output_directory = output_root / album.output_directory
        previous_images = _previous_images(output_directory)
        manifest = build_manifest(album, posts)

        if mode == "download":
            future_map: Dict[Future, Any] = {}
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                for post in manifest["posts"]:
                    if post["status"] != "ready":
                        continue
                    for image in post["images"]:
                        destination = output_directory / Path(image["path"])
                        previous = previous_images.get(image["path"])
                        previous_sha256 = (
                            str(previous.get("sha256") or "").lower() if previous else ""
                        )
                        reuse_existing = bool(
                            previous
                            and str(previous.get("url") or "") == image["url"]
                            and str(previous.get("status") or "") in ("downloaded", "existing")
                            and _valid_sha256(previous_sha256)
                        )
                        future = executor.submit(
                            self.downloader.download,
                            image["url"],
                            destination,
                            reuse_existing,
                            previous_sha256 if reuse_existing else "",
                        )
                        future_map[future] = (post, image)

                for future in as_completed(future_map):
                    post, image = future_map[future]
                    try:
                        result: DownloadResult = future.result()
                    except Exception as error:  # Defensive: one worker must not lose all reports.
                        result = DownloadResult(
                            "failed",
                            error=(str(error) or type(error).__name__).replace("\t", " ").replace("\n", " "),
                        )
                    image["status"] = result.status
                    image["bytes"] = result.bytes_written
                    image["sha256"] = result.sha256
                    image["error"] = result.error

            for post in manifest["posts"]:
                if post["status"] == "ready":
                    post["status"] = _post_download_status(post)
                    if post["status"] == "partial_failure":
                        post["note"] = "至少一张曲谱图下载失败"

        summary = make_summary(manifest, mode)
        if mode == "download":
            write_reports(output_directory, manifest, summary)
        return AlbumRunResult(album, output_directory, manifest, summary)

