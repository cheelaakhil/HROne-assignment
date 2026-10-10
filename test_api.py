"""Comprehensive test script for the attendance API."""
import requests
import json
import sys

BASE = "http://localhost:8000"

def test(name, response, expected_status, checks=None):
    status = response.status_code
    ok = status == expected_status
    body = None
    try:
        body = response.json()
    except:
        pass
    
    if not ok:
        print(f"  FAIL {name}: expected {expected_status}, got {status}")
        if body:
            print(f"    Body: {json.dumps(body, indent=2)[:500]}")
        return False
    
    if checks:
        for check_name, check_fn in checks.items():
            try:
                result = check_fn(body)
                if not result:
                    print(f"  FAIL {name} [{check_name}]: check returned False")
                    print(f"    Body: {json.dumps(body, indent=2)[:500]}")
                    return False
            except Exception as e:
                print(f"  FAIL {name} [{check_name}]: {e}")
                print(f"    Body: {json.dumps(body, indent=2)[:500]}")
                return False
    
    print(f"  PASS {name}")
    return True

passed = 0
failed = 0

def run(name, response, expected_status, checks=None):
    global passed, failed
    if test(name, response, expected_status, checks):
        passed += 1
    else:
        failed += 1

# ========== HEALTH ==========
print("\n=== HEALTH ===")
run("GET /health", requests.get(f"{BASE}/health"), 200, {
    "status_ok": lambda b: b["status"] == "ok"
})

# ========== EMPLOYEES ==========
print("\n=== EMPLOYEES ===")
# List existing employees
run("GET /employees", requests.get(f"{BASE}/employees"), 200, {
    "has_items": lambda b: "items" in b,
    "has_total": lambda b: "total" in b,
    "total_is_6": lambda b: b["total"] == 6,
})

# List with department filter
run("GET /employees?department=Engineering", 
    requests.get(f"{BASE}/employees", params={"department": "Engineering"}), 200, {
    "total_is_2": lambda b: b["total"] == 2,
})

# List with pagination
run("GET /employees?page=1&page_size=2",
    requests.get(f"{BASE}/employees", params={"page": 1, "page_size": 2}), 200, {
    "2_items": lambda b: len(b["items"]) == 2,
    "total_is_6": lambda b: b["total"] == 6,
})

# Check employee format (created_at is epoch ms, no _id)
run("GET /employees format check",
    requests.get(f"{BASE}/employees"), 200, {
    "created_at_is_int": lambda b: isinstance(b["items"][0]["created_at"], int),
    "created_at_is_ms": lambda b: b["items"][0]["created_at"] > 100000000000,
    "no_id": lambda b: "_id" not in b["items"][0],
    "sorted_by_emp_code": lambda b: all(
        b["items"][i]["emp_code"] <= b["items"][i+1]["emp_code"] 
        for i in range(len(b["items"])-1)
    ),
})

# Create employee - duplicate should fail
run("POST /employees duplicate",
    requests.post(f"{BASE}/employees", json={
        "emp_code": "EMP0001", "name": "Test", "email": "t@t.com",
        "department": "Test", "joined_on": "2026-01-01"
    }), 409)

# Create employee - shift_start == shift_end should fail
run("POST /employees shift_start == shift_end",
    requests.post(f"{BASE}/employees", json={
        "emp_code": "EMP9999", "name": "Test", "email": "t@t.com",
        "department": "Test", "shift_start": "09:30", "shift_end": "09:30",
        "joined_on": "2026-01-01"
    }), 422)

# ========== ATTENDANCE: PUNCH IN ==========
print("\n=== PUNCH IN ===")

# Punch in with explicit time (EMP0001 on 2026-07-09, 09:35 IST = 04:05 UTC)
# 2026-07-09T04:05:00Z in ms = ?
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))
ts = datetime(2026, 7, 9, 9, 35, 0, tzinfo=IST)
ts_ms = int(ts.timestamp() * 1000)

