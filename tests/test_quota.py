import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import make_client, server

API, UPLOAD, ANALYTICS = server.YOUTUBE_API_BASE, server.YOUTUBE_UPLOAD_BASE, server.YOUTUBE_ANALYTICS_BASE


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class CostTests(unittest.TestCase):
    def cost(self, base, path, method="GET"):
        return server.request_cost(base, path, method)

    def test_reads_cost_one(self):
        for path in ("/videos", "/channels", "/playlists", "/playlistItems", "/commentThreads"):
            self.assertEqual(self.cost(API, path), 1, path)

    def test_writes_cost_fifty(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.cost(API, "/playlists", method), 50, method)
        self.assertEqual(self.cost(API, "/videos", "PUT"), 50)
        self.assertEqual(self.cost(API, "/commentThreads", "POST"), 50)
        self.assertEqual(self.cost(UPLOAD, "/thumbnails/set", "POST"), 50)

    def test_captions(self):
        self.assertEqual(self.cost(API, "/captions"), 50)
        self.assertEqual(self.cost(UPLOAD, "/captions", "POST"), 400)
        self.assertEqual(self.cost(UPLOAD, "/captions", "PUT"), 450)
        self.assertEqual(self.cost(API, "/captions", "DELETE"), 50)
        self.assertEqual(self.cost(API, "/captions/ABC", "GET"), 200)

    def test_search_is_expensive(self):
        self.assertEqual(self.cost(API, "/search"), 100)

    def test_analytics_is_a_separate_quota(self):
        self.assertEqual(self.cost(ANALYTICS, "/reports"), 0)


class PacificTimeTests(unittest.TestCase):
    def test_offsets_around_dst_changes(self):
        h = lambda x: timedelta(hours=x)
        self.assertEqual(server.pacific_offset(utc(2026, 1, 15, 12)), h(-8))
        self.assertEqual(server.pacific_offset(utc(2026, 7, 15, 12)), h(-7))
        # 2026: DST starts Sun 8 March 10:00 UTC, ends Sun 1 Nov 09:00 UTC
        self.assertEqual(server.pacific_offset(utc(2026, 3, 8, 9, 59)), h(-8))
        self.assertEqual(server.pacific_offset(utc(2026, 3, 8, 10, 0)), h(-7))
        self.assertEqual(server.pacific_offset(utc(2026, 11, 1, 8, 59)), h(-7))
        self.assertEqual(server.pacific_offset(utc(2026, 11, 1, 9, 0)), h(-8))

    def test_nth_sunday(self):
        self.assertEqual(server._nth_sunday(2026, 3, 2), 8)
        self.assertEqual(server._nth_sunday(2026, 11, 1), 1)
        self.assertEqual(server._nth_sunday(2027, 3, 2), 14)
        self.assertEqual(server._nth_sunday(2027, 11, 1), 7)

    def test_quota_day_uses_pacific_calendar(self):
        # 03:00 UTC in summer is still the previous evening in Pacific time
        self.assertEqual(server.quota_day(utc(2026, 7, 15, 3, 0)), "2026-07-14")
        self.assertEqual(server.quota_day(utc(2026, 7, 15, 7, 0)), "2026-07-15")
        self.assertEqual(server.quota_day(utc(2026, 12, 15, 7, 59)), "2026-12-14")
        self.assertEqual(server.quota_day(utc(2026, 12, 15, 8, 0)), "2026-12-15")

    def test_next_reset(self):
        self.assertEqual(server.next_quota_reset(utc(2026, 7, 15, 12)), utc(2026, 7, 16, 7))
        self.assertEqual(server.next_quota_reset(utc(2026, 12, 15, 12)), utc(2026, 12, 16, 8))
        # evening before the November change: still PDT until 02:00
        self.assertEqual(server.next_quota_reset(utc(2026, 10, 31, 20)), utc(2026, 11, 1, 7))
        # the day after the change is on PST
        self.assertEqual(server.next_quota_reset(utc(2026, 11, 1, 20)), utc(2026, 11, 2, 8))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "quota.json"

    def test_add_and_status(self):
        tracker = server.QuotaTracker(self.path, limit=1000)
        tracker.add(50, "POST /playlists")
        tracker.add(1, "GET /videos")
        tracker.add(50, "POST /playlists")
        status = tracker.status()
        self.assertEqual(status["used_estimate"], 101)
        self.assertEqual(status["remaining_estimate"], 899)
        self.assertEqual(status["breakdown"], {"POST /playlists": 100, "GET /videos": 1})
        self.assertNotIn("warning", status)

    def test_zero_cost_is_ignored(self):
        tracker = server.QuotaTracker(self.path)
        tracker.add(0, "GET /reports")
        self.assertEqual(tracker.used, 0)
        self.assertFalse(self.path.exists())

    def test_persists_across_restarts(self):
        server.QuotaTracker(self.path).add(1600, "POST /videos (upload)")
        again = server.QuotaTracker(self.path)
        self.assertEqual(again.used, 1600)
        self.assertEqual(again.calls, {"POST /videos (upload)": 1600})

    def test_old_day_in_file_is_ignored(self):
        self.path.write_text(json.dumps({"day": "2020-01-01", "used": 9999, "calls": {"x": 9999}}))
        self.assertEqual(server.QuotaTracker(self.path).used, 0)

    def test_corrupt_file_starts_from_zero(self):
        self.path.write_text("{not json")
        self.assertEqual(server.QuotaTracker(self.path).used, 0)

    def test_day_rollover_resets_counter(self):
        tracker = server.QuotaTracker(self.path)
        tracker.add(500, "x", now=utc(2026, 12, 15, 12))
        self.assertEqual(tracker.status(utc(2026, 12, 15, 20))["used_estimate"], 500)
        later = tracker.status(utc(2026, 12, 16, 12))
        self.assertEqual((later["used_estimate"], later["breakdown"]), (0, {}))

    def test_warning_at_eighty_percent(self):
        tracker = server.QuotaTracker(None, limit=1000)
        tracker.add(800, "x")
        self.assertIn("warning", tracker.status())

    def test_remaining_never_negative(self):
        tracker = server.QuotaTracker(None, limit=100)
        tracker.add(150, "x")
        self.assertEqual(tracker.status()["remaining_estimate"], 0)

    def test_unwritable_file_does_not_raise(self):
        tracker = server.QuotaTracker(self.path)
        with mock.patch.object(server, "write_json", side_effect=OSError("disk full")):
            tracker.add(5, "x")
        self.assertEqual(tracker.used, 5)


class ClientCountingTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.client.quota = server.QuotaTracker(None)

    def test_successful_request_is_counted(self):
        with mock.patch.object(server, "http_json", return_value={}):
            self.client._request(API, "/playlists", method="POST", body={})
            self.client._request(API, "/videos")
        self.assertEqual(self.client.quota.used, 51)
        self.assertEqual(self.client.quota.calls, {"POST /playlists": 50, "GET /videos": 1})

    def test_failed_request_is_not_counted(self):
        with mock.patch.object(server, "http_json", side_effect=server.ApiError(403, "x", "forbidden")):
            with self.assertRaises(server.ApiError):
                self.client._request(API, "/videos")
        self.assertEqual(self.client.quota.used, 0)

    def test_analytics_is_not_counted(self):
        with mock.patch.object(server, "http_json", return_value={}):
            self.client._request(ANALYTICS, "/reports")
        self.assertEqual(self.client.quota.used, 0)

    def test_caption_download_is_counted_as_text(self):
        with mock.patch.object(server, "http_raw", return_value=(200, {}, b"1\n")):
            self.client._request(API, "/captions/C1", as_text=True)
        self.assertEqual(self.client.quota.used, 200)

    def test_upload_costs_1600_only_when_it_completes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        video = pathlib.Path(tmp.name) / "v.mp4"
        video.write_bytes(b"123")
        responses = [(200, {"Location": "http://s"}, b""), (200, {}, json.dumps({"id": "V"}).encode())]
        with mock.patch.object(server, "http_raw", side_effect=responses):
            self.client.upload_video(str(video), "T")
        self.assertEqual(self.client.quota.used, 1600)

        failing = server.QuotaTracker(None)
        self.client.quota = failing
        responses = [(200, {"Location": "http://s"}, b""), server.ApiError(403, "no", "forbidden")]
        with mock.patch.object(server, "http_raw", side_effect=responses):
            with self.assertRaises(server.ApiError):
                self.client.upload_video(str(video), "T")
        self.assertEqual(failing.used, 0)


class ToolTests(unittest.TestCase):
    def test_tool_returns_status(self):
        srv = server.McpServer()
        srv.youtube.quota = server.QuotaTracker(None, limit=10000)
        srv.youtube.quota.add(1600, "POST /videos (upload)")
        result = srv._call_tool("youtube_quota_status", {})
        self.assertEqual(result["used_estimate"], 1600)
        self.assertEqual(result["remaining_estimate"], 8400)
        self.assertTrue(result["resets_at_utc"].endswith("Z"))

    def test_server_reads_limit_and_file_from_environment(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ",
            {"YOUTUBE_QUOTA_FILE": str(pathlib.Path(tmp) / "q.json"), "YOUTUBE_DAILY_QUOTA": "250"},
        ):
            srv = server.McpServer()
        self.assertEqual(srv.youtube.quota.limit, 250)
        self.assertEqual(srv.youtube.quota.path.name, "q.json")

    def test_tool_is_read_only_and_local(self):
        tool = {t["name"]: t for t in server.McpServer().tools}["youtube_quota_status"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["openWorldHint"])


if __name__ == "__main__":
    unittest.main()
