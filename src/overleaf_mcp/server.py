"""MCP server exposing a self-hosted Overleaf instance to MCP clients such as Claude Code."""

from __future__ import annotations

import argparse
import io
import logging
import os
import zipfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from overleaf_mcp.client import Overleaf, OverleafError, parse_latex_log

DEFAULT_URL = "http://localhost"


def default_env_file() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "overleaf-mcp" / ".env"


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_client: Overleaf | None = None


def client() -> Overleaf:
    global _client
    if _client is None:
        _client = Overleaf(
            os.environ.get("OVERLEAF_URL", DEFAULT_URL),
            os.environ.get("OVERLEAF_EMAIL", ""),
            os.environ.get("OVERLEAF_PASSWORD", ""),
        )
    return _client


mcp = MCPServer(
    "overleaf",
    instructions=(
        "Tools for a self-hosted Overleaf instance. Projects can be referred to by name or by "
        "24-char id. Paths are relative to the project root, e.g. 'main.tex' or "
        "'chapters/intro.tex'. Writing a file replaces it in the project; collaborators with the "
        "file open in the browser will see it reload. A compile can report status 'success' "
        "while the log still contains LaTeX errors, so check the 'errors' list."
    ),
)


@mcp.tool()
def list_projects() -> list[dict]:
    """List the Overleaf projects the configured account can access (id, name, access level)."""
    return [
        {"id": p["_id"], "name": p["name"], "access": p.get("accessLevel")}
        for p in client().projects()
    ]


@mcp.tool()
def list_files(project: str) -> dict:
    """List every doc, file and folder in a project, with the main (root) document marked."""
    ol = client()
    project_id = ol.resolve_project(project)
    tree = ol.tree(project_id)
    return {
        "project_id": project_id,
        "name": tree.name,
        "root_doc": tree.root_doc_path,
        "entries": [{"path": p, "type": e.type} for p, e in sorted(tree.entities.items())],
    }


@mcp.tool()
def read_file(project: str, path: str) -> str:
    """Read a text file (e.g. .tex, .bib, .sty) from a project. Returns its current content."""
    ol = client()
    project_id = ol.resolve_project(project)
    ent = ol.tree(project_id).find(path)
    if ent.type == "folder":
        raise OverleafError(f"{path!r} is a folder")
    data = ol.read(project_id, ent)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise OverleafError(
            f"{path!r} is binary ({len(data)} bytes); use download_project to fetch it"
        ) from None


@mcp.tool()
def write_file(project: str, path: str, content: str) -> str:
    """Create or overwrite a text file in a project. Missing parent folders are created."""
    ol = client()
    ol.upload(ol.resolve_project(project), path, content.encode("utf-8"))
    return f"Wrote {path} ({len(content)} chars)"


@mcp.tool()
def upload_local_file(project: str, local_path: str, dest_path: str | None = None) -> str:
    """Upload a file from the local disk (e.g. an image or PDF figure) into a project.

    dest_path defaults to the local file's name at the project root.
    """
    src = Path(local_path).expanduser()
    if not src.is_file():
        raise OverleafError(f"Local file not found: {src}")
    ol = client()
    dest = dest_path or src.name
    ol.upload(ol.resolve_project(project), dest, src.read_bytes())
    return f"Uploaded {src} -> {dest}"


@mcp.tool()
def delete_path(project: str, path: str) -> str:
    """Delete a doc, file or folder (recursively) from a project."""
    ol = client()
    project_id = ol.resolve_project(project)
    ent = ol.tree(project_id).find(path)
    ol.delete(project_id, ent)
    return f"Deleted {ent.type} {path}"


@mcp.tool()
def move_or_rename(project: str, path: str, new_path: str) -> str:
    """Rename and/or move a doc, file or folder. The destination folder must already exist."""
    ol = client()
    project_id = ol.resolve_project(project)
    tree = ol.tree(project_id)
    ent = tree.find(path)
    old_dir, _, old_name = path.strip("/").rpartition("/")
    new_dir, _, new_name = new_path.strip("/").rpartition("/")
    if new_dir != old_dir:
        ol.move(project_id, ent, tree.folder_id(new_dir))
    if new_name != old_name:
        ol.rename(project_id, ent, new_name)
    return f"{path} -> {new_path}"


@mcp.tool()
def create_project(name: str, template: str = "none") -> dict:
    """Create a new project. template: 'none' (blank main.tex) or 'example' (Overleaf's sample)."""
    return {"project_id": client().create_project(name, template), "name": name}


@mcp.tool()
def compile_project(project: str, save_pdf_to: str | None = None, draft: bool = False) -> dict:
    """Compile a project with LaTeX. Returns the status plus errors/warnings from the log.

    If save_pdf_to is a local path, the resulting PDF is written there.
    """
    ol = client()
    body = ol.compile(ol.resolve_project(project), draft=draft)
    files = {f["path"]: f for f in body.get("outputFiles", [])}
    result: dict = {"status": body.get("status"), "output_files": sorted(files)}

    if "output.log" in files:
        log = ol.fetch_output(body, files["output.log"]).decode("utf-8", errors="replace")
        errors, warnings = parse_latex_log(log)
        result["errors"] = errors
        result["warnings"] = warnings[:30]
        if body.get("status") != "success" or errors:
            result["log_tail"] = log[-3000:]

    if save_pdf_to and "output.pdf" in files:
        dest = Path(save_pdf_to).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(ol.fetch_output(body, files["output.pdf"]))
        result["pdf_saved_to"] = str(dest)
    return result


@mcp.tool()
def download_project(project: str, dest_dir: str) -> str:
    """Download a whole project (all docs and binary files) and extract it into a local folder."""
    ol = client()
    project_id = ol.resolve_project(project)
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(ol.download_zip(project_id))) as z:
        names = z.namelist()
        for member in names:
            if not (dest / member).resolve().is_relative_to(dest.resolve()):
                raise OverleafError(f"Refusing to extract unsafe path {member!r}")
        z.extractall(dest)
    return f"Extracted {len(names)} files into {dest}"


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for a self-hosted Overleaf instance.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.environ.get("OVERLEAF_ENV_FILE") or default_env_file()),
        help="file with OVERLEAF_URL / OVERLEAF_EMAIL / OVERLEAF_PASSWORD "
        "(default: %(default)s; real environment variables take precedence)",
    )
    args = parser.parse_args()
    load_env_file(args.env_file.expanduser())
    # One INFO line per HTTP request is noise in the MCP client's server log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
