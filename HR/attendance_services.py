"""Business rules for attendance and leave.

Views call into this module; it raises ``django.core.exceptions.ValidationError``
for rule violations so callers can show ``error.messages`` to the user.
"""
import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import router, transaction
from django.utils import timezone

from . import leave_utils
from .models import Attendance, Employee, LeaveBalance, LeaveRequest

# Statuses on which no working hours make sense, so any punch times are cleared.
NON_WORKING_STATUSES = (
    Attendance.STATUS_ABSENT,
    Attendance.STATUS_ON_LEAVE,
    Attendance.STATUS_HOLIDAY,
    Attendance.STATUS_WEEK_OFF,
)

STATUS_CODES = {
    Attendance.STATUS_PRESENT: 'P',
    Attendance.STATUS_ABSENT: 'A',
    Attendance.STATUS_HALF_DAY: 'HD',
    Attendance.STATUS_ON_LEAVE: 'L',
    Attendance.STATUS_HOLIDAY: 'H',
    Attendance.STATUS_WEEK_OFF: 'WO',
}


def _atomic(model):
    """transaction.atomic() bound to the DB the router picks for ``model``
    (the per-company database), not blindly to 'default'."""
    return transaction.atomic(using=router.db_for_write(model))


def _user_or_none(user):
    return user if getattr(user, 'is_authenticated', False) else None


# --- attendance --------------------------------------------------------------

def mark_attendance(employee, day, status, user=None, check_in=None, check_out=None, remarks=''):
    """Create or update the attendance row for (employee, day)."""
    if status not in dict(Attendance.STATUS_CHOICES):
        raise ValidationError(f"Unknown attendance status '{status}'.")
    if day > leave_utils.today_local():
        raise ValidationError('Attendance cannot be marked for a future date.')

    if status in NON_WORKING_STATUSES:
        check_in = check_out = None

    with _atomic(Attendance):
        record = Attendance.objects.filter(employee=employee, date=day).first()
        if record is None:
            record = Attendance(employee=employee, date=day, created_by=_user_or_none(user))
        record.status = status
        record.check_in = check_in
        record.check_out = check_out
        record.remarks = (remarks or '').strip()[:255] or None
        record.updated_by = _user_or_none(user)
        # A row manually moved away from leave no longer belongs to that request.
        if status not in (Attendance.STATUS_ON_LEAVE, Attendance.STATUS_HALF_DAY):
            record.leave_request = None
        record.full_clean(exclude=['employee'])
        record.save()
    return record


def check_in_now(employee, user=None):
    """Self-service check-in for today."""
    now = leave_utils.now_local()
    today = now.date()
    with _atomic(Attendance):
        record = Attendance.objects.filter(employee=employee, date=today).first()
        if record is None:
            record = Attendance(employee=employee, date=today, created_by=_user_or_none(user))
        if record.check_in:
            raise ValidationError(f"Already checked in today at {record.check_in:%H:%M}.")
        if (record.status == Attendance.STATUS_ON_LEAVE):
            raise ValidationError('You are on approved leave today.')
        record.status = Attendance.STATUS_PRESENT
        record.check_in = now.time().replace(microsecond=0)
        record.check_out = None
        record.leave_request = None
        record.updated_by = _user_or_none(user)
        record.save()
    return record


def check_out_now(employee, user=None):
    """Self-service check-out for today."""
    now = leave_utils.now_local()
    with _atomic(Attendance):
        record = Attendance.objects.filter(employee=employee, date=now.date()).first()
        if record is None or not record.check_in:
            raise ValidationError('You have not checked in today.')
        if record.check_out:
            raise ValidationError(f"Already checked out today at {record.check_out:%H:%M}.")
        out_time = now.time().replace(microsecond=0)
        if out_time <= record.check_in:
            raise ValidationError('Check-out must be after check-in.')
        record.check_out = out_time
        record.updated_by = _user_or_none(user)
        record.save()
    return record


def monthly_attendance(year, month, employees):
    """Build a month grid: one row per employee with a status code per day and totals."""
    last_day = calendar.monthrange(year, month)[1]
    first, last = date(year, month, 1), date(year, month, last_day)
    days = list(leave_utils.iter_dates(first, last))
    today = leave_utils.today_local()
    weekly_off = leave_utils.weekly_off_weekdays()

    by_employee = defaultdict(dict)
    for rec in Attendance.objects.filter(date__range=(first, last), employee__in=employees):
        by_employee[rec.employee_id][rec.date] = rec

    rows = []
    for emp in employees:
        cells = []
        totals = defaultdict(Decimal)
        records = by_employee.get(emp.pk, {})
        for d in days:
            rec = records.get(d)
            if rec:
                cells.append({'code': STATUS_CODES[rec.status], 'status': rec.status})
                totals[rec.status] += 1
            elif d.weekday() in weekly_off:
                cells.append({'code': 'WO', 'status': Attendance.STATUS_WEEK_OFF, 'implicit': True})
                totals[Attendance.STATUS_WEEK_OFF] += 1
            elif d <= today:
                cells.append({'code': '-', 'status': 'unmarked'})
                totals['unmarked'] += 1
            else:
                cells.append({'code': '', 'status': 'future'})
        rows.append({
            'employee': emp,
            'cells': cells,
            'present_days': totals[Attendance.STATUS_PRESENT] + totals[Attendance.STATUS_HALF_DAY] * Decimal('0.5'),
            'absent': totals[Attendance.STATUS_ABSENT],
            'on_leave': totals[Attendance.STATUS_ON_LEAVE],
            'holiday': totals[Attendance.STATUS_HOLIDAY],
            'week_off': totals[Attendance.STATUS_WEEK_OFF],
            'unmarked': totals['unmarked'],
        })
    return days, rows


