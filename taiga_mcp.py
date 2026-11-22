"""A small Taiga MCP server for Claude Code: list issues, read one with its comments and screenshots, comment, change status.

Runs over stdio, started by Claude Code when needed, so there is no server to keep alive and no open port. The Taiga login comes
from the .env beside this file (TAIGA_BASE_URL, TAIGA_USERNAME, TAIGA_PASSWORD) and is never returned by a tool.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

load_dotenv(Path(__file__).with_name(".env"))
# httpx logs each request's address, and an attachment's carries its access token.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif", ".webp": "webp"}


def _api_root(base: str) -> str:
    base = base.strip().rstrip("/")
    return base if "/api/v" in base else f"{base}/api/v1"


class Taiga:
    """Taiga's REST API with one login, renewed once when Taiga refuses the token."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=_api_root(os.environ["TAIGA_BASE_URL"]), timeout=30.0, transport=transport,
            headers={"Accept": "application/json", "x-disable-pagination": "True"},
        )
        self._token: str | None = None

    async def _login(self) -> None:
        r = await self._client.post("/auth", json={"type": "normal", "username": os.environ["TAIGA_USERNAME"], "password": os.environ["TAIGA_PASSWORD"]})
        if r.status_code >= 400:
            raise RuntimeError(f"Taiga refused the login ({r.status_code}). Check TAIGA_USERNAME and TAIGA_PASSWORD in .env.")
        self._token = r.json()["auth_token"]

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in (1, 2):
            if self._token is None:
                await self._login()
            r = await self._client.request(method, path, headers={"Authorization": f"Bearer {self._token}"}, **kwargs)
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Taiga {method} {path} failed ({r.status_code}): {r.text[:300]}")
            return r.json() if r.content else None
        raise AssertionError("unreachable")

    async def download(self, url: str) -> bytes:
        r = await self._client.get(url, headers={"Authorization": f"Bearer {self._token}"} if self._token else None)
        r.raise_for_status()
        return r.content

    async def project_id(self, project: str) -> int:
        """A project by its id, its slug or its name (case-insensitive)."""
        if project.strip().isdigit():
            return int(project)
        me = (await self.call("GET", "/users/me"))["id"]
        projects = await self.call("GET", "/projects", params={"member": me})
        wanted = project.strip().lower()
        for item in projects:
            if wanted in (item["slug"].lower(), item["name"].lower()):
                return item["id"]
        names = ", ".join(sorted(item["name"] for item in projects))
        raise ValueError(f"No project called {project!r}. Projects: {names}.")

    async def issue(self, project: str, ref: int) -> dict:
        return await self.call("GET", "/issues/by_ref", params={"ref": ref, "project": await self.project_id(project)})

    async def statuses(self, project_id: int) -> list[dict]:
        return await self.call("GET", "/issue-statuses", params={"project": project_id})


_taiga: Taiga | None = None


def taiga() -> Taiga:
    global _taiga  # one client per process: Claude Code starts one server per session
    if _taiga is None:
        _taiga = Taiga()
    return _taiga


def _person(info: dict | None) -> str:
    return (info or {}).get("full_name_display") or "unassigned"


