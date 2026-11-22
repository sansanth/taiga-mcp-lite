"""The write tools against a fake Taiga: what each sends, and that a stale login is renewed once."""

import json

import httpx
import pytest

import taiga_mcp

STATUSES = [{"id": 1, "name": "New", "is_closed": False}, {"id": 2, "name": "Ready for test", "is_closed": False}, {"id": 3, "name": "Closed", "is_closed": True}]
ISSUE = {"id": 1028, "ref": 42, "project": 22, "subject": "Mic missing", "status": 1, "version": 7, "status_extra_info": {"name": "New"}}


@pytest.fixture
def taiga(monkeypatch):
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example.com")
    monkeypatch.setenv("TAIGA_USERNAME", "john")
    monkeypatch.setenv("TAIGA_PASSWORD", "secret")
    sent: list[tuple[str, str, dict | None]] = []
    logins = {"count": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        if path == "/auth":
            logins["count"] += 1
            return httpx.Response(200, json={"auth_token": f"t{logins['count']}"})
        if request.headers["Authorization"] == "Bearer t1" and logins.get("expire_first"):
            logins["expire_first"] = False
            return httpx.Response(401, json={})
        sent.append((request.method, path, body))
        if path == "/issues/by_ref":
            return httpx.Response(200, json=ISSUE)
        if path == "/issue-statuses":
            return httpx.Response(200, json=STATUSES)
        if path == "/issues/1028" and request.method == "PATCH":
            return httpx.Response(200, json={**ISSUE, "version": 8})
        return httpx.Response(404, json={})

    client = taiga_mcp.Taiga(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(taiga_mcp, "_taiga", client)
    return sent, logins


async def test_a_comment_is_sent_exactly_with_the_issues_version_and_nothing_else(taiga):
    sent, _ = taiga
    assert await taiga_mcp.add_comment("22", 42, "Checked on dev.\n\nAttachments work.") == "Commented on #42 Mic missing."
    assert sent[-1] == ("PATCH", "/issues/1028", {"comment": "Checked on dev.\n\nAttachments work.", "version": 7})


async def test_an_empty_comment_sends_nothing(taiga):
    sent, _ = taiga
    with pytest.raises(ValueError):
        await taiga_mcp.add_comment("22", 42, "  \n")
    assert sent == []


async def test_a_status_is_found_by_name_and_an_unknown_one_lists_the_real_ones(taiga):
    sent, _ = taiga
    assert await taiga_mcp.set_status("22", 42, "ready for TEST") == "Moved #42 from New to Ready for test."
    assert sent[-1] == ("PATCH", "/issues/1028", {"status": 2, "version": 7})
    with pytest.raises(ValueError, match="New, Ready for test, Closed"):
        await taiga_mcp.set_status("22", 42, "Done")
    assert await taiga_mcp.set_status("22", 42, "New") == "#42 is already New."
    assert [s for s in sent if s[0] == "PATCH"] == [("PATCH", "/issues/1028", {"status": 2, "version": 7})]


async def test_a_refused_token_is_renewed_once(taiga):
    sent, logins = taiga
    logins["expire_first"] = True
    await taiga_mcp.add_comment("22", 42, "Hello")
    assert logins["count"] == 2 and sent[-1][0] == "PATCH"
