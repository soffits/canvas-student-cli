#!/usr/bin/env python3
"""Student-only Canvas LMS read CLI."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import json
import os
import re
import stat
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

__version__ = "0.1.0"

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 100
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_ITEMS = 500
MAX_SEARCH_RESULTS = 50
MAX_DOWNLOAD_REDIRECTS = 5
DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DECIMAL_RE = re.compile(r"[0-9]+")

EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_SECURITY = 4
EXIT_CANVAS_HTTP = 5
EXIT_NETWORK = 6
EXIT_INTERNAL = 1


class CliError(Exception):
    kind = "internal"
    exit_code = EXIT_INTERNAL

    def __init__(self, message: str, **meta: Any) -> None:
        super().__init__(message)
        self.message = message
        self.meta = meta


class UsageError(CliError):
    kind = "usage"
    exit_code = EXIT_USAGE


class ConfigError(CliError):
    kind = "config"
    exit_code = EXIT_CONFIG


class SecurityError(CliError):
    kind = "security"
    exit_code = EXIT_SECURITY


class CanvasHTTPError(CliError):
    kind = "canvas_http"
    exit_code = EXIT_CANVAS_HTTP


class NetworkError(CliError):
    kind = "network"
    exit_code = EXIT_NETWORK


class CanvasError(CliError):
    kind = "canvas"
    exit_code = EXIT_CANVAS_HTTP


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    http_error_301 = HTTPRedirectHandler.http_error_302
    http_error_302 = HTTPRedirectHandler.http_error_302
    http_error_303 = HTTPRedirectHandler.http_error_302
    http_error_307 = HTTPRedirectHandler.http_error_302
    http_error_308 = HTTPRedirectHandler.http_error_302


class PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def html_to_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parser = PlainTextHTMLParser()
    try:
        parser.feed(value)
        return html.unescape(parser.text())
    except Exception:
        return re.sub(r"<[^>]+>", " ", value).strip()


def compact_json(value: Any, *, file: Any = None) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=file or sys.stdout)


def normalize_base_url(raw: str) -> str:
    if not raw:
        raise ConfigError("missing Canvas base URL; set --base-url or CANVAS_BASE_URL")
    parsed = urlparse(raw.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ConfigError("Canvas base URL must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("Canvas base URL must not contain credentials, query, or fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1"}:
        raise SecurityError("Canvas base URL must use HTTPS except for localhost tests")
    path = parsed.path.rstrip("/")
    if path not in {"", "/api/v1"}:
        raise ConfigError("Canvas base URL path must be empty or /api/v1")
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, "/api/v1", "", "", ""))


def same_origin(left: str, right: str) -> bool:
    a = urlparse(left)
    b = urlparse(right)
    return (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port)


def load_token(token_file: str | None) -> str:
    token_path = token_file or os.environ.get("CANVAS_API_TOKEN_FILE")
    if token_path:
        path = Path(token_path).expanduser()
        try:
            mode = path.stat().st_mode
            if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise SecurityError("token file permissions are too broad; use owner-only permissions")
            token = path.read_text(encoding="utf-8").strip()
        except CliError:
            raise
        except OSError as exc:
            raise ConfigError(f"cannot read token file: {exc.strerror or 'file error'}") from exc
    else:
        token = os.environ.get("CANVAS_API_TOKEN", "").strip()
    if not token:
        raise ConfigError("missing Canvas token; use --token-file, CANVAS_API_TOKEN_FILE, or CANVAS_API_TOKEN")
    return token


def encode_id(value: Any) -> str:
    return quote(str(value), safe="")


def clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, list):
            out[key] = ["true" if item is True else "false" if item is False else item for item in value]
        else:
            out[key] = value
    return out


def parse_link_header(value: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for part in value.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;(.*)$', part)
        if not match:
            continue
        url, attrs = match.groups()
        rel = re.search(r'rel="([^"]+)"', attrs)
        if rel:
            links[rel.group(1)] = html.unescape(url)
    return links


class CanvasClient:
    def __init__(self, base_url: str, token: str, opener: Any | None = None, timeout: int = 30) -> None:
        self.base_url = normalize_base_url(base_url)
        self.token = token
        self.opener = opener or build_opener(NoRedirectHandler())
        self.timeout = timeout

    def api_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            raise CanvasError("internal endpoint path must start with slash")
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if not same_origin(self.base_url, url):
            raise SecurityError("internal endpoint escaped Canvas origin")
        query = urlencode(clean_params(params), doseq=True)
        return f"{url}?{query}" if query else url

    def request_json(self, path_or_url: str, params: dict[str, Any] | None = None, *, method: str = "GET") -> Any:
        if method != "GET":
            raise SecurityError("only GET requests are supported")
        url = path_or_url if path_or_url.startswith(("https://", "http://")) else self.api_url(path_or_url, params)
        if not same_origin(self.base_url, url):
            raise SecurityError("refusing to fetch outside the Canvas origin")
        response = self.open_get(url, auth=True)
        try:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise CanvasError("Canvas API returned invalid JSON") from exc

    def open_get(self, url: str, *, auth: bool) -> Any:
        redirects = 0
        while True:
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {self.token}"
            request = Request(url, headers=headers, method="GET")
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    response = exc
                else:
                    raise CanvasHTTPError(f"Canvas API error: HTTP {exc.code}", status=exc.code) from exc
            except URLError as exc:
                reason = getattr(exc, "reason", None)
                text = reason.__class__.__name__ if reason else "connection error"
                raise NetworkError(f"Canvas API request failed: {text}") from exc
            status = getattr(response, "status", getattr(response, "code", 200))
            if status in {301, 302, 303, 307, 308}:
                redirects += 1
                if redirects > MAX_DOWNLOAD_REDIRECTS:
                    raise SecurityError("Canvas API redirect limit exceeded")
                location = response.headers.get("Location") if hasattr(response, "headers") else None
                if not location:
                    raise SecurityError("Canvas API redirect missing location")
                next_url = urljoin(url, location)
                if auth and not same_origin(self.base_url, next_url):
                    raise SecurityError("refusing cross-origin authenticated redirect")
                url = next_url
                continue
            if status >= 400:
                raise CanvasHTTPError(f"Canvas API error: HTTP {status}", status=status)
            return response

    def paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> list[Any]:
        if max_pages < 1 or max_items < 1:
            raise UsageError("pagination bounds must be positive")
        url = self.api_url(path, params)
        out: list[Any] = []
        pages = 0
        while url:
            pages += 1
            if pages > max_pages:
                raise CanvasError("pagination limit reached before completion")
            if not same_origin(self.base_url, url):
                raise SecurityError("refusing cross-origin pagination link")
            response = self.open_get(url, auth=True)
            try:
                data = json.loads(response.read().decode("utf-8") or "[]")
            except ValueError as exc:
                raise CanvasError("Canvas API returned invalid JSON") from exc
            if not isinstance(data, list):
                raise CanvasError("Canvas API pagination response was not a list")
            out.extend(data)
            if len(out) > max_items:
                raise CanvasError("item limit reached before pagination completed")
            link = response.headers.get("Link", "") if hasattr(response, "headers") else ""
            url = parse_link_header(link).get("next")
        return out

    def download_url(self, url: str, dest: Path, max_bytes: int) -> tuple[int, str]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SecurityError("file URL must be absolute HTTP(S)")
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1"}:
            raise SecurityError("remote file downloads must use HTTPS")
        tmp_path: Path | None = None
        digest = hashlib.sha256()
        total = 0
        current = url
        redirects = 0
        try:
            while True:
                request = Request(current, headers={"Accept": "*/*"}, method="GET")
                try:
                    response = self.opener.open(request, timeout=self.timeout)
                except HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        response = exc
                    else:
                        raise CanvasHTTPError(f"file download error: HTTP {exc.code}", status=exc.code) from exc
                except URLError as exc:
                    raise NetworkError("file download request failed") from exc
                status = getattr(response, "status", getattr(response, "code", 200))
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > MAX_DOWNLOAD_REDIRECTS:
                        raise SecurityError("file download redirect limit exceeded")
                    location = response.headers.get("Location") if hasattr(response, "headers") else None
                    if not location:
                        raise SecurityError("file download redirect missing location")
                    current = urljoin(current, location)
                    parsed_next = urlparse(current)
                    if parsed_next.scheme not in {"http", "https"} or not parsed_next.netloc:
                        raise SecurityError("file download redirect must stay on HTTP(S)")
                    if parsed_next.scheme == "http" and (parsed_next.hostname or "").lower() not in {"localhost", "127.0.0.1"}:
                        raise SecurityError("remote file downloads must use HTTPS")
                    continue
                if status >= 400:
                    raise CanvasHTTPError(f"file download error: HTTP {status}", status=status)
                expected = None
                length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if length is not None:
                    expected = parse_content_length(length)
                    if expected > max_bytes:
                        raise SecurityError("file exceeds max-bytes", max_bytes=max_bytes, size=expected)
                fd, name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
                tmp_path = Path(name)
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
                else:
                    os.chmod(name, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        chunk = response.read(1024 * 64)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise SecurityError("file exceeds max-bytes", max_bytes=max_bytes)
                        digest.update(chunk)
                        fh.write(chunk)
                    if expected is not None and total != expected:
                        raise SecurityError("file Content-Length mismatch", expected=expected, actual=total)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, dest)
                tmp_path = None
                dir_fd = os.open(dest.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                return total, digest.hexdigest()
        finally:
            if tmp_path is not None:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()


def add_plain(record: Any, *fields: str) -> Any:
    if isinstance(record, dict):
        for field in fields:
            value = record.get(field)
            if isinstance(value, str) and value:
                record[f"{field}_text"] = html_to_text(value)
    return record


def add_plain_messages(value: Any) -> Any:
    if isinstance(value, dict):
        add_plain(value, "message")
        for item in value.values():
            add_plain_messages(item)
    elif isinstance(value, list):
        for item in value:
            add_plain_messages(item)
    return value


def page_args(args: argparse.Namespace) -> dict[str, int]:
    return {"per_page": min(max(args.per_page, 1), MAX_PER_PAGE)}


def scalar_values(item: dict[str, Any]) -> list[str]:
    return [str(value) for value in item.values() if value is None or isinstance(value, str | int | float | bool)]


def parse_content_length(value: str) -> int:
    text = value.strip()
    if not DECIMAL_RE.fullmatch(text):
        raise SecurityError("invalid file Content-Length")
    return int(text)


def has_symlink_component(path: Path) -> bool:
    current = Path.cwd() if not path.is_absolute() else Path(path.anchor)
    parts = path.parts if not path.is_absolute() else path.parts[1:]
    for part in parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def sort_key(item: Any, field: str) -> tuple[int, Any, str]:
    if not isinstance(item, dict) or field not in item or item.get(field) is None:
        return (3, "", "")
    value = item[field]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return (0, Decimal(str(value)), str(value))
    if isinstance(value, str):
        text = value.strip()
        try:
            if text and DECIMAL_RE.fullmatch(text):
                return (0, Decimal(text), value)
        except InvalidOperation:
            pass
        return (1, value.casefold(), value)
    return (2, type(value).__name__, str(value))


def apply_projection_filtering(value: Any, args: argparse.Namespace) -> Any:
    fields_raw = getattr(args, "fields", "")
    match_raw = getattr(args, "match", "")
    sort_field = getattr(args, "sort", "")
    descending = getattr(args, "descending", False)
    if not any([fields_raw, match_raw, sort_field, descending]):
        return value
    if not isinstance(value, list):
        raise UsageError("projection and filtering are only supported for list commands")
    items = value
    if not items:
        if descending and not sort_field:
            raise UsageError("--descending requires --sort")
        return []
    dict_items = [item for item in items if isinstance(item, dict)]
    available = set().union(*(item.keys() for item in dict_items)) if dict_items else set()
    if fields_raw:
        fields = [field.strip() for field in fields_raw.split(",") if field.strip()]
        if not fields:
            raise UsageError("--fields must name at least one field")
        missing = sorted(set(fields) - available)
        if missing:
            raise UsageError("unknown field in --fields", fields=missing)
    else:
        fields = []
    if sort_field:
        if sort_field not in available:
            raise UsageError("unknown field in --sort", field=sort_field)
        items = sorted(items, key=lambda item: sort_key(item, sort_field), reverse=descending)
    elif descending:
        raise UsageError("--descending requires --sort")
    if match_raw:
        needle = match_raw.casefold()
        items = [item for item in items if isinstance(item, dict) and needle in " ".join(scalar_values(item)).casefold()]
    if fields:
        items = [{field: item.get(field) for field in fields} for item in items if isinstance(item, dict)]
    return items


def filter_conversations(items: list[Any], args: argparse.Namespace) -> list[Any]:
    context = getattr(args, "context_code", "")
    query = getattr(args, "query", "")
    if context:
        items = [item for item in items if isinstance(item, dict) and item.get("context_code") == context]
    if query:
        needle = query.casefold()
        items = [item for item in items if isinstance(item, dict) and needle in " ".join(scalar_values(item)).casefold()]
    return items


def list_readable_courses(client: CanvasClient, args: argparse.Namespace) -> list[Any]:
    params: dict[str, Any] = page_args(args)
    params["enrollment_state"] = getattr(args, "enrollment_state", None)
    params["include[]"] = ["term", "favorites", "syllabus_body"] if getattr(args, "include_syllabus", False) else ["term", "favorites"]
    return client.paginated("/courses", params, max_pages=args.max_pages, max_items=args.max_items)


def command_auth_check(client: CanvasClient, args: argparse.Namespace) -> Any:
    return {"ok": True, "profile": client.request_json("/users/self/profile")}


def command_profile(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json("/users/self/profile")


def command_dashboard_cards(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated("/dashboard/dashboard_cards", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_todo(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated("/users/self/todo", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_upcoming_events(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["type"] = "event"
    params["all_events"] = args.all_events
    return client.paginated("/users/self/upcoming_events", params, max_pages=args.max_pages, max_items=args.max_items)


def command_missing_submissions(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["filter[]"] = "submittable"
    return [add_plain(item, "description") for item in client.paginated("/users/self/missing_submissions", params, max_pages=args.max_pages, max_items=args.max_items)]


def command_list_courses(client: CanvasClient, args: argparse.Namespace) -> Any:
    return [add_plain(item, "syllabus_body") for item in list_readable_courses(client, args)]


def command_get_course(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/courses/{encode_id(args.course_id)}", {"include[]": ["term", "teachers", "favorites", "syllabus_body"]}), "syllabus_body")


def command_get_syllabus(client: CanvasClient, args: argparse.Namespace) -> Any:
    data = client.request_json(f"/courses/{encode_id(args.course_id)}", {"include[]": "syllabus_body"})
    return {"course_id": args.course_id, "syllabus_body": data.get("syllabus_body", ""), "syllabus_body_text": html_to_text(data.get("syllabus_body", ""))}


def command_list_assignments(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params.update({"order_by": args.order_by, "bucket": args.bucket, "include[]": ["submission", "score_statistics", "rubric"] if args.include_details else ["submission"]})
    return [add_plain(item, "description") for item in client.paginated(f"/courses/{encode_id(args.course_id)}/assignments", params, max_pages=args.max_pages, max_items=args.max_items)]


def command_get_assignment(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/courses/{encode_id(args.course_id)}/assignments/{encode_id(args.assignment_id)}", {"include[]": ["submission", "rubric", "overrides"]}), "description")


def command_list_assignment_groups(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["include[]"] = "assignments" if args.include_assignments else None
    return client.paginated(f"/courses/{encode_id(args.course_id)}/assignment_groups", params, max_pages=args.max_pages, max_items=args.max_items)


def command_get_submission(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json(f"/courses/{encode_id(args.course_id)}/assignments/{encode_id(args.assignment_id)}/submissions/self", {"include[]": ["submission_comments", "rubric_assessment", "assignment", "visibility"]})


def command_list_my_submissions(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["student_ids[]"] = "self"
    params["include[]"] = ["submission_comments", "assignment", "rubric_assessment"]
    return client.paginated(f"/courses/{encode_id(args.course_id)}/students/submissions", params, max_pages=args.max_pages, max_items=args.max_items)


def command_get_submission_feedback(client: CanvasClient, args: argparse.Namespace) -> Any:
    data = command_get_submission(client, args)
    return {"course_id": args.course_id, "assignment_id": args.assignment_id, "comments": data.get("submission_comments", []), "rubric_assessment": data.get("rubric_assessment"), "grade": data.get("grade"), "score": data.get("score")}


def command_get_grades(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["user_id"] = "self"
    params["enrollment_type"] = "student"
    params["include[]"] = ["total_scores", "current_grading_period_scores"]
    return client.paginated(f"/courses/{encode_id(args.course_id)}/enrollments", params, max_pages=args.max_pages, max_items=args.max_items)


def command_list_quizzes(client: CanvasClient, args: argparse.Namespace) -> Any:
    return [add_plain(item, "description") for item in client.paginated(f"/courses/{encode_id(args.course_id)}/quizzes", page_args(args), max_pages=args.max_pages, max_items=args.max_items)]


def command_get_quiz(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/courses/{encode_id(args.course_id)}/quizzes/{encode_id(args.quiz_id)}"), "description")


def command_list_files(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["content_types[]"] = args.content_type
    path = f"/courses/{encode_id(args.course_id)}/files" if args.course_id else "/users/self/files"
    return client.paginated(path, params, max_pages=args.max_pages, max_items=args.max_items)


def command_get_file(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json(f"/files/{encode_id(args.file_id)}")


def command_download_file(client: CanvasClient, args: argparse.Namespace) -> Any:
    if args.max_bytes < 1:
        raise UsageError("--max-bytes must be positive")
    dest = Path(args.local_file)
    if dest.is_symlink():
        raise SecurityError("destination must not be a symlink")
    parent = dest.parent if dest.parent != Path("") else Path(".")
    if parent.is_symlink() or has_symlink_component(parent) or not parent.is_dir():
        raise SecurityError("destination parent must be a real directory")
    if dest.exists():
        if not args.overwrite:
            raise SecurityError("destination exists; pass --overwrite")
        if not dest.is_file():
            raise SecurityError("destination overwrite target must be a regular file")
    metadata = client.request_json(f"/files/{encode_id(args.file_id)}")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("url"), str):
        raise CanvasError("Canvas file metadata did not include a download URL")
    size, sha256 = client.download_url(metadata["url"], dest, args.max_bytes)
    return {"file_id": args.file_id, "display_name": metadata.get("display_name") or metadata.get("filename") or metadata.get("name"), "local_name": dest.name, "size": size, "sha256": sha256, "overwrite": bool(args.overwrite)}


def command_list_groups(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    if args.course_id:
        return client.paginated(f"/courses/{encode_id(args.course_id)}/groups", params, max_pages=args.max_pages, max_items=args.max_items)
    return client.paginated("/users/self/groups", params, max_pages=args.max_pages, max_items=args.max_items)


def command_get_group(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json(f"/groups/{encode_id(args.group_id)}")


def command_list_group_members(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/groups/{encode_id(args.group_id)}/users", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_list_group_files(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/groups/{encode_id(args.group_id)}/files", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_list_group_folders(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/groups/{encode_id(args.group_id)}/folders", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_list_group_discussions(client: CanvasClient, args: argparse.Namespace) -> Any:
    return [add_plain(item, "message") for item in client.paginated(f"/groups/{encode_id(args.group_id)}/discussion_topics", page_args(args), max_pages=args.max_pages, max_items=args.max_items)]


def command_get_group_discussion(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/groups/{encode_id(args.group_id)}/discussion_topics/{encode_id(args.topic_id)}"), "message")


def command_get_group_discussion_view(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain_messages(client.request_json(f"/groups/{encode_id(args.group_id)}/discussion_topics/{encode_id(args.topic_id)}/view"))


def command_list_group_pages(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/groups/{encode_id(args.group_id)}/pages", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_get_group_page(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/groups/{encode_id(args.group_id)}/pages/{encode_id(args.page_url)}"), "body")


def command_list_discussions(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["include[]"] = ["sections", "sections_user_count"]
    return [add_plain(item, "message") for item in client.paginated(f"/courses/{encode_id(args.course_id)}/discussion_topics", params, max_pages=args.max_pages, max_items=args.max_items)]


def command_get_discussion(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/courses/{encode_id(args.course_id)}/discussion_topics/{encode_id(args.topic_id)}", {"include[]": ["sections", "sections_user_count"]}), "message")


def command_get_discussion_view(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain_messages(client.request_json(f"/courses/{encode_id(args.course_id)}/discussion_topics/{encode_id(args.topic_id)}/view"))


def command_list_announcements(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params.update({"context_codes[]": f"course_{args.course_id}", "active_only": args.active_only})
    return [add_plain(item, "message") for item in client.paginated("/announcements", params, max_pages=args.max_pages, max_items=args.max_items)]


def command_list_modules(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["include[]"] = "items" if args.include_items else None
    return client.paginated(f"/courses/{encode_id(args.course_id)}/modules", params, max_pages=args.max_pages, max_items=args.max_items)


def command_get_module(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json(f"/courses/{encode_id(args.course_id)}/modules/{encode_id(args.module_id)}")


def command_list_module_items(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params["include[]"] = ["content_details"]
    return client.paginated(f"/courses/{encode_id(args.course_id)}/modules/{encode_id(args.module_id)}/items", params, max_pages=args.max_pages, max_items=args.max_items)


def command_course_structure(client: CanvasClient, args: argparse.Namespace) -> Any:
    modules = client.paginated(f"/courses/{encode_id(args.course_id)}/modules", page_args(args), max_pages=args.max_pages, max_items=args.max_items)
    for module in modules:
        module["items"] = client.paginated(f"/courses/{encode_id(args.course_id)}/modules/{encode_id(module.get('id'))}/items", {"per_page": args.per_page, "include[]": ["content_details"]}, max_pages=args.max_pages, max_items=args.max_items)
    return {"course_id": args.course_id, "modules": modules}


def command_list_pages(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/courses/{encode_id(args.course_id)}/pages", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_get_page(client: CanvasClient, args: argparse.Namespace) -> Any:
    return add_plain(client.request_json(f"/courses/{encode_id(args.course_id)}/pages/{encode_id(args.page_url)}"), "body")


def command_calendar_events(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params.update({"context_codes[]": args.context_code, "start_date": args.start_date, "end_date": args.end_date, "type": args.event_type, "all_events": args.all_events})
    return [add_plain(item, "description") for item in client.paginated("/calendar_events", params, max_pages=args.max_pages, max_items=args.max_items)]


def command_list_conversations(client: CanvasClient, args: argparse.Namespace) -> Any:
    params = page_args(args)
    params.update({"scope": args.scope, "filter[]": args.filter})
    return filter_conversations(client.paginated("/conversations", params, max_pages=args.max_pages, max_items=args.max_items), args)


def command_get_conversation(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json(f"/conversations/{encode_id(args.conversation_id)}", {"include_all_conversation_ids": True})


def command_conversation_unread_count(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.request_json("/conversations/unread_count")


def command_list_peer_reviews(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/courses/{encode_id(args.course_id)}/assignments/{encode_id(args.assignment_id)}/peer_reviews", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_get_submission_peer_reviews(client: CanvasClient, args: argparse.Namespace) -> Any:
    return client.paginated(f"/courses/{encode_id(args.course_id)}/assignments/{encode_id(args.assignment_id)}/submissions/{encode_id(args.submission_id)}/peer_reviews", page_args(args), max_pages=args.max_pages, max_items=args.max_items)


def command_user_activity_stream(client: CanvasClient, args: argparse.Namespace) -> Any:
    return [add_plain(item, "message") for item in client.paginated("/users/self/activity_stream", page_args(args), max_pages=args.max_pages, max_items=args.max_items)]


def command_course_activity_stream(client: CanvasClient, args: argparse.Namespace) -> Any:
    return [add_plain(item, "message") for item in client.paginated(f"/courses/{encode_id(args.course_id)}/activity_stream", page_args(args), max_pages=args.max_pages, max_items=args.max_items)]


def command_search_course_content(client: CanvasClient, args: argparse.Namespace) -> Any:
    if args.limit < 1 or args.limit > MAX_SEARCH_RESULTS:
        raise UsageError(f"search limit must be between 1 and {MAX_SEARCH_RESULTS}")
    needle = args.query.casefold()
    results: list[dict[str, Any]] = []

    def add(kind: str, item: dict[str, Any], text_fields: list[str]) -> None:
        if len(results) >= args.limit:
            return
        haystack = " ".join(str(item.get(field, "")) for field in text_fields)
        plain = html_to_text(haystack)
        if needle in (haystack + " " + plain).casefold():
            results.append({"type": kind, "id": item.get("id") or item.get("url"), "title": item.get("name") or item.get("title"), "html_url": item.get("html_url"), "text": plain[:500], "source": item})

    for item in command_list_assignments(client, args):
        add("assignment", item, ["name", "description"])
    for page in command_list_pages(client, args):
        full = client.request_json(f"/courses/{encode_id(args.course_id)}/pages/{encode_id(page.get('url'))}") if page.get("url") else page
        add("page", full, ["title", "body"])
    for item in command_list_discussions(client, args):
        add("discussion", item, ["title", "message"])
    for item in command_list_announcements(client, args):
        add("announcement", item, ["title", "message"])
    return {"query": args.query, "course_id": args.course_id, "results": results[: args.limit]}


COMMANDS = {name.removeprefix("command_"): value for name, value in list(globals().items()) if name.startswith("command_")}


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)


def add_list_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fields", default="", help="Comma-separated top-level fields to keep after pagination")
    parser.add_argument("--match", default="", help="Case-insensitive substring match over scalar top-level values")
    parser.add_argument("--sort", default="", help="Top-level field used for client-side sort")
    parser.add_argument("--descending", action="store_true", help="Reverse --sort order")


def add_common_list(parser: argparse.ArgumentParser) -> None:
    add_common(parser)
    add_list_options(parser)


def add_course_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("course_id")


def search_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer from 1 to {MAX_SEARCH_RESULTS}") from exc
    if limit < 1 or limit > MAX_SEARCH_RESULTS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_SEARCH_RESULTS}")
    return limit


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Student-only read CLI for Canvas LMS")
    parser.add_argument("--version", action="version", version=f"canvas-student-cli {__version__}")
    parser.add_argument("--base-url", default=os.environ.get("CANVAS_BASE_URL", ""), help="Canvas origin, for example https://canvas.example.edu")
    parser.add_argument("--token-file", default=None, help="Owner-readable file containing the Canvas API token")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ["auth_check", "profile", "conversation_unread_count"]:
        sub.add_parser(name).set_defaults(func=COMMANDS[name])
    for name in ["dashboard_cards", "todo", "missing_submissions", "user_activity_stream"]:
        p = sub.add_parser(name)
        add_common_list(p)
        p.set_defaults(func=COMMANDS[name])

    p = sub.add_parser("upcoming_events")
    add_common_list(p)
    p.add_argument("--all-events", action="store_true")
    p.set_defaults(func=command_upcoming_events)

    p = sub.add_parser("list_courses")
    add_common_list(p)
    p.add_argument("--enrollment-state", default="")
    p.add_argument("--include-syllabus", action="store_true")
    p.set_defaults(func=command_list_courses)

    for name in ["get_course", "get_syllabus"]:
        p = sub.add_parser(name)
        add_course_id(p)
        p.set_defaults(func=COMMANDS[name])

    p = sub.add_parser("list_assignments")
    add_course_id(p)
    add_common_list(p)
    p.add_argument("--order-by", default="due_at", choices=["due_at", "position", "name"])
    p.add_argument("--bucket", default="")
    p.add_argument("--include-details", action="store_true")
    p.set_defaults(func=command_list_assignments)

    p = sub.add_parser("get_assignment")
    add_course_id(p)
    p.add_argument("assignment_id")
    p.set_defaults(func=command_get_assignment)

    p = sub.add_parser("list_assignment_groups")
    add_course_id(p)
    add_common_list(p)
    p.add_argument("--include-assignments", action="store_true")
    p.set_defaults(func=command_list_assignment_groups)

    for name in ["get_submission", "get_submission_feedback", "list_peer_reviews", "get_submission_peer_reviews"]:
        p = sub.add_parser(name)
        add_course_id(p)
        p.add_argument("assignment_id")
        if name == "get_submission_peer_reviews":
            p.add_argument("submission_id")
        if name.startswith("list") or name == "get_submission_peer_reviews":
            add_common_list(p)
        p.set_defaults(func=COMMANDS[name])

    for name in ["list_my_submissions", "get_grades", "list_quizzes", "list_discussions", "list_modules", "course_structure", "list_pages", "course_activity_stream"]:
        p = sub.add_parser(name)
        add_course_id(p)
        add_common_list(p)
        if name == "list_modules":
            p.add_argument("--include-items", action="store_true")
        p.set_defaults(func=COMMANDS[name])

    p = sub.add_parser("list_groups")
    add_common_list(p)
    p.add_argument("--course-id", default="")
    p.set_defaults(func=command_list_groups)

    for name, arg in [("get_quiz", "quiz_id"), ("get_discussion", "topic_id"), ("get_discussion_view", "topic_id"), ("get_module", "module_id"), ("get_page", "page_url")]:
        p = sub.add_parser(name)
        add_course_id(p)
        p.add_argument(arg)
        p.set_defaults(func=COMMANDS[name])

    p = sub.add_parser("list_files")
    add_common_list(p)
    p.add_argument("--course-id", default="")
    p.add_argument("--content-type", default="")
    p.set_defaults(func=command_list_files)

    p = sub.add_parser("get_file")
    p.add_argument("file_id")
    p.set_defaults(func=command_get_file)

    p = sub.add_parser("download_file")
    p.add_argument("file_id")
    p.add_argument("local_file")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_DOWNLOAD_BYTES)
    p.set_defaults(func=command_download_file)

    p = sub.add_parser("list_group_members")
    add_common_list(p)
    p.add_argument("group_id")
    p.set_defaults(func=command_list_group_members)

    for name in ["get_group"]:
        p = sub.add_parser(name)
        p.add_argument("group_id")
        p.set_defaults(func=COMMANDS[name])
    for name in ["list_group_files", "list_group_folders", "list_group_discussions", "list_group_pages"]:
        p = sub.add_parser(name)
        add_common_list(p)
        p.add_argument("group_id")
        p.set_defaults(func=COMMANDS[name])
    for name in ["get_group_discussion", "get_group_discussion_view"]:
        p = sub.add_parser(name)
        p.add_argument("group_id")
        p.add_argument("topic_id")
        p.set_defaults(func=COMMANDS[name])
    p = sub.add_parser("get_group_page")
    p.add_argument("group_id")
    p.add_argument("page_url")
    p.set_defaults(func=command_get_group_page)

    p = sub.add_parser("list_announcements")
    add_course_id(p)
    add_common_list(p)
    p.add_argument("--active-only", action="store_true")
    p.set_defaults(func=command_list_announcements)

    p = sub.add_parser("list_module_items")
    add_course_id(p)
    p.add_argument("module_id")
    add_common_list(p)
    p.set_defaults(func=command_list_module_items)

    p = sub.add_parser("calendar_events")
    add_common_list(p)
    p.add_argument("--context-code", action="append", default=[])
    p.add_argument("--start-date", default="")
    p.add_argument("--end-date", default="")
    p.add_argument("--event-type", default="")
    p.add_argument("--all-events", action="store_true")
    p.set_defaults(func=command_calendar_events)

    p = sub.add_parser("list_conversations")
    add_common_list(p)
    p.add_argument("--scope", default="")
    p.add_argument("--filter", action="append", default=[])
    p.add_argument("--context-code", default="")
    p.add_argument("--query", default="")
    p.set_defaults(func=command_list_conversations)

    p = sub.add_parser("get_conversation")
    p.add_argument("conversation_id")
    p.set_defaults(func=command_get_conversation)

    p = sub.add_parser("search_course_content")
    add_course_id(p)
    p.add_argument("query")
    add_common(p)
    p.add_argument("--limit", type=search_limit, default=MAX_SEARCH_RESULTS)
    p.add_argument("--order-by", default="due_at", choices=["due_at", "position", "name"])
    p.add_argument("--bucket", default="")
    p.add_argument("--include-details", action="store_true")
    p.add_argument("--active-only", action="store_true")
    p.set_defaults(func=command_search_course_content)
    return parser


def run(argv: list[str] | None = None, opener: Any | None = None) -> Any:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = CanvasClient(args.base_url, load_token(args.token_file), opener=opener)
    result = args.func(client, args)
    return apply_projection_filtering(result, args)


def error_payload(exc: CliError) -> dict[str, Any]:
    payload = {"error": {"type": exc.kind, "message": exc.message}}
    if exc.meta:
        payload["error"]["meta"] = exc.meta
    return payload


def main(argv: list[str] | None = None, opener: Any | None = None) -> int:
    try:
        compact_json(run(argv, opener=opener))
        return 0
    except CliError as exc:
        compact_json(error_payload(exc), file=sys.stderr)
        return exc.exit_code
    except Exception:
        compact_json({"error": {"type": "internal", "message": "internal error"}}, file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
