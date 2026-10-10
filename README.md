# Employee Attendance & Analytics API

An Employee Attendance & Analytics REST API built with **Python**, **FastAPI**, and **MongoDB**.
Implements shift business rules (R1–R10), punch-in/punch-out concurrency safety, regularizations with audit history, and MongoDB aggregation pipelines for monthly, department, and leaderboard analytics.

---

## Features & Implementation

1. **Shift Business Rules (R1–R10)**:
   - **R1 (Single Daily Log)**: Unique compound index on `(emp_code, date)` enforces at most one record per employee per day. Sub-second timestamps truncated to whole seconds.
   - **R2 (Late Arrival)**: 10-minute grace period against shift start; arrival after 10:00 marks floor minutes from shift start.
   - **R3 (Overtime)**: Floor minutes past shift end, counted only if $\ge 30$ minutes (otherwise 0). Full overnight shift support across midnight.
   - **R4 (Work Hours)**: Time difference in seconds divided by 3600, rounded half-up to 2 decimal places.
   - **R5 (Half Day)**: Triggered when rounded work hours $< 4.50$. Counts as $0.5$ present day.
   - **R6 (Presence Status)**: `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
   - **R7 (Working Days & Mid-Month Joiners)**: Mon–Fri calendar days calculated from `joined_on` for mid-month joiners. Weekend logs add to late/overtime totals without increasing `present_days`.
   - **R8 (Rounding)**: Standard half-up rounding (`ROUND_HALF_UP`) using `decimal.Decimal` (rates to 4 decimal places, hours/percentages to 2).
   - **R9 (Headcount)**: Counts all department employees joining on or before month end, even if they have 0 attendance records.
   - **R10 (Pagination)**: 1-indexed pagination (`skip = (page - 1) * page_size`), with `total` accurately reflecting all active query filters.

2. **Concurrency & Race Conditions**:
   - Simultaneous punch-ins: Unique compound index on `(emp_code, date)` ensures atomic single-record creation; losing concurrent requests raise `DuplicateKeyError` and return `409 Conflict`.
   - Simultaneous punch-outs: Atomic conditional updates (`find_one_and_update` on open punch-in) ensure exactly one request completes the punch-out.
   - Regularizations: Optimistic locking checks history length to prevent concurrent update overwrites.

3. **Analytics Pipelines (No Python In-Memory Loops)**:
   - **Employee Monthly (`/analytics/employees/{emp_code}/monthly`)**: Aggregates presence, leaves, late minutes, and overtime using `$group` and `$cond`.
   - **Department Summary (`/analytics/departments/summary`)**: Cross-collection aggregation computing department headcount, total present days, average work hours, and counts.
   - **Late Leaderboard (`/analytics/leaderboard/late`)**: Standard competition ranking (1, 2, 2, 4) using `$setWindowFields` with `$rank`, filtering cutoff after ranking.
   - **Department Trend (`/analytics/departments/{department}/trend`)**: Continuous calendar series using `$densify` for gap filling and `$setWindowFields` for a 7-day moving average.
   - **Explain Endpoint (`/admin/explain/{endpoint}`)**: Runs MongoDB `explain` with `executionStats` verbosity; verified `IXSCAN` on all supported endpoints.

---

## Local Setup & Running

### Requirements
- Python 3.10+
- MongoDB 6.0+ (running locally on port `27017` or configured via `MONGO_URI`)

### Quick Start
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment (optional, defaults to localhost:27017 / attendance_db)
cp .env.example .env

# 3. Seed sample data
python sample_seed.py

# 4. Start the API server
uvicorn app.main:app --port 8000
```

---

## Running Automated Tests

Run the test suites:
```bash
# Core API & OpenAPI test suite (38 test cases)
python test_api.py

# Advanced edge cases (concurrency races, weekend logs, mid-month joiners, ties, overnight shifts)
python test_advanced_cases.py
```

---

## Repository Structure

- `app/main.py`: Complete API application and route handlers.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Code review of all defects identified in the starter code.
- `DECISIONS.md`: Design and architectural decisions (indexes, concurrency, ties, headcount, scaling).
- `sample_seed.py`: MongoDB database seeding script.
- `openapi.yaml`: OpenAPI 3.1 specification.
- `DATA_MODEL.md`: MongoDB schema documentation.
