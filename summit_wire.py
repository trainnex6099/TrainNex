"""The portal's public display contract; full records are backed up separately."""
from datetime import datetime, timezone
import time


def iso(value):
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat() if value is not None else None


def shift_payload(record, now=None):
    now = int(time.time()) if now is None else now
    breaks = int(record.get('break_seconds') or 0)
    if record['status'] == 'break' and record.get('break_started_at') is not None:
        breaks += max(0, now - int(record['break_started_at']))
    worked = max(0, int(record.get('ended_at') or now) - int(record['started_at']) - breaks)
    return {'id': record['shift_id'], 'employeeId': str(record['user_id']),
            'employeeName': record['username'], 'shiftType': record['shift_type_name'],
            'startTime': iso(record['started_at']), 'endTime': iso(record.get('ended_at')),
            'breakSeconds': breaks, 'workedSeconds': worked, 'status': record['status']}


def loa_payload(record):
    return {'id': record['loa_id'], 'employeeId': str(record['user_id']),
            'employeeName': record['username'], 'startTime': iso(record['start_at']),
            'expectedEndTime': iso(record['end_at']), 'actualEndTime': iso(record.get('ended_at')),
            'reason': record['reason'], 'status': record['status'],
            'reviewedBy': record.get('reviewed_by') or None}
