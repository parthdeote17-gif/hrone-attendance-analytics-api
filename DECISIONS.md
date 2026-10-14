# DECISIONS.md

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes and a unique compound index on `attendance_logs.emp_code` and `date` to enforce one attendance record per employee per day. Other indexes support department, date, status, employee, and late-minute queries. I did not add a separate single-field status index because the existing compound status-date index starts with `status`.

### 2. Punch-in race
Both requests may check the employee and calculate the same attendance date. When they insert their records, MongoDB's unique compound index allows only one record for that employee and date. The other insert raises `DuplicateKeyError`, which the API converts into HTTP 409 Conflict.

### 3. Ties
The leaderboard uses MongoDB's `$rank` based on total late minutes. Employees tied at the cutoff receive the same rank and are all included, so the response may contain more employees than the requested limit. Employee code provides a stable display order.

### 4. Headcount
The summary starts from employees and uses `$lookup` for attendance logs. Preserving empty results during `$unwind` ensures employees without logs are still counted.

### 5. Scaling
For 100 times more data, I would review explain execution statistics and optimise indexes and aggregation pipelines using real query patterns.