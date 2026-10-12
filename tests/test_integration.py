"""End-to-end test against a live Overleaf instance.

Runs only when credentials are available, e.g.:

    OVERLEAF_ENV_FILE=~/.config/overleaf-mcp/.env pytest -m integration

It creates a throwaway project named `overleaf-mcp-test-<timestamp>` and moves it
to the trash afterwards.
"""

import base64
import os
import time
from pathlib import Path

import pytest

from overleaf_mcp import server
from overleaf_mcp.server import default_env_file, load_env_file

load_env_file(Path(os.environ.get("OVERLEAF_ENV_FILE") or default_env_file()).expanduser())

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("OVERLEAF_EMAIL") and os.environ.get("OVERLEAF_PASSWORD")),
        reason="OVERLEAF_EMAIL / OVERLEAF_PASSWORD not configured",
    ),
]

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture(scope="module")
def project():
    name = f"overleaf-mcp-test-{int(time.time())}"
    project_id = server.create_project(name)["project_id"]
    yield name
    ol = server.client()
    ol.request("POST", f"/project/{project_id}/trash")


def test_full_workflow(project, tmp_path):
    assert any(p["name"] == project for p in server.list_projects())
    assert server.list_files(project)["root_doc"] == "main.tex"
    assert "\\documentclass" in server.read_file(project, "main.tex")

    main = (
        "\\documentclass{article}\\usepackage{graphicx}\\begin{document}"
        "\\input{chapters/intro}\\includegraphics[width=1cm]{img/dot.png}\\end{document}"
    )
    server.write_file(project, "main.tex", main)
    assert server.read_file(project, "main.tex") == main
    server.write_file(project, "chapters/intro.tex", "Intro text.")
    png = tmp_path / "dot.png"
    png.write_bytes(PNG_1PX)
    server.upload_local_file(project, str(png), "img/dot.png")

    paths = {e["path"] for e in server.list_files(project)["entries"]}
    assert {"chapters/", "chapters/intro.tex", "img/", "img/dot.png", "main.tex"} <= paths

    result = server.compile_project(project, save_pdf_to=str(tmp_path / "out.pdf"))
    assert result["status"] == "success" and result["errors"] == []
    assert (tmp_path / "out.pdf").read_bytes().startswith(b"%PDF")

    server.write_file(project, "chapters/intro.tex", "Intro \\undefinedcommand text.")
    result = server.compile_project(project)
    assert any("Undefined control sequence" in e for e in result["errors"])

    server.move_or_rename(project, "chapters/intro.tex", "intro.tex")
    server.delete_path(project, "chapters")
    server.delete_path(project, "img/dot.png")
    paths = {e["path"] for e in server.list_files(project)["entries"]}
    assert "intro.tex" in paths and "chapters/" not in paths and "img/dot.png" not in paths

    server.download_project(project, str(tmp_path / "dl"))
    assert (tmp_path / "dl" / "main.tex").read_text() == main
