from django.shortcuts import render, get_object_or_404, redirect
from Lyraerp.utils.redirect_utils import redirect_with_company
from django.contrib.auth.models import User
from sales.models import QuotePrefix
from django.views.decorators.http import require_http_methods
from django.contrib import messages
from company.models import Company
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

# Setup checklist helpers
from Tax.models import Tax
from email_config.models import EmailConfiguration
from sms_config.models import SMSConfiguration
from django.db.models import Sum, Q
from decimal import Decimal
from django.utils import timezone
import json
import re
from datetime import date

# Domain models used for dashboard metrics
from expenses.models import Expense
from HR.models import Employee, Candidate
from crm.models import Lead, FollowUp
from sales.models import SalesInvoice, SalesQuotation, SalesOrder, SalesDeliveryNote
from Purchase.models import Bill

# Permission checking
from user.utils import has_permission
from crm.permissions import (
    can_view_leads, can_view_opportunities, can_view_presales, can_view_followups,
    check_dashboard_access
)
from sales.permissions import (
    can_view_quotations, can_view_orders, can_view_invoices, can_view_payments_received,
    can_view_returns, can_view_delivery, can_view_performa_invoice, can_view_sales_dashboard
)
from Purchase.permissions import (
    can_view_purchase_orders, can_view_purchase_bills, can_view_purchase_payments,
    can_view_purchase_returns, can_view_purchase_delivery, can_view_purchase_expenses,
    can_view_purchase_dashboard
)
from chart_of_accounts.permissions import (
    can_view_trial_balance, can_view_profit_loss, can_view_balance_sheet, can_view_cash_flow,
    can_view_chart, can_view_journal
)


def _count_latest_revisions(numbers):
    """Count documents the way the list pages show them: INV-1, INV-1-R1, INV-1-R2 is one row."""
    return len({re.sub(r'-R\d+$', '', n or '') for n in numbers})