run("POST /attendance/punch-in",
    requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ts_ms
    }), 201, {
    "emp_code": lambda b: b["emp_code"] == "EMP0001",
    "date": lambda b: b["date"] == "2026-07-09",
    "status": lambda b: b["status"] == "PRESENT",
    "punch_in_is_int": lambda b: isinstance(b["punch_in"], int),
    "punch_out_null": lambda b: b["punch_out"] is None,
    "work_hours_null": lambda b: b["work_hours"] is None,
    "history_empty": lambda b: b["history"] == [],
    # 09:35 is 5 min after 09:30, within grace -> late_minutes = 0
    "late_minutes_0": lambda b: b["late_minutes"] == 0,
})

# Duplicate punch in should fail
run("POST /attendance/punch-in duplicate",
    requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ts_ms
    }), 409)

# Unknown employee
run("POST /attendance/punch-in unknown",
    requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMPXXXX", "punched_at": ts_ms
    }), 404)

# Invalid status
run("POST /attendance/punch-in bad status",
    requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMP0001", "status": "ABSENT"
    }), 422)

# Late punch-in test: EMP0003 at 10:05 IST on 2026-07-09 (35 min late)
ts_late = datetime(2026, 7, 9, 10, 5, 0, tzinfo=IST)
ts_late_ms = int(ts_late.timestamp() * 1000)
run("POST /attendance/punch-in late",
    requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMP0003", "punched_at": ts_late_ms
    }), 201, {
    "late_minutes_35": lambda b: b["late_minutes"] == 35,
})

# ========== ATTENDANCE: PUNCH OUT ==========
print("\n=== PUNCH OUT ===")

# Punch out EMP0001 at 18:35 IST on 2026-07-09
ts_out = datetime(2026, 7, 9, 18, 35, 0, tzinfo=IST)
ts_out_ms = int(ts_out.timestamp() * 1000)
run("POST /attendance/punch-out",
    requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ts_out_ms
    }), 200, {
    "emp_code": lambda b: b["emp_code"] == "EMP0001",
    "punch_out_not_null": lambda b: b["punch_out"] is not None,
    "work_hours_computed": lambda b: b["work_hours"] is not None,
    "half_day_false": lambda b: b["half_day"] == False,
    # 09:35 to 18:35 = 9 hours
    "work_hours_9": lambda b: b["work_hours"] == 9.0,
    # 18:35 is only 5 min after 18:30 shift end, < 30 min -> overtime = 0
    "overtime_0": lambda b: b["overtime_minutes"] == 0,
})

# Already punched out
run("POST /attendance/punch-out already",
    requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ts_out_ms
    }), 404)  # No open punch-in found

# Unknown employee
run("POST /attendance/punch-out unknown",
    requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": "EMPXXXX", "punched_at": ts_out_ms
    }), 404)

# Punch out EMP0003 at 19:10 IST (40 min after 18:30 shift end)
ts_out3 = datetime(2026, 7, 9, 19, 10, 0, tzinfo=IST)
ts_out3_ms = int(ts_out3.timestamp() * 1000)
run("POST /attendance/punch-out with overtime",
    requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": "EMP0003", "punched_at": ts_out3_ms
    }), 200, {
    "overtime_40": lambda b: b["overtime_minutes"] == 40,
})

# ========== ATTENDANCE: LIST ==========
print("\n=== ATTENDANCE LIST ===")
run("GET /attendance",
    requests.get(f"{BASE}/attendance"), 200, {
    "has_items": lambda b: "items" in b,
    "has_total": lambda b: "total" in b,
    "sorted_desc_date": lambda b: all(
        b["items"][i]["date"] >= b["items"][i+1]["date"]
        for i in range(len(b["items"])-1)
    ) if len(b["items"]) > 1 else True,
    "no_id_field": lambda b: all("_id" not in item and "id" not in item for item in b["items"]),
})

# Filter by emp_code
run("GET /attendance?emp_code=EMP0001",
    requests.get(f"{BASE}/attendance", params={"emp_code": "EMP0001"}), 200, {
    "all_emp0001": lambda b: all(i["emp_code"] == "EMP0001" for i in b["items"]),
})

