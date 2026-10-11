# DECISIONS.md

1. **Indexes.**
   - `uq_employee_emp_code` (`{emp_code: 1}`, unique): Guarantees unique employee identifiers.
   - `uq_attendance_emp_date` (`{emp_code: 1, date: 1}`, unique): Guarantees exactly one attendance record per employee per day.
   - `ix_employees_dept_joined` (`{department: 1, joined_on: 1}`): Serves department lookups and headcount calculations.
   - `ix_attendance_date_desc_emp_asc` (`{date: -1, emp_code: 1}`): Powers attendance listing pagination and trend aggregations.
   - `ix_attendance_emp_punch_in` (`{emp_code: 1, punch_in: -1}`): Speeds up latest punch-in lookups and monthly stats.
   - `ix_attendance_date_late` (`{date: 1, late_minutes: 1}`): Accelerates the late leaderboard pipeline.
   - *Considered and rejected:* A wide compound index `{department, date, status, work_hours}` on attendance logs. Rejected because `department` lives on employees; denormalizing it would increase index write amplification on punch traffic without query benefits.

2. **Punch-in race.**
   Both requests compute the identical attendance date and issue concurrent `insert_one` operations. MongoDB's `uq_attendance_emp_date` unique index atomically serializes them: exactly one insert wins and returns HTTP 201; the second triggers a `DuplicateKeyError`, which our application catches and converts to HTTP 409.

3. **Ties.**
   We compute ranks using `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). The query filters with `rank <= limit`. Any employees sharing the cutoff rank are retained, sorted deterministically by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline roots on the `employees` collection filtering `joined_on <= month_end`, and uses `$lookup` to join attendance records. Employees with zero attendance logs yield an empty logs array, ensuring they remain counted in department headcount.

5. **One thing you would change for 100x data.**
   Replace ad-hoc monthly aggregations with daily rollups or materialized summary collections, avoiding full pipeline scans over hundreds of millions of raw punch documents.
