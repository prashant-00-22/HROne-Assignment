"""
Employee Attendance & Analytics API
Full Production Implementation meeting Contract v2.0
"""
import os
import math
import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone, date as dt_date
from typing import Optional, List, Any, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "hrone_attendance")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Startup & Index Creation (Idempotent)
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def setup_indexes():
    # Employees Indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # Attendance Logs Indexes
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("department", ASCENDING), ("date", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)])
    db.attendance_logs.create_index([("late_minutes", ASCENDING)])


# --------------------------------------------------------------------------- #
# Calculation Helpers (Rules R1 - R5)
# --------------------------------------------------------------------------- #
def truncate_ms(ms: int) -> int:
    return math.floor(ms / 1000) * 1000


def ms_to_ist_datetime(ms: int) -> datetime:
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(IST)


def ist_datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def round_half_up(val: float, places: int = 2) -> float:
    d = Decimal(str(val))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def resolve_shift_and_date(punch_in_dt: datetime, shift_start_str: str, shift_end_str: str) -> tuple[str, datetime, datetime]:
    sh_h, sh_m = map(int, shift_start_str.split(":"))
    eh_h, eh_m = map(int, shift_end_str.split(":"))

    calendar_date = punch_in_dt.date()
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    if is_overnight and (punch_in_dt.hour, punch_in_dt.minute) < (eh_h, eh_m):
        attendance_date = calendar_date - timedelta(days=1)
    else:
        attendance_date = calendar_date

    shift_start_dt = datetime(
        attendance_date.year, attendance_date.month, attendance_date.day,
        sh_h, sh_m, 0, tzinfo=IST
    )

    if is_overnight:
        shift_end_date = attendance_date + timedelta(days=1)
        shift_end_dt = datetime(
            shift_end_date.year, shift_end_date.month, shift_end_date.day,
            eh_h, eh_m, 0, tzinfo=IST
        )
    else:
        shift_end_dt = datetime(
            attendance_date.year, attendance_date.month, attendance_date.day,
            eh_h, eh_m, 0, tzinfo=IST
        )

    return attendance_date.isoformat(), shift_start_dt, shift_end_dt


