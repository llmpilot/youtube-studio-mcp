import copy
import unittest
from unittest import mock

from helpers import make_client, server

VIDEO = {
    "id": "vid",
    "snippet": {"title": "ETF Fehler Nr. 1", "publishedAt": "2026-10-20T16:00:00Z"},
    "status": {"privacyStatus": "public"},
    "statistics": {"viewCount": "1234", "commentCount": "17"},
}


class FakeApi:
    def __init__(self):
        self.calls = []

    def http_json(self, url, *, method="GET", headers=None, data=None):
        self.calls.append((method, url))
        return {"items": [copy.deepcopy(VIDEO)]} if method == "GET" else {}

    @property
    def deletes(self):
        return [c for c in self.calls if c[0] == "DELETE"]


class DeleteVideoTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.client.quota = server.QuotaTracker(None)
        self.api = FakeApi()
        patcher = mock.patch.object(server, "http_json", self.api.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deletes_when_title_matches(self):
        result = self.client.delete_video("vid", "ETF Fehler Nr. 1")
        self.assertEqual(result, {"deleted": "vid", "title": "ETF Fehler Nr. 1"})
        self.assertEqual(len(self.api.deletes), 1)
        self.assertIn("id=vid", self.api.deletes[0][1])
        self.assertTrue(self.api.deletes[0][1].startswith(server.YOUTUBE_API_BASE + "/videos?"))

    def test_surrounding_whitespace_is_tolerated(self):
        self.client.delete_video("vid", "  ETF Fehler Nr. 1 ")
        self.assertEqual(len(self.api.deletes), 1)

    def test_missing_or_wrong_title_deletes_nothing_and_shows_the_real_title(self):
        for confirm in (None, "", "etf fehler nr. 1", "Anderes Video"):
            with self.assertRaises(ValueError, msg=confirm) as ctx:
                self.client.delete_video("vid", confirm)
            self.assertIn("ETF Fehler Nr. 1", str(ctx.exception))
            self.assertIn("Not deleted", str(ctx.exception))
        self.assertEqual(self.api.deletes, [])

    def test_unknown_video(self):
        with mock.patch.object(server, "http_json", return_value={"items": []}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.client.delete_video("nope", "x")

    def test_quota_is_lookup_plus_delete(self):
        self.client.delete_video("vid", "ETF Fehler Nr. 1")
        self.assertEqual(self.client.quota.used, 51)

    def test_failed_delete_is_not_reported_as_success(self):
        def fail(url, *, method="GET", headers=None, data=None):
            if method == "DELETE":
                raise server.ApiError(403, "forbidden", "forbidden")
            return {"items": [copy.deepcopy(VIDEO)]}
        with mock.patch.object(server, "http_json", fail):
            with self.assertRaises(server.ApiError):
                self.client.delete_video("vid", "ETF Fehler Nr. 1")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.srv = server.McpServer()
        self.srv.youtube._access_token = lambda: "t"
        self.srv.youtube.quota = server.QuotaTracker(None)
        self.api = FakeApi()

    def call(self, arguments):
        with mock.patch.object(server, "http_json", self.api.http_json):
            return self.srv._call_tool("youtube_delete_video", arguments)

    def test_dry_run_needs_no_confirmation_and_shows_the_target(self):
        result = self.call({"video_id": "vid", "dry_run": True})
        self.assertEqual(self.api.deletes, [])
        target = result["targets"][0]
        self.assertEqual(target["title"], "ETF Fehler Nr. 1")
        self.assertEqual((target["viewCount"], target["commentCount"], target["privacyStatus"]),
                         ("1234", "17", "public"))
        self.assertEqual(result["planned_requests"][0]["method"], "DELETE")
        self.assertEqual(result["quota_cost_estimate"], 50)

    def test_real_call_without_confirmation_is_a_tool_error_over_the_wire(self):
        from helpers import INIT, ndjson, parse_ndjson, run_stdio
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "youtube_delete_video", "arguments": {"video_id": "vid"}}}
        with mock.patch.object(server, "http_json", self.api.http_json), \
                mock.patch.object(server.YouTubeClient, "_access_token", lambda self: "t"):
            reply = parse_ndjson(run_stdio(ndjson(INIT, call)))[1]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("confirm_title", reply["result"]["content"][0]["text"])
        self.assertEqual(self.api.deletes, [])

    def test_schema_and_annotations(self):
        tool = {t["name"]: t for t in self.srv.tools}["youtube_delete_video"]
        self.assertEqual(tool["inputSchema"]["required"], ["video_id"])
        self.assertIn("confirm_title", tool["inputSchema"]["properties"])
        self.assertIn("dry_run", tool["inputSchema"]["properties"])
        self.assertTrue(tool["annotations"]["destructiveHint"])
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertIn("PERMANENTLY", tool["description"])

    def test_dispatch(self):
        with mock.patch.object(self.srv.youtube, "delete_video", return_value={}) as dele:
            self.srv._call_tool("youtube_delete_video", {"video_id": "vid", "confirm_title": "T"})
        dele.assert_called_once_with("vid", "T")


if __name__ == "__main__":
    unittest.main()
