import io
import json
import unittest
import urllib.error
from unittest import mock

from helpers import make_client, server


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_none_query_values_are_dropped(self):
        with mock.patch.object(server, "http_json", return_value={}) as http:
            self.client._request(server.YOUTUBE_API_BASE, "/videos", query={"id": "a", "pageToken": None})
        url = http.call_args.args[0]
        self.assertIn("id=a", url)
        self.assertNotIn("pageToken", url)

    def test_bearer_header_and_json_body(self):
        with mock.patch.object(server, "http_json", return_value={}) as http:
            self.client._request(server.YOUTUBE_API_BASE, "/x", method="POST", body={"a": 1})
        kwargs = http.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(kwargs["data"]), {"a": 1})

    def test_http_error_message_is_extracted(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(403, '{"error":{"message":"quotaExceeded"}}')):
            with self.assertRaisesRegex(RuntimeError, "quotaExceeded"):
                server.http_json("http://x")

    def test_http_error_with_non_json_body(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(502, "Bad Gateway")):
            with self.assertRaisesRegex(RuntimeError, "Bad Gateway"):
                server.http_json("http://x")


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_get_video_not_found(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                self.client.get_video("abc")

    def test_update_video_changes_only_given_fields(self):
        existing = {
            "snippet": {"title": "Alt", "description": "Beschreibung", "tags": ["a"], "categoryId": "22"},
            "status": {"privacyStatus": "private", "madeForKids": False},
        }
        with mock.patch.object(self.client, "get_video", return_value=existing), \
                mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_video("vid", title="Neu", privacy_status="public")
        body = req.call_args.kwargs["body"]
        self.assertEqual(body["id"], "vid")
        self.assertEqual(body["snippet"]["title"], "Neu")
        self.assertEqual(body["snippet"]["description"], "Beschreibung")
        self.assertEqual(body["snippet"]["tags"], ["a"])
        self.assertEqual(body["status"], {"privacyStatus": "public", "madeForKids": False})
        self.assertEqual(req.call_args.kwargs["method"], "PUT")

    def test_list_videos_merges_details_and_clamps_page_size(self):
        responses = [
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]},
            {"items": [{"contentDetails": {"videoId": "v1"}}], "nextPageToken": "N"},
            {"items": [{"id": "v1", "snippet": {"title": "T"}}]},
        ]
        with mock.patch.object(self.client, "_request", side_effect=responses) as req:
            result = self.client.list_videos(max_results=500)
        self.assertEqual(req.call_args_list[1].kwargs["query"]["maxResults"], 25)
        self.assertEqual(result["nextPageToken"], "N")
        self.assertEqual(result["items"][0]["details"]["snippet"]["title"], "T")

    def test_list_videos_without_channel(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}):
            with self.assertRaisesRegex(RuntimeError, "No authenticated"):
                self.client.list_videos()


class ThumbnailCommentAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_thumbnail_missing_file(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.client.upload_thumbnail("vid", "Z:/does/not/exist.png")

    def test_thumbnail_upload_uses_mime_type(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            image = pathlib.Path(tmp) / "t.png"
            image.write_bytes(b"\x89PNG")
            with mock.patch.object(self.client, "_request", return_value={}) as req:
                self.client.upload_thumbnail("vid", str(image))
        self.assertEqual(req.call_args.kwargs["headers"], {"Content-Type": "image/png"})
        self.assertEqual(req.call_args.kwargs["raw_data"], b"\x89PNG")
        self.assertEqual(req.call_args.kwargs["query"], {"videoId": "vid", "uploadType": "media"})

    def test_post_comment_body(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.post_comment("vid", "Hallo")
        snippet = req.call_args.kwargs["body"]["snippet"]
        self.assertEqual(snippet["videoId"], "vid")
        self.assertEqual(snippet["topLevelComment"]["snippet"]["textOriginal"], "Hallo")

    def test_list_comments_clamps(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.list_comments("vid", max_results=1000)
        self.assertEqual(req.call_args.kwargs["query"]["maxResults"], 100)

    def test_video_analytics_filters_by_video(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.video_analytics("vid", "2026-10-01", "2026-10-28")
        query = req.call_args.kwargs["query"]
        self.assertEqual(query["filters"], "video==vid")
        self.assertEqual(query["dimensions"], "day")
        self.assertEqual(query["ids"], "channel==MINE")


class TokenTests(unittest.TestCase):
    def test_expired_token_is_refreshed(self):
        import time
        auth = server.AuthConfig(server.Path("c.json"), server.Path("t.json"))
        client = server.YouTubeClient(auth)
        old = {"access_token": "old", "expires_in": 3600, "created_at": int(time.time()) - 7200,
               "refresh_token": "R"}
        with mock.patch.object(auth, "load_token", return_value=old), \
                mock.patch.object(client, "_refresh_token", return_value={"access_token": "new"}) as refresh:
            self.assertEqual(client._access_token(), "new")
        refresh.assert_called_once()

    def test_valid_token_is_reused(self):
        import time
        auth = server.AuthConfig(server.Path("c.json"), server.Path("t.json"))
        client = server.YouTubeClient(auth)
        fresh = {"access_token": "ok", "expires_in": 3600, "created_at": int(time.time())}
        with mock.patch.object(auth, "load_token", return_value=fresh), \
                mock.patch.object(client, "_refresh_token") as refresh:
            self.assertEqual(client._access_token(), "ok")
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
