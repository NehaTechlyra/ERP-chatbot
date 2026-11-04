"""Tests for HR attendance and leave management.

Self-contained: view tests swap in a minimal middleware/template-context stack and
disable the per-company DB router so they run against a single test database.
"""
import copy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from HR import attendance_services as services
from HR import leave_utils
from HR.models import Attendance, Employee, LeaveBalance, LeaveRequest, Shift
from leaves.models import Leaves

MON = date(2026, 10, 12)   # Monday
SUN = date(2026, 10, 18)   # Sunday


class CompanyCodeFromURLMiddleware:
    """Test stand-in for the project's company-code middlewares."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        request.company_code = view_kwargs.pop('company_code', None)


def _make_employee(first, email=None, **extra):
    return Employee.objects.create(
        first_name=first, last_name='Tester', email=email or f'{first.lower()}@example.com',
        phone='9000000000', **extra)


def _make_leave_type(name='Casual Leave', days=12):
    return Leaves.objects.create(leaves_name=name, days_allowed=days)


class LeaveUtilsTests(TestCase):
    def test_counts_exclude_sunday_by_default(self):
        self.assertEqual(leave_utils.count_leave_days(MON, SUN), Decimal('6'))

    def test_half_day_counts_half_on_working_day_only(self):
        self.assertEqual(leave_utils.count_leave_days(MON, MON, is_half_day=True), Decimal('0.5'))
        self.assertEqual(leave_utils.count_leave_days(SUN, SUN, is_half_day=True), Decimal('0'))

    @override_settings(HR_WEEKLY_OFF_WEEKDAYS=(5, 6))
    def test_weekly_off_is_configurable(self):
        self.assertEqual(leave_utils.count_leave_days(MON, SUN), Decimal('5'))

    def test_reversed_range_is_zero(self):
        self.assertEqual(leave_utils.count_leave_days(SUN, MON), Decimal('0'))

    def test_ensure_balance_requires_entitlement_and_quota(self):
        emp = _make_employee('Asha')
        quota = _make_leave_type('Casual', 12)
        unlimited = _make_leave_type('Unpaid', 0)
        # Not entitled yet -> no balance
        self.assertIsNone(leave_utils.ensure_balance(emp, quota, 2026))
        emp.leaves.add(quota, unlimited)
        balance = leave_utils.ensure_balance(emp, quota, 2026)
        self.assertEqual(balance.allocated, Decimal('12'))
        # idempotent
        self.assertEqual(leave_utils.ensure_balance(emp, quota, 2026).pk, balance.pk)
        # quota 0 -> not tracked
        self.assertIsNone(leave_utils.ensure_balance(emp, unlimited, 2026))
        self.assertIsNone(leave_utils.available_days(emp, unlimited, 2026))


class LeaveRequestValidationTests(TestCase):
    def setUp(self):
        self.emp = _make_employee('Asha')
        self.casual = _make_leave_type('Casual', 5)
        self.emp.leaves.add(self.casual)

    def _request(self, start, end, half=False, **kw):
        return LeaveRequest(employee=self.emp, leave_type=self.casual,
                            from_date=start, to_date=end, is_half_day=half, **kw)

    def test_end_before_start_rejected(self):
        with self.assertRaises(ValidationError):
            self._request(MON + timedelta(days=2), MON).full_clean()

    def test_cannot_span_two_years(self):
        with self.assertRaises(ValidationError):
            self._request(date(2026, 12, 30), date(2027, 1, 2)).full_clean()

    def test_half_day_must_be_single_date(self):
        with self.assertRaises(ValidationError):
            self._request(MON, MON + timedelta(days=1), half=True).full_clean()

    def test_sunday_only_has_no_working_days(self):
        with self.assertRaises(ValidationError) as ctx:
            self._request(SUN, SUN).full_clean()
        self.assertIn('no working days', str(ctx.exception))

    def test_days_calculated_on_save(self):
        leave = self._request(MON, MON + timedelta(days=2))
        leave.full_clean()
        leave.save()
        self.assertEqual(leave.days, Decimal('3'))

    def test_quota_enforced_including_pending(self):
        first = self._request(MON, MON + timedelta(days=3))  # 4 of 5 days
        first.full_clean()
        first.save()
        second = self._request(MON + timedelta(days=14), MON + timedelta(days=15))  # 2 more -> 6 > 5
        with self.assertRaises(ValidationError) as ctx:
            second.full_clean()
        self.assertIn('Insufficient', str(ctx.exception))

    def test_overlap_rejected_but_cancelled_ignored(self):
        existing = self._request(MON, MON + timedelta(days=1))
        existing.full_clean()
        existing.save()
        with self.assertRaises(ValidationError) as ctx:
            self._request(MON + timedelta(days=1), MON + timedelta(days=2)).full_clean()
        self.assertIn('overlapping', str(ctx.exception))
        existing.status = LeaveRequest.STATUS_CANCELLED
        existing.save()
        self._request(MON + timedelta(days=1), MON + timedelta(days=2)).full_clean()

    def test_unlimited_leave_type_skips_quota(self):
        unpaid = _make_leave_type('Unpaid', 0)
        req = LeaveRequest(employee=self.emp, leave_type=unpaid, from_date=MON,
                           to_date=MON + timedelta(days=20))
        req.full_clean()  # no quota check, no error


class LeaveWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('hr', 'hr@example.com', 'pw')
        self.emp = _make_employee('Asha')
        self.casual = _make_leave_type('Casual', 5)
        self.emp.leaves.add(self.casual)

    def _apply(self, start, end, half=False):
        leave = LeaveRequest(employee=self.emp, leave_type=self.casual, from_date=start,
                             to_date=end, is_half_day=half)
        leave.full_clean()
        leave.save()
        return leave

    def test_approve_creates_attendance_and_updates_balance(self):
        leave = self._apply(MON, MON + timedelta(days=3))  # Mon-Thu, 4 days
        services.approve_leave(leave, self.user, 'ok')
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.STATUS_APPROVED)
        self.assertEqual(leave.reviewed_by, self.user)
        rows = Attendance.objects.filter(employee=self.emp, leave_request=leave)
        self.assertEqual(rows.count(), 4)
        self.assertTrue(all(r.status == Attendance.STATUS_ON_LEAVE for r in rows))
        self.assertEqual(leave_utils.used_days(self.emp, self.casual, 2026), Decimal('4'))
        self.assertEqual(leave_utils.available_days(self.emp, self.casual, 2026), Decimal('1'))

    def test_approval_keeps_real_present_records(self):
        Attendance.objects.create(employee=self.emp, date=MON, status=Attendance.STATUS_PRESENT,
                                  check_in=time(9, 0), check_out=time(17, 0))
        leave = self._apply(MON, MON + timedelta(days=1))
        services.approve_leave(leave, self.user)
        self.assertEqual(Attendance.objects.get(employee=self.emp, date=MON).status,
                         Attendance.STATUS_PRESENT)
        self.assertEqual(Attendance.objects.get(employee=self.emp, date=MON + timedelta(days=1)).status,
                         Attendance.STATUS_ON_LEAVE)

    def test_half_day_leave_marks_half_day(self):
        leave = self._apply(MON, MON, half=True)
        services.approve_leave(leave, self.user)
        self.assertEqual(Attendance.objects.get(employee=self.emp, date=MON).status,
                         Attendance.STATUS_HALF_DAY)

    def test_cannot_approve_twice_or_reject_after_approval(self):
        leave = self._apply(MON, MON)
        services.approve_leave(leave, self.user)
        with self.assertRaises(ValidationError):
            services.approve_leave(leave, self.user)
        with self.assertRaises(ValidationError):
            services.reject_leave(leave, self.user)

    def test_approval_rechecks_quota(self):
        leave = self._apply(MON, MON + timedelta(days=3))  # 4 days
        LeaveBalance.objects.filter(employee=self.emp, leave_type=self.casual, year=2026).update(
            allocated=Decimal('2'))
        with self.assertRaises(ValidationError) as ctx:
            services.approve_leave(leave, self.user)
        self.assertIn('Cannot approve', str(ctx.exception))
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.STATUS_PENDING)
        self.assertFalse(Attendance.objects.filter(leave_request=leave).exists())

    def test_reject_records_reviewer_and_frees_quota(self):
        leave = self._apply(MON, MON + timedelta(days=3))
        services.reject_leave(leave, self.user, 'busy period')
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.STATUS_REJECTED)
        self.assertEqual(leave.review_remarks, 'busy period')
        self.assertEqual(leave_utils.available_days(self.emp, self.casual, 2026), Decimal('5'))

    def test_cancel_approved_removes_generated_attendance_only(self):
        leave = self._apply(MON, MON + timedelta(days=1))
        services.approve_leave(leave, self.user)
        services.cancel_leave(leave, self.user)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveRequest.STATUS_CANCELLED)
        self.assertFalse(Attendance.objects.filter(employee=self.emp).exists())
        self.assertEqual(leave_utils.used_days(self.emp, self.casual, 2026), Decimal('0'))
        with self.assertRaises(ValidationError):
            services.cancel_leave(leave, self.user)

    def test_allocate_balances_is_idempotent(self):
        other = _make_employee('Ben')
        other.leaves.add(self.casual)
        _make_employee('Cara')  # not entitled to anything
        self.assertEqual(services.allocate_balances(2027), 2)
        self.assertEqual(services.allocate_balances(2027), 0)
        self.assertEqual(LeaveBalance.objects.filter(year=2027).count(), 2)


class AttendanceServiceTests(TestCase):
    def setUp(self):
        self.emp = _make_employee('Asha')
        self.user = get_user_model().objects.create_user('hr', 'hr@example.com', 'pw')

    def test_mark_upserts_single_row(self):
        services.mark_attendance(self.emp, MON, Attendance.STATUS_PRESENT, self.user,
                                 time(9, 0), time(17, 30))
        services.mark_attendance(self.emp, MON, Attendance.STATUS_HALF_DAY, self.user,
                                 time(9, 0), time(13, 0), 'left early')
        rec = Attendance.objects.get(employee=self.emp, date=MON)
        self.assertEqual(Attendance.objects.filter(employee=self.emp, date=MON).count(), 1)
        self.assertEqual(rec.status, Attendance.STATUS_HALF_DAY)
        self.assertEqual(rec.worked_hours, Decimal('4.00'))
        self.assertEqual(rec.worked_duration, '4h 00m')
        self.assertEqual(rec.updated_by, self.user)

    def test_worked_duration_shows_hours_and_minutes(self):
        cases = [((9, 0), (17, 30), '8h 30m'), ((9, 0), (17, 45), '8h 45m'),
                 ((9, 15), (9, 20), '0h 05m'), ((9, 0), (9, 0), None)]
        for (ih, im), (oh, om), expected in cases:
            rec = Attendance(employee=self.emp, date=MON, check_in=time(ih, im), check_out=time(oh, om))
            with self.subTest(out=(oh, om)):
                self.assertEqual(rec.worked_duration, expected)
        self.assertIsNone(Attendance(employee=self.emp, date=MON).worked_duration)

    def test_non_working_status_clears_times(self):
        services.mark_attendance(self.emp, MON, Attendance.STATUS_ABSENT, self.user,
                                 time(9, 0), time(10, 0))
        rec = Attendance.objects.get(employee=self.emp, date=MON)
        self.assertIsNone(rec.check_in)
        self.assertIsNone(rec.check_out)

    def test_future_date_and_bad_times_rejected(self):
        with self.assertRaises(ValidationError):
            services.mark_attendance(self.emp, leave_utils.today_local() + timedelta(days=1),
                                     Attendance.STATUS_PRESENT, self.user)
        with self.assertRaises(ValidationError):
            services.mark_attendance(self.emp, MON, Attendance.STATUS_PRESENT, self.user,
                                     time(17, 0), time(9, 0))
        with self.assertRaises(ValidationError):
            services.mark_attendance(self.emp, MON, 'bogus', self.user)

    def _freeze(self, hour, minute):
        tz = ZoneInfo('Asia/Kolkata')
        fixed = datetime(2026, 10, 16, hour, minute, 30, tzinfo=tz)
        return mock.patch.object(leave_utils, 'now_local', return_value=fixed)

    def test_check_in_and_out_flow(self):
        with self._freeze(9, 5):
            rec = services.check_in_now(self.emp, self.user)
            self.assertEqual(rec.check_in, time(9, 5, 30))
            self.assertEqual(rec.date, date(2026, 10, 16))
            with self.assertRaises(ValidationError):
                services.check_in_now(self.emp, self.user)  # already in
        with self._freeze(18, 0):
            rec = services.check_out_now(self.emp, self.user)
            self.assertEqual(rec.check_out, time(18, 0, 30))
            with self.assertRaises(ValidationError):
                services.check_out_now(self.emp, self.user)  # already out

    def test_check_out_without_check_in(self):
        with self._freeze(18, 0):
            with self.assertRaises(ValidationError):
                services.check_out_now(self.emp, self.user)

    def test_check_in_blocked_on_approved_leave(self):
        Attendance.objects.create(employee=self.emp, date=date(2026, 10, 16),
                                  status=Attendance.STATUS_ON_LEAVE)
        with self._freeze(9, 0):
            with self.assertRaises(ValidationError):
                services.check_in_now(self.emp, self.user)

    def test_monthly_grid_counts(self):
        services.mark_attendance(self.emp, date(2026, 10, 1), Attendance.STATUS_PRESENT, self.user)
        services.mark_attendance(self.emp, date(2026, 10, 2), Attendance.STATUS_HALF_DAY, self.user)
        services.mark_attendance(self.emp, date(2026, 10, 3), Attendance.STATUS_ABSENT, self.user)
        with mock.patch.object(leave_utils, 'today_local', return_value=date(2026, 10, 7)):
            days, rows = services.monthly_attendance(2026, 10, [self.emp])
        self.assertEqual(len(days), 31)
        row = rows[0]
        self.assertEqual(row['present_days'], Decimal('1.5'))
        self.assertEqual(row['absent'], 1)
        # Oct 4 (Sun) implicit week off; Oct 5, 6, 7 unmarked (<= "today"); later days are future.
        self.assertEqual(row['unmarked'], 3)
        self.assertEqual(row['cells'][3]['code'], 'WO')
        self.assertTrue(row['cells'][3].get('implicit'))
        self.assertEqual(row['cells'][20]['status'], 'future')


# --- views -------------------------------------------------------------------

def _test_templates():
    templates = copy.deepcopy(settings.TEMPLATES)
    templates[0]['OPTIONS']['context_processors'] = [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'HR.context_processors.hr_permissions',
    ]
    return templates


@override_settings(
    DATABASE_ROUTERS=[],
    ALLOWED_HOSTS=['testserver'],
    MIDDLEWARE=[
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
        'HR.tests.test_attendance_leave.CompanyCodeFromURLMiddleware',
    ],
    TEMPLATES=_test_templates(),
)
class ViewTests(TestCase):
    CO = 'TESTCO'

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.emp = _make_employee('Asha', email='asha@example.com')
        self.other = _make_employee('Ben', email='ben@example.com')
        self.casual = _make_leave_type('Casual', 5)
        self.emp.leaves.add(self.casual)
        self.other.leaves.add(self.casual)

    def url(self, name, *args):
        return reverse(name, kwargs={'company_code': self.CO, **({'pk': args[0]} if args else {})})

    def login_admin(self):
        self.client.force_login(self.admin)

    # daily sheet
    def test_daily_sheet_renders_and_bulk_saves(self):
        self.login_admin()
        resp = self.client.get(self.url('hr_attendance_list'), {'date': '2026-10-12'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Asha')
        self.assertContains(resp, 'Save attendance')

        data = {
            'date': '2026-10-12',
            'employee_id': [self.emp.pk, self.other.pk],
            f'status_{self.emp.pk}': 'present', f'in_{self.emp.pk}': '09:00', f'out_{self.emp.pk}': '17:30',
            f'remarks_{self.emp.pk}': '', f'status_{self.other.pk}': '',
            f'in_{self.other.pk}': '', f'out_{self.other.pk}': '', f'remarks_{self.other.pk}': '',
        }
        resp = self.client.post(self.url('hr_attendance_list'), data)
        self.assertEqual(resp.status_code, 302)
        rec = Attendance.objects.get(employee=self.emp, date=date(2026, 10, 12))
        self.assertEqual((rec.status, rec.check_in, rec.check_out),
                         ('present', time(9, 0), time(17, 30)))
        self.assertFalse(Attendance.objects.filter(employee=self.other).exists())
        # Re-posting identical data changes nothing
        before = Attendance.objects.get(pk=rec.pk).updated_at
        self.client.post(self.url('hr_attendance_list'), data)
        self.assertEqual(Attendance.objects.get(pk=rec.pk).updated_at, before)

    def test_bad_row_reports_error_without_losing_good_rows(self):
        self.login_admin()
        data = {
            'date': '2026-10-12', 'employee_id': [self.emp.pk, self.other.pk],
            f'status_{self.emp.pk}': 'present', f'in_{self.emp.pk}': '09:00', f'out_{self.emp.pk}': '17:00',
            f'status_{self.other.pk}': 'present', f'in_{self.other.pk}': '17:00', f'out_{self.other.pk}': '09:00',
        }
        resp = self.client.post(self.url('hr_attendance_list'), data)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ben Tester')
        self.assertContains(resp, 'Check-out must be after check-in')
        self.assertTrue(Attendance.objects.filter(employee=self.emp).exists())
        self.assertFalse(Attendance.objects.filter(employee=self.other).exists())

    def test_monthly_view_renders(self):
        self.login_admin()
        Attendance.objects.create(employee=self.emp, date=date(2026, 10, 12), status='present')
        resp = self.client.get(self.url('hr_attendance_monthly'), {'year': 2026, 'month': 10})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'October 2026')
        # bad params fall back to the current month instead of erroring
        self.assertEqual(self.client.get(self.url('hr_attendance_monthly'),
                                         {'year': 'x', 'month': '99'}).status_code, 200)

    def test_self_service_punch_requires_linked_employee(self):
        self.login_admin()  # admin@example.com matches no employee
        resp = self.client.post(self.url('hr_attendance_check_in'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('error=', resp['Location'])
        self.assertEqual(self.client.get(self.url('hr_attendance_check_in')).status_code, 405)

    def test_punch_for_linked_employee(self):
        user = get_user_model().objects.create_user('asha', 'ASHA@example.com', 'pw')  # case-insensitive match
        self.client.force_login(user)
        with mock.patch.object(leave_utils, 'now_local', return_value=datetime(
                2026, 10, 16, 9, 0, tzinfo=ZoneInfo('Asia/Kolkata'))):
            resp = self.client.post(self.url('hr_attendance_check_in'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('success=', resp['Location'])
        self.assertEqual(Attendance.objects.get(employee=self.emp).check_in, time(9, 0))

    # permissions
    def test_user_without_permission_or_employee_is_redirected(self):
        user = get_user_model().objects.create_user('nobody', 'nobody@example.com', 'pw')
        self.client.force_login(user)
        for name in ('hr_attendance_list', 'hr_attendance_monthly', 'hr_leave_request_list',
                     'hr_leave_request_create', 'hr_leave_balance_list'):
            with self.subTest(view=name):
                resp = self.client.get(self.url(name))
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp['Location'], f'/{self.CO}/')

    def test_view_only_user_cannot_save_attendance(self):
        user = get_user_model().objects.create_user('viewer', 'viewer@example.com', 'pw')
        self.client.force_login(user)
        with mock.patch('HR.attendance_views.check_employee_access',
                        side_effect=lambda u, perm='View': perm == 'View'):
            self.assertEqual(self.client.get(self.url('hr_attendance_list')).status_code, 200)
            resp = self.client.post(self.url('hr_attendance_list'), {
                'date': '2026-10-12', 'employee_id': [self.emp.pk], f'status_{self.emp.pk}': 'present'})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Attendance.objects.exists())

    # leave flow
    def test_apply_approve_and_cancel_flow(self):
        self.login_admin()
        resp = self.client.post(self.url('hr_leave_request_create'), {
            'employee': self.emp.pk, 'leave_type': self.casual.pk,
            'from_date': '2026-10-12', 'to_date': '2026-10-14', 'reason': 'family'})
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', None) and resp.context['form'].errors)
        leave = LeaveRequest.objects.get()
        self.assertEqual((leave.status, leave.days), ('pending', Decimal('3')))
        self.assertEqual(leave.created_by, self.admin)

        listing = self.client.get(self.url('hr_leave_request_list'))
        self.assertContains(listing, 'Review')

        resp = self.client.post(self.url('hr_leave_request_approve', leave.pk), {'remarks': 'enjoy'})
        self.assertEqual(resp.status_code, 302)
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'approved')
        self.assertEqual(Attendance.objects.filter(leave_request=leave).count(), 3)

        resp = self.client.post(self.url('hr_leave_request_cancel', leave.pk))
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'cancelled')
        self.assertFalse(Attendance.objects.exists())

    def test_invalid_leave_shows_errors(self):
        self.login_admin()
        resp = self.client.post(self.url('hr_leave_request_create'), {
            'employee': self.emp.pk, 'leave_type': self.casual.pk,
            'from_date': '2026-10-12', 'to_date': '2026-10-30'})  # > 5-day quota
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Insufficient')
        self.assertFalse(LeaveRequest.objects.exists())

    def test_action_endpoints_require_post(self):
        self.login_admin()
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type=self.casual,
                                            from_date=MON, to_date=MON)
        self.assertEqual(self.client.get(self.url('hr_leave_request_approve', leave.pk)).status_code, 405)

    def test_non_superuser_cannot_approve_own_leave(self):
        user = get_user_model().objects.create_user('asha', 'asha@example.com', 'pw')
        self.client.force_login(user)
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type=self.casual,
                                            from_date=MON, to_date=MON)
        with mock.patch('HR.attendance_views.check_employee_access', return_value=True):
            resp = self.client.post(self.url('hr_leave_request_approve', leave.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('error=', resp['Location'])
        leave.refresh_from_db()
        self.assertEqual(leave.status, 'pending')

    def test_employee_only_sees_and_cancels_own_requests(self):
        mine = LeaveRequest.objects.create(employee=self.emp, leave_type=self.casual,
                                           from_date=MON, to_date=MON)
        theirs = LeaveRequest.objects.create(employee=self.other, leave_type=self.casual,
                                             from_date=MON, to_date=MON)
        user = get_user_model().objects.create_user('asha', 'asha@example.com', 'pw')
        self.client.force_login(user)
        with mock.patch('HR.attendance_views.check_employee_access', return_value=False):
            listing = self.client.get(self.url('hr_leave_request_list'))
            self.assertEqual(listing.status_code, 200)
            self.assertEqual([l.pk for l in listing.context['page'].object_list], [mine.pk])
            self.assertEqual(self.client.post(self.url('hr_leave_request_cancel', theirs.pk)).status_code, 403)
            self.assertEqual(self.client.post(self.url('hr_leave_request_cancel', mine.pk)).status_code, 302)
        mine.refresh_from_db()
        self.assertEqual(mine.status, 'cancelled')

    def test_pages_have_no_broken_links(self):
        self.login_admin()
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type=self.casual,
                                            from_date=MON, to_date=MON)
        LeaveBalance.objects.create(employee=self.emp, leave_type=self.casual, year=2026, allocated=5)
        for name in ('hr_attendance_list', 'hr_attendance_monthly', 'hr_leave_request_list',
                     'hr_leave_request_create', 'hr_leave_balance_list'):
            with self.subTest(view=name):
                resp = self.client.get(self.url(name), {'year': 2026, 'month': 10})
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, 'URL-ERROR')
        listing = self.client.get(self.url('hr_leave_request_list'))
        self.assertContains(listing, self.url('hr_leave_request_approve', leave.pk))
        self.assertContains(listing, self.url('hr_leave_request_reject', leave.pk))
        sidebar = self.client.get(self.url('hr_attendance_list'))
        self.assertContains(sidebar, self.url('hr_leave_request_list'))

    # balances
    def test_balances_allocate_and_update(self):
        self.login_admin()
        resp = self.client.post(self.url('hr_leave_balance_list'), {'action': 'allocate', 'year': 2026})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(LeaveBalance.objects.filter(year=2026).count(), 2)
        page = self.client.get(self.url('hr_leave_balance_list'), {'year': 2026})
        self.assertContains(page, 'Casual')

        balance = LeaveBalance.objects.get(employee=self.emp, year=2026)
        self.client.post(self.url('hr_leave_balance_list'),
                         {'action': 'update', 'balance_id': balance.pk, 'allocated': '8'})
        balance.refresh_from_db()
        self.assertEqual(balance.allocated, Decimal('8.0'))

        # cannot drop allocation below what is already used
        leave = LeaveRequest.objects.create(employee=self.emp, leave_type=self.casual,
                                            from_date=MON, to_date=MON + timedelta(days=2))
        services.approve_leave(leave, self.admin)
        self.client.post(self.url('hr_leave_balance_list'),
                         {'action': 'update', 'balance_id': balance.pk, 'allocated': '1'})
        balance.refresh_from_db()
        self.assertEqual(balance.allocated, Decimal('8.0'))

    # master form
    def test_leave_master_saves_days_allowed(self):
        self.login_admin()
        resp = self.client.post(reverse('leaves_add', kwargs={'company_code': self.CO}),
                                {'leaves_name': 'Sick', 'days_allowed': '7.5', 'status': 'on'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Leaves.objects.get(leaves_name='Sick').days_allowed, Decimal('7.5'))


class ShiftModelTests(TestCase):
    def test_end_must_be_after_start(self):
        with self.assertRaises(ValidationError):
            Shift(name='Bad', start_time=time(17, 0), end_time=time(9, 0)).full_clean()
        with self.assertRaises(ValidationError):
            Shift(name='Zero', start_time=time(9, 0), end_time=time(9, 0)).full_clean()

    def test_str_and_employee_link(self):
        shift = Shift.objects.create(name='General', start_time=time(9, 0), end_time=time(17, 30))
        self.assertEqual(str(shift), 'General (09:00 - 17:30)')
        emp = _make_employee('Asha', shift=shift)
        self.assertEqual(list(shift.employees.all()), [emp])
        shift.delete()
        emp.refresh_from_db()
        self.assertIsNone(emp.shift)


@override_settings(
    DATABASE_ROUTERS=[],
    ALLOWED_HOSTS=['testserver'],
    MIDDLEWARE=ViewTests._overridden_settings['MIDDLEWARE'],
    TEMPLATES=_test_templates(),
)
class ShiftViewTests(TestCase):
    CO = 'TESTCO'

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser('admin', 'admin@example.com', 'pw')
        self.client.force_login(self.admin)
        self.general = Shift.objects.create(name='General', start_time=time(9, 0), end_time=time(17, 30))
        self.emp = _make_employee('Asha', shift=self.general)
        self.nosh = _make_employee('Ben')

    def url(self, name, pk=None):
        kwargs = {'company_code': self.CO}
        if pk:
            kwargs['pk'] = pk
        return reverse(name, kwargs=kwargs)

    def test_create_edit_and_remove_shift(self):
        resp = self.client.post(self.url('hr_shift_create'),
                                {'name': 'Night', 'start_time': '14:00', 'end_time': '22:00'})
        self.assertEqual(resp.status_code, 302)
        night = Shift.objects.get(name='Night')
        self.assertEqual((night.start_time, night.end_time, night.created_by), (time(14, 0), time(22, 0), self.admin))

        self.client.post(self.url('hr_shift_edit', night.pk),
                         {'name': 'Evening', 'start_time': '13:30', 'end_time': '21:30'})
        night.refresh_from_db()
        self.assertEqual((night.name, night.start_time), ('Evening', time(13, 30)))

        self.client.post(self.url('hr_shift_deactivate', night.pk))
        night.refresh_from_db()
        self.assertFalse(night.status)
        listing = self.client.get(self.url('hr_shift_list'))
        self.assertContains(listing, 'General')
        self.assertNotContains(listing, 'Evening')

    def test_validation_errors(self):
        resp = self.client.post(self.url('hr_shift_create'),
                                {'name': 'general', 'start_time': '09:00', 'end_time': '17:00'})
        self.assertContains(resp, 'already exists')  # case-insensitive duplicate
        resp = self.client.post(self.url('hr_shift_create'),
                                {'name': 'Backwards', 'start_time': '17:00', 'end_time': '09:00'})
        self.assertContains(resp, 'End time must be after start time')
        self.assertEqual(Shift.objects.count(), 1)

    def test_deactivate_requires_post_and_permission(self):
        self.assertEqual(self.client.get(self.url('hr_shift_deactivate', self.general.pk)).status_code, 405)
        user = get_user_model().objects.create_user('plain', 'plain@example.com', 'pw')
        self.client.force_login(user)
        with mock.patch('HR.attendance_views.check_master_access', return_value=False):
            self.assertEqual(self.client.post(self.url('hr_shift_deactivate', self.general.pk)).status_code, 403)
            self.assertEqual(self.client.get(self.url('hr_shift_list')).status_code, 302)
        self.general.refresh_from_db()
        self.assertTrue(self.general.status)

    def test_list_shows_employee_counts_and_sidebar_link(self):
        resp = self.client.get(self.url('hr_shift_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'URL-ERROR')
        self.assertEqual(resp.context['shifts'][0].employee_count, 1)
        self.assertContains(resp, self.url('hr_shift_list'))

    def test_attendance_sheet_exposes_shift_times_for_prefill(self):
        resp = self.client.get(self.url('hr_attendance_list'), {'date': '2026-10-12'})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(html.count('data-shift-in="09:00" data-shift-out="17:30"'), 1)  # only Asha
        self.assertIn('General', html)
        self.assertIn('att-in', html)
        self.assertIn('att-out', html)

    def test_employee_form_helper_ignores_blank_invalid_and_removed(self):
        from django.test import RequestFactory
        from HR.views import _shift_from_post
        rf = RequestFactory()
        self.assertEqual(_shift_from_post(rf.post('/', {'shift': str(self.general.pk)})), self.general)
        self.assertIsNone(_shift_from_post(rf.post('/', {'shift': ''})))
        self.assertIsNone(_shift_from_post(rf.post('/', {'shift': 'abc'})))
        self.assertIsNone(_shift_from_post(rf.post('/', {'shift': '99999'})))
        self.general.status = False
        self.general.save()
        self.assertIsNone(_shift_from_post(rf.post('/', {'shift': str(self.general.pk)})))
