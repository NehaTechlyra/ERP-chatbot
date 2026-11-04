from django.db import models
from django.db.models import Q, Count
from django.db import IntegrityError, OperationalError, ProgrammingError
from django.shortcuts import get_object_or_404, render, redirect
from urllib.parse import urlencode
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_redirect_url
from django.http import JsonResponse
import traceback
from datetime import datetime
from collections import defaultdict
from .permissions import (
    check_hr_module_access, check_dashboard_access,
    check_employee_access, check_recruitment_access,
    check_master_access
)
from .forms import CandidateForm, CandidateStatusUpdateForm, JobOpeningForm, RecruitmentOfferForm
from .models import (
    Candidate,
    CandidateStatusHistory,
    Employee,
    EmployeeEducation,
    EmployeeExperience,
    EmployeePersonalDocument,
    JobOpening,
    RecruitmentOffer,
    Shift,
)
from .services import sync_candidate_with_offer, update_candidate_status
import re
import base64


# Normalize phone into format: "<country> <local>" (e.g. "91 9633624411").
def format_phone_db(raw_phone, max_total=16):
    """Normalize phone into "[+]<country> <local>" keeping a leading '+' if present.
    Examples:
      +919633624411 -> +91 9633624411
      919633624411  -> 91 9633624411
      9633624411     -> 9633624411
    max_total includes the '+' if present and the space separator.
    """
    if not raw_phone:
        return None
    s = str(raw_phone).strip()
    # detect and preserve leading plus
    preserve_plus = s.startswith('+')
    # normalize whitespace
    s = re.sub(r"\s+", " ", s)

    # If contains explicit space, treat first token as country
    if ' ' in s:
        parts = s.split(' ')
        country = re.sub(r"\D", '', parts[0])
        rest = ''.join(re.findall(r"\d+", ' '.join(parts[1:])))
        if country and rest:
            out_core = f"{country} {rest}"
        else:
            digits = re.sub(r"\D", '', s)
            if len(digits) > 10:
                country = digits[:-10]
                rest = digits[-10:]
                out_core = f"{country} {rest}"
            else:
                out_core = digits
    else:
        # no explicit spacer: strip all non-digits and decide split
        digits = re.sub(r"\D", '', s)
        if len(digits) > 10:
            country = digits[:-10]
            rest = digits[-10:]
            out_core = f"{country} {rest}"
        else:
            out_core = digits

    # prepend '+' if it was present in the original input
    out = f"+{out_core}" if preserve_plus and out_core else out_core

    # enforce max_total length by truncating the local part if needed
    if len(out) > max_total:
        # try to keep country (and plus if present) and as much local as fits
        if ' ' in out:
            # split into optional +country and local
            if out.startswith('+'):
                plus = '+'
                rest_out = out[1:]
            else:
                plus = ''
                rest_out = out
            country, local = rest_out.split(' ', 1)
            allowed_local = max_total - (len(country) + len(plus) + 1)
            if allowed_local < 0:
                # weird case: truncate overall
                out = out[:max_total]
            else:
                local = local[:allowed_local]
                out = f"{plus}{country} {local}"
        else:
            out = out[:max_total]

    return out

from department.models import Department
from designation.models import Designations
from allowances.models import Allowances
from leaves.models import Leaves
from bank.models import Bank
from django.shortcuts import redirect
from user.utils import has_permission
from personaldocuments.models import PersonalDocumentType
from chart_of_accounts.models import ChartOfAccounts
from django.db.models.functions import TruncMonth

def _shift_from_post(request):
    """The active Shift chosen in the employee form, or None when left blank / invalid."""
    shift_id = (request.POST.get('shift') or '').strip()
    if not shift_id.isdigit():
        return None
    return Shift.objects.filter(pk=int(shift_id), status=True).first()


