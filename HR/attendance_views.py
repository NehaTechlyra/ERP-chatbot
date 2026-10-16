"""Attendance and leave views for the HR module.

Access model (reuses the existing "HR Employee" permission module):
  * View   - see everyone's attendance, leave requests and balances.
  * Create - apply for leave on behalf of any employee.
  * Edit   - mark attendance, approve/reject leave, change/allocate balances.
  * Any user whose login email matches an active Employee can also use the
    self-service parts for themselves (check in/out, apply, cancel, see own data).
"""
import calendar
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from department.models import Department
from leaves.models import Leaves
from Lyraerp.utils.redirect_utils import get_company_code, get_company_redirect_url

from . import attendance_services as services
from . import leave_utils
from .attendance_forms import LeaveRequestForm
from .models import Attendance, Employee, LeaveBalance, LeaveRequest
from .permissions import check_employee_access


# --- helpers -----------------------------------------------------------------

def _my_employee(request):
    """The active Employee whose email matches the logged-in user, if any."""
    if not hasattr(request, '_hr_my_employee'):
        email = (getattr(request.user, 'email', '') or '').strip()
        request._hr_my_employee = (
            Employee.objects.filter(email__iexact=email, status=True).first() if email else None)
    return request._hr_my_employee


def _can_view_all(user):
    return check_employee_access(user, 'View')


def _can_create(user):
    return check_employee_access(user, 'Create')


def _can_manage(user):
    return check_employee_access(user, 'Edit')


def _deny(request):
    """Send users without access back to the company home page."""
    company_code = get_company_code(request)
    return redirect(f'/{company_code}/' if company_code else '/')


def _parse_date(value):
    try:
        return datetime.strptime((value or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _parse_time(value):
    value = (value or '').strip()
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _redirect_to(request, view_name, params=None, **kwargs):
    url = get_company_redirect_url(request, view_name, **kwargs)
    if params:
        url += '?' + urlencode({k: v for k, v in params.items() if v not in (None, '')})
    return redirect(url)


def _safe_next(request, fallback_view, params=None):
    """Redirect to ?next= when it is a same-site URL, else to ``fallback_view``."""
    target = request.POST.get('next') or request.GET.get('next')
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        if params:
            sep = '&' if '?' in target else '?'
            target += sep + urlencode({k: v for k, v in params.items() if v})
        return redirect(target)
    return _redirect_to(request, fallback_view, params)


def _paginate(request, queryset, per_page):
    paginator = Paginator(queryset, per_page)
    page = request.GET.get('page', 1)
    try:
        return paginator.page(page)
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def _minute(value):
    return value.replace(second=0, microsecond=0) if value else None


def _is_unchanged(record, status, check_in, check_out, remarks):
    """True when the posted row matches what is already stored (to minute precision)."""
    if record is None:
        return False
    if status in services.NON_WORKING_STATUSES:
        check_in = check_out = None
    return (record.status, _minute(record.check_in), _minute(record.check_out), record.remarks or '') == (
        status, _minute(check_in), _minute(check_out), remarks)


def _scoped_employees(request, view_all, me):
    """Active employees the user may see, with department/search filters applied."""
    employees = Employee.objects.filter(status=True).select_related('department', 'designation')
    if not view_all:
        employees = employees.filter(pk=me.pk)
    department = request.GET.get('department')
    if department and department.isdigit():
        employees = employees.filter(department_id=int(department))
    query = (request.GET.get('q') or '').strip()
    if query:
        employees = employees.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query) | Q(emp_code__icontains=query))
    return employees.order_by('first_name', 'last_name')


# --- attendance --------------------------------------------------------------

