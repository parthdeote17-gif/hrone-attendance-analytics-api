# REVIEW.md

## Defects Found in the Starter Code

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `_explain_aggregate()` | MongoDB aggregation explain results can contain BSON-specific values, such as `Timestamp`, that FastAPI's normal JSON serialization cannot handle. | Calling the `/admin/explain/{endpoint}` routes returned HTTP 500, with a serialization error involving BSON `Timestamp`. | Converted the explain result to BSON-aware Extended JSON and parsed it into a JSON-compatible Python object using `json.loads(bson_dumps(result))`. |

## Reviewed but Not Considered Defects

- **Root URL (`/`):** Returning `404 Not Found` is expected if no root route is defined. It does not by itself indicate an API defect.
- **Punch-in duplicate protection:** The unique compound index on `emp_code` and `date`, together with handling for `DuplicateKeyError`, prevents duplicate attendance records for the same employee and attendance date.
- **Leaderboard ties:** Returning more employees than the requested limit can be intentional when all employees tied at the cutoff rank are included.
- **Employees without attendance logs:** Preserving empty results during `$unwind` allows employees with no attendance records to remain in department-summary calculations.
