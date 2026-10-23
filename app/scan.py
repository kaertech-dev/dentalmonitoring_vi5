from datetime import datetime
import re
from app.db import query_one, query_all, get_conn

REQUIRED_STATIONS = (
    "progtest",
    "soldering",
    "vi1",
    "assembly1",
    "vi4",
    "assembly2",
    "assembly3",
    "assembly4",
    "assembly5",
    "assembly6",
    "camcalibration",
    "lasermarking",
    "labelling",
)
MAX_FAIL_REPETITIONS = 3
CUSTOMER_SN_STATIONS = {
    "lasermarking": "dentalmonitoring.mainboard_lasermarking",
    "labelling": "dentalmonitoring.mainboard_labelling",
}
CUSTOMER_SN_PATTERN = re.compile(r"\(21\)([^()\x1d]*)")

def extract_customer_sn(scan_value: str) -> str | None:
    """Extract the customer serial from GS1 application identifier 21."""
    match = CUSTOMER_SN_PATTERN.search(scan_value)
    if match is None:
        return None
    customer_sn = match.group(1).strip()
    return customer_sn or None

def lookup_unit(serial_num: str):
    """Check b2btag_main for the serial and its station statuses."""
    return query_one(
        """
        SELECT serial_num, po_num, progtest, soldering, vi1, assembly1, vi4, assembly2, assembly3, assembly4, assembly5, assembly6, camcalibration, lasermarking, labelling
        FROM dentalmonitoring.mainboard_main
        WHERE serial_num = %s
        LIMIT 1
        """,
        (serial_num,)
    )

def lookup_customer_sn(customer_sn: str, station: str):
    """Look up a customer SN and its serial number in one scan station."""
    table = CUSTOMER_SN_STATIONS.get(station)
    if table is None:
        return None

    return query_one(
        f"""
        SELECT customer_sn, serial_num
        FROM {table}
        WHERE BINARY customer_sn = BINARY %s
        LIMIT 1
        """,
        (customer_sn,)
    )

def verify_customer_sn_matches_serial(customer_sn: str, serial_num: str) -> bool:
    """Confirm both scan stations record this exact customer SN and serial."""
    for station in CUSTOMER_SN_STATIONS:
        row = lookup_customer_sn(customer_sn, station)
        if (
            row is None
            or row["customer_sn"] != customer_sn
            or row["serial_num"].casefold() != serial_num.casefold()
        ):
            return False
    return True

def already_vi5(serial_num: str) -> dict | None:
    return query_one(
        """
        SELECT serial_num, po_num, operator_en, shift, date_time, status
        FROM dentalmonitoring.mainboard_vi5
        WHERE serial_num = %s AND status = 1
        LIMIT 1
        """,
        (serial_num,)
    )

def _fail_serials(serial_num: str):
    return tuple(f"{serial_num}_{attempt}" for attempt in range(1, MAX_FAIL_REPETITIONS + 1))

def _failed_attempt_rows(serial_num: str, cursor=None):
    sql = """
        SELECT serial_num
        FROM dentalmonitoring.mainboard_vi5
        WHERE serial_num IN (%s, %s, %s) AND status = 0
    """
    params = _fail_serials(serial_num)
    if cursor is None:
        return query_all(sql, params)

    cursor.execute(sql + " FOR UPDATE", params)
    return cursor.fetchall()

def _fail_repetition(rows) -> int:
    recorded_repetitions = [
        int(row["serial_num"].rsplit("_", 1)[1])
        for row in rows
    ]
    return max(len(rows), max(recorded_repetitions, default=0))

def fail_repetition(serial_num: str) -> int:
    """Return the number of failed repetitions already recorded for a serial."""
    return _fail_repetition(_failed_attempt_rows(serial_num))

def stations_passed(row: dict) -> bool:
    """Return True if all required stations are recorded as 1."""
    return all(row.get(s) == 1 for s in REQUIRED_STATIONS)

def _already_vi_message(record: dict) -> str:
    recorded_at = record["date_time"]
    if isinstance(recorded_at, datetime):
        recorded_at = recorded_at.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"Serial '{record['serial_num']}' already has a saved PASS row "
        f"(PO {record['po_num']}, operator {record['operator_en']}, "
        f"shift {record['shift']}, recorded at {recorded_at}). "
        "No duplicate was written."
    )