def compute_late_minutes_exact(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    diff_sec = int((punch_in_dt - shift_start_dt).total_seconds())
    if diff_sec > 600:
        return max(0, diff_sec // 60)
    return 0


def compute_overtime_exact(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    diff_sec = int((punch_out_dt - shift_end_dt).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_work_hours_exact(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = max(0, int((punch_out_dt - punch_in_dt).total_seconds()))
    return round_half_up(diff_sec / 3600.0, 2)


# --------------------------------------------------------------------------- #
# Request / Response Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError("punched_at must be an integer epoch in milliseconds")
            if v < 1_000_000_000_000:
                raise ValueError("punched_at must be in milliseconds, not seconds")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, v):
        if v is not None:
            if not isinstance(v, int):
                raise ValueError("punched_at must be an integer epoch in milliseconds")
            if v < 1_000_000_000_000:
                raise ValueError("punched_at must be in milliseconds, not seconds")
        return v


class CorrectionIn(BaseModel):
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    status: Optional[str] = None
    reason: str
    modified_by: str


def serialize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    for field in ["punch_in", "punch_out", "created_at"]:
        if field in doc and isinstance(doc[field], datetime):
            doc[field] = ist_datetime_to_ms(doc[field])
    if "history" in doc and isinstance(doc["history"], list):
        for h in doc["history"]:
            if "modified_at" in h and isinstance(h["modified_at"], datetime):
                h["modified_at"] = ist_datetime_to_ms(h["modified_at"])
            for sub in ["old_value", "new_value"]:
                if sub in h and isinstance(h[sub], dict):
                    for k in ["punch_in", "punch_out"]:
                        if k in h[sub] and isinstance(h[sub][k], datetime):
                            h[sub][k] = ist_datetime_to_ms(h[sub][k])
    return doc


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    page_size = min(max(1, page_size), 100)
    page = max(1, page)
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid punch-in status")

    raw_ms = body.punched_at if body.punched_at is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    trunc_ms = truncate_ms(raw_ms)
    punch_dt = ms_to_ist_datetime(trunc_ms)

    att_date, shift_start_dt, _ = resolve_shift_and_date(punch_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    late_mins = compute_late_minutes_exact(punch_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    raw_ms = body.punched_at if body.punched_at is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    trunc_ms = truncate_ms(raw_ms)
    punch_out_dt = ms_to_ist_datetime(trunc_ms)

    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": None}, sort=[("date", DESCENDING)])
    if not rec:
        raise HTTPException(404, "No active punch-in found")

    punch_in_dt = rec["punch_in"]
    if isinstance(punch_in_dt, int):
        punch_in_dt = ms_to_ist_datetime(punch_in_dt)
    elif punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc).astimezone(IST)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    _, _, shift_end_dt = resolve_shift_and_date(punch_in_dt, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    work_hrs = compute_work_hours_exact(punch_in_dt, punch_out_dt)
    ot_mins = compute_overtime_exact(punch_out_dt, shift_end_dt)
    is_half_day = work_hrs < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_dt,
            "work_hours": work_hrs,
            "overtime_minutes": ot_mins,
            "half_day": is_half_day
        }},
        return_document=True
    )

    if not updated:
        raise HTTPException(409, "Punch-out already processed concurrently")

    return serialize_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    page_size = min(max(1, page_size), 100)
    page = max(1, page)
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: CorrectionIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "Attendance record not found for this date")

    old_vals = {}
    new_vals = {}
    updates = {}

    target_status = body.status if body.status is not None else rec.get("status")
    if body.status is not None and body.status != rec.get("status"):
        old_vals["status"] = rec.get("status")
        new_vals["status"] = body.status
        updates["status"] = body.status

    target_punch_in = rec.get("punch_in")
    if body.punch_in is not None:
        p_in_dt = ms_to_ist_datetime(truncate_ms(body.punch_in))
        if p_in_dt != target_punch_in:
            old_vals["punch_in"] = target_punch_in
            new_vals["punch_in"] = p_in_dt
            updates["punch_in"] = p_in_dt
            target_punch_in = p_in_dt

    target_punch_out = rec.get("punch_out")
    if body.punch_out is not None:
        p_out_dt = ms_to_ist_datetime(truncate_ms(body.punch_out))
        if p_out_dt != target_punch_out:
            old_vals["punch_out"] = target_punch_out
            new_vals["punch_out"] = p_out_dt
            updates["punch_out"] = p_out_dt
            target_punch_out = p_out_dt

    if target_status in ["ABSENT", "LEAVE"]:
        updates["punch_in"] = None
        updates["punch_out"] = None
        updates["work_hours"] = None
        updates["late_minutes"] = 0
        updates["overtime_minutes"] = 0
        updates["half_day"] = False
    elif target_punch_in:
        p_in = target_punch_in if isinstance(target_punch_in, datetime) else ms_to_ist_datetime(target_punch_in)
        _, s_start, s_end = resolve_shift_and_date(p_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        updates["late_minutes"] = compute_late_minutes_exact(p_in, s_start)

        if target_punch_out:
            p_out = target_punch_out if isinstance(target_punch_out, datetime) else ms_to_ist_datetime(target_punch_out)
            if p_out <= p_in:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            wh = compute_work_hours_exact(p_in, p_out)
            updates["work_hours"] = wh
            updates["overtime_minutes"] = compute_overtime_exact(p_out, s_end)
            updates["half_day"] = wh < 4.50
        else:
            updates["work_hours"] = None
            updates["overtime_minutes"] = 0
            updates["half_day"] = False

    history_entry = {
        "modified_at": datetime.now(tz=timezone.utc),
        "modified_by": body.modified_by,
        "reason": body.reason,
        "old_value": old_vals,
        "new_value": new_vals
    }

    result = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date},
        {
            "$set": updates,
            "$push": {"history": history_entry}
        },
        return_document=True
    )
    return serialize_doc(result)


# --------------------------------------------------------------------------- #
# Analytics Aggregations
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}"}
        }},
        {"$group": {
            "_id": None,
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$eq": ["$half_day", False]}
                        ]},
                        1.0,
                        {"$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$eq": ["$half_day", True]}
                            ]},
                            0.5,
                            0.0
                        ]}
                    ]
                }
            }
        }}
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        return {
            "emp_code": emp_code,
            "month": month,
            "present_days": 0.0,
            "total_work_hours": 0.0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    data = res[0]
    return {
        "emp_code": emp_code,
        "month": month,
        "present_days": round_half_up(data["present_days"], 2),
        "total_work_hours": round_half_up(data["total_work_hours"], 2),
        "total_late_minutes": int(data["total_late_minutes"]),
        "total_overtime_minutes": int(data["total_overtime_minutes"])
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    pipeline = [
        {"$match": {"joined_on": {"$lte": f"{month}-31"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$regexMatch": {"input": "$date", "regex": f"^{month}"}}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "employees": {"$addToSet": "$emp_code"},
            "total_work_hours": {"$sum": {"$ifNull": ["$logs.work_hours", 0]}},
            "record_count": {"$sum": {"$cond": [{"$ifNull": ["$logs.work_hours", False]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$eq": ["$logs.half_day", False]}
                        ]},
                        1.0,
                        {"$cond": [
                            {"$and": [
                                {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$eq": ["$logs.half_day", True]}
                            ]},
                            0.5,
                            0.0
                        ]}
                    ]
                }
            }
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$employees"},
            "total_work_hours": "$total_work_hours",
            "avg_work_hours": {
                "$cond": [
                    {"$gt": ["$record_count", 0]},
                    {"$divide": ["$total_work_hours", "$record_count"]},
                    0.0
                ]
            },
            "present_days": "$present_days",
            "total_late_minutes": "$total_late_minutes",
            "total_overtime_minutes": "$total_overtime_minutes"
        }},
        {"$sort": {"department": 1}}
    ]
    rows = list(db.employees.aggregate(pipeline))
    for r in rows:
        r.pop("_id", None)
        r["total_work_hours"] = round_half_up(r["total_work_hours"], 2)
        r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2)
        r["present_days"] = round_half_up(r["present_days"], 2)
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def leaderboard_late(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    limit: int = 10
):
    pipeline = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp_info"
        }},
        {"$unwind": "$emp_info"},
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp_info.name",
            "department": "$emp_info.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$sort": {"rank": 1, "emp_code": 1}},
        {"$limit": limit}
    ]
    results = list(db.attendance_logs.aggregate(pipeline))
    for r in results:
        r.pop("_id", None)
    return {"items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    emp_codes = [e["emp_code"] for e in emps]

    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$group": {
            "_id": "$date",
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$eq": ["$half_day", False]}]},
                        1.0,
                        {"$cond": [
                            {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$eq": ["$half_day", True]}]},
                            0.5,
                            0.0
                        ]}
                    ]
                }
            }
        }}
    ]
    raw_data = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    start_dt = dt_date.fromisoformat(from_date)
    end_dt = dt_date.fromisoformat(to_date)
    curr = start_dt
    all_days = []

    while curr <= end_dt:
        d_str = curr.isoformat()
        if d_str in raw_data:
            all_days.append({
                "date": d_str,
                "present_count": round_half_up(raw_data[d_str]["present_count"], 2),
                "total_work_hours": round_half_up(raw_data[d_str]["total_work_hours"], 2)
            })
        else:
            all_days.append({
                "date": d_str,
                "present_count": 0.0,
                "total_work_hours": 0.0
            })
        curr += timedelta(days=1)

    for i in range(len(all_days)):
        window = all_days[max(0, i - 6): i + 1]
        sum_hrs = sum(w["total_work_hours"] for w in window)
        all_days[i]["moving_avg_hours"] = round_half_up(sum_hrs / len(window), 2)

    return {"department": department, "items": all_days}


