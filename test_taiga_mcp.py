"""The tools against a fake Taiga: what each sends, and that a stale login is renewed once."""

import json
from types import SimpleNamespace

import httpx
import pytest

import taiga_mcp

STATUSES = [{"id": 1, "name": "New", "is_closed": False}, {"id": 2, "name": "Ready for test", "is_closed": False}, {"id": 3, "name": "Closed", "is_closed": True}]
ISSUE = {
    "id": 1028, "ref": 42, "project": 22, "subject": "Mic missing", "status": 1, "version": 7, "status_extra_info": {"name": "New"},
    "assigned_to": None, "created_date": "2026-11-20T09:15:00Z", "modified_date": "2026-11-21T10:00:00Z",
}
MEMBERS = [
    {"user": 5, "full_name": "John Carver"}, {"user": 6, "full_name": "Adam Hollis"}, {"user": 7, "full_name": "Adam Brierley"},
    {"user": None, "full_name": "Sam Invited"}, {"user": 8, "full_name": "Adam Quist", "is_user_active": False},
    {"user": 9, "full_name": "Ann Lee"}, {"user": 10, "full_name": "Ann Leeson"},
]
ISSUES = [
    {**ISSUE, "ref": 41, "subject": "Login slow", "assigned_to_extra_info": {"full_name_display": "Ned Ashby"}},
    {**ISSUE, "ref": 40, "subject": "Typo on home"},
]
ATTACHMENTS = [
    {"name": "gone.png", "size": 10, "url": "https://media.example.com/attachments/gone.png?token=abc"},
    {"name": "mic.png", "size": None, "url": "https://media.example.com/attachments/mic.png?token=abc"},
]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("TAIGA_BASE_URL", "https://taiga.example.com")
    monkeypatch.setenv("TAIGA_USERNAME", "john")
    monkeypatch.setenv("TAIGA_PASSWORD", "secret")
    state = SimpleNamespace(sent=[], logins=0, expire_first=False, media_auth=[])

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "media.example.com":
            state.media_auth.append(request.headers.get("Authorization"))
            return httpx.Response(404) if request.url.path.endswith("gone.png") else httpx.Response(200, content=b"\x89PNG")
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        if path == "/auth":
            state.logins += 1
            return httpx.Response(200, json={"auth_token": f"t{state.logins}"})
        if request.headers.get("Authorization") == "Bearer t1" and state.expire_first:
            state.expire_first = False
            return httpx.Response(401, json={})
        state.sent.append((request.method, path, body))
        if path == "/issues/by_ref":
            return httpx.Response(200, json=ISSUE)
        if path == "/issues" and request.method == "GET":
            return httpx.Response(200, json=ISSUES)
        if path == "/history/issue/1028":
            return httpx.Response(200, json=[])
        if path == "/issues/attachments":
            return httpx.Response(200, json=ATTACHMENTS)
        if path == "/memberships":
            return httpx.Response(200, json=MEMBERS)
        if path == "/issue-statuses":
            return httpx.Response(200, json=STATUSES)
        if path == "/issues/1028" and request.method == "PATCH":
            return httpx.Response(200, json={**ISSUE, "version": 8})
        return httpx.Response(404, json={})

    monkeypatch.setattr(taiga_mcp, "_taiga", taiga_mcp.Taiga(transport=httpx.MockTransport(handle)))
    return state


def patches(fake):
    return [s for s in fake.sent if s[0] == "PATCH"]


async def test_a_comment_is_sent_exactly_with_the_issues_version_and_nothing_else(fake):
    assert await taiga_mcp.add_comment("22", 42, "Checked on dev.\n\nAttachments work.") == "Commented on #42 Mic missing."
    assert fake.sent[-1] == ("PATCH", "/issues/1028", {"comment": "Checked on dev.\n\nAttachments work.", "version": 7})


