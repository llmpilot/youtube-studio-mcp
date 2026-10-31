"""End-to-end upload against a local fake YouTube endpoint (real urllib, real sockets)."""

import json
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from helpers import make_client, server


class FakeYouTube(BaseHTTPRequestHandler):
    received = bytearray()
    log: list = []
    fail_once = False

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        meta = json.loads(self.rfile.read(length))
        FakeYouTube.log.append(("start", dict(self.headers), meta))
        self.send_response(200)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/session")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        content_range = self.headers["Content-Range"]
        FakeYouTube.log.append(("put", content_range))
        if FakeYouTube.fail_once and content_range.startswith("bytes 8-"):
            FakeYouTube.fail_once = False
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if content_range.startswith("bytes */"):
            self._resume_incomplete()
            return
        start, rest = content_range[len("bytes "):].split("-")
        end, total = rest.split("/")
        del FakeYouTube.received[int(start):]
        FakeYouTube.received.extend(data)
        if int(end) + 1 == int(total):
            body = json.dumps({"id": "LOCAL1", "status": {"uploadStatus": "uploaded"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._resume_incomplete()

    def _resume_incomplete(self):
        self.send_response(308)
        if FakeYouTube.received:
            self.send_header("Range", f"bytes=0-{len(FakeYouTube.received) - 1}")
        self.send_header("Content-Length", "0")
        self.end_headers()


class LocalUploadTests(unittest.TestCase):
    def setUp(self):
        FakeYouTube.received = bytearray()
        FakeYouTube.log = []
        FakeYouTube.fail_once = False
        self.httpd = HTTPServer(("127.0.0.1", 0), FakeYouTube)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        base = f"http://127.0.0.1:{self.httpd.server_port}"
        for patcher in (
            mock.patch.object(server, "YOUTUBE_UPLOAD_BASE", base),
            mock.patch.object(server, "UPLOAD_CHUNK_SIZE", 4),
            mock.patch.object(server.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.video = pathlib.Path(tmp.name) / "film.mp4"
        self.video.write_bytes(b"0123456789")
        self.client = make_client()

    def test_full_upload(self):
        result = self.client.upload_video(str(self.video), "Titel", description="Beschreibung")
        self.assertEqual(result["id"], "LOCAL1")
        self.assertEqual(result["watchUrl"], "https://youtu.be/LOCAL1")
        self.assertEqual(bytes(FakeYouTube.received), b"0123456789")
        self.assertEqual(FakeYouTube.log[0][2]["snippet"]["title"], "Titel")
        self.assertEqual([e[1] for e in FakeYouTube.log[1:]],
                         ["bytes 0-3/10", "bytes 4-7/10", "bytes 8-9/10"])

    def test_resume_after_server_error(self):
        FakeYouTube.fail_once = True
        result = self.client.upload_video(str(self.video), "Titel")
        self.assertEqual(result["id"], "LOCAL1")
        self.assertEqual(bytes(FakeYouTube.received), b"0123456789")
        ranges = [e[1] for e in FakeYouTube.log[1:]]
        self.assertIn("bytes */10", ranges)


if __name__ == "__main__":
    unittest.main()
