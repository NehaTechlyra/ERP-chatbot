"""Helpers for attendance and leave calculations.

Kept free of module-level model imports so ``HR.models`` can use them from
``clean()`` without creating an import cycle.

Settings (all optional):
    HR_WEEKLY_OFF_WEEKDAYS   tuple of weekday numbers (Mon=0 .. Sun=6) that are
                             not counted as leave days. Default: (6,) = Sunday.
    HR_ATTENDANCE_TIMEZONE   IANA zone used for "today" and check-in/out times.
                             Default: 'Asia/Kolkata' (project TIME_ZONE is UTC).
"""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def weekly_off_weekdays():
    return tuple(getattr(settings, 'HR_WEEKLY_OFF_WEEKDAYS', (6,)))


def attendance_tz():
    return ZoneInfo(getattr(settings, 'HR_ATTENDANCE_TIMEZONE', 'Asia/Kolkata'))


def now_local():
    """Current datetime in the attendance timezone."""
    return timezone.now().astimezone(attendance_tz())


def today_local():
    return now_local().date()


def iter_dates(start, end):
    """Yield every date from start to end inclusive."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def working_dates(start, end):
    """Dates between start and end inclusive that are not weekly-off days."""
    off = weekly_off_weekdays()
    return [d for d in iter_dates(start, end) if d.weekday() not in off]


def count_leave_days(start, end, is_half_day=False):
    """Number of leave days charged for a request, as a Decimal.

    Weekly-off days inside the range are not charged. A half-day request
    (same start/end date) is charged 0.5 if that date is a working day.
    """
    if not start or not end or end < start:
        return Decimal('0')
    days = Decimal(len(working_dates(start, end)))
    if is_half_day and start == end and days:
        return Decimal('0.5')
    return days


# --- balances ---------------------------------------------------------------
# Convention: a leave type with days_allowed == 0 is not quota-tracked
# (unlimited / unpaid), so no LeaveBalance is created or enforced for it.

def _models():
    from HR.models import LeaveBalance, LeaveRequest
    return LeaveBalance, LeaveRequest


def is_quota_tracked(leave_type):
    return bool(leave_type and leave_type.days_allowed and leave_type.days_allowed > 0)


def ensure_balance(employee, leave_type, year):
    """Return the LeaveBalance row for (employee, leave_type, year), creating it
    from the leave type's annual quota when the employee is entitled to that
    leave type. Returns None if the type is not quota-tracked or the employee is
    not entitled to it and has no row yet.
    """
    LeaveBalance, _ = _models()
    if not is_quota_tracked(leave_type):
        return None
    existing = LeaveBalance.objects.filter(
        employee=employee, leave_type=leave_type, year=year).first()
    if existing:
        return existing
    if not employee.leaves.filter(pk=leave_type.pk).exists():
        return None
    return LeaveBalance.objects.create(
        employee=employee, leave_type=leave_type, year=year,
        allocated=leave_type.days_allowed)


def _sum_days(employee, leave_type, year, status, exclude_pk=None):
    _, LeaveRequest = _models()
    qs = LeaveRequest.objects.filter(
        employee=employee, leave_type=leave_type,
        from_date__year=year, status=status)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    total = Decimal('0')
    for days in qs.values_list('days', flat=True):
        total += days
    return total


def used_days(employee, leave_type, year):
    """Days consumed by approved requests starting in ``year``."""
    return _sum_days(employee, leave_type, year, 'approved')


def pending_days(employee, leave_type, year, exclude_pk=None):
    """Days reserved by requests still awaiting a decision."""
    return _sum_days(employee, leave_type, year, 'pending', exclude_pk)


def available_days(employee, leave_type, year, exclude_pk=None):
    """Days still free to request (allocated - approved - pending).

    Returns None when the leave type is not quota-tracked (no limit), and
    Decimal('0') when it is tracked but the employee has no entitlement.
    """
    if not is_quota_tracked(leave_type):
        return None
    balance = ensure_balance(employee, leave_type, year)
    if balance is None:
        return Decimal('0')
    return (balance.allocated
            - used_days(employee, leave_type, year)
            - pending_days(employee, leave_type, year, exclude_pk))