# Filter by date range
run("GET /attendance?date_from=2026-07-07&date_to=2026-07-08",
    requests.get(f"{BASE}/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-08"}), 200, {
    "within_range": lambda b: all("2026-07-07" <= i["date"] <= "2026-07-08" for i in b["items"]),
})

# date_from > date_to should be 422
run("GET /attendance date_from > date_to",
    requests.get(f"{BASE}/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}), 422)

# ========== REGULARIZE ==========
print("\n=== REGULARIZE ===")

# Regularize: change punch_in for EMP0001 on 2026-07-09 (shift it earlier)
new_punch_in = datetime(2026, 7, 9, 9, 28, 0, tzinfo=IST)
new_punch_in_ms = int(new_punch_in.timestamp() * 1000)
run("PATCH /attendance/EMP0001/2026-07-09",
    requests.patch(f"{BASE}/attendance/EMP0001/2026-07-09", json={
        "punch_in": new_punch_in_ms,
        "reason": "biometric issue corrected",
        "regularized_by": "hr.admin",
    }), 200, {
    "has_history": lambda b: len(b["history"]) > 0,
    "history_has_changes": lambda b: "changes" in b["history"][-1],
    "punch_in_changed": lambda b: "punch_in" in b["history"][-1]["changes"],
    "late_0": lambda b: b["late_minutes"] == 0,
})

# Regularize: no-op change should be 422
run("PATCH /attendance/EMP0001/2026-07-09 no change",
    requests.patch(f"{BASE}/attendance/EMP0001/2026-07-09", json={
        "punch_in": new_punch_in_ms,
        "reason": "try again no change",
        "regularized_by": "hr.admin",
    }), 422)

# Regularize: unknown employee
run("PATCH /attendance/EMPXXXX/2026-07-09",
    requests.patch(f"{BASE}/attendance/EMPXXXX/2026-07-09", json={
        "reason": "test missing emp",
        "regularized_by": "hr.admin",
        "status": "ABSENT",
    }), 404)

# Regularize: unknown date
run("PATCH /attendance/EMP0001/2099-01-01",
    requests.patch(f"{BASE}/attendance/EMP0001/2099-01-01", json={
        "reason": "test missing date",
        "regularized_by": "hr.admin",
        "status": "ABSENT",
    }), 404)

# ========== ANALYTICS: EMPLOYEE MONTHLY ==========
print("\n=== EMPLOYEE MONTHLY ===")
run("GET /analytics/employees/EMP0001/monthly?month=2026-07",
    requests.get(f"{BASE}/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}), 200, {
    "emp_code": lambda b: b["emp_code"] == "EMP0001",
    "month": lambda b: b["month"] == "2026-07",
    "has_working_days": lambda b: isinstance(b["working_days"], int),
    "has_present_days": lambda b: "present_days" in b,
    "has_leave_days": lambda b: "leave_days" in b,
    "has_late_count": lambda b: "late_count" in b,
    "has_attendance_pct": lambda b: "attendance_pct" in b,
})

# Unknown employee
run("GET /analytics/employees/EMPXXXX/monthly",
    requests.get(f"{BASE}/analytics/employees/EMPXXXX/monthly", params={"month": "2026-07"}), 404)

# ========== ANALYTICS: DEPARTMENT SUMMARY ==========
print("\n=== DEPARTMENT SUMMARY ===")
run("GET /analytics/departments/summary?month=2026-07",
    requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07"}), 200, {
    "has_month": lambda b: b["month"] == "2026-07",
    "has_items": lambda b: "items" in b,
    "items_have_fields": lambda b: all(
        all(k in i for k in ["department", "headcount", "present_days", "avg_work_hours", "late_count", "total_late_minutes", "leave_count", "on_duty_count"])
        for i in b["items"]
    ) if b["items"] else True,
    "sorted_by_dept": lambda b: all(
        b["items"][i]["department"] <= b["items"][i+1]["department"]
        for i in range(len(b["items"])-1)
    ) if len(b["items"]) > 1 else True,
})

# With department filter
run("GET /analytics/departments/summary?month=2026-07&department=Engineering",
    requests.get(f"{BASE}/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}), 200, {
    "only_engineering": lambda b: all(i["department"] == "Engineering" for i in b["items"]),
})

# ========== ANALYTICS: LATE LEADERBOARD ==========
print("\n=== LATE LEADERBOARD ===")
run("GET /analytics/leaderboard/late?month=2026-07",
    requests.get(f"{BASE}/analytics/leaderboard/late", params={"month": "2026-07"}), 200, {
    "has_month": lambda b: b["month"] == "2026-07",
    "has_items": lambda b: "items" in b,
    "items_have_rank": lambda b: all("rank" in i for i in b["items"]),
    "items_have_emp_code": lambda b: all("emp_code" in i for i in b["items"]),
    "items_have_name": lambda b: all("name" in i for i in b["items"]),
    "items_have_department": lambda b: all("department" in i for i in b["items"]),
    "items_have_total_late_minutes": lambda b: all("total_late_minutes" in i for i in b["items"]),
    "items_have_late_count": lambda b: all("late_count" in i for i in b["items"]),
})

# ========== ANALYTICS: DEPARTMENT TREND ==========
print("\n=== DEPARTMENT TREND ===")
run("GET /analytics/departments/Engineering/trend",
    requests.get(f"{BASE}/analytics/departments/Engineering/trend", params={
        "from": "2026-07-06", "to": "2026-07-12"
    }), 200, {
    "has_department": lambda b: b["department"] == "Engineering",
    "has_items": lambda b: "items" in b,
    "7_days": lambda b: len(b["items"]) == 7,
    "items_have_date": lambda b: all("date" in i for i in b["items"]),
    "items_have_is_working_day": lambda b: all("is_working_day" in i for i in b["items"]),
    "items_have_headcount": lambda b: all("headcount" in i for i in b["items"]),
    "items_have_present_count": lambda b: all("present_count" in i for i in b["items"]),
    "items_have_late_count": lambda b: all("late_count" in i for i in b["items"]),
    "items_have_attendance_rate": lambda b: all("attendance_rate" in i for i in b["items"]),
    "items_have_moving_avg_7d": lambda b: all("moving_avg_7d" in i for i in b["items"]),
    # Weekend check: 2026-07-11 is Saturday, 2026-07-12 is Sunday
    "weekend_rate_null": lambda b: b["items"][5]["attendance_rate"] is None and b["items"][6]["attendance_rate"] is None,
})

# Unknown department
run("GET /analytics/departments/NONEXISTENT/trend",
    requests.get(f"{BASE}/analytics/departments/NONEXISTENT/trend", params={
        "from": "2026-07-06", "to": "2026-07-12"
    }), 404)

# Range > 92 days
run("GET /analytics/departments/Engineering/trend range too long",
    requests.get(f"{BASE}/analytics/departments/Engineering/trend", params={
        "from": "2026-01-01", "to": "2026-12-31"
    }), 422)

# to < from
run("GET /analytics/departments/Engineering/trend to < from",
    requests.get(f"{BASE}/analytics/departments/Engineering/trend", params={
        "from": "2026-07-12", "to": "2026-07-06"
    }), 422)

# ========== ADMIN: EXPLAIN ==========
print("\n=== EXPLAIN ===")
for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
    params = {"month": "2026-07"}
    if ep == "attendance_list":
        params = {"date_from": "2026-07-01", "date_to": "2026-07-31"}
    elif ep == "employee_monthly":
        params = {"emp_code": "EMP0001", "month": "2026-07"}
    elif ep == "department_trend":
        params = {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-12"}
    elif ep == "late_leaderboard":
        params = {"month": "2026-07"}
    
    run(f"GET /admin/explain/{ep}",
        requests.get(f"{BASE}/admin/explain/{ep}", params=params), 200, {
        "has_endpoint": lambda b: "endpoint" in b,
        "has_collection": lambda b: "collection" in b,
        "has_explain": lambda b: "explain" in b,
    })

# ========== SUMMARY ==========
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed+failed} tests")
if failed > 0:
    print("SOME TESTS FAILED!")
else:
    print("ALL TESTS PASSED!")
