import copy
import json
import math
import unittest

from tests.test_commute_distance import coords, independent_routes
from tests.test_inventory_scheduling import request
from utils.inventory_scheduling import InventorySchedulingAgent, _Planner


class TeamContinuityTests(unittest.TestCase):
    def generate(self, payload):
        before = copy.deepcopy(payload)
        result = InventorySchedulingAgent().generate(payload)
        self.assertEqual(payload, before)
        self.assertEqual(result, InventorySchedulingAgent().generate(payload))
        json.dumps(result, allow_nan=False)
        self.assertEqual(_Planner.continuity_score(result["schedule"]),
                         result["metrics"]["same_team_transitions"])
        self.assertLessEqual(result["metrics"]["search_nodes"], payload.get("search_limit", 20000))
        return result

    def pair_payload(self):
        payload = request(stores=("S", "T"), workers=("A", "B", "C"), headcount=2)
        payload["store_calendar"][0]["allowed_pm"] = False
        payload["store_calendar"][1]["allowed_am"] = False
        return payload

    def test_intact_team_beats_shorter_split_commute(self):
        payload = self.pair_payload()
        for row, point in zip(payload["workers"], ((0, 0), (0, 0), (1, 0))):
            coords(row, point)
        for row, point in zip(payload["store_locations"], ((0, 0), (1, 0))):
            coords(row, point)
        payload["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A", "B"]}]
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "B"], ["A", "B"]])
        split = copy.deepcopy(result["schedule"])
        split[1]["worker_ids"] = ["A", "C"]
        self.assertLess(math.fsum(independent_routes(payload, split).values()),
                        result["metrics"]["total_commute_distance_km"])
        self.assertEqual(result["metrics"]["same_team_transitions"], 1)
        self.assertTrue(result["metrics"]["team_continuity_optimal"])
        self.assertTrue(result["metrics"]["distance_optimal"])
        self.assertFalse(any(w.startswith("team_split:") for w in result["warnings"]))

    def test_later_complete_incumbent_improves_even_with_zero_or_unknown_distance(self):
        for mode in ("known", "zero", "missing_store", "missing_worker"):
            with self.subTest(mode=mode):
                payload = self.pair_payload()
                payload["store_rules"] = [{"store_id": "T", "required_worker_ids": ["C"]}]
                if mode == "known":
                    payload["store_locations"][1]["map_x"] = 122
                elif mode.startswith("missing"):
                    table = "workers" if mode == "missing_worker" else "store_locations"
                    payload[table][0].pop("map_x")
                    payload[table][0].pop("map_y")
                planner = _Planner(payload)
                first_complete = []
                score = planner.continuity_score

                def record(schedule):
                    value = score(schedule)
                    if len(schedule) == 2 and not first_complete:
                        first_complete.append(value)
                    return value

                planner.continuity_score = record
                result = planner.generate()
                self.assertEqual(first_complete, [0])
                self.assertEqual(result, self.generate(payload))
                self.assertEqual(result["metrics"]["same_team_transitions"], 1)
                self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "C"], ["A", "C"]])
                self.assertTrue(result["metrics"]["team_continuity_optimal"])
                self.assertEqual(result["metrics"]["distance_optimal"], not mode.startswith("missing"))
                self.assertFalse(any(w.startswith("team_split:") for w in result["warnings"]))
                if mode == "zero":
                    self.assertEqual(result["metrics"]["total_commute_distance_km"], 0)

    def test_equal_coverage_skip_branch_improves_without_coordinates(self):
        payload = request(stores=("S", "T", "U"), workers=("A", "B", "C"), headcount=2)
        for row in payload["store_calendar"]:
            row.update(allowed_am=row["store_id"] == "S", allowed_pm=row["store_id"] != "S")
        payload["store_rules"] = [
            {"store_id": "S", "priority": True, "required_worker_ids": ["A", "B"]},
            {"store_id": "T", "priority": True, "required_worker_ids": ["A", "C"]},
            {"store_id": "U", "required_worker_ids": ["A", "B"]},
        ]
        payload["store_locations"] = []
        result = self.generate(payload)
        self.assertEqual(result["status"], "partial")
        self.assertEqual([r["store_id"] for r in result["schedule"]], ["S", "U"])
        self.assertEqual(result["metrics"]["same_team_transitions"], 1)
        self.assertTrue(result["metrics"]["team_continuity_optimal"])
        self.assertFalse(result["metrics"]["distance_optimal"])

    def test_coverage_beats_intact_team(self):
        payload = request(stores=("S", "T", "U"), workers=("A", "B", "C"), headcount=2)
        payload["workers"][1]["is_leader"] = True
        payload["store_requirements"][2]["required_worker_num"] = 1
        for row in payload["store_calendar"]:
            row.update(allowed_am=row["store_id"] == "S", allowed_pm=row["store_id"] != "S")
        payload["store_rules"] = [
            {"store_id": "S", "required_worker_ids": ["A", "B"]},
            {"store_id": "T", "required_worker_ids": ["A"]},
            {"store_id": "U", "required_worker_ids": ["B"]},
        ]
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "B"], ["A", "C"], ["B"]])
        self.assertEqual(result["metrics"]["same_team_transitions"], 0)
        self.assertTrue(result["metrics"]["team_continuity_optimal"])

    def test_distance_breaks_equal_continuity_tie(self):
        payload = self.pair_payload()
        payload["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A"]},
                                  {"store_id": "T", "required_worker_ids": ["A"]}]
        coords(payload["workers"][1], (120, 24))
        result = self.generate(payload)
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "C"], ["A", "C"]])
        self.assertEqual(result["metrics"]["same_team_transitions"], 1)
        self.assertEqual(result["metrics"]["total_commute_distance_km"], 0)
        self.assertTrue(result["metrics"]["distance_optimal"])

    def test_search_limit_preserves_incumbent_and_does_not_certify_objectives(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                payload = self.pair_payload()
                payload["store_rules"] = [{"store_id": "T", "required_worker_ids": ["C"]}]
                if missing:
                    payload["store_locations"] = []
                payload["search_limit"] = 10
                result = self.generate(payload)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["metrics"]["same_team_transitions"], 0)
                self.assertTrue(result["metrics"]["reached_search_limit"])
                self.assertFalse(result["metrics"]["team_continuity_optimal"])
                self.assertFalse(result["metrics"]["distance_optimal"])
                self.assertTrue(any(w.startswith("search_limit:") for w in result["warnings"]))
                payload["search_limit"] = 17
                proven = self.generate(payload)
                self.assertEqual(proven["metrics"]["same_team_transitions"], 1)
                self.assertFalse(proven["metrics"]["reached_search_limit"])
                self.assertTrue(proven["metrics"]["team_continuity_optimal"])
                self.assertEqual(proven["metrics"]["distance_optimal"], not missing)

    def test_multiple_intact_pairs_reach_whole_problem_bound(self):
        payload = request(stores=("S", "T", "U", "V"), workers=("A", "B", "C", "D"), headcount=2)
        payload["workers"][2]["is_leader"] = True
        payload["worker_groups"] = [{"worker_id": w, "group_id": "X" if w in "AB" else "Y"}
                                    for w in ("A", "B", "C", "D")]
        for row in payload["store_calendar"]:
            row.update(allowed_am=row["store_id"] in ("S", "T"),
                       allowed_pm=row["store_id"] in ("U", "V"))
        result = self.generate(payload)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["metrics"]["same_team_transitions"], 2)
        self.assertTrue(result["metrics"]["team_continuity_optimal"])
        self.assertTrue(result["metrics"]["distance_optimal"])
        self.assertFalse(result["metrics"]["reached_search_limit"])

    def test_exact_score_is_pure_and_requires_same_date_and_different_stores(self):
        planner = _Planner(self.pair_payload())
        warnings = set(planner.warnings)
        am = {"store_id": "S", "date": "2026-10-01", "slot": "AM", "worker_ids": ["A", "B"]}
        for changes, expected in (({}, 1), ({"worker_ids": ["A"]}, 0),
                                  ({"worker_ids": ["A", "B", "C"]}, 0),
                                  ({"worker_ids": ["A", "C"]}, 0),
                                  ({"date": "2026-10-02"}, 0), ({"store_id": "S"}, 0)):
            with self.subTest(changes=changes):
                pm = {**am, "slot": "PM", "store_id": "T", "worker_ids": ["B", "A"], **changes}
                schedule = [am, pm]
                before = copy.deepcopy(schedule)
                self.assertEqual(planner.continuity_score(schedule), expected)
                self.assertEqual(planner.warnings, warnings)
                self.assertEqual(schedule, before)

    def test_group_identity_and_cross_date_reuse_are_not_intact_pairs(self):
        for same_date in (True, False):
            with self.subTest(same_date=same_date):
                payload = request(stores=("S", "T"), workers=("A", "B", "C"), headcount=2,
                                  days=("2026-10-01", "2026-10-02"))
                payload["store_calendar"] = [
                    {"store_id": "S", "date": "2026-10-01", "allowed_am": True, "allowed_pm": False},
                    {"store_id": "T", "date": "2026-10-01" if same_date else "2026-10-02",
                     "allowed_am": False, "allowed_pm": True},
                ]
                payload["store_rules"] = [
                    {"store_id": "S", "required_worker_ids": ["A", "B"]},
                    {"store_id": "T", "required_worker_ids": ["A", "C"] if same_date else ["A", "B"]},
                ]
                self.assertEqual({r["group_id"] for r in payload["worker_groups"]}, {"G"})
                result = self.generate(payload)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["metrics"]["same_team_transitions"], 0)
                self.assertEqual(result["metrics"]["split_team_transitions"], int(same_date))
                self.assertEqual(len(result["metrics"]["transitions"]), int(same_date))
                self.assertTrue(result["metrics"]["team_continuity_optimal"])

    def test_unequal_headcounts_still_schedule_without_exact_continuity(self):
        for am_size, pm_size in ((2, 1), (1, 2)):
            with self.subTest(am_size=am_size, pm_size=pm_size):
                payload = self.pair_payload()
                payload["store_requirements"][0]["required_worker_num"] = am_size
                payload["store_requirements"][1]["required_worker_num"] = pm_size
                result = self.generate(payload)
                self.assertEqual(result["status"], "complete")
                self.assertEqual([len(r["worker_ids"]) for r in result["schedule"]], [am_size, pm_size])
                self.assertEqual(result["metrics"]["same_team_transitions"], 0)
                self.assertEqual(result["metrics"]["split_team_transitions"], 1)

    def test_run_describes_lexicographic_objectives_and_missing_coordinates(self):
        text = InventorySchedulingAgent().run(json.dumps(self.pair_payload()))
        coverage = text.index("優先安排最多門市")
        continuity = text.index("再最大化同日上下午完整同隊轉店次數")
        commute = text.index("最後最小化")
        self.assertLess(coverage, continuity)
        self.assertLess(continuity, commute)
        self.assertIn("缺少座標僅停用距離最佳化", text)


if __name__ == "__main__":
    unittest.main()