def record_vi5(
    serial_num: str,
    product_sn: str,
    po_num: str,
    operator_en: str,
    shift: str,
) -> dict | None:
    """
    Atomically:
      1. Lock the unit row and check for an existing PASS record.
      2. Set vi5=1 and insert a PASS row if none exists.
    Returns an existing PASS row if this is a repeated request.
    """
    conn = get_conn(read_timeout=15)
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT serial_num
            FROM dentalmonitoring.mainboard_main
            WHERE serial_num = %s
            FOR UPDATE
            """,
            (serial_num,)
        )
        cur.execute(
            """
            SELECT serial_num, po_num, operator_en, shift, date_time, test_rep, status
            FROM dentalmonitoring.mainboard_vi5
            WHERE serial_num = %s AND status = 1
            LIMIT 1
            FOR UPDATE
            """,
            (serial_num,)
        )
        existing_record = cur.fetchone()
        if existing_record:
            conn.commit()
            return existing_record

        cur.execute(
            """
            UPDATE dentalmonitoring.mainboard_main
            SET vi5 = 1
            WHERE serial_num = %s
            """,
            (serial_num,)
        )

        cur.execute(
            """
            INSERT INTO dentalmonitoring.mainboard_vi5
                (serial_num, product_sn, po_num, operator_en, shift, date_time, test_rep, remarks, status)
            VALUES (%s, %s, %s, %s, %s, %s, 1, "", 1)
            """,
            (serial_num, product_sn, po_num, operator_en, shift, datetime.now())
        )

        conn.commit()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def record_fail(
    serial_num: str,
    product_sn: str,
    po_num: str,
    operator_en: str,
    shift: str,
    reason: str,
):
    """
    Record a FAIL:
      - Inserts into dentalmonitoring.mainboard_vi5 with serial_num suffixed "_1", "_2", or "_3"
      - status = 0, remarks = reason
      - dentalmonitoring.mainboard_main.vi5 is NOT touched (the unit can be reworked and re-scanned).
    """
    conn = get_conn(read_timeout=15)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT serial_num
            FROM dentalmonitoring.mainboard_main
            WHERE serial_num = %s
            FOR UPDATE
            """,
            (serial_num,)
        )
        rows = _failed_attempt_rows(serial_num, cur)
        repetition = _fail_repetition(rows) + 1
        if repetition > MAX_FAIL_REPETITIONS:
            conn.rollback()
            return None

        fail_serial = f"{serial_num}_{repetition}"
        cur.execute(
            """
            INSERT INTO dentalmonitoring.mainboard_vi5
                (serial_num, product_sn, po_num, operator_en, shift, date_time, test_rep, remarks, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
            """,
            (
                fail_serial,
                product_sn,
                po_num,
                operator_en,
                shift,
                datetime.now(),
                repetition,
                reason,
            )
        )
        conn.commit()
        return repetition
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def process_customersn_check(customer_sn: str, station: str):
    """
    Extract AI (21) and validate that customer SN at a scan station.
    """
    scan_value = customer_sn.strip()
    station = station.strip().lower()
    if station not in CUSTOMER_SN_STATIONS:
        return {
            "status": "fail",
            "message": "Choose either the laser-marking or labelling station.",
            "ok": False,
        }

    customer_sn = extract_customer_sn(scan_value)
    if customer_sn is None:
        return {
            "status": "fail",
            "message": "Invalid Data Matrix: customer serial AI (21) was not found.",
            "ok": False,
        }

    record = lookup_customer_sn(customer_sn, station)
    if (
        record is None
        or record["customer_sn"] != customer_sn
        or not record["serial_num"]
    ):
        return {
            "status": "not_found",
            "message": f"Customer SN '{customer_sn}' not found at {station}.",
            "ok": False,
        }

    return {
        "status": "ok",
        "message": (
            f"Customer SN '{customer_sn}' verified at {station}; "
            f"station serial is '{record['serial_num']}'."
        ),
        "customer_sn": customer_sn,
        "product_sn": scan_value,
        "serial_num": record["serial_num"],
        "ok": True,
    }

