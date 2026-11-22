# taiga-mcp-lite

A small [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude Code, Claude Desktop, or any MCP client) work with issues in a [Taiga](https://taiga.io) project, cloud or self-hosted.

It runs over stdio: the client starts it when needed, so there is no server to keep running and no open port.

## Tools

| Tool | What it does | Writes |
|---|---|---|
| `list_issues` | A project's issues, filtered by `open`, `closed`, `all` or a status name, an assignee, or words in the title | No |
| `get_issue` | One issue by its number: details, description, every comment, and its image attachments as images | No |
| `add_comment` | Posts a comment, exactly as given; changes nothing else on the issue | Yes |
| `set_status` | Moves an issue to a status by its name ("Ready for test") | Yes |
| `assign_issue` | Assigns an issue to a project member found by part of their name, or `unassigned`; two matching members are refused | Yes |

A project can be named by its slug, its name or its id.

## Set up

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12 or later.

```sh
git clone <this repo> && cd taiga-mcp-lite
uv sync
```

Create `.env` beside `taiga_mcp.py` (it is git-ignored; keep it private with `chmod 600 .env`):

```sh
TAIGA_BASE_URL=https://taiga.example.com   # the site, or its /api/v1
TAIGA_USERNAME=your-username
TAIGA_PASSWORD=your-password
```

A Taiga account of its own, with access only to the projects it needs, is the safer choice: the write tools act as that account.

## Add it to Claude Code

```sh
claude mcp add --scope user taiga -- "$PWD/.venv/bin/python" "$PWD/taiga_mcp.py"
```

To be asked every time before a comment, status change or assignment, add the write tools to the `ask` list in `~/.claude/settings.json`:

```json
{ "permissions": { "ask": ["mcp__taiga__add_comment", "mcp__taiga__set_status", "mcp__taiga__assign_issue"] } }
```

## Test

```sh
uv run pytest
```

The tests run the write tools against a fake Taiga; they make no network call.

## Licence

MIT. See [LICENSE](LICENSE).
