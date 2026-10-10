# DECISIONS.md

1. **Indexes.**
   - `employees: {emp_code: 1}` (unique) guarantees employee uniqueness and speeds up lookups.
   - `attendance_logs: {emp_code: 1, date: 1}` (unique) enforces R1 (one daily record per employee) and powers punch-out, regularization, employee monthly analytics, and trend lookups.
   - `attendance_logs: {date: -1, emp_code: 1}` satisfies `GET /attendance` pagination without in-memory sorting.
   - `attendance_logs: {date: 1, emp_code: 1}` backs `department_summary` range scans.
   - `attendance_logs: {date: 1, late_minutes: -1}` accelerates `late_leaderboard` filtering.
   - `employees: {department: 1}` and `{joined_on: 1}` optimize headcount filtering.
   - *Rejected*: An index on `{status: 1}` due to low cardinality (5 values), which would add write overhead without improving index selectivity over date-based compound indexes.

2. **Punch-in race.**
   Both requests reach MongoDB concurrently. The unique index on `{emp_code, date}` enforces exclusivity at the storage engine level. Exactly one insert transaction acquires the index key lock and commits (returning 201). The second triggers a `DuplicateKeyError`. Our exception handler intercepts this and returns 409 Conflict with `"already punched in for this date"`, preventing duplicate records.

3. **Ties.**
   Our leaderboard returns *all* tied employees, even if the total count exceeds `limit`. Under standard competition ranking, `$setWindowFields` with `$rank` assigns tied late totals identical ranks (e.g. 1, 2, 2, 4), and `rank <= limit` filtering occurs *after* ranking. Both tied cutoff rows qualify, sorted secondarily by `emp_code` ascending.

4. **Headcount.**
   Headcount is calculated directly from `employees` matching `joined_on <= last_day`, grouped by department before joining attendance data. Because employee records exist independently of attendance records, employees with zero monthly logs are fully preserved and counted in department headcount.

5. **One thing you would change for 100x data.**
   At 100x scale (~10M attendance documents), on-the-fly monthly and moving average aggregations become computationally expensive. I would implement asynchronous incremental rollups via change streams or an end-of-day aggregation pipeline to pre-calculate daily/monthly summaries, and shard `attendance_logs` by `{date: 1, emp_code: 1}`.
