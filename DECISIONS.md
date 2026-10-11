# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.


# Architectural & Design Decisions (Part C)

### 1. Indexes Created & Rationale
We created a compound unique index on `attendance_logs(emp_code, date)` to guarantee business record uniqueness and race-safety. Supporting compound indexes on `(date, emp_code)`, `(status, date)`, and `employees(department, emp_code)` ensure that listing and analytical aggregation pipelines use index scans (`IXSCAN`) rather than collection scans (`COLLSCAN`), providing deterministic sub-millisecond query execution on 100k+ records.

### 2. Punch-In Concurrency
Instead of relying on multi-step application checks (`find_one` followed by `insert`), concurrency is enforced at the database layer via the unique compound index on `(emp_code, date)`. When concurrent identical punch-ins arrive, exactly one write succeeds; all other simultaneous writes throw a `DuplicateKeyError`, intercepted and returned cleanly as HTTP `409 Conflict`.

### 3. Leaderboard Cutoff & Ties
Leaderboard rankings are calculated using MongoDB's `$setWindowFields` stage with the `$rank` operator. This adheres to standard competition ranking (1, 2, 2, 4) where ties share identical ranks and skip subsequent ones. Deterministic tie-breaking is handled by sorting on `total_late_minutes DESC, emp_code ASC` prior to applying `$limit`, ensuring tied candidates are never arbitrarily truncated.

### 4. Department Summary Zero-Log Headcount
The department aggregation starts from the `employees` collection filtered by `joined_on <= period_end` rather than from `attendance_logs`. A `$lookup` with `preserveNullAndEmptyArrays: true` brings in available logs without dropping employees who have zero attendance records, guaranteeing exact active headcount reporting.

### 5. Scaling to 100x Data (10M+ Documents)
To support 100x volume, we would shard `attendance_logs` using a compound hashed shard key on `(emp_code, date)` or range-shard on `date`. We would decouple analytical reads by directing aggregation queries to secondary read replicas using `ReadPreference.SECONDARY_PREFERRED`, and maintain incremental daily rollup collections via MongoDB Change Streams.