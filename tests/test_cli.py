import contextlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from canvas_student_cli import cli


class FakeHeaders(dict):
    def get(self, key, default=None):
        for existing, value in self.items():
            if existing.lower() == key.lower():
                return value
        return default


class FakeResponse:
    def __init__(self, data, status=200, headers=None, raw=False):
        self.status = status
        self.code = status
        self.headers = FakeHeaders(headers or {})
        self.data = data if raw or isinstance(data, bytes) else json.dumps(data).encode()
        self.offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self.data[self.offset :]
            self.offset = len(self.data)
            return chunk
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=30):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def canvas_env(monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example.edu")
    monkeypatch.setenv("CANVAS_API_TOKEN", "credential-placeholder")


def run_cli(argv, responses):
    opener = FakeOpener(responses)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv, opener=opener)
    return code, stdout.getvalue(), stderr.getvalue(), opener


def test_package_entrypoint_help_and_version_are_credential_free():
    env = os.environ.copy()
    env["CANVAS_API_TOKEN"] = "credential-placeholder"
    for args in (["--help"], ["--version"]):
        result = subprocess.run([sys.executable, "-m", "canvas_student_cli", *args], check=False, text=True, capture_output=True, env=env)
        assert result.returncode == 0
        assert "credential-placeholder" not in result.stdout + result.stderr
    assert "canvas-student-cli" in result.stdout


def test_representative_legacy_endpoints_and_compact_success():
    checks = [
        (["list_courses", "--enrollment-state", "active"], "/courses", "enrollment_state=active"),
        (["list_assignments", "42", "--order-by", "name"], "/courses/42/assignments", "order_by=name"),
        (["get_submission", "42", "7"], "/courses/42/assignments/7/submissions/self", "include%5B%5D=assignment"),
        (["list_groups", "--course-id", "42"], "/courses/42/groups", ""),
        (["get_discussion_view", "42", "8"], "/courses/42/discussion_topics/8/view", ""),
        (["calendar_events", "--context-code", "course_42"], "/calendar_events", "context_codes%5B%5D=course_42"),
        (["list_conversations", "--scope", "unread"], "/conversations", "scope=unread"),
    ]
    for argv, path, query in checks:
        data = {} if argv[0].startswith("get_") else []
        code, stdout, stderr, opener = run_cli(argv, [FakeResponse(data)])
        assert code == 0, stderr
        assert stdout in {"{}\n", "[]\n"}
        assert path in opener.requests[0].full_url
        if query:
            assert query in opener.requests[0].full_url


def test_group_endpoints_encode_ids_and_add_plain_text():
    checks = [
        (["get_group", "a/b"], "/groups/a%2Fb"),
        (["list_group_files", "7"], "/groups/7/files"),
        (["list_group_folders", "7"], "/groups/7/folders"),
        (["list_group_discussions", "7"], "/groups/7/discussion_topics"),
        (["get_group_discussion", "7", "t/1"], "/groups/7/discussion_topics/t%2F1"),
        (["get_group_discussion_view", "7", "8"], "/groups/7/discussion_topics/8/view"),
        (["list_group_pages", "7"], "/groups/7/pages"),
        (["get_group_page", "7", "front/page"], "/groups/7/pages/front%2Fpage"),
    ]
    for argv, path in checks:
        data = [{"message": "<p>Hello</p>"}] if argv[0].startswith("list_") else {"message": "<p>Hello</p>", "body": "<p>Body</p>"}
        code, stdout, stderr, opener = run_cli(argv, [FakeResponse(data)])
        assert code == 0, stderr
        assert path in opener.requests[0].full_url
        parsed = json.loads(stdout)
        if argv[0] in {"list_group_discussions", "get_group_discussion"}:
            item = parsed[0] if isinstance(parsed, list) else parsed
            assert item["message_text"] == "Hello"
        if argv[0] == "get_group_page":
            assert parsed["body_text"] == "Body"


