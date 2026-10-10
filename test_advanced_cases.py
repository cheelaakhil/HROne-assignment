"""Advanced test suite covering awkward cases, concurrency, and rules R1-R10."""
import concurrent.futures
import requests
import json
from datetime import datetime, timezone, timedelta

BASE = "http://localhost:8000"
IST = timezone(timedelta(hours=5, minutes=30))

print("\n==========================================")
print("RUNNING ADVANCED EDGE CASE & CONCURRENCY TESTS")
print("==========================================")

# 1. CONCURRENCY: Simultaneous punch-ins
print("\n--- Test 1: Simultaneous Punch-in Race Condition ---")
# Create a test employee
emp_payload = {
    "emp_code": "EMP5001",
    "name": "Race Tester",
    "email": "race@test.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-01"
}
r = requests.post(f"{BASE}/employees", json=emp_payload)
assert r.status_code in [201, 409]

# Punch in time for a specific date: 2026-08-10 09:35 IST
punch_time = int(datetime(2026, 8, 10, 9, 35, 0, tzinfo=IST).timestamp() * 1000)

def do_punch_in():
    return requests.post(f"{BASE}/attendance/punch-in", json={
        "emp_code": "EMP5001",
        "punched_at": punch_time
    })

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(do_punch_in) for _ in range(10)]
    results = [f.result() for f in futures]

status_codes = [res.status_code for res in results]
count_201 = status_codes.count(201)
count_409 = status_codes.count(409)
print(f"Punch-in race results: 201 Created: {count_201}, 409 Conflict: {count_409}")
assert count_201 == 1, f"Expected exactly 1 201, got {count_201}"
assert count_409 == 9, f"Expected exactly 9 409s, got {count_409}"
print("PASS: Exactly one punch-in succeeded and all others got 409 Conflict!")


# 2. CONCURRENCY: Simultaneous punch-outs
print("\n--- Test 2: Simultaneous Punch-out Race Condition ---")
out_time = int(datetime(2026, 8, 10, 18, 35, 0, tzinfo=IST).timestamp() * 1000)

def do_punch_out():
    return requests.post(f"{BASE}/attendance/punch-out", json={
        "emp_code": "EMP5001",
        "punched_at": out_time
    })

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(do_punch_out) for _ in range(10)]
    results = [f.result() for f in futures]

out_codes = [res.status_code for res in results]
count_200 = out_codes.count(200)
count_404 = out_codes.count(404)
print(f"Punch-out race results: 200 OK: {count_200}, 404 Not Found: {count_404}")
assert count_200 == 1, f"Expected exactly 1 200, got {count_200}"
assert count_404 + out_codes.count(409) == 9, f"Expected 9 failed punch-outs (404/409), got {count_404 + out_codes.count(409)}"
print("PASS: Exactly one punch-out succeeded and all others received 404 or 409!")


# 3. AWKWARD CASE: Mid-month Joiner & Working Days (R7)
print("\n--- Test 3: Mid-Month Joiner Working Days & Headcount ---")
emp_mid = {
    "emp_code": "EMP5002",
    "name": "Mid Joiner",
    "email": "mid@test.com",
    "department": "QA",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-07-20"  # Joins Monday July 20, 2026
}
requests.post(f"{BASE}/employees", json=emp_mid)

# Month 2026-07 has 23 total working days (Mon-Fri)
# From 2026-07-20 to 2026-07-31:
# July 20..24 (5 days), July 27..31 (5 days) = 10 working days
r_monthly = requests.get(f"{BASE}/analytics/employees/EMP5002/monthly?month=2026-07").json()
print("Mid joiner monthly summary:", r_monthly)
assert r_monthly["working_days"] == 10, f"Expected 10 working days, got {r_monthly['working_days']}"
assert r_monthly["present_days"] == 0
assert r_monthly["attendance_pct"] == 0.0
print("PASS: Mid-month joiner working days correctly calculated starting from joined_on!")


# 4. AWKWARD CASE: Weekend Records (R7)
print("\n--- Test 4: Weekend Records (Mon-Fri vs Weekends R7) ---")
# Punch-in on Saturday: 2026-07-25 (Saturday), 10:05 IST (35 min late)
sat_in = int(datetime(2026, 7, 25, 10, 5, 0, tzinfo=IST).timestamp() * 1000)
sat_out = int(datetime(2026, 7, 25, 19, 15, 0, tzinfo=IST).timestamp() * 1000) # 45 min overtime

r_pin = requests.post(f"{BASE}/attendance/punch-in", json={"emp_code": "EMP5002", "punched_at": sat_in})
assert r_pin.status_code == 201
r_pout = requests.post(f"{BASE}/attendance/punch-out", json={"emp_code": "EMP5002", "punched_at": sat_out})
assert r_pout.status_code == 200