def attendance_list(request):
    """Daily attendance sheet; users with Edit permission can save it in bulk."""
    view_all, me = _can_view_all(request.user), _my_employee(request)
    if not (view_all or me):
        return _deny(request)
    can_edit = _can_manage(request.user)
    today = leave_utils.today_local()

    errors, success = [], request.GET.get('success')
    flash_error = request.GET.get('error')
    if flash_error:
        errors.append(flash_error)

    if request.method == 'POST':
        if not can_edit:
            return HttpResponseForbidden('You do not have permission to mark attendance.')
        day = _parse_date(request.POST.get('date')) or today
        posted_ids = [int(i) for i in request.POST.getlist('employee_id') if i.isdigit()]
        employees = {e.pk: e for e in Employee.objects.filter(status=True, pk__in=posted_ids)}
        existing = {a.employee_id: a for a in Attendance.objects.filter(date=day, employee_id__in=posted_ids)}
        saved = 0
        for pk in posted_ids:
            status = (request.POST.get(f'status_{pk}') or '').strip()
            employee = employees.get(pk)
            if not status or employee is None:
                continue
            check_in = _parse_time(request.POST.get(f'in_{pk}'))
            check_out = _parse_time(request.POST.get(f'out_{pk}'))
            remarks = (request.POST.get(f'remarks_{pk}') or '').strip()
            if _is_unchanged(existing.get(pk), status, check_in, check_out, remarks):
                continue
            try:
                services.mark_attendance(employee, day, status, request.user, check_in, check_out, remarks)
                saved += 1
            except ValidationError as exc:
                errors.append(f"{employee.first_name} {employee.last_name}: {'; '.join(exc.messages)}")
        if not errors:
            return _redirect_to(request, 'hr_attendance_list', {
                'date': day.isoformat(),
                'success': f"Attendance saved for {saved} employee(s)." if saved else 'No changes to save.',
            })
        success = f"Saved {saved} row(s); {len(errors)} could not be saved." if saved else None
        selected_day = day
    else:
        selected_day = _parse_date(request.GET.get('date')) or today

    employees = list(_scoped_employees(request, view_all, me))
    records = {a.employee_id: a for a in Attendance.objects.filter(
        date=selected_day, employee_id__in=[e.pk for e in employees])}
    rows = [{'employee': e, 'record': records.get(e.pk)} for e in employees]

    counts = defaultdict(int)
    for rec in records.values():
        counts[rec.status] += 1

    my_record = None
    if me and selected_day == today:
        my_record = Attendance.objects.filter(employee=me, date=today).first()

    return render(request, 'attendance_list.html', {
        'rows': rows,
        'selected_day': selected_day,
        'is_future': selected_day > today,
        'today': today,
        'prev_day': selected_day - timedelta(days=1),
        'next_day': selected_day + timedelta(days=1),
        'status_choices': Attendance.STATUS_CHOICES,
        'counts': {
            'present': counts[Attendance.STATUS_PRESENT],
            'half_day': counts[Attendance.STATUS_HALF_DAY],
            'absent': counts[Attendance.STATUS_ABSENT],
            'on_leave': counts[Attendance.STATUS_ON_LEAVE],
            'unmarked': len(employees) - len(records),
            'total': len(employees),
        },
        'departments': Department.objects.filter(status=True).order_by('department_name') if view_all else [],
        'department_filter': request.GET.get('department', ''),
        'search': request.GET.get('q', ''),
        'can_view_all': view_all,
        'can_edit_attendance': can_edit,
        'me': me,
        'my_record': my_record,
        'errors': errors,
        'success_message': success,
    })


