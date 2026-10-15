import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from afdian_downloader.cli import build_parser, resolve_token
from afdian_downloader.client import AfdianClient, SameOriginRedirectHandler
from afdian_downloader.config import AlbumConfig, DEFAULT_ALBUMS
from afdian_downloader.downloader import (
    DownloadResult,
    ImageDownloader,
    _looks_like_image,
    image_relative_path,
    safe_title,
    track_directory_name,
)
from afdian_downloader.errors import AuthenticationError
from afdian_downloader.workflow import AlbumRunner, build_manifest, score_image_urls


JPEG_ONE = b"\xff\xd8\xff\xe0" + b"one-image" + b"\xff\xd9"
JPEG_TWO = b"\xff\xd8\xff\xe0" + b"two-image" + b"\xff\xd9"


class FakeHeaders(dict):
    def get_content_type(self):
        return self.get("Content-Type", "").split(";", 1)[0]


class FakeResponse:
    def __init__(self, body, content_type="application/json"):
        self._stream = io.BytesIO(body)
        self.headers = FakeHeaders({"Content-Type": content_type})

    def read(self, size=-1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class SequenceOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, (dict, list)):
            response = FakeResponse(json.dumps(response).encode("utf-8"))
        return response


def api_page(items, has_more):
    return {"ec": 200, "em": "ok", "data": {"list": items, "has_more": has_more}}


class ClientTests(unittest.TestCase):
    def test_catalog_paginates_and_scopes_token_to_cookie(self):
        opener = SequenceOpener(
            [
                api_page([{"post_id": "p1", "rank": 1}], 1),
                api_page([{"post_id": "p2", "rank": 2}], 0),
            ]
        )
        client = AfdianClient("top-secret", opener=opener, sleep=lambda _: None)

        posts = client.fetch_album_catalog("album-1", per_page=100)

        self.assertEqual([post["post_id"] for post in posts], ["p1", "p2"])
        first_request = opener.requests[0][0]
        second_request = opener.requests[1][0]
        first_query = urllib.parse.parse_qs(urllib.parse.urlsplit(first_request.full_url).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlsplit(second_request.full_url).query)
        self.assertEqual(first_query["page"], ["1"])
        self.assertEqual(second_query["page"], ["2"])
        self.assertEqual(first_query["per_page"], ["100"])
        self.assertEqual(first_request.get_header("Cookie"), "auth_token=top-secret")
        self.assertNotIn("top-secret", first_request.full_url)

    def test_ranked_endpoint_uses_last_rank_and_ascending_rank(self):
        opener = SequenceOpener(
            [
                api_page([{"post_id": "p10", "rank": 10}], 1),
                api_page([{"post_id": "p20", "rank": 20}], 0),
            ]
        )
        client = AfdianClient("secret", opener=opener, sleep=lambda _: None)

        posts = client.fetch_album_posts_ranked("album-1")

        self.assertEqual(len(posts), 2)
        first_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opener.requests[0][0].full_url).query,
            keep_blank_values=True,
        )
        second_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(opener.requests[1][0].full_url).query,
            keep_blank_values=True,
        )
        self.assertEqual(first_query["lastRank"], [""])
        self.assertEqual(first_query["rankOrder"], ["asc"])
        self.assertEqual(first_query["rankField"], ["rank"])
        self.assertEqual(second_query["lastRank"], ["10"])

    def test_cross_origin_redirect_is_rejected_before_cookie_can_be_forwarded(self):
        handler = SameOriginRedirectHandler()
        request = urllib.request.Request(
            "https://afdian.com/api/user/get-album-catalog",
            headers={"Cookie": "auth_token=secret"},
        )

        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.com/collect",
            )

    def test_token_rejects_cookie_injection(self):
        with self.assertRaises(AuthenticationError):
            AfdianClient("bad;other_cookie=value")

    def test_session_validation_accepts_logged_in_profile(self):
        opener = SequenceOpener(
            [{"ec": 200, "em": "ok", "data": {"user": {"user_id": "user-1"}}}]
        )
        client = AfdianClient("secret", opener=opener, sleep=lambda _: None)

        client.validate_session()

        request = opener.requests[0][0]
        self.assertEqual(request.full_url, "https://afdian.com/api/my/profile")
        self.assertEqual(request.get_header("Cookie"), "auth_token=secret")

    def test_session_validation_rejects_expired_token(self):
        opener = SequenceOpener(
            [{"ec": 40100, "em": "该功能需要登录查看", "data": {}}]
        )
        client = AfdianClient("expired", opener=opener, sleep=lambda _: None)

        with self.assertRaises(AuthenticationError):
            client.validate_session()

    def test_session_validation_rejects_anonymous_success_response(self):
        opener = SequenceOpener([{"ec": 200, "em": "ok", "data": {"user": {}}}])
        client = AfdianClient("invalid", opener=opener, sleep=lambda _: None)

        with self.assertRaises(AuthenticationError):
            client.validate_session()


