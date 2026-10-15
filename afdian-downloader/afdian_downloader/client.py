"""Small stdlib-only client for the Afdian web API."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional

from .errors import APIError, AuthenticationError, NetworkError, ProtocolError


DEFAULT_BASE_URL = "https://afdian.com"
CATALOG_ENDPOINT = "/api/user/get-album-catalog"
RANKED_POST_ENDPOINT = "/api/user/get-album-post"
PROFILE_ENDPOINT = "/api/my/profile"
USER_AGENT = "AfdianScoreDownloader/1.0 (+personal archival use)"


def _origin(url: str) -> Any:
    parsed = urllib.parse.urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port)


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward the explicit auth cookie to another origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Optional[urllib.request.Request]:
        absolute_url = urllib.parse.urljoin(request.full_url, new_url)
        if _origin(request.full_url) != _origin(absolute_url):
            raise urllib.error.HTTPError(
                absolute_url,
                code,
                "拒绝携带认证 Cookie 跳转到其他站点",
                headers,
                file_pointer,
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, absolute_url
        )


def _is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "null")
    return bool(value)


def _safe_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise AuthenticationError("认证 token 不能为空")
    if any(character in value for character in ("\r", "\n", ";")):
        raise AuthenticationError("认证 token 含有非法字符")
    return value


class AfdianClient:
    """Read album posts with an auth token held only in memory."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        retries: int = 3,
        opener: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self._token = _safe_token(token)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self._opener = opener or urllib.request.build_opener(SameOriginRedirectHandler())
        self._sleep = sleep

    def _request_json(self, endpoint: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = "{}{}".format(self.base_url, endpoint)
        if query:
            url = "{}?{}".format(url, query)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Cookie": "auth_token={}".format(self._token),
                "User-Agent": USER_AGENT,
            },
        )

        last_error: Optional[BaseException] = None
        for attempt in range(self.retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read()
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ProtocolError("Afdian API 返回的 JSON 顶层不是对象")
                return payload
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in (408, 425, 429) and error.code < 500:
                    raise NetworkError("Afdian API 请求失败：HTTP {}".format(error.code)) from error
            except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as error:
                last_error = error
            if attempt < self.retries:
                self._sleep(min(0.5 * (2 ** attempt), 8.0))

        detail = type(last_error).__name__ if last_error is not None else "unknown error"
        raise NetworkError("Afdian API 请求在重试后仍失败：{}".format(detail)) from last_error

    @staticmethod
    def _response_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if payload.get("ec") != 200:
            message = str(payload.get("em") or "未知 API 错误")
            if payload.get("ec") in (40100, 40101):
                raise AuthenticationError("爱发电登录凭证无效或已过期：{}".format(message))
            raise APIError("Afdian API 拒绝请求：{}".format(message))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProtocolError("Afdian API 响应缺少 data 对象")
        return data

    @staticmethod
    def _page_list(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
        page_items = data.get("list")
        if not isinstance(page_items, list):
            raise ProtocolError("Afdian API 响应缺少 data.list 数组")
        if not all(isinstance(item, dict) for item in page_items):
            raise ProtocolError("Afdian API 的 data.list 含有非对象条目")
        return page_items

    def validate_session(self) -> None:
        """Fail early instead of silently treating an invalid token as a locked catalog."""
        payload = self._request_json(PROFILE_ENDPOINT, {})
        data = self._response_data(payload)
        user = data.get("user")
        user_id = user.get("user_id") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id.strip():
            raise AuthenticationError("爱发电会话未返回有效的登录用户")

    @staticmethod
    def _append_unique(
        destination: List[Dict[str, Any]],
        page_items: List[Dict[str, Any]],
        seen_post_ids: set,
    ) -> int:
        added = 0
        for item in page_items:
            post_id = str(item.get("post_id") or "").strip()
            if not post_id:
                raise ProtocolError("Afdian API 返回了缺少 post_id 的条目")
            if post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)
            destination.append(item)
            added += 1
        return added

    def fetch_album_catalog(self, album_id: str, per_page: int = 100) -> List[Dict[str, Any]]:
        """Fetch the complete album catalog using page-number pagination."""
        if per_page <= 0:
            raise ValueError("per_page must be positive")
        posts: List[Dict[str, Any]] = []
        seen_post_ids: set = set()
        page = 1

        while True:
            payload = self._request_json(
                CATALOG_ENDPOINT,
                {"album_id": album_id, "page": page, "per_page": per_page},
            )
            data = self._response_data(payload)
            page_items = self._page_list(data)
            added = self._append_unique(posts, page_items, seen_post_ids)
            has_more = _is_truthy(data.get("has_more"))
            if not has_more:
                break
            if not page_items or added == 0:
                raise ProtocolError("专辑目录分页没有前进，已停止以避免死循环")
            page += 1

        return posts

    def fetch_album_posts_ranked(self, album_id: str) -> List[Dict[str, Any]]:
        """Fetch via the ranked endpoint retained as a verified compatibility path."""
        posts: List[Dict[str, Any]] = []
        seen_post_ids: set = set()
        seen_cursors: set = set()
        last_rank = ""

        while True:
            payload = self._request_json(
                RANKED_POST_ENDPOINT,
                {
                    "album_id": album_id,
                    "lastRank": last_rank,
                    "rankOrder": "asc",
                    "rankField": "rank",
                },
            )
            data = self._response_data(payload)
            page_items = self._page_list(data)
            added = self._append_unique(posts, page_items, seen_post_ids)
            has_more = _is_truthy(data.get("has_more"))
            if not has_more:
                break
            if not page_items or added == 0:
                raise ProtocolError("rank 分页没有返回新条目，已停止以避免死循环")
            next_rank = str(page_items[-1].get("rank") or "").strip()
            if not next_rank or next_rank == last_rank or next_rank in seen_cursors:
                raise ProtocolError("rank 分页游标没有前进，已停止以避免死循环")
            seen_cursors.add(next_rank)
            last_rank = next_rank

        return posts

    def fetch_album_posts(self, album_id: str) -> List[Dict[str, Any]]:
        """Primary workflow: the catalog endpoint exposes type and cover reliably."""
        return self.fetch_album_catalog(album_id)

