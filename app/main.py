"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


# --------------------------------------------------------------------------- #
# Startup: create indexes idempotently
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)]
    )
    db.employees.create_index("department")
    db.employees.create_index("joined_on")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def sanitize_mongo_doc(obj):
    """Sanitize MongoDB / BSON documents so binary hashes and BSON types are JSON serializable."""
    if isinstance(obj, dict):
        return {k: sanitize_mongo_doc(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_mongo_doc(v) for v in obj]
    elif isinstance(obj, bytes):
        return obj.hex()
    return obj


def round_half_up(value: float, decimals: int = 2) -> float:
    """Round using half-up (not banker's rounding)."""
    d = Decimal(str(value))
    quantize_str = "0." + "0" * decimals
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


def ensure_naive_utc(dt: datetime) -> datetime:
    """Convert any datetime to naive-UTC (what PyMongo stores/returns by default)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def truncate_to_seconds(dt: datetime) -> datetime:
    """Truncate a datetime to whole seconds (R1)."""
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a naive-UTC datetime, truncated to seconds."""
    # datetime.utcfromtimestamp gives naive datetime in UTC
    dt = datetime.utcfromtimestamp(ms / 1000)
    return truncate_to_seconds(dt)


def datetime_to_epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a (naive-UTC) datetime to epoch milliseconds."""
    if dt is None:
        return None
    # If naive, treat as UTC
    if dt.tzinfo is None:
        aware = dt.replace(tzinfo=timezone.utc)
    else:
        aware = dt
    return int(aware.timestamp() * 1000)


def validate_epoch_ms(value) -> int:
    """Validate that a value is a valid epoch millisecond integer."""
    if value is None:
        return None
    if isinstance(value, float):
        raise HTTPException(422, detail="Epoch milliseconds must be an integer, not a float")
    if isinstance(value, str):
        raise HTTPException(422, detail="Epoch milliseconds must be an integer, not a string")
    if not isinstance(value, (int,)):
        raise HTTPException(422, detail="Epoch milliseconds must be an integer")
    if value < 100000000000:
        raise HTTPException(422, detail="Epoch milliseconds value too small (looks like seconds)")
    if value > 4102444800000:
        raise HTTPException(422, detail="Epoch milliseconds value out of range")
    return value


def to_ist(dt: datetime) -> datetime:
    """Convert a naive-UTC datetime to IST-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def determine_attendance_date(punch_in_naive_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date per R1.
    For overnight shifts (shift_end <= shift_start), if the IST time of punch-in
    is earlier than shift_end, the record belongs to the previous day's shift.
    """
    ist_dt = to_ist(punch_in_naive_utc)
    ist_date = ist_dt.date()

    if shift_end <= shift_start:
        punch_time = ist_dt.strftime("%H:%M")
        if punch_time < shift_end:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_naive_utc: datetime, shift_start: str) -> int:
    """
    Compute late minutes per R2.
    Grace: 10 min. Late only if STRICTLY MORE THAN 10:00 after shift_start.
    late_minutes = floor(total minutes from shift_start). Otherwise 0.
    """
    ist_dt = to_ist(punch_in_naive_utc)
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = ist_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_seconds = (ist_dt - shift_start_dt).total_seconds()

    if diff_seconds < 0:
        return 0
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    diff_seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(diff_seconds / 3600, 2)


def compute_overtime_with_shift(punch_out_naive_utc: datetime, shift_start: str,
                                shift_end: str, attendance_date: str) -> int:
    """R3: floor minutes from shift_end to punch_out, counted only if >= 30."""
    h, m = map(int, shift_end.split(":"))
    att_date = date_type.fromisoformat(attendance_date)

    is_overnight = shift_end <= shift_start
    end_date = att_date + timedelta(days=1) if is_overnight else att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, h, m, 0, tzinfo=IST)
    punch_out_ist = to_ist(punch_out_naive_utc)
    diff = (punch_out_ist - shift_end_dt).total_seconds()

    if diff < 0:
        return 0
    overtime = int(diff // 60)
    return overtime if overtime >= 30 else 0


def format_attendance_record(doc: dict) -> dict:
    """Format a MongoDB attendance_logs document for API output."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": format_history(doc.get("history", [])),
    }


def format_history(history: list) -> list:
    """Format history entries: convert BSON dates to epoch ms."""
    formatted = []
    for entry in (history or []):
        fmt_entry = {
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field_name, change in entry.get("changes", {}).items():
            if field_name in ("punch_in", "punch_out"):
                fmt_entry["changes"][field_name] = {
                    "from": datetime_to_epoch_ms(change.get("from")),
                    "to": datetime_to_epoch_ms(change.get("to")),
                }
            else:
                fmt_entry["changes"][field_name] = {
                    "from": change.get("from"),
                    "to": change.get("to"),
                }
        formatted.append(fmt_entry)
    return formatted


def format_employee(doc: dict) -> dict:
    """Format a MongoDB employee document for API output."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def get_month_date_range(month_str: str):
    """Parse 'YYYY-MM' -> (first_day_str, last_day_str)."""
    year, month = map(int, month_str.split("-"))
    first_day = f"{year:04d}-{month:02d}-01"
    last_day_num = calendar.monthrange(year, month)[1]
    last_day = f"{year:04d}-{month:02d}-{last_day_num:02d}"
    return first_day, last_day


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """Count Mon-Fri days between two dates inclusive."""
    start = date_type.fromisoformat(start_date_str)
    end = date_type.fromisoformat(end_date_str)
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def validate_date_format(date_str: str) -> str:
    try:
        date_type.fromisoformat(date_str)
        return date_str
    except (ValueError, TypeError):
        raise HTTPException(422, detail=f"Invalid date format: {date_str}")


def validate_month_format(month_str: str) -> str:
    if not re.match(r'^\d{4}-(0[1-9]|1[0-2])$', month_str):
        raise HTTPException(422, detail=f"Invalid month format: {month_str}. Expected YYYY-MM.")
    return month_str


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v):
        if not re.match(r'^EMP\d{4,6}$', v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        if len(v) > 120 or not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v):
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v):
        if not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            date_type.fromisoformat(v)
        except (ValueError, TypeError):
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB not available")


# --- Employees ---

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    doc["created_at"] = ensure_naive_utc(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- Attendance: Punch In ---

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(PRESENCE_STATUSES)}")

    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at)
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = truncate_to_seconds(ensure_naive_utc(datetime.now(timezone.utc)))

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    att_date = determine_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    doc.pop("_id", None)
    return format_attendance_record(doc)


# --- Attendance: Punch Out ---

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at)
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = truncate_to_seconds(ensure_naive_utc(datetime.now(timezone.utc)))

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Find the most recent open record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "punch_out": None,
            "status": {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, detail="No open punch-in found")

    if ts <= record["punch_in"]:
        raise HTTPException(422, detail="punch_out must be after punch_in")

    if (ts - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    work_hrs = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime_with_shift(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hrs < 4.50

    # Atomic update: check punch_out is still None (concurrency)
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hrs,
            "overtime_minutes": overtime,
            "half_day": half_day,
        }},
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Already punched out (concurrent request)")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


# --- Attendance: List ---

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(ALL_STATUSES)}")

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
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_attendance_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --- Attendance: Regularize (PATCH) ---

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeIn = ...,
):
    validate_date_format(date)

    if not body.reason or len(body.reason) < 5 or len(body.reason) > 200:
        raise HTTPException(422, detail="reason must be 5-200 characters")
    if not body.regularized_by or len(body.regularized_by) > 50:
        raise HTTPException(422, detail="regularized_by must be 1-50 characters")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record found for this date")

    new_status = body.status
    if new_status is not None and new_status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(ALL_STATUSES)}")

    if body.punch_in is not None:
        validate_epoch_ms(body.punch_in)
    if body.punch_out is not None:
        validate_epoch_ms(body.punch_out)

    effective_status = new_status if new_status is not None else record["status"]

    # ABSENT/LEAVE clears punch times -> can't supply them
    if effective_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch_in or punch_out with ABSENT/LEAVE status")

    # Presence status requires punch_in
    if effective_status in PRESENCE_STATUSES:
        has_punch_in = body.punch_in is not None or record.get("punch_in") is not None
        if not has_punch_in:
            raise HTTPException(422, detail="Presence status requires a punch_in")

    changes = {}
    update_fields = {}

    # Status change
    if new_status is not None and new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
        update_fields["status"] = new_status

    if effective_status in ("ABSENT", "LEAVE"):
        # Clear punch times and reset derived fields
        old_pi = record.get("punch_in")
        old_po = record.get("punch_out")
        if old_pi is not None:
            changes["punch_in"] = {"from": old_pi, "to": None}
            update_fields["punch_in"] = None
        if old_po is not None:
            changes["punch_out"] = {"from": old_po, "to": None}
            update_fields["punch_out"] = None

        old_wh = record.get("work_hours")
        old_late = record.get("late_minutes", 0)
        old_ot = record.get("overtime_minutes", 0)
        old_hd = record.get("half_day", False)

        if old_wh is not None:
            changes["work_hours"] = {"from": old_wh, "to": None}
            update_fields["work_hours"] = None
        if old_late != 0:
            changes["late_minutes"] = {"from": old_late, "to": 0}
            update_fields["late_minutes"] = 0
        if old_ot != 0:
            changes["overtime_minutes"] = {"from": old_ot, "to": 0}
            update_fields["overtime_minutes"] = 0
        if old_hd is not False:
            changes["half_day"] = {"from": old_hd, "to": False}
            update_fields["half_day"] = False
    else:
        # Presence status path
        if body.punch_in is not None:
            new_pi = epoch_ms_to_datetime(body.punch_in)
            # Validate punch_in stays on the record's attendance date (R1)
            new_att_date = determine_attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
            if new_att_date != date:
                raise HTTPException(422, detail="punch_in must stay on the record's attendance date")
            old_pi = record.get("punch_in")
            if old_pi != new_pi:
                changes["punch_in"] = {"from": old_pi, "to": new_pi}
                update_fields["punch_in"] = new_pi

        if body.punch_out is not None:
            new_po = epoch_ms_to_datetime(body.punch_out)
            old_po = record.get("punch_out")
            if old_po != new_po:
                changes["punch_out"] = {"from": old_po, "to": new_po}
                update_fields["punch_out"] = new_po

        final_pi = update_fields.get("punch_in", record.get("punch_in"))
        final_po = update_fields.get("punch_out", record.get("punch_out"))

        if final_po is not None and final_pi is not None:
            if final_po <= final_pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (final_po - final_pi).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

        # Recompute derived fields
        new_late = compute_late_minutes(final_pi, emp["shift_start"])
        old_late = record.get("late_minutes", 0)
        if new_late != old_late:
            changes["late_minutes"] = {"from": old_late, "to": new_late}
            update_fields["late_minutes"] = new_late

        if final_po is not None:
            new_wh = compute_work_hours(final_pi, final_po)
            new_ot = compute_overtime_with_shift(final_po, emp["shift_start"], emp["shift_end"], date)
            new_hd = new_wh < 4.50

            old_wh = record.get("work_hours")
            old_ot = record.get("overtime_minutes", 0)
            old_hd = record.get("half_day", False)

            if old_wh != new_wh:
                changes["work_hours"] = {"from": old_wh, "to": new_wh}
                update_fields["work_hours"] = new_wh
            if old_ot != new_ot:
                changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
                update_fields["overtime_minutes"] = new_ot
            if old_hd != new_hd:
                changes["half_day"] = {"from": old_hd, "to": new_hd}
                update_fields["half_day"] = new_hd
        else:
            old_wh = record.get("work_hours")
            old_ot = record.get("overtime_minutes", 0)
            old_hd = record.get("half_day", False)
            if old_wh is not None:
                changes["work_hours"] = {"from": old_wh, "to": None}
                update_fields["work_hours"] = None
            if old_ot != 0:
                changes["overtime_minutes"] = {"from": old_ot, "to": 0}
                update_fields["overtime_minutes"] = 0
            if old_hd is not False:
                changes["half_day"] = {"from": old_hd, "to": False}
                update_fields["half_day"] = False

    if not changes:
        raise HTTPException(422, detail="No changes to apply")

    history_entry = {
        "at": ensure_naive_utc(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: check history length hasn't changed
    current_history_len = len(record.get("history", []))
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Concurrent modification detected")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
):
    validate_month_format(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = get_month_date_range(month)
    joined = emp["joined_on"]

    if joined > last_day:
        working_days = 0
    else:
        effective_start = max(joined, first_day)
        working_days = count_working_days(effective_start, last_day)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "date_parsed": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
            "effective_half_day": {"$ifNull": ["$half_day", False]},
            "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "effective_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
        }},
        {"$addFields": {
            "is_weekday": {"$and": [
                {"$gte": [{"$dayOfWeek": "$date_parsed"}, 2]},
                {"$lte": [{"$dayOfWeek": "$date_parsed"}, 6]},
            ]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    "$is_weekday",
                ]},
                {"$cond": ["$effective_half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$effective_late_minutes"},
            "total_overtime_minutes": {"$sum": "$effective_overtime_minutes"},
        }},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        r = results[0]
        present_days = r["present_days"]
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_ot = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
):
    validate_month_format(month)
    first_day, last_day = get_month_date_range(month)

    emp_match = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    # Headcount per department (R9)
    dept_emps = {
        d["_id"]: d
        for d in db.employees.aggregate([
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
        ])
    }

    if not dept_emps:
        return {"month": month, "items": []}

    # Attendance stats via aggregation with $lookup
    att_match = {"date": {"$gte": first_day, "$lte": last_day}}
    if department:
        emp_codes = dept_emps.get(department, {}).get("emp_codes", [])
        if not emp_codes:
            return {"month": month, "items": []}
        att_match["emp_code"] = {"$in": emp_codes}

    att_pipeline = [
        {"$match": att_match},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$addFields": {
            "department": "$employee.department",
            "effective_half_day": {"$ifNull": ["$half_day", False]},
            "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
        }},
        {"$group": {
            "_id": "$department",
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": ["$effective_half_day", 0.5, 1]},
                0,
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$type": "$work_hours"}, "null"]},
                    {"$ne": [{"$type": "$work_hours"}, "missing"]},
                ]},
                "$work_hours", 0,
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": [{"$type": "$work_hours"}, "null"]},
                    {"$ne": [{"$type": "$work_hours"}, "missing"]},
                ]},
                1, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [
                {"$gt": ["$effective_late_minutes", 0]}, "$effective_late_minutes", 0
            ]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]

    att_results = {r["_id"]: r for r in db.attendance_logs.aggregate(att_pipeline)}

    items = []
    for dept_name in sorted(dept_emps.keys()):
        info = dept_emps[dept_name]
        att = att_results.get(dept_name, {})
        hc = info["headcount"]
        if hc == 0:
            continue

        whc = att.get("work_hours_count", 0)
        avg_wh = round_half_up(att.get("work_hours_sum", 0) / whc, 2) if whc > 0 else None

        items.append({
            "department": dept_name,
            "headcount": hc,
            "present_days": att.get("present_days", 0),
            "avg_work_hours": avg_wh,
            "late_count": att.get("late_count", 0),
            "total_late_minutes": att.get("total_late_minutes", 0),
            "leave_count": att.get("leave_count", 0),
            "on_duty_count": att.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month_format(month)
    first_day, last_day = get_month_date_range(month)

    pipeline = [
        {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": 1},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = [{
        "rank": r["rank"],
        "emp_code": r["_id"],
        "name": r["name"],
        "department": r["department"],
        "total_late_minutes": r["total_late_minutes"],
        "late_count": r["late_count"],
    } for r in results]

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    validate_date_format(date_from)
    validate_date_format(date_to)

    if date_to < date_from:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    from_d = date_type.fromisoformat(date_from)
    to_d = date_type.fromisoformat(date_to)
    if (to_d - from_d).days > 92:
        raise HTTPException(422, detail="Range must not exceed 92 days")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, detail="Department not found")

    dept_employees = list(db.employees.find(
        {"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0},
    ))
    emp_codes = [e["emp_code"] for e in dept_employees]

    from_dt = datetime.fromisoformat(date_from)
    to_dt = datetime.fromisoformat(date_to) + timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": date_from, "$lte": date_to},
        }},
        {"$addFields": {
            "date_dt": {"$dateFromString": {"dateString": "$date"}},
            "effective_half_day": {"$ifNull": ["$half_day", False]},
            "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
        }},
        {"$group": {
            "_id": "$date_dt",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": ["$effective_half_day", 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
        }},
        {"$densify": {
            "field": "_id",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt],
            },
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
            ]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "all_dept_employees": dept_employees,
        }},
        {"$addFields": {
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": "$all_dept_employees",
                        "as": "e",
                        "cond": {"$lte": ["$$e.joined_on", "$date"]},
                    }
                }
            }
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None,
                ]
            }
        }},
        {"$project": {"all_dept_employees": 0}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, "current"]},
                }
            },
        }},
        {"$addFields": {
            "moving_avg_7d": {
                "$cond": [
                    {"$ne": ["$moving_avg_7d_raw", None]},
                    {"$round": ["$moving_avg_7d_raw", 4]},
                    None,
                ]
            }
        }},
        {"$project": {"_id": 0, "moving_avg_7d_raw": 0}},
        {"$sort": {"date": 1}},
    ]

    items = [{
        "date": r["date"],
        "is_working_day": r["is_working_day"],
        "headcount": r["headcount"],
        "present_count": r["present_count"],
        "late_count": r["late_count"],
        "attendance_rate": r["attendance_rate"],
        "moving_avg_7d": r["moving_avg_7d"],
    } for r in list(db.attendance_logs.aggregate(pipeline))]

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, alias="date_from"),
    date_to: Optional[str] = Query(None, alias="date_to"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    valid_endpoints = [
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {valid_endpoints}")

    if endpoint == "attendance_list":
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

        skip = (page - 1) * page_size
        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(explain)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        validate_month_format(month)
        first_day, last_day = get_month_date_range(month)

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$addFields": {
                "date_parsed": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "effective_half_day": {"$ifNull": ["$half_day", False]},
                "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "effective_overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            }},
            {"$addFields": {"is_weekday": {"$and": [
                {"$gte": [{"$dayOfWeek": "$date_parsed"}, 2]},
                {"$lte": [{"$dayOfWeek": "$date_parsed"}, 6]},
            ]}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [
                    {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                    {"$cond": ["$effective_half_day", 0.5, 1]}, 0,
                ]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$effective_late_minutes"},
                "total_overtime_minutes": {"$sum": "$effective_overtime_minutes"},
            }},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(explain)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        validate_month_format(month)
        first_day, last_day = get_month_date_range(month)

        att_match = {"date": {"$gte": first_day, "$lte": last_day}}
        pipeline = [
            {"$match": att_match},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$addFields": {
                "department": "$employee.department",
                "effective_half_day": {"$ifNull": ["$half_day", False]},
                "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }},
            {"$group": {
                "_id": "$department",
                "present_days": {"$sum": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$effective_half_day", 0.5, 1]}, 0,
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$cond": [
                    {"$gt": ["$effective_late_minutes", 0]}, "$effective_late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$sort": {"_id": 1}},
        ])
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(explain)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        validate_month_format(month)
        first_day, last_day = get_month_date_range(month)

        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$group": {
                "_id": "$emp_code", "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
        ])
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(explain)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from, and to are required")
        validate_date_format(from_date)
        validate_date_format(to_date)

        dept_employees = list(db.employees.find(
            {"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0},
        ))
        emp_codes = [e["emp_code"] for e in dept_employees]

        from_dt = datetime.fromisoformat(from_date)
        to_dt = datetime.fromisoformat(to_date) + timedelta(days=1)

        pipeline = [
            {"$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }},
            {"$addFields": {
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "effective_half_day": {"$ifNull": ["$half_day", False]},
                "effective_late_minutes": {"$ifNull": ["$late_minutes", 0]},
            }},
            {"$group": {
                "_id": "$date_dt",
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$effective_half_day", 0.5, 1]},
                    0,
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$effective_late_minutes", 0]}, 1, 0]}},
            }},
            {"$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt],
                },
            }},
            {"$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "is_working_day": {"$and": [
                    {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                ]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "all_dept_employees": dept_employees,
            }},
            {"$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$all_dept_employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                }
            }},
            {"$project": {"all_dept_employees": 0}},
            {"$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }},
            {"$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d_raw", None]},
                        {"$round": ["$moving_avg_7d_raw", 4]},
                        None,
                    ]
                }
            }},
            {"$project": {"_id": 0, "moving_avg_7d_raw": 0}},
            {"$sort": {"date": 1}},
        ]
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": sanitize_mongo_doc(explain)}
