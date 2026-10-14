"""Smoke tests for the HR Attendance FastAPI project.
Run app: uvicorn app.main:app --reload --port 8000
Run tests: python test_api.py
Safe GET tests only; write endpoints are not exercised.
"""
import json, sys, urllib.error, urllib.parse, urllib.request
BASE = "http://127.0.0.1:8000"
EMP = "EMP0001"
MONTH = "2026-07"
DEPT = "Engineering"
passed = failed = 0

def call(method, path, params=None, body=None):
    url = BASE + path
    if params: url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try: payload = json.loads(raw) if raw else None
        except json.JSONDecodeError: payload = raw
        return e.code, payload

def test(name, method, path, expected=200, params=None, check=None):
    global passed, failed
    try:
        status, data = call(method, path, params)
        okay = status == expected and (check(data) if check else True)
        if okay:
            passed += 1; print(f"[PASS] {name} (HTTP {status})")
        else:
            failed += 1; print(f"[FAIL] {name}: expected {expected}, got {status}; response={str(data)[:350]}")
    except Exception as e:
        failed += 1; print(f"[FAIL] {name}: {e}")

def main():
    print(f"Testing {BASE}\n")
    test("Health / MongoDB ping", "GET", "/health", check=lambda d: isinstance(d, dict) and d.get("status") == "ok")
    test("List employees + pagination", "GET", "/employees", params={"page":1,"page_size":20}, check=lambda d: isinstance(d,dict) and isinstance(d.get("items"),list) and isinstance(d.get("total"),int) and d.get("page")==1)
    test("Employee department filter", "GET", "/employees", params={"department":DEPT}, check=lambda d: isinstance(d,dict) and all(x.get("department")==DEPT for x in d.get("items",[])))
    test("Reject page_size above 100", "GET", "/employees", 422, {"page":1,"page_size":101})
    test("Attendance list structure", "GET", "/attendance", params={"emp_code":EMP,"page":1,"page_size":20}, check=lambda d: isinstance(d,dict) and isinstance(d.get("items"),list) and isinstance(d.get("total"),int))
    test("Attendance filters", "GET", "/attendance", params={"date_from":"2026-07-01","date_to":"2026-07-31","status":"PRESENT"}, check=lambda d: isinstance(d,dict) and all(x.get("status")=="PRESENT" and "2026-07-01"<=x.get("date","")<="2026-07-31" for x in d.get("items",[])))
    test("Reject reversed attendance date range", "GET", "/attendance", 422, {"date_from":"2026-07-08","date_to":"2026-07-06"})
    test("Reject malformed attendance date", "GET", "/attendance", 422, {"date_from":"07-06-2026"})
    test("Reject invalid attendance status", "GET", "/attendance", 422, {"status":"HOLIDAY"})
    test("Employee monthly analytics", "GET", f"/analytics/employees/{EMP}/monthly", params={"month":MONTH}, check=lambda d: isinstance(d,dict) and d.get("emp_code")==EMP and d.get("month")==MONTH and all(k in d for k in ("working_days","present_days","leave_days","late_count","total_late_minutes","total_overtime_minutes","attendance_pct")))
    test("Unknown employee returns 404", "GET", "/analytics/employees/EMP9999/monthly", 404, {"month":MONTH})
    test("Invalid month returns 422", "GET", f"/analytics/employees/{EMP}/monthly", 422, {"month":"2026-13"})
    test("Department summary analytics", "GET", "/analytics/departments/summary", params={"month":MONTH}, check=lambda d: isinstance(d,dict) and d.get("month")==MONTH and isinstance(d.get("items"),list))
    test("Late leaderboard analytics", "GET", "/analytics/leaderboard/late", params={"month":MONTH,"limit":10}, check=lambda d: isinstance(d,dict) and d.get("month")==MONTH and isinstance(d.get("items"),list))
    trend = f"/analytics/departments/{urllib.parse.quote(DEPT,safe='')}/trend"
    test("Department trend analytics", "GET", trend, params={"from":"2026-07-06","to":"2026-07-08"}, check=lambda d: isinstance(d,dict) and d.get("department")==DEPT and isinstance(d.get("items"),list))
    test("Trend rejects reversed date range", "GET", trend, 422, {"from":"2026-07-08","to":"2026-07-06"})
    test("Trend rejects range over 92 days", "GET", trend, 422, {"from":"2026-01-01","to":"2026-04-10"})
    cases = [("attendance_list",{"emp_code":EMP,"page":1,"page_size":20}), ("employee_monthly",{"emp_code":EMP,"month":MONTH}), ("department_summary",{"month":MONTH}), ("late_leaderboard",{"month":MONTH}), ("department_trend",{"department":DEPT,"from":"2026-07-06","to":"2026-07-08"})]
    for endpoint, params in cases:
        test(f"MongoDB explain: {endpoint}", "GET", f"/admin/explain/{endpoint}", params=params, check=lambda d: isinstance(d,dict) and all(k in d for k in ("endpoint","collection","explain")))
    print(f"\nRESULT: {passed} PASS | {failed} FAIL")
    if failed:
        print("Check server logs, MongoDB connection, seed data and department name."); sys.exit(1)
    print("All executed smoke tests passed. Write endpoints were not called to avoid changing data."); sys.exit(0)

if __name__ == "__main__": main()
