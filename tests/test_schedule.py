import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import make_client, server


def future(days=3, tz=timezone.utc):
    return (datetime.now(tz) + timedelta(days=days)).replace(microsecond=0)


class NormalizeTests(unittest.TestCase):
    def test_utc_z(self):
        when = future()
        self.assertEqual(server.normalize_publish_at(when.strftime("%Y-%m-%dT%H:%M:%SZ")),
                         when.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_offset_is_converted_to_utc(self):
        berlin = timezone(timedelta(hours=1))
        when = future(tz=berlin)
        result = server.normalize_publish_at(when.isoformat())
        self.assertEqual(result, when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_fixed_example(self):
        # 17:00 at +01:00 is 16:00 UTC (far-future date so the test never expires)
        self.assertEqual(server.normalize_publish_at("2099-11-05T17:00:00+01:00"), "2099-11-05T16:00:00Z")

    def test_naive_time_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            server.normalize_publish_at("2099-11-05T17:00:00")

    def test_garbage_rejected(self):
        with self.assertRaisesRegex(ValueError, "ISO 8601"):
            server.normalize_publish_at("morgen um fünf")

    def test_past_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            server.normalize_publish_at("2020-01-01T00:00:00Z")


class UpdateVideoScheduleTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def update(self, existing_status=None, **kwargs):
        existing = {"snippet": {"title": "T", "description": "D"},
                    "status": existing_status or {"privacyStatus": "private"}}
        with mock.patch.object(self.client, "get_video", return_value=existing), \
                mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_video("vid", **kwargs)
        return req.call_args.kwargs["body"]["status"]

    def test_schedule_sets_private_and_publish_at(self):
        status = self.update({"privacyStatus": "public"}, publish_at="2099-11-05T17:00:00+01:00")
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(status["publishAt"], "2099-11-05T16:00:00Z")

    def test_schedule_with_public_is_rejected_before_any_request(self):
        with mock.patch.object(self.client, "get_video") as get:
            with self.assertRaisesRegex(ValueError, "private"):
                self.client.update_video("vid", privacy_status="public", publish_at="2099-11-05T17:00:00Z")
        get.assert_not_called()

    def test_empty_string_clears_schedule(self):
        status = self.update({"privacyStatus": "private", "publishAt": "2099-01-01T00:00:00Z"}, publish_at="")
        self.assertNotIn("publishAt", status)
        self.assertEqual(status["privacyStatus"], "private")

    def test_publishing_now_drops_old_schedule(self):
        status = self.update({"privacyStatus": "private", "publishAt": "2099-01-01T00:00:00Z"},
                             privacy_status="public")
        self.assertEqual(status["privacyStatus"], "public")
        self.assertNotIn("publishAt", status)

    def test_untouched_schedule_is_preserved(self):
        existing = {"privacyStatus": "private", "publishAt": "2099-01-01T00:00:00Z"}
        status = self.update(dict(existing), title="Neuer Titel")
        self.assertEqual(status, existing)

    def test_dispatch_passes_publish_at(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "update_video", return_value={}) as up:
            srv._call_tool("youtube_update_video", {"video_id": "v", "publish_at": "2099-11-05T17:00:00Z"})
        self.assertEqual(up.call_args.kwargs["publish_at"], "2099-11-05T17:00:00Z")


class UploadScheduleTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.video = pathlib.Path(tmp.name) / "v.mp4"
        self.video.write_bytes(b"123")

    @staticmethod
    def responses():
        return [(200, {"Location": "http://s"}, b""),
                (200, {}, json.dumps({"id": "V"}).encode())]

    def test_upload_metadata_contains_publish_at(self):
        with mock.patch.object(server, "http_raw", side_effect=self.responses()) as http:
            self.client.upload_video(str(self.video), "T", publish_at="2099-11-05T17:00:00+01:00")
        status = json.loads(http.call_args_list[0].kwargs["data"])["status"]
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(status["publishAt"], "2099-11-05T16:00:00Z")

    def test_upload_without_schedule_has_no_publish_at(self):
        with mock.patch.object(server, "http_raw", side_effect=self.responses()) as http:
            self.client.upload_video(str(self.video), "T")
        self.assertNotIn("publishAt", json.loads(http.call_args_list[0].kwargs["data"])["status"])

    def test_upload_schedule_requires_private(self):
        with mock.patch.object(server, "http_raw") as http:
            with self.assertRaisesRegex(ValueError, "private"):
                self.client.upload_video(str(self.video), "T", privacy_status="public",
                                         publish_at="2099-11-05T17:00:00Z")
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