def attendance_punch(request, action):
    """Self-service check-in / check-out for today."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    me = _my_employee(request)
    if me is None:
        return _redirect_to(request, 'hr_attendance_list', {
            'error': 'Your login is not linked to an employee record (matching email).'})
    try:
        if action == 'check-in':
            record = services.check_in_now(me, request.user)
            message = f"Checked in at {record.check_in:%H:%M}."
        else:
            record = services.check_out_now(me, request.user)
            message = f"Checked out at {record.check_out:%H:%M}."
    except ValidationError as exc:
        return _redirect_to(request, 'hr_attendance_list', {'error': ' '.join(exc.messages)})
    return _redirect_to(request, 'hr_attendance_list', {'success': message})


def attendance_monthly(request):
    """Month grid of attendance codes with per-employee totals."""
    view_all, me = _can_view_all(request.user), _my_employee(request)
    if not (view_all or me):
        return _deny(request)
    today = leave_utils.today_local()
    year = _parse_int(request.GET.get('year'), today.year)
    month = _parse_int(request.GET.get('month'), today.month)
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        year, month = today.year, today.month

    employees = list(_scoped_employees(request, view_all, me))
    days, rows = services.monthly_attendance(year, month, employees)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return render(request, 'attendance_monthly.html', {
        'rows': rows,
        'days': days,
        'year': year,
        'month': month,
        'month_name': calendar.month_name[month],
        'prev': {'year': prev_year, 'month': prev_month},
        'next': {'year': next_year, 'month': next_month},
        'weekly_off': leave_utils.weekly_off_weekdays(),
        'departments': Department.objects.filter(status=True).order_by('department_name') if view_all else [],
        'department_filter': request.GET.get('department', ''),
        'search': request.GET.get('q', ''),
        'can_view_all': view_all,
    })


# --- leave requests ----------------------------------------------------------

def _decorate_leave_rows(page, request, me, can_manage):
    """Attach per-row action flags used by the template."""
    for leave in page.object_list:
        is_own = bool(me and leave.employee_id == me.pk)
        leave.can_review = bool(
            can_manage and leave.status == LeaveRequest.STATUS_PENDING
            and (not is_own or request.user.is_superuser))
        leave.can_cancel = bool(leave.is_open and (is_own or can_manage))


def _my_balances(me, year):
    """Balance summary per quota-tracked leave type for one employee."""
    summary = []
    for leave_type in Leaves.objects.filter(status=True).order_by('leaves_name'):
        if not leave_utils.is_quota_tracked(leave_type):
            continue
        balance = leave_utils.ensure_balance(me, leave_type, year)
        if balance is None:
            continue
        used = leave_utils.used_days(me, leave_type, year)
        pending = leave_utils.pending_days(me, leave_type, year)
        summary.append({
            'leave_type': leave_type,
            'allocated': balance.allocated,
            'used': used,
            'pending': pending,
            'available': balance.allocated - used - pending,
        })
    return summary


def leave_request_list(request):
    view_all, me = _can_view_all(request.user), _my_employee(request)
    if not (view_all or me):
        return _deny(request)
    can_manage = _can_manage(request.user)
    today = leave_utils.today_local()

    requests_qs = LeaveRequest.objects.select_related('employee', 'leave_type', 'reviewed_by')
    if not view_all:
        requests_qs = requests_qs.filter(employee=me)

    status = request.GET.get('status', '')
    if status in dict(LeaveRequest.STATUS_CHOICES):
        requests_qs = requests_qs.filter(status=status)
    else:
        status = ''
    employee_filter = request.GET.get('employee', '')
    if view_all and employee_filter.isdigit():
        requests_qs = requests_qs.filter(employee_id=int(employee_filter))
    year = _parse_int(request.GET.get('year'), 0)
    if year:
        requests_qs = requests_qs.filter(from_date__year=year)

    page = _paginate(request, requests_qs.order_by('-created_at'), 15)
    _decorate_leave_rows(page, request, me, can_manage)

    pending_total = None
    if view_all:
        pending_total = LeaveRequest.objects.filter(status=LeaveRequest.STATUS_PENDING).count()

    return render(request, 'leave_request_list.html', {
        'page': page,
        'status_choices': LeaveRequest.STATUS_CHOICES,
        'status_filter': status,
        'employee_filter': employee_filter,
        'year_filter': year or '',
        'employees': Employee.objects.filter(status=True).order_by('first_name', 'last_name') if view_all else [],
        'can_create_leave': _can_create(request.user) or bool(me),
        'can_view_all': view_all,
        'my_balances': _my_balances(me, today.year) if me else [],
        'balance_year': today.year,
        'pending_total': pending_total,
        'success_message': request.GET.get('success'),
        'error_message': request.GET.get('error'),
        'current_path': request.get_full_path(),
    })


def leave_request_create(request):
    can_create, me = _can_create(request.user), _my_employee(request)
    if not (can_create or me):
        return _deny(request)
    fixed_employee = None if can_create else me

    if request.method == 'POST':
        form = LeaveRequestForm(request.POST, fixed_employee=fixed_employee)
        if form.is_valid():
            leave = form.save(commit=False)
            leave.status = LeaveRequest.STATUS_PENDING
            leave.created_by = request.user if request.user.is_authenticated else None
            leave.save()
            return _redirect_to(request, 'hr_leave_request_list', {
                'success': 'Leave request submitted for approval.'})
    else:
        initial = {'employee': me.pk} if me else {}
        form = LeaveRequestForm(initial=initial, fixed_employee=fixed_employee)

    return render(request, 'leave_request_form.html', {
        'form': form,
        'my_balances': _my_balances(me, leave_utils.today_local().year) if me else [],
        'balance_year': leave_utils.today_local().year,
        'weekly_off_names': [calendar.day_name[d] for d in leave_utils.weekly_off_weekdays()],
        'weekly_off_numbers': json.dumps(list(leave_utils.weekly_off_weekdays())),
    })


def leave_request_action(request, pk, action):
    """Approve / reject (Edit permission) or cancel (owner or Edit permission)."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    leave = get_object_or_404(LeaveRequest.objects.select_related('employee', 'leave_type'), pk=pk)
    me = _my_employee(request)
    is_own = bool(me and leave.employee_id == me.pk)
    can_manage = _can_manage(request.user)
    remarks = (request.POST.get('remarks') or '').strip()

    try:
        if action == 'cancel':
            if not (is_own or can_manage):
                return HttpResponseForbidden('You cannot cancel this request.')
            services.cancel_leave(leave, request.user)
            message = 'Leave request cancelled.'
        else:
            if not can_manage:
                return HttpResponseForbidden('You do not have permission to review leave requests.')
            if is_own and not request.user.is_superuser:
                raise ValidationError('You cannot approve or reject your own leave request.')
            if action == 'approve':
                services.approve_leave(leave, request.user, remarks)
                message = 'Leave request approved.'
            else:
                services.reject_leave(leave, request.user, remarks)
                message = 'Leave request rejected.'
    except ValidationError as exc:
        return _safe_next(request, 'hr_leave_request_list', {'error': ' '.join(exc.messages)})
    return _safe_next(request, 'hr_leave_request_list', {'success': message})