mcp = FastMCP("taiga")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def list_issues(project: str, status: str = "open", assigned_to: str | None = None, search: str | None = None) -> str:
    """List a project's issues, newest number first.

    project: its slug, name or id (e.g. "website-redesign" or "Website Redesign").
    status: "open" (every status Taiga does not count as closed), "closed", "all", or one status's name such as "Ready for test".
    assigned_to: part of the assignee's name, or "unassigned".
    search: words that must appear in the subject.
    """
    t = taiga()
    pid = await t.project_id(project)
    statuses = await t.statuses(pid)
    closed = {s["id"] for s in statuses if s["is_closed"]}
    wanted = status.strip().lower()
    named = {s["id"] for s in statuses if s["name"].lower() == wanted}
    if wanted not in ("open", "closed", "all") and not named:
        raise ValueError(f"No status {status!r}. Statuses: {', '.join(s['name'] for s in statuses)}.")
    issues = await t.call("GET", "/issues", params={"project": pid})

    def keep(issue: dict) -> bool:
        if wanted == "open" and issue["status"] in closed:
            return False
        if wanted == "closed" and issue["status"] not in closed:
            return False
        if named and issue["status"] not in named:
            return False
        if assigned_to and assigned_to.strip().lower() not in _person(issue.get("assigned_to_extra_info")).lower():
            return False
        return not search or search.strip().lower() in issue["subject"].lower()

    rows = sorted((i for i in issues if keep(i)), key=lambda i: i["ref"], reverse=True)
    lines = [f"{len(rows)} of {len(issues)} issues ({status})."]
    for i in rows:
        lines.append(
            f"#{i['ref']} [{(i.get('status_extra_info') or {}).get('name')}] {i['subject']} | {_person(i.get('assigned_to_extra_info'))}"
            f" | by {_person(i.get('owner_extra_info'))} | updated {i['modified_date'][:10]}"
        )
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def get_issue(project: str, ref: int) -> list:
    """Read one issue by its number (#42 is ref 42): its details, description, every comment, and its image attachments as images."""
    t = taiga()
    i = await t.issue(project, ref)
    history = await t.call("GET", f"/history/issue/{i['id']}")
    attachments = await t.call("GET", "/issues/attachments", params={"object_id": i["id"], "project": i["project"]})
    tags = ", ".join(tag[0] if isinstance(tag, list) else tag for tag in i.get("tags") or []) or "none"
    lines = [
        f"#{i['ref']} {i['subject']}",
        f"Status: {(i.get('status_extra_info') or {}).get('name')} | Assigned: {_person(i.get('assigned_to_extra_info'))}"
        f" | Reported by: {_person(i.get('owner_extra_info'))} | Created: {i['created_date'][:16]} UTC | Updated: {i['modified_date'][:16]} UTC",
        f"Tags: {tags}",
        "", "Description:", (i.get("description") or "").strip() or "(empty)", "",
    ]
    comments = sorted((h for h in history if h.get("comment") and not h.get("delete_comment_date")), key=lambda h: h["created_at"])
    lines.append(f"Comments ({len(comments)}, oldest first):")
    lines += [f"- {h['created_at'][:16]} UTC, {h['user']['name']}: {h['comment'].strip()}" for h in comments]
    lines += ["", f"Attachments ({len(attachments)}):"]
    images: list = []
    for a in attachments:
        lines.append(f"- {a['name']} ({a.get('size', 0)} bytes)")
        kind = IMAGE_TYPES.get(Path(a["name"]).suffix.lower())
        if kind and len(images) < MAX_IMAGES and a.get("size", 0) <= MAX_IMAGE_BYTES:
            images.append(Image(data=await t.download(a["url"]), format=kind))
    return ["\n".join(lines), *images]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def add_comment(project: str, ref: int, comment: str) -> str:
    """Post a comment on an issue, under the Taiga account in .env. Changes nothing else on the issue. Posts exactly the text given."""
    if not comment.strip():
        raise ValueError("The comment is empty.")
    t = taiga()
    i = await t.issue(project, ref)
    await t.call("PATCH", f"/issues/{i['id']}", json={"comment": comment, "version": i["version"]})
    return f"Commented on #{i['ref']} {i['subject']}."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def set_status(project: str, ref: int, status: str) -> str:
    """Move an issue to another status, by the status's name as Taiga shows it (e.g. "Ready for test", "Closed")."""
    t = taiga()
    i = await t.issue(project, ref)
    statuses = await t.statuses(i["project"])
    match = next((s for s in statuses if s["name"].lower() == status.strip().lower()), None)
    if match is None:
        raise ValueError(f"No status {status!r}. Statuses: {', '.join(s['name'] for s in statuses)}.")
    before = (i.get("status_extra_info") or {}).get("name")
    if match["id"] == i["status"]:
        return f"#{i['ref']} is already {before}."
    await t.call("PATCH", f"/issues/{i['id']}", json={"status": match["id"], "version": i["version"]})
    return f"Moved #{i['ref']} from {before} to {match['name']}."


if __name__ == "__main__":
    mcp.run()