# --- leave -------------------------------------------------------------------

def _apply_leave_to_attendance(leave, user):
    """Mark each working day of an approved leave as on-leave in attendance.

    A day on which the employee already has a genuine present / half-day record
    is left alone (they did attend). Returns the number of rows written.
    """
    status = Attendance.STATUS_HALF_DAY if leave.is_half_day else Attendance.STATUS_ON_LEAVE
    remarks = f"{'Half-day ' if leave.is_half_day else ''}{leave.leave_type}"[:255]
    written = 0
    for day in leave_utils.working_dates(leave.from_date, leave.to_date):
        record = Attendance.objects.filter(employee=leave.employee, date=day).first()
        if record and not record.leave_request_id and record.status in (
                Attendance.STATUS_PRESENT, Attendance.STATUS_HALF_DAY):
            continue
        if record is None:
            record = Attendance(employee=leave.employee, date=day, created_by=_user_or_none(user))
        record.status = status
        record.leave_request = leave
        record.remarks = remarks
        record.check_in = record.check_out = None
        record.updated_by = _user_or_none(user)
        record.save()
        written += 1
    return written


def approve_leave(leave, user=None, remarks=''):
    """Approve a pending request, re-checking the quota, and reflect it in attendance."""
    with _atomic(LeaveRequest):
        leave = LeaveRequest.objects.select_for_update().select_related(
            'employee', 'leave_type').get(pk=leave.pk)
        if leave.status != LeaveRequest.STATUS_PENDING:
            raise ValidationError(f"Only pending requests can be approved (this one is {leave.get_status_display().lower()}).")

        year = leave.from_date.year
        if leave_utils.is_quota_tracked(leave.leave_type):
            balance = leave_utils.ensure_balance(leave.employee, leave.leave_type, year)
            allocated = balance.allocated if balance else Decimal('0')
            remaining = allocated - leave_utils.used_days(leave.employee, leave.leave_type, year)
            if leave.days > remaining:
                raise ValidationError(
                    f"Cannot approve: {leave.days} day(s) requested but only {max(remaining, 0)} "
                    f"of {leave.leave_type} remain for {year}.")

        leave.status = LeaveRequest.STATUS_APPROVED
        leave.reviewed_by = _user_or_none(user)
        leave.reviewed_at = timezone.now()
        leave.review_remarks = (remarks or '').strip()[:255] or None
        leave.save()
        _apply_leave_to_attendance(leave, user)
    return leave


def reject_leave(leave, user=None, remarks=''):
    with _atomic(LeaveRequest):
        leave = LeaveRequest.objects.select_for_update().get(pk=leave.pk)
        if leave.status != LeaveRequest.STATUS_PENDING:
            raise ValidationError(f"Only pending requests can be rejected (this one is {leave.get_status_display().lower()}).")
        leave.status = LeaveRequest.STATUS_REJECTED
        leave.reviewed_by = _user_or_none(user)
        leave.reviewed_at = timezone.now()
        leave.review_remarks = (remarks or '').strip()[:255] or None
        leave.save()
    return leave


def cancel_leave(leave, user=None):
    """Cancel a pending or approved request; undo any attendance it generated."""
    with _atomic(LeaveRequest):
        leave = LeaveRequest.objects.select_for_update().get(pk=leave.pk)
        if leave.status not in (LeaveRequest.STATUS_PENDING, LeaveRequest.STATUS_APPROVED):
            raise ValidationError(f"A {leave.get_status_display().lower()} request cannot be cancelled.")
        if leave.status == LeaveRequest.STATUS_APPROVED:
            Attendance.objects.filter(
                leave_request=leave,
                status__in=(Attendance.STATUS_ON_LEAVE, Attendance.STATUS_HALF_DAY),
            ).delete()
        leave.status = LeaveRequest.STATUS_CANCELLED
        leave.save()
    return leave


def allocate_balances(year):
    """Create any missing balance rows for the year from each leave type's quota,
    for every active employee entitled to that type. Returns how many were created."""
    created = 0
    with _atomic(LeaveBalance):
        for employee in Employee.objects.filter(status=True):
            for leave_type in employee.leaves.filter(status=True):
                if not leave_utils.is_quota_tracked(leave_type):
                    continue
                if LeaveBalance.objects.filter(
                        employee=employee, leave_type=leave_type, year=year).exists():
                    continue
                LeaveBalance.objects.create(
                    employee=employee, leave_type=leave_type, year=year,
                    allocated=leave_type.days_allowed)
                created += 1
    return created
