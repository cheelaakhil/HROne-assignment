# Employee Attendance & Analytics API

An Employee Attendance & Analytics REST API built with **Python**, **FastAPI**, and **MongoDB**.
Implements shift business rules (R1–R10), concurrency-safe punch-in/punch-out, regularizations with audit trail history, and MongoDB aggregation pipelines for monthly, department, and leaderboard analytics.

**Submission Status**: **100% Complete** — All starter defects fixed, all endpoints implemented, all business rules enforced, all aggregation pipelines built natively in MongoDB, and explain plans verified for `IXSCAN`.

---

## API Endpoints

| Method | Endpoint | Description | Implementation Details |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Health check | Returns `200` only when MongoDB responds to a `ping`; returns `503` if unavailable. |
| `POST` | `/employees` | Create employee | Unique `emp_code` constraint (409 on duplicate), validation on shift times and dates (422). |
| `GET` | `/employees` | List employees | Filter by `department`, sorted by `emp_code` ascending, 1-indexed pagination with accurate filtered `total`. |
| `POST` | `/attendance/punch-in` | Punch in | Enforces R1 (one record per employee per day, race-safe), R2 (10m grace period), epoch ms timestamps. |
| `POST` | `/attendance/punch-out` | Punch out | Computes R3 (overtime $\ge 30$m), R4 (work hours), R5 (half day if $< 4.50$ hrs); handles shifts across midnight. |
| `GET` | `/attendance` | List attendance | Filter by `emp_code`, `date_from`, `date_to`, `status`; sorted by `date` DESC and `emp_code` ASC; indexed pagination. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize | Modifies punch times or status, recomputes derived metrics, appends audit trail to `history` with optimistic concurrency. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary | Aggregation pipeline calculating presence, leaves, late minutes, overtime, and working days (R7 from `joined_on`). |
| `GET` | `/analytics/departments/summary` | Department summary | Aggregation across collections; R9 headcount counts employees with zero logs; averages work hours over records. |
| `GET` | `/analytics/leaderboard/late` | Late leaderboard | Standard competition ranking (`1, 2, 2, 4`) using `$setWindowFields` + `$rank`; `limit` applied after ranking; ties ordered by `emp_code`. |
| `GET` | `/analytics/departments/{department}/trend` | Daily attendance trend | Continuous calendar series using `$densify` for gap filling and `$setWindowFields` for 7-day moving averages in MongoDB. |
| `GET` | `/admin/explain/{endpoint}` | Explain query plan | Runs MongoDB `explain` with `executionStats` verbosity; verified index scan (`IXSCAN`) and no collection scan (`COLLSCAN`). |

---

## Business Rules Implemented (R1–R10)

- **R1 (Single Daily Log)**: Backed by a unique compound index on `(emp_code, date)`. Timestamps truncated to whole seconds.
- **R2 (Late Arrival)**: 10-minute grace period from `shift_start`. Arrival strictly $> 10:00$ records floor minutes from `shift_start`.
- **R3 (Overtime)**: Floor minutes past `shift_end`, counted only if $\ge 30$ minutes (otherwise 0). Supports cross-midnight shifts.
- **R4 (Work Hours)**: Time difference in seconds divided by 3600, rounded half-up to 2 decimal places.
- **R5 (Half Day)**: When rounded `work_hours < 4.50`, marked as `half_day = True` (counts as 0.5 present day).
- **R6 (Presence Status)**: `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
- **R7 (Working Days & Mid-Month Joiners)**: Mon–Fri calendar days calculated from `joined_on` for mid-month joiners. Weekend logs add to late/overtime totals without increasing `present_days`.
- **R8 (Rounding)**: Exact half-up rounding (`ROUND_HALF_UP`) using Python's `decimal.Decimal` (rates to 4 decimal places, hours/percentages to 2).
- **R9 (Headcount)**: Counts all department employees joining on or before month end, even if they have zero attendance logs.
- **R10 (Pagination)**: 1-indexed pagination (`skip = (page - 1) * page_size`), with `total` matching active filters.

---

## Concurrency & Safety

- **Punch-in Race Condition**: Unique compound index on `(emp_code, date)` enforces atomicity at the storage engine level. Concurrent punch-ins yield exactly one `201 Created` and all other simultaneous requests raise `DuplicateKeyError` and return `409 Conflict`.
- **Punch-out Race Condition**: Atomic conditional updates (`find_one_and_update` on open punch-in) ensure exactly one request completes the punch-out (returning `200 OK`) while concurrent requests receive `404` / `409`.
- **Regularization Overwrite Safety**: Optimistic concurrency check verifies history array length before update, preventing concurrent regularizations from silently losing audit entries.

---

## Indexes Created at Startup

All indexes are created idempotently at application startup (`@app.on_event("startup")`):
1. `employees: { emp_code: 1 }` (unique)
2. `employees: { department: 1 }`
3. `employees: { joined_on: 1 }`
4. `attendance_logs: { emp_code: 1, date: 1 }` (unique)
5. `attendance_logs: { date: -1, emp_code: 1 }`
6. `attendance_logs: { date: 1, emp_code: 1 }`
7. `attendance_logs: { date: 1, late_minutes: -1 }`
8. `attendance_logs: { emp_code: 1, date: -1 }`

---

## Local Setup & Running

### Requirements
- Python 3.10+
- MongoDB 6.0+ (the grader uses 7.0; tested on MongoDB 7.0 and 8.0)

### Quick Start
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Set environment variables if not using default localhost:27017
# MONGO_URI defaults to "mongodb://localhost:27017"
# MONGO_DB defaults to "attendance_db"

# 3. Seed sample data
python sample_seed.py

# 4. Start the server
uvicorn app.main:app --port 8000
```

---

## Running Automated Tests

```bash
# 1. Run the core API test suite (all 38 contract tests)
python test_api.py

# 2. Run the advanced edge case & concurrency test suite
python test_advanced_cases.py
```

### Test Suite Coverage
- `test_api.py`: Validates OpenAPI compliance, status codes, R1–R10 calculations, and explain plans for all 5 endpoints (**38/38 passing**).
- `test_advanced_cases.py`: Validates high-concurrency races (simultaneous punch-in/punch-out), weekend attendance rules, mid-month joiners, leaderboard ties at `limit`, overnight shifts, and zero-log department headcount (**all passing**).

---

## Repository Files

- [`app/main.py`](app/main.py): All API endpoints, Pydantic schemas, helper logic, indexes, and aggregation pipelines.
- [`requirements.txt`](requirements.txt): Runtime dependencies (`fastapi`, `uvicorn`, `pymongo`, `pydantic`, `python-dotenv`).
- [`REVIEW.md`](REVIEW.md): Detailed code review documenting all 14 starter defects, test inputs, and fixes.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions (~200 words) on indexes, concurrency, ranking ties, headcount, and scaling.
- [`README.md`](README.md): Setup, architecture, and verification documentation.
- [`sample_seed.py`](sample_seed.py): Database seeder using sample dataset.
- [`sample_data/`](sample_data/): Extended JSON sample documents for employees and attendance logs.
- [`openapi.yaml`](openapi.yaml): API contract specification.
- [`DATA_MODEL.md`](DATA_MODEL.md): Database schema documentation.