def validate_scan(
    customer_sn: str,
    serial_num: str,
    laser_product_sn: str,
    labelling_product_sn: str,
):
    """
    Validate both customer-SN station records, serial, stations, and VI5 history.
    Does NOT record anything — just tells the UI whether it's OK to
    show the PASS / FAIL buttons.
    Returns:
      status : "ok" | "already_vi5" | "fail" | "not_found" | "mismatch"
      message: human-readable string
      unit   : the dentalmonitoring.mainboard_main row (or None)
    """
    serial_num = serial_num.strip()
    customer_sn = customer_sn.strip()
    laser_product_sn = laser_product_sn.strip()
    labelling_product_sn = labelling_product_sn.strip()

    if not customer_sn or not laser_product_sn or not labelling_product_sn:
        return {
            "status": "fail",
            "message": "Both Data Matrix scans must be verified first.",
            "unit": None,
        }

    if (
        laser_product_sn != labelling_product_sn
        or extract_customer_sn(laser_product_sn) != customer_sn
    ):
        return {
            "status": "mismatch",
            "message": "The complete laser-marking and labelling Data Matrix values must match.",
            "unit": None,
        }

    unit = lookup_unit(serial_num)
    if not unit:
        return {"status": "not_found", "message": f"Serial '{serial_num}' not found in database.", "unit": None}

    vi5_record = already_vi5(serial_num)
    if vi5_record:
        return {
            "status": "already_vi5",
            "message": _already_vi_message(vi5_record),
            "unit": unit,
            "saved_record": vi5_record,
        }

    if fail_repetition(serial_num) >= MAX_FAIL_REPETITIONS:
        return {
            "status": "max_failures",
            "message": f"Serial '{serial_num}' has reached the maximum of {MAX_FAIL_REPETITIONS} FAIL attempts.",
            "unit": unit,
        }

    if not verify_customer_sn_matches_serial(customer_sn, serial_num):
        return {
            "status": "mismatch",
            "message": (
                f"Customer SN '{customer_sn}' must match the serial '{serial_num}' "
                "in both laser-marking and labelling records."
            ),
            "unit": unit,
        }

    if not stations_passed(unit):
        failed = [s for s in REQUIRED_STATIONS if unit.get(s) != 1]
        return {
            "status": "fail",
            "message": f"Serial '{serial_num}' has not passed: {', '.join(failed).upper()}.",
            "unit": unit,
        }

    return {
        "status": "ok",
        "message": f"Serial '{serial_num}' verified. Choose PASS or FAIL.",
        "unit": unit,
    }

def finalize_scan(
    customer_sn: str,
    serial_num: str,
    laser_product_sn: str,
    labelling_product_sn: str,
    operator_en: str,
    shift: str,
    decision: str,
    remarks: str = "",
):
    """
    Step 4: record the operator's decision.
    Re-runs validate_scan first so a stale/tampered client can't force a write.
    decision: "pass" | "fail"
    """
    serial_num = serial_num.strip()
    customer_sn = customer_sn.strip()
    laser_product_sn = laser_product_sn.strip()
    labelling_product_sn = labelling_product_sn.strip()
    decision = (decision or "").strip().lower()

    if decision not in ("pass", "fail"):
        return {"status": "fail", "message": "Invalid decision.", "unit": None}

    check = validate_scan(
        customer_sn,
        serial_num,
        laser_product_sn,
        labelling_product_sn,
    )
    if check["status"] != "ok":
        if decision == "pass" and check["status"] == "already_vi5":
            return {
                "status": "ok",
                "message": check["message"],
                "unit": check["unit"],
                "decision": "pass",
                "serial_num": serial_num,
                "already_recorded": True,
            }
        return check

    unit = check["unit"]

    if decision == "fail":
        reason = remarks.strip()
        if not reason:
            return {"status": "fail", "message": "A fail reason is required.", "unit": unit}
        repetition = record_fail(
            serial_num,
            laser_product_sn,
            unit["po_num"],
            operator_en,
            shift,
            reason,
        )
        if repetition is None:
            return {
                "status": "max_failures",
                "message": f"Serial '{serial_num}' has reached the maximum of {MAX_FAIL_REPETITIONS} FAIL attempts.",
                "unit": unit,
            }
        return {
            "status": "ok",
            "message": f"Serial '{serial_num}' recorded as FAIL (attempt {repetition} of {MAX_FAIL_REPETITIONS}): {reason}",
            "unit": unit,
            "decision": "fail",
            "serial_num": f"{serial_num}_{repetition}",
        }

    existing_record = record_vi5(
        serial_num,
        laser_product_sn,
        unit["po_num"],
        operator_en,
        shift,
    )
    if existing_record:
        return {
            "status": "ok",
            "message": _already_vi_message(existing_record),
            "unit": unit,
            "decision": "pass",
            "serial_num": serial_num,
            "already_recorded": True,
        }

    return {
        "status": "ok",
        "message": f"Serial '{serial_num}' recorded as VI5 successfully.",
        "unit": unit,
        "decision": "pass",
        "serial_num": serial_num,
    }