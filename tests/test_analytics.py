import unittest
from unittest import mock

from helpers import make_client, server

REPORT = {
    "columnHeaders": [
        {"name": "video", "columnType": "DIMENSION", "dataType": "STRING"},
        {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
    ],
    "rows": [["abc", 120], ["def", 80]],
}


class AnalyticsQueryTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def run_query(self, response=None, **kwargs):
        kwargs.setdefault("start_date", "2026-10-01")
        kwargs.setdefault("end_date", "2026-10-28")
        kwargs.setdefault("metrics", ["views"])
        with mock.patch.object(self.client, "_request", return_value=response or REPORT) as req:
            result = self.client.analytics_query(**kwargs)
        return result, req

    def test_query_string_is_built_from_lists(self):
        _, req = self.run_query(
            metrics=["views", "estimatedMinutesWatched"],
            dimensions=["video"],
            filters={"country": "DE"},
            sort=["-views"],
            max_results=10,
            start_index=1,
        )
        self.assertEqual(req.call_args.args[:2], (server.YOUTUBE_ANALYTICS_BASE, "/reports"))
        query = req.call_args.kwargs["query"]
        self.assertEqual(query, {
            "ids": "channel==MINE",
            "startDate": "2026-10-01",
            "endDate": "2026-10-28",
            "metrics": "views,estimatedMinutesWatched",
            "dimensions": "video",
            "filters": "country==DE",
            "sort": "-views",
            "maxResults": 10,
            "startIndex": 1,
        })

    def test_optional_parts_are_omitted(self):
        _, req = self.run_query()
        query = req.call_args.kwargs["query"]
        for key in ("dimensions", "filters", "sort", "maxResults", "startIndex"):
            self.assertIsNone(query[key], key)

    def test_comma_string_inputs_work(self):
        _, req = self.run_query(metrics="views, likes", dimensions="day,country", sort="-views,day")
        query = req.call_args.kwargs["query"]
        self.assertEqual((query["metrics"], query["dimensions"], query["sort"]), ("views,likes", "day,country", "-views,day"))

    def test_filters_string_list_and_object_forms(self):
        for value in ("video==ID;country==DE", ["video==ID", "country==DE"], {"video": "ID", "country": "DE"}):
            _, req = self.run_query(filters=value)
            self.assertEqual(req.call_args.kwargs["query"]["filters"], "video==ID;country==DE", value)

    def test_multiple_values_in_one_filter(self):
        _, req = self.run_query(filters="video==a,b,c")
        self.assertEqual(req.call_args.kwargs["query"]["filters"], "video==a,b,c")

    def test_result_is_compact(self):
        result, _ = self.run_query(dimensions=["video"])
        self.assertEqual(result["columns"], ["video", "views"])
        self.assertEqual(result["rows"], [["abc", 120], ["def", 80]])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["query"]["dimensions"], ["video"])

    def test_empty_report(self):
        result, _ = self.run_query(response={"columnHeaders": [{"name": "views"}]})
        self.assertEqual((result["rows"], result["row_count"]), ([], 0))

    def test_invalid_input_sends_nothing(self):
        base = {"start_date": "2026-10-01", "end_date": "2026-10-28", "metrics": ["views"]}
        bad = [
            {"start_date": "01.10.2026"},
            {"end_date": "2026-13-45"},
            {"start_date": "2026-11-01", "end_date": "2026-10-01"},
            {"metrics": []},
            {"metrics": ""},
            {"metrics": ["views,likes;drop"]},
            {"metrics": ["views&ids=other"]},
            {"metrics": 5},
            {"dimensions": ["day;x"]},
            {"sort": ["--views"]},
            {"filters": "video"},
            {"filters": "video==a;bad"},
            {"filters": {"video": "a;country==DE"}},
            {"max_results": 0},
            {"start_index": 0},
        ]
        with mock.patch.object(self.client, "_request") as req:
            for override in bad:
                with self.assertRaises(ValueError, msg=override):
                    self.client.analytics_query(**{**base, **override})
        req.assert_not_called()

    def test_analytics_costs_no_data_api_quota(self):
        self.client.quota = server.QuotaTracker(None)
        with mock.patch.object(server, "http_json", return_value=REPORT):
            self.client.analytics_query("2026-10-01", "2026-10-28", ["views"])
        self.assertEqual(self.client.quota.used, 0)

    def test_dispatch(self):
        srv = server.McpServer()
        with mock.patch.object(srv.youtube, "analytics_query", return_value={}) as q:
            srv._call_tool("youtube_analytics_query",
                           {"start_date": "2026-10-01", "end_date": "2026-10-28", "metrics": ["views"], "sort": ["-views"]})
        q.assert_called_once_with("2026-10-01", "2026-10-28", ["views"], None, None, ["-views"], None, None)

    def test_tool_schema_requires_dates_and_metrics(self):
        tool = {t["name"]: t for t in server.McpServer().tools}["youtube_analytics_query"]
        self.assertEqual(tool["inputSchema"]["required"], ["start_date", "end_date", "metrics"])
        self.assertTrue(tool["annotations"]["readOnlyHint"])

    def test_bad_request_gets_a_hint(self):
        text = server.explain_error(server.ApiError(400, "Unknown identifier (foo)", "badRequest"))
        self.assertIn("metrics", text)
        self.assertIn("Unknown identifier (foo)", text)


if __name__ == "__main__":
    unittest.main()
