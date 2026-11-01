#!/usr/bin/env python3
"""Dependency-free MCP server for YouTube metadata, thumbnails, and analytics."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REDIRECT_URI = "http://127.0.0.1:8765/oauth2callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
YOUTUBE_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"
PRIVACY_STATUSES = ("private", "unlisted", "public")
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # must be a multiple of 256 KiB
UPLOAD_RETRIES = 3
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
CAPTION_FORMATS = ("srt", "vtt", "sbv", "scc", "ttml")
VIDEO_LICENSES = ("youtube", "creativeCommon")
VIDEO_FLAG_SCHEMA = {
    "made_for_kids": {
        "type": "boolean",
        "description": "Declare the video as made for kids (COPPA). Cannot be undone freely; YouTube may override it.",
    },
    "embeddable": {"type": "boolean", "description": "Allow embedding on other sites."},
    "license": {"type": "string", "enum": list(VIDEO_LICENSES), "description": "'youtube' (standard) or 'creativeCommon'."},
    "public_stats_viewable": {"type": "boolean", "description": "Show like and view counts publicly."},
    "contains_synthetic_media": {
        "type": "boolean",
        "description": "Disclose realistic altered or synthetic (AI-generated) content.",
    },
}
MODERATION_STATUSES = ("published", "heldForReview", "rejected")
MAX_COMMENT_LENGTH = 10_000
COMMENT_ORDERS = ("time", "relevance")
THREAD_MODERATION_FILTERS = ("published", "heldForReview", "likelySpam")
ANALYTICS_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
ANALYTICS_SORT_RE = re.compile(r"^-?[A-Za-z][A-Za-z0-9]*$")
ANALYTICS_FILTER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*==[^;=]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Hints for MCP clients so they can auto-allow reads and confirm writes.
# (title, readOnly, destructive, idempotent, openWorld)
#   destructive = overwrites or removes existing data; additive writes are not.
#   openWorld   = talks to YouTube/Google rather than only local files.
_TOOL_HINTS = {
    "youtube_auth_status": ("Check YouTube auth status", True, False, True, False),
    "youtube_start_auth": ("Start YouTube authorization", True, False, False, False),
    "youtube_channel_overview": ("Channel overview", True, False, True, True),
    "youtube_list_videos": ("List videos", True, False, True, True),
    "youtube_get_video": ("Get video", True, False, True, True),
    "youtube_update_video": ("Update video metadata", False, True, True, True),
    "youtube_upload_video": ("Upload video", False, False, False, True),
    "youtube_upload_thumbnail": ("Upload thumbnail", False, True, True, True),
    "youtube_analytics_query": ("Analytics query", True, False, True, True),
    "youtube_quota_status": ("Quota status", True, False, True, False),
    "youtube_list_playlists": ("List playlists", True, False, True, True),
    "youtube_list_playlist_items": ("List playlist items", True, False, True, True),
    "youtube_create_playlist": ("Create playlist", False, False, False, True),
    "youtube_update_playlist": ("Update playlist", False, True, True, True),
    "youtube_delete_playlist": ("Delete playlist", False, True, True, True),
    "youtube_add_playlist_item": ("Add video to playlist", False, False, False, True),
    "youtube_remove_playlist_item": ("Remove video from playlist", False, True, True, True),
    "youtube_move_playlist_item": ("Reorder playlist item", False, False, True, True),
    "youtube_reply_to_comment": ("Reply to comment", False, False, False, True),
    "youtube_list_replies": ("List replies", True, False, True, True),
    "youtube_delete_comment": ("Delete comment", False, True, True, True),
    "youtube_moderate_comments": ("Moderate comments", False, True, True, True),
    "youtube_list_channel_comments": ("List channel comments", True, False, True, True),
    "youtube_list_captions": ("List captions", True, False, True, True),
    "youtube_upload_caption": ("Upload caption", False, False, False, True),
    "youtube_update_caption": ("Update caption", False, True, True, True),
    "youtube_delete_caption": ("Delete caption", False, True, True, True),
    # not read-only: may write a local file when output_path is given
    "youtube_download_caption": ("Download caption", False, False, True, True),
    "youtube_channel_analytics": ("Channel analytics", True, False, True, True),
    "youtube_video_analytics": ("Video analytics", True, False, True, True),
    "youtube_post_comment": ("Post comment", False, False, False, True),
    "youtube_list_comments": ("List comments", True, False, True, True),
}


def tool_annotations(name: str) -> dict[str, Any]:
    title, read_only, destructive, idempotent, open_world = _TOOL_HINTS[name]
    annotations: dict[str, Any] = {"title": title, "readOnlyHint": read_only, "openWorldHint": open_world}
    if not read_only:  # destructive/idempotent hints only apply to writes
        annotations["destructiveHint"] = destructive
        annotations["idempotentHint"] = idempotent
    return annotations


SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def abs_path(path_value: str | None) -> Path:
    if not path_value:
        raise ValueError("Missing path value.")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = PLUGIN_ROOT / path
    return path.resolve()


def normalize_publish_at(value: str) -> str:
    """Validate a scheduled publish time and return it as UTC ('...Z').

    Needs an explicit UTC offset (or 'Z') so the schedule never depends on
    the server's time zone, and must lie in the future.
    """
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            "publish_at must be an ISO 8601 timestamp with UTC offset, "
            "for example 2026-11-05T17:00:00+01:00 or 2026-11-05T16:00:00Z."
        ) from None
    if parsed.tzinfo is None:
        raise ValueError(
            "publish_at needs a UTC offset (for example +01:00 or Z), "
            "otherwise the time zone is ambiguous."
        )
    utc = parsed.astimezone(timezone.utc)
    if utc <= datetime.now(timezone.utc):
        raise ValueError("publish_at must be in the future.")
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def status_flags(
    *,
    made_for_kids: Any = None,
    embeddable: Any = None,
    license: Any = None,
    public_stats_viewable: Any = None,
    contains_synthetic_media: Any = None,
) -> dict[str, Any]:
    """Validate the optional video status fields and map them to API names (only those given).

    Booleans must be real booleans: the text "false" must never turn into True.
    """
    given = {
        "selfDeclaredMadeForKids": ("made_for_kids", made_for_kids),
        "embeddable": ("embeddable", embeddable),
        "publicStatsViewable": ("public_stats_viewable", public_stats_viewable),
        "containsSyntheticMedia": ("contains_synthetic_media", contains_synthetic_media),
    }
    result: dict[str, Any] = {}
    for api_name, (arg_name, value) in given.items():
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"{arg_name} must be true or false.")
        result[api_name] = value
    if license is not None:
        if license not in VIDEO_LICENSES:
            raise ValueError(f"license must be one of {', '.join(VIDEO_LICENSES)}.")
        result["license"] = license
    return result


def build_multipart(
    metadata: dict[str, Any], media: bytes, media_type: str = "application/octet-stream"
) -> tuple[str, bytes]:
    """multipart/related body (JSON metadata + media) as Google upload endpoints expect."""
    boundary = secrets.token_hex(16)
    parts = [
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8"),
        json.dumps(metadata).encode("utf-8"),
        f"\r\n--{boundary}\r\nContent-Type: {media_type}\r\n\r\n".encode("utf-8"),
        media,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return f"multipart/related; boundary={boundary}", b"".join(parts)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


class ApiError(RuntimeError):
    """HTTP error from a Google API; keeps status and reason for hints and retries."""

    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def parse_api_error(status: int, body: str, fallback: str = "") -> ApiError:
    """Build an ApiError from a Google error body.

    Handles the API shape {"error": {"message", "errors": [{"reason"}]}} and the
    OAuth token endpoint shape {"error": "invalid_grant", "error_description": ...}.
    """
    message, reason = body or fallback, None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or message
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = errors[0].get("reason")
        reason = reason or error.get("status")
    elif isinstance(error, str):
        reason = error
        message = parsed.get("error_description") or error
    return ApiError(status, message, reason)


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}  # DELETE answers 204 No Content
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise parse_api_error(exc.code, body, str(exc)) from exc


def http_raw(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    data: bytes | None = None,
    timeout: int = 300,
) -> tuple[int, Any, bytes]:
    """Return (status, headers, body). 308 (resume incomplete) is a normal answer."""
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code == 308:
            return 308, exc.headers, body
        raise parse_api_error(exc.code, body.decode("utf-8", errors="replace"), str(exc)) from exc


_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
_REAUTH = "Run `python scripts/auth.py auth` in the youtube-studio-mcp folder, then reconnect the server."


def explain_error(exc: BaseException) -> str:
    """Turn any exception raised by a tool into a message that says what to do next."""
    if isinstance(exc, ApiError):
        reason, status = exc.reason or "", exc.status
        detail = f"HTTP {status}" + (f" {reason}" if reason else "") + f": {exc}"
        if reason in _QUOTA_REASONS:
            hint = (
                "The YouTube API quota is used up (default 10,000 units per day, resets at midnight "
                "Pacific Time). Try again tomorrow, or request more quota in the Google Cloud console. "
                "Uploads cost 1,600 units, captions 50-450, most reads 1."
            )
        elif reason == "invalid_grant":
            hint = (
                "Google rejected the saved login (revoked, expired or password changed). "
                "OAuth apps in 'Testing' mode expire after 7 days. " + _REAUTH
            )
        elif status == 401 or reason in {"authError", "invalid_client", "UNAUTHENTICATED"}:
            hint = "The login is missing or invalid. " + _REAUTH
        elif reason in {"insufficientPermissions", "ACCESS_NOT_CONFIGURED", "accessNotConfigured"}:
            hint = (
                "Missing permission or API. Check that YouTube Data API v3 and YouTube Analytics API "
                "are enabled in the Google Cloud project and that all scopes were granted. " + _REAUTH
            )
        elif reason in {"commentsDisabled", "forbidden"} or status == 403:
            hint = "YouTube refused this action (for example not the owner, comments disabled, or a restricted feature)."
        elif status == 404 or reason in {"videoNotFound", "playlistNotFound", "notFound", "captionNotFound"}:
            hint = "Not found. Check the ID (video, playlist, playlist item and caption ids are different things)."
        elif reason == "uploadLimitExceeded":
            hint = "The channel hit YouTube's upload limit. Try again later."
        elif status >= 500:
            hint = "YouTube had a temporary problem. Try again in a minute."
        elif status == 400:
            hint = (
                "The request was invalid. Check ids and dates and, for analytics, that the metrics, "
                "dimensions, filters and sort can be combined."
            )
        else:
            hint = "YouTube rejected the request."
        return f"{hint}\n\nDetails: {detail}"
    if isinstance(exc, KeyError):
        return f"Missing required argument: {exc.args[0]}"
    if isinstance(exc, ValueError):
        return f"Invalid input: {exc}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return f"Network problem while contacting Google: {getattr(exc, 'reason', exc)}. Check the connection and try again."
    if isinstance(exc, RuntimeError):
        return str(exc)
    return f"Unexpected error ({type(exc).__name__}): {exc}"


class UnknownTool(RuntimeError):
    """Protocol-level error: the client called a tool that does not exist."""


# --- Quota accounting -------------------------------------------------------
# YouTube Data API units per call (https://developers.google.com/youtube/v3/determine_quota_cost).
# The Analytics API has its own quota and is not counted here.
DEFAULT_DAILY_QUOTA = 10_000
VIDEO_UPLOAD_COST = 1600
_COSTS = {
    ("search", "GET"): 100,
    ("captions", "GET"): 50,
    ("captions", "PUT"): 450,
    ("captions", "POST"): 400,
    ("captions", "DELETE"): 50,
    ("thumbnails", "POST"): 50,
}


def request_cost(base_url: str, path: str, method: str) -> int:
    """Quota units one successful call costs (0 for APIs outside the YouTube Data quota)."""
    if base_url == YOUTUBE_ANALYTICS_BASE:
        return 0
    parts = [p for p in path.split("/") if p]
    resource = parts[0] if parts else ""
    if resource == "captions" and len(parts) > 1 and method == "GET":
        return 200  # captions.download
    if (resource, method) in _COSTS:
        return _COSTS[(resource, method)]
    return 1 if method == "GET" else 50


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = datetime(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + 7 * (n - 1)


def pacific_offset(utc: datetime) -> timedelta:
    """UTC offset of US Pacific time at a UTC instant (DST: 2nd Sun March to 1st Sun Nov)."""
    year = utc.year
    start = datetime(year, 3, _nth_sunday(year, 3, 2), 10, tzinfo=timezone.utc)  # 02:00 PST
    end = datetime(year, 11, _nth_sunday(year, 11, 1), 9, tzinfo=timezone.utc)  # 02:00 PDT
    return timedelta(hours=-7) if start <= utc < end else timedelta(hours=-8)


def quota_day(now: datetime | None = None) -> str:
    """The quota day (Pacific calendar date) a moment belongs to."""
    now = now or datetime.now(timezone.utc)
    return (now + pacific_offset(now)).date().isoformat()


def next_quota_reset(now: datetime | None = None) -> datetime:
    """Next midnight Pacific time, as a UTC datetime."""
    now = now or datetime.now(timezone.utc)
    local = now + pacific_offset(now)
    midnight = datetime(local.year, local.month, local.day) + timedelta(days=1)
    for hours in (-7, -8):
        candidate = (midnight - timedelta(hours=hours)).replace(tzinfo=timezone.utc)
        if pacific_offset(candidate) == timedelta(hours=hours):
            return candidate
    return (midnight + timedelta(hours=8)).replace(tzinfo=timezone.utc)


class QuotaTracker:
    """Counts the units this server spends per quota day, persisted so restarts keep the total.

    It is an estimate: only calls made through this server count, and failed calls are not counted.
    """

    def __init__(self, path: Path | None = None, limit: int = DEFAULT_DAILY_QUOTA):
        self.path = path
        self.limit = limit
        self.day = quota_day()
        self.used = 0
        self.calls: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            saved = read_json(self.path)
            if saved.get("day") == self.day:
                self.used = int(saved.get("used", 0))
                self.calls = {str(k): int(v) for k, v in saved.get("calls", {}).items()}
        except (OSError, ValueError, AttributeError):
            pass  # unreadable counter file: start from zero

    def _roll(self, now: datetime | None) -> None:
        today = quota_day(now)
        if today != self.day:
            self.day, self.used, self.calls = today, 0, {}

    def add(self, units: int, label: str, now: datetime | None = None) -> None:
        if units <= 0:
            return
        self._roll(now)
        self.used += units
        self.calls[label] = self.calls.get(label, 0) + units
        if self.path:
            try:
                write_json(self.path, {"day": self.day, "used": self.used, "calls": self.calls})
            except OSError:
                pass  # never fail a request because the counter cannot be saved

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        self._roll(now)
        remaining = max(self.limit - self.used, 0)
        result: dict[str, Any] = {
            "quota_day": self.day,
            "resets_at_utc": next_quota_reset(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "daily_limit": self.limit,
            "used_estimate": self.used,
            "remaining_estimate": remaining,
            "breakdown": dict(sorted(self.calls.items(), key=lambda kv: -kv[1])),
            "note": (
                "Estimate of YouTube Data API units spent through this server today (Pacific day). "
                "Other apps using the same Google Cloud project, failed calls and the separate "
                "Analytics API are not included. Compare with the Google Cloud console for the exact figure."
            ),
        }
        if self.used >= self.limit * 0.8:
            result["warning"] = f"{self.used} of {self.limit} units used; a video upload costs {VIDEO_UPLOAD_COST}."
        return result


@dataclass
class AuthConfig:
    client_secrets_path: Path
    token_path: Path

    def load_client_config(self) -> dict[str, Any]:
        payload = read_json(self.client_secrets_path)
        client = payload.get("installed") or payload.get("web")
        if not client:
            raise RuntimeError(
                "client_secret.json must contain an 'installed' or 'web' client definition."
            )
        return client

    def load_token(self) -> dict[str, Any]:
        if not self.token_path.exists():
            raise RuntimeError(
                f"Token file not found at {self.token_path}. Run scripts/auth.py first."
            )
        return read_json(self.token_path)

    def save_token(self, payload: dict[str, Any]) -> None:
        write_json(self.token_path, payload)

    def auth_status(self) -> dict[str, Any]:
        return {
            "client_secrets_exists": self.client_secrets_path.exists(),
            "token_exists": self.token_path.exists(),
            "client_secrets_path": str(self.client_secrets_path),
            "token_path": str(self.token_path),
        }


class YouTubeClient:
    def __init__(self, auth: AuthConfig, quota: QuotaTracker | None = None):
        self.auth = auth
        self.quota = quota or QuotaTracker()
        self._plan: dict[str, Any] | None = None  # set while a dry run is active

    def _refresh_token(self, token: dict[str, Any]) -> dict[str, Any]:
        client = self.auth.load_client_config()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Token file does not contain a refresh_token.")
        payload = urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "client_secret": client.get("client_secret", ""),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        refreshed = http_json(
            TOKEN_URL,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
        refreshed["refresh_token"] = refresh_token
        refreshed["created_at"] = int(time.time())
        self.auth.save_token(refreshed)
        return refreshed

    def _access_token(self) -> str:
        token = self.auth.load_token()
        expires_in = int(token.get("expires_in", 0))
        created_at = int(token.get("created_at", 0))
        if not token.get("access_token"):
            raise RuntimeError("Token file is missing access_token.")
        if created_at + max(expires_in - 120, 0) <= int(time.time()):
            token = self._refresh_token(token)
        return token["access_token"]

    @contextmanager
    def dry_run(self):
        """While active, writes are recorded in the yielded plan instead of being sent.

        Reads still run (validation and before/after need them); only non-GET requests,
        uploads and local file writes are intercepted.
        """
        plan: dict[str, Any] = {"requests": [], "changes": {}, "targets": []}
        self._plan = plan
        try:
            yield plan
        finally:
            self._plan = None

    def _note_changes(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        if self._plan is None:
            return
        changes: dict[str, Any] = {}
        for section, new in after.items():
            old = before.get(section, {})
            for key in sorted(set(old) | set(new)):
                if old.get(key) != new.get(key):
                    changes[f"{section}.{key}"] = {"before": old.get(key), "after": new.get(key)}
        self._plan["changes"] = changes

    def _preview_targets(self, kind: str, ids: list[str]) -> None:
        """In a dry run, look up what the ids point to so they can be checked before deleting."""
        if self._plan is None:
            return
        paths = {"comments": "/comments", "playlists": "/playlists",
                 "playlistItems": "/playlistItems", "captions": "/captions"}
        parts = {"comments": "snippet", "playlists": "snippet,status",
                 "playlistItems": "snippet", "captions": "snippet"}
        items = self._request(
            YOUTUBE_API_BASE,
            paths[kind],
            query={
                "part": parts[kind],
                "id": ",".join(ids),
                "textFormat": "plainText" if kind == "comments" else None,
            },
        ).get("items", [])

        def brief(item: dict[str, Any]) -> dict[str, Any]:
            snippet = item.get("snippet", {})
            if kind == "comments":
                return {"author": snippet.get("authorDisplayName"),
                        "text": (snippet.get("textDisplay") or "")[:200],
                        "videoId": snippet.get("videoId")}
            if kind == "playlists":
                return {"title": snippet.get("title"),
                        "privacyStatus": item.get("status", {}).get("privacyStatus")}
            if kind == "playlistItems":
                return {"title": snippet.get("title"), "playlistId": snippet.get("playlistId"),
                        "position": snippet.get("position")}
            return {"language": snippet.get("language"), "name": snippet.get("name"),
                    "videoId": snippet.get("videoId")}

        found = {item["id"]: brief(item) for item in items}
        self._plan["targets"] = [
            {"id": i, **found.get(i, {"warning": "not found"})} for i in ids
        ]

    def _request(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        raw_data: bytes | None = None,
        as_text: bool = False,
    ) -> dict[str, Any]:
        query_string = ""
        if query:
            cleaned = {key: value for key, value in query.items() if value is not None}
            query_string = "?" + urllib.parse.urlencode(cleaned, doseq=True)
        url = f"{base_url}{path}{query_string}"
        if self._plan is not None and method != "GET":
            self._plan["requests"].append(
                {
                    "method": method,
                    "endpoint": path,
                    "query": {k: v for k, v in (query or {}).items() if v is not None},
                    **({"body": body} if body is not None else {}),
                    **({"payload_bytes": len(raw_data)} if raw_data else {}),
                    "quota_cost": request_cost(base_url, path, method),
                }
            )
            return {}
        request_headers = {
            "Authorization": f"Bearer {self._access_token()}",
        }
        if headers:
            request_headers.update(headers)
        data = raw_data
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if as_text:  # non-JSON answer (caption download)
            _, _, content = http_raw(url, method=method, headers=request_headers, data=data, timeout=60)
            result = {"content": content.decode("utf-8-sig")}
        else:
            result = http_json(url, method=method, headers=request_headers, data=data)
        self.quota.add(request_cost(base_url, path, method), f"{method} {path}")
        return result

    def channel_overview(self) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE,
            "/channels",
            query={"part": "snippet,statistics,brandingSettings,contentDetails", "mine": "true"},
        )

    def list_videos(self, max_results: int = 10, page_token: str | None = None) -> dict[str, Any]:
        channel = self.channel_overview()
        channels = channel.get("items", [])
        if not channels:
            raise RuntimeError("No authenticated YouTube channel was returned.")
        uploads_playlist = (
            channels[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        )
        if not uploads_playlist:
            raise RuntimeError("Could not find the uploads playlist for the authenticated channel.")
        playlist = self._request(
            YOUTUBE_API_BASE,
            "/playlistItems",
            query={
                "part": "snippet,contentDetails,status",
                "playlistId": uploads_playlist,
                "maxResults": max(1, min(max_results, 25)),
                "pageToken": page_token,
            },
        )
        video_ids = [
            item.get("contentDetails", {}).get("videoId")
            for item in playlist.get("items", [])
            if item.get("contentDetails", {}).get("videoId")
        ]
        details = {}
        if video_ids:
            details_response = self._request(
                YOUTUBE_API_BASE,
                "/videos",
                query={
                    "part": "snippet,statistics,status,contentDetails",
                    "id": ",".join(video_ids),
                },
            )
            details = {item["id"]: item for item in details_response.get("items", [])}
        merged = []
        for item in playlist.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            merged.append(
                {
                    "playlistItem": item,
                    "details": details.get(video_id),
                }
            )
        return {
            "items": merged,
            "nextPageToken": playlist.get("nextPageToken"),
            "pageInfo": playlist.get("pageInfo", {}),
        }

    def get_video(self, video_id: str) -> dict[str, Any]:
        result = self._request(
            YOUTUBE_API_BASE,
            "/videos",
            query={"part": "snippet,statistics,status,contentDetails", "id": video_id},
        )
        items = result.get("items", [])
        if not items:
            raise RuntimeError(f"Video {video_id} not found.")
        return items[0]

    def update_video(
        self,
        video_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str | None = None,
        default_language: str | None = None,
        privacy_status: str | None = None,
        publish_at: str | None = None,
        made_for_kids: bool | None = None,
        embeddable: bool | None = None,
        license: str | None = None,
        public_stats_viewable: bool | None = None,
        contains_synthetic_media: bool | None = None,
    ) -> dict[str, Any]:
        """publish_at: ISO 8601 with offset schedules the video (forces private
        until then); an empty string removes an existing schedule."""
        scheduled = normalize_publish_at(publish_at) if publish_at else None
        if scheduled and privacy_status not in (None, "private"):
            raise ValueError("A scheduled video must be private until publish_at.")
        flags = status_flags(
            made_for_kids=made_for_kids,
            embeddable=embeddable,
            license=license,
            public_stats_viewable=public_stats_viewable,
            contains_synthetic_media=contains_synthetic_media,
        )
        existing = self.get_video(video_id)
        before = copy.deepcopy({"snippet": existing["snippet"], "status": existing["status"]})
        snippet = existing["snippet"]
        status = existing["status"]
        snippet["title"] = title if title is not None else snippet.get("title", "")
        snippet["description"] = (
            description if description is not None else snippet.get("description", "")
        )
        if tags is not None:
            snippet["tags"] = tags
        if category_id is not None:
            snippet["categoryId"] = category_id
        if default_language is not None:
            snippet["defaultLanguage"] = default_language
        if privacy_status is not None:
            status["privacyStatus"] = privacy_status
        status.update(flags)
        if scheduled:
            status["privacyStatus"] = "private"
            status["publishAt"] = scheduled
        elif publish_at == "" or (privacy_status is not None and privacy_status != "private"):
            status.pop("publishAt", None)  # clearing, or publishing now
        body = {
            "id": video_id,
            "snippet": snippet,
            "status": status,
        }
        self._note_changes(before, {"snippet": snippet, "status": status})
        return self._request(
            YOUTUBE_API_BASE,
            "/videos",
            method="PUT",
            query={"part": "snippet,status"},
            body=body,
        )

    def upload_thumbnail(self, video_id: str, image_path: str) -> dict[str, Any]:
        path = abs_path(image_path)
        if not path.exists():
            raise RuntimeError(f"Thumbnail file not found: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        return self._request(
            YOUTUBE_UPLOAD_BASE,
            "/thumbnails/set",
            method="POST",
            query={"videoId": video_id, "uploadType": "media"},
            headers={"Content-Type": mime},
            raw_data=body,
        )

    def upload_video(
        self,
        file_path: str,
        title: str,
        *,
        description: str = "",
        tags: list[str] | None = None,
        category_id: str = "22",
        default_language: str | None = None,
        privacy_status: str = "private",
        made_for_kids: bool = False,
        publish_at: str | None = None,
        embeddable: bool | None = None,
        license: str | None = None,
        public_stats_viewable: bool | None = None,
        contains_synthetic_media: bool | None = None,
    ) -> dict[str, Any]:
        scheduled = normalize_publish_at(publish_at) if publish_at else None
        if scheduled and privacy_status != "private":
            raise ValueError("A scheduled video must be private until publish_at.")
        if not isinstance(made_for_kids, bool):
            raise ValueError("made_for_kids must be true or false.")
        flags = status_flags(
            embeddable=embeddable,
            license=license,
            public_stats_viewable=public_stats_viewable,
            contains_synthetic_media=contains_synthetic_media,
        )
        path = abs_path(file_path)
        if not path.is_file():
            raise RuntimeError(f"Video file not found: {path}")
        size = path.stat().st_size
        if size == 0:
            raise RuntimeError(f"Video file is empty: {path}")
        if not title or not title.strip():
            raise ValueError("title must not be empty.")
        if len(title) > 100:
            raise ValueError("title must be at most 100 characters.")
        if privacy_status not in PRIVACY_STATUSES:
            raise ValueError(f"privacy_status must be one of {', '.join(PRIVACY_STATUSES)}.")
        mime = mimetypes.guess_type(path.name)[0] or "video/*"
        snippet: dict[str, Any] = {
            "title": title,
            "description": description,
            "categoryId": category_id,
        }
        if tags:
            snippet["tags"] = tags
        if default_language:
            snippet["defaultLanguage"] = default_language
        metadata = {
            "snippet": snippet,
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }
        metadata["status"].update(flags)
        if scheduled:
            metadata["status"]["publishAt"] = scheduled
        if self._plan is not None:
            self._plan["requests"].append(
                {
                    "method": "POST",
                    "endpoint": "upload/videos (resumable)",
                    "file": str(path),
                    "size_bytes": size,
                    "metadata": metadata,
                    "quota_cost": VIDEO_UPLOAD_COST,
                }
            )
            return {}
        session_url = self._start_resumable_upload(metadata, size, mime)
        video = self._send_resumable_upload(session_url, path, size, mime)
        self.quota.add(VIDEO_UPLOAD_COST, "POST /videos (upload)")
        if video.get("id"):
            video["watchUrl"] = f"https://youtu.be/{video['id']}"
        return video

    def _start_resumable_upload(self, metadata: dict[str, Any], size: int, mime: str) -> str:
        _, headers, _ = http_raw(
            f"{YOUTUBE_UPLOAD_BASE}/videos?uploadType=resumable&part=snippet,status",
            method="POST",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": mime,
            },
            data=json.dumps(metadata).encode("utf-8"),
        )
        location = headers.get("Location")
        if not location:
            raise RuntimeError("YouTube did not return an upload session URL.")
        return location

    def _upload_put(
        self, session_url: str, mime: str, content_range: str, data: bytes
    ) -> tuple[int, Any, bytes]:
        return http_raw(
            session_url,
            method="PUT",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": mime,
                "Content-Range": content_range,
            },
            data=data,
        )

    @staticmethod
    def _next_offset(headers: Any) -> int:
        """Bytes already stored by YouTube, from the 'Range: bytes=0-N' header."""
        received = headers.get("Range")
        return int(received.split("-")[-1]) + 1 if received else 0

    def _send_resumable_upload(
        self, session_url: str, path: Path, size: int, mime: str
    ) -> dict[str, Any]:
        offset = 0
        failures = 0
        with path.open("rb") as handle:
            while True:
                handle.seek(offset)
                chunk = handle.read(UPLOAD_CHUNK_SIZE)
                end = offset + len(chunk) - 1
                try:
                    status, headers, body = self._upload_put(
                        session_url, mime, f"bytes {offset}-{end}/{size}", chunk
                    )
                except (ApiError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                    retryable = not isinstance(exc, ApiError) or exc.status >= 500
                    failures += 1
                    if not retryable or failures > UPLOAD_RETRIES:
                        raise
                    time.sleep(min(2**failures, 30))
                    status, headers, body = self._upload_put(
                        session_url, mime, f"bytes */{size}", b""
                    )
                    if status == 308:
                        offset = self._next_offset(headers)
                        continue
                if status in (200, 201):
                    return json.loads(body.decode("utf-8")) if body else {}
                if status == 308:
                    failures = 0
                    offset = self._next_offset(headers)
                    continue
                raise RuntimeError(f"Unexpected upload response status {status}.")

    def list_playlists(
        self, max_results: int = 25, page_token: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE,
            "/playlists",
            query={
                "part": "snippet,status,contentDetails",
                "mine": "true",
                "maxResults": max(1, min(max_results, 50)),
                "pageToken": page_token,
            },
        )

    def list_playlist_items(
        self, playlist_id: str, max_results: int = 50, page_token: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE,
            "/playlistItems",
            query={
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": max(1, min(max_results, 50)),
                "pageToken": page_token,
            },
        )

    @staticmethod
    def _check_playlist_fields(title: str | None, privacy_status: str | None) -> None:
        if title is not None and (not title.strip() or len(title) > 150):
            raise ValueError("title must be 1-150 characters.")
        if privacy_status is not None and privacy_status not in PRIVACY_STATUSES:
            raise ValueError(f"privacy_status must be one of {', '.join(PRIVACY_STATUSES)}.")

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        result = self._request(
            YOUTUBE_API_BASE, "/playlists", query={"part": "snippet,status", "id": playlist_id}
        )
        items = result.get("items", [])
        if not items:
            raise RuntimeError(f"Playlist {playlist_id} not found.")
        return items[0]

    def create_playlist(
        self,
        title: str,
        *,
        description: str = "",
        privacy_status: str = "private",
        default_language: str | None = None,
    ) -> dict[str, Any]:
        self._check_playlist_fields(title, privacy_status)
        snippet: dict[str, Any] = {"title": title, "description": description}
        if default_language:
            snippet["defaultLanguage"] = default_language
        return self._request(
            YOUTUBE_API_BASE,
            "/playlists",
            method="POST",
            query={"part": "snippet,status"},
            body={"snippet": snippet, "status": {"privacyStatus": privacy_status}},
        )

    def update_playlist(
        self,
        playlist_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        privacy_status: str | None = None,
        default_language: str | None = None,
    ) -> dict[str, Any]:
        if all(v is None for v in (title, description, privacy_status, default_language)):
            raise ValueError("Give at least one field to change.")
        self._check_playlist_fields(title, privacy_status)
        existing = self.get_playlist(playlist_id)
        old = existing["snippet"]
        snippet: dict[str, Any] = {
            "title": title if title is not None else old.get("title", ""),
            "description": description if description is not None else old.get("description", ""),
        }
        language = default_language if default_language is not None else old.get("defaultLanguage")
        if language:
            snippet["defaultLanguage"] = language
        if old.get("tags"):
            snippet["tags"] = old["tags"]
        status = {
            "privacyStatus": privacy_status
            if privacy_status is not None
            else existing.get("status", {}).get("privacyStatus", "private")
        }
        self._note_changes(
            {"snippet": old, "status": existing.get("status", {})},
            {"snippet": snippet, "status": status},
        )
        return self._request(
            YOUTUBE_API_BASE,
            "/playlists",
            method="PUT",
            query={"part": "snippet,status"},
            body={"id": playlist_id, "snippet": snippet, "status": status},
        )

    def delete_playlist(self, playlist_id: str) -> dict[str, Any]:
        self._preview_targets("playlists", [playlist_id])
        self._request(YOUTUBE_API_BASE, "/playlists", method="DELETE", query={"id": playlist_id})
        return {"deleted": playlist_id}

    def add_playlist_item(
        self, playlist_id: str, video_id: str, position: int | None = None
    ) -> dict[str, Any]:
        if position is not None and position < 0:
            raise ValueError("position must be 0 or greater.")
        snippet: dict[str, Any] = {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
        if position is not None:
            snippet["position"] = position
        return self._request(
            YOUTUBE_API_BASE,
            "/playlistItems",
            method="POST",
            query={"part": "snippet"},
            body={"snippet": snippet},
        )

    def remove_playlist_item(self, item_id: str) -> dict[str, Any]:
        """item_id is the playlist item id (from list_playlist_items), not the video id."""
        self._preview_targets("playlistItems", [item_id])
        self._request(YOUTUBE_API_BASE, "/playlistItems", method="DELETE", query={"id": item_id})
        return {"removed": item_id}

    def move_playlist_item(self, item_id: str, position: int) -> dict[str, Any]:
        if position < 0:
            raise ValueError("position must be 0 or greater.")
        result = self._request(
            YOUTUBE_API_BASE, "/playlistItems", query={"part": "snippet", "id": item_id}
        )
        items = result.get("items", [])
        if not items:
            raise RuntimeError(f"Playlist item {item_id} not found.")
        old = items[0]["snippet"]
        snippet = {
            "playlistId": old["playlistId"],
            "resourceId": old["resourceId"],
            "position": position,
        }
        return self._request(
            YOUTUBE_API_BASE,
            "/playlistItems",
            method="PUT",
            query={"part": "snippet"},
            body={"id": item_id, "snippet": snippet},
        )

    def reply_to_comment(self, parent_id: str, text: str) -> dict[str, Any]:
        """parent_id: id of a top-level comment (YouTube has no replies to replies)."""
        if not text or not text.strip():
            raise ValueError("text must not be empty.")
        if len(text) > MAX_COMMENT_LENGTH:
            raise ValueError(f"text must be at most {MAX_COMMENT_LENGTH} characters.")
        return self._request(
            YOUTUBE_API_BASE,
            "/comments",
            method="POST",
            query={"part": "snippet"},
            body={"snippet": {"parentId": parent_id, "textOriginal": text}},
        )

    def list_replies(
        self, parent_id: str, max_results: int = 20, page_token: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE,
            "/comments",
            query={
                "part": "snippet",
                "parentId": parent_id,
                "maxResults": max(1, min(max_results, 100)),
                "pageToken": page_token,
                "textFormat": "plainText",
            },
        )

    def delete_comment(self, comment_id: str) -> dict[str, Any]:
        self._preview_targets("comments", [comment_id])
        self._request(YOUTUBE_API_BASE, "/comments", method="DELETE", query={"id": comment_id})
        return {"deleted": comment_id}

    def moderate_comments(
        self, comment_ids: list[str], status: str, ban_author: bool = False
    ) -> dict[str, Any]:
        if not comment_ids or not all(isinstance(c, str) and c for c in comment_ids):
            raise ValueError("comment_ids must be a non-empty list of comment ids.")
        if len(comment_ids) > 50:
            raise ValueError("At most 50 comments per call.")
        if status not in MODERATION_STATUSES:
            raise ValueError(f"status must be one of {', '.join(MODERATION_STATUSES)}.")
        if ban_author and status != "rejected":
            raise ValueError("ban_author only works together with status 'rejected'.")
        self._preview_targets("comments", comment_ids)
        self._request(
            YOUTUBE_API_BASE,
            "/comments/setModerationStatus",
            method="POST",
            query={
                "id": ",".join(comment_ids),
                "moderationStatus": status,
                "banAuthor": "true" if ban_author else None,
            },
            raw_data=b"",
        )
        return {"moderated": comment_ids, "status": status, "banned_author": ban_author}

    @staticmethod
    def _analytics_list(value: Any, field: str, pattern: "re.Pattern[str]", required: bool = False) -> list[str]:
        """Accept 'a,b' or ['a', 'b']; check every name so nothing odd reaches the query string."""
        if value is None or value == "" or value == []:
            items: list[str] = []
        elif isinstance(value, str):
            items = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            items = [v.strip() for v in value if v.strip()]
        else:
            raise ValueError(f"{field} must be a comma-separated string or a list of strings.")
        if required and not items:
            raise ValueError(f"{field} must not be empty.")
        for item in items:
            if not pattern.match(item):
                raise ValueError(f"Invalid name in {field}: {item!r}.")
        return items

    @staticmethod
    def _analytics_filters(value: Any) -> list[str]:
        """Accept 'video==ID;country==DE', ['video==ID', ...] or {'video': 'ID', 'country': 'DE'}."""
        if value is None or value == "" or value == [] or value == {}:
            return []
        if isinstance(value, dict):
            parts = [f"{key}=={val}" for key, val in value.items()]
        elif isinstance(value, str):
            parts = [part.strip() for part in value.split(";") if part.strip()]
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            parts = [v.strip() for v in value if v.strip()]
        else:
            raise ValueError("filters must be a string, a list of 'name==value' or an object.")
        for part in parts:
            if not ANALYTICS_FILTER_RE.match(part):
                raise ValueError(f"Invalid filter {part!r}; use name==value, for example video==VIDEO_ID.")
        return parts

    def analytics_query(
        self,
        start_date: str,
        end_date: str,
        metrics: Any,
        dimensions: Any = None,
        filters: Any = None,
        sort: Any = None,
        max_results: int | None = None,
        start_index: int | None = None,
    ) -> dict[str, Any]:
        for field, value in (("start_date", start_date), ("end_date", end_date)):
            if not isinstance(value, str) or not DATE_RE.match(value):
                raise ValueError(f"{field} must look like 2026-10-31.")
            datetime.strptime(value, "%Y-%m-%d")  # rejects 2026-13-45
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date.")
        metric_list = self._analytics_list(metrics, "metrics", ANALYTICS_NAME_RE, required=True)
        dimension_list = self._analytics_list(dimensions, "dimensions", ANALYTICS_NAME_RE)
        sort_list = self._analytics_list(sort, "sort", ANALYTICS_SORT_RE)
        filter_list = self._analytics_filters(filters)
        if max_results is not None and max_results < 1:
            raise ValueError("max_results must be 1 or more.")
        if start_index is not None and start_index < 1:
            raise ValueError("start_index must be 1 or more.")
        response = self._request(
            YOUTUBE_ANALYTICS_BASE,
            "/reports",
            query={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": ",".join(metric_list),
                "dimensions": ",".join(dimension_list) or None,
                "filters": ";".join(filter_list) or None,
                "sort": ",".join(sort_list) or None,
                "maxResults": max_results,
                "startIndex": start_index,
            },
        )
        rows = response.get("rows", [])
        return {
            "columns": [header.get("name") for header in response.get("columnHeaders", [])],
            "rows": rows,
            "row_count": len(rows),
            "query": {
                "start_date": start_date,
                "end_date": end_date,
                "metrics": metric_list,
                "dimensions": dimension_list,
                "filters": filter_list,
                "sort": sort_list,
            },
        }

    def list_captions(self, video_id: str) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE, "/captions", query={"part": "snippet", "videoId": video_id}
        )

    @staticmethod
    def _read_caption_file(file_path: str) -> bytes:
        path = abs_path(file_path)
        if not path.is_file():
            raise RuntimeError(f"Caption file not found: {path}")
        data = path.read_bytes()
        if not data.strip():
            raise RuntimeError(f"Caption file is empty: {path}")
        return data

    def upload_caption(
        self,
        video_id: str,
        file_path: str,
        language: str,
        *,
        name: str = "",
        is_draft: bool = False,
    ) -> dict[str, Any]:
        if not LANGUAGE_RE.match(language or ""):
            raise ValueError("language must be a BCP-47 code such as 'de' or 'en-US'.")
        data = self._read_caption_file(file_path)
        metadata = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": is_draft,
            }
        }
        content_type, body = build_multipart(metadata, data, "application/octet-stream")
        return self._request(
            YOUTUBE_UPLOAD_BASE,
            "/captions",
            method="POST",
            query={"part": "snippet", "uploadType": "multipart"},
            headers={"Content-Type": content_type},
            raw_data=body,
        )

    def update_caption(
        self,
        caption_id: str,
        *,
        file_path: str | None = None,
        is_draft: bool | None = None,
    ) -> dict[str, Any]:
        if file_path is None and is_draft is None:
            raise ValueError("Give file_path (new content) and/or is_draft.")
        metadata: dict[str, Any] = {"id": caption_id}
        if is_draft is not None:
            metadata["snippet"] = {"isDraft": is_draft}
        if file_path is None:
            return self._request(
                YOUTUBE_API_BASE, "/captions", method="PUT", query={"part": "snippet"}, body=metadata
            )
        content_type, body = build_multipart(
            metadata, self._read_caption_file(file_path), "application/octet-stream"
        )
        return self._request(
            YOUTUBE_UPLOAD_BASE,
            "/captions",
            method="PUT",
            query={"part": "snippet", "uploadType": "multipart"},
            headers={"Content-Type": content_type},
            raw_data=body,
        )

    def delete_caption(self, caption_id: str) -> dict[str, Any]:
        self._preview_targets("captions", [caption_id])
        self._request(YOUTUBE_API_BASE, "/captions", method="DELETE", query={"id": caption_id})
        return {"deleted": caption_id}

    def download_caption(
        self, caption_id: str, fmt: str = "srt", output_path: str | None = None
    ) -> dict[str, Any]:
        if fmt not in CAPTION_FORMATS:
            raise ValueError(f"format must be one of {', '.join(CAPTION_FORMATS)}.")
        target = None
        if output_path:
            target = abs_path(output_path)
            if target.exists():
                raise RuntimeError(f"{target} already exists; choose another output_path.")
        if self._plan is not None:
            self._plan["requests"].append(
                {
                    "method": "GET",
                    "endpoint": f"/captions/{caption_id}",
                    "format": fmt,
                    "save_to": str(target) if target else "returned inline",
                    "quota_cost": 200,
                }
            )
            return {"format": fmt}
        text = self._request(
            YOUTUBE_API_BASE,
            f"/captions/{urllib.parse.quote(caption_id, safe='')}",
            query={"tfmt": fmt},
            as_text=True,
        )["content"]
        if target is None:
            return {"format": fmt, "content": text}
        ensure_parent(target)
        target.write_text(text, encoding="utf-8")
        return {"format": fmt, "saved_to": str(target), "characters": len(text)}

    def channel_analytics(self, start_date: str, end_date: str) -> dict[str, Any]:
        return self._request(
            YOUTUBE_ANALYTICS_BASE,
            "/reports",
            query={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": ",".join(
                    [
                        "views",
                        "estimatedMinutesWatched",
                        "averageViewDuration",
                        "averageViewPercentage",
                        "likes",
                        "comments",
                        "shares",
                        "subscribersGained",
                        "subscribersLost",
                    ]
                ),
            },
        )

    def video_analytics(self, video_id: str, start_date: str, end_date: str) -> dict[str, Any]:
        return self._request(
            YOUTUBE_ANALYTICS_BASE,
            "/reports",
            query={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": "day",
                "filters": f"video=={video_id}",
                "metrics": ",".join(
                    [
                        "views",
                        "estimatedMinutesWatched",
                        "averageViewDuration",
                        "likes",
                        "comments",
                        "shares",
                        "subscribersGained",
                    ]
                ),
            },
        )

    def post_comment(self, video_id: str, text: str) -> dict[str, Any]:
        body = {
            "snippet": {
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {
                        "textOriginal": text,
                    }
                },
            }
        }
        return self._request(
            YOUTUBE_API_BASE,
            "/commentThreads",
            method="POST",
            query={"part": "snippet"},
            body=body,
        )

    @staticmethod
    def _check_thread_filters(order: str, moderation_status: str | None) -> None:
        if order not in COMMENT_ORDERS:
            raise ValueError(f"order must be one of {', '.join(COMMENT_ORDERS)}.")
        if moderation_status is not None and moderation_status not in THREAD_MODERATION_FILTERS:
            raise ValueError(
                f"moderation_status must be one of {', '.join(THREAD_MODERATION_FILTERS)}."
            )

    def _comment_threads(
        self,
        target: dict[str, str],
        *,
        max_results: int,
        page_token: str | None,
        order: str,
        search_terms: str | None,
        moderation_status: str | None,
        include_replies: bool,
    ) -> dict[str, Any]:
        return self._request(
            YOUTUBE_API_BASE,
            "/commentThreads",
            query={
                "part": "snippet,replies" if include_replies else "snippet",
                **target,
                "maxResults": max(1, min(max_results, 100)),
                "pageToken": page_token,
                "order": order,
                "searchTerms": search_terms or None,
                "moderationStatus": moderation_status,
                "textFormat": "plainText",
            },
        )

    def list_comments(
        self,
        video_id: str,
        max_results: int = 20,
        page_token: str | None = None,
        order: str = "relevance",
        search_terms: str | None = None,
        moderation_status: str | None = None,
        include_replies: bool = False,
    ) -> dict[str, Any]:
        self._check_thread_filters(order, moderation_status)
        return self._comment_threads(
            {"videoId": video_id},
            max_results=max_results,
            page_token=page_token,
            order=order,
            search_terms=search_terms,
            moderation_status=moderation_status,
            include_replies=include_replies,
        )

    def list_channel_comments(
        self,
        max_results: int = 20,
        page_token: str | None = None,
        order: str = "time",
        search_terms: str | None = None,
        moderation_status: str | None = None,
        include_replies: bool = False,
    ) -> dict[str, Any]:
        """Comment threads on all videos of the authenticated channel, newest first by default."""
        self._check_thread_filters(order, moderation_status)
        channel_id = self._own_channel_id()
        return self._comment_threads(
            {"allThreadsRelatedToChannelId": channel_id},
            max_results=max_results,
            page_token=page_token,
            order=order,
            search_terms=search_terms,
            moderation_status=moderation_status,
            include_replies=include_replies,
        )

    def _own_channel_id(self) -> str:
        items = self._request(YOUTUBE_API_BASE, "/channels", query={"part": "id", "mine": "true"}).get("items", [])
        if not items:
            raise RuntimeError("No authenticated YouTube channel was returned.")
        return items[0]["id"]


class McpServer:
    def __init__(self) -> None:
        client_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "secrets/client_secret.json")
        token_file = os.environ.get("YOUTUBE_TOKEN_FILE", "secrets/token.json")
        self.auth = AuthConfig(
            client_secrets_path=abs_path(client_secrets),
            token_path=abs_path(token_file),
        )
        quota_file = os.environ.get("YOUTUBE_QUOTA_FILE") or str(
            abs_path(token_file).parent / "quota_usage.json"
        )
        self.youtube = YouTubeClient(
            self.auth,
            QuotaTracker(
                abs_path(quota_file),
                int(os.environ.get("YOUTUBE_DAILY_QUOTA", DEFAULT_DAILY_QUOTA)),
            ),
        )
        self.tools = [
            {
                "name": "youtube_auth_status",
                "description": "Show whether YouTube OAuth credentials and tokens are configured.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "youtube_start_auth",
                "description": "Generate the OAuth authorization URL and local auth command.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "youtube_channel_overview",
                "description": "Fetch the authenticated YouTube channel profile and public statistics.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "youtube_list_videos",
                "description": "List your most recent channel videos with details and statistics.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                        "page_token": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_get_video",
                "description": "Fetch detailed metadata for a single YouTube video.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"video_id": {"type": "string"}},
                    "required": ["video_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_update_video",
                "description": "Update title, description, tags, category, language, or privacy status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "category_id": {"type": "string"},
                        "default_language": {"type": "string"},
                        "privacy_status": {"type": "string"},
                        **VIDEO_FLAG_SCHEMA,
                        "publish_at": {
                            "type": "string",
                            "description": (
                                "Schedule publication: ISO 8601 with UTC offset, e.g. "
                                "2026-11-05T17:00:00+01:00. The video stays private until then. "
                                "Empty string removes an existing schedule."
                            ),
                        },
                    },
                    "required": ["video_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_upload_video",
                "description": (
                    "Upload a local video file to the channel (resumable, chunked). Defaults to "
                    "privacy_status 'private'. Costs 1,600 quota units; videos uploaded through "
                    "an unverified API project are locked to private by YouTube."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute path to the video file."},
                        "title": {"type": "string", "maxLength": 100},
                        "description": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "category_id": {"type": "string", "description": "Default 22 (People & Blogs)."},
                        "default_language": {"type": "string", "description": "For example 'de'."},
                        "privacy_status": {"type": "string", "enum": list(PRIVACY_STATUSES)},
                        "made_for_kids": {"type": "boolean", "description": "Default false."},
                        **{k: v for k, v in VIDEO_FLAG_SCHEMA.items() if k != "made_for_kids"},
                        "publish_at": {
                            "type": "string",
                            "description": (
                                "Schedule publication: ISO 8601 with UTC offset, e.g. "
                                "2026-11-05T17:00:00+01:00. Requires privacy_status 'private' (default)."
                            ),
                        },
                    },
                    "required": ["file_path", "title"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_upload_thumbnail",
                "description": "Upload a new custom thumbnail for a video from a local image path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "image_path": {"type": "string"},
                    },
                    "required": ["video_id", "image_path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_analytics_query",
                "description": (
                    "Free YouTube Analytics query for your channel (own quota, not the Data API quota). "
                    "Rows come back as lists in the order of 'columns' (dimensions first, then metrics). "
                    "Examples: top videos = dimensions ['video'], metrics ['views','estimatedMinutesWatched'], "
                    "sort '-views', max_results 10; traffic sources = dimensions ['insightTrafficSourceType'], "
                    "metrics ['views']; countries = dimensions ['country']; daily trend = dimensions ['day']; "
                    "retention of one video = dimensions ['elapsedVideoTimeRatio'], metrics "
                    "['audienceWatchRatio'], filters 'video==VIDEO_ID'. Unsupported metric/dimension "
                    "combinations are rejected by Google with an explanation."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "metrics": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": "For example views, estimatedMinutesWatched, averageViewDuration, likes, subscribersGained.",
                        },
                        "dimensions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "For example day, month, video, country, ageGroup, gender, deviceType, insightTrafficSourceType.",
                        },
                        "filters": {
                            "type": "string",
                            "description": "name==value pairs joined by ';', for example 'video==ID;country==DE'.",
                        },
                        "sort": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Column names, '-' prefix for descending, for example ['-views'].",
                        },
                        "max_results": {"type": "integer", "minimum": 1},
                        "start_index": {"type": "integer", "minimum": 1},
                    },
                    "required": ["start_date", "end_date", "metrics"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_quota_status",
                "description": (
                    "Show the estimated YouTube Data API quota this server has used today "
                    "(Pacific day), what is left, when it resets, and which calls cost the most. Free."
                ),
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "youtube_list_playlists",
                "description": "List your channel's playlists (title, privacy, item count). Costs 1 quota unit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                        "page_token": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_list_playlist_items",
                "description": "List the videos in a playlist in order, with item ids and positions. Costs 1 quota unit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "playlist_id": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                        "page_token": {"type": "string"},
                    },
                    "required": ["playlist_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_create_playlist",
                "description": "Create a playlist (default privacy 'private'). Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 150},
                        "description": {"type": "string"},
                        "privacy_status": {"type": "string", "enum": list(PRIVACY_STATUSES)},
                        "default_language": {"type": "string", "description": "For example 'de'."},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_update_playlist",
                "description": "Change a playlist's title, description, language or privacy. Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "playlist_id": {"type": "string"},
                        "title": {"type": "string", "maxLength": 150},
                        "description": {"type": "string"},
                        "privacy_status": {"type": "string", "enum": list(PRIVACY_STATUSES)},
                        "default_language": {"type": "string"},
                    },
                    "required": ["playlist_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_delete_playlist",
                "description": "Permanently delete a playlist (its videos stay on the channel). Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"playlist_id": {"type": "string"}},
                    "required": ["playlist_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_add_playlist_item",
                "description": "Add a video to a playlist (optionally at a position, 0 = first). Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "playlist_id": {"type": "string"},
                        "video_id": {"type": "string"},
                        "position": {"type": "integer", "minimum": 0},
                    },
                    "required": ["playlist_id", "video_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_remove_playlist_item",
                "description": (
                    "Remove an entry from a playlist (the video itself is kept). item_id is the "
                    "playlist item id from youtube_list_playlist_items, not the video id. Costs 50 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"item_id": {"type": "string"}},
                    "required": ["item_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_move_playlist_item",
                "description": (
                    "Move a playlist entry to a new position (0 = first). Only works for playlists "
                    "with manual ordering. Costs 50 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "position": {"type": "integer", "minimum": 0},
                    },
                    "required": ["item_id", "position"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_reply_to_comment",
                "description": (
                    "Reply to a comment. comment_id is the top-level comment id (topLevelComment.id from "
                    "youtube_list_comments); YouTube has no replies to replies. Costs 50 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string"},
                        "text": {"type": "string", "maxLength": 10000},
                    },
                    "required": ["comment_id", "text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_list_replies",
                "description": "List the replies to a top-level comment. Costs 1 quota unit.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                        "page_token": {"type": "string"},
                    },
                    "required": ["comment_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_delete_comment",
                "description": (
                    "Permanently delete a comment or reply (your own, or any comment on your videos). "
                    "Costs 50 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {"comment_id": {"type": "string"}},
                    "required": ["comment_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_moderate_comments",
                "description": (
                    "Set the moderation status of up to 50 comments on your videos in one call: "
                    "'published' (approve), 'heldForReview' (hide until approved) or 'rejected' "
                    "(remove from view; spam handling). ban_author also bans the authors from your "
                    "channel (only with 'rejected'). Costs 50 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "comment_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
                        "status": {"type": "string", "enum": list(MODERATION_STATUSES)},
                        "ban_author": {"type": "boolean", "description": "Default false."},
                    },
                    "required": ["comment_ids", "status"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_list_captions",
                "description": "List the caption tracks of a video (id, language, name, draft status). Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"video_id": {"type": "string"}},
                    "required": ["video_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_upload_caption",
                "description": (
                    "Add a caption track to a video from a local subtitle file (.srt, .vtt, ...). "
                    "Costs 400 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "file_path": {"type": "string", "description": "Absolute path to the subtitle file."},
                        "language": {"type": "string", "description": "BCP-47 code, for example 'de'."},
                        "name": {"type": "string", "description": "Track name shown to viewers (optional)."},
                        "is_draft": {"type": "boolean", "description": "Keep unpublished. Default false."},
                    },
                    "required": ["video_id", "file_path", "language"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_update_caption",
                "description": (
                    "Replace the content of a caption track and/or change its draft status. "
                    "Costs 450 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "caption_id": {"type": "string"},
                        "file_path": {"type": "string", "description": "New subtitle file (replaces the track)."},
                        "is_draft": {"type": "boolean"},
                    },
                    "required": ["caption_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_delete_caption",
                "description": "Permanently delete a caption track. Costs 50 quota units.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"caption_id": {"type": "string"}},
                    "required": ["caption_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_download_caption",
                "description": (
                    "Download a caption track you own as srt, vtt, sbv, scc or ttml. Returns the text, "
                    "or saves it when output_path is given (refuses to overwrite). Costs 200 quota units."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "caption_id": {"type": "string"},
                        "format": {"type": "string", "enum": list(CAPTION_FORMATS)},
                        "output_path": {"type": "string"},
                    },
                    "required": ["caption_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_channel_analytics",
                "description": "Return channel-level analytics for a date range.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["start_date", "end_date"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_video_analytics",
                "description": "Return per-day analytics for one video across a date range.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["video_id", "start_date", "end_date"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_post_comment",
                "description": "Post a top-level comment on one of your YouTube videos.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["video_id", "text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_list_comments",
                "description": (
                    "List comment threads on one video, with paging, sorting and search. Each thread "
                    "has the thread id and topLevelComment.id (use that one for replies and moderation). "
                    "Costs 1 quota unit per page."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "video_id": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                        "page_token": {"type": "string", "description": "nextPageToken of the previous page."},
                        "order": {"type": "string", "enum": list(COMMENT_ORDERS), "description": "Default relevance."},
                        "search_terms": {"type": "string"},
                        "moderation_status": {"type": "string", "enum": list(THREAD_MODERATION_FILTERS)},
                        "include_replies": {"type": "boolean", "description": "Embed up to 5 replies per thread."},
                    },
                    "required": ["video_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "youtube_list_channel_comments",
                "description": (
                    "List comment threads across all videos of your channel (newest first by default), "
                    "with paging, search and a moderation filter such as 'heldForReview' or 'likelySpam'. "
                    "Costs 2 quota units (channel lookup + page)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                        "page_token": {"type": "string"},
                        "order": {"type": "string", "enum": list(COMMENT_ORDERS), "description": "Default time."},
                        "search_terms": {"type": "string"},
                        "moderation_status": {"type": "string", "enum": list(THREAD_MODERATION_FILTERS)},
                        "include_replies": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
        ]
        for tool in self.tools:
            tool["annotations"] = tool_annotations(tool["name"])
            if not tool["annotations"]["readOnlyHint"]:
                tool["inputSchema"]["properties"]["dry_run"] = {
                    "type": "boolean",
                    "description": (
                        "Preview only: validates the input, runs read-only lookups and returns the planned "
                        "request(s), before/after changes and the quota cost without changing anything."
                    ),
                }

    def _start_auth_payload(self) -> dict[str, Any]:
        status = self.auth.auth_status()
        if not status["client_secrets_exists"]:
            raise RuntimeError(
                "client_secret.json is missing. Add your Google OAuth desktop client JSON first."
            )
        client = self.auth.load_client_config()
        state = secrets.token_urlsafe(24)
        params = urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
        helper = PLUGIN_ROOT / "scripts" / "auth.py"
        return {
            "authorization_url": f"{AUTH_URL}?{params}",
            "token_path": status["token_path"],
            "client_secrets_path": status["client_secrets_path"],
            "helper_command": f"python3 {helper} auth",
            "redirect_uri": REDIRECT_URI,
        }

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        requested = arguments.pop("dry_run", False)
        # Anything that is not clearly "off" counts as a dry run: the safe side.
        dry = requested not in (False, None, 0, "", "false", "False", "FALSE", "0")
        hints = _TOOL_HINTS.get(name)
        if not dry or hints is None or hints[1]:  # unknown tools fail in _dispatch; reads ignore the flag
            return self._dispatch(name, arguments)
        with self.youtube.dry_run() as plan:
            self._dispatch(name, arguments)
        result: dict[str, Any] = {
            "dry_run": True,
            "executed": False,
            "tool": name,
            "message": "Nothing was changed. Call again without dry_run to apply.",
            "planned_requests": plan["requests"],
            "quota_cost_estimate": sum(r.get("quota_cost", 0) for r in plan["requests"]),
        }
        if plan["changes"]:
            result["changes"] = plan["changes"]
        if plan["targets"]:
            result["targets"] = plan["targets"]
        return result

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "youtube_auth_status":
            return self.auth.auth_status()
        if name == "youtube_start_auth":
            return self._start_auth_payload()
        if name == "youtube_channel_overview":
            return self.youtube.channel_overview()
        if name == "youtube_list_videos":
            return self.youtube.list_videos(
                int(arguments.get("max_results", 10)), arguments.get("page_token")
            )
        if name == "youtube_get_video":
            return self.youtube.get_video(arguments["video_id"])
        if name == "youtube_update_video":
            return self.youtube.update_video(
                arguments["video_id"],
                title=arguments.get("title"),
                description=arguments.get("description"),
                tags=arguments.get("tags"),
                category_id=arguments.get("category_id"),
                default_language=arguments.get("default_language"),
                privacy_status=arguments.get("privacy_status"),
                publish_at=arguments.get("publish_at"),
                made_for_kids=arguments.get("made_for_kids"),
                embeddable=arguments.get("embeddable"),
                license=arguments.get("license"),
                public_stats_viewable=arguments.get("public_stats_viewable"),
                contains_synthetic_media=arguments.get("contains_synthetic_media"),
            )
        if name == "youtube_upload_video":
            return self.youtube.upload_video(
                arguments["file_path"],
                arguments["title"],
                description=arguments.get("description", ""),
                tags=arguments.get("tags"),
                category_id=arguments.get("category_id", "22"),
                default_language=arguments.get("default_language"),
                privacy_status=arguments.get("privacy_status", "private"),
                made_for_kids=arguments.get("made_for_kids", False),
                publish_at=arguments.get("publish_at"),
                embeddable=arguments.get("embeddable"),
                license=arguments.get("license"),
                public_stats_viewable=arguments.get("public_stats_viewable"),
                contains_synthetic_media=arguments.get("contains_synthetic_media"),
            )
        if name == "youtube_upload_thumbnail":
            return self.youtube.upload_thumbnail(arguments["video_id"], arguments["image_path"])
        if name == "youtube_analytics_query":
            return self.youtube.analytics_query(
                arguments["start_date"],
                arguments["end_date"],
                arguments["metrics"],
                arguments.get("dimensions"),
                arguments.get("filters"),
                arguments.get("sort"),
                arguments.get("max_results"),
                arguments.get("start_index"),
            )
        if name == "youtube_quota_status":
            return self.youtube.quota.status()
        if name == "youtube_list_playlists":
            return self.youtube.list_playlists(
                int(arguments.get("max_results", 25)), arguments.get("page_token")
            )
        if name == "youtube_list_playlist_items":
            return self.youtube.list_playlist_items(
                arguments["playlist_id"],
                int(arguments.get("max_results", 50)),
                arguments.get("page_token"),
            )
        if name == "youtube_create_playlist":
            return self.youtube.create_playlist(
                arguments["title"],
                description=arguments.get("description", ""),
                privacy_status=arguments.get("privacy_status", "private"),
                default_language=arguments.get("default_language"),
            )
        if name == "youtube_update_playlist":
            return self.youtube.update_playlist(
                arguments["playlist_id"],
                title=arguments.get("title"),
                description=arguments.get("description"),
                privacy_status=arguments.get("privacy_status"),
                default_language=arguments.get("default_language"),
            )
        if name == "youtube_delete_playlist":
            return self.youtube.delete_playlist(arguments["playlist_id"])
        if name == "youtube_add_playlist_item":
            position = arguments.get("position")
            return self.youtube.add_playlist_item(
                arguments["playlist_id"],
                arguments["video_id"],
                int(position) if position is not None else None,
            )
        if name == "youtube_remove_playlist_item":
            return self.youtube.remove_playlist_item(arguments["item_id"])
        if name == "youtube_move_playlist_item":
            return self.youtube.move_playlist_item(arguments["item_id"], int(arguments["position"]))
        if name == "youtube_reply_to_comment":
            return self.youtube.reply_to_comment(arguments["comment_id"], arguments["text"])
        if name == "youtube_list_replies":
            return self.youtube.list_replies(
                arguments["comment_id"],
                int(arguments.get("max_results", 20)),
                arguments.get("page_token"),
            )
        if name == "youtube_delete_comment":
            return self.youtube.delete_comment(arguments["comment_id"])
        if name == "youtube_moderate_comments":
            return self.youtube.moderate_comments(
                arguments["comment_ids"],
                arguments["status"],
                bool(arguments.get("ban_author", False)),
            )
        if name == "youtube_list_captions":
            return self.youtube.list_captions(arguments["video_id"])
        if name == "youtube_upload_caption":
            return self.youtube.upload_caption(
                arguments["video_id"],
                arguments["file_path"],
                arguments["language"],
                name=arguments.get("name", ""),
                is_draft=bool(arguments.get("is_draft", False)),
            )
        if name == "youtube_update_caption":
            return self.youtube.update_caption(
                arguments["caption_id"],
                file_path=arguments.get("file_path"),
                is_draft=arguments.get("is_draft"),
            )
        if name == "youtube_delete_caption":
            return self.youtube.delete_caption(arguments["caption_id"])
        if name == "youtube_download_caption":
            return self.youtube.download_caption(
                arguments["caption_id"],
                arguments.get("format", "srt"),
                arguments.get("output_path"),
            )
        if name == "youtube_channel_analytics":
            return self.youtube.channel_analytics(arguments["start_date"], arguments["end_date"])
        if name == "youtube_video_analytics":
            return self.youtube.video_analytics(
                arguments["video_id"], arguments["start_date"], arguments["end_date"]
            )
        if name == "youtube_post_comment":
            return self.youtube.post_comment(arguments["video_id"], arguments["text"])
        if name == "youtube_list_comments":
            return self.youtube.list_comments(
                arguments["video_id"],
                int(arguments.get("max_results", 20)),
                arguments.get("page_token"),
                arguments.get("order", "relevance"),
                arguments.get("search_terms"),
                arguments.get("moderation_status"),
                bool(arguments.get("include_replies", False)),
            )
        if name == "youtube_list_channel_comments":
            return self.youtube.list_channel_comments(
                int(arguments.get("max_results", 20)),
                arguments.get("page_token"),
                arguments.get("order", "time"),
                arguments.get("search_terms"),
                arguments.get("moderation_status"),
                bool(arguments.get("include_replies", False)),
            )
        raise UnknownTool(f"Unknown tool: {name}")

    # "ndjson" (MCP stdio standard: one JSON object per line) or "headers" (Content-Length).
    _framing = "headers"

    @classmethod
    def _read_message(cls) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            if line.lstrip().startswith(b"{"):
                cls._framing = "ndjson"
                return json.loads(line.decode("utf-8"))
            if line.strip() == b"" and not headers:
                continue
            if line == b"\r\n":
                break
            key, _, value = line.decode("utf-8").partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))

    @classmethod
    def _write_message(cls, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        if cls._framing == "ndjson":
            sys.stdout.buffer.write(encoded + b"\n")
        else:
            sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("utf-8"))
            sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()

    def _success(self, message_id: Any, result: dict[str, Any]) -> None:
        self._write_message({"jsonrpc": "2.0", "id": message_id, "result": result})

    def _error(self, message_id: Any, code: int, message: str) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": code, "message": message},
            }
        )

    def serve(self) -> None:
        while True:
            message = self._read_message()
            if message is None:
                return
            message_id = message.get("id")
            method = message.get("method")
            try:
                if method == "initialize":
                    self._success(
                        message_id,
                        {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "youtube-studio-mcp", "version": "0.1.0"},
                        },
                    )
                elif method == "notifications/initialized":
                    continue
                elif method == "ping":
                    self._success(message_id, {})
                elif method == "tools/list":
                    self._success(message_id, {"tools": self.tools})
                elif method == "tools/call":
                    params = message.get("params", {})
                    try:
                        result = self._call_tool(params["name"], params.get("arguments", {}))
                    except UnknownTool as exc:
                        self._error(message_id, -32602, str(exc))
                        continue
                    except Exception as exc:  # noqa: BLE001 - tool failures are results, not protocol errors
                        self._success(
                            message_id,
                            {"content": [text_content(explain_error(exc))], "isError": True},
                        )
                        continue
                    self._success(message_id, {"content": [text_content(json.dumps(result, indent=2))]})
                else:
                    self._error(message_id, -32601, f"Method not found: {method}")
            except Exception as exc:  # noqa: BLE001
                self._error(message_id, -32000, str(exc))


def main() -> None:
    McpServer().serve()


if __name__ == "__main__":
    main()
