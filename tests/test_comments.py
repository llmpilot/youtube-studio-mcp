import unittest
from unittest import mock

from helpers import make_client, server


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_reply_body(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.reply_to_comment("C1", "Danke für das Feedback!")
        kwargs = req.call_args.kwargs
        self.assertEqual(req.call_args.args[1], "/comments")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["body"], {"snippet": {"parentId": "C1", "textOriginal": "Danke für das Feedback!"}})

    def test_reply_validation_sends_nothing(self):
        with mock.patch.object(self.client, "_request") as req:
            for text in ("", "   ", "x" * 10_001):
                with self.assertRaises(ValueError):
                    self.client.reply_to_comment("C1", text)
        req.assert_not_called()

    def test_list_replies_query_and_clamp(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.list_replies("C1", max_results=999, page_token="T")
        query = req.call_args.kwargs["query"]
        self.assertEqual(req.call_args.args[1], "/comments")
        self.assertEqual((query["parentId"], query["maxResults"], query["pageToken"]), ("C1", 100, "T"))


class DeleteAndModerateTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_delete(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.assertEqual(self.client.delete_comment("C1"), {"deleted": "C1"})
        self.assertEqual(req.call_args.kwargs["method"], "DELETE")
        self.assertEqual(req.call_args.kwargs["query"], {"id": "C1"})

    def test_moderate_batch_in_one_call(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            result = self.client.moderate_comments(["A", "B", "C"], "rejected", ban_author=True)
        self.assertEqual(req.call_count, 1)
        kwargs = req.call_args.kwargs
        self.assertEqual(req.call_args.args[1], "/comments/setModerationStatus")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["query"], {"id": "A,B,C", "moderationStatus": "rejected", "banAuthor": "true"})
        self.assertEqual(kwargs["raw_data"], b"")  # POST needs Content-Length: 0
        self.assertEqual(result["moderated"], ["A", "B", "C"])
        self.assertTrue(result["banned_author"])

    def test_ban_flag_omitted_by_default(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.moderate_comments(["A"], "published")
        self.assertIsNone(req.call_args.kwargs["query"]["banAuthor"])

    def test_moderate_validation_sends_nothing(self):
        bad = [
            ([], "published", False),
            ([""], "published", False),
            (["A"] * 51, "published", False),
            (["A"], "spam", False),
            (["A"], "published", True),   # ban only with rejected
            (["A"], "heldForReview", True),
        ]
        with mock.patch.object(self.client, "_request") as req:
            for ids, status, ban in bad:
                with self.assertRaises(ValueError, msg=(ids[:1], status, ban)):
                    self.client.moderate_comments(ids, status, ban)
        req.assert_not_called()

    def test_body_less_post_sends_empty_data(self):
        """data=b"" (not None) makes urllib add Content-Length: 0 to the body-less POST."""
        captured = {}

        class Empty:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            return Empty()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.client.moderate_comments(["A"], "published")
        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"")


class DispatchTests(unittest.TestCase):
    def test_dispatch(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "reply_to_comment", return_value={}) as reply:
            srv._call_tool("youtube_reply_to_comment", {"comment_id": "C1", "text": "Hi"})
        reply.assert_called_once_with("C1", "Hi")
        with mock.patch.object(srv.youtube, "list_replies", return_value={}) as lr:
            srv._call_tool("youtube_list_replies", {"comment_id": "C1"})
        lr.assert_called_once_with("C1", 20, None)
        with mock.patch.object(srv.youtube, "moderate_comments", return_value={}) as mod:
            srv._call_tool("youtube_moderate_comments", {"comment_ids": ["A"], "status": "rejected"})
        mod.assert_called_once_with(["A"], "rejected", False)
        with mock.patch.object(srv.youtube, "delete_comment", return_value={}) as dele:
            srv._call_tool("youtube_delete_comment", {"comment_id": "C1"})
        dele.assert_called_once_with("C1")


if __name__ == "__main__":
    unittest.main()