# --------------------------------------------------------------------------- #
# Part C: Explain Endpoint (Robust BSON Handling)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = 10
):
    try:
        raw_res = None
        if endpoint == "attendance_list":
            cmd = {
                "find": "attendance_logs",
                "filter": {},
                "sort": {"date": -1, "emp_code": 1},
                "limit": 20
            }
            raw_res = db.command("explain", cmd, verbosity="executionStats")

        elif endpoint == "employee_monthly":
            pipe = [
                {"$match": {"emp_code": emp_code or "EMP0001", "date": {"$regex": f"^{month or '2026-10'}"}}},
                {"$group": {"_id": None, "total": {"$sum": "$late_minutes"}}}
            ]
            raw_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")

        elif endpoint == "department_summary":
            pipe = [
                {"$match": {"joined_on": {"$lte": f"{month or '2026-10'}-31"}}},
                {"$group": {"_id": "$department", "count": {"$sum": 1}}}
            ]
            raw_res = db.command("explain", {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")

        elif endpoint == "late_leaderboard":
            pipe = [
                {"$match": {"date": {"$gte": date_from or "2026-10-01", "$lte": date_to or "2026-10-31"}, "late_minutes": {"$gt": 0}}},
                {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
                {"$sort": {"total": -1, "_id": 1}},
                {"$limit": limit}
            ]
            raw_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")

        elif endpoint == "department_trend":
            pipe = [
                {"$match": {"date": {"$gte": date_from or "2026-10-01", "$lte": date_to or "2026-10-31"}}},
                {"$group": {"_id": "$date", "hours": {"$sum": "$work_hours"}}}
            ]
            raw_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, verbosity="executionStats")

        if raw_res is not None:
            # Use json_util to cleanly convert all BSON types (like ObjectId, regex, etc.) to valid JSON
            clean_json_str = json_util.dumps(raw_res)
            return Response(content=clean_json_str, media_type="application/json")

        raise HTTPException(404, f"Unknown explain target: {endpoint}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))