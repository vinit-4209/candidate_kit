"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
from decimal import Decimal, ROUND_HALF_UP
import re
from datetime import date as dt_date
date = dt_date
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    # Employees indexes
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employee_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employees_dept_joined",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employees_dept_emp_code",
    )

    # Attendance logs indexes
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_desc_emp_asc",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="ix_attendance_emp_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1)],
        name="ix_attendance_date_late",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", 1)],
        name="ix_attendance_status_date",
    )



# --------------------------------------------------------------------------- #
# Constants & Helpers (Phase 1)
# --------------------------------------------------------------------------- #

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


def round_half_up_2(val: Optional[float | Decimal]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[float | Decimal]) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    return float(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Truncates instant to whole seconds per Rule R1 and returns UTC datetime."""
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise ValueError("Instant must be an integer epoch milliseconds")
    if ms < MIN_EPOCH_MS or ms > MAX_EPOCH_MS:
        raise ValueError(f"Instant must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS}")
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc).replace(microsecond=0)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts datetime (naive or aware) to whole-second epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp()) * 1000


def determine_attendance_date(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than shift_end
    belongs to the shift that started the previous calendar day.
    """
    if punch_utc.tzinfo is None:
        punch_utc = punch_utc.replace(tzinfo=timezone.utc)
    punch_ist = punch_utc.astimezone(IST)
    att_date = punch_ist.date()

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if end_minutes <= start_minutes:
        punch_sec = punch_ist.hour * 3600 + punch_ist.minute * 60 + punch_ist.second
        end_sec = eh * 3600 + em * 60
        if punch_sec < end_sec:
            att_date -= timedelta(days=1)

    return att_date.isoformat()


def compute_late_minutes(punch_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes (600s).
    Late only if strictly more than 10:00 after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start. Otherwise 0.
    """
    if punch_utc.tzinfo is None:
        punch_utc = punch_utc.replace(tzinfo=timezone.utc)
    punch_ist = punch_utc.astimezone(IST)

    sh, sm = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(attendance_date)
    shift_start_dt = datetime(att_d.year, att_d.month, att_d.day, sh, sm, 0, tzinfo=IST)

    diff_sec = (punch_ist - shift_start_dt).total_seconds()
    if diff_sec <= 600:
        return 0
    return int(diff_sec // 60)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30, else 0.
    For overnight shifts (shift_end <= shift_start), shift end falls on the next calendar day.
    """
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    punch_ist = punch_out_utc.astimezone(IST)

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    att_d = date.fromisoformat(attendance_date)
    if end_minutes <= start_minutes:
        end_d = att_d + timedelta(days=1)
    else:
        end_d = att_d

    shift_end_dt = datetime(end_d.year, end_d.month, end_d.day, eh, em, 0, tzinfo=IST)
    diff_sec = (punch_ist - shift_end_dt).total_seconds()
    if diff_sec < 0:
        return 0
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_history_entry(h: dict) -> dict:
    """Converts a history document into the schema expected by the API."""
    at_val = h.get("at")
    if isinstance(at_val, datetime):
        at_epoch = datetime_to_epoch_ms(at_val)
    elif isinstance(at_val, (int, float)):
        at_epoch = int(at_val)
    else:
        at_epoch = at_val

    entry = {
        "at": at_epoch,
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": {},
    }

    changes = h.get("changes", {})
    for k, v in changes.items():
        ch = {}
        if k in ("punch_in", "punch_out"):
            from_val = v.get("from")
            to_val = v.get("to")
            ch["from"] = datetime_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            ch["to"] = datetime_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        else:
            ch["from"] = v.get("from")
            ch["to"] = v.get("to")
        entry["changes"][k] = ch

    return entry


def format_attendance_doc(doc: dict) -> dict:
    """Formats an attendance document to strictly match openapi.yaml AttendanceRecord."""
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
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email format")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        if len(value) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
        description="Epoch milliseconds",
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
        description="Epoch milliseconds",
    )


class RegularizeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )

    doc.pop("_id", None)
    doc["created_at"] = datetime_to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = datetime_to_epoch_ms(item["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        if body.punched_at is None:
            punch_utc = datetime.now(timezone.utc).replace(microsecond=0)
        else:
            punch_utc = epoch_ms_to_datetime_utc(body.punched_at)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(status_code=422, detail="Invalid punched_at")

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    date_str = determine_attendance_date(punch_utc, shift_start, shift_end)
    late_minutes = compute_late_minutes(punch_utc, shift_start, shift_end, date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )

    return format_attendance_doc(doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    if date_from:
        try:
            dt_date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from")
    if date_to:
        try:
            dt_date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be greater than date_to",
        )

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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = build_attendance_list_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(d) for d in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        if body.punched_at is None:
            out_utc = datetime.now(timezone.utc).replace(microsecond=0)
        else:
            out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(status_code=422, detail="Invalid punched_at")

    # Find the most recent punch-in record
    recent = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
        },
        sort=[("punch_in", -1)],
    )
    if recent is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    punch_in_utc = recent["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (out_utc - punch_in_utc).total_seconds()

    if recent.get("punch_out") is None:
        # Open record
        if diff_sec <= 0 or diff_sec > 24 * 3600:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in and within 24 hours",
            )
    else:
        # Already closed record
        if 0 <= diff_sec <= 24 * 3600:
            raise HTTPException(status_code=409, detail="Already punched out")
        else:
            raise HTTPException(status_code=404, detail="No punch-in found")

    work_hours = compute_work_hours(punch_in_utc, out_utc)
    overtime = compute_overtime(out_utc, emp["shift_start"], emp["shift_end"], recent["date"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": recent["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Already punched out")

    return format_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequestIn,
):
    try:
        dt_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")
    if len(date) != 10:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
        new_late_minutes = 0
        new_work_hours = None
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status ("PRESENT", "WFH", "ON_DUTY")
        if body.punch_in is not None:
            try:
                target_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(status_code=422, detail="Invalid punch_in")
        else:
            target_punch_in = record.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        calc_date = determine_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in maps to attendance date {calc_date}, but record date is {date}",
            )

        if body.punch_out is not None:
            try:
                target_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(status_code=422, detail="Invalid punch_out")
        else:
            target_punch_out = record.get("punch_out")

        if target_punch_out is not None:
            t_in = target_punch_in if target_punch_in.tzinfo else target_punch_in.replace(tzinfo=timezone.utc)
            t_out = target_punch_out if target_punch_out.tzinfo else target_punch_out.replace(tzinfo=timezone.utc)
            diff_sec = (t_out - t_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            new_work_hours = compute_work_hours(t_in, t_out)
            new_overtime_minutes = compute_overtime(t_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], emp["shift_end"], date)

    # Compute changes diff
    changes = {}
    if record["status"] != new_status:
        changes["status"] = {"from": record["status"], "to": new_status}

    old_in_epoch = datetime_to_epoch_ms(record.get("punch_in"))
    new_in_epoch = datetime_to_epoch_ms(target_punch_in)
    if old_in_epoch != new_in_epoch:
        changes["punch_in"] = {"from": old_in_epoch, "to": new_in_epoch}

    old_out_epoch = datetime_to_epoch_ms(record.get("punch_out"))
    new_out_epoch = datetime_to_epoch_ms(target_punch_out)
    if old_out_epoch != new_out_epoch:
        changes["punch_out"] = {"from": old_out_epoch, "to": new_out_epoch}

    old_wh = record.get("work_hours")
    if old_wh != new_work_hours:
        changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_late = record.get("late_minutes", 0)
    if old_late != new_late_minutes:
        changes["late_minutes"] = {"from": old_late, "to": new_late_minutes}

    old_ot = record.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = record.get("half_day", False)
    if old_hd != new_half_day:
        changes["half_day"] = {"from": old_hd, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="No fields were changed")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    hist_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(status_code=409, detail="Concurrent update detected")

    return format_attendance_doc(updated)



