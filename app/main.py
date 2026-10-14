"""Employee Attendance & Analytics API for the HROne engineering assignment.

Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB (environment variables override .env).
"""
import os
import re
import calendar
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
import json
from bson import ObjectId
from bson.json_util import dumps as bson_dumps

load_dotenv(override=False)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    """Create the indexes used by writes, list queries, lookups and analytics."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_date")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_late_emp")
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp_asc")


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


def _date(value: str, field: str = "date") -> Date:
    try:
        parsed = Date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD")


def _month_range(month: str) -> tuple[str, str, Date, Date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    start = Date(year, mon, 1)
    end = Date(year, mon, calendar.monthrange(year, mon)[1])
    return start.isoformat(), end.isoformat(), start, end


def _round_half_up(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _to_datetime(ms: int) -> datetime:
    """Convert validated epoch milliseconds to a whole-second UTC datetime."""
    if type(ms) is not int or not MIN_EPOCH_MS <= ms <= MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail="timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def _from_datetime(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # PyMongo commonly returns naive UTC datetimes. Treat those as UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def _utc_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = _utc_aware(punch_in).astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = _utc_aware(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start = datetime.combine(local.date(), time(h, m), tzinfo=IST)
    elapsed = int((local - start).total_seconds() // 60)
    seconds_elapsed = (local - start).total_seconds()
    return elapsed if seconds_elapsed > 600 else 0


def _shift_end_datetime(punch_in: datetime, shift_start: str, shift_end: str, attendance_day: str) -> datetime:
    h, m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    day = _date(attendance_day)
    overnight = shift_end <= shift_start
    end_day = day + timedelta(days=1) if overnight else day
    return datetime.combine(end_day, time(h, m), tzinfo=IST).astimezone(UTC)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (_utc_aware(punch_out) - _utc_aware(punch_in)).total_seconds()
    return _round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    h, m = map(int, shift_end.split(":"))
    start_h, start_m = map(int, shift_start.split(":"))
    day = _date(date_str)
    overnight = shift_end <= shift_start
    end_day = day + timedelta(days=1) if overnight else day
    end = datetime.combine(end_day, time(h, m), tzinfo=IST).astimezone(UTC)
    seconds = (_utc_aware(punch_out) - end).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def _record_out(doc: dict) -> dict:
    """API representation; no MongoDB _id is exposed and instants become epoch ms."""
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    result["punch_in"] = _from_datetime(result.get("punch_in"))
    result["punch_out"] = _from_datetime(result.get("punch_out"))
    for entry in result.get("history", []):
        entry["at"] = _from_datetime(entry.get("at"))
        for field, pair in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                pair["from"] = _from_datetime(pair.get("from"))
                pair["to"] = _from_datetime(pair.get("to"))
    return result


def _employee_out(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = _from_datetime(result["created_at"])
    return result


def _normalise_seeded_record(doc: dict) -> dict:
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("work_hours", None)
    doc.setdefault("punch_in", None)
    doc.setdefault("punch_out", None)
    return doc


def _page_params(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            if Date.fromisoformat(v).isoformat() != v:
                raise ValueError
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return _employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _page_params(page, page_size)
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = [_employee_out(d) for d in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "emp_code": 1, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = _to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    attendance_day = _attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": attendance_day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return _record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "emp_code": 1, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = _to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    # Find the most recent punch-in first. Do not skip an already-closed newer record.
    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not current:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record already punched out")
    pin = _utc_aware(current["punch_in"])
    elapsed = (ts - pin).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    work_hours = compute_work_hours(pin, ts)
    overtime = compute_overtime(ts, emp["shift_end"], current["date"], emp["shift_start"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}]},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="attendance record already punched out")
    return _record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _page_params(page, page_size)
    q: dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from:
        _date(date_from, "date_from")
    if date_to:
        _date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [_record_out(_normalise_seeded_record(d)) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    _date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current:
        raise HTTPException(status_code=404, detail="attendance record not found")
    original_current = dict(current)
    current = _normalise_seeded_record(current)
    patch = body.model_dump(exclude_unset=True)
    requested_status = patch.get("status", current["status"])
    if requested_status in ("ABSENT", "LEAVE"):
        if "punch_in" in patch or "punch_out" in patch:
            raise HTTPException(422, detail="ABSENT/LEAVE cannot include punch times")
        new_pin = None
        new_pout = None
    else:
        pin_value = patch.get("punch_in", _from_datetime(current.get("punch_in")))
        pout_value = patch.get("punch_out", _from_datetime(current.get("punch_out")))
        if pin_value is None:
            raise HTTPException(422, detail="presence status requires punch_in")
        new_pin = _to_datetime(pin_value)
        new_pout = _to_datetime(pout_value) if pout_value is not None else None
        if _attendance_date(new_pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, detail="punch_in must belong to the attendance date")
        if new_pout is not None:
            elapsed = (new_pout - new_pin).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(422, detail="punch_out must be after punch_in and within 24 hours")
    late = compute_late_minutes(new_pin, emp["shift_start"]) if new_pin else 0
    if new_pin and new_pout:
        hours = compute_work_hours(new_pin, new_pout)
        overtime = compute_overtime(new_pout, emp["shift_end"], date, emp["shift_start"])
        half_day = hours < 4.50
    else:
        hours, overtime, half_day = None, 0, False
    desired = {"status": requested_status, "punch_in": new_pin, "punch_out": new_pout, "work_hours": hours,
               "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}
    changes = {}
    for key, new_value in desired.items():
        old_value = current.get(key)
        if key in ("punch_in", "punch_out"):
            old_cmp, new_cmp = _from_datetime(old_value), _from_datetime(new_value)
        else:
            old_cmp, new_cmp = old_value, new_value
        if old_cmp != new_cmp:
            changes[key] = {"from": old_cmp, "to": new_cmp}
    if not changes:
        raise HTTPException(status_code=422, detail="regularization does not change anything")
    now = datetime.now(UTC).replace(microsecond=0)
    changes_db = {key: dict(pair) for key, pair in changes.items()}
    for field, pair in changes_db.items():
        if field in ("punch_in", "punch_out"):
            pair["from"] = _to_datetime(pair["from"]) if pair["from"] is not None else None
            pair["to"] = _to_datetime(pair["to"]) if pair["to"] is not None else None
    history_entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes_db}
    # Optimistic compare-and-swap: concurrent corrections cannot overwrite each other's audit trail.
    filter_doc: dict[str, Any] = {"_id": original_current["_id"], "emp_code": emp_code, "date": date}
    for key in ("status", "punch_in", "punch_out", "history"):
        if key in original_current:
            filter_doc[key] = original_current[key]
        else:
            filter_doc[key] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        filter_doc,
        {"$set": desired, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently; retry")
    return _record_out(_normalise_seeded_record(updated))


# Aggregation helpers: analytics calculations intentionally happen in MongoDB.
def _working_days_expr(start_expr: Any, end_expr: Any) -> dict:
    """Weekdays in inclusive [start_expr, end_expr], using MongoDB expressions."""
    return {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start_expr, "endDate": end_expr, "unit": "day"}}, 1]}]}, "as": "i", "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$i"}}}}, "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}


def _month_date_expr(value: str) -> dict:
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "UTC"}}


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start_s, end_s, start, end = _month_range(month)
    start_dt, end_dt = datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)
    # Use employee as the root so employees with no logs still return a zero-valued summary.
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}
        ], "as": "logs"}},
        {"$project": {
            "emp_code": 1,
            "working_days": {"$cond": [{"$gt": ["$joined_on", end_s]}, 0, _working_days_expr(_month_date_expr({"$cond": [{"$gt": ["$joined_on", start_s]}, "$joined_on", start_s]}), _month_date_expr(end_s))]},
            "logs": 1,
        }},
        {"$project": {
            "emp_code": 1, "working_days": 1,
            "present_days": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [{"$in": ["$$l.status", list(PRESENCE)]}, {"$gte": [{"$dayOfWeek": _month_date_expr("$$l.date")}, 2]}, {"$lte": [{"$dayOfWeek": _month_date_expr("$$l.date")}, 6]}]}}}, "as": "l", "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}}}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    _month_range(month)
    if not db.employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="employee not found")
    result = next(db.employees.aggregate(_employee_monthly_pipeline(emp_code, month)), None)
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    working = int(result.get("working_days", 0))
    pct = _round_half_up(result.get("present_days", 0) / working * 100, 2) if working else None
    return {"emp_code": emp_code, "month": month, "working_days": working,
            "present_days": _round_half_up(result.get("present_days", 0), 2),
            "leave_days": int(result.get("leave_days", 0)), "late_count": int(result.get("late_count", 0)),
            "total_late_minutes": int(result.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(result.get("total_overtime_minutes", 0)), "attendance_pct": pct}


def _department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start_s, end_s, _, _ = _month_range(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end_s}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"}, "department": {"$first": "$department"},
            "present_days": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$cond": [{"$and": [{"$gte": [{"$dayOfWeek": _month_date_expr("$logs.date")}, 2]}, {"$lte": [{"$dayOfWeek": _month_date_expr("$logs.date")}, 6]}]}, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    _month_range(month)
    items = list(db.employees.aggregate(_department_summary_pipeline(month, department)))
    for item in items:
        item["present_days"] = _round_half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = _round_half_up(item.get("avg_work_hours"), 2)
        for k in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            item[k] = int(item.get(k, 0))
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    start_s, end_s, _, _ = _month_range(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50), department: Optional[str] = None,
):
    _month_range(month)
    items = list(db.attendance_logs.aggregate(_late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def _trend_pipeline(department: str, from_day: str, to_day: str) -> list[dict]:
    from_date, to_date = _date(from_day, "from"), _date(to_day, "to")
    day_count = (to_date - from_date).days + 1
    if day_count <= 0 or day_count > 92:
        raise HTTPException(422, detail="to must be >= from and range must not exceed 92 days")
    start_dt = datetime.combine(from_date, time.min, UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"employees": 1, "days": {"$map": {"input": {"$range": [0, day_count]}, "as": "i", "in": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"day_string": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "UTC"}},
                   "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$days"}, 2]}, {"$lte": [{"$dayOfWeek": "$days"}, 6]}]},
                   "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "UTC"}}]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$day_string", "codes": {"$map": {"input": "$employees", "as": "e", "in": "$$e.emp_code"}}}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}}
        ], "as": "logs"}},
        {"$set": {
            "present_count": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE)]}}}, "as": "l", "in": {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
        }},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]} }},
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$day_string", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_raw": 1}},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: str = Query(..., alias="from"), to_day: str = Query(..., alias="to")):
    start, end = _date(from_day, "from"), _date(to_day, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, detail="to must be >= from and range must not exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, detail="department not found")
    rows = list(db.employees.aggregate(_trend_pipeline(department, from_day, to_day)))
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving = row.get("moving_avg_raw")
        items.append({"date": row["date"], "is_working_day": row["is_working_day"], "headcount": int(row.get("headcount", 0)),
                      "present_count": _round_half_up(row.get("present_count", 0), 2), "late_count": int(row.get("late_count", 0)),
                      "attendance_rate": _round_half_up(rate, 4), "moving_avg_7d": _round_half_up(moving, 4)})
    return {"department": department, "items": items}


def _explain_aggregate(collection, pipeline: list[dict]) -> dict:
    # Raw MongoDB explain with executionStats verbosity for the same pipeline used by the endpoint.
    result = db.command({
        "explain": {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {},
        },
        "verbosity": "executionStats",
    })
    # Convert BSON-specific values, including Timestamp, to JSON-compatible data.
    return json.loads(bson_dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_day: Optional[str] = Query(None, alias="from"),
    to_day: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _page_params(page, page_size)
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from:
            _date(date_from, "date_from")
            q.setdefault("date", {})["$gte"] = date_from
        if date_to:
            _date(date_to, "date_to")
            q.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must be <= date_to")
        if status: q["status"] = status
        # Explain the same filter, sort, skip and limit as GET /attendance.
        pipeline = [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]
        explain = _explain_aggregate(db.attendance_logs, pipeline)
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        _month_range(month)
        explain = _explain_aggregate(db.employees, _employee_monthly_pipeline(emp_code, month))
        collection = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        _month_range(month)
        explain = _explain_aggregate(db.employees, _department_summary_pipeline(month, department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        _month_range(month)
        explain = _explain_aggregate(db.attendance_logs, _late_leaderboard_pipeline(month, limit, department))
        collection = "attendance_logs"
    else:
        if not department or not from_day or not to_day:
            raise HTTPException(422, detail="department, from and to are required for department_trend")
        explain = _explain_aggregate(db.employees, _trend_pipeline(department, from_day, to_day))
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
