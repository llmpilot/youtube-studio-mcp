import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from helpers import make_client, server

SESSION = "https://upload.example/session/abc"


def raw(status, headers=None, body=b""):
    return status, headers or {}, body


def done(video_id="VID123"):
    return raw(200, body=json.dumps({"id": video_id, "snippet": {"title": "T"}}).encode("utf-8"))


class UploadVideoTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = pathlib.Path(self.tmp.name) / "film.mp4"
        self.video.write_bytes(b"0123456789")  # 10 bytes
        patcher = mock.patch.object(server, "UPLOAD_CHUNK_SIZE", 4)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, responses, **kwargs):
        kwargs.setdefault("title", "Mein Video")
        with mock.patch.object(server, "http_raw", side_effect=responses) as http, \
                mock.patch.object(server.time, "sleep"):
            result = self.client.upload_video(str(self.video), **kwargs)
        return result, http.call_args_list

    def test_session_request_and_metadata(self):
        result, calls = self.upload(
            [raw(200, {"Location": SESSION}), done()],
            title="Titel", description="Text", tags=["etf"], default_language="de",
            privacy_status="unlisted", made_for_kids=False,
        )
        start = calls[0]
        self.assertIn("uploadType=resumable", start.args[0])
        self.assertIn("part=snippet,status", start.args[0])
        self.assertEqual(start.kwargs["method"], "POST")
        headers = start.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(headers["X-Upload-Content-Length"], "10")
        self.assertEqual(headers["X-Upload-Content-Type"], "video/mp4")
        meta = json.loads(start.kwargs["data"])
        self.assertEqual(meta["snippet"]["title"], "Titel")
        self.assertEqual(meta["snippet"]["tags"], ["etf"])
        self.assertEqual(meta["snippet"]["defaultLanguage"], "de")
        self.assertEqual(meta["status"], {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False})
        self.assertEqual(result["watchUrl"], "https://youtu.be/VID123")

    def test_default_privacy_is_private(self):
        _, calls = self.upload([raw(200, {"Location": SESSION}), done()])
        self.assertEqual(json.loads(calls[0].kwargs["data"])["status"]["privacyStatus"], "private")

    def test_chunks_follow_server_range(self):
        _, calls = self.upload([
            raw(200, {"Location": SESSION}),
            raw(308, {"Range": "bytes=0-3"}),
            raw(308, {"Range": "bytes=0-7"}),
            done(),
        ])
        puts = calls[1:]
        self.assertEqual([c.kwargs["headers"]["Content-Range"] for c in puts],
                         ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"])
        self.assertEqual([c.kwargs["data"] for c in puts], [b"0123", b"4567", b"89"])
        self.assertTrue(all(c.args[0] == SESSION for c in puts))

    def test_resumes_from_server_offset_after_503(self):
        responses = [
            raw(200, {"Location": SESSION}),
            raw(308, {"Range": "bytes=0-3"}),
            server.ApiError(503, "backend error"),
            raw(308, {"Range": "bytes=0-5"}),   # status query: 6 bytes arrived
            done(),
        ]
        _, calls = self.upload(responses)
        ranges = [c.kwargs["headers"]["Content-Range"] for c in calls[1:]]
        self.assertEqual(ranges, ["bytes 0-3/10", "bytes 4-7/10", "bytes */10", "bytes 6-9/10"])

    def test_network_error_is_retried(self):
        responses = [
            raw(200, {"Location": SESSION}),
            urllib.error.URLError("reset"),
            raw(308, {}),                        # nothing stored yet
            raw(308, {"Range": "bytes=0-3"}),
            raw(308, {"Range": "bytes=0-7"}),
            done(),
        ]
        result, _ = self.upload(responses)
        self.assertEqual(result["id"], "VID123")

    def test_client_error_is_not_retried(self):
        with mock.patch.object(server, "http_raw",
                               side_effect=[raw(200, {"Location": SESSION}), server.ApiError(403, "forbidden")]) as http:
            with mock.patch.object(server.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "forbidden"):
                    self.client.upload_video(str(self.video), "T")
        self.assertEqual(http.call_count, 2)

    def test_gives_up_after_retries(self):
        responses = [raw(200, {"Location": SESSION})] + [server.ApiError(500, "boom")] * 10
        with mock.patch.object(server, "http_raw", side_effect=responses), \
                mock.patch.object(server.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.client.upload_video(str(self.video), "T")

    def test_missing_session_url(self):
        with mock.patch.object(server, "http_raw", return_value=raw(200, {})):
            with self.assertRaisesRegex(RuntimeError, "session URL"):
                self.client.upload_video(str(self.video), "T")

    def test_validation(self):
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.client.upload_video(str(self.video) + ".nope", "T")
        with self.assertRaisesRegex(ValueError, "privacy_status"):
            self.client.upload_video(str(self.video), "T", privacy_status="secret")
        with self.assertRaisesRegex(ValueError, "100 characters"):
            self.client.upload_video(str(self.video), "x" * 101)
        with self.assertRaisesRegex(ValueError, "empty"):
            self.client.upload_video(str(self.video), "   ")
        empty = pathlib.Path(self.tmp.name) / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            self.client.upload_video(str(empty), "T")

    def test_no_request_is_sent_when_validation_fails(self):
        with mock.patch.object(server, "http_raw") as http:
            with self.assertRaises(ValueError):
                self.client.upload_video(str(self.video), "T", privacy_status="secret")
        http.assert_not_called()


class HttpRawTests(unittest.TestCase):
    def test_308_is_returned_not_raised(self):
        import io
        err = urllib.error.HTTPError("http://x", 308, "Resume Incomplete", {"Range": "bytes=0-9"}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            status, headers, _ = server.http_raw("http://x", method="PUT", headers={})
        self.assertEqual(status, 308)
        self.assertEqual(headers["Range"], "bytes=0-9")

    def test_error_carries_status_and_message(self):
        import io
        err = urllib.error.HTTPError("http://x", 403, "Forbidden", {},
                                     io.BytesIO(b'{"error":{"message":"uploadLimitExceeded"}}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(server.ApiError) as ctx:
                server.http_raw("http://x", method="POST", headers={})
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("uploadLimitExceeded", str(ctx.exception))


class ToolDispatchTests(unittest.TestCase):
    def test_tool_schema(self):
        tools = {t["name"]: t for t in server.McpServer().tools}
        schema = tools["youtube_upload_video"]["inputSchema"]
        self.assertEqual(schema["required"], ["file_path", "title"])
        self.assertEqual(schema["properties"]["privacy_status"]["enum"], ["private", "unlisted", "public"])

    def test_call_passes_arguments_with_safe_defaults(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "upload_video", return_value={"id": "x"}) as up:
            srv._call_tool("youtube_upload_video", {"file_path": "/a.mp4", "title": "T"})
        up.assert_called_once_with(
            "/a.mp4", "T", description="", tags=None, category_id="22",
            default_language=None, privacy_status="private", made_for_kids=False,
            publish_at=None,
        )


if __name__ == "__main__":
    unittest.main()
