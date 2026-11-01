import copy
import pathlib
import tempfile
import unittest
from unittest import mock

from helpers import server

VIDEO = {
    "id": "vid",
    "snippet": {"title": "Alt", "description": "Beschreibung", "tags": ["etf"], "categoryId": "22"},
    "status": {"privacyStatus": "private", "madeForKids": False},
}


class FakeYouTube:
    """Routes GET requests by path; records every call so tests can prove no write left the process."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def http_json(self, url, *, method="GET", headers=None, data=None):
        self.calls.append((method, url, data))
        if method == "GET":
            for fragment, response in self.routes.items():
                if fragment in url:
                    return copy.deepcopy(response)  # like a fresh API answer each time
        return {}

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class DryRunBase(unittest.TestCase):
    def setUp(self):
        self.srv = server.McpServer()
        self.srv.youtube._access_token = lambda: "test-token"
        self.srv.youtube.quota = server.QuotaTracker(None)

    def run_tool(self, name, arguments, routes=None):
        fake = FakeYouTube(routes)
        with mock.patch.object(server, "http_json", fake.http_json), \
                mock.patch.object(server, "http_raw", side_effect=AssertionError("network write in dry run")):
            result = self.srv._call_tool(name, arguments)
        return result, fake


class SwitchTests(DryRunBase):
    def test_result_shape(self):
        result, fake = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1", "dry_run": True})
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["tool"], "youtube_delete_playlist")
        self.assertIn("Nothing was changed", result["message"])
        self.assertEqual(result["planned_requests"][0]["method"], "DELETE")
        self.assertEqual(result["quota_cost_estimate"], 50)
        self.assertEqual(fake.writes, [])

    def test_without_flag_the_write_happens(self):
        _, fake = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1"})
        self.assertEqual([c[0] for c in fake.writes], ["DELETE"])

    def test_false_values_run_for_real_everything_else_is_a_preview(self):
        for off in (False, None, "false", "False", 0, "0", ""):
            _, fake = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1", "dry_run": off})
            self.assertEqual(len(fake.writes), 1, repr(off))
        for on in (True, "true", "yes", 1):
            result, fake = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1", "dry_run": on})
            self.assertEqual(fake.writes, [], repr(on))
            self.assertTrue(result["dry_run"])

    def test_read_only_tools_ignore_the_flag(self):
        result, _ = self.run_tool("youtube_get_video", {"video_id": "vid", "dry_run": True},
                                  {"/videos": {"items": [VIDEO]}})
        self.assertNotIn("dry_run", result)
        self.assertEqual(result["id"], "vid")

    def test_unknown_tool_still_fails(self):
        with self.assertRaises(server.UnknownTool):
            self.srv._call_tool("youtube_nope", {"dry_run": True})

    def test_flag_is_not_forwarded_to_the_tool(self):
        with mock.patch.object(self.srv.youtube, "delete_comment", return_value={}) as dele:
            self.srv._call_tool("youtube_delete_comment", {"comment_id": "C1"})
        dele.assert_called_once_with("C1")

    def test_state_is_reset_after_success_and_after_errors(self):
        self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1", "dry_run": True})
        self.assertIsNone(self.srv.youtube._plan)
        with self.assertRaises(ValueError):
            self.run_tool("youtube_create_playlist", {"title": " ", "dry_run": True})
        self.assertIsNone(self.srv.youtube._plan)
        _, fake = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1"})
        self.assertEqual(len(fake.writes), 1)

    def test_validation_still_runs_in_a_dry_run(self):
        with self.assertRaisesRegex(ValueError, "privacy_status"):
            self.run_tool("youtube_create_playlist", {"title": "T", "privacy_status": "secret", "dry_run": True})

    def test_schema_offers_dry_run_for_every_write_tool_only(self):
        for tool in self.srv.tools:
            has_flag = "dry_run" in tool["inputSchema"]["properties"]
            self.assertEqual(has_flag, not tool["annotations"]["readOnlyHint"], tool["name"])
            self.assertNotIn("dry_run", tool["inputSchema"].get("required", []))

    def test_quota_counts_only_the_lookups(self):
        self.run_tool("youtube_update_video", {"video_id": "vid", "title": "Neu", "dry_run": True},
                      {"/videos": {"items": [VIDEO]}})
        self.assertEqual(self.srv.youtube.quota.used, 1)  # one GET videos.list, the PUT was not sent


class ChangeTests(DryRunBase):
    def test_update_video_shows_before_and_after(self):
        result, fake = self.run_tool(
            "youtube_update_video",
            {"video_id": "vid", "title": "Neu", "tags": ["etf", "sparen"], "dry_run": True},
            {"/videos": {"items": [VIDEO]}},
        )
        self.assertEqual(fake.writes, [])
        self.assertEqual(result["changes"], {
            "snippet.title": {"before": "Alt", "after": "Neu"},
            "snippet.tags": {"before": ["etf"], "after": ["etf", "sparen"]},
        })
        request = result["planned_requests"][0]
        self.assertEqual((request["method"], request["endpoint"]), ("PUT", "/videos"))
        self.assertEqual(request["body"]["snippet"]["title"], "Neu")
        self.assertEqual(result["quota_cost_estimate"], 50)

    def test_update_video_schedule_shows_forced_private_and_publish_at(self):
        video = {**VIDEO, "status": {"privacyStatus": "public", "madeForKids": False}}
        result, _ = self.run_tool(
            "youtube_update_video",
            {"video_id": "vid", "publish_at": "2099-11-05T17:00:00+01:00", "dry_run": True},
            {"/videos": {"items": [video]}},
        )
        self.assertEqual(result["changes"]["status.privacyStatus"], {"before": "public", "after": "private"})
        self.assertEqual(result["changes"]["status.publishAt"], {"before": None, "after": "2099-11-05T16:00:00Z"})

    def test_update_video_without_changes_reports_none(self):
        result, _ = self.run_tool(
            "youtube_update_video", {"video_id": "vid", "title": "Alt", "dry_run": True},
            {"/videos": {"items": [VIDEO]}},
        )
        self.assertNotIn("changes", result)

    def test_update_playlist_shows_diff(self):
        playlist = {"items": [{"id": "PL1", "snippet": {"title": "Alt", "description": "D"},
                               "status": {"privacyStatus": "public"}}]}
        result, fake = self.run_tool(
            "youtube_update_playlist",
            {"playlist_id": "PL1", "title": "Neu", "privacy_status": "unlisted", "dry_run": True},
            {"/playlists": playlist},
        )
        self.assertEqual(fake.writes, [])
        self.assertEqual(result["changes"], {
            "snippet.title": {"before": "Alt", "after": "Neu"},
            "status.privacyStatus": {"before": "public", "after": "unlisted"},
        })


class TargetTests(DryRunBase):
    def test_delete_comment_shows_what_would_be_deleted(self):
        comments = {"items": [{"id": "C1", "snippet": {"authorDisplayName": "Max", "textDisplay": "x" * 500,
                                                       "videoId": "vid"}}]}
        result, fake = self.run_tool("youtube_delete_comment", {"comment_id": "C1", "dry_run": True},
                                     {"/comments": comments})
        self.assertEqual(fake.writes, [])
        target = result["targets"][0]
        self.assertEqual((target["id"], target["author"], target["videoId"]), ("C1", "Max", "vid"))
        self.assertEqual(len(target["text"]), 200)

    def test_moderation_lists_all_comments_in_one_lookup(self):
        comments = {"items": [{"id": "A", "snippet": {"authorDisplayName": "a", "textDisplay": "spam"}}]}
        result, fake = self.run_tool(
            "youtube_moderate_comments",
            {"comment_ids": ["A", "B"], "status": "rejected", "ban_author": True, "dry_run": True},
            {"/comments": comments},
        )
        self.assertEqual(fake.writes, [])
        self.assertEqual(len([c for c in fake.calls if c[0] == "GET"]), 1)
        self.assertEqual(result["targets"][0]["text"], "spam")
        self.assertEqual(result["targets"][1], {"id": "B", "warning": "not found"})
        self.assertEqual(result["planned_requests"][0]["query"]["banAuthor"], "true")

    def test_delete_playlist_and_item_and_caption_show_titles(self):
        playlist = {"items": [{"id": "PL1", "snippet": {"title": "Serie"}, "status": {"privacyStatus": "public"}}]}
        result, _ = self.run_tool("youtube_delete_playlist", {"playlist_id": "PL1", "dry_run": True},
                                  {"/playlists": playlist})
        self.assertEqual(result["targets"][0]["title"], "Serie")

        item = {"items": [{"id": "I1", "snippet": {"title": "Folge 1", "playlistId": "PL1", "position": 0}}]}
        result, _ = self.run_tool("youtube_remove_playlist_item", {"item_id": "I1", "dry_run": True},
                                  {"/playlistItems": item})
        self.assertEqual(result["targets"][0]["title"], "Folge 1")

        caption = {"items": [{"id": "CAP", "snippet": {"language": "de", "name": "Deutsch", "videoId": "vid"}}]}
        result, _ = self.run_tool("youtube_delete_caption", {"caption_id": "CAP", "dry_run": True},
                                  {"/captions": caption})
        self.assertEqual(result["targets"][0]["language"], "de")

    def test_unknown_id_is_flagged(self):
        result, _ = self.run_tool("youtube_delete_playlist", {"playlist_id": "nope", "dry_run": True},
                                  {"/playlists": {"items": []}})
        self.assertEqual(result["targets"], [{"id": "nope", "warning": "not found"}])


class UploadAndPostTests(DryRunBase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def test_upload_video_sends_nothing(self):
        video = self.dir / "v.mp4"
        video.write_bytes(b"x" * 2048)
        result, fake = self.run_tool(
            "youtube_upload_video",
            {"file_path": str(video), "title": "Mein Video", "publish_at": "2099-11-05T17:00:00Z", "dry_run": True},
        )
        self.assertEqual(fake.calls, [])
        request = result["planned_requests"][0]
        self.assertEqual(request["size_bytes"], 2048)
        self.assertEqual(request["metadata"]["status"]["publishAt"], "2099-11-05T17:00:00Z")
        self.assertEqual(result["quota_cost_estimate"], 1600)
        self.assertEqual(self.srv.youtube.quota.used, 0)

    def test_upload_video_still_checks_the_file(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.run_tool("youtube_upload_video", {"file_path": str(self.dir / "x.mp4"), "title": "T", "dry_run": True})

    def test_thumbnail_reports_size_not_content(self):
        image = self.dir / "t.png"
        image.write_bytes(b"\x89PNG" + b"0" * 100)
        result, fake = self.run_tool("youtube_upload_thumbnail",
                                     {"video_id": "vid", "image_path": str(image), "dry_run": True})
        self.assertEqual(fake.writes, [])
        request = result["planned_requests"][0]
        self.assertEqual(request["payload_bytes"], 104)
        self.assertNotIn("body", request)
        self.assertEqual(result["quota_cost_estimate"], 50)

    def test_comment_text_is_shown(self):
        result, fake = self.run_tool("youtube_post_comment",
                                     {"video_id": "vid", "text": "Hallo zusammen", "dry_run": True})
        self.assertEqual(fake.writes, [])
        body = result["planned_requests"][0]["body"]
        self.assertEqual(body["snippet"]["topLevelComment"]["snippet"]["textOriginal"], "Hallo zusammen")

    def test_caption_download_writes_no_file(self):
        target = self.dir / "out" / "de.srt"
        result, fake = self.run_tool("youtube_download_caption",
                                     {"caption_id": "CAP", "output_path": str(target), "dry_run": True})
        self.assertEqual(fake.calls, [])
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertEqual(result["planned_requests"][0]["save_to"], str(target.resolve()))

    def test_caption_upload_reports_size(self):
        srt = self.dir / "de.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n", encoding="utf-8")
        result, fake = self.run_tool("youtube_upload_caption",
                                     {"video_id": "vid", "file_path": str(srt), "language": "de", "dry_run": True})
        self.assertEqual(fake.writes, [])
        self.assertEqual(result["quota_cost_estimate"], 400)
        self.assertGreater(result["planned_requests"][0]["payload_bytes"], 40)

    def test_playlist_item_move_shows_new_position(self):
        found = {"items": [{"id": "I1", "snippet": {"playlistId": "PL1", "position": 5,
                                                    "resourceId": {"kind": "youtube#video", "videoId": "vid"}}}]}
        result, fake = self.run_tool("youtube_move_playlist_item",
                                     {"item_id": "I1", "position": 0, "dry_run": True}, {"/playlistItems": found})
        self.assertEqual(fake.writes, [])
        self.assertEqual(result["planned_requests"][0]["body"]["snippet"]["position"], 0)


class EndToEndTests(unittest.TestCase):
    def test_over_the_wire(self):
        from helpers import INIT, ndjson, parse_ndjson, run_stdio
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "youtube_create_playlist", "arguments": {"title": "Test", "dry_run": True}}}
        reply = parse_ndjson(run_stdio(ndjson(INIT, call)))[1]
        text = reply["result"]["content"][0]["text"]
        self.assertNotIn("isError", reply["result"])
        self.assertIn('"dry_run": true', text)
        self.assertIn("POST", text)


if __name__ == "__main__":
    unittest.main()
