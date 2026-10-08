import copy
import itertools
import json
import math
import unittest

from tests.test_inventory_scheduling import request
from utils.inventory_scheduling import InventorySchedulingAgent


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    value = (math.sin((lat2 - lat1) / 2) ** 2 +
             math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 12742 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def coords(row, point):
    row.update(map_x=point[0], map_y=point[1])


def independent_routes(payload, schedule):
    homes = {w["worker_id"]: (w["map_x"], w["map_y"]) for w in payload["workers"]}
    stores = {s["store_id"]: (s["map_x"], s["map_y"]) for s in payload["store_locations"]}
    routes = {}
    for row in schedule:
        for worker in row["worker_ids"]:
            routes.setdefault((worker, row["date"]), {})[row["slot"]] = row["store_id"]
    totals = {}
    for (worker, day), stops in routes.items():
        points = [homes[worker]] + [stores[stops[slot]] for slot in ("AM", "PM") if slot in stops]
        points.append(homes[worker])
        totals[worker, day] = math.fsum(distance(a, b) for a, b in zip(points, points[1:]))
    return totals


class CommuteDistanceTests(unittest.TestCase):
    def generate(self, payload):
        before = copy.deepcopy(payload)
        result = InventorySchedulingAgent().generate(payload)
        self.assertEqual(payload, before)
        self.assertEqual(result, InventorySchedulingAgent().generate(payload))
        json.dumps(result, allow_nan=False)
        return result

    def assert_routes(self, payload, result):
        expected = independent_routes(payload, result["schedule"])
        metrics = result["metrics"]
        self.assertAlmostEqual(metrics["total_commute_distance_km"], math.fsum(expected.values()))
        self.assertEqual(len(metrics["worker_routes"]), len(expected))
        for route in metrics["worker_routes"]:
            self.assertAlmostEqual(route["distance_km"], expected[route["worker_id"], route["date"]])
            for slot in ("am", "pm"):
                matches = [r["store_id"] for r in result["schedule"]
                           if r["date"] == route["date"] and r["slot"] == slot.upper() and
                           route["worker_id"] in r["worker_ids"]]
                self.assertEqual(route[slot + "_store_id"], matches[0] if matches else None)

    def farther_first_payload(self):
        payload = request(workers=("A-far", "Z-near"))
        for worker, point in zip(payload["workers"], ((120, 24), (121.49, 25.01))):
            worker["is_leader"] = True
            coords(worker, point)
        payload["worker_groups"] = [{"worker_id": worker, "group_id": worker}
                                    for worker in ("A-far", "Z-near")]
        payload["store_calendar"][0]["allowed_pm"] = False
        return payload

    def test_improves_after_first_complete_and_preserves_budget_incumbent(self):
        payload = self.farther_first_payload()
        payload["search_limit"] = 5
        first = self.generate(payload)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["schedule"][0]["worker_ids"], ["A-far"])
        self.assertTrue(first["metrics"]["reached_search_limit"])
        self.assertFalse(first["metrics"]["distance_optimal"])
        self.assertTrue(any(w.startswith("search_limit:") for w in first["warnings"]))
        self.assert_routes(payload, first)
        payload["search_limit"] = 8
        improved = self.generate(payload)
        self.assertEqual(improved["status"], "complete")
        self.assertEqual(improved["schedule"][0]["worker_ids"], ["Z-near"])
        self.assertTrue(improved["metrics"]["reached_search_limit"])
        self.assertFalse(improved["metrics"]["distance_optimal"])
        payload["search_limit"] = 20000
        best = self.generate(payload)
        self.assertEqual(best["schedule"][0]["worker_ids"], ["Z-near"])
        self.assertLess(best["metrics"]["total_commute_distance_km"],
                        first["metrics"]["total_commute_distance_km"])
        self.assertTrue(best["metrics"]["distance_optimal"])
        self.assert_routes(payload, best)

    def test_nearest_eligible_worker_is_tried_with_small_budget(self):
        payload = self.farther_first_payload()
        for membership in payload["worker_groups"]:
            membership["group_id"] = "G"
        payload["search_limit"] = 5
        result = self.generate(payload)
        self.assertEqual(result["schedule"][0]["worker_ids"], ["Z-near"])
        self.assertFalse(result["metrics"]["distance_optimal"])

    def test_single_half_day_and_remote_have_two_home_legs(self):
        for slot, remote in (("AM", False), ("PM", False), ("AM", True)):
            with self.subTest(slot=slot, remote=remote):
                payload = request(workers=("W",))
                coords(payload["workers"][0], (120, 24))
                payload["store_calendar"][0].update(allowed_am=slot == "AM", allowed_pm=slot == "PM")
                if remote:
                    payload["store_rules"] = [{"store_id": "S", "ultra_remote": True}]
                result = self.generate(payload)
                self.assertEqual(result["status"], "complete")
                self.assert_routes(payload, result)
                self.assertAlmostEqual(result["metrics"]["total_commute_distance_km"],
                                       2 * distance((120, 24), (121.5, 25)))

    def test_counts_each_worker_on_each_day_including_split_teams(self):
        payload = request(stores=("S", "T", "U"), workers=("A", "B", "C"),
                          days=("2026-10-01", "2026-10-02"), headcount=2)
        for row, point in zip(payload["workers"], ((0, 0), (0.2, 0.4), (0.8, 0.1))):
            coords(row, point)
        for row, point in zip(payload["store_locations"], ((0.1, 0.2), (0.4, 0.5), (1, 1))):
            coords(row, point)
        payload["store_calendar"] = [
            {"store_id": "S", "date": "2026-10-01", "allowed_am": True, "allowed_pm": False},
            {"store_id": "T", "date": "2026-10-01", "allowed_am": False, "allowed_pm": True},
            {"store_id": "U", "date": "2026-10-02", "allowed_am": True, "allowed_pm": False},
        ]
        payload["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A", "B"]},
                                  {"store_id": "T", "required_worker_ids": ["A", "C"]},
                                  {"store_id": "U", "required_worker_ids": ["A", "B"]}]
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["metrics"]["worker_routes"]), 5)
        self.assertEqual(result["metrics"]["split_team_transitions"], 1)
        self.assert_routes(payload, result)

    def test_return_home_cost_can_outweigh_shortest_store_transition(self):
        payload = request(stores=("S", "T", "U"), workers=("A", "B"))
        for row, point in zip(payload["workers"], ((0, 0), (0, 0.01))):
            coords(row, point)
            row["is_leader"] = True
        for row, point in zip(payload["store_locations"], ((0, 0), (1, 0), (0, 0.01))):
            coords(row, point)
        for row in payload["store_calendar"]:
            row.update(allowed_am=row["store_id"] != "U", allowed_pm=row["store_id"] == "U")
        payload["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A"]},
                                  {"store_id": "T", "required_worker_ids": ["B"]}]
        result = self.generate(payload)
        pm = next(r for r in result["schedule"] if r["slot"] == "PM")
        self.assertEqual(pm["worker_ids"], ["B"])
        self.assertGreater(distance((1, 0), (0, 0.01)), distance((0, 0), (0, 0.01)))
        self.assert_routes(payload, result)

    def test_exhaustive_independent_oracle_noncollinear_coordinates(self):
        for missing_slot in (False, True):
            with self.subTest(missing_slot=missing_slot):
                payload = request(stores=("S", "T", "U"), workers=("A", "B"),
                                  days=("2026-10-01", "2026-10-02"))
                for row, point in zip(payload["workers"], ((0, 0), (0.8, 0.4))):
                    row["is_leader"] = True
                    coords(row, point)
                for row, point in zip(payload["store_locations"], ((0.1, 0.3), (1, 0.1), (0.4, 0.8))):
                    coords(row, point)
                for row in payload["store_calendar"]:
                    row.update(allowed_am=row["store_id"] != "U", allowed_pm=row["store_id"] == "U")
                if missing_slot:
                    payload["worker_calendar"][0]["allowed_am"] = False
                choices = []
                for store in ("S", "T", "U"):
                    options = [None]
                    for row in payload["store_calendar"]:
                        if row["store_id"] != store:
                            continue
                        for worker in payload["worker_calendar"]:
                            if worker["date"] != row["date"]:
                                continue
                            for slot in ("AM", "PM"):
                                if row["allowed_" + slot.lower()] and worker["allowed_" + slot.lower()]:
                                    options.append({"store_id": store, "date": row["date"], "slot": slot,
                                                    "worker_ids": [worker["worker_id"]]})
                    choices.append(options)
                oracle = (0, 0, 0)
                for candidate in itertools.product(*choices):
                    schedule = [r for r in candidate if r is not None]
                    reservations = [(r["worker_ids"][0], r["date"], r["slot"]) for r in schedule]
                    if len(set(reservations)) != len(reservations):
                        continue
                    intact = sum(am["date"] == pm["date"] and
                                 set(am["worker_ids"]) == set(pm["worker_ids"])
                                 for am in schedule if am["slot"] == "AM"
                                 for pm in schedule if pm["slot"] == "PM")
                    objective = (-len(schedule), -intact,
                                 math.fsum(independent_routes(payload, schedule).values()))
                    oracle = min(oracle, objective)
                result = self.generate(payload)
                self.assertTrue(result["metrics"]["distance_optimal"])
                self.assertEqual(-len(result["schedule"]), oracle[0])
                self.assertEqual(-result["metrics"]["same_team_transitions"], oracle[1])
                self.assertAlmostEqual(result["metrics"]["total_commute_distance_km"], oracle[2])
                self.assert_routes(payload, result)

    def test_coverage_beats_zero_distance_partial_schedule(self):
        payload = request(stores=("S", "T"), workers=("W",))
        coords(payload["store_locations"][1], (100, 20))
        payload["store_calendar"][0]["allowed_pm"] = False
        payload["store_calendar"][1]["allowed_am"] = False
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["schedule"]), 2)
        self.assertGreater(result["metrics"]["total_commute_distance_km"], 0)
        self.assert_routes(payload, result)

    def test_equal_coverage_skip_branch_can_improve_partial_schedule(self):
        payload = request(stores=("A-far", "Z-near"), workers=("W",))
        for row in payload["store_calendar"]:
            row["allowed_pm"] = False
        coords(payload["store_locations"][0], (100, 20))
        coords(payload["store_locations"][1], (121.51, 25.01))
        result = self.generate(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["store_id"] for r in result["schedule"]], ["Z-near"])
        self.assertTrue(result["metrics"]["distance_optimal"])
        self.assert_routes(payload, result)

    def test_missing_coordinates_disable_optimization_and_report_unknown_routes(self):
        for table in ("workers", "store_locations"):
            with self.subTest(table=table):
                payload = self.farther_first_payload()
                payload[table][0].pop("map_x")
                payload[table][0].pop("map_y")
                result = self.generate(payload)
                self.assertEqual(result["schedule"][0]["worker_ids"], ["A-far"])
                self.assertFalse(result["metrics"]["distance_optimization_enabled"])
                self.assertFalse(result["metrics"]["distance_optimal"])
                self.assertIsNone(result["metrics"]["total_commute_distance_km"])
                self.assertIsNone(result["metrics"]["worker_routes"][0]["distance_km"])
                self.assertIn("distance_optimization_disabled: missing coordinates", result["warnings"])

    def test_unused_missing_worker_still_disables_optimization_not_known_total(self):
        payload = self.farther_first_payload()
        payload["workers"][1].pop("map_x")
        payload["workers"][1].pop("map_y")
        result = self.generate(payload)
        self.assertEqual(result["schedule"][0]["worker_ids"], ["A-far"])
        self.assertFalse(result["metrics"]["distance_optimization_enabled"])
        self.assertFalse(result["metrics"]["distance_optimal"])
        self.assertGreater(result["metrics"]["total_commute_distance_km"], 0)

    def test_unknown_due_store_disables_even_if_unselected(self):
        payload = request(stores=("S", "T"))
        payload["store_calendar"][1].update(allowed_am=False, allowed_pm=False)
        payload["store_locations"].pop()
        result = self.generate(payload)
        self.assertFalse(result["metrics"]["distance_optimization_enabled"])
        self.assertFalse(result["metrics"]["distance_optimal"])
        self.assertEqual(result["metrics"]["total_commute_distance_km"], 0)

    def test_unknown_route_does_not_erase_other_known_worker_distances(self):
        payload = request(workers=("A", "B"), headcount=2)
        payload["workers"][1].pop("map_x")
        payload["workers"][1].pop("map_y")
        coords(payload["store_locations"][0], (121.6, 25.1))
        result = self.generate(payload)
        routes = {r["worker_id"]: r for r in result["metrics"]["worker_routes"]}
        self.assertGreater(routes["A"]["distance_km"], 0)
        self.assertIsNone(routes["B"]["distance_km"])
        self.assertIsNone(result["metrics"]["total_commute_distance_km"])

    def test_non_due_missing_store_does_not_disable_optimization(self):
        payload = request(stores=("S", "T"))
        payload["store_rules"] = [{"store_id": "T", "store_type": "RC",
                                  "last_inventory_date": "2026-10-01"}]
        payload["store_locations"].pop()
        result = self.generate(payload)
        self.assertTrue(result["metrics"]["distance_optimization_enabled"])
        self.assertTrue(result["metrics"]["distance_optimal"])
        self.assertNotIn("distance_optimization_disabled: missing coordinates", result["warnings"])

    def test_empty_schedule_is_zero_and_disabled_is_not_optimal(self):
        for due in (False, True):
            with self.subTest(due=due):
                payload = request()
                payload["store_calendar"] = []
                payload["workers"][0].pop("map_x")
                payload["workers"][0].pop("map_y")
                if not due:
                    payload["store_rules"] = [{"store_id": "S", "store_type": "RC",
                                              "last_inventory_date": "2026-10-01"}]
                result = self.generate(payload)
                self.assertEqual(result["metrics"]["total_commute_distance_km"], 0)
                self.assertEqual(result["metrics"]["worker_routes"], [])
                self.assertFalse(result["metrics"]["distance_optimal"])
                self.assertEqual("distance_optimization_disabled: missing coordinates" in
                                 result["warnings"], due)

    def test_zero_complete_proves_bound_without_exhausting_search(self):
        payload = request()
        payload["search_limit"] = 5
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["metrics"]["total_commute_distance_km"], 0)
        self.assertTrue(result["metrics"]["distance_optimal"])
        self.assertFalse(result["metrics"]["reached_search_limit"])


if __name__ == "__main__":
    unittest.main()
