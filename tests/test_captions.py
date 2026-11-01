import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from helpers import make_client, server

SRT = "1\n00:00:00,000 --> 00:00:02,000\nHallo Welt, schöne Grüße\n"


class MultipartTests(unittest.TestCase):
    def test_structure(self):
        content_type, body = server.build_multipart({"a": 1}, b"MEDIA", "text/plain")
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(content_type.startswith("multipart/related"))
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"\r\n--{boundary}--\r\n".encode()))
        self.assertEqual(body.count(f"--{boundary}".encode()), 3)
        self.assertIn(b'{"a": 1}', body)
        self.assertIn(b"Content-Type: text/plain\r\n\r\nMEDIA\r\n", body)

    def test_unique_boundary_and_binary_safe(self):
        a, _ = server.build_multipart({}, b"")
        b, _ = server.build_multipart({}, b"")
        self.assertNotEqual(a, b)
        _, body = server.build_multipart({}, b"\x00\xff\r\n--x")
        self.assertIn(b"\x00\xff\r\n--x", body)


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.srt = self.dir / "de.srt"
        self.srt.write_text(SRT, encoding="utf-8")

    def test_list(self):
        with mock.patch.object(self.client, "_request", return_value={"items": []}) as req:
            self.client.list_captions("vid")
        self.assertEqual(req.call_args.args[1], "/captions")
        self.assertEqual(req.call_args.kwargs["query"], {"part": "snippet", "videoId": "vid"})

    def test_upload_sends_multipart_with_metadata_and_utf8_media(self):
        with mock.patch.object(server, "http_json", return_value={"id": "C1"}) as http:
            result = self.client.upload_caption("vid", str(self.srt), "de", name="Deutsch")
        self.assertEqual(result["id"], "C1")
        url = http.call_args.args[0]
        self.assertTrue(url.startswith(server.YOUTUBE_UPLOAD_BASE + "/captions?"))
        self.assertIn("uploadType=multipart", url)
        self.assertEqual(http.call_args.kwargs["method"], "POST")
        headers, data = http.call_args.kwargs["headers"], http.call_args.kwargs["data"]
        boundary = headers["Content-Type"].split("boundary=")[1]
        json_part = data.split(f"--{boundary}".encode())[1].split(b"\r\n\r\n", 1)[1].strip()
        snippet = json.loads(json_part)["snippet"]
        self.assertEqual(snippet, {"videoId": "vid", "language": "de", "name": "Deutsch", "isDraft": False})
        self.assertIn("schöne Grüße".encode("utf-8"), data)

    def test_upload_validation(self):
        with self.assertRaisesRegex(ValueError, "BCP-47"):
            self.client.upload_caption("vid", str(self.srt), "deutsch!")
        with self.assertRaisesRegex(RuntimeError, "not found"):
            self.client.upload_caption("vid", str(self.dir / "x.srt"), "de")
        empty = self.dir / "e.srt"
        empty.write_text("  \n")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            self.client.upload_caption("vid", str(empty), "de")

    def test_language_codes(self):
        for ok in ("de", "en-US", "zh-Hans", "fil"):
            self.assertTrue(server.LANGUAGE_RE.match(ok), ok)
        for bad in ("", "d", "de_DE", "english1", "de-"):
            self.assertFalse(server.LANGUAGE_RE.match(bad), bad)

    def test_update_metadata_only_uses_json_put(self):
        with mock.patch.object(self.client, "_request", return_value={}) as req:
            self.client.update_caption("C1", is_draft=True)
        kwargs = req.call_args.kwargs
        self.assertEqual(kwargs["method"], "PUT")
        self.assertEqual(kwargs["body"], {"id": "C1", "snippet": {"isDraft": True}})

    def test_update_with_file_uses_multipart_put(self):
        with mock.patch.object(server, "http_json", return_value={}) as http:
            self.client.update_caption("C1", file_path=str(self.srt))
        self.assertEqual(http.call_args.kwargs["method"], "PUT")
        self.assertIn("uploadType=multipart", http.call_args.args[0])
        self.assertIn(b'"id": "C1"', http.call_args.kwargs["data"])

    def test_update_needs_something(self):
        with self.assertRaisesRegex(ValueError, "file_path"):
            self.client.update_caption("C1")

    def test_delete_handles_empty_204_answer(self):
        class Empty:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""
        with mock.patch("urllib.request.urlopen", return_value=Empty()) as opened:
            result = self.client.delete_caption("C1")
        self.assertEqual(result, {"deleted": "C1"})
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertIn("id=C1", request.full_url)

    def test_download_returns_text(self):
        with mock.patch.object(server, "http_raw", return_value=(200, {}, b"\xef\xbb\xbf" + SRT.encode())) as raw:
            result = self.client.download_caption("C/1", "vtt")
        self.assertEqual(result, {"format": "vtt", "content": SRT})
        url = raw.call_args.args[0]
        self.assertTrue(url.endswith("/captions/C%2F1?tfmt=vtt"), url)

    def test_download_saves_file_and_refuses_overwrite(self):
        target = self.dir / "out" / "de.srt"
        with mock.patch.object(server, "http_raw", return_value=(200, {}, SRT.encode())):
            result = self.client.download_caption("C1", "srt", str(target))
            self.assertEqual(result["saved_to"], str(target.resolve()))
            self.assertEqual(target.read_text(encoding="utf-8"), SRT)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                self.client.download_caption("C1", "srt", str(target))

    def test_download_rejects_unknown_format_before_request(self):
        with mock.patch.object(server, "http_raw") as raw:
            with self.assertRaisesRegex(ValueError, "format"):
                self.client.download_caption("C1", "docx")
        raw.assert_not_called()

    def test_dispatch(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "upload_caption", return_value={}) as up:
            srv._call_tool("youtube_upload_caption", {"video_id": "v", "file_path": "/a.srt", "language": "de"})
        up.assert_called_once_with("v", "/a.srt", "de", name="", is_draft=False)
        with mock.patch.object(srv.youtube, "download_caption", return_value={}) as down:
            srv._call_tool("youtube_download_caption", {"caption_id": "c"})
        down.assert_called_once_with("c", "srt", None)


class HttpJsonEmptyTests(unittest.TestCase):
    def test_empty_body_is_empty_dict(self):
        class Empty:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""
        with mock.patch("urllib.request.urlopen", return_value=Empty()):
            self.assertEqual(server.http_json("http://x", method="DELETE"), {})


if __name__ == "__main__":
    unittest.main()
