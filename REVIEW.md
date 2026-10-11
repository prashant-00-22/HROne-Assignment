# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | | | | |

Also note anything you looked at and decided was **not** a defect, and why.

# Code Review (Part A)

### 1. GET /health lacked database connectivity probe
- **Where:** `GET /health`
- **Defect:** Returned a hardcoded `{"status": "ok"}` without pinging MongoDB.
- **Symptom:** App returned HTTP 200 even when the database was completely down or unreachable.
- **Fix:** Added `client.admin.command('ping')` inside a `try/except` block, returning HTTP 503 if unreachable.

### 2. Race condition on employee creation
- **Where:** `POST /employees`
- **Defect:** Performed application-level `find_one` followed by `insert_one`. Concurrent requests with the same `emp_code` both passed the validation check.
- **Symptom:** Duplicate `emp_code` records created under parallel requests.
- **Fix:** Created a unique index on `employees.emp_code` and intercepted `DuplicateKeyError` to return HTTP 409.

### 3. Off-by-one pagination and un-filtered total count
- **Where:** `GET /employees`
- **Defect:** Computed `skip = page * page_size`, which caused page 1 to skip the first 20 records. Total count was computed using `count_documents({})`, ignoring department filters.
- **Symptom:** Missing records on page 1 and inaccurate pagination metadata when filtering.
- **Fix:** Changed calculation to `skip = (page - 1) * page_size` and passed the query filter `q` into `count_documents(q)`.

### 4. Naive local timezone conversion & floating seconds (Rule R1)
- **Where:** `POST /attendance/punch-in`
- **Defect:** Used `datetime.fromtimestamp(body.punched_at / 1000)` which adopted host system local timezone instead of IST (+05:30), and failed to truncate milliseconds to whole seconds.
- **Symptom:** Incorrect date attribution and late calculation discrepancies across different machine environments.
- **Fix:** Truncated milliseconds to whole seconds (`math.floor(ms / 1000) * 1000`) and explicitly converted to `Asia/Kolkata` (IST, UTC+05:30).

### 5. Unhandled missing employee crash (HTTP 500)
- **Where:** `POST /attendance/punch-in`
- **Defect:** If `emp_code` did not exist, `emp` was `None`. Accessing `emp["shift_start"]` raised an unhandled `TypeError`.
- **Symptom:** Returned HTTP 500 Internal Server Error instead of HTTP 404.
- **Fix:** Added an explicit existence check returning HTTP 404 if the employee does not exist.

### 6. Overnight shift calendar date misattribution (Rule R1)
- **Where:** `POST /attendance/punch-in`
- **Defect:** Calendar date was taken directly from punch-in timestamp without checking overnight shift bounds (`shift_end <= shift_start`).
- **Symptom:** Early morning punches after midnight for a night shift were logged under the wrong attendance date.
- **Fix:** If `shift_end <= shift_start` and punch-in time is before `shift_end`, attributed the attendance date to the previous calendar day.

### 7. Late minutes grace calculation bug (Rule R2)
- **Where:** `compute_late_minutes`
- **Defect:** Evaluated `minutes > 10` on rough integer minutes instead of strictly checking if the punch occurred after 10 minutes 00 seconds from shift start.
- **Symptom:** Punches at exact grace boundary (09:40:00) failed or miscalculated total late minutes from shift start.
- **Fix:** Computed total elapsed seconds from `shift_start_dt`. If `diff_sec > 600`, floored total minutes as `diff_sec // 60`.

### 8. Banker's rounding bias on work hours (Rule R4)
- **Where:** `compute_work_hours`
- **Defect:** Used standard Python `round()` which applies round-half-to-even logic.
- **Symptom:** Precision discrepancies on boundary values (e.g., `4.495` vs `4.50`).
- **Fix:** Implemented `Decimal` arithmetic with `ROUND_HALF_UP`.

### 9. Internal `_id` leakage in API responses
- **Where:** `POST /attendance/punch-in` and `GET /attendance`
- **Defect:** Returned MongoDB `_id` as `doc["id"] = str(...)`. Contract states `_id` is strictly internal and never exposed.
- **Symptom:** Response schema mismatch against OpenAPI v2.0 specification.
- **Fix:** Removed all `_id` and `id` references; records are identified strictly by `(emp_code, date)`.

### 10. In-memory loading and sorting on attendance listing
- **Where:** `GET /attendance`
- **Defect:** Loaded the full collection into memory via `list(db.attendance_logs.find(q))` before sorting and slicing in Python.
- **Symptom:** High memory footprint and latency spikes at scale (100k+ documents).
- **Fix:** Pushed sorting, skipping, and limiting directly down to MongoDB cursor operations backed by compound indexes.