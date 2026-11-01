import json
import pathlib
import tempfile
import unittest
from unittest import mock

from helpers import make_client, server

VIDEO = {
    "id": "vid",
    "snippet": {"title": "T", "description": "D"},
    "status": {
        "privacyStatus": "private",
        "embeddable": True,
        "license": "youtube",
        "publicStatsViewable": True,
        "madeForKids": False,
        "selfDeclaredMadeForKids": False,
    },
}


class StatusFlagTests(unittest.TestCase):
    def test_only_given_fields_are_returned_with_api_names(self):
        self.assertEqual(server.status_flags(), {})
        self.assertEqual(
            server.status_flags(made_for_kids=True, embeddable=False, license="creativeCommon",
                                public_stats_viewable=False, contains_synthetic_media=True),
            {"selfDeclaredMadeForKids": True, "embeddable": False, "license": "creativeCommon",
             "publicStatsViewable": False, "containsSyntheticMedia": True},
        )
        self.assertEqual(server.status_flags(embeddable=False), {"embeddable": False})

    def test_false_is_kept_not_treated_as_missing(self):
        self.assertEqual(server.status_flags(made_for_kids=False), {"selfDeclaredMadeForKids": False})

    def test_strings_and_numbers_are_rejected(self):
        for kwargs in ({"made_for_kids": "false"}, {"embeddable": "true"}, {"public_stats_viewable": 1},
                       {"contains_synthetic_media": "no"}):
            with self.assertRaisesRegex(ValueError, "true or false", msg=kwargs):
                server.status_flags(**kwargs)

    def test_license_is_validated(self):
        with self.assertRaisesRegex(ValueError, "license"):
            server.status_flags(license="cc-by")


class UpdateVideoFieldTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def update(self, **kwargs):
        with mock.patch.object(self.client, "get_video", return_value=json.loads(json.dumps(VIDEO))), \
                mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_video("vid", **kwargs)
        return req.call_args.kwargs["body"]["status"]

    def test_flags_change_only_what_is_given(self):
        status = self.update(embeddable=False, contains_synthetic_media=True, license="creativeCommon")
        self.assertFalse(status["embeddable"])
        self.assertTrue(status["containsSyntheticMedia"])
        self.assertEqual(status["license"], "creativeCommon")
        self.assertTrue(status["publicStatsViewable"])  # untouched
        self.assertEqual(status["privacyStatus"], "private")

    def test_made_for_kids_sets_the_self_declared_field(self):
        status = self.update(made_for_kids=True)
        self.assertTrue(status["selfDeclaredMadeForKids"])

    def test_no_flags_leaves_status_alone(self):
        self.assertEqual(self.update(title="Neu"), VIDEO["status"])

    def test_invalid_flag_fails_before_any_request(self):
        with mock.patch.object(self.client, "get_video") as get:
            with self.assertRaises(ValueError):
                self.client.update_video("vid", embeddable="false")
            with self.assertRaises(ValueError):
                self.client.update_video("vid", license="mit")
        get.assert_not_called()

    def test_dry_run_shows_the_flag_changes(self):
        srv = server.McpServer()
        srv.youtube._access_token = lambda: "t"
        with mock.patch.object(server, "http_json", return_value={"items": [json.loads(json.dumps(VIDEO))]}):
            result = srv._call_tool("youtube_update_video",
                                    {"video_id": "vid", "embeddable": False, "license": "creativeCommon", "dry_run": True})
        self.assertEqual(result["changes"], {
            "status.embeddable": {"before": True, "after": False},
            "status.license": {"before": "youtube", "after": "creativeCommon"},
        })


class UploadFieldTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.video = pathlib.Path(tmp.name) / "v.mp4"
        self.video.write_bytes(b"123")

    def status_sent(self, **kwargs):
        responses = [(200, {"Location": "http://s"}, b""), (200, {}, json.dumps({"id": "V"}).encode())]
        with mock.patch.object(server, "http_raw", side_effect=responses) as http:
            self.client.upload_video(str(self.video), "T", **kwargs)
        return json.loads(http.call_args_list[0].kwargs["data"])["status"]

    def test_defaults_are_unchanged(self):
        self.assertEqual(self.status_sent(), {"privacyStatus": "private", "selfDeclaredMadeForKids": False})

    def test_flags_are_added_when_given(self):
        status = self.status_sent(embeddable=False, license="creativeCommon", public_stats_viewable=False,
                                  contains_synthetic_media=True, made_for_kids=True)
        self.assertEqual(status, {
            "privacyStatus": "private", "selfDeclaredMadeForKids": True, "embeddable": False,
            "license": "creativeCommon", "publicStatsViewable": False, "containsSyntheticMedia": True,
        })

    def test_flags_combine_with_a_schedule(self):
        status = self.status_sent(publish_at="2099-11-05T17:00:00Z", contains_synthetic_media=False)
        self.assertEqual(status["publishAt"], "2099-11-05T17:00:00Z")
        self.assertFalse(status["containsSyntheticMedia"])

    def test_invalid_values_send_nothing(self):
        with mock.patch.object(server, "http_raw") as http:
            for kwargs in ({"made_for_kids": "false"}, {"embeddable": "yes"}, {"license": "x"}):
                with self.assertRaises(ValueError, msg=kwargs):
                    self.client.upload_video(str(self.video), "T", **kwargs)
        http.assert_not_called()

    def test_string_false_is_no_longer_turned_into_true(self):
        srv = server.McpServer()
        with mock.patch.object(server, "http_raw") as http:
            with self.assertRaisesRegex(ValueError, "made_for_kids"):
                srv._call_tool("youtube_upload_video",
                               {"file_path": str(self.video), "title": "T", "made_for_kids": "false"})
        http.assert_not_called()


class SchemaAndDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tools = {t["name"]: t for t in server.McpServer().tools}

    def test_schemas_offer_the_fields(self):
        names = {"made_for_kids", "embeddable", "license", "public_stats_viewable", "contains_synthetic_media"}
        for tool in ("youtube_update_video", "youtube_upload_video"):
            self.assertTrue(names <= set(self.tools[tool]["inputSchema"]["properties"]), tool)
        self.assertEqual(self.tools["youtube_update_video"]["inputSchema"]["properties"]["license"]["enum"],
                         ["youtube", "creativeCommon"])
        self.assertEqual(self.tools["youtube_upload_video"]["inputSchema"]["properties"]["made_for_kids"]["description"],
                         "Default false.")

    def test_update_dispatch_forwards_the_fields(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "update_video", return_value={}) as up:
            srv._call_tool("youtube_update_video", {"video_id": "v", "embeddable": False, "license": "youtube"})
        kwargs = up.call_args.kwargs
        self.assertIs(kwargs["embeddable"], False)
        self.assertEqual(kwargs["license"], "youtube")
        self.assertIsNone(kwargs["made_for_kids"])
        self.assertIsNone(kwargs["public_stats_viewable"])


if __name__ == "__main__":
    unittest.main()
