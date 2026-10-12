import json
import os

import httpx
import pytest

from overleaf_mcp import client as client_mod
from overleaf_mcp.client import (
    Entity,
    Overleaf,
    OverleafError,
    ProjectTree,
    parse_latex_log,
    split_socketio_frames,
)
from overleaf_mcp.server import load_env_file

CSRF_PAGE = '<html><meta name="ol-csrfToken" content="tok123"></html>'


def make_client(handler) -> Overleaf:
    ol = Overleaf("http://overleaf.test", "me@example.com", "secret")
    ol.http = httpx.Client(base_url=ol.url, transport=httpx.MockTransport(handler))
    return ol


def login_routes(request: httpx.Request) -> httpx.Response | None:
    if request.url.path in ("/login", "/project") and request.method == "GET":
        return httpx.Response(200, text=CSRF_PAGE)
    if request.url.path == "/login" and request.method == "POST":
        assert json.loads(request.content)["_csrf"] == "tok123"
        return httpx.Response(200, json={"redir": "/project"})
    return None


def test_split_socketio_frames_single_and_batched():
    assert split_socketio_frames("") == []
    assert split_socketio_frames("1::") == ["1::"]
    batched = "\ufffd3\ufffd1::\ufffd8\ufffd5:::{\"a\"}"
    assert split_socketio_frames(batched) == ["1::", '5:::{"a"}']


def test_parse_latex_log():
    log = "\n".join(
        [
            "This is pdfTeX",
            "! Undefined control sequence.",
            "l.3 \\foo",
            "        bar",
            "LaTeX Warning: Reference `x' on page 1 undefined.",
            "./main.tex:12: Missing $ inserted.",
        ]
    )
    errors, warnings = parse_latex_log(log)
    assert errors[0] == "! Undefined control sequence.\nl.3 \\foo\n        bar"
    assert errors[1].startswith("./main.tex:12: Missing $ inserted.")
    assert warnings == ["LaTeX Warning: Reference `x' on page 1 undefined."]


def test_project_tree_lookup():
    tree = ProjectTree(
        name="p",
        root_folder_id="root",
        root_doc_id="d1",
        entities={
            "main.tex": Entity("d1", "doc"),
            "img/": Entity("f1", "folder"),
            "img/a.png": Entity("x1", "file"),
        },
    )
    assert tree.find("/main.tex").id == "d1"
    assert tree.find("img").type == "folder"
    assert tree.folder_id("") == "root"
    assert tree.folder_id("img/") == "f1"
    assert tree.root_doc_path == "main.tex"
    with pytest.raises(OverleafError):
        tree.find("missing.tex")
    with pytest.raises(OverleafError):
        tree.folder_id("nope")


def test_request_logs_in_again_when_session_expires():
    calls = {"projects": 0, "logins": 0}

    def handler(request):
        if request.url.path == "/login" and request.method == "POST":
            calls["logins"] += 1
        if (resp := login_routes(request)) is not None:
            return resp
        if request.url.path == "/user/projects":
            calls["projects"] += 1
            if calls["projects"] == 1:
                return httpx.Response(302, headers={"location": "/login"})
            assert request.headers["X-Csrf-Token"] == "tok123"
            return httpx.Response(200, json={"projects": [{"_id": "a" * 24, "name": "Thesis"}]})
        return httpx.Response(404)

    ol = make_client(handler)
    ol.logged_in, ol.csrf = True, "stale"
    assert ol.projects()[0]["name"] == "Thesis"
    assert calls == {"projects": 2, "logins": 1}


def test_resolve_project_by_name():
    projects = [
        {"_id": "a" * 24, "name": "Thesis"},
        {"_id": "b" * 24, "name": "Paper"},
        {"_id": "c" * 24, "name": "paper"},
    ]

    def handler(request):
        if (resp := login_routes(request)) is not None:
            return resp
        return httpx.Response(200, json={"projects": projects})

    ol = make_client(handler)
    assert ol.resolve_project("d" * 24) == "d" * 24
    assert ol.resolve_project("thesis") == "a" * 24
    assert ol.resolve_project("Paper") == "b" * 24
    with pytest.raises(OverleafError, match="Several projects"):
        ol.resolve_project("PAPER")
    with pytest.raises(OverleafError, match="No project"):
        ol.resolve_project("Missing")


def test_compile_retries_when_too_recently_compiled(monkeypatch):
    monkeypatch.setattr(client_mod, "COMPILE_RETRY_DELAY", 0)
    statuses = iter(["too-recently-compiled", "success"])

    def handler(request):
        if (resp := login_routes(request)) is not None:
            return resp
        return httpx.Response(200, json={"status": next(statuses), "outputFiles": []})

    assert make_client(handler).compile("a" * 24)["status"] == "success"


def test_load_env_file_does_not_override_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nOVERLEAF_URL='http://from-file'\nOVERLEAF_EMAIL=file@example.com\n")
    monkeypatch.delenv("OVERLEAF_URL", raising=False)
    monkeypatch.setenv("OVERLEAF_EMAIL", "env@example.com")
    load_env_file(env)
    assert os.environ["OVERLEAF_URL"] == "http://from-file"
    assert os.environ["OVERLEAF_EMAIL"] == "env@example.com"
