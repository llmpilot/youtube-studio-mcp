import unittest
from unittest import mock

from helpers import make_client, server

API_ITEMS = {
    "items": [
        {"id": "1", "snippet": {"title": "Film & Animation", "assignable": True}},
        {"id": "18", "snippet": {"title": "Kurzfilme", "assignable": False}},
        {"id": "27", "snippet": {"title": "Bildung", "assignable": True}},
    ]
}


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def fetch(self, *args, **kwargs):
        with mock.patch.object(self.client, "_request", return_value=API_ITEMS) as req:
            result = self.client.list_categories(*args, **kwargs)
        return result, req

    def test_defaults_target_germany_in_german(self):
        result, req = self.fetch()
        self.assertEqual(req.call_args.args[1], "/videoCategories")
        self.assertEqual(req.call_args.kwargs["query"], {"part": "snippet", "regionCode": "DE", "hl": "de"})
        self.assertEqual((result["region"], result["language"]), ("DE", "de"))

    def test_only_assignable_categories_by_default(self):
        result, _ = self.fetch()
        self.assertEqual(result["categories"], [
            {"id": "1", "title": "Film & Animation", "assignable": True},
            {"id": "27", "title": "Bildung", "assignable": True},
        ])

    def test_all_categories_on_request(self):
        result, _ = self.fetch(assignable_only=False)
        self.assertEqual([c["id"] for c in result["categories"]], ["1", "18", "27"])
        self.assertFalse(result["categories"][1]["assignable"])

    def test_region_is_upper_cased(self):
        result, req = self.fetch("us", "en-US")
        self.assertEqual(req.call_args.kwargs["query"]["regionCode"], "US")
        self.assertEqual(result["region"], "US")

    def test_empty_answer(self):
        with mock.patch.object(self.client, "_request", return_value={}):
            self.assertEqual(self.client.list_categories()["categories"], [])

    def test_invalid_input_sends_nothing(self):
        with mock.patch.object(self.client, "_request") as req:
            for args in (("DEU",), ("1",), ("",), ("DE", "deutsch!"), ("DE", ""), ("DE", "de", "yes"), (5,)):
                with self.assertRaises(ValueError, msg=args):
                    self.client.list_categories(*args)
        req.assert_not_called()

    def test_costs_one_unit(self):
        self.client.quota = server.QuotaTracker(None)
        with mock.patch.object(server, "http_json", return_value=API_ITEMS):
            self.client.list_categories()
        self.assertEqual(self.client.quota.used, 1)

    def test_tool_and_dispatch(self):
        srv = server.McpServer()
        tool = {t["name"]: t for t in srv.tools}["youtube_list_categories"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertNotIn("required", tool["inputSchema"])
        with mock.patch.object(srv.youtube, "list_categories", return_value={}) as lc:
            srv._call_tool("youtube_list_categories", {})
            srv._call_tool("youtube_list_categories", {"region_code": "AT", "assignable_only": False})
        self.assertEqual(lc.call_args_list[0].args, ("DE", "de", True))
        self.assertEqual(lc.call_args_list[1].args, ("AT", "de", False))


if __name__ == "__main__":
    unittest.main()