# --- leave balances ----------------------------------------------------------

def leave_balance_list(request):
    view_all, me = _can_view_all(request.user), _my_employee(request)
    if not (view_all or me):
        return _deny(request)
    can_manage = _can_manage(request.user)
    today = leave_utils.today_local()
    year = _parse_int(request.POST.get('year') or request.GET.get('year'), today.year)

    if request.method == 'POST':
        if not can_manage:
            return HttpResponseForbidden('You do not have permission to change leave balances.')
        action = request.POST.get('action')
        if action == 'allocate':
            created = services.allocate_balances(year)
            message = (f"Created {created} balance row(s) for {year}." if created
                       else f"All balances for {year} already exist.")
            return _redirect_to(request, 'hr_leave_balance_list', {'year': year, 'success': message})
        if action == 'update':
            balance = get_object_or_404(LeaveBalance, pk=_parse_int(request.POST.get('balance_id'), 0))
            try:
                allocated = Decimal((request.POST.get('allocated') or '').strip())
            except InvalidOperation:
                allocated = None
            if allocated is None or allocated < 0:
                return _redirect_to(request, 'hr_leave_balance_list', {
                    'year': balance.year, 'error': 'Enter a valid number of days (0 or more).'})
            used = leave_utils.used_days(balance.employee, balance.leave_type, balance.year)
            if allocated < used:
                return _redirect_to(request, 'hr_leave_balance_list', {
                    'year': balance.year,
                    'error': f"Allocation cannot be below the {used} day(s) already used."})
            balance.allocated = allocated
            balance.save(update_fields=['allocated', 'updated_at'])
            return _redirect_to(request, 'hr_leave_balance_list', {
                'year': balance.year, 'success': 'Balance updated.'})
        return _redirect_to(request, 'hr_leave_balance_list', {'year': year})

    balances_qs = LeaveBalance.objects.filter(year=year).select_related('employee', 'leave_type')
    if not view_all:
        balances_qs = balances_qs.filter(employee=me)
    query = (request.GET.get('q') or '').strip()
    if query:
        balances_qs = balances_qs.filter(
            Q(employee__first_name__icontains=query) | Q(employee__last_name__icontains=query)
            | Q(employee__emp_code__icontains=query))
    page = _paginate(request, balances_qs.order_by('employee__first_name', 'employee__last_name',
                                                    'leave_type__leaves_name'), 20)

    # One query for all consumption on this page instead of one per row.
    totals = defaultdict(lambda: {'approved': Decimal('0'), 'pending': Decimal('0')})
    employee_ids = {b.employee_id for b in page.object_list}
    for emp_id, type_id, leave_status, days in LeaveRequest.objects.filter(
            employee_id__in=employee_ids, from_date__year=year,
            status__in=(LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_PENDING),
    ).values_list('employee_id', 'leave_type_id', 'status', 'days'):
        totals[(emp_id, type_id)][leave_status] += days
    for balance in page.object_list:
        t = totals[(balance.employee_id, balance.leave_type_id)]
        balance.used = t['approved']
        balance.pending = t['pending']
        balance.available = balance.allocated - t['approved'] - t['pending']

    return render(request, 'leave_balance_list.html', {
        'page': page,
        'year': year,
        'year_choices': range(today.year - 2, today.year + 2),
        'search': query,
        'can_manage': can_manage,
        'can_view_all': view_all,
        'success_message': request.GET.get('success'),
        'error_message': request.GET.get('error'),
    })