def employee_register(request):
    # === Permission Check ===
    from .permissions import check_employee_access
    if not check_employee_access(request.user, 'Create'):
        return redirect_with_company('/')

    departments = Department.objects.filter(status=True)
    allowances = Allowances.objects.filter(status=True)
    leaves = Leaves.objects.filter(status=True)
    banks = Bank.objects.filter(status=True)
    personal_doc_types = PersonalDocumentType.objects.filter(status=True)
    source_candidate_id = request.POST.get("source_candidate_id") or request.GET.get("source_candidate_id")
    source_offer_id = request.POST.get("source_offer_id") or request.GET.get("source_offer_id")

    if request.method == 'POST':
        try:
            # --- Helper: Normalize phone number ---
            def format_phone_db(raw_phone, max_total=16):
                if not raw_phone:
                    return None
                s = str(raw_phone).strip()
                s = re.sub(r"\s+", " ", s)
                if ' ' in s:
                    parts = s.split(' ')
                    country = re.sub(r"\D", '', parts[0])
                    rest = ''.join(re.findall(r"\d+", ' '.join(parts[1:])))
                    if country and rest:
                        out = f"{country} {rest}"
                    else:
                        digits = re.sub(r"\D", '', s)
                        if len(digits) > 10:
                            country = digits[:-10]
                            rest = digits[-10:]
                            out = f"{country} {rest}"
                        else:
                            out = digits
                else:
                    digits = re.sub(r"\D", '', s)
                    if len(digits) > 10:
                        country = digits[:-10]
                        rest = digits[-10:]
                        out = f"{country} {rest}"
                    else:
                        out = digits

                if len(out) > max_total:
                    if ' ' in out:
                        country, local = out.split(' ', 1)
                        allowed_local = max_total - (len(country) + 1)
                        local = local[:allowed_local]
                        out = f"{country} {local}"
                    else:
                        out = out[:max_total]
                return out

            # === Personal Details ===
            salutation = request.POST.get("salutation")
            first_name = request.POST.get("first_name")
            last_name = request.POST.get("last_name")
            dob = request.POST.get("date_of_birth")
            email = request.POST.get("email_id")
            phone = format_phone_db(request.POST.get("phone"), max_total=15)
            address = request.POST.get("address")
            permanent_address = request.POST.get("permanent_address")
            marital_status = request.POST.get("marital_status")
            gender = request.POST.get("gender")
            photo = request.FILES.get("photo")

            # === Bank Details ===
            bank_id = request.POST.get("bankname")
            bank = Bank.objects.get(id=bank_id) if bank_id else None
            account_number = request.POST.get("account_number")
            ifsc_code = request.POST.get("ifsc_code")
            bank_upload = request.FILES.get("passbook_photo")

            # === Department & Designation ===
            department_id = request.POST.get("department")
            designation_id = request.POST.get("designation")
            department = Department.objects.get(id=department_id) if department_id else None
            designation = Designations.objects.get(id=designation_id) if designation_id else None

            # === Job Details ===
            job_category = request.POST.get("job_category")
            joining_date = request.POST.get("joining_date")
            confirm_date = request.POST.get("confirm_date")
            notice_period = request.POST.get("notice_period")
            current_salary = request.POST.get("ctc")
            salary_mode = request.POST.get("salary_mode")
            upload_resume = request.FILES.get("resume")

            # === Education Validation ===
            qualifications = request.POST.getlist('qualification[]')
            boards = request.POST.getlist('board_university[]')
            years_pass = request.POST.getlist('year_of_passing[]')

            if not upload_resume:
                return JsonResponse({'status': 'error', 'message': 'Please upload a resume (maximum 2 MB).'})

            # Resume validation
            def is_allowed_resume(f):
                if not f:
                    return False, 'No file provided.'
                try:
                    f_size = f.size
                except Exception:
                    try:
                        pos = f.tell()
                        f.seek(0, 2)
                        f_size = f.tell()
                        f.seek(pos)
                    except Exception:
                        f_size = None
                if f_size and f_size > 2 * 1024 * 1024:
                    return False, 'Resume exceeds 2 MB limit.'

                name = getattr(f, 'name', '') or ''
                ext = name.split('.')[-1].lower() if '.' in name else ''
                allowed_ext = ('pdf', 'doc', 'docx', 'jpg', 'jpeg')
                if ext not in allowed_ext:
                    return False, 'Invalid file type for resume.'

                return True, ''

            ok, msg = is_allowed_resume(upload_resume)
            if not ok:
                return JsonResponse({'status': 'error', 'message': msg})

            education_has_entry = any(
                q.strip() and b.strip() and y.strip()
                for q, b, y in zip(qualifications, boards, years_pass)
            )
            if not education_has_entry:
                return JsonResponse({'status': 'error', 'message': 'Please add at least one complete education entry.'})

            # === Allowances & Leaves ===
            allowance_ids = request.POST.getlist("allowances")
            leave_ids = request.POST.getlist("leaves")

            # File helper
            def file_to_data_url(f):
                if not f:
                    return None
                content = f.read()
                content_type = getattr(f, 'content_type', 'application/octet-stream')
                b64 = base64.b64encode(content).decode('ascii')
                return f"data:{content_type};base64,{b64}"

            # === Create Employee ===
            # --- Validation: required fields ---
            if not email or not str(email).strip():
                return JsonResponse({'status': 'error', 'message': 'Please provide an email address.'})
            if not phone:
                return JsonResponse({'status': 'error', 'message': 'Please provide a phone number.'})

            employee = Employee.objects.create(
                salutation=salutation,
                first_name=first_name,
                last_name=last_name,
                dob=dob,
                email=email,
                phone=phone,
                address=address,
                permanent_address=permanent_address,
                marital_status=marital_status,
                gender=gender,
                photo_base64=file_to_data_url(photo),
                bank_name=bank,
                account_number=account_number,
                ifsc_code=ifsc_code,
                bank_upload_base64=file_to_data_url(bank_upload),
                department=department,
                designation=designation,
                shift=_shift_from_post(request),
                job_category=job_category,
                joining_date=joining_date,
                confirm_date=confirm_date,
                notice_period=notice_period,
                current_salary=current_salary,
                salary_mode=salary_mode,
                upload_resume_base64=file_to_data_url(upload_resume),
            )

            if source_candidate_id:
                source_candidate = Candidate.objects.filter(pk=source_candidate_id, is_active=True).first()
                if source_candidate:
                    source_candidate.employee = employee
                    if request.user.is_authenticated:
                        source_candidate.updated_by = request.user
                    source_candidate.save(update_fields=['employee', 'updated_by', 'updated_at'])
                    if source_candidate.current_status != 'joined':
                        update_candidate_status(source_candidate, 'joined', user=request.user, remarks=f'Converted to employee {employee.emp_code}.')

            if source_offer_id and _recruitment_offer_table_ready():
                source_offer = RecruitmentOffer.objects.filter(pk=source_offer_id, is_active=True).first()
                if source_offer:
                    source_offer.converted_to_employee_at = timezone.now()
                    if request.user.is_authenticated:
                        source_offer.updated_by = request.user
                    source_offer.save(update_fields=['converted_to_employee_at', 'updated_by', 'updated_at'])

            # Create a Chart of Accounts entry for this employee under the "Salary" parent
            try:
                salary_parent = ChartOfAccounts.objects.filter(code='40218').first() or ChartOfAccounts.objects.filter(name__iexact='Salary').first()
                if salary_parent:
                    # Determine next available child code by using numeric suffixes after parent.code
                    children_codes = ChartOfAccounts.objects.filter(parent=salary_parent).values_list('code', flat=True)
                    suffix_nums = []
                    for c in children_codes:
                        suffix = c[len(salary_parent.code):]
                        if suffix.isdigit():
                            suffix_nums.append(int(suffix))
                    next_suffix = (max(suffix_nums) + 1) if suffix_nums else 1
                    new_code = f"{salary_parent.code}{next_suffix:02d}"
                    # Ensure uniqueness by incrementing if needed
                    while ChartOfAccounts.objects.filter(code=new_code).exists():
                        next_suffix += 1
                        new_code = f"{salary_parent.code}{next_suffix:02d}"

                    coa = ChartOfAccounts.objects.create(
                        code=new_code,
                        name=f"{first_name} {last_name}",
                        type=salary_parent.type,
                        parent=salary_parent,
                        description=f"Salary account for {first_name} {last_name}",
                        is_header=False,
                        active=True,
                        status=True,
                        created_by=request.user if getattr(request, 'user', None) and request.user.is_authenticated else None,
                        updated_by=request.user if getattr(request, 'user', None) and request.user.is_authenticated else None,
                    )
                    employee.account_number = new_code
                    employee.save(update_fields=['account_number'])
            except Exception:
                # silent fail: employee is still created even if COA creation fails
                pass

            if allowance_ids:
                employee.allowances.set(allowance_ids)
            if leave_ids:
                employee.leaves.set(leave_ids)

            # === Education ===
            streams = request.POST.getlist('stream[]')
            years_join = request.POST.getlist('year_of_joining[]')

            for i in range(len(qualifications)):
                if qualifications[i]:
                    EmployeeEducation.objects.create(
                        employee=employee,
                        qualification=qualifications[i],
                        board_university=boards[i] if i < len(boards) else "",
                        stream=streams[i] if i < len(streams) else "",
                        year_of_joining=years_join[i] if i < len(years_join) else None,
                        year_of_passing=years_pass[i] if i < len(years_pass) else None,
                    )

            # === Work Experience ===
            roles = request.POST.getlist('role[]')
            company_names = request.POST.getlist('company_name[]')
            ctcs = request.POST.getlist('ctc_exp[]')
            durations = request.POST.getlist('duration_months[]')

            for i in range(len(roles)):
                if roles[i] or company_names[i] or ctcs[i] or durations[i]:
                    EmployeeExperience.objects.create(
                        employee=employee,
                        role=roles[i],
                        company_name=company_names[i],
                        ctc=ctcs[i] if ctcs[i] else None,
                        duration_months=durations[i] if durations[i] else None,
                    )

            # === Personal Documents ===
            doc_type_ids = request.POST.getlist('personal_document_type[]')
            doc_numbers = request.POST.getlist('personal_document_number[]')
            doc_files = request.FILES.getlist('personal_document_file[]')
            doc_expiry_dates = request.POST.getlist('personal_document_expiry[]')

            for i in range(len(doc_type_ids)):
                if doc_type_ids[i].strip():
                    expiry = None
                    if i < len(doc_expiry_dates) and doc_expiry_dates[i].strip():
                        try:
                            expiry = datetime.strptime(doc_expiry_dates[i], '%Y-%m-%d').date()
                        except ValueError:
                            expiry = None

                    EmployeePersonalDocument.objects.create(
                        employee=employee,
                        document_type_id=int(doc_type_ids[i]),
                        document_number=doc_numbers[i] if i < len(doc_numbers) else "NA",
                        document_file_base64=file_to_data_url(doc_files[i]) if i < len(doc_files) else None,
                        document_file_name=getattr(doc_files[i], 'name', None) if i < len(doc_files) else None,
                        expiry_date=expiry
                    )

            return JsonResponse({'status': 'success', 'message': 'Employee registered successfully!'})

        except IntegrityError as e:
            if 'email' in str(e):
                return JsonResponse({'status': 'error', 'message': 'Email already exists.'})
            return JsonResponse({'status': 'error', 'message': 'Database error occurred.'})

        except Exception as e:
            traceback.print_exc()
            return JsonResponse({'status': 'error', 'message': str(e)})

    # === GET Request ===
    prefill = {
        'first_name': request.GET.get('first_name', ''),
        'last_name': request.GET.get('last_name', ''),
        'email': request.GET.get('email', ''),
        'phone': request.GET.get('phone', ''),
        'department_id': request.GET.get('department_id', ''),
        'designation_id': request.GET.get('designation_id', ''),
        'job_category': request.GET.get('job_category', ''),
        'joining_date': request.GET.get('joining_date', ''),
        'notice_period': request.GET.get('notice_period', ''),
        'ctc': request.GET.get('ctc', ''),
        'resume_name': request.GET.get('resume_name', ''),
    }
    return render(request, 'employee_register.html', {
        "shifts": Shift.objects.filter(status=True),
        "departments": departments,
        "allowances": allowances,
        "leaves": leaves,
        "banks": banks,
        "personal_doc_types": personal_doc_types,
        "prefill": prefill,
        "source_candidate_id": source_candidate_id,
        "source_offer_id": source_offer_id,
    })


def sidebar(request):
    # Sidebar endpoint placeholder. Original implementation was malformed
    # and referenced undefined variables. Keep as a small stub to avoid
    # breaking imports until a proper implementation is provided.
    return JsonResponse({'success': False, 'error': 'not_implemented'})



from django.shortcuts import render
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.utils import timezone
from datetime import timedelta
from HR.models import Employee, Department, EmployeePersonalDocument, HRNotification

def hr_dashboard(request):
    # === Employees list with pagination ===
    employees_qs = Employee.objects.filter(status=True).order_by('-id')
    page = request.GET.get('page', 1)
    paginator = Paginator(employees_qs, 10)
    try:
        employees_page = paginator.page(page)
    except PageNotAnInteger:
        employees_page = paginator.page(1)
    except EmptyPage:
        employees_page = paginator.page(paginator.num_pages)

    # === Department Chart ===
    departments = Department.objects.filter(status=True)
    dept_chart = []
    max_count = 0
    for i, dept in enumerate(departments):
        cnt = Employee.objects.filter(department=dept, status=True).count()
        hue = int((i * 360) / max(1, len(departments)))
        color = f"hsl({hue}, 65%, 50%)"
        dept_chart.append({
            'name': getattr(dept, 'department_name', str(dept.id)),
            'count': cnt,
            'color': color,
        })
        if cnt > max_count:
            max_count = cnt
    for d in dept_chart:
        d['pct'] = int((d['count'] * 100) / max_count) if max_count > 0 else 0

    # === Expiry Date Logic ===
    today = timezone.now().date()
    three_months = today + timedelta(days=90)
    expiry_docs = EmployeePersonalDocument.objects.filter(
        expiry_date__lte=three_months,
        employee__status=True
    ).select_related('employee', 'document_type').order_by('expiry_date')

    # === Ensure HR Notifications Exist for Expiring Documents ===
    for doc in expiry_docs:
        exists = HRNotification.objects.filter(document=doc).exists()
        if not exists:
            HRNotification.objects.create(
                document=doc,
                employee=doc.employee,
                is_read=False,
                method_email=False,
                method_sms=False,
                last_sent_email=None,
                last_sent_sms=None,
                created_at=timezone.now()
            )

    # === Unread Notifications ===
    unread_alerts = HRNotification.objects.filter(
        is_read=False,
        document__employee__status=True
    ).select_related('document__employee', 'document__document_type')

    # Sort by latest send (either email or SMS)
    def latest_sent(notif):
        dates = [d for d in [notif.last_sent_email, notif.last_sent_sms] if d]
        return max(dates) if dates else notif.created_at

    unread_alerts = sorted(unread_alerts, key=lambda n: latest_sent(n), reverse=True)

    alerts = []
    for notif in unread_alerts[:5]:  # limit for dropdown
        expiry_date = getattr(notif.document, 'expiry_date', None)
        alerts.append({
            'employee': notif.document.employee,
            'doc_name': notif.document.document_type.name if notif.document.document_type else 'Unknown',
            'expiry_date': expiry_date,
            'is_expired': expiry_date and expiry_date < today,
        })

    unread_count = len(unread_alerts) if unread_alerts else 0

    return render(request, 'hr_dashboard.html', {
        'employees_page': employees_page,
        'paginator': paginator,
        'dept_chart': dept_chart,
        'expiry_alerts': alerts,
        'unread_count': unread_count,
    })

