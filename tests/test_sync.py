"""Tests for jira_sync against an in-memory fake Jira (httpx.MockTransport).

Run:  uv run --with httpx python -m unittest discover -s tests -v
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jira_sync  # noqa: E402


class FakeJira:
    """Minimal Jira Cloud double that records which issue endpoints are requested."""

    def __init__(self):
        self.issues = {}
        self.attachment_bytes = {"900": b"hello"}
        self.requests = []
        self.failing_issue_ids = set()
        for number in (1, 2, 3):
            self.add_issue(str(100 + number), f"PROJ-{number}")
        # PROJ-1 blocks PROJ-2; PROJ-3 is a child of PROJ-1
        self.issues["101"]["fields"]["issuelinks"] = [self._link("501", outward="PROJ-2")]
        self.issues["102"]["fields"]["issuelinks"] = [self._link("501", inward="PROJ-1")]
        self.issues["103"]["fields"]["parent"] = {"id": "101", "key": "PROJ-1"}
        self.issues["102"]["fields"]["attachment"] = [
            {"id": "900", "filename": "a b.txt", "size": 5, "content": "https://jira.test/att/900"}
        ]

    @staticmethod
    def _link(link_id, outward=None, inward=None):
        link = {"id": link_id, "type": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}}
        if outward:
            link["outwardIssue"] = {"key": outward}
        if inward:
            link["inwardIssue"] = {"key": inward}
        return link

    def add_issue(self, issue_id, key):
        self.issues[issue_id] = {
            "id": issue_id,
            "key": key,
            "fields": {"updated": "2026-01-01T00:00:00.000+0000", "issuelinks": [], "attachment": [], "summary": key},
        }

    def touch(self, issue_id, stamp):
        self.issues[issue_id]["fields"]["updated"] = stamp

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/rest/api/3/search/jql":
            listing = [
                {"id": issue["id"], "key": issue["key"], "fields": issue["fields"]}
                for issue in self.issues.values()
            ]
            return httpx.Response(200, json={"issues": listing, "isLast": True})
        if path.startswith("/att/"):
            return httpx.Response(200, content=self.attachment_bytes[path.rsplit("/", 1)[1]])
        if path.startswith("/rest/api/3/issue/"):
            issue_id = path.split("/")[5]
            if issue_id in self.failing_issue_ids:
                return httpx.Response(404, json={})
            suffix = path.split("/")[6] if len(path.split("/")) > 6 else ""
            if suffix == "":
                return httpx.Response(200, json=self.issues[issue_id])
            if suffix == "remotelink":
                return httpx.Response(200, json=[])
            key = {"changelog": "values", "comment": "comments", "worklog": "worklogs"}[suffix]
            return httpx.Response(200, json={key: [], "total": 0, "isLast": True})
        if path.startswith("/rest/agile/1.0/board") and path.endswith("/board"):
            return httpx.Response(200, json={"values": [{"id": 7}], "isLast": True})
        if path.endswith("/configuration"):
            return httpx.Response(200, json={"id": 7})
        if path.endswith("/sprint"):
            return httpx.Response(400, json={})  # kanban-style board
        if path == "/rest/api/3/project/PROJ":
            return httpx.Response(200, json={"key": "PROJ"})
        return httpx.Response(200, json=[])

    def issue_fetches(self):
        return [path for path in self.requests if path.startswith("/rest/api/3/issue/") and path.count("/") == 5]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "PROJ"
        self.output.mkdir()
        self.jira = FakeJira()

    def tearDown(self):
        self.temp.cleanup()

    def run_sync(self, full=False):
        async def run():
            client = jira_sync.JiraClient("https://jira.test", "u", "t", 5, transport=self.jira.transport())
            try:
                await jira_sync.sync_model(client, "PROJ", self.output)
                await jira_sync.sync_issues(client, "PROJ", self.output, full)
            finally:
                await client.close()

        self.jira.requests.clear()
        asyncio.run(run())

    def test_first_run_downloads_everything(self):
        self.run_sync()
        self.assertEqual(len(self.jira.issue_fetches()), 3)
        self.assertEqual(sorted(p.name for p in (self.output / "issues").iterdir()), ["PROJ-1.json", "PROJ-2.json", "PROJ-3.json"])
        attachment = self.output / "attachments" / "PROJ-2" / "900_a b.txt"
        self.assertEqual(attachment.read_bytes(), b"hello")
        self.assertTrue((self.output / "model" / "project.json").exists())

    def test_second_run_without_changes_fetches_nothing(self):
        self.run_sync()
        self.run_sync()
        self.assertEqual(self.jira.issue_fetches(), [])
        self.assertFalse([p for p in self.jira.requests if p.startswith("/att/")])

    def test_edited_issue_refetches_only_that_issue(self):
        self.run_sync()
        self.jira.touch("102", "2026-02-02T00:00:00.000+0000")
        self.run_sync()
        self.assertEqual(self.jira.issue_fetches(), ["/rest/api/3/issue/102"])

    def test_new_issue_fetches_only_the_new_one(self):
        self.run_sync()
        self.jira.add_issue("104", "PROJ-4")
        self.run_sync()
        self.assertEqual(self.jira.issue_fetches(), ["/rest/api/3/issue/104"])

    def test_link_change_is_detected_even_if_updated_is_unchanged(self):
        self.run_sync()
        self.jira.issues["103"]["fields"]["issuelinks"] = [FakeJira._link("502", outward="PROJ-1")]
        self.run_sync()
        self.assertEqual(self.jira.issue_fetches(), ["/rest/api/3/issue/103"])

    def test_deleted_issue_is_flagged_and_file_kept(self):
        self.run_sync()
        del self.jira.issues["103"]
        self.run_sync()
        state = json.loads((self.output / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["103"]["deleted"])
        self.assertTrue((self.output / "issues" / "PROJ-3.json").exists())

    def test_links_and_hierarchy_are_preserved_with_types(self):
        self.run_sync()
        graph = json.loads((self.output / "links.json").read_text(encoding="utf-8"))
        self.assertEqual(len(graph["links"]), 1)
        link = graph["links"][0]
        self.assertEqual((link["inward_issue"], link["type"]["outward"], link["outward_issue"]), ("PROJ-1", "blocks", "PROJ-2"))
        self.assertEqual(graph["hierarchy"], [{"child": "PROJ-3", "parent": "PROJ-1"}])

    def test_interrupted_run_resumes_without_repeating_work(self):
        self.jira.failing_issue_ids = {"102"}
        with self.assertRaises(httpx.HTTPStatusError):
            self.run_sync()
        self.jira.failing_issue_ids = set()
        self.run_sync()
        self.assertEqual(self.jira.issue_fetches(), ["/rest/api/3/issue/102"])

    def test_full_flag_refetches_everything(self):
        self.run_sync()
        self.run_sync(full=True)
        self.assertEqual(len(self.jira.issue_fetches()), 3)


if __name__ == "__main__":
    unittest.main()