# --------------------------------------------------------------------------- #
# Pipeline Builders (Shared by Analytics and Explain Endpoints)
# --------------------------------------------------------------------------- #

def get_month_date_range(month_str: str) -> tuple[dt_date, dt_date]:
    """Returns (month_start, month_end) dates for YYYY-MM."""
    year, month_num = map(int, month_str.split("-"))
    month_start = dt_date(year, month_num, 1)
    if month_num == 12:
        month_end = dt_date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = dt_date(year, month_num + 1, 1) - timedelta(days=1)
    return month_start, month_end


def build_employee_monthly_pipeline(emp_code: str, month_start_str: str, month_end_str: str) -> list[dict]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start_str, "$lte": month_end_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_summary_pipeline(month_start_str: str, month_end_str: str, department: Optional[str] = None) -> list[dict]:
    match_filter = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        # match_filter["department"] = department
        if "department" not in match_filter:
            match_filter["department"] = {}
        match_filter["department"]["$eq"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$l.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "emp_late_minutes": {
                    "$sum": "$logs.late_minutes",
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month_start_str: str, month_end_str: str, limit: int, department: Optional[str] = None) -> list[dict]:
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {"rank": {"$lte": limit}},
        },
        {
            "$sort": {"total_late_minutes": -1, "emp_code": 1},
        },
    ])
    return pipeline