def recruit_dashboard(request):
    # Require recruitment access
    if not check_recruitment_access(request.user):
        return redirect_with_company('/')
    open_jobs = JobOpening.objects.filter(is_active=True, status='open')
    active_candidates = Candidate.objects.filter(is_active=True)
    offers_enabled = _recruitment_offer_table_ready()
    active_offers = RecruitmentOffer.objects.filter(is_active=True) if offers_enabled else RecruitmentOffer.objects.none()
    now = timezone.now()
    def _build_month_starts(base, count):
        starts = []
        for offset in range(count - 1, -1, -1):
            month = base.month - offset
            year = base.year
            while month <= 0:
                month += 12
                year -= 1
            starts.append(base.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0))
        return starts

    month_starts = _build_month_starts(now, 6)
    trend_start = month_starts[0] if month_starts else now
    trend_qs = active_candidates.filter(created_at__gte=trend_start)
    trend_counts = trend_qs.annotate(month=TruncMonth('created_at')).values('month').annotate(count=Count('pk')).order_by('month')
    trend_map = {
        row['month'].strftime('%Y-%m'): row['count']
        for row in trend_counts
        if row.get('month')
    }
    trend_rows = [
        {'label': start.strftime('%b'), 'count': trend_map.get(start.strftime('%Y-%m'), 0)}
        for start in month_starts
    ]

    dept_field = 'job_opening__department__department_name'
    dept_aggregates = active_candidates.values(dept_field).annotate(count=Count('pk')).order_by('-count')
    dept_rows = []
    for row in dept_aggregates:
        label = row.get(dept_field) or 'Unassigned Department'
        dept_rows.append({'label': label, 'count': row['count']})

    # Open positions by department (for dynamic open positions chart)
    open_positions_aggregates = open_jobs.values('department__department_name').annotate(count=Count('pk')).order_by('-count')
    open_positions_rows = []
    for row in open_positions_aggregates:
        label = row.get('department__department_name') or 'Unassigned Department'
        open_positions_rows.append({'label': label, 'count': row['count']})

    # Build source rows including all defined choices (show zero if none)
    source_label_map = dict(Candidate.SOURCE_CHOICES)
    source_aggregates = active_candidates.values('source').annotate(count=Count('pk'))
    source_count_map = {row.get('source'): row['count'] for row in source_aggregates}
    source_rows = []
    for code, label in Candidate.SOURCE_CHOICES:
        display = label or (code.title() if code else 'Unknown')
        count = source_count_map.get(code, 0)
        source_rows.append({'label': display, 'count': count})

    hire_time_map = defaultdict(list)
    joined_candidates = active_candidates.filter(current_status='joined').select_related('job_opening')
    for candidate in joined_candidates:
        if not candidate.created_at or not candidate.updated_at:
            continue
        days = (candidate.updated_at - candidate.created_at).days
        if days < 0:
            continue
        title = candidate.job_opening.title if candidate.job_opening else 'Unassigned Position'
        hire_time_map[title].append(days)

    hire_time_rows = []
    for title, values in hire_time_map.items():
        if not values:
            continue
        avg_days = round(sum(values) / len(values))
        hire_time_rows.append({'label': title, 'days': avg_days})
    hire_time_rows.sort(key=lambda row: row['days'], reverse=True)
    hire_time_rows = hire_time_rows[:6]

    context = {
        'total_open_positions': open_jobs.count(),
        'total_applications': active_candidates.count(),
        'shortlisted_candidates': active_candidates.filter(current_status='shortlisted').count(),
        'interviews_scheduled': active_candidates.filter(current_status='interview_scheduled').count(),
        'offers_released': active_offers.filter(status__in=['sent', 'accepted']).count(),
        'offers_accepted': active_offers.filter(status='accepted').count(),
        'new_hires_this_month': active_candidates.filter(
            current_status='joined',
            updated_at__year=now.year,
            updated_at__month=now.month,
        ).count(),
        'recent_jobs': JobOpening.objects.filter(is_active=True).select_related('department', 'designation').order_by('-created_at')[:5],
        'recent_candidates': active_candidates.select_related('job_opening').order_by('-created_at')[:5],
        'dept_rows': dept_rows,
        'open_positions_rows': open_positions_rows,
        'trend_rows': trend_rows,
        'hire_time_rows': hire_time_rows,
        'source_rows': source_rows,
    }
    return render(request, 'recruit_dashboard.html', context)


def open_positions_data(request):
    if not check_recruitment_access(request.user):
        return JsonResponse({'error': 'forbidden'}, status=403)
    open_jobs = JobOpening.objects.filter(is_active=True, status='open')
    aggregates = open_jobs.values('department__department_name').annotate(count=Count('pk')).order_by('-count')
    rows = []
    for row in aggregates:
        label = row.get('department__department_name') or 'Unassigned Department'
        rows.append({'label': label, 'count': row['count']})
    return JsonResponse({'rows': rows})


