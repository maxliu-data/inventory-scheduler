import copy
import json
import math
import random
import unittest

from utils.inventory_scheduling import InventorySchedulingAgent, haversine_km


def request(stores=("S",), workers=("A", "B"), days=("2026-10-01",), headcount=1):
    return {
        "month": days[0][:7],
        "store_requirements": [{"store_id": s, "required_worker_num": headcount} for s in stores],
        "store_calendar": [{"store_id": s, "date": d, "allowed_am": True, "allowed_pm": True}
                           for s in stores for d in days],
        "workers": [{"worker_id": w, "worker_char": w, "is_leader": w == workers[0],
                     "area_id": "north", "map_x": 121.5, "map_y": 25.0} for w in workers],
        "worker_groups": [{"worker_id": w, "group_id": "G"} for w in workers],
        "worker_calendar": [{"worker_id": w, "date": d, "allowed_am": True, "allowed_pm": True}
                            for w in workers for d in days],
        "store_locations": [{"store_id": s, "map_x": 121.5, "map_y": 25.0} for s in stores],
    }


class InventorySchedulingTests(unittest.TestCase):
    def setUp(self):
        self.agent = InventorySchedulingAgent()

    def generate(self, p):
        before = copy.deepcopy(p)
        result = self.agent.generate(p)
        self.assertEqual(p, before)
        json.dumps(result, allow_nan=False)
        return result

    def rule(self, p, **fields):
        p["store_rules"] = [{"store_id": p["store_requirements"][0]["store_id"], **fields}]
        return p

    def test_exact_headcount_leader_and_determinism(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C"), headcount=2)
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result, self.generate(p))
        for row in result["schedule"]:
            self.assertEqual(len(row["worker_ids"]), 2)
            self.assertIn("A", row["worker_ids"])
            self.assertEqual(row["leader_id"], "A")
        self.assertEqual([r["slot"] for r in result["schedule"]], ["AM", "PM"])

    def test_missing_slots_are_unavailable(self):
        for table in ("worker_calendar", "store_calendar"):
            p = request()
            p[table] = []
            self.assertEqual(self.generate(p)["status"], "partial")

    def test_leader_shortage_never_emits_partial_team(self):
        p = request(headcount=2)
        p["workers"][0]["is_leader"] = False
        self.assertEqual(self.generate(p)["schedule"], [])
        p["workers"][0]["is_leader"] = True
        p["store_requirements"][0]["required_worker_num"] = 3
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_calendar_disjoint(self):
        p = request()
        p["store_calendar"][0]["allowed_pm"] = False
        for row in p["worker_calendar"]:
            row["allowed_am"] = False
        self.assertEqual(self.generate(p)["status"], "partial")

    def test_actual_weekdays_and_sunday_saturday(self):
        p = request(days=("2026-10-03", "2026-10-04"))
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-03")
        p = request(days=("2026-10-04",))
        self.assertEqual(self.generate(p)["status"], "partial")
        p = request(days=("2026-10-01", "2026-10-02", "2026-10-03"))
        self.rule(p, weekdays=[1, 3, 5])
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-02")
        self.rule(p, weekdays=[2, 4, 6])
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-01")

    def test_multiple_memberships_need_global_intersection(self):
        p = request(workers=("A", "B", "C"), headcount=3)
        p["worker_groups"] = [{"worker_id": w, "group_id": g}
                              for w, groups in (("A", ("X", "Y")), ("B", ("Y", "Z")),
                                                ("C", ("X", "Z"))) for g in groups]
        self.assertEqual(self.generate(p)["status"], "partial")
        p["worker_groups"].append({"worker_id": "C", "group_id": "Y"})
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_am_pm_different_teams(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C", "D"), headcount=2)
        p["workers"][2]["is_leader"] = True
        p["worker_groups"] = [{"worker_id": w, "group_id": "X" if w in "AB" else "Y"}
                              for w in ("A", "B", "C", "D")]
        for row in p["worker_calendar"]:
            row["allowed_am"] = row["worker_id"] in "AB"
            row["allowed_pm"] = row["worker_id"] in "CD"
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "B"], ["C", "D"]])

    def test_am_pm_can_regroup(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C"), headcount=2)
        self.rule(p, required_worker_ids=["B"])
        p["store_rules"].append({"store_id": "T", "required_worker_ids": ["C"]})
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual({tuple(r["worker_ids"]) for r in result["schedule"]}, {("A", "B"), ("A", "C")})

    def test_atomic_group_same_day(self):
        p = request(stores=("S", "T"), days=("2026-10-01", "2026-10-02"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        for row in p["store_calendar"]:
            row["allowed_am"] = row["allowed_pm"] = (
                row["date"] == ("2026-10-01" if row["store_id"] == "S" else "2026-10-02"))
        result = self.generate(p)
        self.assertEqual(result["schedule"], [])
        self.assertEqual(result["unscheduled"][0]["store_ids"], ["S", "T"])
        p["store_calendar"][-2]["allowed_am"] = True
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len({r["date"] for r in result["schedule"]}), 1)

    def test_atomic_group_recovers_scarce_pair(self):
        p = request(stores=("S", "T"), workers=("A", "B"))
        p["workers"][1]["is_leader"] = True
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_calendar"][0]["allowed_pm"] = False
        p["store_calendar"][1]["allowed_am"] = False
        self.rule(p, required_worker_ids=["A"])
        p["store_rules"].append({"store_id": "T", "required_worker_ids": ["B"]})
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_group_backtracks_slot_to_free_afternoon(self):
        p = request(stores=("S", "T"), workers=("A",))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_calendar"][1]["allowed_am"] = False
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual({r["store_id"]: r["slot"] for r in result["schedule"]},
                         {"S": "AM", "T": "PM"})

    def test_backtracking_reassigns_earlier_group_team(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C"))
        for worker in p["workers"]:
            worker["is_leader"] = True
        for row in p["store_calendar"]:
            row["allowed_pm"] = False
        p["store_rules"] = [{"store_id": "T", "required_worker_ids": ["A"]}]
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        by_store = {r["store_id"]: r["worker_ids"] for r in result["schedule"]}
        self.assertEqual(by_store["T"], ["A"])
        self.assertNotEqual(by_store["S"], ["A"])

    def test_ultra_remote_reserves_entire_day(self):
        p = request(stores=("S", "T"), workers=("A",))
        self.rule(p, ultra_remote=True)
        result = self.generate(p)
        self.assertEqual(len(result["schedule"]), 1)
        if result["schedule"][0]["store_id"] == "S":
            self.assertEqual(result["schedule"][0]["slot"], "AM")
        p = self.rule(request(workers=("A",)), ultra_remote=True)
        p["worker_calendar"][0]["allowed_pm"] = False
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_ultra_remote_prevents_previously_booked_pm(self):
        p = request(stores=("S", "T"), workers=("A",))
        p["store_calendar"][0]["allowed_am"] = False
        p["store_rules"] = [{"store_id": "T", "ultra_remote": True}]
        self.assertEqual(len(self.generate(p)["schedule"]), 1)

    def test_takeover_pm_siblings_am_and_am_conflict(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        self.rule(p, takeover_date="2026-10-01", takeover_slot="PM")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual({r["store_id"]: r["slot"] for r in result["schedule"]}, {"S": "PM", "T": "AM"})
        p["store_rules"][0]["takeover_slot"] = "AM"
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_non_due_takeover_member_still_constrains_due_sibling(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_rules"] = [{"store_id": "S", "store_type": "RC",
                             "last_inventory_date": "2026-10-01",
                             "takeover_date": "2026-10-01", "takeover_slot": "PM"}]
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["schedule"][0]["slot"], "AM")
        p["store_rules"][0]["takeover_slot"] = "AM"
        self.assertEqual(self.generate(p)["status"], "partial")

    def test_takeover_outside_requested_month_does_not_constrain_inventory(self):
        for takeover_date in ("2026-09-01", "2026-11-01"):
            for slot in ("AM", "PM"):
                for grouped in (False, True):
                    with self.subTest(date=takeover_date, slot=slot, grouped=grouped):
                        p = request(stores=("S", "T") if grouped else ("S",))
                        if grouped:
                            p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
                        self.rule(p, takeover_date=takeover_date, takeover_slot=slot)
                        result = self.generate(p)
                        self.assertEqual(result["status"], "complete")
                        self.assertEqual({r["date"] for r in result["schedule"]}, {"2026-10-01"})
                        self.assertEqual(len(result["schedule"]), 2 if grouped else 1)

    def test_meeting_and_holiday_optin(self):
        p = request(workers=("A",))
        p["store_calendar"][0]["allowed_am"] = False
        p["workers"][0]["meeting_dates"] = ["2026-10-01"]
        self.assertEqual(self.generate(p)["schedule"], [])
        p["workers"][0]["meeting_dates"] = []
        p["holidays"] = ["2026-10-01"]
        self.assertEqual(self.generate(p)["schedule"], [])
        p["workers"][0]["holiday_available"] = 1
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_support_department_sections_never_relaxes_groups(self):
        p = request(headcount=2)
        p["organizational_support"] = True
        for w in p["workers"]:
            w.update(department_id="department1", section_id="other")
        self.rule(p, department_id="department1", section_id="store")
        self.assertEqual(self.generate(p)["status"], "complete")
        p["store_rules"][0]["department_id"] = "department2"
        for w in p["workers"]:
            w["department_id"] = "department2"
        self.assertEqual(self.generate(p)["schedule"], [])
        for w in p["workers"]:
            w["section_id"] = "store"
        self.assertEqual(self.generate(p)["status"], "complete")
        p["worker_groups"][1]["group_id"] = "other"
        self.assertEqual(self.generate(p)["schedule"], [])
        del p["workers"][0]["section_id"]
        with self.assertRaises(ValueError):
            self.generate(p)

    def test_required_worker_is_hard_constraint(self):
        p = self.rule(request(headcount=2), required_worker_ids=["B"])
        p["worker_calendar"][1]["allowed_am"] = False
        p["worker_calendar"][1]["allowed_pm"] = False
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_rc_fourteen_day_boundary_and_same_month(self):
        for last, expected in (("2026-09-18", "partial"), ("2026-09-17", "complete")):
            p = self.rule(request(), store_type="RC", last_inventory_date=last)
            self.assertEqual(self.generate(p)["status"], expected)
        p = self.rule(request(days=("2026-10-20",)), store_type="RC",
                      last_inventory_date="2026-10-01")
        self.assertEqual(self.generate(p)["skipped"], [{"store_id": "S", "reason": "not_due"}])

    def test_rc_conversion_exception_explicit(self):
        p = self.rule(request(), store_type="RC", last_inventory_date="2026-09-28",
                      converted_date="2026-09-29")
        self.assertEqual(self.generate(p)["status"], "partial")
        p["store_rules"][0]["conversion_interval_exception"] = True
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_fc_strict_interval_boundaries(self):
        for last, expected in (("2026-08-17", "partial"), ("2026-08-16", "complete"),
                               ("2026-07-19", "complete"), ("2026-07-18", "partial")):
            p = self.rule(request(), store_type="FC", cycle_months=[2, 4, 6, 8, 10, 12],
                          last_inventory_date=last)
            self.assertEqual(self.generate(p)["status"], expected, last)

    def test_fc_alternation_cross_year(self):
        p = self.rule(request(days=("2027-01-02",)), store_type="FC",
                      last_inventory_date="2026-11-02")
        self.assertEqual(self.generate(p)["status"], "complete")
        p = self.rule(request(days=("2026-12-01",)), store_type="FC",
                      last_inventory_date="2026-11-02")
        self.assertEqual(self.generate(p)["skipped"][0]["reason"], "not_due")

    def test_new_rc_next_month_and_fc_first_override(self):
        p = self.rule(request(), store_type="RC", opened_date="2026-09-15")
        self.assertEqual(self.generate(p)["status"], "complete")
        p["store_rules"][0]["opened_date"] = "2026-10-01"
        self.assertEqual(self.generate(p)["schedule"], [])
        p = self.rule(request(), store_type="FC", renewed_date="2026-09-29",
                      last_inventory_date="2026-09-20")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(any("interval_override" in w for w in result["warnings"]))

    def test_converted_fc_first_next_month_with_explicit_override(self):
        p = self.rule(request(), store_type="FC", converted_date="2026-09-29",
                      last_inventory_date="2026-09-28", cycle_months=[1, 3, 5, 7, 9, 11])
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(any("interval_override" in w for w in result["warnings"]))
        del p["store_rules"][0]["last_inventory_date"]
        del p["store_rules"][0]["cycle_months"]
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_count_later_in_event_month_does_not_cancel_mandatory_fc_next_month(self):
        for event_field in ("opened_date", "converted_date", "renewed_date"):
            with self.subTest(event_field=event_field):
                p = self.rule(request(), store_type="FC", last_inventory_date="2026-09-20",
                              **{event_field: "2026-09-01"})
                result = self.generate(p)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(len(result["schedule"]), 1)
                self.assertTrue(any("interval_override" in w for w in result["warnings"]))

    def test_count_in_mandatory_month_fulfills_obligation_without_duplicate(self):
        for kind in ("RC", "FC"):
            with self.subTest(kind=kind):
                p = self.rule(request(days=("2026-10-20",)), store_type=kind,
                              opened_date="2026-09-01", last_inventory_date="2026-10-01")
                result = self.generate(p)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["schedule"], [])
                self.assertEqual(result["skipped"], [{"store_id": "S", "reason": "not_due"}])
                self.assertFalse(any("first_inventory_overdue" in w for w in result["warnings"]))

    def test_future_renewal_does_not_suppress_existing_fc_current_cycle(self):
        p = self.rule(request(days=("2026-10-15",)), store_type="FC",
                      last_inventory_date="2026-08-15", renewed_date="2026-11-15")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["schedule"][0]["date"], "2026-10-15")
        self.assertEqual(result["skipped"], [])
        self.assertFalse(any("interval_override" in w for w in result["warnings"]))
        p = self.rule(request(days=("2026-12-01",)), store_type="FC",
                      last_inventory_date="2026-08-15", renewed_date="2026-11-15")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["schedule"]), 1)
        self.assertTrue(any("interval_override" in w for w in result["warnings"]))

    def test_future_renewal_deferral_requires_established_operation(self):
        p = self.rule(request(days=("2026-10-15",)), store_type="FC",
                      cycle_months=[2, 4, 6, 8, 10, 12], renewed_date="2026-11-15")
        self.assertEqual(len(self.generate(p)["schedule"]), 1)
        del p["store_rules"][0]["cycle_months"]
        self.assertEqual(self.generate(p)["schedule"], [])
        for event in ("opened_date", "converted_date"):
            with self.subTest(event=event):
                p = self.rule(request(days=("2026-10-15",)), store_type="FC",
                              last_inventory_date="2026-08-15", renewed_date="2026-12-15",
                              **{event: "2026-11-15"})
                self.assertEqual(self.generate(p)["schedule"], [])

    def test_all_multi_fc_events_use_first_cycle_strictly_after_event_month(self):
        for field in ("opened_date", "converted_date", "renewed_date"):
            for event, expected in (("2026-08-15", True), ("2026-09-15", True),
                                    ("2026-10-01", False), ("2026-11-01", False)):
                with self.subTest(field=field, event=event):
                    p = request(stores=("S", "T"))
                    p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
                    p["store_rules"] = [
                        {"store_id": "S", "store_type": "FC", "last_inventory_date": "2026-08-01"},
                        {"store_id": "T", "store_type": "FC", "first_store_id": "S", field: event},
                    ]
                    result = self.generate(p)
                    self.assertEqual(result["status"], "complete")
                    self.assertEqual("T" in {r["store_id"] for r in result["schedule"]}, expected)

    def test_multi_converted_fc_first_cycle_crosses_year(self):
        p = request(stores=("S", "T"), days=("2027-01-02",))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_rules"] = [
            {"store_id": "S", "store_type": "FC", "last_inventory_date": "2026-11-02"},
            {"store_id": "T", "store_type": "FC", "first_store_id": "S",
             "converted_date": "2026-11-30"},
        ]
        self.assertEqual(self.generate(p)["status"], "complete")
        self.assertEqual(len(self.generate(p)["schedule"]), 2)

    def test_overdue_rc_is_due_and_missing_fc_first_history_is_not_skipped(self):
        p = self.rule(request(), store_type="RC", opened_date="2026-08-15")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["schedule"]), 1)
        self.assertTrue(any("first_inventory_overdue" in w for w in result["warnings"]))
        p["store_rules"][0]["store_type"] = "FC"
        result = self.generate(p)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["unscheduled"][0]["reason"], "first_inventory_overdue")
        p["store_rules"][0]["last_inventory_date"] = "2026-09-02"
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["skipped"][0]["reason"], "not_due")

    def test_multi_new_fc_follows_first_store_cycle_and_override(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_rules"] = [
            {"store_id": "S", "store_type": "FC", "last_inventory_date": "2026-08-01"},
            {"store_id": "T", "store_type": "FC", "first_store_id": "S",
             "opened_date": "2026-09-28", "last_inventory_date": "2026-09-27"},
        ]
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(any("interval_override" in w for w in result["warnings"]))

    def test_designated_first_store_month_overrides_interval(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_rules"] = [
            {"store_id": "S", "store_type": "FC", "last_inventory_date": "2026-08-01"},
            {"store_id": "T", "store_type": "FC", "first_store_id": "S",
             "last_inventory_date": "2026-09-01"},
        ]
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(any("interval_override" in w for w in result["warnings"]))

    def test_future_multi_store_opening_cannot_schedule_prematurely(self):
        p = request(stores=("S", "T"), days=("2026-10-01", "2026-10-02"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_rules"] = [
            {"store_id": "S", "store_type": "FC", "last_inventory_date": "2026-08-01"},
            {"store_id": "T", "store_type": "FC", "first_store_id": "S",
             "opened_date": "2026-11-01"},
        ]
        result = self.generate(p)
        self.assertEqual([r["store_id"] for r in result["schedule"]], ["S"])
        self.assertEqual(result["skipped"], [{"store_id": "T", "reason": "not_due"}])
        p["store_rules"][1]["opened_date"] = "2026-10-02"
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([r["store_id"] for r in result["schedule"]], ["S"])
        self.assertEqual(result["skipped"], [{"store_id": "T", "reason": "not_due"}])

    def test_missing_fc_metadata_is_validation_error(self):
        with self.assertRaises(ValueError):
            self.generate(self.rule(request(), store_type="FC"))

    def test_first_inventory_month_takes_precedence_over_fc_cycle_parity(self):
        p = self.rule(request(), store_type="FC", opened_date="2026-09-01",
                      last_inventory_date="2026-08-01", cycle_months=[1, 3, 5, 7, 9, 11])
        self.assertEqual(self.generate(p)["status"], "complete")

    def test_only_due_members_participate_in_atomic_group(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        self.rule(p, store_type="FC", last_inventory_date="2026-09-01")
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([r["store_id"] for r in result["schedule"]], ["T"])

    def test_event_deadline_am_and_priority(self):
        p = self.rule(request(days=("2026-10-01", "2026-10-02")), status="terminated",
                      status_date="2026-10-01")
        result = self.generate(p)
        self.assertEqual((result["schedule"][0]["date"], result["schedule"][0]["slot"]),
                         ("2026-10-01", "AM"))
        p["store_calendar"][0]["allowed_am"] = False
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_generic_priority_is_not_am_only(self):
        p = self.rule(request(), priority=True)
        p["store_calendar"][0]["allowed_am"] = False
        self.assertEqual(self.generate(p)["schedule"][0]["slot"], "PM")
        p = request(stores=("A", "Z"), workers=("W",))
        p["store_rules"] = [{"store_id": "Z", "priority": True}]
        for row in p["store_calendar"]:
            row["allowed_pm"] = False
        self.assertEqual([r["store_id"] for r in self.generate(p)["schedule"]], ["Z"])

    def test_all_event_status_deadlines_and_conversion_deadline(self):
        for status in ("closed", "transfer", "terminated", "renewal"):
            with self.subTest(status=status):
                p = self.rule(request(days=("2026-10-02",)), status=status,
                              status_date="2026-10-01")
                self.assertEqual(self.generate(p)["status"], "partial")
        p = self.rule(request(days=("2026-10-16",)), store_type="RC",
                      converted_date="2026-09-15", last_inventory_date="2026-09-10",
                      status_date="2026-10-15", conversion_interval_exception=True)
        self.assertEqual(self.generate(p)["status"], "partial")

    def test_renovation_three_working_days_include_saturday(self):
        p = self.rule(request(days=("2026-10-06", "2026-10-07", "2026-10-08")),
                      renovation_start="2026-10-12", renovation_end="2026-10-20")
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-08")
        p["holidays"] = ["2026-10-10"]
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-07")

    def test_renovation_blackout_inclusive_and_after(self):
        p = self.rule(request(days=("2026-10-20", "2026-10-21", "2026-10-24")),
                      renovation_start="2026-10-12", renovation_end="2026-10-20")
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-21")
        p["store_calendar"][1]["allowed_am"] = p["store_calendar"][1]["allowed_pm"] = False
        self.assertEqual(self.generate(p)["schedule"], [])

    def test_renovation_year_boundary_and_expiry_blacklist(self):
        p = self.rule(request(days=("2026-12-29", "2026-12-30", "2026-12-31")),
                      renovation_start="2027-01-02", renovation_end="2027-01-10",
                      expiry_check_dates=["2026-12-30"])
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-12-31")

    def test_month_part_is_hard(self):
        p = self.rule(request(days=("2026-10-01", "2026-10-15", "2026-10-22")),
                      month_part="middle")
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-15")

    def test_regional_middle_and_out_of_region_late_preferences(self):
        p = request(days=("2026-10-01", "2026-10-15", "2026-10-22"))
        p["store_areas"] = [{"store_id": "S", "area_id": "north"}]
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-15")
        p["store_areas"][0]["area_id"] = "south"
        self.assertEqual(self.generate(p)["schedule"][0]["date"], "2026-10-22")

    def test_distance_preference_selects_nearby_coworker(self):
        p = request(workers=("A", "B", "C"), headcount=2)
        p["workers"][1]["map_x"] = 0
        self.assertEqual(self.generate(p)["schedule"][0]["worker_ids"], ["A", "C"])

    def test_intact_team_objective_outweighs_ten_kilometer_slot_preference(self):
        p = request(stores=("S", "T"))
        p["workers"][1]["is_leader"] = True
        p["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A"]}]
        near = self.generate(p)
        self.assertEqual({r["store_id"]: r["slot"] for r in near["schedule"]},
                         {"S": "AM", "T": "PM"})
        p["store_locations"][1]["map_x"] = 0
        far = self.generate(p)
        self.assertEqual({r["store_id"]: r["slot"] for r in far["schedule"]},
                         {"S": "AM", "T": "PM"})
        self.assertEqual(far["metrics"]["same_team_transitions"], 1)
        self.assertEqual(far["metrics"]["long_distance_transitions"], 1)
        self.assertTrue(any(w.startswith("transition_over_10km:") for w in far["warnings"]))

    def test_same_team_preference_keeps_both_members_across_half_days(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C"), headcount=2)
        p["workers"][2]["map_x"] = 0
        result = self.generate(p)
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "B"], ["A", "B"]])
        self.assertEqual([r["slot"] for r in result["schedule"]], ["AM", "PM"])

    def test_actual_am_team_preferred_over_lexicographically_first_group(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C", "D"), headcount=2)
        p["workers"][2]["is_leader"] = True
        p["worker_groups"] = [{"worker_id": w, "group_id": "Z" if w in "AB" else "A"}
                              for w in ("A", "B", "C", "D")]
        p["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A", "B"]}]
        p["store_calendar"][0]["allowed_pm"] = False
        p["store_calendar"][1]["allowed_am"] = False
        result = self.generate(p)
        self.assertEqual([r["worker_ids"] for r in result["schedule"]], [["A", "B"], ["A", "B"]])
        self.assertEqual(result["metrics"]["same_team_transitions"], 1)

    def test_group_continuity_ranks_partial_team_before_group_id(self):
        p = request(stores=("S", "T"), workers=("A", "B", "C", "D", "E", "F"), headcount=2)
        p["workers"][2]["is_leader"] = True
        p["worker_groups"] = [{"worker_id": w, "group_id": "Z" if w in "ABE" else "A"}
                              for w in ("A", "B", "C", "D", "E", "F")]
        p["store_requirements"][1]["required_worker_num"] = 3
        p["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A", "B"]}]
        p["store_calendar"][0]["allowed_pm"] = False
        p["store_calendar"][1]["allowed_am"] = False
        result = self.generate(p)
        self.assertEqual(result["schedule"][1]["worker_ids"], ["A", "B", "E"])
        self.assertEqual(result["metrics"]["split_team_transitions"], 1)
        self.assertTrue(any(w.startswith("team_split:") for w in result["warnings"]))

    def test_nearest_pending_store_wins_over_store_id(self):
        p = request(stores=("A", "B-far", "Z-near"), workers=("W",))
        p["store_calendar"][0]["allowed_pm"] = False
        for row in p["store_calendar"][1:]:
            row["allowed_am"] = False
        p["store_locations"][1]["map_x"] = 0
        p["store_locations"][2]["map_x"] = 121.51
        result = self.generate(p)
        self.assertEqual({r["store_id"] for r in result["schedule"]}, {"A", "Z-near"})
        self.assertLess(result["metrics"]["transitions"][0]["distance_km"], 10)

    def test_pm_chooses_nearest_actual_am_team_not_union(self):
        p = request(stores=("A-far", "B-near", "C-pm"), workers=("A", "B"))
        p["workers"][1]["is_leader"] = True
        p["worker_groups"] = [{"worker_id": "A", "group_id": "A"},
                              {"worker_id": "B", "group_id": "Z"}]
        p["store_rules"] = [{"store_id": "A-far", "required_worker_ids": ["A"]},
                            {"store_id": "B-near", "required_worker_ids": ["B"]}]
        for row in p["store_calendar"][:2]:
            row["allowed_pm"] = False
        p["store_calendar"][2]["allowed_am"] = False
        p["store_locations"][0]["map_x"] = 0
        result = self.generate(p)
        by_store = {r["store_id"]: r for r in result["schedule"]}
        self.assertEqual(by_store["C-pm"]["worker_ids"], ["B"])
        self.assertEqual(result["metrics"]["transitions"][0]["am_store_id"], "B-near")

    def test_actual_transition_distance_warns_for_far_or_unknown(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                p = request(stores=("S", "T"), workers=("W",))
                p["store_calendar"][0]["allowed_pm"] = False
                p["store_calendar"][1]["allowed_am"] = False
                if unknown:
                    p["store_locations"] = []
                else:
                    p["store_locations"][1]["map_x"] = 0
                result = self.generate(p)
                metric = "unknown_distance_transitions" if unknown else "long_distance_transitions"
                warning = "unknown_transition_distance:" if unknown else "transition_over_10km:"
                self.assertEqual(result["metrics"][metric], 1)
                self.assertTrue(any(w.startswith(warning) for w in result["warnings"]))
                distance = result["metrics"]["transitions"][0]["distance_km"]
                self.assertIsNone(distance) if unknown else self.assertGreater(distance, 10)

    def test_unrelated_pm_team_has_no_invented_transition_distance(self):
        p = request(stores=("S", "T"), workers=("A", "B"))
        p["workers"][1]["is_leader"] = True
        p["store_rules"] = [{"store_id": "S", "required_worker_ids": ["A"]},
                            {"store_id": "T", "required_worker_ids": ["B"]}]
        p["store_calendar"][0]["allowed_pm"] = False
        p["store_calendar"][1]["allowed_am"] = False
        result = self.generate(p)
        self.assertEqual(result["metrics"]["transitions"], [])
        self.assertEqual(result["metrics"]["pm_teams_without_am_continuity"], 1)
        self.assertTrue(any(w.startswith("pm_team_without_am_continuity:") for w in result["warnings"]))

    def test_missing_history_is_explicitly_unchecked(self):
        p = self.rule(request(), store_type="FC", cycle_months=[2, 4, 6, 8, 10, 12])
        result = self.generate(p)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(any("cycle_interval_unchecked" in w for w in result["warnings"]))
        self.assertTrue(any("missing history is not inferred" in n for n in result["notes"]))

    def test_unknown_coordinates_are_diagnosed(self):
        p = request()
        p.pop("store_locations")
        p["workers"][0].pop("map_x")
        p["workers"][0].pop("map_y")
        result = self.generate(p)
        self.assertTrue(any("unknown_store_distance" in w for w in result["warnings"]))
        self.assertTrue(any("unknown_worker_distance" in w for w in result["warnings"]))

    def test_partial_vs_search_limit_and_atomic_limit(self):
        p = request(stores=("S", "T"), workers=("A",))
        for row in p["store_calendar"]:
            row["allowed_pm"] = False
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "no_feasible_assignment")
        p["search_limit"] = 1
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "search_limit")
        self.assertTrue(result["metrics"]["reached_search_limit"])
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        for budget in range(1, 8):
            p["search_limit"] = budget
            result = self.generate(p)
            self.assertIn(len(result["schedule"]), (0, 2))

    def test_static_store_slot_and_group_date_diagnostics(self):
        p = request()
        p["store_calendar"] = []
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "no_store_allowed_slots")
        self.assertEqual(result["unscheduled"][0]["diagnostics"],
                         [{"store_id": "S", "reason": "no_store_allowed_slots"}])
        p = request(stores=("S", "T"), days=("2026-10-01", "2026-10-02"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        p["store_calendar"] = [row for row in p["store_calendar"]
                               if (row["store_id"] == "S") == (row["date"] == "2026-10-01")]
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "no_common_store_date")

    def test_static_staffing_shortage_diagnostics(self):
        p = request(headcount=3)
        self.assertEqual(self.generate(p)["unscheduled"][0]["reason"], "insufficient_available_workers")
        p = request()
        p["workers"][0]["is_leader"] = False
        self.assertEqual(self.generate(p)["unscheduled"][0]["reason"], "no_available_leader")
        p = request(headcount=2)
        p["worker_groups"][1]["group_id"] = "other"
        self.assertEqual(self.generate(p)["unscheduled"][0]["reason"], "no_common_worker_group")
        p = self.rule(request(), required_worker_ids=["B"])
        p["worker_calendar"][1]["allowed_am"] = False
        p["worker_calendar"][1]["allowed_pm"] = False
        self.assertEqual(self.generate(p)["unscheduled"][0]["reason"], "required_worker_unavailable")
        p = self.rule(request(), required_worker_ids=["B"])
        self.assertEqual(self.generate(p)["unscheduled"][0]["reason"], "no_common_group_leader")

    def test_combinatorial_conflict_is_not_mislabeled_as_static_shortage(self):
        p = request(stores=("S", "T"), workers=("W",))
        for row in p["store_calendar"]:
            row["allowed_pm"] = False
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "no_feasible_assignment")
        self.assertEqual(result["unscheduled"][0]["diagnostics"], [])

    def test_static_takeover_conflict_is_visible_in_chat(self):
        p = request(stores=("S", "T"))
        p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
        self.rule(p, takeover_date="2026-10-01", takeover_slot="AM")
        result = self.generate(p)
        self.assertEqual(result["unscheduled"][0]["reason"], "takeover_slot_conflict")
        self.assertIn("takeover_slot_conflict", self.agent.run(json.dumps(p)))

    def test_validation_bad_flags_dates_references_duplicates(self):
        edits = [
            lambda p: p.update(month="2026-13"),
            lambda p: p["workers"][0].update(is_leader="false"),
            lambda p: p["store_calendar"][0].update(allowed_am="1"),
            lambda p: p["worker_calendar"][0].update(date="2026-02-30"),
            lambda p: p["worker_calendar"][0].update(date="2026-11-01"),
            lambda p: p["worker_calendar"][0].update(worker_id="unknown"),
            lambda p: p["worker_groups"][0].update(worker_id="unknown"),
            lambda p: p["store_requirements"].append(dict(p["store_requirements"][0])),
            lambda p: p["workers"].append(dict(p["workers"][0])),
            lambda p: p["worker_groups"].append(dict(p["worker_groups"][0])),
            lambda p: p["store_calendar"].append(dict(p["store_calendar"][0])),
            lambda p: p.update(store_groups=[{"store_id": "S", "store_group_id": "A"},
                                             {"store_id": "S", "store_group_id": "B"}]),
            lambda p: p.update(store_locations=[{"store_id": "unknown", "map_x": 0, "map_y": 0}]),
            lambda p: p["workers"][0].update(map_x=float("nan")),
            lambda p: p["workers"][0].update(map_x=None),
            lambda p: p.update(allow_sunday=True),
            lambda p: p.update(search_limit=200001),
            lambda p: p["store_requirements"][0].update(required_worker_num=True),
            lambda p: p.update(store_rules=[{"store_id": "S", "required_worker_ids": ["unknown"]}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "takeover_date": "2026-10-01"}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "renovation_start": "2026-10-10",
                                             "renovation_end": "2026-10-01"}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "weekdays": [True]}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "conversion_interval_exception": True}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "last_inventory_date": "2026-09-01"}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "store_type": "FC",
                                             "first_store_id": "unknown"}]),
            lambda p: p.update(store_rules=[{"store_id": "S", "store_type": "FC",
                                             "cycle_months": [1, 2, 3, 4, 5, 6]}]),
            lambda p: p["workers"][0].update(holiday_available="yes"),
        ]
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                p = request()
                edit(p)
                with self.assertRaises(ValueError):
                    self.generate(p)

    def test_table_and_search_bounds(self):
        for field, value in (("workers", [{}] * 201), ("store_requirements", [{}] * 101),
                             ("worker_calendar", [{}] * 6201), ("worker_groups", [{}] * 2001),
                             ("search_limit", 0), ("search_limit", True), ("holidays", ["x"] * 367)):
            with self.subTest(field=field):
                p = request()
                p[field] = value
                with self.assertRaises(ValueError):
                    self.generate(p)

    def test_random_small_schedules_never_violate_hard_constraints(self):
        rng = random.Random(219)
        for _ in range(40):
            p = request(stores=("S", "T", "U"), workers=("A", "B", "C", "D"),
                        days=("2026-10-01", "2026-10-02"))
            for w in p["workers"]:
                w["is_leader"] = rng.choice((True, False))
            for row in p["worker_calendar"] + p["store_calendar"]:
                row["allowed_am"] = rng.choice((True, False))
                row["allowed_pm"] = rng.choice((True, False))
            for row in p["store_requirements"]:
                row["required_worker_num"] = rng.randint(1, 3)
            p["worker_groups"] = [{"worker_id": w, "group_id": g} for w in ("A", "B", "C", "D")
                                  for g in ("X", "Y") if rng.choice((True, False))]
            p["store_groups"] = [{"store_id": s, "store_group_id": "M"} for s in ("S", "T")]
            result = self.generate(p)
            self.assertEqual(result, self.generate(p))
            reservations = set()
            done = {r["store_id"]: r for r in result["schedule"]}
            self.assertEqual("S" in done, "T" in done)
            if "S" in done:
                self.assertEqual(done["S"]["date"], done["T"]["date"])
            for row in result["schedule"]:
                required = next(r["required_worker_num"] for r in p["store_requirements"]
                                if r["store_id"] == row["store_id"])
                self.assertEqual(len(row["worker_ids"]), required)
                self.assertTrue(next(w["is_leader"] for w in p["workers"]
                                     if w["worker_id"] == row["leader_id"]))
                common = None
                for worker in row["worker_ids"]:
                    groups = {r["group_id"] for r in p["worker_groups"] if r["worker_id"] == worker}
                    common = groups if common is None else common & groups
                    reservation = (worker, row["date"], row["slot"])
                    self.assertNotIn(reservation, reservations)
                    reservations.add(reservation)
                    worker_slot = next(r for r in p["worker_calendar"]
                                       if r["worker_id"] == worker and r["date"] == row["date"])
                    self.assertTrue(worker_slot["allowed_" + row["slot"].lower()])
                self.assertTrue(common)
                store_slot = next(r for r in p["store_calendar"]
                                  if r["store_id"] == row["store_id"] and r["date"] == row["date"])
                self.assertTrue(store_slot["allowed_" + row["slot"].lower()])

    def test_run_escapes_identifiers_and_no_source_parser_delimiters(self):
        p = request(stores=("<script>[x](y)</script>",))
        output = self.agent.run(json.dumps(p))
        self.assertIn("&lt;script&gt;", output)
        for char in "[]()":
            self.assertNotIn(char, output)
        self.assertNotIn("<script>", output)
        self.assertIn("無效", self.agent.run("not JSON"))

    def test_run_discloses_concrete_warnings_and_unscheduled_reasons(self):
        p = self.rule(request(), store_type="FC", converted_date="2026-09-29",
                      last_inventory_date="2026-09-28")
        self.assertIn("fc_first_inventory_interval_override", self.agent.run(json.dumps(p)))
        p["search_limit"] = 1
        self.assertIn("原因：search_limit", self.agent.run(json.dumps(p)))
        p = self.rule(request(), store_type="FC", opened_date="2026-08-01")
        self.assertIn("原因：first_inventory_overdue", self.agent.run(json.dumps(p)))
        p = request(stores=("S", "<T>[x](y)"), workers=("A", "B"), headcount=2)
        p["store_requirements"][1]["required_worker_num"] = 1
        p["store_calendar"][0]["allowed_pm"] = False
        p["store_calendar"][1]["allowed_am"] = False
        p["store_locations"][1]["map_x"] = 0
        output = self.agent.run(json.dumps(p))
        self.assertIn("team_split", output)
        self.assertIn("transition_over_10km", output)
        self.assertIn("&lt;T&gt;［x］（y）", output)
        for char in "[]()":
            self.assertNotIn(char, output)
        p["store_locations"] = []
        output = self.agent.run(json.dumps(p))
        self.assertIn("unknown_store_distance", output)
        self.assertIn("unknown_transition_distance", output)

    def test_empty_requirements_and_year_extremes(self):
        p = request(stores=())
        self.assertEqual(self.generate(p)["status"], "complete")
        for day in ("0001-01-01", "9999-12-31"):
            p = request(days=(day,))
            self.assertEqual(self.generate(p)["status"], "complete")


class HaversineTests(unittest.TestCase):
    def test_distance_and_boundaries(self):
        self.assertEqual(haversine_km(0, 0, 0, 0), 0)
        self.assertAlmostEqual(haversine_km(0, 0, 180, 0), math.pi * 6371)
        self.assertAlmostEqual(haversine_km(-180, -90, 180, 90), math.pi * 6371)
        self.assertAlmostEqual(haversine_km(0, 0, 1, 0), 111.19492664455873)
        self.assertIsNone(haversine_km(None, None, 0, 0))
        self.assertIsNone(haversine_km(None, 1, 0, 0))

    def test_invalid_coordinates(self):
        for value in (float("nan"), float("inf"), -181, 181, "0", True, 10 ** 500):
            with self.subTest(value=value), self.assertRaises(ValueError):
                haversine_km(value, 0, 0, 0)
        with self.assertRaises(ValueError):
            haversine_km(0, 91, 0, 0)
        with self.assertRaises(ValueError):
            haversine_km(None, float("nan"), 0, 0)


if __name__ == "__main__":
    unittest.main()
