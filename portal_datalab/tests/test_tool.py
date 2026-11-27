from __future__ import annotations

import json
import unittest
from unittest import mock

import portal_datalab


class DatalabToolTests(unittest.TestCase):
    def test_forwards_conversation_and_ignores_runtime_kwargs(self) -> None:
        with mock.patch.object(portal_datalab, "_call_portal", return_value='{"success": true}') as call:
            result = portal_datalab._datalab_query(
                {
                    "tables": [{"path": "/uploads/t/c/local/data.csv", "table": "data"}],
                    "sql": "select count(*) from data",
                    "limit": 20,
                },
                conversation_id="conversation-1",
                task_id="task-1",
                turn_id="turn-1",
            )
        self.assertEqual(json.loads(result), {"success": True})
        payload = call.call_args.args[0]
        self.assertEqual(payload["conversation_id"], "conversation-1")
        self.assertEqual(payload["tables"][0]["table"], "data")
        self.assertEqual(payload["limit"], 20)

    def test_missing_conversation_is_an_error(self) -> None:
        result = portal_datalab._datalab_query(
            {"tables": [{"path": "data.csv"}], "sql": "select 1"}
        )
        self.assertFalse(json.loads(result)["success"])


if __name__ == "__main__":
    unittest.main()
