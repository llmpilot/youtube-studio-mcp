import io
import json
import unittest
import urllib.error
from unittest import mock

from helpers import INIT, ndjson, parse_ndjson, run_stdio, server


def api_error_body(message, reason=None):
    error = {"code": 403, "message": message}
    if reason:
        error["errors"] = [{"message": message, "reason": reason}]
    return json.dumps({"error": error})


class ParseApiErrorTests(unittest.TestCase):
    def test_api_shape_with_reason(self):
        err = server.parse_api_error(403, api_error_body("Quota", "quotaExceeded"))
        self.assertEqual((err.status, err.reason, str(err)), (403, "quotaExceeded", "Quota"))

    def test_status_used_when_no_reason(self):
        body = json.dumps({"error": {"message": "nope", "status": "PERMISSION_DENIED"}})
        self.assertEqual(server.parse_api_error(403, body).reason, "PERMISSION_DENIED")

    def test_oauth_token_endpoint_shape_does_not_crash(self):
        body = json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        err = server.parse_api_error(400, body)
        self.assertEqual(err.reason, "invalid_grant")
        self.assertIn("expired or revoked", str(err))

    def test_non_json_body(self):
        err = server.parse_api_error(502, "Bad Gateway")
        self.assertEqual((str(err), err.reason), ("Bad Gateway", None))

    def test_empty_body_uses_fallback(self):
        self.assertEqual(str(server.parse_api_error(500, "", "HTTP Error 500")), "HTTP Error 500")

    def test_json_that_is_not_an_object(self):
        self.assertEqual(str(server.parse_api_error(400, "[1,2]")), "[1,2]")

    def test_is_runtime_error_for_backwards_compatibility(self):
        self.assertIsInstance(server.parse_api_error(400, ""), RuntimeError)


class ExplainTests(unittest.TestCase):
    def explain(self, status, reason, message="msg"):
        return server.explain_error(server.ApiError(status, message, reason))

    def test_quota(self):
        for reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            text = self.explain(403, reason)
            self.assertIn("10,000 units", text, reason)
            self.assertIn("midnight Pacific", text)
            self.assertIn(f"HTTP 403 {reason}", text)

    def test_invalid_grant_mentions_testing_mode_and_reauth(self):
        text = self.explain(400, "invalid_grant")
        self.assertIn("7 days", text)
        self.assertIn("auth.py auth", text)

    def test_401_asks_for_reauth(self):
        self.assertIn("auth.py auth", self.explain(401, None))

    def test_insufficient_permissions(self):
        self.assertIn("scopes", self.explain(403, "insufficientPermissions"))

    def test_forbidden_and_comments_disabled(self):
        self.assertIn("refused", self.explain(403, "commentsDisabled"))
        self.assertIn("refused", self.explain(403, None))

    def test_not_found_mentions_id_kinds(self):
        self.assertIn("ids are different", self.explain(404, "videoNotFound"))

    def test_server_error_is_temporary(self):
        self.assertIn("temporary", self.explain(503, "backendError"))

    def test_details_keep_original_message(self):
        self.assertIn("Details: HTTP 400: bad field", self.explain(400, None, "bad field"))

    def test_other_exception_types(self):
        self.assertEqual(server.explain_error(KeyError("video_id")), "Missing required argument: video_id")
        self.assertEqual(server.explain_error(ValueError("title too long")), "Invalid input: title too long")
        self.assertIn("Network problem", server.explain_error(urllib.error.URLError("dns failure")))
        self.assertIn("Network problem", server.explain_error(TimeoutError("timed out")))
        self.assertEqual(server.explain_error(RuntimeError("Video file not found: x")), "Video file not found: x")
        self.assertIn("Unexpected error (ZeroDivisionError)", server.explain_error(ZeroDivisionError("x")))


def call(name, arguments=None):
    message = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
               "params": {"name": name, "arguments": arguments or {}}}
    return parse_ndjson(run_stdio(ndjson(INIT, message)))[1]


class ToolResultTests(unittest.TestCase):
    def test_api_error_becomes_is_error_result(self):
        with mock.patch.object(server.YouTubeClient, "channel_overview",
                               side_effect=server.ApiError(403, "Quota", "quotaExceeded")):
            reply = call("youtube_channel_overview")
        self.assertNotIn("error", reply)
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("quota", reply["result"]["content"][0]["text"].lower())

    def test_missing_argument_is_a_tool_error(self):
        reply = call("youtube_get_video", {})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("video_id", reply["result"]["content"][0]["text"])

    def test_validation_error_is_a_tool_error(self):
        reply = call("youtube_create_playlist", {"title": " "})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("Invalid input", reply["result"]["content"][0]["text"])

    def test_success_has_no_is_error(self):
        reply = call("youtube_auth_status")
        self.assertNotIn("isError", reply["result"])

    def test_unknown_tool_stays_a_protocol_error(self):
        reply = call("youtube_nope")
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertIn("Unknown tool", reply["error"]["message"])

    def test_server_survives_a_failing_call(self):
        bad = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "youtube_get_video", "arguments": {}}}
        good = {"jsonrpc": "2.0", "id": 6, "method": "ping"}
        replies = parse_ndjson(run_stdio(ndjson(INIT, bad, good)))
        self.assertEqual([r["id"] for r in replies], [1, 5, 6])


class HttpIntegrationTests(unittest.TestCase):
    def test_http_json_raises_api_error_with_reason(self):
        err = urllib.error.HTTPError("http://x", 403, "Forbidden", {},
                                     io.BytesIO(api_error_body("Quota", "quotaExceeded").encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(server.ApiError) as ctx:
                server.http_json("http://x")
        self.assertEqual((ctx.exception.status, ctx.exception.reason), (403, "quotaExceeded"))

    def test_token_refresh_failure_is_readable(self):
        body = json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(body.encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(server.ApiError) as ctx:
                server.http_json(server.TOKEN_URL, method="POST", data=b"")
        text = server.explain_error(ctx.exception)
        self.assertIn("auth.py auth", text)
        self.assertIn("expired or revoked", text)

    def test_http_raw_raises_api_error_with_reason(self):
        err = urllib.error.HTTPError("http://x", 403, "Forbidden", {},
                                     io.BytesIO(api_error_body("Too many", "uploadLimitExceeded").encode()))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(server.ApiError) as ctx:
                server.http_raw("http://x", method="POST", headers={})
        self.assertEqual(ctx.exception.reason, "uploadLimitExceeded")


if __name__ == "__main__":
    unittest.main()