def test_pagination_projection_match_sort_and_errors():
    page2 = "https://canvas.example.edu/api/v1/courses?page=2"
    responses = [
        FakeResponse([{"id": 2, "name": "Beta", "term": {}}], headers={"Link": f'<{page2}>; rel="next"'}),
        FakeResponse([{"id": 1, "name": "Alpha", "term": {}}]),
    ]
    code, stdout, stderr, opener = run_cli(["list_courses", "--fields", "id,name", "--match", "a", "--sort", "name", "--descending"], responses)
    assert code == 0, stderr
    assert json.loads(stdout) == [{"id": 2, "name": "Beta"}, {"id": 1, "name": "Alpha"}]
    assert len(opener.requests) == 2

    code, stdout, stderr, _ = run_cli(["list_courses", "--fields", "missing"], [FakeResponse([{"id": 1}])])
    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert json.loads(stderr)["error"]["type"] == "usage"

    code, _, stderr, _ = run_cli(["list_courses", "--descending"], [FakeResponse([{"id": 1}])])
    assert code == cli.EXIT_USAGE
    assert json.loads(stderr)["error"]["message"] == "--descending requires --sort"


def test_projection_and_sort_empty_results_are_empty():
    code, stdout, stderr, _ = run_cli(["list_courses", "--fields", "id,missing", "--sort", "missing"], [FakeResponse([])])
    assert code == 0, stderr
    assert json.loads(stdout) == []


def test_numeric_sort_preserves_number_order_and_mixed_determinism():
    data = [
        {"id": 10, "name": "ten", "size": 10},
        {"id": 2, "name": "two", "size": 2},
        {"id": 1, "name": "one", "size": "11"},
        {"id": 3, "name": "missing"},
        {"id": 4, "name": "text", "size": "unknown"},
    ]

    code, stdout, stderr, _ = run_cli(["list_files", "--sort", "size", "--fields", "id,size"], [FakeResponse(data)])
    assert code == 0, stderr
    assert json.loads(stdout) == [
        {"id": 2, "size": 2},
        {"id": 10, "size": 10},
        {"id": 1, "size": "11"},
        {"id": 4, "size": "unknown"},
        {"id": 3, "size": None},
    ]

    code, stdout, stderr, _ = run_cli(["list_files", "--sort", "id", "--fields", "id"], [FakeResponse(data)])
    assert code == 0, stderr
    assert json.loads(stdout) == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 10}]


def test_conversation_client_filters_preserve_canvas_flags():
    data = [
        {"id": 1, "context_code": "course_1", "subject": "Lab note"},
        {"id": 2, "context_code": "course_2", "subject": "Old noise"},
    ]
    code, stdout, stderr, opener = run_cli(["list_conversations", "--scope", "archived", "--filter", "starred", "--context-code", "course_1", "--query", "lab"], [FakeResponse(data)])
    assert code == 0, stderr
    assert json.loads(stdout) == [data[0]]
    assert "scope=archived" in opener.requests[0].full_url
    assert "filter%5B%5D=starred" in opener.requests[0].full_url


def test_error_json_status_and_redaction():
    err = HTTPError("https://canvas.example.edu/api/v1/courses", 404, "missing credential-placeholder", {}, io.BytesIO(b"credential-placeholder"))
    code, stdout, stderr, _ = run_cli(["list_courses"], [err])
    payload = json.loads(stderr)
    assert code == cli.EXIT_CANVAS_HTTP
    assert stdout == ""
    assert payload["error"]["type"] == "canvas_http"
    assert payload["error"]["meta"]["status"] == 404
    assert "credential-placeholder" not in stderr

    code, _, stderr, _ = run_cli(["list_courses"], [RuntimeError("credential-placeholder https://canvas.example.edu")])
    assert code == cli.EXIT_INTERNAL
    assert json.loads(stderr) == {"error": {"type": "internal", "message": "internal error"}}


def test_get_only_static_and_runtime_invariant():
    client = cli.CanvasClient("https://canvas.example.edu", "credential-placeholder", opener=FakeOpener([]))
    with pytest.raises(cli.SecurityError):
        client.request_json("/users/self/profile", method="post")
    source = Path(cli.__file__).read_text(encoding="utf-8")
    cleaned = source.replace('method != "GET"', "").replace('method="GET"', "")
    for forbidden in ["POST", "PUT", "PATCH", "DELETE"]:
        assert forbidden not in cleaned
    for forbidden_command in ["submit_assignment", "send_message", "reply_to_discussion", "create_calendar", "mark_read", "grade_change", "upload_file"]:
        assert forbidden_command not in source


