import unittest

from helpers import (
    INIT,
    INITIALIZED,
    TOOLS_LIST,
    framed,
    ndjson,
    parse_framed,
    parse_ndjson,
    run_stdio,
    server,
)

EXPECTED_TOOLS = {
    "youtube_auth_status",
    "youtube_start_auth",
    "youtube_channel_overview",
    "youtube_list_videos",
    "youtube_get_video",
    "youtube_update_video",
    "youtube_upload_video",
    "youtube_upload_thumbnail",
    "youtube_channel_analytics",
    "youtube_video_analytics",
    "youtube_post_comment",
    "youtube_list_comments",
}


class ProtocolTests(unittest.TestCase):
    def test_ndjson_roundtrip(self):
        out = parse_ndjson(run_stdio(ndjson(INIT, INITIALIZED, TOOLS_LIST)))
        self.assertEqual([m["id"] for m in out], [1, 2])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "youtube-studio-mcp")
        self.assertIn("tools", out[0]["result"]["capabilities"])

    def test_content_length_roundtrip(self):
        out = parse_framed(run_stdio(framed(INIT, INITIALIZED, TOOLS_LIST)))
        self.assertEqual([m["id"] for m in out], [1, 2])

    def test_ndjson_reply_has_no_headers(self):
        raw = run_stdio(ndjson(INIT))
        self.assertTrue(raw.startswith(b"{"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"Content-Length", raw)

    def test_notification_gets_no_reply(self):
        self.assertEqual(run_stdio(ndjson(INITIALIZED)), b"")

    def test_ping(self):
        out = parse_ndjson(run_stdio(ndjson({"jsonrpc": "2.0", "id": 7, "method": "ping"})))
        self.assertEqual(out, [{"jsonrpc": "2.0", "id": 7, "result": {}}])

    def test_blank_lines_are_ignored(self):
        out = parse_ndjson(run_stdio(b"\n\n" + ndjson(INIT)))
        self.assertEqual(out[0]["id"], 1)

    def test_tools_list_names_and_schemas(self):
        out = parse_ndjson(run_stdio(ndjson(INIT, TOOLS_LIST)))
        tools = out[1]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, EXPECTED_TOOLS)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])
            self.assertTrue(tool["description"], tool["name"])

    def test_unknown_method(self):
        out = parse_ndjson(run_stdio(ndjson({"jsonrpc": "2.0", "id": 3, "method": "nope"})))
        self.assertEqual(out[0]["error"]["code"], -32601)

    def test_unknown_tool_is_an_error(self):
        call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "youtube_nope", "arguments": {}}}
        out = parse_ndjson(run_stdio(ndjson(call)))
        self.assertIn("Unknown tool", out[0]["error"]["message"])

    def test_auth_status_tool_call(self):
        call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "youtube_auth_status", "arguments": {}}}
        out = parse_ndjson(run_stdio(ndjson(call)))
        text = out[0]["result"]["content"][0]["text"]
        self.assertIn("client_secrets_exists", text)

    def test_every_tool_is_dispatchable(self):
        """Each advertised tool must be handled by _call_tool (not 'Unknown tool')."""
        srv = server.McpServer()
        for tool in srv.tools:
            try:
                srv._call_tool(tool["name"], {})
            except RuntimeError as exc:
                self.assertNotIn("Unknown tool", str(exc), tool["name"])
            except Exception:  # missing arguments / no network are fine here
                pass


if __name__ == "__main__":
    unittest.main()
