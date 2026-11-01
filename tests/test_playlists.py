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


if __name__ == "__main__":
    unittest.main()