def build_department_trend_pipeline(emp_codes: list[str], joined_dates: list[str], from_str: str, to_str: str, from_dt: datetime, to_dt_plus_1: datetime) -> list[dict]:
    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_str, "$lte": to_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
                },
            }
        },
        {
            "$project": {
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$addFields": {
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": joined_dates,
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Phase 4)
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end = get_month_date_range(month)
    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    emp_joined = dt_date.fromisoformat(emp["joined_on"])
    if emp_joined > month_end:
        working_days = 0
    else:
        start_date = max(month_start, emp_joined)
        working_days = sum(
            1 for d in range((month_end - start_date).days + 1)
            if (start_date + timedelta(days=d)).weekday() < 5
        )

    pipeline = build_employee_monthly_pipeline(emp_code, month_start_str, month_end_str)
    res = list(db.attendance_logs.aggregate(pipeline))

    data = res[0] if res else {
        "present_days": 0.0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    if working_days > 0:
        attendance_pct = round_half_up_2(
            (Decimal(str(data["present_days"])) / Decimal(str(working_days))) * Decimal("100")
        )
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": data["present_days"],
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = get_month_date_range(month)
    pipeline = build_department_summary_pipeline(month_start.isoformat(), month_end.isoformat(), department)
    docs = list(db.employees.aggregate(pipeline))

    departments_out = []
    for d in docs:
        if d["total_work_hours_count"] > 0:
            avg_wh = round_half_up_2(
                Decimal(str(d["total_work_hours"])) / Decimal(str(d["total_work_hours_count"]))
            )
        else:
            avg_wh = None

        departments_out.append({
            "department": d["_id"],
            "headcount": d["headcount"],
            "present_days": d["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": d["late_count"],
            "total_late_minutes": d["total_late_minutes"],
            "leave_count": d["leave_count"],
            "on_duty_count": d["on_duty_count"],
        })

    return {
        "month": month,
        "items": departments_out,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = get_month_date_range(month)
    pipeline = build_late_leaderboard_pipeline(month_start.isoformat(), month_end.isoformat(), limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_d = dt_date.fromisoformat(from_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid from date")
    try:
        to_d = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid to date")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")

    total_days = (to_d - from_d).days + 1
    if total_days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in dept_emps]
    joined_dates = [e["joined_on"] for e in dept_emps]

    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = build_department_trend_pipeline(emp_codes, joined_dates, from_date, to_date, from_dt, to_dt_plus_1)
    results = list(db.attendance_logs.aggregate(pipeline))

    days_out = []
    for row in results:
        days_out.append({
            "date": row["date"],
            "is_working_day": row["is_working_day"],
            "headcount": row["headcount"],
            "present_count": row["present_count"],
            "late_count": row["late_count"],
            "attendance_rate": round_half_up_4(row["attendance_rate"]),
            "moving_avg_7d": round_half_up_4(row["moving_avg_7d"]),
        })

    return {
        "department": department,
        "items": days_out,
    }


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Phase 5)
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        q = build_attendance_list_query(emp_code, date_from, date_to, status)
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_raw = db.command("explain", find_cmd, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        collection_name = "attendance_logs"
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        month_start, month_end = get_month_date_range(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start.isoformat(), month_end.isoformat())
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_raw = db.command("explain", agg_cmd, verbosity="executionStats")

    elif endpoint == "department_summary":
        collection_name = "employees"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        month_start, month_end = get_month_date_range(month)
        pipeline = build_department_summary_pipeline(month_start.isoformat(), month_end.isoformat(), department)
        agg_cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_raw = db.command("explain", agg_cmd, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        collection_name = "attendance_logs"
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        month_start, month_end = get_month_date_range(month)
        pipeline = build_late_leaderboard_pipeline(month_start.isoformat(), month_end.isoformat(), limit, department)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_raw = db.command("explain", agg_cmd, verbosity="executionStats")

    elif endpoint == "department_trend":
        collection_name = "attendance_logs"
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date:
            raise HTTPException(status_code=422, detail="'from' is required for department_trend")
        if not to_date:
            raise HTTPException(status_code=422, detail="'to' is required for department_trend")

        try:
            from_d = dt_date.fromisoformat(from_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid from date")
        try:
            to_d = dt_date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid to date")

        if to_d < from_d:
            raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date")
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]
        joined_dates = [e["joined_on"] for e in dept_emps]

        from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
        to_dt_plus_1 = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        pipeline = build_department_trend_pipeline(emp_codes, joined_dates, from_date, to_date, from_dt, to_dt_plus_1)
        agg_cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {},
        }
        explain_raw = db.command("explain", agg_cmd, verbosity="executionStats")

    else:
        raise HTTPException(status_code=422, detail="Invalid endpoint name")

    sanitized_explain = json.loads(json_util.dumps(explain_raw))
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": sanitized_explain,
    }