def Index(request):
    company_id = request.session.get('company_id')
    company_db = request.session.get('company_db')
    company = None
    
    if not company_db and company_id:
        company = Company.objects.using('default').filter(pk=company_id).first()
        company_db = company.db_name if company and company.db_name else 'default'
    if not company_db:
        company_db = 'default'
    
    # Fetch company if not already retrieved
    if not company and company_id:
        company = Company.objects.using('default').filter(pk=company_id).first()

    # Compute setup status in the active company DB.
    has_taxes = Tax.objects.using(company_db).exists()
    has_email_config = EmailConfiguration.objects.using(company_db).exists()
    has_sms_config = SMSConfiguration.objects.using(company_db).exists()

    # ═══════════════════════════════════════════════════════
    # SETUP CHECKLIST PERMISSION CHECKS
    # ═══════════════════════════════════════════════════════
    has_tax_perm = any(has_permission(request.user, mod, 'View') for mod in ['Tax', 'Masters Tax'])
    has_email_perm = any(has_permission(request.user, mod, 'View') for mod in ['Email Configuration', 'System Settings Email Configuration', 'System settings Email Configuration'])
    has_sms_perm = any(has_permission(request.user, mod, 'View') for mod in ['SMS Configuration', 'System Settings SMS Configuration', 'System settings SMS Configuration'])

    # Force-show once immediately after registration completes, then
    # continue showing on logins while setup items are missing.
    show_after_registration = bool(request.session.pop('registration_complete', False))
    
    # Check if user has ANY setup permission
    has_any_setup_perm = has_tax_perm or has_email_perm or has_sms_perm
    
    # Only show setup checklist if user has at least one setup permission AND some items need configuration
    show_setup = request.user.is_authenticated and has_any_setup_perm and (
        show_after_registration or
        (not has_taxes and has_tax_perm) or
        (not has_email_config and has_email_perm) or
        (not has_sms_config and has_sms_perm)
    )

    context = {
        'show_setup_checklist': show_setup,
        'has_taxes': has_taxes,
        'has_email_config': has_email_config,
        'has_sms_config': has_sms_config,
        'has_tax_perm': has_tax_perm,
        'has_email_perm': has_email_perm,
        'has_sms_perm': has_sms_perm,
        'company': company,
    }

    # --- Additional dashboard metrics ---
    try:
        # Only active employees (Employee.status is a BooleanField, True = active)
        employees = Employee.objects.using(company_db).filter(status=True)
        staff = employees.count()
    except Exception:
        staff = 0

    try:
        # Sum expense totals in company base currency.
        # Exclude soft-deleted expenses (status=False), same as the Expenses list page.
        total_expenses = 0.0
        for e in Expense.objects.using(company_db).filter(status=True):
            base = getattr(e, 'total_amount_base', None)
            if base:
                total_expenses += float(base)
            else:
                # Older rows without a stored base amount: convert using the saved FX rate
                fx = getattr(e, 'fx_rate_to_base', None) or 1
                total_expenses += float(getattr(e, 'total_amount', 0) or 0) * float(fx)
    except Exception:
        total_expenses = 0

    try:
        leads_qs = Lead.objects.using(company_db)
        hot_leads = leads_qs.filter(priority='Hot').count()
        warm_leads = leads_qs.filter(priority='Warm').count()
        cold_leads = leads_qs.filter(priority='Cold').count()
        total_leads = leads_qs.count()
    except Exception:
        hot_leads = warm_leads = cold_leads = total_leads = 0

    try:
        # Paid / open follow payment_status (same rule as the Sales dashboard),
        # not the document status (Draft/Open/Closed). Drafts are not counted.
        sinv_qs = SalesInvoice.objects.using(company_db).exclude(status='Draft')
        paid_q = Q(payment_status__name__iexact='Paid')
        # Count revision chains once so the card matches the invoice list page
        paid_invoices = _count_latest_revisions(sinv_qs.filter(paid_q).values_list('inv_number', flat=True))
        open_invoices = _count_latest_revisions(sinv_qs.exclude(paid_q).values_list('inv_number', flat=True))
        total_revenue = sinv_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    except Exception:
        open_invoices = paid_invoices = 0
        total_revenue = 0

    try:
        # Pending = not fully paid, matching the Purchase dashboard rule
        bill_qs = Bill.objects.using(company_db).exclude(status='Draft')
        # List page keeps only the latest revision of each bill, then filters on payment status
        latest = {}
        for num, ps in bill_qs.values_list('bill_number', 'payment_status__name'):
            m = re.match(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$', num or '')
            base, rev = (m.group('base'), int(m.group('rev') or 0)) if m else (num, 0)
            if base not in latest or rev > latest[base][0]:
                latest[base] = (rev, (ps or '').strip().lower())
        pending_bills = sum(1 for _, ps in latest.values() if ps != 'paid')
        total_payable = bill_qs.aggregate(total=Sum('total_amount'))['total'] or 0
    except Exception:
        pending_bills = 0
        total_payable = 0

    try:
        # Recruitment funnel is cumulative: a candidate who has joined was also
        # selected, sent an offer and accepted it, so count every later stage.
        cand_qs = Candidate.objects.using(company_db)
        candidate_count = cand_qs.count()
        selected_count = cand_qs.filter(current_status__in=[
            'selected', 'offer_sent', 'offer_accepted', 'offer_declined', 'offer_cancelled', 'joined'
        ]).count()
        offer_sent_count = cand_qs.filter(current_status__in=[
            'offer_sent', 'offer_accepted', 'offer_declined', 'offer_cancelled', 'joined'
        ]).count()
        offer_accepted_count = cand_qs.filter(current_status__in=['offer_accepted', 'joined']).count()
        joined_count = cand_qs.filter(current_status='joined').count()
    except Exception:
        candidate_count = selected_count = offer_sent_count = offer_accepted_count = joined_count = 0

    try:
        total_quotations = SalesQuotation.objects.using(company_db).count()
        total_sales_orders = SalesOrder.objects.using(company_db).count()
        total_deliveries = SalesDeliveryNote.objects.using(company_db).count()
    except Exception:
        total_quotations = total_sales_orders = total_deliveries = 0

    # Home dashboard: invoiced sales per calendar month (current year)
    # (Uses the same "date__gte/date__lt" approach as the Sales module dashboard chart.)
    sales_chart_year = timezone.localdate().year
    sales_month_totals = [0.0] * 12
    try:
        y = sales_chart_year
        for month_idx in range(1, 13):
            start = date(y, month_idx, 1)
            next_start = date(y + 1, 1, 1) if month_idx == 12 else date(y, month_idx + 1, 1)
            month_total = (
                SalesInvoice.objects.using(company_db)
                .filter(date__gte=start, date__lt=next_start)
                .aggregate(t=Sum('total_amount'))['t']
            )
            sales_month_totals[month_idx - 1] = float(month_total or 0)
    except Exception:
        sales_month_totals = [0.0] * 12

    # Today's pending follow-ups/tasks
    sometasks = []
    try:
        today = timezone.now().date()
        fqs = FollowUp.objects.using(company_db).filter(followup_date__date=today, status='Pending')[:10]
        for f in fqs:
            sometasks.append({
                'Task': f.description,
                'to_whom': (f.assigned_to.get_full_name() if f.assigned_to else '') or (getattr(f, 'assigned_employee', None) and getattr(f.assigned_employee, 'first_name', '') ) or '',
                'dateandtime': f.followup_date,
            })
    except Exception:
        sometasks = []

    # ═══════════════════════════════════════════════════════
    # MODULE PERMISSION CHECKS - only show cards for modules user has access to
    # ═══════════════════════════════════════════════════════
    
    # For Sales: check parent 'Sales' + all sales submodules
    sales_module_aliases = [
        'Sales', 'Sales Quotation', 'Sales Invoice', 'Sales Order', 'Sales Orders',
        'Sales Delivery', 'Sales Delivery Note', 'Sales Return', 'Sales Performa Invoice',
        'Sales Payments Received', 'Sales Dashboard'
    ]
    has_sales_perm = any(has_permission(request.user, mod, 'View') for mod in sales_module_aliases)
    
    # For Purchase: check parent + submodules  
    purchase_module_aliases = [
        'Purchase', 'Purchase Order', 'Purchase Orders', 'Purchase Bill', 'Bills',
        'Purchase Delivery', 'Purchase Delivery Note', 'Purchase Return', 
        'Purchase Payments Made', 'Purchase Dashboard'
    ]
    has_purchase_perm = any(has_permission(request.user, mod, 'View') for mod in purchase_module_aliases)
    
    # For HR: check parent + submodules (support singular/plural alias variants)
    hr_module_aliases = ['HR', 'HR Dashboard', 'HR Employee', 'HR Employees', 'HR Recruitment', 'HR masters', 'HR Masters']
    has_hr_perm = any(has_permission(request.user, mod, 'View') for mod in hr_module_aliases)
    
    # For CRM: check submodules ONLY (not generic 'CRM' parent permission)
    # User must have at least one CRM submodule permission to access CRM section
    crm_module_aliases = [
        'CRM Dashboard', 'CRM Leads', 'CRM Lead', 'Leads',
        'CRM Opportunities', 'CRM Opportunity', 'Opportunities',
        'CRM Presales', 'CRM Presale', 'Pre-sales', 'Pre Sales',
        'CRM Follow Ups', 'CRM FollowUps', 'Follow Ups', 'FollowUps'
    ]
    has_crm_perm = any(has_permission(request.user, mod, 'View') for mod in crm_module_aliases)
    
    # For Accounts: check only actual Accounts submodules (not Reports P&L/Balance Sheet)
    accounts_module_aliases = [
        'Accounts', 'Chart of Accounts', 'Journal Entry', 'Journal Entries',
        'Accounts Dashboard'
    ]
    has_accounts_perm = any(has_permission(request.user, mod, 'View') for mod in accounts_module_aliases)
    
    # For Reports: check if user has access to ANY report (read individual report permissions)
    # This will be computed after checking individual reports below
    
    # For Masters: check parent + submodules
    masters_module_aliases = [
        'Masters', 'Masters Dashboard',
        'Masters Users', 'Masters User Roles',
        'Masters Items', 'Masters Brand', 'Masters Category', 'Masters Type', 'Masters Unit',
        'Masters Customer', 'Masters Vendor', 'Masters vendor',
        'Masters Warehouse', 'Masters Stock', 'Masters Payment Terms'
    ]
    has_masters_perm = any(has_permission(request.user, mod, 'View') for mod in masters_module_aliases)
    
    # For System Settings: consider all submodules, not only Company
    system_settings_aliases = [
        'System Settings', 'System settings', 'System',
        'Company',
        'Email Config', 'Email Configuration', 'System Settings Email Configuration', 'System settings Email Configuration',
        'SMS Config', 'SMS Configuration', 'System Settings SMS Configuration', 'System settings SMS Configuration',
        'Email Templates', 'SMS Templates',
        'System Settings Period Lock', 'System settings Period Lock', 'Period Lock', 'Period Management',
    ]
    has_system_settings_perm = any(has_permission(request.user, mod, 'View') for mod in system_settings_aliases)

    # ═══════════════════════════════════════════════════════
    # SALES SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_sales_dashboard_perm = can_view_sales_dashboard(request.user)
    has_sales_quotation_perm = can_view_quotations(request.user)
    has_sales_order_perm = can_view_orders(request.user)
    has_sales_invoice_perm = can_view_invoices(request.user)
    has_sales_payment_perm = can_view_payments_received(request.user)
    has_sales_return_perm = can_view_returns(request.user)
    has_sales_delivery_perm = can_view_delivery(request.user)
    has_sales_performa_invoice_perm = can_view_performa_invoice(request.user)
    
    # ═══════════════════════════════════════════════════════
    # PURCHASE SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_purchase_dashboard_perm = can_view_purchase_dashboard(request.user)
    has_purchase_order_perm = can_view_purchase_orders(request.user)
    has_purchase_bill_perm = can_view_purchase_bills(request.user)
    has_purchase_payment_perm = can_view_purchase_payments(request.user)
    has_purchase_expense_perm = can_view_purchase_expenses(request.user)
    has_purchase_delivery_perm = can_view_purchase_delivery(request.user)
    has_purchase_return_perm = can_view_purchase_returns(request.user)

    # ═══════════════════════════════════════════════════════
    # CRM SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    can_view_crm_leads = can_view_leads(request.user)
    can_view_crm_opportunities = can_view_opportunities(request.user)
    can_view_crm_presales = can_view_presales(request.user)
    can_view_crm_followups = can_view_followups(request.user)
    # Dashboard is only accessible if user has explicit 'CRM Dashboard' permission
    can_view_crm = check_dashboard_access(request.user, 'View')

    # ═══════════════════════════════════════════════════════
    # HR SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_hr_dashboard_perm = has_permission(request.user, 'HR Dashboard', 'View')
    has_hr_employees_perm = (
        has_permission(request.user, 'HR Employee', 'View')
        or has_permission(request.user, 'HR Employees', 'View')
    )
    has_hr_candidates_perm = has_permission(request.user, 'HR Recruitment', 'View')
    has_hr_offers_perm = has_permission(request.user, 'HR Recruitment', 'View')
    has_hr_notices_perm = has_permission(request.user, 'HR notificationss', 'View')
    has_hr_masters_perm = has_permission(request.user, 'HR masters', 'View') or has_permission(request.user, 'HR Masters', 'View')

    # ═══════════════════════════════════════════════════════
    # ACCOUNTS SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_accounts_journal_perm = can_view_journal(request.user)
    has_accounts_chart_perm = can_view_chart(request.user)

    # ═══════════════════════════════════════════════════════
    # REPORTS PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_reports_trial_balance_perm = can_view_trial_balance(request.user)
    has_reports_pl_perm = can_view_profit_loss(request.user)
    has_reports_balance_sheet_perm = can_view_balance_sheet(request.user)
    has_reports_cash_flow_perm = can_view_cash_flow(request.user)

    # Build dynamic reports description based on available permissions
    available_reports = []
    if has_reports_trial_balance_perm:
        available_reports.append('Trial Balance')
    if has_reports_pl_perm:
        available_reports.append('P&L')
    if has_reports_balance_sheet_perm:
        available_reports.append('Balance Sheet')
    if has_reports_cash_flow_perm:
        available_reports.append('Cash Flow')
    reports_description = ' · '.join(available_reports) if available_reports else 'No reports available'
    
    # User has reports access if they have access to at least one report
    has_reports_perm = bool(available_reports)

    # ═══════════════════════════════════════════════════════
    # MASTERS SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_masters_taxes_perm = has_permission(request.user, 'Masters', 'View') or has_permission(request.user, 'Masters Taxes', 'View')
    has_masters_dashboard_perm = has_permission(request.user, 'Masters Dashboard', 'View') or has_permission(request.user, 'Dashboard', 'View')
    has_masters_users_perm = has_permission(request.user, 'Masters Users', 'View')
    has_masters_user_roles_perm = has_permission(request.user, 'Masters User Roles', 'View')
    has_masters_items_perm = has_permission(request.user, 'Masters Items', 'View')
    has_masters_brands_perm = has_permission(request.user, 'Masters Brand', 'View')
    has_masters_categories_perm = has_permission(request.user, 'Masters Category', 'View')
    has_masters_types_perm = has_permission(request.user, 'Masters Type', 'View')
    has_masters_units_perm = has_permission(request.user, 'Masters Unit', 'View')
    has_masters_customers_perm = has_permission(request.user, 'Masters Customer', 'View')
    has_masters_vendors_perm = has_permission(request.user, 'Masters Vendor', 'View') or has_permission(request.user, 'Masters vendor', 'View')
    has_masters_warehouses_perm = has_permission(request.user, 'Masters Warehouse', 'View')
    has_masters_stock_perm = has_permission(request.user, 'Masters Stock', 'View')
    has_masters_payterms_perm = has_permission(request.user, 'Masters Payment Terms', 'View')

    # ═══════════════════════════════════════════════════════
    # SETTINGS SUBMODULE PERMISSION CHECKS for modal items
    # ═══════════════════════════════════════════════════════
    has_settings_company_perm = has_permission(request.user, 'Company', 'View')
    has_settings_email_config_perm = (
        has_permission(request.user, 'Email Config', 'View')
        or has_permission(request.user, 'Email Configuration', 'View')
        or has_permission(request.user, 'System Settings Email Configuration', 'View')
        or has_permission(request.user, 'System settings Email Configuration', 'View')
    )
    has_settings_sms_config_perm = (
        has_permission(request.user, 'SMS Config', 'View')
        or has_permission(request.user, 'SMS Configuration', 'View')
        or has_permission(request.user, 'System Settings SMS Configuration', 'View')
        or has_permission(request.user, 'System settings SMS Configuration', 'View')
    )
    has_settings_email_templates_perm = has_permission(request.user, 'Email Templates', 'View')
    has_settings_sms_templates_perm = has_permission(request.user, 'SMS Templates', 'View')
    has_settings_period_lock_perm = (
        has_permission(request.user, 'System Settings Period Lock', 'View')
        or has_permission(request.user, 'System settings Period Lock', 'View')
        or has_permission(request.user, 'Period Lock', 'View')
        or has_permission(request.user, 'Period Management', 'View')
    )
    has_settings_general_perm = (
        has_permission(request.user, 'System Settings', 'View')
        or has_permission(request.user, 'System settings', 'View')
        or has_permission(request.user, 'System', 'View')
    )

    # update context with computed metrics used by the template
    context.update({
        'total_expenses': total_expenses,
        'staff': staff,
        'hot_leads': hot_leads,
        'warm_leads': warm_leads,
        'cold_leads': cold_leads,
        'total_leads': total_leads,
        'open_invoices': open_invoices,
        'paid_invoices': paid_invoices,
        'pending_bills': pending_bills,
        'candidate_count': candidate_count,
        'selected_count': selected_count,
        'offer_sent_count': offer_sent_count,
        'offer_accepted_count': offer_accepted_count,
        'joined_count': joined_count,
        'total_quotations': total_quotations,
        'total_sales_orders': total_sales_orders,
        'total_deliveries': total_deliveries,
        'total_revenue': total_revenue,
        'total_payable': total_payable,
        'sales_chart_year': sales_chart_year,
        'sales_per_month_json': json.dumps(sales_month_totals),
        'sometasks': sometasks,
        # Module permission checks
        'has_sales_perm': has_sales_perm,
        'has_purchase_perm': has_purchase_perm,
        'has_hr_perm': has_hr_perm,
        'has_crm_perm': has_crm_perm,
        'has_accounts_perm': has_accounts_perm,
        'has_reports_perm': has_reports_perm,
        'has_masters_perm': has_masters_perm,
        'has_system_settings_perm': has_system_settings_perm,
        # Sales submodule permission checks
        'has_sales_dashboard_perm': has_sales_dashboard_perm,
        'has_sales_quotation_perm': has_sales_quotation_perm,
        'has_sales_order_perm': has_sales_order_perm,
        'has_sales_invoice_perm': has_sales_invoice_perm,
        'has_sales_payment_perm': has_sales_payment_perm,
        'has_sales_return_perm': has_sales_return_perm,
        'has_sales_delivery_perm': has_sales_delivery_perm,
        'has_sales_performa_invoice_perm': has_sales_performa_invoice_perm,
        # Purchase submodule permission checks
        'has_purchase_dashboard_perm': has_purchase_dashboard_perm,
        'has_purchase_order_perm': has_purchase_order_perm,
        'has_purchase_bill_perm': has_purchase_bill_perm,
        'has_purchase_payment_perm': has_purchase_payment_perm,
        'has_purchase_expense_perm': has_purchase_expense_perm,
        'has_purchase_delivery_perm': has_purchase_delivery_perm,
        'has_purchase_return_perm': has_purchase_return_perm,
        # CRM submodule permission checks
        'can_view_crm': can_view_crm,
        'can_view_crm_leads': can_view_crm_leads,
        'can_view_crm_opportunities': can_view_crm_opportunities,
        'can_view_crm_presales': can_view_crm_presales,
        'can_view_crm_followups': can_view_crm_followups,
        # HR submodule permission checks
        'has_hr_dashboard_perm': has_hr_dashboard_perm,
        'has_hr_employees_perm': has_hr_employees_perm,
        'has_hr_candidates_perm': has_hr_candidates_perm,
        'has_hr_offers_perm': has_hr_offers_perm,
        'has_hr_notices_perm': has_hr_notices_perm,
        'has_hr_masters_perm': has_hr_masters_perm,
        # Accounts submodule permission checks
        'has_accounts_journal_perm': has_accounts_journal_perm,
        'has_accounts_chart_perm': has_accounts_chart_perm,
        # Reports permission checks
        'has_reports_trial_balance_perm': has_reports_trial_balance_perm,
        'has_reports_pl_perm': has_reports_pl_perm,
        'has_reports_balance_sheet_perm': has_reports_balance_sheet_perm,
        'has_reports_cash_flow_perm': has_reports_cash_flow_perm,
        'reports_description': reports_description,
        # Masters submodule permission checks
        'has_masters_taxes_perm': has_masters_taxes_perm,
        'has_masters_dashboard_perm': has_masters_dashboard_perm,
        'has_masters_users_perm': has_masters_users_perm,
        'has_masters_user_roles_perm': has_masters_user_roles_perm,
        'has_masters_items_perm': has_masters_items_perm,
        'has_masters_brands_perm': has_masters_brands_perm,
        'has_masters_categories_perm': has_masters_categories_perm,
        'has_masters_types_perm': has_masters_types_perm,
        'has_masters_units_perm': has_masters_units_perm,
        'has_masters_customers_perm': has_masters_customers_perm,
        'has_masters_vendors_perm': has_masters_vendors_perm,
        'has_masters_warehouses_perm': has_masters_warehouses_perm,
        'has_masters_stock_perm': has_masters_stock_perm,
        'has_masters_payterms_perm': has_masters_payterms_perm,
        # Settings submodule permission checks
        'has_settings_company_perm': has_settings_company_perm,
        'has_settings_email_config_perm': has_settings_email_config_perm,
        'has_settings_sms_config_perm': has_settings_sms_config_perm,
        'has_settings_email_templates_perm': has_settings_email_templates_perm,
        'has_settings_sms_templates_perm': has_settings_sms_templates_perm,
        'has_settings_period_lock_perm': has_settings_period_lock_perm,
        'has_settings_general_perm': has_settings_general_perm,
    })

    return render(request, 'website/dashboard.html', context)


def ShowStaff(request):
    data = User.objects.all()

    context = {'data': data}
    return render(request, 'website/showStaff.html', context)

def master_list(request):
    return render(request, 'website/master_list.html')

def ViewStaffData(request, id):
    data = User.objects.get(id=id)

    context = {'data': data}
    return render(request, 'website/viewstaffdata.html', context)

def prefix_sq(request):
    quote_prefix = get_object_or_404(QuotePrefix)  # fetch single row without id

    if request.method == "POST":
        new_prefix = request.POST.get('prefix', '').strip()
        if new_prefix:
            quote_prefix.prefix = new_prefix
            quote_prefix.save()
            messages.success(request, f"Prefix updated to '{new_prefix}'.")
            return redirect_with_company('prefix_sq')  # redirect to same view without id
        else:
            messages.error(request, "Prefix cannot be empty.")

    return render(request, 'website/update_quote_prefix.html', {'quote_prefix': quote_prefix})


# ════════════════════════════════════════════════════════════════
# Dashboard Layout Persistence — Server-side storage per user
# ════════════════════════════════════════════════════════════════

@login_required
@require_http_methods(["GET"])
def get_dashboard_layout(request):
    """Fetch saved dashboard layout for the current user."""
    from .models import DashboardLayout
    obj, _ = DashboardLayout.objects.get_or_create(user=request.user)
    try:
        data = json.loads(obj.layout_json)
    except Exception:
        data = {}
    return JsonResponse({'layout': data})


@login_required
@require_http_methods(["POST"])
def save_dashboard_layout(request):
    """Save dashboard layout for the current user."""
    from .models import DashboardLayout
    try:
        body = json.loads(request.body)
        layout = body.get('layout', {})
        obj, _ = DashboardLayout.objects.get_or_create(user=request.user)
        obj.layout_json = json.dumps(layout)
        obj.save()
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)