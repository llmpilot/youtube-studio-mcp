import unittest
from unittest import mock

from helpers import make_client, server


class ReadPlaylistTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_list_playlists_query(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}) as req:
            self.client.list_playlists(max_results=500, page_token="P")
        self.assertEqual(req.call_args.args[1], "/playlists")
        query = req.call_args.kwargs["query"]
        self.assertEqual(query["mine"], "true")
        self.assertEqual(query["maxResults"], 50)
        self.assertEqual(query["pageToken"], "P")
        self.assertIn("contentDetails", query["part"])

    def test_list_playlist_items_query(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.list_playlist_items("PL1", max_results=0)
        self.assertEqual(req.call_args.args[1], "/playlistItems")
        query = req.call_args.kwargs["query"]
        self.assertEqual(query["playlistId"], "PL1")
        self.assertEqual(query["maxResults"], 1)

    def test_dispatch_defaults(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "list_playlists", return_value={}) as lp:
            srv._call_tool("youtube_list_playlists", {})
        lp.assert_called_once_with(25, None)
        with mock.patch.object(srv.youtube, "list_playlist_items", return_value={}) as li:
            srv._call_tool("youtube_list_playlist_items", {"playlist_id": "PL1"})
        li.assert_called_once_with("PL1", 50, None)


class WritePlaylistTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_create_defaults_to_private(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.create_playlist("ETF-Serie")
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["body"]["status"], {"privacyStatus": "private"})
        self.assertEqual(kwargs["body"]["snippet"], {"title": "ETF-Serie", "description": ""})

    def test_create_with_language(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.create_playlist("T", description="D", privacy_status="public", default_language="de")
        body = req.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["defaultLanguage"], "de")
        self.assertEqual(body["status"]["privacyStatus"], "public")

    def test_create_validation_sends_nothing(self):
        with mock.patch.object(self.client, "_request") as req:
            for kwargs in ({"title": " "}, {"title": "x" * 151}, {"title": "T", "privacy_status": "secret"}):
                with self.assertRaises(ValueError):
                    self.client.create_playlist(**kwargs)
        req.assert_not_called()

    def existing(self):
        return {"snippet": {"title": "Alt", "description": "Beschr", "defaultLanguage": "de", "tags": ["a"]},
                "status": {"privacyStatus": "public"}}

    def test_update_changes_only_given_fields(self):
        with mock.patch.object(self.client, "get_playlist", return_value=self.existing()), \
                mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_playlist("PL1", title="Neu")
        body = req.call_args.kwargs["body"]
        self.assertEqual(req.call_args.kwargs["method"], "PUT")
        self.assertEqual(body["id"], "PL1")
        self.assertEqual(body["snippet"], {"title": "Neu", "description": "Beschr",
                                           "defaultLanguage": "de", "tags": ["a"]})
        self.assertEqual(body["status"], {"privacyStatus": "public"})

    def test_update_privacy(self):
        with mock.patch.object(self.client, "get_playlist", return_value=self.existing()), \
                mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_playlist("PL1", privacy_status="unlisted")
        self.assertEqual(req.call_args.kwargs["body"]["status"], {"privacyStatus": "unlisted"})

    def test_update_needs_a_change_and_validates_before_lookup(self):
        with mock.patch.object(self.client, "get_playlist") as get:
            with self.assertRaisesRegex(ValueError, "at least one"):
                self.client.update_playlist("PL1")
            with self.assertRaisesRegex(ValueError, "privacy_status"):
                self.client.update_playlist("PL1", privacy_status="secret")
        get.assert_not_called()

    def test_get_playlist_not_found(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.client.get_playlist("nope")

    def test_delete(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.assertEqual(self.client.delete_playlist("PL1"), {"deleted": "PL1"})
        self.assertEqual(req.call_args.kwargs["method"], "DELETE")
        self.assertEqual(req.call_args.kwargs["query"], {"id": "PL1"})

    def test_dispatch(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "create_playlist", return_value={}) as c:
            srv._call_tool("youtube_create_playlist", {"title": "T"})
        c.assert_called_once_with("T", description="", privacy_status="private", default_language=None)
        with mock.patch.object(srv.youtube, "delete_playlist", return_value={}) as d:
            srv._call_tool("youtube_delete_playlist", {"playlist_id": "PL1"})
        d.assert_called_once_with("PL1")


class PlaylistItemTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_add_body(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.add_playlist_item("PL1", "vid", position=0)
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["body"]["snippet"], {
            "playlistId": "PL1",
            "resourceId": {"kind": "youtube#video", "videoId": "vid"},
            "position": 0,
        })

    def test_add_without_position_appends(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.add_playlist_item("PL1", "vid")
        self.assertNotIn("position", req.call_args.kwargs["body"]["snippet"])

    def test_negative_position_rejected_without_request(self):
        with mock.patch.object(self.client, "_request") as req:
            with self.assertRaisesRegex(ValueError, "position"):
                self.client.add_playlist_item("PL1", "vid", position=-1)
            with self.assertRaisesRegex(ValueError, "position"):
                self.client.move_playlist_item("I1", -2)
        req.assert_not_called()

    def test_remove(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.assertEqual(self.client.remove_playlist_item("I1"), {"removed": "I1"})
        self.assertEqual(req.call_args.kwargs["method"], "DELETE")
        self.assertEqual(req.call_args.kwargs["query"], {"id": "I1"})

    def test_move_keeps_playlist_and_resource(self):
        found = {"items": [{"snippet": {"playlistId": "PL1", "position": 5, "title": "x",
                                         "resourceId": {"kind": "youtube#video", "videoId": "vid"}}}]}
        with mock.patch.object(self.client, "_request", side_effect=[found, {}]) as req:
            self.client.move_playlist_item("I1", 0)
        put = req.call_args_list[1].kwargs
        self.assertEqual(put["method"], "PUT")
        self.assertEqual(put["body"], {"id": "I1", "snippet": {
            "playlistId": "PL1",
            "resourceId": {"kind": "youtube#video", "videoId": "vid"},
            "position": 0,
        }})

    def test_move_unknown_item(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.client.move_playlist_item("nope", 1)

    def test_dispatch(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "add_playlist_item", return_value={}) as add:
            srv._call_tool("youtube_add_playlist_item", {"playlist_id": "PL1", "video_id": "v"})
        add.assert_called_once_with("PL1", "v", None)
        with mock.patch.object(srv.youtube, "move_playlist_item", return_value={}) as mv:
            srv._call_tool("youtube_move_playlist_item", {"item_id": "I1", "position": 2})
        mv.assert_called_once_with("I1", 2)


if __name__ == "__main__":
    unittest.main()