def job_creation(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Create'):
        return redirect_with_company('/')

    form = JobOpeningForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        job = form.save(commit=False)
        if request.user.is_authenticated:
            if not job.pk:
                job.created_by = request.user
            job.updated_by = request.user
        job.save()
        return redirect_with_company(request, 'job_list')

    return render(request, 'job_creation.html', {
        'form': form,
        'status_choices': JobOpening.STATUS_CHOICES,
        'page_mode': 'create',
    })


def job_list(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')

    search_query = (request.GET.get('q') or '').strip()
    jobs_qs = JobOpening.objects.filter(is_active=True).select_related('department', 'designation')
    if search_query:
        jobs_qs = jobs_qs.filter(
            Q(title__icontains=search_query) |
            Q(department__department_name__icontains=search_query) |
            Q(designation__designation_name__icontains=search_query)
        )
    paginator = Paginator(jobs_qs.order_by('-created_at'), 10)
    page_number = request.GET.get('page', 1)
    try:
        jobs_page = paginator.page(page_number)
    except PageNotAnInteger:
        jobs_page = paginator.page(1)
    except EmptyPage:
        jobs_page = paginator.page(paginator.num_pages)

    return render(request, 'job_list.html', {
        'jobs': jobs_page,
        'search_query': search_query,
    })


def job_detail(request, pk):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')

    job = get_object_or_404(
        JobOpening.objects.select_related('department', 'designation', 'created_by', 'updated_by'),
        pk=pk,
        is_active=True,
    )
    return render(request, 'job_detail.html', {
        'job': job,
    })


def job_edit(request, pk):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Edit'):
        return redirect_with_company('/')

    job = get_object_or_404(JobOpening, pk=pk, is_active=True)
    form = JobOpeningForm(request.POST or None, instance=job)
    if request.method == 'POST' and form.is_valid():
        job = form.save(commit=False)
        if request.user.is_authenticated:
            job.updated_by = request.user
        job.save()
        return redirect_with_company(request, 'job_list')

    return render(request, 'job_creation.html', {
        'form': form,
        'status_choices': JobOpening.STATUS_CHOICES,
        'page_mode': 'edit',
        'job': job,
    })


def job_delete(request, pk):
    if not check_recruitment_access(request.user, 'Delete'):
        return redirect_with_company('/')

    job = get_object_or_404(JobOpening, pk=pk, is_active=True)
    if request.method == 'POST':
        job.is_active = False
        if request.user.is_authenticated:
            job.updated_by = request.user
        job.save(update_fields=['is_active', 'updated_by', 'updated_at'])
        return redirect_with_company(request, 'job_list')

    return render(request, 'job_delete_confirm.html', {
        'job': job,
    })


def _file_to_data_url(f):
    if not f:
        return None
    content = f.read()
    content_type = getattr(f, 'content_type', 'application/octet-stream')
    b64 = base64.b64encode(content).decode('ascii')
    return f"data:{content_type};base64,{b64}"


def _recruitment_offer_table_ready():
    try:
        RecruitmentOffer.objects.exists()
        return True
    except (ProgrammingError, OperationalError):
        return False


def _build_offer_context(offer):
    company_obj = Company.objects.first()
    company_name = company_obj.name if company_obj else "Company"
    department_name = (
        offer.job_opening.department.department_name
        if offer.job_opening.department else "-"
    )
    designation_name = (
        offer.job_opening.designation.designation_name
        if offer.job_opening.designation else offer.job_opening.title
    )
    offered_ctc = offer.offered_ctc if offer.offered_ctc is not None else "-"
    joining_date = offer.joining_date.strftime("%d %B %Y") if offer.joining_date else "-"
    expiry_date = offer.expiry_date.strftime("%d %B %Y") if offer.expiry_date else "-"
    return {
        "candidate": offer.candidate.full_name,
        "job_title": offer.job_opening.title,
        "department": department_name,
        "designation": designation_name,
        "offered_ctc": offered_ctc,
        "joining_date": joining_date,
        "reporting_manager": offer.reporting_manager or "-",
        "company": company_name,
        "offer_date": offer.offer_date.strftime("%d %B %Y") if offer.offer_date else "-",
        "expiry_date": expiry_date,
    }


def _get_offer_email_content(offer):
    context = _build_offer_context(offer)
    template = get_default_email_template("Appointment Letter")
    if template:
        return (
            replace_placeholders(template["subject"], context),
            replace_placeholders(template["body"], context),
        )

    subject = f"Appointment Letter - {context['designation']}"
    body = f"""
        <p>Dear <strong>{context['candidate']}</strong>,</p>
        <p>We are pleased to offer you the position of <strong>{context['designation']}</strong> at <strong>{context['company']}</strong>.</p>
        <p><strong>Department:</strong> {context['department']}<br>
        <strong>Joining Date:</strong> {context['joining_date']}<br>
        <strong>Reporting Manager:</strong> {context['reporting_manager']}<br>
        <strong>Offered CTC:</strong> {context['offered_ctc']}</p>
        <p>Please confirm your acceptance before <strong>{context['expiry_date']}</strong>.</p>
        <p>Regards,<br>{context['company']}</p>
    """
    return subject, body


def _send_offer_email(offer):
    email_to = offer.email_to or offer.candidate.email
    if not email_to:
        return False, "Candidate email not found."

    email_config = EmailConfiguration.objects.filter(
        usage_types__icontains="Appointment Letter",
        status=True,
    ).first()
    if not email_config:
        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first() or EmailConfiguration.objects.filter(status=True).first()
    if not email_config:
        return False, "Email configuration not found. Configure an active email account first."

    subject, message_html = _get_offer_email_content(offer)
    connection = get_connection(
        host=email_config.host,
        port=email_config.port,
        username=email_config.host_user,
        password=email_config.host_password,
        use_tls=email_config.use_tls,
        fail_silently=False,
    )
    django_send_mail(
        subject,
        "",
        email_config.default_from_email or email_config.host_user,
        [email_to],
        connection=connection,
        html_message=message_html,
    )
    return True, f"Appointment letter sent to {email_to}."


def _split_candidate_name(full_name):
    parts = [part for part in (full_name or '').strip().split() if part]
    if not parts:
        return 'Employee', '-'
    if len(parts) == 1:
        return parts[0], '-'
    return parts[0], ' '.join(parts[1:])


def _build_employee_prefill(candidate, offer=None):
    first_name, last_name = _split_candidate_name(candidate.full_name)
    return {
        'first_name': first_name,
        'last_name': last_name if last_name != '-' else '',
        'email': candidate.email or '',
        'phone': candidate.phone or '',
        'department_id': candidate.job_opening.department_id or '',
        'designation_id': candidate.job_opening.designation_id or '',
        'job_category': candidate.job_opening.get_employment_type_display(),
        'joining_date': offer.joining_date.strftime('%Y-%m-%d') if offer and offer.joining_date else '',
        'notice_period': candidate.notice_period_days or '',
        'ctc': offer.offered_ctc if offer and offer.offered_ctc is not None else '',
        'resume_name': candidate.resume_name or '',
        'source_candidate_id': candidate.pk,
        'source_offer_id': offer.pk if offer else '',
    }


def candidate_list(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')

    candidates = Candidate.objects.filter(is_active=True).select_related('job_opening')
    search_query = (request.GET.get('q') or '').strip()
    selected_status = (request.GET.get('status') or '').strip()
    selected_job = (request.GET.get('job') or '').strip()
    if search_query:
        candidates = candidates.filter(
            Q(full_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(job_opening__title__icontains=search_query) |
            Q(current_company__icontains=search_query)
        )
    if selected_status:
        candidates = candidates.filter(current_status=selected_status)
    if selected_job:
        candidates = candidates.filter(job_opening_id=selected_job)

    return render(request, 'candidate_list.html', {
        'candidates': candidates.order_by('-created_at'),
        'jobs': JobOpening.objects.filter(is_active=True).order_by('title'),
        'status_choices': Candidate.STATUS_CHOICES,
        'selected_status': selected_status,
        'selected_job': selected_job,
        'search_query': search_query,
    })


def candidate_detail(request, pk):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')

    candidate = get_object_or_404(
        Candidate.objects.select_related('job_opening', 'created_by', 'updated_by'),
        pk=pk,
        is_active=True,
    )
    return render(request, 'candidate_detail.html', {
        'candidate': candidate,
        'history': candidate.status_history.select_related('changed_by').all(),
        'offers': candidate.offers.filter(is_active=True).order_by('-created_at') if _recruitment_offer_table_ready() else [],
    })


def candidate_create(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Create'):
        return redirect_with_company('/')

    form = CandidateForm(request.POST or None, request.FILES or None)
    status_form = CandidateStatusUpdateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid() and status_form.is_valid():
        candidate = form.save(commit=False)
        status_candidate = status_form.save(commit=False)
        candidate.current_status = status_candidate.current_status
        candidate.remarks = status_candidate.remarks
        resume = form.cleaned_data.get('resume')
        if resume:
            candidate.resume_base64 = _file_to_data_url(resume)
            candidate.resume_name = resume.name
        if request.user.is_authenticated:
            candidate.created_by = request.user
            candidate.updated_by = request.user
        candidate.save()
        CandidateStatusHistory.objects.create(
            candidate=candidate,
            old_status='',
            new_status=candidate.current_status,
            remarks=candidate.remarks,
            changed_by=request.user if request.user.is_authenticated else None,
        )
        return redirect_with_company(request, 'candidate_list')

    return render(request, 'candidate_form.html', {
        'form': form,
        'status_form': status_form,
        'page_title': 'Add Candidate',
        'submit_label': 'Save Candidate',
        'candidate': None,
        'history': [],
    })


def candidate_edit(request, pk):
    candidate = get_object_or_404(Candidate.objects.select_related('job_opening'), pk=pk, is_active=True)
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Edit'):
        return redirect_with_company('/')

    original_status = candidate.current_status
    form = CandidateForm(request.POST or None, request.FILES or None, instance=candidate)
    status_form = CandidateStatusUpdateForm(request.POST or None, instance=candidate)

    if request.method == 'POST' and form.is_valid() and status_form.is_valid():
        candidate = form.save(commit=False)
        status_candidate = status_form.save(commit=False)
        candidate.current_status = status_candidate.current_status
        candidate.remarks = status_candidate.remarks
        resume = form.cleaned_data.get('resume')
        if resume:
            candidate.resume_base64 = _file_to_data_url(resume)
            candidate.resume_name = resume.name
        if request.user.is_authenticated:
            candidate.updated_by = request.user
        candidate.save()
        if original_status != candidate.current_status or candidate.remarks:
            CandidateStatusHistory.objects.create(
                candidate=candidate,
                old_status=original_status,
                new_status=candidate.current_status,
                remarks=candidate.remarks,
                changed_by=request.user if request.user.is_authenticated else None,
            )
        return redirect_with_company(request, 'candidate_edit', pk=candidate.pk)

    return render(request, 'candidate_form.html', {
        'form': form,
        'status_form': status_form,
        'page_title': 'Edit Candidate',
        'submit_label': 'Update Candidate',
        'candidate': candidate,
        'history': candidate.status_history.select_related('changed_by').all(),
    })


def candidate_delete(request, pk):
    if not check_recruitment_access(request.user, 'Delete'):
        return redirect_with_company('/')

    candidate = get_object_or_404(Candidate, pk=pk, is_active=True)
    if request.method == 'POST':
        candidate.is_active = False
        if request.user.is_authenticated:
            candidate.updated_by = request.user
        candidate.save(update_fields=['is_active', 'updated_by', 'updated_at'])
        return redirect_with_company(request, 'candidate_list')

    return render(request, 'candidate_delete_confirm.html', {
        'candidate': candidate,
    })


def offer_list(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')

    search_query = (request.GET.get('q') or '').strip()
    selected_status = (request.GET.get('status') or '').strip()
    offers_qs = RecruitmentOffer.objects.filter(is_active=True).select_related('candidate', 'job_opening')
    if selected_status:
        offers_qs = offers_qs.filter(status=selected_status)
    if search_query:
        offers_qs = offers_qs.filter(
            Q(candidate__full_name__icontains=search_query) |
            Q(candidate__email__icontains=search_query) |
            Q(job_opening__title__icontains=search_query)
        )

    paginator = Paginator(offers_qs.order_by('-created_at'), 10)
    page_number = request.GET.get('page', 1)
    try:
        offers_page = paginator.page(page_number)
    except PageNotAnInteger:
        offers_page = paginator.page(1)
    except EmptyPage:
        offers_page = paginator.page(paginator.num_pages)

    return render(request, 'offer_list.html', {
        'offers': offers_page,
        'status_choices': RecruitmentOffer.STATUS_CHOICES,
        'selected_status': selected_status,
        'search_query': search_query,
    })


def offer_create(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Create'):
        return redirect_with_company('/')

    initial = {}
    candidate_id = request.GET.get('candidate')
    if candidate_id:
        initial['candidate'] = candidate_id
    form = RecruitmentOfferForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        offer = form.save(commit=False)
        if request.user.is_authenticated:
            offer.created_by = request.user
            offer.updated_by = request.user
        offer._skip_candidate_sync = True
        try:
            offer.save()
        finally:
            if hasattr(offer, '_skip_candidate_sync'):
                delattr(offer, '_skip_candidate_sync')
        sync_candidate_with_offer(offer, request.user)
        return redirect_with_company(request, 'offer_detail', pk=offer.pk)

    return render(request, 'offer_form.html', {
        'form': form,
        'page_title': 'Create Offer',
        'submit_label': 'Save Offer',
        'offer': None,
    })


def offer_detail(request, pk):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')

    offer = get_object_or_404(
        RecruitmentOffer.objects.select_related('candidate', 'job_opening', 'created_by', 'updated_by'),
        pk=pk,
        is_active=True,
    )
    subject, preview_body = _get_offer_email_content(offer)
    return render(request, 'offer_detail.html', {
        'offer': offer,
        'email_preview_subject': subject,
        'email_preview_body': preview_body,
        'mail_status': request.GET.get('mail_status', '').strip(),
        'mail_message': request.GET.get('mail_message', '').strip(),
        'can_convert_to_employee': False,
    })


def offer_edit(request, pk):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')
    if request.method == 'POST' and not check_recruitment_access(request.user, 'Edit'):
        return redirect_with_company('/')

    offer = get_object_or_404(RecruitmentOffer, pk=pk, is_active=True)
    previous_status = offer.status
    form = RecruitmentOfferForm(request.POST or None, instance=offer)
    if request.method == 'POST' and form.is_valid():
        offer = form.save(commit=False)
        if request.user.is_authenticated:
            offer.updated_by = request.user
        offer._skip_candidate_sync = True
        try:
            offer.save()
        finally:
            if hasattr(offer, '_skip_candidate_sync'):
                delattr(offer, '_skip_candidate_sync')
        if previous_status != offer.status:
            sync_candidate_with_offer(offer, request.user)
        return redirect_with_company(request, 'offer_detail', pk=offer.pk)

    return render(request, 'offer_form.html', {
        'form': form,
        'page_title': 'Edit Offer',
        'submit_label': 'Update Offer',
        'offer': offer,
    })


def offer_delete(request, pk):
    if not check_recruitment_access(request.user, 'Delete'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')

    offer = get_object_or_404(RecruitmentOffer, pk=pk, is_active=True)
    if request.method == 'POST':
        offer.is_active = False
        if request.user.is_authenticated:
            offer.updated_by = request.user
        offer.save(update_fields=['is_active', 'updated_by', 'updated_at'])
        return redirect_with_company(request, 'offer_list')

    return render(request, 'offer_delete_confirm.html', {
        'offer': offer,
    })


def offer_send_email(request, pk):
    if request.method != 'POST':
        return redirect_with_company(request, 'offer_detail', pk=pk)
    if not check_recruitment_access(request.user, 'Edit'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')

    offer = get_object_or_404(RecruitmentOffer, pk=pk, is_active=True)
    try:
        success, message = _send_offer_email(offer)
        if success:
            offer.status = 'sent'
            offer.sent_at = timezone.now()
            if request.user.is_authenticated:
                offer.updated_by = request.user
            offer._skip_candidate_sync = True
            try:
                offer.save(update_fields=['status', 'sent_at', 'updated_by', 'updated_at'])
            finally:
                if hasattr(offer, '_skip_candidate_sync'):
                    delattr(offer, '_skip_candidate_sync')
            sync_candidate_with_offer(offer, request.user)
        status = 'success' if success else 'error'
        base_url = get_company_redirect_url(request, 'offer_detail', pk=offer.pk)
        query = urlencode({'mail_status': status, 'mail_message': message})
        return redirect(f'{base_url}?{query}')
    except Exception as exc:
        traceback.print_exc()
        base_url = get_company_redirect_url(request, 'offer_detail', pk=offer.pk)
        query = urlencode({'mail_status': 'error', 'mail_message': str(exc)})
        return redirect(f'{base_url}?{query}')


def offer_update_status(request, pk):
    if request.method != 'POST':
        return redirect_with_company(request, 'offer_detail', pk=pk)
    if not check_recruitment_access(request.user, 'Edit'):
        return redirect_with_company('/')
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')

    offer = get_object_or_404(RecruitmentOffer, pk=pk, is_active=True)
    new_status = (request.POST.get('status') or '').strip()
    if new_status not in dict(RecruitmentOffer.STATUS_CHOICES):
        base_url = get_company_redirect_url(request, 'offer_detail', pk=offer.pk)
        query = urlencode({'mail_status': 'error', 'mail_message': 'Invalid offer status.'})
        return redirect(f'{base_url}?{query}')

    offer.status = new_status
    offer.responded_at = timezone.now() if new_status in ['accepted', 'declined'] else offer.responded_at
    offer.response_notes = request.POST.get('response_notes', offer.response_notes)
    if request.user.is_authenticated:
        offer.updated_by = request.user
    offer._skip_candidate_sync = True
    try:
        offer.save(update_fields=['status', 'responded_at', 'response_notes', 'updated_by', 'updated_at'])
    finally:
        if hasattr(offer, '_skip_candidate_sync'):
            delattr(offer, '_skip_candidate_sync')
    sync_candidate_with_offer(offer, request.user)
    base_url = get_company_redirect_url(request, 'offer_detail', pk=offer.pk)
    query = urlencode({'mail_status': 'success', 'mail_message': f'Offer marked as {offer.get_status_display()}.'})
    return redirect(f'{base_url}?{query}')


def offer_convert_to_employee(request, pk):
    if request.method != 'POST':
        return redirect_with_company(request, 'offer_detail', pk=pk)
    if not _recruitment_offer_table_ready():
        return redirect_with_company(request, 'recruiter_dashboard')
    if not (check_employee_access(request.user, 'Create') or check_recruitment_access(request.user, 'Edit')):
        return redirect_with_company('/')

    offer = get_object_or_404(
        RecruitmentOffer.objects.select_related('candidate', 'job_opening__department', 'job_opening__designation'),
        pk=pk,
        is_active=True,
    )
    base_url = get_company_redirect_url(request, 'offer_detail', pk=offer.pk)

    if offer.status != 'accepted':
        query = urlencode({'mail_status': 'error', 'mail_message': 'Only accepted offers can be converted to employee.'})
        return redirect(f'{base_url}?{query}')
    if offer.candidate.current_status != 'joined':
        update_candidate_status(offer.candidate, 'joined', user=request.user, remarks='Ready for employee onboarding.')
    prefill = _build_employee_prefill(offer.candidate, offer)
    base_employee_url = get_company_redirect_url(request, 'employee_register')
    return redirect(f"{base_employee_url}?{urlencode(prefill)}")


def candidate_convert_to_employee(request, pk):
    if request.method != 'POST':
        return redirect_with_company(request, 'candidate_detail', pk=pk)
    if not (check_employee_access(request.user, 'Create') or check_recruitment_access(request.user, 'Edit')):
        return redirect_with_company('/')

    candidate = get_object_or_404(Candidate.objects.select_related('job_opening__department', 'job_opening__designation'), pk=pk, is_active=True)
    if candidate.current_status != 'joined':
        return redirect_with_company(request, 'candidate_detail', pk=pk)
    if candidate.employee_id:
        employee_url = get_company_redirect_url(request, 'employee_edit', pk=candidate.employee_id)
        return redirect(employee_url)

    offer = None
    if _recruitment_offer_table_ready():
        offer = candidate.offers.filter(is_active=True, status='accepted').order_by('-updated_at', '-created_at').first()
    prefill = _build_employee_prefill(candidate, offer)
    base_employee_url = get_company_redirect_url(request, 'employee_register')
    return redirect(f"{base_employee_url}?{urlencode(prefill)}")


def recruitment_reports(request):
    if not check_recruitment_access(request.user, 'View'):
        return redirect_with_company('/')

    jobs = JobOpening.objects.filter(is_active=True)
    candidates = Candidate.objects.filter(is_active=True)
    offers = RecruitmentOffer.objects.filter(is_active=True) if _recruitment_offer_table_ready() else RecruitmentOffer.objects.none()
    source_rows = []
    for value, label in Candidate.SOURCE_CHOICES:
        source_rows.append({
            'label': label,
            'count': candidates.filter(source=value).count(),
        })
    status_rows = []
    for value, label in Candidate.STATUS_CHOICES:
        status_rows.append({
            'label': label,
            'count': candidates.filter(current_status=value).count(),
        })

    now = timezone.localtime()
    month_starts = []
    for offset in range(5, -1, -1):
        month = now.month - offset
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        month_starts.append(now.replace(
            year=year, month=month, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        ))
    month_counts = (
        candidates.filter(created_at__gte=month_starts[0])
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(count=Count('pk'))
    )
    month_count_map = {
        row['month'].strftime('%Y-%m'): row['count']
        for row in month_counts if row.get('month')
    }
    month_rows = [
        {'label': month.strftime('%b'), 'count': month_count_map.get(month.strftime('%Y-%m'), 0)}
        for month in month_starts
    ]

    return render(request, 'recruitment_reports.html', {
        'job_count': jobs.count(),
        'candidate_count': candidates.count(),
        'selected_count': candidates.filter(current_status='selected').count(),
        'rejected_count': candidates.filter(current_status='rejected').count(),
        'offer_sent_count': offers.filter(status='sent').count(),
        'offer_accepted_count': offers.filter(status='accepted').count(),
        'joined_count': candidates.filter(current_status='joined').count(),
        'source_rows': source_rows,
        'status_rows': status_rows,
        'month_rows': month_rows,
        'offer_rows': [
            {'label': label, 'count': offers.filter(status=value).count()}
            for value, label in RecruitmentOffer.STATUS_CHOICES
        ],
    })



def admin_dashboard(request):
    """Render a small admin dashboard with counts for each master model.

    This view is restricted to staff users via @staff_member_required.
    """
    designation_count = Designations.objects.filter(status=True).count()
    department_count = Department.objects.filter(status=True).count()
    allowances_count = Allowances.objects.filter(status=True).count()
    leaves_count = Leaves.objects.filter(status=True).count()
    bank_count = Bank.objects.filter(status=True).count()
    personal_docs_count = PersonalDocumentType.objects.filter(status=True).count()

    # also provide paginated lists so template can render names/details
    # page param keys: designations_page, departments_page, allowances_page, leaves_page, banks_page
    def _paginate(qs, param, per_page=8):
        page = request.GET.get(param, 1)
        paginator = Paginator(qs, per_page)
        try:
            page_obj = paginator.page(page)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)
        return paginator, page_obj

    designations_qs = Designations.objects.filter(status=True).select_related('departments').order_by('designation_name')
    departments_qs = Department.objects.filter(status=True).order_by('department_name')
    allowances_qs = Allowances.objects.filter(status=True).order_by('allowances_name')
    leaves_qs = Leaves.objects.filter(status=True).order_by('leaves_name')
    banks_qs = Bank.objects.filter(status=True).order_by('bank_name')
    personal_docs_qs = PersonalDocumentType.objects.filter(status=True).order_by('name')

    # show fewer rows per page in admin lists to avoid long tables and extra scrollbars
    designations_paginator, designations_page = _paginate(designations_qs, 'designations_page', per_page=5)
    departments_paginator, departments_page = _paginate(departments_qs, 'departments_page', per_page=5)
    allowances_paginator, allowances_page = _paginate(allowances_qs, 'allowances_page', per_page=5)
    leaves_paginator, leaves_page = _paginate(leaves_qs, 'leaves_page', per_page=5)
    banks_paginator, banks_page = _paginate(banks_qs, 'banks_page', per_page=5)
    personal_docs_paginator, personal_docs_page = _paginate(personal_docs_qs, 'personal_docs_page', per_page=5)

    context = {
        'designation_count': designation_count,
        'department_count': department_count,
        'allowances_count': allowances_count,
        'leaves_count': leaves_count,
        'bank_count': bank_count,
        'personal_docs_count': personal_docs_count,
        'designations_page': designations_page,
        'designations_paginator': designations_paginator,
        'departments_page': departments_page,
        'departments_paginator': departments_paginator,
        'allowances_page': allowances_page,
        'allowances_paginator': allowances_paginator,
        'leaves_page': leaves_page,
        'leaves_paginator': leaves_paginator,
        'banks_page': banks_page,
        'banks_paginator': banks_paginator,
        'personal_docs_page': personal_docs_page,
        'personal_docs_paginator': personal_docs_paginator
    }
    return render(request, 'admin.html', context)


def employee_edit(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    from .permissions import check_employee_access

    # Require view access to see edit page; require Edit to POST
    if request.method == 'GET' and not check_employee_access(request.user, 'View'):
        return redirect_with_company('/')
    if request.method == 'POST' and not check_employee_access(request.user, 'Edit'):
        is_ajax = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'
        )
        if is_ajax:
            return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)
        return redirect_with_company('/')

    departments = Department.objects.all()
    designations = Designations.objects.all()

    if request.method == 'POST':
        try:
            # === Basic personal updates ===
            employee.salutation = request.POST.get('salutation', employee.salutation)
            dob_in = request.POST.get('date_of_birth')
            if dob_in:
                employee.dob = dob_in

            employee.first_name = request.POST.get('first_name', employee.first_name)
            employee.last_name = request.POST.get('last_name', employee.last_name)
            employee.email = request.POST.get('email_id', employee.email)

            incoming_phone = request.POST.get('phone', employee.phone)
            if incoming_phone:
                incoming_phone = format_phone_db(incoming_phone, max_total=16)
            employee.phone = incoming_phone or employee.phone

            dept_id = request.POST.get('department')
            desig_id = request.POST.get('designation')
            employee.department = Department.objects.get(id=dept_id) if dept_id else None
            employee.designation = Designations.objects.get(id=desig_id) if desig_id else None

            # === Address / personal details ===
            employee.address = request.POST.get('address', employee.address)
            employee.permanent_address = request.POST.get('permanent_address', employee.permanent_address)
            employee.marital_status = request.POST.get('marital_status', employee.marital_status)
            employee.gender = request.POST.get('gender', employee.gender)

            # === Bank details ===
            bank_id = request.POST.get('bankname')
            employee.bank_name = Bank.objects.get(id=bank_id) if bank_id else None
            employee.account_number = request.POST.get('account_number', employee.account_number)
            employee.ifsc_code = request.POST.get('ifsc_code', employee.ifsc_code)

            # === Job details ===
            employee.job_category = request.POST.get('job_category', employee.job_category)
            employee.shift = _shift_from_post(request)
            joining_date = request.POST.get('joining_date')
            if joining_date:
                employee.joining_date = joining_date
            confirm_date = request.POST.get('confirm_date')
            if confirm_date:
                employee.confirm_date = confirm_date
            employee.notice_period = request.POST.get('notice_period', employee.notice_period)

            # === Salary / CTC ===
            ctc_val = request.POST.get('ctc')
            if ctc_val:
                try:
                    employee.current_salary = ctc_val
                except Exception:
                    try:
                        employee.ctc = ctc_val
                    except Exception:
                        pass
            employee.salary_mode = request.POST.get('salary_mode', employee.salary_mode)

            # === Helper to encode uploaded file to base64 ===
            import base64

            def file_to_data_url(f):
                if not f:
                    return None
                content = f.read()
                content_type = getattr(f, 'content_type', None) or 'application/octet-stream'
                b64 = base64.b64encode(content).decode('ascii')
                return f"data:{content_type};base64,{b64}"

            # === Passbook upload ===
            bank_upload = request.FILES.get('passbook_photo')
            if bank_upload:
                employee.bank_upload_base64 = file_to_data_url(bank_upload)

            # === Main photo upload ===
            photo = request.FILES.get('photo')
            if photo:
                employee.photo_base64 = file_to_data_url(photo)

            # === Resume upload ===
            upload_resume = request.FILES.get('resume')
            if upload_resume:
                try:
                    employee.upload_resume_base64 = file_to_data_url(upload_resume)
                except Exception:
                    pass

            # === Existing personal documents ===
            employee_docs = employee.personaldocuments.all()
            doc_type_ids = [doc.document_type.id for doc in employee_docs]
            personal_doc_types = PersonalDocumentType.objects.filter(
                models.Q(status=True) | models.Q(id__in=doc_type_ids)
            )

            # === ManyToMany: allowances & leaves ===
            try:
                allowance_ids = request.POST.getlist('allowances')
                if allowance_ids:
                    employee.allowances.set(allowance_ids)
                leave_ids = request.POST.getlist('leaves')
                if leave_ids:
                    employee.leaves.set(leave_ids)
            except Exception:
                pass

            employee.save()

            # === Personal Documents ===
            existing_doc_ids = request.POST.getlist('personal_document_id[]')
            doc_types = request.POST.getlist('personal_document_type[]')
            doc_numbers = request.POST.getlist('personal_document_number[]')
            doc_expiries = request.POST.getlist('personal_document_expiry[]')
            new_files = request.FILES.getlist('personal_document_file[]')
            keep_ids = []
            for i in range(len(doc_types)):
                    doc_id = existing_doc_ids[i] if i < len(existing_doc_ids) else None
                    doc_type_id = doc_types[i]
                    doc_number = doc_numbers[i]
                    doc_expiry = doc_expiries[i] if i < len(doc_expiries) else None

                    if doc_id:
                        file_field_name = f"personal_document_file_{doc_id}"
                        doc_file = request.FILES.get(file_field_name)
                    else:
                        idx = i - len(existing_doc_ids)
                        doc_file = new_files[idx] if 0 <= idx < len(new_files) else None

                    if doc_id:
                        # Update existing document
                        doc = EmployeePersonalDocument.objects.get(id=doc_id, employee=employee)
                        doc.document_type_id = doc_type_id
                        doc.document_number = doc_number
                        doc.expiry_date = doc_expiry if doc_expiry else None
                        if doc_file:
                            doc.document_file_base64 = file_to_data_url(doc_file)
                            doc.document_file_name = doc_file.name
                        doc.save()
                        keep_ids.append(doc.id)
                    else:
                        # Create new document
                        if doc_number.strip() or doc_file:
                            new_doc = EmployeePersonalDocument.objects.create(
                                employee=employee,
                                document_type_id=doc_type_id,
                                document_number=doc_number,
                                expiry_date=doc_expiry if doc_expiry else None,
                                document_file_base64=file_to_data_url(doc_file) if doc_file else None,
                                document_file_name=doc_file.name if doc_file else None,
                            )
                            keep_ids.append(new_doc.id)

                # ✅ DELETE any docs that were NOT resubmitted
            EmployeePersonalDocument.objects.filter(employee=employee).exclude(id__in=keep_ids).delete()


            # === Education (recreate all) ===
            try:
                qualifications = request.POST.getlist('qualification[]')
                boards = request.POST.getlist('board_university[]')
                streams = request.POST.getlist('stream[]')
                years_join = request.POST.getlist('year_of_joining[]')
                years_pass = request.POST.getlist('year_of_passing[]')

                EmployeeEducation.objects.filter(employee=employee).delete()
                for i in range(len(qualifications)):
                    q = qualifications[i].strip() if i < len(qualifications) else ''
                    if q:
                        EmployeeEducation.objects.create(
                            employee=employee,
                            qualification=q,
                            board_university=boards[i] if i < len(boards) else '',
                            stream=streams[i] if i < len(streams) else '',
                            year_of_joining=years_join[i] if i < len(years_join) else None,
                            year_of_passing=years_pass[i] if i < len(years_pass) else None,
                        )
            except Exception as edu_e:
                with open('error_debug.log', 'a', encoding='utf-8') as fh:
                    fh.write(f"\n---- EDUCATION UPDATE ERROR {datetime.utcnow().isoformat()} UTC ----\n{edu_e}\n")

            # === Experiences (recreate all) ===
            try:
                roles = request.POST.getlist('role[]')
                company_names = request.POST.getlist('company_name[]')
                ctcs = request.POST.getlist('ctc_exp[]')
                durations = request.POST.getlist('duration_months[]')

                EmployeeExperience.objects.filter(employee=employee).delete()
                max_len = max(len(roles), len(company_names), len(ctcs), len(durations))
                for i in range(max_len):
                    r = roles[i] if i < len(roles) else ''
                    c = company_names[i] if i < len(company_names) else ''
                    t = ctcs[i] if i < len(ctcs) else ''
                    d = durations[i] if i < len(durations) else ''
                    if any([r.strip(), c.strip(), t.strip(), d.strip()]):
                        EmployeeExperience.objects.create(
                            employee=employee,
                            role=r,
                            company_name=c,
                            ctc=t or None,
                            duration_months=d or None,
                        )
            except Exception as exp_e:
                with open('error_debug.log', 'a', encoding='utf-8') as fh:
                    fh.write(f"\n---- EXPERIENCE UPDATE ERROR {datetime.utcnow().isoformat()} UTC ----\n{exp_e}\n")

            # === Response (AJAX or normal) ===
            is_ajax = (
                request.headers.get('x-requested-with') == 'XMLHttpRequest'
                or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'
            )

            from django.urls import reverse
            from Lyraerp.utils.redirect_utils import get_company_redirect_url
            from django.shortcuts import redirect

            if is_ajax:
                return JsonResponse({
                    'status': 'success',
                    'message': 'Employee updated successfully!',
                    'redirect': get_company_redirect_url(request, 'employees_list')
                })

            success_msg = 'Employee updated successfully!'
            base = get_company_redirect_url(request, 'employees_list')
            return redirect(base + '?success=' + success_msg)

        except Exception as e:
            import traceback as _tb
            with open('error_debug.log', 'a', encoding='utf-8') as fh:
                fh.write(f"\n---- EMPLOYEE_EDIT ERROR {datetime.utcnow().isoformat()} UTC ----\n{e}\n")
                _tb.print_exc(file=fh)

            is_ajax = (
                request.headers.get('x-requested-with') == 'XMLHttpRequest'
                or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'
            )
            if is_ajax:
                return JsonResponse({'status': 'error', 'message': str(e)})
            raise

    # === Prefill for GET ===
    allowances = Allowances.objects.filter(status=True)
    leaves = Leaves.objects.filter(status=True)
    banks = Bank.objects.filter(status=True)

    educations = EmployeeEducation.objects.filter(employee=employee)
    experiences = EmployeeExperience.objects.filter(employee=employee)
    # Define personal_doc_types for GET requests too
    employee_docs = employee.personaldocuments.all()
    doc_type_ids = [doc.document_type.id for doc in employee_docs]
    personal_doc_types = PersonalDocumentType.objects.filter(
        models.Q(status=True) | models.Q(id__in=doc_type_ids)
    )

    shifts = Shift.objects.filter(models.Q(status=True) | models.Q(pk=employee.shift_id))

    return render(request, 'employee_edit.html', {
        'shifts': shifts,
        'employee': employee,
        'departments': departments,
        'designations': designations,
        'allowances': allowances,
        'leaves': leaves,
        'banks': banks,
        'educations': educations,
        'experiences': experiences,
        'employee_docs': employee_docs,
        'personal_doc_types': personal_doc_types,
        'editing': True,
    })


def employee_delete(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    # Deletion requires Delete permission
    if request.method == 'POST':
        if not has_permission(request.user, 'HR', 'Delete'):
            return redirect_with_company('/')
        employee.delete()
        return redirect_with_company('hr_dashboard')
    # If not POST, redirect back
    return redirect_with_company('hr_dashboard')



# Employee status go 0 and delete from frontend stay in backend
def employee_deactivate(request, pk):
    """Mark employee as inactive (status=False). Returns JSON for AJAX calls."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'POST required'}, status=405)
    try:
        from .permissions import check_employee_access
        # Check if user has Delete permission for employees
        if not check_employee_access(request.user, 'Delete'):
            return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)

        employee = get_object_or_404(Employee, pk=pk)
        employee.status = False
        employee.save(update_fields=['status'])
        return JsonResponse({'status': 'success', 'message': 'Employee deactivated'})
    except Exception as e:
        traceback.print_exc()
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)



def employees_list(request):
    """Dedicated employees listing page (paginated)."""
    employees_qs = Employee.objects.filter(status = True).order_by('id')
    page = request.GET.get('page', 1)
    paginator = Paginator(employees_qs, 10)
    try:
        employees_page = paginator.page(page)
    except PageNotAnInteger:
        employees_page = paginator.page(1)
    except EmptyPage:
        employees_page = paginator.page(paginator.num_pages)

    return render(request, 'employees_list.html', {
        'employees_page': employees_page,
        'paginator': paginator,
    })


from django.views.decorators.http import require_POST
from department.models import Department
from designation.models import Designations
from bank.models import Bank
from allowances.models import Allowances
from leaves.models import Leaves


# CHANGE: AJAX endpoints to allow inline creation of Department and Designation
# These endpoints are consumed by the employee register form via JavaScript.
# - `ajax_create_department`: accepts POST 'name', creates a Department and
#   returns JSON {status,id,name}. Expects CSRF token and uses request.user
#   as created_by when available.
@require_POST
def ajax_create_department(request):
    """Create a department via AJAX. Expects POST 'name'. Returns JSON {status,id,name}.
    CSRF token required. The view sets created_by if user is authenticated.
    """
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Department name is required.'}, status=400)
    try:
        # create department (minimal fields). Validation/duplication checks
        # can be added here in future (e.g., return 409 if name exists).
        dept = Department.objects.create(department_name=name, created_by=request.user if request.user.is_authenticated else None)
        return JsonResponse({'status': 'success', 'id': dept.id, 'name': dept.department_name})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


# CHANGE: create designation for a given department via AJAX
# - Expects POST 'name' and 'department_id'
# - Returns JSON {status,id,name}
@require_POST
def ajax_create_designation(request):
    """Create a designation via AJAX. Expects POST 'name' and 'department_id'.
    Returns JSON {status,id,name}.
    """
    name = request.POST.get('name', '').strip()
    dept_id = request.POST.get('department_id')
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Designation name is required.'}, status=400)
    if not dept_id:
        return JsonResponse({'status': 'error', 'message': 'Department is required.'}, status=400)
    try:
        dept = Department.objects.get(id=dept_id)
    except Department.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Department not found.'}, status=404)
    try:
        # create designation; future improvements: check duplicate names
        desig = Designations.objects.create(departments=dept, designation_name=name, created_by=request.user if request.user.is_authenticated else None)
        return JsonResponse({'status': 'success', 'id': desig.id, 'name': desig.designation_name})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


# Simple AJAX endpoints for Bank, Allowances and Leaves so the employee register
# page can create these inline using the same modal UI.
@require_POST
def ajax_create_bank(request):
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Bank name is required.'}, status=400)
    try:
        bank = Bank.objects.create(bank_name=name, created_by=request.user if request.user.is_authenticated else None)
        return JsonResponse({'status': 'success', 'id': bank.id, 'name': bank.bank_name})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@require_POST
def ajax_create_allowance(request):
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Allowance name is required.'}, status=400)
    try:
        a = Allowances.objects.create(allowances_name=name, created_by=request.user if request.user.is_authenticated else None)
        return JsonResponse({'status': 'success', 'id': a.id, 'name': a.allowances_name})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@require_POST
def ajax_create_leave(request):
    name = request.POST.get('name', '').strip()
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Leave name is required.'}, status=400)
    try:
        l = Leaves.objects.create(leaves_name=name, created_by=request.user if request.user.is_authenticated else None)
        return JsonResponse({'status': 'success', 'id': l.id, 'name': l.leaves_name})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@require_POST
def ajax_create_personal_document_type(request):
    try:
        name = request.POST.get('name', '').strip()
        example_format = request.POST.get('format', '').strip()

        if not name or not example_format:
            return JsonResponse({'status': 'error', 'message': 'Name or format required'}, status=400)

        # Ensure new document type is active by default
        doc_type, created = PersonalDocumentType.objects.get_or_create(
            name=name,
            defaults={'example_format': example_format, 'status': True}
        )

        return JsonResponse({
            'status': 'success',
            'id': doc_type.id,
            'name': doc_type.name,
            'format': doc_type.example_format
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)



def admin_view(request):
    return render(request, 'admin.html')


from django.views.decorators.http import require_GET


@require_GET
def load_designations(request):
    """AJAX endpoint: GET /ajax/load-designations/?department_id=ID
    Returns JSON array of designations for the given department.
    Used by employee_register.js to populate the designation select.
    """
    dept_id = request.GET.get('department_id')
    if not dept_id:
        return JsonResponse([], safe=False)
    try:
        ds = Designations.objects.filter(departments_id=dept_id, status=True).order_by('designation_name')
        data = [{'id': d.id, 'designation_name': d.designation_name} for d in ds]
        return JsonResponse(data, safe=False)
    except Exception:
        return JsonResponse([], safe=False)



from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from django.conf import settings
from email_config.utils import send_email
from sms_config.utils import send_sms
from .models import HRNotification, EmployeePersonalDocument
from email_config.models import EmailConfiguration
from django.core.mail import send_mail as django_send_mail
from django.core.mail import get_connection
from django.shortcuts import get_object_or_404
from email_templates.models import EmailTemplateStyle
from email_templates.utils import replace_placeholders
from company.models import Company
from company.utils import get_company_logo_base64

# Constants
REMINDER_INTERVAL_DAYS = 10
EXPIRY_LOOKAHEAD_DAYS = 90  # ~ 3 months

def _reset_due_notifications():
    now = timezone.now()
    # Only use select_for_update when a DB transaction is active. Some DB
    # backends and configurations (or calls outside an atomic block) raise
    # when select_for_update() is used without a transaction.
    # Avoid using select_for_update() here to support DBs/configurations that
    # disallow row-level locking or raise when transactions are not supported.
    due = HRNotification.objects.all()

    reset_ids = []

    for n in due:
        if n.should_reset_method("email"):
            n.reset_method_if_due("email")
            reset_ids.append(f"{n.id}-email")
        if n.should_reset_method("sms"):
            n.reset_method_if_due("sms")
            reset_ids.append(f"{n.id}-sms")

    return reset_ids

def _ensure_expiring_doc_notifications():
    today = timezone.now().date()
    three_months = today + timedelta(days=EXPIRY_LOOKAHEAD_DAYS)
    now = timezone.now()

    expiring_docs = EmployeePersonalDocument.objects.filter(
        expiry_date__lte=three_months,
        employee__status=True
    ).select_related("employee","document_type")

    expiring_doc_ids = [doc.id for doc in expiring_docs]
    created_count = 0

    for doc in expiring_docs:
        exists = HRNotification.objects.filter(document=doc).exists()
        if not exists:
            HRNotification.objects.create(
                document=doc,
                employee=doc.employee,
                method_email=False,
                method_sms=False,
                last_sent_email=None,
                last_sent_sms=None,
                is_read=False,
                created_at=now
            )
            created_count += 1

    removed_count, _ = HRNotification.objects.exclude(document_id__in=expiring_doc_ids).delete()
    return created_count, removed_count

def all_notifications(request):
    with transaction.atomic():
        reset_ids = _reset_due_notifications()
        created_count, removed_count = _ensure_expiring_doc_notifications()

    notifications = HRNotification.objects.filter(
        is_read=False, document__employee__status=True
    ).select_related("document__employee","document__document_type").order_by("-created_at")

    today = timezone.now().date()
    for n in notifications:
        expiry_date = getattr(n.document, "expiry_date", None)
        n.is_expired = expiry_date and expiry_date < today

    return render(request, "all_notifications.html", {
        "notifications": notifications,
        "filter_type": request.GET.get("filter","all"),
        "reset_count": len(reset_ids),
        "created_count": created_count
    })

@csrf_exempt
def mark_notification_read(request, notif_id):
    if request.method=="POST":
        notif = HRNotification.objects.filter(document_id=notif_id).first()
        if notif:
            notif.is_read = True
            notif.save()
            return JsonResponse({"success": True})
        return JsonResponse({"success": False, "message":"No notification found."})
    return JsonResponse({"success": False}, status=400)

@csrf_exempt
def mark_notification_unread(request, notif_id):
    if request.method=="POST":
        notif = HRNotification.objects.filter(document_id=notif_id).first()
        if notif:
            notif.is_read = False
            notif.save()
            return JsonResponse({"success": True})
        return JsonResponse({"success": False, "message":"No notification found."})
    return JsonResponse({"success": False}, status=400)

@csrf_exempt
def mark_all_as_read(request):
    if request.method!="POST":
        return JsonResponse({"success": False, "message":"Invalid request method"}, status=400)
    HRNotification.objects.filter(is_read=False).update(is_read=True)
    return JsonResponse({"success": True, "message":"All notifications cleared."})

@csrf_exempt
def delete_notification(request, notif_id):
    if request.method!="POST":
        return JsonResponse({"success": False, "message":"Invalid request method"}, status=400)
    notif = HRNotification.objects.filter(id=notif_id).first()
    if not notif:
        return JsonResponse({"success": False, "message":"Notification not found"}, status=404)
    notif.is_read=True
    notif.save(update_fields=["is_read"])
    return JsonResponse({"success": True, "message":"Notification dismissed."})

import traceback
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db import transaction
from django.core.mail import send_mail as django_send_mail
from django.core.mail import get_connection

from email_templates.models import EmailTemplateStyle
from email_templates.constants import HARDCODED_EMAIL_TEMPLATES
from sms_templates.models import SMSTemplateOption
from sms_templates.constants import HARDCODED_SMS_TEMPLATES
from sms_config.models import SMSConfiguration
from email_config.models import EmailConfiguration


# ===================================================================
#                    HELPER FUNCTIONS
# ===================================================================
def get_default_email_template(template_name):
    """
    Get the default email template for a given template_name.
    First checks DB for default (custom or hardcoded marker),
    then falls back to first hardcoded template if no default is set.
    
    Returns dict with 'subject' and 'body' or None.
    """
    # Check if there's a default in the database
    db_template = EmailTemplateStyle.objects.filter(
        template_name=template_name,
        is_default=True,
        status=True
    ).first()
    
    if db_template:
        return {
            "subject": db_template.subject,
            "body": db_template.body
        }
    
    # Fall back to first hardcoded template
    hardcoded_templates = HARDCODED_EMAIL_TEMPLATES.get(template_name, [])
    if hardcoded_templates:
        return {
            "subject": hardcoded_templates[0]["subject"],
            "body": hardcoded_templates[0]["body"]
        }
    
    return None


def get_default_sms_template(template_name):
    """
    Get the default SMS template for a given template_name.
    First checks DB for default (custom or hardcoded marker),
    then falls back to first hardcoded template if no default is set.
    
    Returns the SMS content string or None.
    """
    # Check if there's a default in the database
    db_template = SMSTemplateOption.objects.filter(
        template_name=template_name,
        is_default=True,
        status=True
    ).first()
    
    if db_template:
        return db_template.content
    
    # Fall back to first hardcoded template
    hardcoded_templates = HARDCODED_SMS_TEMPLATES.get(template_name, [])
    if hardcoded_templates:
        return hardcoded_templates[0]["content"]
    
    return None


# ===================================================================
#                    SEND MESSAGE FUNCTION
# ===================================================================
@csrf_exempt
def send_message(request, document_id):
    try:
        if request.method != "POST":
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get("method")
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(","))
        for m in methods:
            if m not in ("email", "sms"):
                return JsonResponse({"success": False, "message": f"Invalid method '{m}'"}, status=400)

        doc = EmployeePersonalDocument.objects.select_related("employee", "document_type").filter(id=document_id).first()
        if not doc:
            return JsonResponse({"success": False, "message": "Document not found"}, status=404)

        employee = getattr(doc, "employee", None)
        if not employee or not getattr(employee, "status", True):
            return JsonResponse({"success": False, "message": "Employee inactive."}, status=400)

        now = timezone.now()

        with transaction.atomic():
            # Try to use a SELECT ... FOR UPDATE to avoid races when available.
            # Some DB backends or setups may not allow select_for_update(), so
            # fall back to a non-locking get_or_create if it fails.
            # Use non-locking get_or_create to avoid DB-specific transaction
            # requirements that may raise if transactions are not available.
            notif, created = HRNotification.objects.get_or_create(
                document=doc,
                defaults={
                    "employee": employee,
                    "method_email": False,
                    "method_sms": False,
                    "last_sent_email": None,
                    "last_sent_sms": None,
                    "is_read": False,
                    "created_at": now
                }
            )

            messages_sent = []
            doc_name = doc.document_type.name if getattr(doc, "document_type", None) else "Document"
            expiry_date = getattr(doc, "expiry_date", None)
            expiry_str = expiry_date.strftime("%d %B %Y") if expiry_date else "N/A"
            today = timezone.now().date()

            # Determine template type
            if expiry_date and expiry_date < today:
                template_name = "Document Expired Reminder"
            else:
                template_name = "Document Expiry Reminder"

            # Get company details
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)

            
            # ===== CONTEXT FOR PLACEHOLDER REPLACE =====
            context = {
                "employee": f"{employee.first_name} {employee.last_name}",
                "doc": doc_name,
                "date": expiry_str,
                "company": company_name,
                "logo": company_logo,
            }

            # ================= EMAIL SEND BLOCK =================
            if "email" in methods:
                # ✅ Get default email template (supports both hardcoded and custom)
                email_template = get_default_email_template(template_name)

                if not email_template:
                    return JsonResponse({
                        "success": False,
                        "message": f"No email template found for '{template_name}'"
                    }, status=400)

                # Apply placeholder replacement for EMAIL
                subject = replace_placeholders(email_template["subject"], context)
                message_html = replace_placeholders(email_template["body"], context)

                email = getattr(employee, "email", None)
                if not email:
                    return JsonResponse({"success": False, "message": "Employee email not found."}, status=400)

                email_config = EmailConfiguration.objects.filter(
                    usage_types__icontains=template_name,  # MultiSelectField
                    status=True
                ).first()

                if not email_config:
                    return JsonResponse({"success": False, "message": "⚠️ Email configuration not found. Please go to System Settings → Email Configuration and add one for this template."}, status=400)
                    

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    django_send_mail(
                        subject,
                        "",  # text body blank, using html only
                        email_config.default_from_email or email_config.host_user,
                        [email],
                        connection=connection,
                        html_message=message_html
                    )

                    notif.method_email = True
                    notif.last_sent_email = now
                    notif.email_content = message_html
                    notif.is_read = False
                    notif.save(update_fields=["method_email", "last_sent_email", "email_content", "is_read"])

                    messages_sent.append(f"Email sent to {email} successfully!")

                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)

            # ================= SMS SEND BLOCK =================
            if "sms" in methods:
                phone = getattr(employee, "phone", None)
                if not phone:
                    return JsonResponse({"success": False, "message": "Employee phone not found."}, status=400)

                # ✅ Get default SMS template (supports both hardcoded and custom)
                sms_content = get_default_sms_template(template_name)

                if not sms_content:
                    return JsonResponse({
                        "success": False,
                        "message": f"No SMS template found for '{template_name}'"
                    }, status=400)

                # Replace placeholders for SMS
                sms_message = replace_placeholders(sms_content, context)

                # ✅ Get SMS Configuration
                sms_config = SMSConfiguration.objects.filter(
                    usage_types__icontains=template_name,  # MultiSelectField
                    status=True
                ).first()

                if not sms_config:
                    return JsonResponse({
                        "success": False,
                        "message": f"No SMS configuration assigned to '{template_name}'"
                    }, status=400)

                try:
                    # ✅ Pass the sms_config object to send_sms
                    success, resp = send_sms(phone, sms_message, sms_config)
                    
                    if not success:
                        return JsonResponse({"success": False, "message": f"SMS failed: {resp}"}, status=500)

                    notif.method_sms = True
                    notif.last_sent_sms = now
                    notif.sms_content = sms_message
                    notif.is_read = False
                    notif.save(update_fields=["method_sms", "last_sent_sms", "sms_content", "is_read"])

                    messages_sent.append(f"SMS sent to {phone} successfully!")

                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"SMS failed: {str(e)}"}, status=500)

        return JsonResponse({"success": True, "message": " | ".join(messages_sent)})

    except Exception as e:
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Internal server error: {str(e)}"}, status=500)



def refresh_notifications(request):
    due_notifications = HRNotification.objects.all()
    reset_count=0
    for notif in due_notifications:
        if notif.should_reset_method("email"):
            notif.reset_method_if_due("email")
            reset_count+=1
        if notif.should_reset_method("sms"):
            notif.reset_method_if_due("sms")
            reset_count+=1
    return JsonResponse({"success": True, "reset": reset_count})