r_monthly2 = requests.get(f"{BASE}/analytics/employees/EMP5002/monthly?month=2026-07").json()
print("Monthly after weekend record:", r_monthly2)
# R7: present_days counts only Mon-Fri records -> present_days must STILL be 0!
assert r_monthly2["present_days"] == 0, f"Expected present_days to remain 0 for weekend, got {r_monthly2['present_days']}"
# Weekend record still adds to late and overtime totals!
assert r_monthly2["late_count"] == 1, f"Expected late_count 1, got {r_monthly2['late_count']}"
assert r_monthly2["total_late_minutes"] == 35, f"Expected total_late_minutes 35, got {r_monthly2['total_late_minutes']}"
assert r_monthly2["total_overtime_minutes"] == 45, f"Expected total_overtime_minutes 45, got {r_monthly2['total_overtime_minutes']}"
print("PASS: Weekend record does NOT increment present_days, but DOES contribute to late and overtime totals!")


# 5. AWKWARD CASE: Ties in Late Leaderboard & Limit Cutoff
print("\n--- Test 5: Ties in Late Leaderboard & Limit Cutoff ---")
# Create EMP5003 and EMP5004 both in 'Support' with exact same 50 late minutes
requests.post(f"{BASE}/employees", json={
    "emp_code": "EMP5003", "name": "Tie Support B", "email": "b@s.com",
    "department": "Support", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
})
requests.post(f"{BASE}/employees", json={
    "emp_code": "EMP5004", "name": "Tie Support A", "email": "a@s.com",
    "department": "Support", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
})

# Punch both in at 10:20 IST on 2026-07-21 (50 min late)
t_late = int(datetime(2026, 7, 21, 10, 20, 0, tzinfo=IST).timestamp() * 1000)
requests.post(f"{BASE}/attendance/punch-in", json={"emp_code": "EMP5003", "punched_at": t_late})
requests.post(f"{BASE}/attendance/punch-in", json={"emp_code": "EMP5004", "punched_at": t_late})

# Query leaderboard for department Support with limit=1
r_board = requests.get(f"{BASE}/analytics/leaderboard/late?month=2026-07&department=Support&limit=1").json()
print("Leaderboard tied response with limit=1:", json.dumps(r_board, indent=2))

assert len(r_board["items"]) == 2, f"Expected both tied employees returned (count=2), got {len(r_board['items'])}"
assert r_board["items"][0]["rank"] == 1
assert r_board["items"][1]["rank"] == 1
assert r_board["items"][0]["emp_code"] == "EMP5003" # Alphabetical EMP5003 < EMP5004
assert r_board["items"][1]["emp_code"] == "EMP5004"
print("PASS: Tied employees share rank 1, both returned when limit=1, sorted by emp_code!")


# 6. AWKWARD CASE: Overnight Shift (Across Midnight)
print("\n--- Test 6: Overnight Shift Across Midnight ---")
requests.post(f"{BASE}/employees", json={
    "emp_code": "EMP5005", "name": "Night Owl", "email": "owl@test.com",
    "department": "Operations", "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-01"
})
# Punch in at 22:05 IST on 2026-07-22 (within 10m grace period -> late = 0)
night_in = int(datetime(2026, 7, 22, 22, 5, 0, tzinfo=IST).timestamp() * 1000)
r_nin = requests.post(f"{BASE}/attendance/punch-in", json={"emp_code": "EMP5005", "punched_at": night_in})
assert r_nin.status_code == 201
assert r_nin.json()["late_minutes"] == 0

# Punch out at 06:40 IST on 2026-07-23 (40m after 06:00 shift end -> overtime = 40m)
# Duration: 22:05 to 06:40 = 8 hours 35 min = 8.58 hours
night_out = int(datetime(2026, 7, 23, 6, 40, 0, tzinfo=IST).timestamp() * 1000)
r_nout = requests.post(f"{BASE}/attendance/punch-out", json={"emp_code": "EMP5005", "punched_at": night_out})
assert r_nout.status_code == 200
nout_data = r_nout.json()
print("Overnight shift punch-out data:", nout_data)
assert nout_data["work_hours"] == 8.58, f"Expected 8.58 work_hours, got {nout_data['work_hours']}"
assert nout_data["overtime_minutes"] == 40, f"Expected 40 overtime minutes, got {nout_data['overtime_minutes']}"
assert nout_data["half_day"] is False
print("PASS: Overnight shift correctly handled duration and overtime across midnight!")


# 7. AWKWARD CASE: Department Summary Headcount for 0-log Employee
print("\n--- Test 7: Department Summary Zero-Log Headcount ---")
# Employee in 'Design' with NO logs at all
requests.post(f"{BASE}/employees", json={
    "emp_code": "EMP5006", "name": "No Logs Designer", "email": "nolog@test.com",
    "department": "Design", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
})
r_dept = requests.get(f"{BASE}/analytics/departments/summary?month=2026-07&department=Design").json()
print("Design department summary:", r_dept)
assert len(r_dept["items"]) == 1
assert r_dept["items"][0]["headcount"] == 1, f"Expected headcount 1, got {r_dept['items'][0]['headcount']}"
assert r_dept["items"][0]["present_days"] == 0
assert r_dept["items"][0]["avg_work_hours"] is None
print("PASS: Department summary includes employees with zero logs in headcount!")

print("\n==========================================")
print("ALL ADVANCED EDGE CASE TESTS PASSED!")
print("==========================================")
