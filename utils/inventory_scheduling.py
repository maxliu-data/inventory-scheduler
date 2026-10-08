"""Bounded scheduling: maximize coverage, keep teams intact, minimize round trips."""

import calendar
import html
import itertools
import json
import math
import re
from datetime import date


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def _flag(value, label):
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ValueError(f"{label} must be bool or 0/1")
    return bool(value)


def _id(value, label):
    if (type(value) not in (str, int) or
            isinstance(value, str) and (not value.strip() or len(value) > 100)):
        raise ValueError(f"{label} must be a nonempty string or integer")
    return value


def _key(value):
    return type(value).__name__, str(value)


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} is not a valid date") from None


def _list(value, label, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{label} must be a list with at most {limit} entries")
    return value


def _coordinate(value, label, bound):
    if type(value) not in (int, float) or not -bound <= value <= bound or not math.isfinite(value):
        raise ValueError(f"{label} must be finite and within ±{bound}")
    return float(value)


def haversine_km(lon1, lat1, lon2, lat2):
    """Great-circle distance, radius 6371 km; incomplete coordinates are unknown."""
    points = []
    for lon, lat in ((lon1, lat1), (lon2, lat2)):
        if lon is not None:
            _coordinate(lon, "longitude", 180)
        if lat is not None:
            _coordinate(lat, "latitude", 90)
        points.append(None if lon is None or lat is None else (lon, lat))
    if None in points:
        return None
    a_lon, a_lat, b_lon, b_lat = map(math.radians, (lon1, lat1, lon2, lat2))
    a = (math.sin((b_lat - a_lat) / 2) ** 2 +
         math.cos(a_lat) * math.cos(b_lat) * math.sin((b_lon - a_lon) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


class _SearchLimit(Exception):
    pass


class InventorySchedulingAgent:
    """Eight-table interface. Calendars are explicit availability, never defaults."""

    def generate(self, payload):
        return _Planner(payload).generate()

    def run(self, query):
        try:
            if not isinstance(query, str) or len(query) > 2_000_000:
                raise ValueError("query must be a bounded JSON string")
            result = self.generate(json.loads(query))
        except (ValueError, TypeError, RecursionError):
            return "盤點排程資料無效，請檢查日期、欄位及參照資料。"
        lines = ["盤點排程：" + ("完成" if result["status"] == "complete" else "部分完成")]
        for row in result["schedule"]:
            lines.append(f"{row['date']} {'上午' if row['slot'] == 'AM' else '下午'} "
                         f"門市 {row['store_id']} 人員 "
                         + "、".join(map(str, row["worker_ids"])))
        for row in result["unscheduled"]:
            lines.append("未排門市：" + "、".join(map(str, row["store_ids"])) +
                         "，原因：" + row["reason"])
            for diagnostic in row.get("diagnostics", []):
                lines.append("限制：" + str(diagnostic.get("store_id", "整組")) +
                             "，" + diagnostic["reason"])
        lines.append("優先安排最多門市，再最大化同日上下午完整同隊轉店次數，最後最小化"
                     "每位人員每日住家往返門市總距離；同隊偏好不阻擋可行排程。"
                     "搜尋達上限時不保證最佳解；缺少座標僅停用距離最佳化，仍優先維持完整同隊。"
                     "缺少業務資料時須由上游確認到期及日曆。")
        lines.extend("提醒：" + warning for warning in result["warnings"])
        text = "\n".join(lines).translate(str.maketrans({"[": "［", "]": "］", "(": "（", ")": "）"}))
        return html.escape(text, quote=True)


class _Planner:
    TABLES = {
        "store_requirements": {"store_id", "required_worker_num"},
        "store_calendar": {"store_id", "date", "allowed_am", "allowed_pm"},
        "store_groups": {"store_group_id", "store_id"},
        "store_locations": {"store_id", "map_x", "map_y"},
        "store_areas": {"area_id", "store_id"},
        "workers": {"worker_char", "worker_id", "is_leader", "area_id", "map_x", "map_y",
                    "department_id", "section_id", "holiday_available", "meeting_dates"},
        "worker_groups": {"group_id", "worker_id"},
        "worker_calendar": {"worker_id", "date", "allowed_am", "allowed_pm"},
        "store_rules": {"store_id", "store_type", "last_inventory_date", "cycle_months",
                        "opened_date", "converted_date", "renewed_date",
                        "conversion_interval_exception", "first_store_id", "takeover_date",
                        "takeover_slot", "renovation_start", "renovation_end",
                        "expiry_check_dates", "weekdays", "month_part", "ultra_remote",
                        "priority", "status", "status_date", "required_worker_ids",
                        "department_id", "section_id"},
    }

    def __init__(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        unknown = set(payload) - (set(self.TABLES) |
                                  {"month", "holidays", "search_limit", "organizational_support"})
        if unknown:
            raise ValueError("unsupported payload fields")
        month = payload.get("month")
        if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("month must be YYYY-MM")
        first = _date(month + "-01", "month")
        self.month = month
        self.month_index = first.year * 12 + first.month - 1
        self.days = [date(first.year, first.month, d)
                     for d in range(1, calendar.monthrange(first.year, first.month)[1] + 1)]
        self.limit = _integer(payload.get("search_limit", 20000), "search_limit", 1, 200000)
        self.nodes = 0
        self.support = _flag(payload.get("organizational_support", False), "organizational_support")
        self.holidays = self.date_set(payload.get("holidays", []), "holidays", 366)
        self.warnings = set()
        self.skipped = []
        self.overdue_first = set()
        tables = {}
        required = {"store_requirements", "store_calendar", "workers", "worker_groups",
                    "worker_calendar"}
        for name, fields in self.TABLES.items():
            if name in required and name not in payload:
                raise ValueError(f"{name} is required")
            cap = 6200 if name.endswith("calendar") else 2000
            if name in ("store_requirements", "store_rules"):
                cap = 100
            if name == "workers":
                cap = 200
            rows = _list(payload.get(name, []), name, cap)
            if any(not isinstance(row, dict) or set(row) - fields for row in rows):
                raise ValueError(f"{name} has invalid rows or unsupported fields")
            tables[name] = rows
        self.stores = self.index(tables["store_requirements"], "store_id")
        self.workers = self.index(tables["workers"], "worker_id")
        self.needs = {s: _integer(row.get("required_worker_num"), "required_worker_num", 1, 200)
                      for s, row in self.stores.items()}
        self.groups = {}
        self.memberships = {w: set() for w in self.workers}
        for row in tables["store_groups"]:
            s = self.ref(row, "store_id", self.stores)
            g = _id(row.get("store_group_id"), "store_group_id")
            if s in self.groups:
                raise ValueError("duplicate or contradictory store group membership")
            self.groups[s] = g
        for row in tables["worker_groups"]:
            w = self.ref(row, "worker_id", self.workers)
            g = _id(row.get("group_id"), "group_id")
            if g in self.memberships[w]:
                raise ValueError("duplicate worker group membership")
            self.memberships[w].add(g)
        self.areas = self.related_index(tables["store_areas"])
        for row in self.areas.values():
            _id(row.get("area_id"), "area_id")
        self.locations = self.related_index(tables["store_locations"])
        self.store_coords = {s: self.coords(self.locations.get(s, {})) for s in self.stores}
        self.worker_coords = {w: self.coords(row) for w, row in self.workers.items()}
        if any(c is None for c in self.store_coords.values()):
            self.warnings.add("unknown_store_distance: missing store coordinates")
        if any(c is None for c in self.worker_coords.values()):
            self.warnings.add("unknown_worker_distance: missing worker coordinates")
        self.leaders = {}
        self.meetings = {}
        self.holiday_optin = {}
        for w, row in self.workers.items():
            self.leaders[w] = _flag(row.get("is_leader"), "is_leader")
            if "worker_char" in row:
                _id(row["worker_char"], "worker_char")
            if "area_id" in row:
                _id(row["area_id"], "area_id")
            self.holiday_optin[w] = _flag(row.get("holiday_available", False), "holiday_available")
            self.meetings[w] = self.date_set(row.get("meeting_dates", []), "meeting_dates", 366)
        self.store_cal = self.calendar(tables["store_calendar"], "store_id", self.stores)
        self.worker_cal = self.calendar(tables["worker_calendar"], "worker_id", self.workers)
        self.rules = self.related_index(tables["store_rules"])
        for s in self.stores:
            self.rules[s] = self.parse_rule(s, self.rules.get(s, {}))
        self.validate_cycles()
        if self.support:
            for row in list(self.workers.values()) + list(self.rules.values()):
                _id(row.get("department_id"), "department_id")
                if row.get("department_id") not in ("department1", "department2", 1, 2):
                    raise ValueError("organizational_support requires department1/department2 metadata")
                _id(row.get("section_id"), "section_id")
        self.available = {}
        self.due = []
        for s in sorted(self.stores, key=_key):
            if not self.is_due(s):
                self.skipped.append({"store_id": s, "reason": "not_due"})
                continue
            self.due.append(s)
            self.available[s] = {(d, slot) for d in self.days for slot in ("AM", "PM")
                                 if self.store_allowed(s, d, slot)}
        units = {}
        for s in self.due:
            units.setdefault(("group", self.groups[s]) if s in self.groups else ("store", s), []).append(s)
        self.units = sorted(units.values(), key=lambda ss: (
            not any(self.priority(s) for s in ss), -len(ss),
            sum(len(self.available[s]) for s in ss), tuple(_key(s) for s in ss)))
        self.booked = set()
        self.current = []
        self.best = []
        self.reached = False
        self.optimize_distance = (all(self.store_coords[s] is not None for s in self.due) and
                                  all(c is not None for c in self.worker_coords.values()))
        if self.due and not self.optimize_distance:
            self.warnings.add("distance_optimization_disabled: missing coordinates")
        self.best_distance = 0.0
        self.best_continuity = 0
        self.continuity_bound = len(self.due) // 2
        self.distance_cache = {}

    @staticmethod
    def index(rows, field):
        result = {}
        for row in rows:
            key = _id(row.get(field), field)
            if key in result:
                raise ValueError(f"duplicate {field}")
            result[key] = dict(row)
        return result

    @staticmethod
    def ref(row, field, known):
        key = _id(row.get(field), field)
        if key not in known:
            raise ValueError(f"unknown {field}")
        return key

    def related_index(self, rows):
        for row in rows:
            self.ref(row, "store_id", self.stores)
        return self.index(rows, "store_id")

    @staticmethod
    def date_set(values, label, limit):
        parsed = [_date(v, label) for v in _list(values, label, limit)]
        if len(set(parsed)) != len(parsed):
            raise ValueError(f"duplicate {label}")
        return set(parsed)

    @staticmethod
    def coords(row):
        lon, lat = row.get("map_x"), row.get("map_y")
        if (lon is None) != (lat is None):
            raise ValueError("map_x and map_y must both be supplied or both absent")
        if lon is None:
            return None
        return _coordinate(lon, "map_x", 180), _coordinate(lat, "map_y", 90)

    def calendar(self, rows, field, known):
        result = {}
        for row in rows:
            key = self.ref(row, field, known)
            d = _date(row.get("date"), "calendar date")
            if d not in self.days:
                raise ValueError("calendar date must be in requested month")
            if (key, d) in result:
                raise ValueError("duplicate calendar row")
            result[key, d] = (_flag(row.get("allowed_am"), "allowed_am"),
                              _flag(row.get("allowed_pm"), "allowed_pm"))
        return result

    def parse_rule(self, s, raw):
        rule = dict(raw)
        for name in ("last_inventory_date", "opened_date", "converted_date", "renewed_date",
                     "takeover_date", "renovation_start", "renovation_end", "status_date"):
            if name in rule:
                rule[name] = _date(rule[name], name)
        for name in ("ultra_remote", "priority", "conversion_interval_exception"):
            rule[name] = _flag(rule.get(name, False), name)
        for name in ("expiry_check_dates",):
            rule[name] = self.date_set(rule.get(name, []), name, 366)
        if "store_type" in rule and rule["store_type"] not in ("RC", "FC"):
            raise ValueError("store_type must be RC or FC")
        cycle_fields = {"last_inventory_date", "cycle_months", "opened_date",
                        "converted_date", "renewed_date", "first_store_id"}
        if cycle_fields.intersection(raw) and "store_type" not in rule:
            raise ValueError("cycle metadata requires store_type")
        if "renewed_date" in rule and rule.get("store_type") != "FC":
            raise ValueError("renewed_date is supported for FC only; use renewal status for RC")
        if "month_part" in rule and rule["month_part"] not in ("early", "middle", "late"):
            raise ValueError("month_part must be early, middle or late")
        if "status" in rule and rule["status"] not in ("normal", "closed", "transfer", "terminated", "renewal"):
            raise ValueError("unsupported store status")
        if rule.get("status", "normal") != "normal" and "status_date" not in rule:
            raise ValueError("event status requires status_date deadline")
        if "weekdays" in rule:
            values = _list(rule["weekdays"], "weekdays", 7)
            for v in values:
                _integer(v, "weekday", 1, 7)
            if len(set(values)) != len(values):
                raise ValueError("duplicate weekday")
        if "cycle_months" in rule:
            values = _list(rule["cycle_months"], "cycle_months", 12)
            for v in values:
                _integer(v, "cycle month", 1, 12)
            if not values or len(set(values)) != len(values):
                raise ValueError("cycle_months must be nonempty and unique")
            if rule.get("store_type") != "FC":
                raise ValueError("cycle_months requires FC store_type")
            if len(values) != 6 or len({m % 2 for m in values}) != 1:
                raise ValueError("FC cycle_months must contain six alternating months")
        if "first_store_id" in rule:
            other = self.ref(rule, "first_store_id", self.stores)
            if other == s or s not in self.groups or self.groups.get(other) != self.groups[s]:
                raise ValueError("first_store_id must be a different store in the same store group")
        if ("takeover_date" in rule) != ("takeover_slot" in rule):
            raise ValueError("takeover_date and takeover_slot are required together")
        if "takeover_slot" in rule and rule["takeover_slot"] not in ("AM", "PM"):
            raise ValueError("takeover_slot must be AM or PM")
        if ("renovation_start" in rule) != ("renovation_end" in rule):
            raise ValueError("renovation_start and renovation_end are required together")
        if "renovation_start" in rule and rule["renovation_start"] > rule["renovation_end"]:
            raise ValueError("renovation_start exceeds renovation_end")
        if "renovation_start" in rule:
            rule["_renovation_dates"] = self.renovation_dates(
                rule["renovation_start"], rule["renovation_end"])
        ids = _list(rule.get("required_worker_ids", []), "required_worker_ids", 200)
        for w in ids:
            _id(w, "required_worker_id")
            if w not in self.workers:
                raise ValueError("unknown required_worker_id")
        if len(set(ids)) != len(ids) or len(ids) > self.needs[s]:
            raise ValueError("required_worker_ids duplicate or exceed headcount")
        rule["required_worker_ids"] = set(ids)
        if rule["conversion_interval_exception"] and (
                rule.get("store_type") != "RC" or "converted_date" not in rule):
            raise ValueError("conversion interval exception requires converted RC")
        return rule

    def validate_cycles(self):
        for s, r in self.rules.items():
            if "store_type" in r and "last_inventory_date" not in r:
                self.warnings.add("cycle_interval_unchecked: missing last_inventory_date for " + str(s))
            first = r.get("first_store_id")
            if first is not None:
                source = self.rules[first]
                if source.get("store_type") != "FC" or source.get("first_store_id") is not None:
                    raise ValueError("designated first store must be FC with its own cycle")
                if "cycle_months" not in source and "last_inventory_date" not in source:
                    raise ValueError("designated first store needs cycle_months or history")
            if r.get("store_type") == "FC" and not any(
                    k in r for k in ("cycle_months", "last_inventory_date", "opened_date",
                                     "converted_date", "renewed_date", "first_store_id")):
                raise ValueError("FC requires cycle metadata or a first inventory event")
            if first is not None and r.get("store_type") != "FC":
                raise ValueError("first_store_id requires FC store_type")
            if (r.get("store_type") == "FC" and s in self.groups and
                    self.pending_event(s) is not None and first is None):
                raise ValueError("new/converted/renewed multi-store FC requires first_store_id")

    def inventory_events(self, s):
        r = self.rules[s]
        fields = ("opened_date", "converted_date", "renewed_date")
        established = any(k in r for k in ("last_inventory_date", "cycle_months"))
        return [r[k] for k in fields if k in r and not (
            k == "renewed_date" and established and r[k] > self.days[-1])]

    def pending_event(self, s):
        r = self.rules[s]
        events = self.inventory_events(s)
        if not events:
            return None
        event = max(events)
        last = r.get("last_inventory_date")
        if last is not None:
            counted_month = last.year * 12 + last.month - 1
            if counted_month >= self.first_inventory_month(s, event):
                return None
        return event

    def first_inventory_month(self, s, event):
        r = self.rules[s]
        target = event.year * 12 + event.month
        if r.get("store_type") == "FC" and "first_store_id" in r:
            months = self.cycle_months(s)
            while target % 12 + 1 not in months:
                target += 1
        return target

    def mandatory_month(self, s):
        event = self.pending_event(s)
        return self.first_inventory_month(s, event) if event is not None else None

    def cycle_months(self, s):
        r = self.rules[s]
        source = self.rules[r["first_store_id"]] if "first_store_id" in r else r
        if "cycle_months" in source:
            return source["cycle_months"]
        last = source.get("last_inventory_date")
        return list(range(1 if last.month % 2 else 2, 13, 2)) if last else None

    def is_due(self, s):
        r = self.rules[s]
        kind = r.get("store_type")
        if not kind:
            return True
        events = self.inventory_events(s)
        if events and max(events) > self.days[-1]:
            return False
        mandatory = self.mandatory_month(s)
        if mandatory is not None:
            if self.month_index < mandatory:
                return False
            if self.month_index > mandatory:
                self.warnings.add("first_inventory_overdue: missing post-event inventory history for " + str(s))
                if kind == "FC":
                    self.overdue_first.add(s)
            return True
        last = r.get("last_inventory_date")
        if last and (last.year, last.month) >= (self.days[0].year, self.days[0].month):
            return False
        if kind == "FC":
            months = self.cycle_months(s)
            if months is None:
                raise ValueError("FC cycle cannot be determined")
            return self.days[0].month in months
        return True

    def priority(self, s):
        r = self.rules[s]
        return r["priority"] or r.get("status", "normal") != "normal"

    def active_takeover(self, s):
        takeover = self.rules[s].get("takeover_date")
        return takeover is not None and self.days[0] <= takeover <= self.days[-1]

    def renovation_dates(self, start, end):
        allowed = set()
        for anchor, direction in ((start, -1), (end, 1)):
            ordinal = anchor.toordinal()
            found = 0
            while found < 3:
                ordinal += direction
                if not date.min.toordinal() <= ordinal <= date.max.toordinal():
                    break
                d = date.fromordinal(ordinal)
                if d.weekday() != 6 and d not in self.holidays:
                    allowed.add(d)
                    found += 1
        return allowed

    def store_allowed(self, s, d, slot):
        r = self.rules[s]
        if s in self.overdue_first:
            return False
        if d.weekday() == 6 or not self.store_cal.get((s, d), (False, False))[slot == "PM"]:
            return False
        if any(d < event for event in self.inventory_events(s)):
            return False
        if d in r["expiry_check_dates"]:
            return False
        if "weekdays" in r and d.isoweekday() not in r["weekdays"]:
            return False
        part = "early" if d.day <= 10 else "middle" if d.day <= 20 else "late"
        if "month_part" in r and r["month_part"] != part:
            return False
        if slot == "PM" and (r["ultra_remote"] or r.get("status", "normal") != "normal"):
            return False
        if "status_date" in r and d > r["status_date"]:
            return False
        if self.active_takeover(s) and (d != r["takeover_date"] or slot != r["takeover_slot"]):
            return False
        if "renovation_start" in r:
            if d not in r["_renovation_dates"]:
                return False
        last = r.get("last_inventory_date")
        if last:
            interval = (d - last).days
            if interval <= 0:
                return False
            if r.get("store_type") == "RC" and interval < 14 and not r["conversion_interval_exception"]:
                return False
            if r.get("store_type") == "FC" and not 45 < interval < 75:
                override = self.mandatory_month(s) is not None or "first_store_id" in r
                if not override:
                    return False
        return True

    def worker_allowed(self, w, s, d, slot):
        r = self.rules[s]
        worker = self.workers[w]
        slots = ("AM", "PM") if r["ultra_remote"] else (slot,)
        if d in self.holidays and not self.holiday_optin[w]:
            return False
        if self.support:
            dept = worker["department_id"]
            worker_dept = 1 if dept in (1, "department1") else 2
            store_dept = 1 if r["department_id"] in (1, "department1") else 2
            if worker_dept != store_dept:
                return False
            if dept in (2, "department2") and worker["section_id"] != r["section_id"]:
                return False
        return all(self.worker_cal.get((w, d), (False, False))[sl == "PM"] and
                   (w, d, sl) not in self.booked and
                   not (sl == "PM" and d in self.meetings[w]) for sl in slots)

    def tick(self):
        if self.nodes >= self.limit:
            raise _SearchLimit()
        self.nodes += 1

    def distance(self, a, b):
        if a is None or b is None:
            return None
        key = tuple(sorted((a, b)))
        if key not in self.distance_cache:
            self.distance_cache[key] = haversine_km(*a, *b)
        return self.distance_cache[key]

    def route_distance(self, worker, am, pm):
        home = self.worker_coords[worker]
        stops = [self.store_coords[s] for s in (am, pm) if s is not None]
        points = [home, *stops, home]
        legs = [self.distance(a, b) for a, b in zip(points, points[1:])]
        return None if None in legs else math.fsum(legs)

    def commute_metrics(self, schedule):
        routes = {}
        for row in schedule:
            for w in row["worker_ids"]:
                route = routes.setdefault((row["date"], w), {
                    "worker_id": w, "date": row["date"],
                    "am_store_id": None, "pm_store_id": None})
                route[row["slot"].lower() + "_store_id"] = row["store_id"]
        ordered = [routes[key] for key in sorted(routes, key=lambda key: (key[0], _key(key[1])))]
        for route in ordered:
            route["distance_km"] = self.route_distance(
                route["worker_id"], route["am_store_id"], route["pm_store_id"])
        distances = [route["distance_km"] for route in ordered]
        return {"worker_routes": ordered,
                "total_commute_distance_km": None if None in distances else math.fsum(distances)}

    def added_commute(self, worker, store, prior):
        home = self.worker_coords[worker]
        destination = self.store_coords[store]
        opposite = next((row["store_id"] for row in prior if worker in row["worker_ids"]), None)
        if opposite is None:
            return 2 * self.distance(home, destination)
        other = self.store_coords[opposite]
        return (self.distance(home, destination) + self.distance(destination, other) -
                self.distance(home, other))

    def counterpart_rows(self, d, slot):
        return [r for r in self.current if r["date"] == d.isoformat() and r["slot"] != slot]

    def transition_rank(self, s, row, eligible):
        previous = set(row["worker_ids"])
        shared = previous & set(eligible)
        required = self.rules[s]["required_worker_ids"]
        overlap = len(shared & required) + min(len(shared - required), self.needs[s] - len(required))
        if not overlap:
            return (3, 1, 0, float("inf"))
        distance = self.distance(self.store_coords[s], self.store_coords[row["store_id"]])
        proximity = 2 if distance is None else int(distance > 10)
        exact = len(previous) == self.needs[s] and previous.issubset(eligible) and required.issubset(previous)
        return (proximity, not exact, -overlap,
                distance if distance is not None else float("inf"))

    def transition_preference(self, s, day=None, slot=None):
        ranks = []
        for row in self.current:
            d = _date(row["date"], "date")
            opposite = "PM" if row["slot"] == "AM" else "AM"
            if (day is not None and day != d or slot is not None and slot != opposite or
                    (d, opposite) not in self.available[s] or self.rules[s]["ultra_remote"]):
                continue
            eligible = [w for w in row["worker_ids"] if self.worker_allowed(w, s, d, opposite)]
            ranks.append(self.transition_rank(s, row, eligible))
        return min(ranks, default=(3, 1, 0, float("inf")))

    def teams(self, s, d, slot):
        eligible = [w for w in self.workers if self.worker_allowed(w, s, d, slot)]
        required = self.rules[s]["required_worker_ids"]
        if not required.issubset(eligible):
            return
        prior = self.counterpart_rows(d, slot)
        prior.sort(key=lambda row: (self.transition_rank(s, row, eligible), _key(row["store_id"])))
        commute = {w: self.added_commute(w, s, prior) for w in eligible} if self.optimize_distance else {}
        seen = set()
        # Try an actual intact half-day team before generating regrouped combinations.
        for row in prior:
            self.tick()
            team = tuple(sorted(row["worker_ids"], key=_key))
            if (len(team) == self.needs[s] and required.issubset(team) and
                    set(team).issubset(eligible) and
                    set.intersection(*(self.memberships[w] for w in team))):
                seen.add(team)
                yield team

        def preference(w):
            distances = [self.distance(self.worker_coords[w], self.worker_coords[v])
                         for v in required if v != w]
            known = [x for x in distances if x is not None]
            same_area = s in self.areas and self.workers[w].get("area_id") == self.areas[s]["area_id"]
            legacy = (not same_area, not self.leaders[w],
                      sum(known) if known else float("inf"), _key(w))
            return (commute[w], *legacy) if self.optimize_distance else legacy
        eligible.sort(key=preference)
        pools = {}
        for w in eligible:
            for group in self.memberships[w]:
                pools.setdefault(group, []).append(w)

        def group_preference(group):
            pool = pools[group]
            return (min((self.transition_rank(s, row, pool) for row in prior),
                        default=(3, 1, 0, float("inf"))), _key(group))

        for group in sorted(pools, key=group_preference):
            self.tick()
            pool = pools[group]
            if not required.issubset(pool) or len(pool) < self.needs[s]:
                continue
            focus = min(prior, key=lambda row: (
                self.transition_rank(s, row, pool), _key(row["store_id"])), default=None)
            previous = set(focus["worker_ids"]) if focus else set()
            pool.sort(key=lambda w: ((commute[w], w not in previous, preference(w))
                                    if self.optimize_distance else (w not in previous, preference(w))))
            optional = [w for w in pool if w not in required]
            anchors = sorted(required, key=_key)
            if not anchors and pool:
                # Closest compatible coworkers are tried first; this is not commute time.
                anchor = pool[0]
                def coworker_preference(w):
                    distance = self.distance(self.worker_coords[anchor], self.worker_coords[w])
                    legacy = (w != anchor, w not in previous,
                              distance if distance is not None else float("inf"), preference(w))
                    return (commute[w], *legacy) if self.optimize_distance else legacy
                optional.sort(key=coworker_preference)
            elif previous:
                optional.sort(key=lambda w: ((commute[w], w not in previous, preference(w))
                                            if self.optimize_distance else (w not in previous, preference(w))))
            for extra in itertools.combinations(optional, self.needs[s] - len(anchors)):
                self.tick()
                team = tuple(sorted(anchors + list(extra), key=_key))
                if team in seen:
                    continue
                seen.add(team)
                if any(self.leaders[w] for w in team):
                    yield team

    def date_preference(self, stores, d):
        early = any(self.priority(s) for s in stores) or len(stores) > 1
        if early:
            return (0, 0, 0, 0), d.day, d.day
        transition = min(self.transition_preference(s, d) for s in stores)
        regional = any(self.workers[w].get("area_id") == self.areas.get(s, {}).get("area_id")
                       for s in stores for w in self.workers
                       if "area_id" in self.workers[w] and s in self.areas)
        if any(s in self.areas for s in stores):
            return transition, (abs(d.day - 15) if regional else -d.day), d.day
        return transition, d.day, d.day

    def group_plans(self, stores):
        dates = set(self.days)
        for s in stores:
            dates &= {d for d, slot in self.available[s]}
        members = [s for s in self.stores if s in stores or
                   stores[0] in self.groups and self.groups.get(s) == self.groups[stores[0]]]
        takeover = [s for s in members if self.active_takeover(s)]
        if takeover:
            dates &= {self.rules[takeover[0]]["takeover_date"]}
        if len(takeover) > 1 or (takeover and len(members) > 1 and
                                 self.rules[takeover[0]]["takeover_slot"] == "AM"):
            return
        for d in sorted(dates, key=lambda day: self.date_preference(stores, day)):
            self.tick()
            yield from self.assign_group(stores, d, takeover)

    def assign_group(self, stores, d, takeover):
        if not stores:
            yield None
            return
        s = min(stores, key=lambda sid: (
            (d, "AM") not in self.available[sid],
            self.transition_preference(sid, d), len(self.available[sid]),
            -len(self.rules[sid]["required_worker_ids"]), -self.needs[sid], _key(sid)))
        remaining = [sid for sid in stores if sid != s]
        slots = [sl for sl in ("AM", "PM") if (d, sl) in self.available[s]]
        if takeover and s != takeover[0]:
            slots = [sl for sl in slots if sl == "AM"]
        slots.sort(key=lambda sl: (self.transition_preference(s, d, sl)[0] != 0, sl != "AM"))
        for slot in slots:
            for team in self.teams(s, d, slot):
                reserved = {(w, d, sl) for w in team
                            for sl in (("AM", "PM") if self.rules[s]["ultra_remote"] else (slot,))}
                self.booked.update(reserved)
                row = {"store_id": s, "date": d.isoformat(), "slot": slot,
                       "worker_ids": list(team),
                       "leader_id": next(w for w in team if self.leaders[w])}
                if s in self.groups:
                    row["store_group_id"] = self.groups[s]
                self.current.append(row)
                try:
                    yield from self.assign_group(remaining, d, takeover)
                finally:
                    self.current.pop()
                    self.booked.difference_update(reserved)

    def unit_preference(self, stores):
        return (not any(self.priority(s) for s in stores), -len(stores),
                not any(slot == "AM" for s in stores for _, slot in self.available[s]),
                min(self.transition_preference(s) for s in stores),
                sum(len(self.available[s]) for s in stores), tuple(_key(s) for s in stores))

    @staticmethod
    def continuity_score(schedule):
        """Count exact same-date AM/PM teams, without emitting candidate warnings."""
        morning = {(r["date"], frozenset(r["worker_ids"])): r["store_id"]
                   for r in schedule if r["slot"] == "AM"}
        return sum((r["date"], frozenset(r["worker_ids"])) in morning and
                   morning[r["date"], frozenset(r["worker_ids"])] != r["store_id"]
                   for r in schedule if r["slot"] == "PM")

    def search(self, remaining):
        self.tick()
        if len(self.current) >= len(self.best):
            continuity = self.continuity_score(self.current)
            distance = (self.commute_metrics(self.current)["total_commute_distance_km"]
                        if self.optimize_distance else 0.0)
            objective = (len(self.current), continuity, -distance)
            incumbent = (len(self.best), self.best_continuity, -self.best_distance)
            if objective > incumbent:
                self.best = list(self.current)
                self.best_continuity = continuity
                self.best_distance = distance
        # Each store participates in at most one intact pair, so this proves all objectives.
        if (len(self.best) == len(self.due) and self.best_continuity == self.continuity_bound and
                (not self.optimize_distance or self.best_distance == 0)):
            return True
        if not remaining:
            return False
        possible = len(self.current) + sum(map(len, remaining))
        if possible < len(self.best):
            return False
        unit = min(remaining, key=self.unit_preference)
        tail = [ss for ss in remaining if ss is not unit]
        plans = self.group_plans(unit)
        try:
            for _ in plans:
                if self.search(tail):
                    return True
        finally:
            plans.close()
        return self.search(tail)

    def transition_metrics(self):
        transitions = []
        unpaired = 0
        for pm in sorted(self.best, key=lambda r: (r["date"], _key(r["store_id"]))):
            if pm["slot"] != "PM":
                continue
            morning = [am for am in self.best if am["date"] == pm["date"] and am["slot"] == "AM"]
            paired = False
            for am in sorted(morning, key=lambda r: _key(r["store_id"])):
                workers = set(am["worker_ids"]) & set(pm["worker_ids"])
                if not workers:
                    continue
                paired = True
                distance = self.distance(self.store_coords[am["store_id"]], self.store_coords[pm["store_id"]])
                same = set(am["worker_ids"]) == set(pm["worker_ids"])
                label = str(am["store_id"]) + " -> " + str(pm["store_id"])
                if not same:
                    self.warnings.add("team_split: " + label)
                if distance is None:
                    self.warnings.add("unknown_transition_distance: " + label)
                elif distance > 10:
                    self.warnings.add("transition_over_10km: " + label)
                transitions.append({"date": pm["date"], "am_store_id": am["store_id"],
                                    "pm_store_id": pm["store_id"],
                                    "worker_ids": sorted(workers, key=_key),
                                    "same_team": same, "distance_km": distance})
            if morning and not paired:
                unpaired += 1
                self.warnings.add("pm_team_without_am_continuity: " + str(pm["store_id"]))
        return {"transitions": transitions,
                "same_team_transitions": sum(r["same_team"] for r in transitions),
                "split_team_transitions": sum(not r["same_team"] for r in transitions),
                "long_distance_transitions": sum(r["distance_km"] is not None and r["distance_km"] > 10
                                                 for r in transitions),
                "unknown_distance_transitions": sum(r["distance_km"] is None for r in transitions),
                "pm_teams_without_am_continuity": unpaired}

    def static_diagnostics(self, stores):
        diagnostics = []
        common_dates = set(self.days)
        for s in stores:
            slots = sorted(self.available[s])
            common_dates &= {d for d, _ in slots}
            if not slots:
                diagnostics.append({"store_id": s, "reason": "no_store_allowed_slots"})
                continue
            possibilities = [[w for w in self.workers if self.worker_allowed(w, s, d, slot)]
                             for d, slot in slots]
            required = self.rules[s]["required_worker_ids"]
            possibilities = [pool for pool in possibilities if required.issubset(pool)]
            if not possibilities:
                reason = "required_worker_unavailable"
            elif max(map(len, possibilities)) < self.needs[s]:
                reason = "insufficient_available_workers"
            elif not any(self.leaders[w] for pool in possibilities for w in pool):
                reason = "no_available_leader"
            else:
                capacity = False
                feasible = False
                for eligible in possibilities:
                    groups = {}
                    for w in eligible:
                        for group in self.memberships[w]:
                            groups.setdefault(group, set()).add(w)
                    for pool in groups.values():
                        if len(pool) < self.needs[s] or not required.issubset(pool):
                            continue
                        capacity = True
                        leader_pool = required if len(required) == self.needs[s] else pool
                        if any(self.leaders[w] for w in leader_pool):
                            feasible = True
                            break
                    if feasible:
                        break
                if feasible:
                    continue
                reason = "no_common_group_leader" if capacity else "no_common_worker_group"
            diagnostics.append({"store_id": s, "reason": reason})
        if len(stores) > 1 and not common_dates and all(self.available[s] for s in stores):
            diagnostics.append({"reason": "no_common_store_date"})
        members = [s for s in self.stores if s in stores or
                   stores[0] in self.groups and self.groups.get(s) == self.groups[stores[0]]]
        takeover = [s for s in members if self.active_takeover(s)]
        if len(takeover) > 1 or (takeover and len(members) > 1 and
                                 self.rules[takeover[0]]["takeover_slot"] == "AM"):
            diagnostics.append({"reason": "takeover_slot_conflict"})
        return diagnostics

    def generate(self):
        try:
            self.search(self.units)
        except _SearchLimit:
            self.reached = True
        done = {r["store_id"] for r in self.best}
        unscheduled = []
        for stores in self.units:
            if set(stores).issubset(done):
                continue
            diagnostics = self.static_diagnostics(stores)
            reason = ("search_limit" if self.reached else
                      "first_inventory_overdue" if self.overdue_first.intersection(stores) else
                      diagnostics[0]["reason"] if diagnostics else "no_feasible_assignment")
            unscheduled.append({"store_ids": list(stores), "reason": reason,
                                "diagnostics": diagnostics})
        for row in self.best:
            r = self.rules[row["store_id"]]
            last = r.get("last_inventory_date")
            if r.get("store_type") == "FC" and last and not 45 < (
                    _date(row["date"], "date") - last).days < 75:
                self.warnings.add("fc_first_inventory_interval_override: " + str(row["store_id"]))
        if self.reached:
            self.warnings.add("search_limit: a better or complete schedule may exist")
        transitions = self.transition_metrics()
        return {"status": "partial" if unscheduled else "complete",
                "schedule": sorted(self.best, key=lambda r: (r["date"], r["slot"], _key(r["store_id"]))),
                "unscheduled": unscheduled, "skipped": self.skipped,
                "warnings": sorted(self.warnings),
                "notes": ["Maximize scheduled stores first, then maximize same-date AM/PM pairs "
                          "at different stores with exactly identical worker ID sets "
                          "(same_team_transitions), then minimize total worker-day "
                          "home -> AM -> PM -> home great-circle distance. A single half-day "
                          "or ultra-remote AM visit is home -> store -> home.",
                          "Team continuity is a soft objective, never a feasibility constraint; "
                          "adding or removing workers does not count as an intact team.",
                          "Distance optimization requires coordinates for all due stores and all workers; "
                          "otherwise coverage and team continuity are still optimized. "
                          "Unknown route distances are null.",
                          "Team continuity optimality is certified when search finishes without its limit. "
                          "Distance optimality is conditional on maximum coverage and continuity, "
                          "and is certified only when enabled and search finishes without its limit. "
                          "A full schedule reaching floor(due stores / 2) intact pairs and zero distance "
                          "(or disabled distance optimization) also proves optimality. "
                          "Equal objective values retain deterministic preferences.",
                          "Without cycle/event metadata, upstream must supply due stores and "
                          "calendars encoding business constraints. Missing slots are unavailable.",
                          "Historical inventory intervals are checked only when last_inventory_date "
                          "is supplied; missing history is not inferred.",
                          "Organizational support checks require organizational_support=true.",
                          "Sunday is prohibited. Saturday requires explicit store and worker availability.",
                          "Distances are great-circle distances, not travel-time feasibility.",
                          "Driving, vehicle rotation, fuel reimbursement and inventory-based "
                          "headcount calculations are not modeled."],
                "metrics": {"search_nodes": self.nodes, "search_limit": self.limit,
                            "reached_search_limit": self.reached, **transitions,
                            "team_continuity_optimal": not self.reached,
                            "distance_optimization_enabled": self.optimize_distance,
                            "distance_optimal": self.optimize_distance and not self.reached,
                            **self.commute_metrics(self.best)}}
