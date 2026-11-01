import unittest
from unittest import mock

from helpers import make_client, server


def item(video_id):
    return {"id": video_id, "snippet": {"title": f"Titel {video_id}"}, "statistics": {"viewCount": "1"}}


class GetVideosTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def fetch(self, ids, parts=None, api_items=None):
        items = api_items if api_items is not None else [item(i) for i in ids]
        with mock.patch.object(self.client, "_request", return_value={"items": items}) as req:
            result = self.client.get_videos(ids, parts)
        return result, req

    def test_one_call_for_many_videos(self):
        result, req = self.fetch(["a", "b", "c"])
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.args[1], "/videos")
        query = req.call_args.kwargs["query"]
        self.assertEqual(query["id"], "a,b,c")
        self.assertEqual(query["part"], "snippet,statistics,status,contentDetails")
        self.assertEqual([v["id"] for v in result["items"]], ["a", "b", "c"])
        self.assertEqual(result["not_found"], [])

    def test_order_follows_the_request_not_the_api(self):
        result, _ = self.fetch(["c", "a", "b"], api_items=[item("a"), item("b"), item("c")])
        self.assertEqual([v["id"] for v in result["items"]], ["c", "a", "b"])

    def test_unknown_ids_are_reported(self):
        result, _ = self.fetch(["a", "ghost", "b"], api_items=[item("b"), item("a")])
        self.assertEqual([v["id"] for v in result["items"]], ["a", "b"])
        self.assertEqual(result["not_found"], ["ghost"])

    def test_all_unknown(self):
        result, _ = self.fetch(["x", "y"], api_items=[])
        self.assertEqual((result["items"], result["not_found"]), ([], ["x", "y"]))

    def test_duplicates_are_sent_once(self):
        result, req = self.fetch(["a", "b", "a", " a "])
        self.assertEqual(req.call_args.kwargs["query"]["id"], "a,b")
        self.assertEqual(len(result["items"]), 2)

    def test_comma_string_is_accepted(self):
        _, req = self.fetch("a, b ,c")
        self.assertEqual(req.call_args.kwargs["query"]["id"], "a,b,c")

    def test_parts_limit_the_data(self):
        _, req = self.fetch(["a"], parts=["statistics", "statistics"])
        self.assertEqual(req.call_args.kwargs["query"]["part"], "statistics")
        _, req = self.fetch(["a"], parts="snippet")
        self.assertEqual(req.call_args.kwargs["query"]["part"], "snippet")

    def test_fifty_is_allowed_fifty_one_is_not(self):
        ids = [f"v{i}" for i in range(50)]
        result, _ = self.fetch(ids)
        self.assertEqual(len(result["items"]), 50)
        with mock.patch.object(self.client, "_request") as req:
            with self.assertRaisesRegex(ValueError, "At most 50"):
                self.client.get_videos(ids + ["extra"])
        req.assert_not_called()

    def test_invalid_input_sends_nothing(self):
        bad = [
            ([], None), ("", None), ([" "], None), (None, None), (5, None), ([1, 2], None),
            (["a b"], None), (["a"], []), (["a"], ["secrets"]), (["a"], 5),
        ]
        with mock.patch.object(self.client, "_request") as req:
            for ids, parts in bad:
                with self.assertRaises(ValueError, msg=(ids, parts)):
                    self.client.get_videos(ids, parts)
        req.assert_not_called()

    def test_costs_one_unit_for_fifty_videos(self):
        self.client.quota = server.QuotaTracker(None)
        ids = [f"v{i}" for i in range(50)]
        with mock.patch.object(server, "http_json", return_value={"items": [item(i) for i in ids]}):
            self.client.get_videos(ids)
        self.assertEqual(self.client.quota.used, 1)

    def test_tool(self):
        srv = server.McpServer()
        tool = {t["name"]: t for t in srv.tools}["youtube_get_videos"]
        self.assertEqual(tool["inputSchema"]["required"], ["video_ids"])
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertNotIn("dry_run", tool["inputSchema"]["properties"])
        with mock.patch.object(srv.youtube, "get_videos", return_value={}) as gv:
            srv._call_tool("youtube_get_videos", {"video_ids": ["a"], "parts": ["status"]})
        gv.assert_called_once_with(["a"], ["status"])


if __name__ == "__main__":
    unittest.main()
