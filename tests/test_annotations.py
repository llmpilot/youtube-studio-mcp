import unittest

from helpers import INIT, TOOLS_LIST, ndjson, parse_ndjson, run_stdio, server

READ_ONLY = {
    "youtube_auth_status",
    "youtube_start_auth",
    "youtube_channel_overview",
    "youtube_list_videos",
    "youtube_get_video",
    "youtube_channel_analytics",
    "youtube_video_analytics",
    "youtube_list_comments",
    "youtube_list_captions",
    "youtube_list_playlists",
    "youtube_list_playlist_items",
    "youtube_quota_status",
    "youtube_list_replies",
    "youtube_list_channel_comments",
}
WRITES = {
    "youtube_reply_to_comment",
    "youtube_delete_comment",
    "youtube_moderate_comments",
    "youtube_add_playlist_item",
    "youtube_remove_playlist_item",
    "youtube_move_playlist_item",
    "youtube_create_playlist",
    "youtube_update_playlist",
    "youtube_delete_playlist",
    "youtube_upload_caption",
    "youtube_update_caption",
    "youtube_delete_caption",
    "youtube_download_caption",
    "youtube_update_video",
    "youtube_upload_video",
    "youtube_upload_thumbnail",
    "youtube_post_comment",
}


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.tools = {t["name"]: t for t in server.McpServer().tools}

    def test_every_tool_is_classified(self):
        self.assertEqual(set(self.tools), READ_ONLY | WRITES)
        self.assertEqual(set(server._TOOL_HINTS), set(self.tools))
        for name, tool in self.tools.items():
            self.assertIn("annotations", tool, name)
            self.assertTrue(tool["annotations"]["title"], name)

    def test_read_tools_are_read_only(self):
        for name in READ_ONLY:
            ann = self.tools[name]["annotations"]
            self.assertTrue(ann["readOnlyHint"], name)
            self.assertNotIn("destructiveHint", ann, name)

    def test_write_tools_are_not_read_only(self):
        for name in WRITES:
            ann = self.tools[name]["annotations"]
            self.assertFalse(ann["readOnlyHint"], name)
            self.assertIn("destructiveHint", ann, name)
            self.assertIn("idempotentHint", ann, name)

    def test_overwriting_tools_are_destructive_additive_ones_are_not(self):
        destructive = {n for n in WRITES if self.tools[n]["annotations"]["destructiveHint"]}
        self.assertEqual(destructive, {
            "youtube_update_video", "youtube_upload_thumbnail",
            "youtube_update_caption", "youtube_delete_caption",
            "youtube_update_playlist", "youtube_delete_playlist",
            "youtube_remove_playlist_item",
            "youtube_delete_comment", "youtube_moderate_comments",
        })

    def test_repeating_additive_writes_is_not_idempotent(self):
        for name in ("youtube_upload_video", "youtube_post_comment", "youtube_upload_caption",
                     "youtube_create_playlist", "youtube_add_playlist_item",
                     "youtube_reply_to_comment"):
            self.assertFalse(self.tools[name]["annotations"]["idempotentHint"], name)

    def test_local_tools_are_not_open_world(self):
        self.assertFalse(self.tools["youtube_auth_status"]["annotations"]["openWorldHint"])
        self.assertFalse(self.tools["youtube_start_auth"]["annotations"]["openWorldHint"])
        self.assertTrue(self.tools["youtube_list_videos"]["annotations"]["openWorldHint"])

    def test_annotations_are_sent_over_the_wire(self):
        out = parse_ndjson(run_stdio(ndjson(INIT, TOOLS_LIST)))
        for tool in out[1]["result"]["tools"]:
            self.assertIn("readOnlyHint", tool["annotations"], tool["name"])


if __name__ == "__main__":
    unittest.main()