class PlanningTests(unittest.TestCase):
    def test_cover_and_pics_are_union_deduplicated_per_post(self):
        post = {
            "cover": "https://cdn.test/cover.jpg",
            "pics": ["https://cdn.test/score.jpg", "https://cdn.test/cover.jpg"],
        }
        self.assertEqual(
            score_image_urls(post),
            ["https://cdn.test/cover.jpg", "https://cdn.test/score.jpg"],
        )

    def test_locked_no_pics_and_non_pic_are_distinct(self):
        posts = [
            {
                "post_id": "locked",
                "rank": 1,
                "title": "locked",
                "cate": "pic",
                "has_right": 0,
                "cover": "https://cdn.test/locked.jpg",
                "pics": [],
                "has_right_errMsg": "locked",
            },
            {
                "post_id": "empty",
                "rank": 2,
                "title": "empty",
                "cate": "pic",
                "has_right": 1,
                "cover": "",
                "pics": [],
            },
            {
                "post_id": "video",
                "rank": 3,
                "title": "video",
                "cate": "video",
                "has_right": 1,
                "cover": "https://cdn.test/video-cover.jpg",
                "pics": ["https://cdn.test/not-a-score.jpg"],
            },
            {
                "post_id": "missing-right",
                "rank": 4,
                "title": "missing right",
                "cate": "pic",
                "cover": "https://cdn.test/preview.jpg",
                "pics": [],
            },
        ]

        manifest = build_manifest(DEFAULT_ALBUMS[0], posts)

        self.assertEqual(
            [post["status"] for post in manifest["posts"]],
            ["locked", "no_pics", "non_pic", "locked"],
        )
        self.assertTrue(all(not post["images"] for post in manifest["posts"]))

    def test_unexpected_has_right_values_remain_locked(self):
        for value in ("true", "unknown", 1.0, 2, -1, {}, []):
            post = {
                "post_id": "post",
                "rank": 1,
                "title": "preview",
                "cate": "pic",
                "has_right": value,
                "cover": "https://cdn.test/preview.jpg",
                "pics": [],
            }
            manifest = build_manifest(DEFAULT_ALBUMS[0], [post])
            self.assertEqual(manifest["posts"][0]["status"], "locked")
            self.assertEqual(manifest["posts"][0]["images"], [])

    def test_human_readable_paths_are_safe_and_duplicate_titles_do_not_collide(self):
        unsafe_title = '同名/曲目:第一版?*<>|"'
        first = image_relative_path(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 1, "https://cdn.test/a.jpg", 1, unsafe_title
        )
        second = image_relative_path(
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 1, "https://cdn.test/b.jpg", 2, unsafe_title
        )

        self.assertNotEqual(first.parent, second.parent)
        for path in (first, second):
            directory = path.parts[1]
            self.assertFalse(any(character in directory for character in '/\\:*?"<>|'))
            self.assertLessEqual(len(directory.encode("utf-8")), 255)
        self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", first.parts[1])
        self.assertIn("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", second.parts[1])

    def test_long_multibyte_title_is_truncated_at_character_boundary(self):
        title = safe_title("曲" * 200)
        self.assertLessEqual(len(title.encode("utf-8")), 120)
        directory = track_directory_name(12, "曲" * 200, "c" * 32)
        self.assertLessEqual(len(directory.encode("utf-8")), 255)


