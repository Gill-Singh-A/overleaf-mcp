"""HTTP client for the Overleaf web app, using the same endpoints as the browser."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

CSRF_RE = re.compile(r'<meta\s+name="ol-csrfToken"\s+content="([^"]+)"')
OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")

# Overleaf allows one compile per project per second (CompileManager.COMPILE_DELAY).
COMPILE_RETRIES = 5
COMPILE_RETRY_DELAY = 1.5


class OverleafError(Exception):
    pass


class _SessionRejected(OverleafError):
    pass


@dataclass
class Entity:
    id: str
    type: str  # "doc", "file" or "folder"


@dataclass
class ProjectTree:
    name: str
    root_folder_id: str
    root_doc_id: str | None
    # Keyed by path relative to the project root; folder paths end in "/".
    entities: dict[str, Entity] = field(default_factory=dict)

    def find(self, path: str) -> Entity:
        path = path.strip("/")
        ent = self.entities.get(path) or self.entities.get(path + "/")
        if not ent:
            raise OverleafError(f"No such path in project: {path!r}")
        return ent

    def folder_id(self, folder_path: str) -> str:
        folder_path = folder_path.strip("/")
        if not folder_path:
            return self.root_folder_id
        ent = self.entities.get(folder_path + "/")
        if not ent:
            raise OverleafError(f"Folder does not exist: {folder_path!r}")
        return ent.id

    @property
    def root_doc_path(self) -> str | None:
        return next((p for p, e in self.entities.items() if e.id == self.root_doc_id), None)


class Overleaf:
    def __init__(self, url: str, email: str, password: str, timeout: float = 120) -> None:
        self.url = url.rstrip("/")
        self.email = email
        self.password = password
        self.http = httpx.Client(base_url=self.url, timeout=timeout, follow_redirects=False)
        self.csrf: str | None = None
        self.logged_in = False

    # ---- session -----------------------------------------------------------

    def _fetch_csrf(self, page: str) -> str:
        r = self.http.get(page, follow_redirects=True)
        m = CSRF_RE.search(r.text)
        if not m:
            raise OverleafError(f"Could not find CSRF token on {page} (HTTP {r.status_code})")
        return m.group(1)

    def login(self) -> None:
        if not self.email or not self.password:
            raise OverleafError("OVERLEAF_EMAIL / OVERLEAF_PASSWORD are not configured")
        self.http.cookies.clear()
        csrf = self._fetch_csrf("/login")
        r = self.http.post(
            "/login",
            json={"email": self.email, "password": self.password, "_csrf": csrf},
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            raise OverleafError(f"Login failed (HTTP {r.status_code}): {r.text[:300]}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if "redir" not in body:
            raise OverleafError(f"Login failed: {body.get('message', body)}")
        self.csrf = self._fetch_csrf("/project")
        self.logged_in = True

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        """Authenticated request; logs in (again) when the session is missing or expired."""
        extra_headers = kw.pop("headers", None) or {}
        for attempt in range(2):
            if not self.logged_in:
                self.login()
            headers = {"X-Csrf-Token": self.csrf or "", "Accept": "application/json"}
            headers.update(extra_headers)
            r = self.http.request(method, url, headers=headers, **kw)
            expired = r.status_code in (401, 403) or (
                r.status_code == 302 and "/login" in r.headers.get("location", "")
            )
            if expired and attempt == 0:
                self.logged_in = False
                continue
            return r
        return r

    @staticmethod
    def check(r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code >= 400 or r.status_code == 302:
            raise OverleafError(f"{what} failed (HTTP {r.status_code}): {r.text[:500]}")
        return r

    # ---- projects ----------------------------------------------------------

    def projects(self) -> list[dict]:
        r = self.check(self.request("GET", "/user/projects"), "Listing projects")
        return r.json()["projects"]

    def resolve_project(self, project: str) -> str:
        """Accept a project id or an (exact, then case-insensitive) project name."""
        if OBJECT_ID_RE.match(project):
            return project
        projects = self.projects()
        for matches in (
            lambda p: p["name"] == project,
            lambda p: p["name"].lower() == project.lower(),
        ):
            hits = [p for p in projects if matches(p)]
            if len(hits) == 1:
                return hits[0]["_id"]
            if len(hits) > 1:
                raise OverleafError(
                    f"Several projects are named {project!r}; use an id: {[h['_id'] for h in hits]}"
                )
        raise OverleafError(f"No project named {project!r}")

    def create_project(self, name: str, template: str = "none") -> str:
        r = self.request("POST", "/project/new", json={"projectName": name, "template": template})
        return self.check(r, "Creating project").json()["project_id"]

    def tree(self, project_id: str) -> ProjectTree:
        """Fetch the project's file tree, with entity ids, from the real-time service."""
        try:
            project = self._join_project(project_id)
        except _SessionRejected:
            self.logged_in = False
            project = self._join_project(project_id)
        root = project["rootFolder"][0]
        tree = ProjectTree(
            name=project.get("name", ""),
            root_folder_id=root["_id"],
            root_doc_id=project.get("rootDoc_id"),
        )

        def walk(folder: dict, prefix: str) -> None:
            for d in folder.get("docs", []):
                tree.entities[prefix + d["name"]] = Entity(d["_id"], "doc")
            for f in folder.get("fileRefs", []):
                tree.entities[prefix + f["name"]] = Entity(f["_id"], "file")
            for sub in folder.get("folders", []):
                path = prefix + sub["name"] + "/"
                tree.entities[path] = Entity(sub["_id"], "folder")
                walk(sub, path)

        walk(root, "")
        return tree

    def _join_project(self, project_id: str) -> dict:
        # The real-time service speaks socket.io 0.9; xhr-polling avoids a websocket dependency.
        # Connecting with ?projectId=... makes the server join the project and emit its tree.
        if not self.logged_in:
            self.login()
        q = {"projectId": project_id}
        r = self.http.get("/socket.io/1/", params={**q, "t": int(time.time() * 1000)})
        self.check(r, "Opening real-time session")
        sid = r.text.split(":", 1)[0]
        poll_url = f"/socket.io/1/xhr-polling/{sid}"
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                r = self.http.get(poll_url, params={**q, "t": int(time.time() * 1000)})
                self.check(r, "Polling real-time session")
                for msg in split_socketio_frames(r.text):
                    if msg.startswith("0:"):
                        raise _SessionRejected(f"Real-time session closed: {msg}")
                    if msg.startswith("7:"):
                        raise OverleafError(f"Real-time error: {msg}")
                    if msg.startswith("5:"):
                        event = json.loads(msg.split(":", 3)[3])
                        if event.get("name") == "joinProjectResponse":
                            return event["args"][0]["project"]
                        if event.get("name") in ("connectionRejected", "forceDisconnect"):
                            raise _SessionRejected(f"Real-time rejected: {event}")
            raise OverleafError("Timed out waiting for joinProjectResponse")
        finally:
            try:
                self.http.get(poll_url, params={**q, "disconnect": 1})
            except httpx.HTTPError:
                pass

    # ---- files -------------------------------------------------------------

    def read(self, project_id: str, ent: Entity) -> bytes:
        if ent.type == "doc":
            r = self.request("GET", f"/Project/{project_id}/doc/{ent.id}/download")
        elif ent.type == "file":
            r = self.request("GET", f"/Project/{project_id}/file/{ent.id}")
        else:
            raise OverleafError("Cannot read a folder")
        return self.check(r, "Reading file").content

    def upload(self, project_id: str, path: str, data: bytes) -> dict:
        """Create or replace a file; Overleaf creates missing parent folders from relativePath."""
        path = path.strip("/")
        root_id = self.tree(project_id).root_folder_id
        name = path.rsplit("/", 1)[-1]
        r = self.request(
            "POST",
            f"/Project/{project_id}/upload",
            params={"folder_id": root_id},
            data={"name": name, "relativePath": path},
            files={"qqfile": (name, data, "application/octet-stream")},
        )
        body = self.check(r, f"Uploading {path}").json()
        if not body.get("success"):
            raise OverleafError(f"Upload of {path} failed: {body}")
        return body

    def delete(self, project_id: str, ent: Entity) -> None:
        r = self.request("DELETE", f"/project/{project_id}/{ent.type}/{ent.id}")
        self.check(r, "Deleting")

    def move(self, project_id: str, ent: Entity, folder_id: str) -> None:
        r = self.request(
            "POST", f"/project/{project_id}/{ent.type}/{ent.id}/move", json={"folder_id": folder_id}
        )
        self.check(r, "Moving")

    def rename(self, project_id: str, ent: Entity, name: str) -> None:
        r = self.request(
            "POST", f"/project/{project_id}/{ent.type}/{ent.id}/rename", json={"name": name}
        )
        self.check(r, "Renaming")

    def download_zip(self, project_id: str) -> bytes:
        r = self.request("GET", f"/Project/{project_id}/download/zip")
        return self.check(r, "Downloading zip").content

    # ---- compile -----------------------------------------------------------

    def compile(self, project_id: str, draft: bool = False) -> dict:
        for _ in range(COMPILE_RETRIES):
            r = self.request(
                "POST",
                f"/project/{project_id}/compile",
                params={"auto_compile": "false"},
                json={
                    "rootDoc_id": None,
                    "draft": draft,
                    "check": "silent",
                    "incrementalCompilesEnabled": False,
                    "stopOnFirstError": False,
                },
            )
            body = self.check(r, "Compiling").json()
            if body.get("status") != "too-recently-compiled":
                return body
            time.sleep(COMPILE_RETRY_DELAY)
        return body

    def fetch_output(self, compile_result: dict, output_file: dict) -> bytes:
        server_id = compile_result.get("clsiServerId")
        params = {"clsiserverid": server_id} if server_id else {}
        r = self.request("GET", output_file["url"], params=params)
        return self.check(r, f"Fetching {output_file['path']}").content


def split_socketio_frames(payload: str) -> list[str]:
    """socket.io 0.9 batches messages as \\ufffd<len>\\ufffd<msg>... when there is more than one."""
    if not payload.startswith("�"):
        return [payload] if payload else []
    parts = payload.split("�")
    return [parts[i] for i in range(2, len(parts), 2)]


def parse_latex_log(log: str) -> tuple[list[str], list[str]]:
    """Pull errors (with two lines of context) and warnings out of a LaTeX log."""
    errors, warnings = [], []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!") or re.match(r"^\S+:\d+: ", line):
            errors.append("\n".join(lines[i : i + 3]).strip())
        elif "Warning:" in line:
            warnings.append(line.strip())
    return errors, warnings