async def test_an_empty_comment_sends_nothing(fake):
    with pytest.raises(ValueError):
        await taiga_mcp.add_comment("22", 42, "  \n")
    assert fake.sent == []


async def test_a_status_is_found_by_name_and_an_unknown_one_lists_the_real_ones(fake):
    assert await taiga_mcp.set_status("22", 42, "ready for TEST") == "Moved #42 from New to Ready for test."
    assert fake.sent[-1] == ("PATCH", "/issues/1028", {"status": 2, "version": 7})
    with pytest.raises(ValueError, match="New, Ready for test, Closed"):
        await taiga_mcp.set_status("22", 42, "Done")
    assert await taiga_mcp.set_status("22", 42, "New") == "#42 is already New."
    assert patches(fake) == [("PATCH", "/issues/1028", {"status": 2, "version": 7})]


async def test_a_refused_token_is_renewed_once(fake):
    fake.expire_first = True
    await taiga_mcp.add_comment("22", 42, "Hello")
    assert fake.logins == 2 and fake.sent[-1][0] == "PATCH"


async def test_an_issue_is_assigned_by_part_of_a_name_and_two_matches_are_refused(fake):
    assert await taiga_mcp.assign_issue("22", 42, "john") == "Assigned #42 to John Carver (was unassigned)."
    assert fake.sent[-1] == ("PATCH", "/issues/1028", {"assigned_to": 5, "version": 7})
    with pytest.raises(ValueError, match=r"matches 2 members: Adam Brierley, Adam Hollis\."):
        await taiga_mcp.assign_issue("22", 42, "Adam")  # Adam Quist's account is deactivated
    with pytest.raises(ValueError, match="No member matches 'Sam'"):
        await taiga_mcp.assign_issue("22", 42, "Sam")  # an invitation not yet accepted is no one to assign
    with pytest.raises(ValueError, match="No member matches 'Quist'"):
        await taiga_mcp.assign_issue("22", 42, "Quist")
    with pytest.raises(ValueError, match="No name given"):
        await taiga_mcp.assign_issue("22", 42, "  ")
    assert await taiga_mcp.assign_issue("22", 42, "unassigned") == "#42 is already unassigned."
    assert patches(fake) == [("PATCH", "/issues/1028", {"assigned_to": 5, "version": 7})]


async def test_an_exact_name_wins_over_a_longer_one_containing_it(fake):
    assert await taiga_mcp.assign_issue("22", 42, "ann lee") == "Assigned #42 to Ann Lee (was unassigned)."
    assert fake.sent[-1] == ("PATCH", "/issues/1028", {"assigned_to": 9, "version": 7})


async def test_an_assignee_is_taken_off(fake, monkeypatch):
    monkeypatch.setitem(ISSUE, "assigned_to", 5)
    monkeypatch.setitem(ISSUE, "assigned_to_extra_info", {"full_name_display": "John Carver"})
    assert await taiga_mcp.assign_issue("22", 42, "Nobody") == "Unassigned #42 (was John Carver)."
    assert fake.sent[-1] == ("PATCH", "/issues/1028", {"assigned_to": None, "version": 7})


async def test_an_assignee_filter_matches_names_only_and_unassigned_exactly(fake):
    ned = await taiga_mcp.list_issues("22", assigned_to="ned")  # "ned" is also the end of "unassigned"
    assert ned.splitlines()[0] == "1 of 2 issues (open)." and "#41" in ned
    nobody = await taiga_mcp.list_issues("22", assigned_to="unassigned")
    assert nobody.splitlines()[0] == "1 of 2 issues (open)." and "#40" in nobody


async def test_an_attachment_that_cannot_be_fetched_is_noted_and_the_token_stays_on_taiga(fake):
    text, image = await taiga_mcp.get_issue("22", 42)
    assert "- gone.png (10 bytes) (could not be loaded)" in text
    assert "- mic.png (0 bytes)" in text and image.data == b"\x89PNG"
    assert fake.media_auth == [None, None]