class ImageDownloaderTests(unittest.TestCase):
    def test_atomic_download_and_idempotent_rerun(self):
        opener = SequenceOpener([FakeResponse(JPEG_ONE, "image/jpeg")])
        downloader = ImageDownloader(opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "nested" / "score.jpg"

            first = downloader.download("https://cdn.test/score.jpg", destination)
            second = downloader.download(
                "https://cdn.test/score.jpg",
                destination,
                reuse_existing=True,
                expected_sha256=first.sha256,
            )

            self.assertEqual(first.status, "downloaded")
            self.assertEqual(second.status, "existing")
            self.assertEqual(destination.read_bytes(), JPEG_ONE)
            self.assertFalse(destination.with_name("score.jpg.part").exists())
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(first.sha256, second.sha256)

    def test_download_retries_transient_failure(self):
        opener = SequenceOpener(
            [urllib.error.URLError("temporary"), FakeResponse(JPEG_ONE, "image/jpeg")]
        )
        downloader = ImageDownloader(retries=1, opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = downloader.download(
                "https://cdn.test/score.png", Path(temporary_directory) / "score.png"
            )
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(len(opener.requests), 2)

    def test_corrupt_existing_file_is_replaced_atomically(self):
        opener = SequenceOpener([FakeResponse(JPEG_TWO, "image/jpeg")])
        downloader = ImageDownloader(retries=0, opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "score.jpg"
            destination.write_text("<html>not an image</html>", encoding="utf-8")

            result = downloader.download("https://cdn.test/score.jpg", destination)

            self.assertEqual(result.status, "downloaded")
            self.assertEqual(destination.read_bytes(), JPEG_TWO)

    def test_non_image_body_is_rejected_even_with_image_content_type(self):
        opener = SequenceOpener([FakeResponse(b"<html>error</html>", "image/jpeg")])
        downloader = ImageDownloader(retries=0, opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "score.jpg"
            result = downloader.download("https://cdn.test/score.jpg", destination)

            self.assertEqual(result.status, "failed")
            self.assertFalse(destination.exists())

    def test_truncated_supported_containers_are_rejected(self):
        samples = {
            "short.png": b"\x89PNG\r\n\x1a\n" + b"12345",
            "short.gif": b"GIF89a" + b"1234567",
            "short.bmp": b"BM" + (100).to_bytes(4, "little") + b"1234567",
            "short.webp": b"RIFF" + (100).to_bytes(4, "little") + b"WEBP1",
            "short.avif": (24).to_bytes(4, "big") + b"ftypavif12345",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for filename, body in samples.items():
                path = root / filename
                path.write_bytes(body)
                self.assertFalse(_looks_like_image(path), filename)

    def test_empty_expected_hash_never_blesses_existing_image(self):
        opener = SequenceOpener([FakeResponse(JPEG_TWO, "image/jpeg")])
        downloader = ImageDownloader(retries=0, opener=opener, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "score.jpg"
            destination.write_bytes(JPEG_ONE)

            result = downloader.download(
                "https://cdn.test/score.jpg",
                destination,
                reuse_existing=True,
                expected_sha256="",
            )

            self.assertEqual(result.status, "downloaded")
            self.assertEqual(destination.read_bytes(), JPEG_TWO)


class StubDownloader:
    def __init__(self, result=None):
        self.result = result or DownloadResult("downloaded", 10)
        self.calls = []

    def download(self, url, destination, reuse_existing=True, expected_sha256=""):
        self.calls.append((url, destination))
        return self.result


class WorkflowTests(unittest.TestCase):
    def test_reports_keep_locked_and_non_pic_out_of_failures(self):
        album = AlbumConfig("test", "测试", "album", "测试输出")
        posts = [
            {
                "post_id": "ok",
                "rank": 1,
                "title": "曲谱",
                "cate": "pic",
                "has_right": 1,
                "cover": "",
                "pics": ["https://cdn.test/score.jpg"],
            },
            {
                "post_id": "locked",
                "rank": 2,
                "title": "锁定",
                "cate": "pic",
                "has_right": 0,
                "cover": "https://cdn.test/locked.jpg",
                "pics": [],
            },
            {
                "post_id": "video",
                "rank": 3,
                "title": "视频",
                "cate": "video",
                "has_right": 1,
                "cover": "https://cdn.test/video.jpg",
                "pics": [],
            },
        ]
        downloader = StubDownloader()
        runner = AlbumRunner(object(), downloader, workers=1)

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = runner.run_posts(album, posts, Path(temporary_directory), "download")
            failure_lines = (result.output_directory / "failures.tsv").read_text(
                encoding="utf-8"
            ).splitlines()
            index_text = (result.output_directory / "index.tsv").read_text(encoding="utf-8")

        self.assertEqual(len(failure_lines), 1)
        self.assertEqual(result.summary["posts_locked"], 1)
        self.assertEqual(result.summary["posts_non_pic"], 1)
        self.assertIn("locked", index_text)
        self.assertIn("non_pic", index_text)
        self.assertEqual(len(downloader.calls), 1)

    def test_network_failure_is_written_to_failures_report(self):
        album = AlbumConfig("test", "测试", "album", "测试输出")
        posts = [
            {
                "post_id": "failed",
                "rank": 1,
                "title": "失败曲目",
                "cate": "pic",
                "has_right": 1,
                "cover": "",
                "pics": ["https://cdn.test/score.jpg"],
            }
        ]
        runner = AlbumRunner(
            object(), StubDownloader(DownloadResult("failed", error="HTTP 503")), workers=1
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = runner.run_posts(album, posts, Path(temporary_directory), "download")
            failure_text = (result.output_directory / "failures.tsv").read_text(encoding="utf-8")

        self.assertEqual(result.summary["images_failed"], 1)
        self.assertIn("HTTP 503", failure_text)

    def test_dry_run_does_not_create_output(self):
        album = AlbumConfig("test", "测试", "album", "测试输出")
        posts = [
            {
                "post_id": "ok",
                "rank": 1,
                "title": "曲谱",
                "cate": "pic",
                "has_right": 1,
                "cover": "",
                "pics": ["https://cdn.test/score.jpg"],
            }
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_root = Path(temporary_directory) / "output"
            result = AlbumRunner(object(), StubDownloader(), workers=1).run_posts(
                album, posts, output_root, "dry-run"
            )
            self.assertFalse(result.output_directory.exists())


class CLITests(unittest.TestCase):
    def test_default_album_choices_include_whistle(self):
        albums = {album.key: album for album in DEFAULT_ALBUMS}
        self.assertEqual(albums["whistle"].title, "哨笛")
        self.assertEqual(albums["whistle"].album_id, "73f44a40b65411eca6d752540025c377")
        self.assertEqual(albums["whistle"].output_directory, "哨笛")

    def test_token_prefers_environment_without_prompt(self):
        prompt = mock.Mock(side_effect=AssertionError("prompt should not run"))
        self.assertEqual(
            resolve_token({"AFDIAN_AUTH_TOKEN": " secret "}, prompt=prompt), "secret"
        )

    def test_empty_prompt_is_an_authentication_error(self):
        with self.assertRaises(AuthenticationError):
            resolve_token({}, prompt=lambda _: "")

    def test_parser_has_no_token_argument(self):
        destinations = {action.dest for action in build_parser()._actions}
        self.assertNotIn("token", destinations)
        self.assertNotIn("auth_token", destinations)


if __name__ == "__main__":
    unittest.main()

