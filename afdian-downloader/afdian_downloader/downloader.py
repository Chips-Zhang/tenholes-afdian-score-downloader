"""Concurrent-safe, atomic image downloads using urllib."""

import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .client import USER_AGENT


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


@dataclass(frozen=True)
class DownloadResult:
    status: str
    bytes_written: int = 0
    sha256: str = ""
    error: str = ""


def safe_post_id(post_id: str) -> str:
    cleaned = "".join(character for character in post_id if character.isalnum() or character in "-_")
    if cleaned:
        return cleaned[:96]
    return hashlib.sha256(post_id.encode("utf-8")).hexdigest()[:32]


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def safe_title(title: str, max_bytes: int = 120) -> str:
    forbidden = set('/\\:*?"<>|')
    cleaned_characters = []
    previous_was_separator = False
    for character in title:
        is_separator = character in forbidden or ord(character) < 32
        if is_separator:
            if not previous_was_separator:
                cleaned_characters.append("_")
            previous_was_separator = True
            continue
        cleaned_characters.append(character)
        previous_was_separator = False
    cleaned = " ".join("".join(cleaned_characters).split()).strip(" ._")
    cleaned = _truncate_utf8(cleaned, max_bytes).rstrip(" ._")
    return cleaned or "未命名曲目"


def track_directory_name(rank: Any, title: str, post_id: str) -> str:
    try:
        rank_text = "{:03d}".format(int(rank))
    except (TypeError, ValueError):
        raw_rank = "".join(character for character in str(rank) if character.isalnum() or character in "-_")
        rank_text = raw_rank[:12] or "000"
    return "{}_{}__{}".format(rank_text, safe_title(title), safe_post_id(post_id))


def image_extension(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    suffix = Path(path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".img"


def image_relative_path(
    post_id: str,
    image_index: int,
    url: str,
    rank: Any = "",
    title: str = "",
) -> Path:
    directory = (
        track_directory_name(rank, title, post_id)
        if rank != "" or title
        else safe_post_id(post_id)
    )
    return Path("曲谱") / directory / "score_{:02d}{}".format(image_index, image_extension(url))


def _content_type(headers: Any) -> str:
    if hasattr(headers, "get_content_type"):
        return str(headers.get_content_type()).lower()
    if hasattr(headers, "get"):
        return str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    return ""


def _content_length(headers: Any) -> Optional[int]:
    if not hasattr(headers, "get"):
        return None
    raw_value = str(headers.get("Content-Length", "")).strip()
    if not raw_value:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if value >= 0 else None


def _remove_partial(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _valid_iso_bmff(path: Path, file_size: int) -> bool:
    """Validate top-level ISO-BMFF box lengths used by AVIF."""
    offset = 0
    saw_avif_brand = False
    try:
        with path.open("rb") as source:
            while offset < file_size:
                source.seek(offset)
                header = source.read(8)
                if len(header) != 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if box_size < header_size or offset + box_size > file_size:
                    return False
                if box_type == b"ftyp":
                    brands = source.read(min(box_size - header_size, 64))
                    saw_avif_brand = b"avif" in brands or b"avis" in brands
                offset += box_size
    except OSError:
        return False
    return offset == file_size and saw_avif_brand


def _looks_like_image(path: Path) -> bool:
    """Recognize supported image containers and reject obvious/truncated impostors."""
    try:
        size = path.stat().st_size
        if size < 13:
            return False
        with path.open("rb") as source:
            prefix = source.read(32)
            if prefix.startswith(b"\xff\xd8\xff"):
                source.seek(-2, os.SEEK_END)
                return source.read(2) == b"\xff\xd9"
            if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
                if size < 20:
                    return False
                source.seek(-12, os.SEEK_END)
                return source.read(12) == b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
            if prefix.startswith((b"GIF87a", b"GIF89a")):
                source.seek(-1, os.SEEK_END)
                return source.read(1) == b";"
    except OSError:
        return False

    if prefix.startswith(b"BM"):
        return len(prefix) >= 6 and int.from_bytes(prefix[2:6], "little") == size
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return int.from_bytes(prefix[4:8], "little") + 8 == size
    if prefix[4:8] == b"ftyp" and (b"avif" in prefix[8:] or b"avis" in prefix[8:]):
        return _valid_iso_bmff(path, size)
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 128)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ImageDownloader:
    """Download score images without sending the Afdian auth cookie to the CDN."""

    def __init__(
        self,
        timeout: float = 60.0,
        retries: int = 3,
        opener: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self.timeout = timeout
        self.retries = retries
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep

    def download(
        self,
        url: str,
        destination: Path,
        reuse_existing: bool = True,
        expected_sha256: str = "",
    ) -> DownloadResult:
        if reuse_existing and destination.exists() and destination.is_file():
            if len(expected_sha256) == 64 and _looks_like_image(destination):
                actual_sha256 = _sha256(destination)
                if actual_sha256 == expected_sha256.lower():
                    return DownloadResult(
                        "existing", destination.stat().st_size, actual_sha256
                    )

        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return DownloadResult("failed", error="不是有效的 HTTP(S) 图片地址")

        part_path = destination.with_name(destination.name + ".part")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "image/*,*/*;q=0.8", "User-Agent": USER_AGENT},
        )
        last_error: Optional[BaseException] = None

        for attempt in range(self.retries + 1):
            _remove_partial(part_path)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    content_type = _content_type(response.headers)
                    content_length = _content_length(response.headers)
                    if content_type and not (
                        content_type.startswith("image/")
                        or content_type in ("application/octet-stream", "binary/octet-stream")
                    ):
                        raise ValueError("响应不是图片（{}）".format(content_type))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    total = 0
                    with part_path.open("wb") as output:
                        while True:
                            chunk = response.read(1024 * 128)
                            if not chunk:
                                break
                            output.write(chunk)
                            total += len(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if total <= 0:
                        raise ValueError("图片响应为空")
                    if content_length is not None and total != content_length:
                        raise ValueError(
                            "响应长度不完整（预期 {}，实际 {}）".format(
                                content_length, total
                            )
                        )
                    if not _looks_like_image(part_path):
                        raise ValueError("响应内容不是完整的受支持图片")
                    digest = _sha256(part_path)
                    os.replace(str(part_path), str(destination))
                    return DownloadResult("downloaded", total, digest)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in (408, 425, 429) and error.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                last_error = error

            _remove_partial(part_path)
            if attempt < self.retries:
                self._sleep(min(0.5 * (2 ** attempt), 8.0))

        _remove_partial(part_path)
        if isinstance(last_error, urllib.error.HTTPError):
            message = "HTTP {}".format(last_error.code)
        elif last_error is None:
            message = "未知下载错误"
        else:
            message = str(last_error) or type(last_error).__name__
        return DownloadResult(
            "failed", error=message.replace("\t", " ").replace("\n", " ")
        )

