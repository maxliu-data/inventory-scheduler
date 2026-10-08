import unittest
from unittest.mock import patch

from flask import Flask

from tests.test_inventory_scheduling import request
from utils.scheduling_routes import scheduling_api


class InventoryRoutesTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(scheduling_api)
        self.client = app.test_client()

    def test_json_is_passed_to_inventory_engine(self):
        payload = {"month": "2026-10", "store_requirements": []}
        result = {"status": "complete", "schedule": [], "unscheduled": []}
        with patch("utils.scheduling_routes.InventorySchedulingAgent.generate",
                   return_value=result) as generate:
            response = self.client.post("/inventory_schedule", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, result)
        generate.assert_called_once_with(payload)

    def test_partial_schedule_is_not_an_http_error(self):
        result = {"status": "partial", "schedule": [],
                  "unscheduled": [{"store_ids": ["S1"], "reason": "leader_unavailable"}]}
        with patch("utils.scheduling_routes.InventorySchedulingAgent.generate",
                   return_value=result):
            response = self.client.post("/inventory_schedule", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, result)

    def test_invalid_json_never_calls_engine(self):
        for body in [b"{", b"", b"\xff", b"[" * 2000]:
            with self.subTest(body=body[:20]):
                with patch("utils.scheduling_routes.InventorySchedulingAgent.generate") as generate:
                    response = self.client.post(
                        "/inventory_schedule", data=body, content_type="application/json"
                    )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["status"], "error")
                generate.assert_not_called()

    def test_schema_errors_do_not_leak_internal_details(self):
        with patch("utils.scheduling_routes.InventorySchedulingAgent.generate",
                   side_effect=ValueError("internal employee data")):
            response = self.client.post("/inventory_schedule", json={})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("internal employee data", response.get_data(as_text=True))

    def test_content_type_is_required(self):
        response = self.client.post("/inventory_schedule", data="{}")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json["status"], "error")

    def test_body_size_is_bounded(self):
        with patch("utils.scheduling_routes.InventorySchedulingAgent.generate") as generate:
            response = self.client.post(
                "/inventory_schedule", data=b" " * (2 * 1024 * 1024 + 1),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 413)
        generate.assert_not_called()

    def test_get_is_not_supported(self):
        self.assertEqual(self.client.get("/inventory_schedule").status_code, 405)

    def test_real_engine_assigns_saturday_half_days(self):
        payload = {
            "month": "2026-10",
            "store_requirements": [
                {"store_id": "S1", "required_worker_num": 2},
                {"store_id": "S2", "required_worker_num": 1},
            ],
            "store_calendar": [
                {"store_id": "S1", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 0},
                {"store_id": "S2", "date": "2026-10-03", "allowed_am": 0, "allowed_pm": 1},
            ],
            "store_groups": [
                {"store_group_id": "M1", "store_id": "S1"},
                {"store_group_id": "M1", "store_id": "S2"},
            ],
            "workers": [
                {"worker_id": "W1", "worker_char": "王", "is_leader": 1, "area_id": "A1"},
                {"worker_id": "W2", "worker_char": "林", "is_leader": 0, "area_id": "A1"},
            ],
            "worker_groups": [
                {"group_id": "G1", "worker_id": "W1"},
                {"group_id": "G1", "worker_id": "W2"},
            ],
            "worker_calendar": [
                {"worker_id": "W1", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 1},
                {"worker_id": "W2", "date": "2026-10-03", "allowed_am": 1, "allowed_pm": 1},
            ],
        }
        response = self.client.post("/inventory_schedule", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "complete")
        schedule = {row["store_id"]: row for row in response.json["schedule"]}
        self.assertEqual(schedule["S1"]["slot"], "AM")
        self.assertEqual(set(schedule["S1"]["worker_ids"]), {"W1", "W2"})
        self.assertEqual(schedule["S2"]["slot"], "PM")
        self.assertEqual(schedule["S2"]["worker_ids"], ["W1"])
        self.assertEqual(schedule["S1"]["date"], schedule["S2"]["date"])
        metrics = response.json["metrics"]
        self.assertFalse(metrics["distance_optimization_enabled"])
        self.assertFalse(metrics["distance_optimal"])
        self.assertIsNone(metrics["total_commute_distance_km"])
        self.assertEqual(len(metrics["worker_routes"]), 2)
        self.assertTrue(all(row["distance_km"] is None for row in metrics["worker_routes"]))
        self.assertIn("distance_optimization_disabled: missing coordinates", response.json["warnings"])

    def test_real_engine_exposes_commute_metrics(self):
        payload = request(stores=("S", "T"), workers=("W",))
        payload["store_calendar"][0]["allowed_pm"] = False
        payload["store_calendar"][1]["allowed_am"] = False
        payload["store_locations"][1]["map_x"] = 121.6
        response = self.client.post("/inventory_schedule", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "complete")
        metrics = response.json["metrics"]
        self.assertTrue(metrics["distance_optimization_enabled"])
        self.assertTrue(metrics["distance_optimal"])
        self.assertEqual(metrics["same_team_transitions"], 1)
        self.assertEqual(len(metrics["worker_routes"]), 1)
        route = metrics["worker_routes"][0]
        self.assertEqual((route["worker_id"], route["date"], route["am_store_id"], route["pm_store_id"]),
                         ("W", "2026-10-01", "S", "T"))
        self.assertGreater(route["distance_km"], 0)
        self.assertEqual(route["distance_km"], metrics["total_commute_distance_km"])

    def test_real_engine_rejects_invalid_schema(self):
        for body in [b"null", b"[]", b"{}", b'{"month":"2026-13"}']:
            with self.subTest(body=body):
                response = self.client.post(
                    "/inventory_schedule", data=body, content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["status"], "error")


if __name__ == "__main__":
    unittest.main()
