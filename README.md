# overleaf-mcp

An [MCP](https://modelcontextprotocol.io) server for **self-hosted Overleaf** (Community Edition or
Server Pro). It lets Claude Code, or any other MCP client, list projects, read and write files,
upload images, compile with LaTeX and fetch the PDF. It all runs against your own Overleaf instance.

```
> compile my "Thesis" project and fix the errors
● overleaf - compile_project (project: "Thesis")
  ⎿ status: success, errors: ["! Undefined control sequence.\nl.42 \\citet ..."]
● overleaf - read_file (project: "Thesis", path: "chapters/related.tex")
● overleaf - write_file (project: "Thesis", path: "chapters/related.tex", ...)
● overleaf - compile_project (project: "Thesis")
  ⎿ status: success, errors: []
```

Overleaf Community Edition has no Git bridge and no public API. This server signs in as a normal
user and calls the same HTTP endpoints the browser editor uses. You don't need any changes to
your Overleaf deployment.

## Tools

| Tool | What it does |
|---|---|
| `list_projects` | Projects the account can access (id, name, access level). |
| `create_project` | New project from the blank (`none`) or `example` template. |
| `list_files` | Every doc, file and folder in a project, with the main document marked. |
| `read_file` | Current content of a text file (`.tex`, `.bib`, `.cls`, …). |
| `write_file` | Create or overwrite a text file; missing folders are created. |
| `upload_local_file` | Upload a file from local disk, such as a figure, into the project. |
| `move_or_rename` | Move and/or rename a doc, file or folder. |
| `delete_path` | Delete a doc, file or folder (recursively). |
| `compile_project` | Compile, return LaTeX errors and warnings, optionally save the PDF locally. |
| `download_project` | Download the whole project and extract it into a local folder. |

Projects can be referred to by **name** (exact, then case-insensitive) or by their 24-character
**id**. Paths are relative to the project root, e.g. `chapters/intro.tex`.

## Requirements

- Python 3.10+
- A self-hosted Overleaf instance, tested with Community Edition 6.3.0 via
  [overleaf/toolkit](https://github.com/overleaf/toolkit)
- An account on that instance that can sign in with email and password. SSO-only accounts are not
  supported.

## Installation

```bash
git clone https://github.com/<your-username>/overleaf-mcp.git
cd overleaf-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configuration

Put your Overleaf login in `~/.config/overleaf-mcp/.env`, outside the repository:

```bash
install -d -m 700 ~/.config/overleaf-mcp
install -m 600 .env.example ~/.config/overleaf-mcp/.env
$EDITOR ~/.config/overleaf-mcp/.env
```

```ini
OVERLEAF_URL=http://localhost
OVERLEAF_EMAIL=you@example.com
OVERLEAF_PASSWORD=change-me
```

Settings are read in this order, and the first one found wins:

1. Environment variables `OVERLEAF_URL`, `OVERLEAF_EMAIL` and `OVERLEAF_PASSWORD`
2. The file given by `--env-file` or `$OVERLEAF_ENV_FILE`
3. `$XDG_CONFIG_HOME/overleaf-mcp/.env`, which defaults to `~/.config/overleaf-mcp/.env`

## Registering with Claude Code

```bash
claude mcp add --scope user overleaf -- "$PWD/.venv/bin/overleaf-mcp"
claude mcp list   # overleaf: ... - ✔ Connected
```

`--scope user` makes the server available in every directory. Restart Claude Code, or run `/mcp`,
to load the tools.

### Other MCP clients

Any client that launches stdio servers works. For example, Claude Desktop's
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "overleaf": {
      "command": "/absolute/path/to/overleaf-mcp/.venv/bin/overleaf-mcp"
    }
  }
}
```

## How it works

- **Login:** the server reads the CSRF token from `/login` and posts the credentials. It keeps the
  session cookie and the CSRF token for later requests. When the session expires, it signs in
  again automatically.
- **File tree:** the plain HTTP endpoints don't return entity IDs, but the editor's real-time
  service does. The server connects to it over socket.io 0.9 `xhr-polling` and joins the project
  to get the full tree.
- **Writes** use the editor's upload endpoint with `relativePath`. Overleaf creates any missing
  folders and replaces a file that already exists at that path.
- **Compiles** call `/project/:id/compile` and fetch `output.log` (and `output.pdf`, if asked for)
  from the build output. Overleaf allows one compile per project per second, so the server retries
  when it gets `too-recently-compiled`.

## Caveats

- **Writes replace the whole file.** They don't merge with edits made in the browser at the same
  time. Anyone with the file open sees it reload.
- **`status: success` doesn't mean the document is error-free.** Overleaf keeps compiling past
  errors, so check the `errors` list as well.
- **Login rate limits.** Overleaf allows about 10 login attempts per email every 2 minutes. If the
  password is wrong, fix it before retrying.
- **Binary files.** `read_file` returns text only. Use `download_project` to get images and other
  binary files.
- **Untested setups.** It hasn't been tested against overleaf.com, which has CAPTCHA on login, or
  against instances that use SSO only.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -m 'not integration'   # unit tests, no Overleaf needed
.venv/bin/pytest -m integration         # live end-to-end test, see below
```

The integration test reads the same configuration as the server. It creates a throwaway project
named `overleaf-mcp-test-<timestamp>` and runs every tool against it. When it finishes, it moves
the project to the Overleaf trash.

To try the server interactively, use the MCP Inspector:
`npx @modelcontextprotocol/inspector .venv/bin/overleaf-mcp`.