def test_download_file_safe_success_no_cross_origin_auth_and_overwrite_policy(tmp_path):
    dest = tmp_path / "file.txt"
    data = b"hello"
    metadata = {"id": 10, "display_name": "File", "url": "https://files.example.net/download/opaque-path"}
    code, stdout, stderr, opener = run_cli(["download_file", "10", str(dest), "--max-bytes", "10"], [FakeResponse(metadata), FakeResponse(data, headers={"Content-Length": str(len(data))}, raw=True)])
    assert code == 0, stderr
    assert dest.read_bytes() == data
    payload = json.loads(stdout)
    assert payload["sha256"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert "opaque-path" not in stdout
    assert opener.requests[0].get_header("Authorization") == "Bearer credential-placeholder"
    assert opener.requests[1].get_header("Authorization") is None

    code, _, stderr, _ = run_cli(["download_file", "10", str(dest)], [FakeResponse(metadata)])
    assert code == cli.EXIT_SECURITY
    assert "destination exists" in stderr

    code, _, stderr, _ = run_cli(["download_file", "10", str(dest), "--overwrite"], [FakeResponse(metadata), FakeResponse(b"ok", headers={"Content-Length": "2"}, raw=True)])
    assert code == 0, stderr
    assert dest.read_bytes() == b"ok"


def test_download_rejects_size_bounds_redirects_symlinks_and_cleans_tmp(tmp_path):
    dest = tmp_path / "file.bin"
    metadata = {"display_name": "File", "url": "https://files.example.net/file"}
    code, _, _, _ = run_cli(["download_file", "1", str(dest), "--max-bytes", "3"], [FakeResponse(metadata), FakeResponse(b"1234", headers={"Content-Length": "4"}, raw=True)])
    assert code == cli.EXIT_SECURITY
    assert not dest.exists()
    assert list(tmp_path.glob(".*.tmp")) == []

    link = tmp_path / "link"
    link.symlink_to(dest)
    code, _, _, _ = run_cli(["download_file", "1", str(link)], [FakeResponse(metadata)])
    assert code == cli.EXIT_SECURITY

    real = tmp_path / "real"
    real.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real, target_is_directory=True)
    code, _, _, _ = run_cli(["download_file", "1", str(parent_link / "x")], [FakeResponse(metadata)])
    assert code == cli.EXIT_SECURITY

    remote_http = {"display_name": "File", "url": "http://files.example.net/file"}
    code, _, _, _ = run_cli(["download_file", "1", str(dest)], [FakeResponse(remote_http)])
    assert code == cli.EXIT_SECURITY

    responses = [FakeResponse(metadata)] + [FakeResponse(b"", status=302, headers={"Location": "https://files.example.net/next"}, raw=True)] * 6
    code, _, _, _ = run_cli(["download_file", "1", str(dest)], responses)
    assert code == cli.EXIT_SECURITY


def test_download_rejects_invalid_or_mismatched_content_length_and_cleans_tmp(tmp_path):
    dest = tmp_path / "file.bin"
    metadata = {"display_name": "File", "url": "https://files.example.net/file"}

    for header, body in [("-1", b""), ("2.0", b"12"), ("5", b"12"), ("2", b"1234")]:
        code, _, _, _ = run_cli(["download_file", "1", str(dest), "--max-bytes", "10"], [FakeResponse(metadata), FakeResponse(body, headers={"Content-Length": header}, raw=True)])
        assert code == cli.EXIT_SECURITY
        assert not dest.exists()
        assert list(tmp_path.glob(".*.tmp")) == []


def test_download_rejects_symlinked_ancestor_and_directory_overwrite(tmp_path):
    metadata = {"display_name": "File", "url": "https://files.example.net/file"}
    real = tmp_path / "real"
    real.mkdir()
    nested = real / "nested"
    nested.mkdir()
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real, target_is_directory=True)

    code, _, _, _ = run_cli(["download_file", "1", str(parent_link / "nested" / "x")], [FakeResponse(metadata)])
    assert code == cli.EXIT_SECURITY

    existing_dir = tmp_path / "existing-dir"
    existing_dir.mkdir()
    code, _, _, _ = run_cli(["download_file", "1", str(existing_dir), "--overwrite"], [FakeResponse(metadata)])
    assert code == cli.EXIT_SECURITY


def test_token_file_permissions_portable(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("file-token\n", encoding="utf-8")
    if os.name != "nt":
        token.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert cli.load_token(str(token)) == "file-token"
    if os.name != "nt":
        token.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
        with pytest.raises(cli.SecurityError):
            cli.load_token(str(token))


def test_isolated_console_script_import_smoke(tmp_path):
    result = subprocess.run([sys.executable, "-c", "import canvas_student_cli; print(canvas_student_cli.__version__)"], check=True, text=True, capture_output=True)
    assert result.stdout.strip() == cli.__version__
