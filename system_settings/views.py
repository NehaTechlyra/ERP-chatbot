from django.shortcuts import render, redirect, get_object_or_404
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_redirect_url
from django.http import JsonResponse, HttpResponse, FileResponse
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.db import transaction
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.conf import settings
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import csv
from collections import Counter

from email_config.models import EmailConfiguration
from sms_config.models import SMSConfiguration, GSMModemConfig, MobileModemConfig, APIConfig
from company.models import Company
from company_settings.models import CompanyBankAccount, Location, LicenseKey
from currencies.models import Currency
from currencies.services import base_currency_code_from_country, ensure_base_currency_row
from currencies.utils import get_currency_name, get_currency_symbol
from email_templates.models import EmailTemplateStyle
from sms_templates.models import SMSTemplateOption

# E-Way Bill imports
try:
    from sales.models import EWayBillCredential
    from sales.forms import EWayBillCredentialForm
    EWAY_BILL_AVAILABLE = True
except ImportError:
    EWAY_BILL_AVAILABLE = False
    EWayBillCredential = None
    EWayBillCredentialForm = None

from email_templates.constants import TEMPLATE_NAME_CHOICES, HARDCODED_EMAIL_TEMPLATES
from sms_templates.constants import SMS_TEMPLATE_NAME_CHOICES, HARDCODED_SMS_TEMPLATES
from company.constants import SYSTEM_MODULES

from .models import FiscalYear, PeriodLock, PeriodLockExemption, BackupSchedule
from .forms import FiscalYearForm
from sales.models import QuotePrefix, OrderPrefix as SalesOrderPrefix, InvoicePrefix
from Purchase.models import OrderPrefix as PurchaseOrderPrefix, BillPrefix


def _has_trial_module_access(company, license_obj):
    has_valid_license = (
        license_obj
        and license_obj.is_active
        and not license_obj.is_expired()
    )
    return bool(
        company.trial_active
        and not company.is_trial_expired
        and not has_valid_license
    )


def _repair_company_currency_display(company):
    base_currency = Currency.objects.filter(company=company, is_base=True).order_by("id").first()
    base_code = ((base_currency.code if base_currency else "") or getattr(company, "base_currency", None) or "").strip().upper()[:3]
    if not base_code:
        base_code = base_currency_code_from_country(getattr(company, "country", None))
    if base_code and (getattr(company, "base_currency", None) or "").strip().upper()[:3] != base_code:
        Company.objects.using("default").filter(pk=company.pk).update(base_currency=base_code)
        if getattr(company._state, "db", None) and company._state.db != "default":
            Company.objects.using(company._state.db).filter(pk=company.pk).update(base_currency=base_code)
        company.base_currency = base_code

    db_alias = getattr(company._state, "db", None)
    if db_alias and db_alias != "default" and base_code and not base_currency:
        ensure_base_currency_row(company, db_alias, base_code)

    base_currency = Currency.objects.filter(company=company, is_base=True).order_by("id").first()
    if base_currency and not (company.base_currency or "").strip():
        company.base_currency = base_currency.code
        company.save(update_fields=["base_currency"])

    for currency in Currency.objects.filter(company=company, is_active=True):
        code = (currency.code or "").strip().upper()[:3]
        if not code:
            continue
        updates = {}
        if not currency.symbol or currency.symbol.strip().upper() == code:
            updates["symbol"] = get_currency_symbol(code) or code
        if not currency.name or currency.name.strip().upper() == code:
            updates["name"] = get_currency_name(code) or code
        if updates:
            Currency.objects.filter(pk=currency.pk).update(**updates)
from .backup_utils import create_database_backup, get_backup_storage_path

from .permissions import (
    can_view_period_lock, can_create_period_lock,
    
    can_edit_period_lock, can_delete_period_lock,
    can_view_other, can_edit_other
)

# Import permission functions from other modules
from email_config.permissions import (
    can_view_email_config, can_create_email_config,
    can_edit_email_config, can_delete_email_config
)
from sms_config.permissions import (
    can_view_sms_config, can_create_sms_config,
    can_edit_sms_config, can_delete_sms_config
)
from email_templates.permissions import (
    can_view_email_templates, can_create_email_templates,
    can_edit_email_templates, can_delete_email_templates
)
from sms_templates.permissions import (
    can_view_sms_templates, can_create_sms_templates,
    can_edit_sms_templates, can_delete_sms_templates
)

@never_cache
def settings_page(request):
    """
    SETTINGS PAGE – Loads ALL configuration sections:
    - Company Details
    - License Key
    - Company Bank Accounts
    - Company Locations
    - Email Configurations
    - SMS Configurations
    - Email Templates
    - SMS Templates
    - Module Access Control (LICENSE)
    """

    # --------------------------------------------------------
    # 1️⃣ DEFAULT (ACTIVE) EMAIL & SMS CONFIG
    # --------------------------------------------------------
    email_obj = EmailConfiguration.objects.filter(
        is_default=True, status=True
    ).first()

    sms_obj = SMSConfiguration.objects.filter(
        is_default=True, status=True
    ).first()

    # --------------------------------------------------------
    # 2️⃣ ALL EMAIL & SMS CONFIG LISTS
    # --------------------------------------------------------
    email_list = EmailConfiguration.objects.filter(status=True).order_by("id")
    sms_list = SMSConfiguration.objects.filter(status=True).order_by("id")

    # --------------------------------------------------------
    # 3️⃣ COMPANY + LICENSE + MODULE ACCESS (🔥 FIXED)
    # --------------------------------------------------------
    companies_with_data = []

    for company in Company.objects.all().order_by("id"):
        _repair_company_currency_display(company)
        license_obj = LicenseKey.objects.filter(company=company).first()
        trial_module_access = _has_trial_module_access(company, license_obj)

        modules = []
        enabled_modules_count = 0  # ✅ FIX: correct enabled count

        for module in SYSTEM_MODULES:
            if trial_module_access:
                has_access = True
            elif license_obj:
                has_access = license_obj.module_access.get(
                    module["code"],
                    module["is_core"]
                )
            else:
                has_access = module["is_core"]

            if has_access:
                enabled_modules_count += 1

            modules.append({
                "id": module["id"],
                "name": module["name"],
                "code": module["code"],
                "icon": module["icon"],          # ✅ icon preserved
                "description": module.get("description", ""),
                "is_core": module["is_core"],
                "has_access": has_access,
            })

        company_data = {
            "company": company,
            "has_base_currency": Currency.objects.filter(company=company, is_base=True).exists(),
            "license": license_obj,
            "trial_module_access": trial_module_access,
            "modules": modules,
            "enabled_modules_count": enabled_modules_count,  # ✅ PASS TO TEMPLATE
            "bank_accounts": CompanyBankAccount.objects.filter(
                company=company, status=True
            ),
            "locations": Location.objects.filter(
                company=company, status=True
            ).select_related("location_type"),
        }

        companies_with_data.append(company_data)

    # --------------------------------------------------------
    # 4️⃣ EMAIL TEMPLATE GROUPING (✅ FIXED - MERGE HARDCODED + CUSTOM)
    # --------------------------------------------------------
    template_groups = {}

    for value, label in TEMPLATE_NAME_CHOICES:
        # Start with hardcoded templates
        hardcoded_templates = HARDCODED_EMAIL_TEMPLATES.get(value, [])
        
        # Convert hardcoded templates to a unified format
        all_templates = []
        
        # Check if any hardcoded template is set as default in DB
        default_hardcoded_style = None
        default_db_record = EmailTemplateStyle.objects.filter(
            template_name=value,
            is_default=True,
            is_hardcoded=True,
            status=True
        ).first()
        
        if default_db_record:
            default_hardcoded_style = default_db_record.style
        
        # Add hardcoded templates
        for template in hardcoded_templates:
            all_templates.append({
                "id": None,  # No database ID for hardcoded
                "style": template["style"],
                "subject": template["subject"],
                "body": template["body"],
                "is_hardcoded": True,
                "is_default": template["style"] == default_hardcoded_style,
                "status": True,
            })
        
        # Get custom templates from database
        custom_templates = EmailTemplateStyle.objects.filter(
            template_name=value,
            is_hardcoded=False,
            status=True
        ).order_by("-is_default", "style")
        
        # Add custom templates
        for template in custom_templates:
            all_templates.append({
                "id": template.id if template.id else None,
                "style": template.style,
                "subject": template.subject,
                "body": template.body,
                "is_hardcoded": False,
                "is_default": template.is_default,
                "status": template.status,
            })
        
        # If no default is set, mark the first template as default
        if all_templates and not any(t["is_default"] for t in all_templates):
            all_templates[0]["is_default"] = True
        
        template_groups[value] = all_templates

    # --------------------------------------------------------
    # 5️⃣ SMS TEMPLATE GROUPING (✅ FIXED - MERGE HARDCODED + CUSTOM)
    # --------------------------------------------------------
    grouped_sms_templates = {}

    for value, label in SMS_TEMPLATE_NAME_CHOICES:
        # Start with hardcoded templates
        hardcoded_templates = HARDCODED_SMS_TEMPLATES.get(value, [])
        
        # Convert hardcoded templates to a unified format
        all_templates = []
        
        # Check if any hardcoded template is set as default in DB
        default_hardcoded_style = None
        default_db_record = SMSTemplateOption.objects.filter(
            template_name=value,
            is_default=True,
            is_hardcoded=True,
            status=True
        ).first()
        
        if default_db_record:
            default_hardcoded_style = default_db_record.style
        
        # Add hardcoded templates
        for template in hardcoded_templates:
            all_templates.append({
                "id": None,  # No database ID for hardcoded
                "style": template["style"],
                "content": template["content"],
                "is_hardcoded": True,
                "is_default": template["style"] == default_hardcoded_style,
                "status": True,
            })
        
        # Get custom templates from database
        custom_templates = SMSTemplateOption.objects.filter(
            template_name=value,
            is_hardcoded=False,
            status=True
        ).order_by("-is_default", "style")
        
        # Add custom templates
        for template in custom_templates:
            all_templates.append({
                "id": template.id,
                "style": template.style,
                "content": template.content,
                "is_hardcoded": False,
                "is_default": template.is_default,
                "status": template.status,
            })
        
        # If no default is set, mark the first template as default
        if all_templates and not any(t["is_default"] for t in all_templates):
            all_templates[0]["is_default"] = True
        
        grouped_sms_templates[value] = all_templates

    # --------------------------------------------------------
    # 6️⃣ SMS MODE CONFIGS
    # --------------------------------------------------------
    all_gsm_modems = GSMModemConfig.objects.all()

    gsm = sms_obj.gsm_configs.all() if sms_obj else []
    mobile = sms_obj.mobile_configs.all() if sms_obj else []
    api = sms_obj.api_configs.all() if sms_obj else []

    # --------------------------------------------------------
    # 7️⃣ PREFIX SETTINGS
    # --------------------------------------------------------
    db_alias = getattr(request, 'company_db', 'default')
    company_code = getattr(request, 'company_code', None) or getattr(request, 'company_db', 'default')
    backup_files = _get_backup_file_rows(company_code)
    sales_quote_prefix = QuotePrefix.objects.using(db_alias).first()
    sales_order_prefix = SalesOrderPrefix.objects.using(db_alias).first()
    sales_invoice_prefix = InvoicePrefix.objects.using(db_alias).first()
    purchase_order_prefix = PurchaseOrderPrefix.objects.using(db_alias).first()
    purchase_bill_prefix = BillPrefix.objects.using(db_alias).first()
    # Load company-specific flags (tenant DB)
    try:
        current_company = None
        if getattr(request, 'company_id', None):
            current_company = Company.objects.using('default').filter(
                pk=request.company_id,
            ).first()
        if current_company is None and getattr(request, 'company_code', None):
            current_company = Company.objects.using('default').filter(
                company_code=request.company_code,
            ).first()
        if current_company is None:
            current_company = Company.objects.using('default').first()
        auto_load_cash_customer = bool(getattr(current_company, 'auto_load_cash_customer', False)) if current_company else False
        show_base_transaction_summary = bool(getattr(current_company, 'show_base_transaction_summary', True)) if current_company else True
        sales_rounding_method = (getattr(current_company, 'sales_rounding_method', 'none') or 'none') if current_company else 'none'
        sales_rounding_increment = getattr(current_company, 'sales_rounding_increment', 0) if current_company else 0
        show_sales_rounding_adjustment = bool(getattr(current_company, 'show_sales_rounding_adjustment', True)) if current_company else True
    except Exception:
        auto_load_cash_customer = False
        show_base_transaction_summary = True
        sales_rounding_method = 'none'
        sales_rounding_increment = 0
        show_sales_rounding_adjustment = True

    # --------------------------------------------------------
    # 8️⃣ BACKUP SETTINGS
    # --------------------------------------------------------
    backup_schedule = BackupSchedule.objects.using(db_alias).first()
    backup_storage_path = get_backup_storage_path(company_code)

    # --------------------------------------------------------
    # 9️⃣ ACTIVE TAB
    # --------------------------------------------------------
    active_tab = request.GET.get("tab", "details")

    # --------------------------------------------------------
    # 🔟 E-WAY BILL SETTINGS
    # --------------------------------------------------------
    eway_bill_form = None
    eway_bill_cred = None
    eway_bill_mock_mode = False
    eway_bill_active_mode = None
    eway_bill_gsp_provider = None
    eway_bill_gsp_client_id = None
    eway_bill_gsp_gstin = None
    eway_bill_gsp_username = None
    eway_bill_gsp_sandbox = True
    eway_bill_gsp_configured = False
    eway_bill_direct_configured = False

    if EWAY_BILL_AVAILABLE and EWayBillCredential and EWayBillCredentialForm:
        try:
            import logging
            logger = logging.getLogger(__name__)
            
            # Get the current company
            eway_bill_company = Company.objects.filter(status=True).first() or Company.objects.first()
            
            if eway_bill_company:
                # Get or create E-Way Bill credentials
                eway_bill_cred, _ = EWayBillCredential.objects.get_or_create(
                    company=eway_bill_company,
                    defaults={"gstin": getattr(eway_bill_company, "tax_id", "") or ""},
                )
                
                # Get settings
                eway_bill_gsp_cfg = getattr(settings, "EWAY_BILL_GSP", {}) or {}
                eway_bill_mock_mode = getattr(settings, "EWAY_BILL_MOCK", False)
                eway_bill_gsp_provider = eway_bill_gsp_cfg.get("PROVIDER", "masters_india")
                eway_bill_gsp_client_id = eway_bill_gsp_cfg.get("CLIENT_ID", "")
                eway_bill_gsp_gstin = eway_bill_gsp_cfg.get("GSTIN", "")
                eway_bill_gsp_username = eway_bill_gsp_cfg.get("USERNAME", "")
                eway_bill_gsp_sandbox = eway_bill_gsp_cfg.get("SANDBOX", True)
                
                # Determine active API mode
                eway_bill_gsp_configured = bool(eway_bill_gsp_client_id and eway_bill_gsp_client_id.strip())
                
                if eway_bill_mock_mode:
                    eway_bill_active_mode = "mock"
                elif eway_bill_gsp_configured:
                    eway_bill_active_mode = "gsp"
                else:
                    eway_bill_active_mode = "direct"
                
                # Create form (GET or POST)
                if request.method == "POST":
                    # Check if this POST is from E-Way Bill form (contains E-Way Bill specific fields)
                    if "active_tab" in request.POST and ("gstin" in request.POST or "username" in request.POST or "is_sandbox" in request.POST):
                        # Set active tab to Other Settings for E-Way Bill form
                        active_tab = "other_settings_tab"
                        
                        eway_bill_form = EWayBillCredentialForm(request.POST, instance=eway_bill_cred, mock_mode=eway_bill_mock_mode)
                        if eway_bill_form.is_valid():
                            try:
                                obj = eway_bill_form.save(commit=False)
                                raw_pass = eway_bill_form.cleaned_data.get("password_raw", "").strip()
                                
                                # Handle password - ONLY set if user provided one
                                if raw_pass:
                                    obj.set_password(raw_pass)
                                # If password_raw is empty, DON'T modify the password field
                                # This preserves the existing password
                                
                                obj.save()
                                msg = "✓ E-Way Bill settings saved (Mock mode enabled)." if eway_bill_mock_mode else "✓ E-Way Bill credentials saved."
                                messages.success(request, msg)
                                
                                
                            except Exception as exc:
                                logger.error(f"Error saving E-Way Bill credentials: {exc}")
                                messages.error(request, f"Failed to save E-Way Bill credentials: {str(exc)}")
                        else:
                            # Log form errors
                            for field, errors in eway_bill_form.errors.items():
                                for error in errors:
                                    messages.error(request, f"E-Way Bill {field}: {error}")
                            logger.error(f"E-Way Bill form validation errors: {eway_bill_form.errors}")
                    else:
                        eway_bill_form = EWayBillCredentialForm(instance=eway_bill_cred, mock_mode=eway_bill_mock_mode)
                else:
                    eway_bill_form = EWayBillCredentialForm(instance=eway_bill_cred, mock_mode=eway_bill_mock_mode)
                
                eway_bill_direct_configured = bool(eway_bill_cred and eway_bill_cred.gstin)
        except Exception as exc:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error loading E-Way Bill settings: {exc}")

    # --------------------------------------------------------
    # 🔟 CONTEXT DICTIONARY

    # --------------------------------------------------------
    # 10️⃣ ACTIVE SMS MODE
    # --------------------------------------------------------
    active_sms_mode = sms_obj.mode if sms_obj else "gsm"

    if request.method == "POST" and "mode" in request.POST:
        active_sms_mode = request.POST.get("mode", "gsm")

    # --------------------------------------------------------
    # Company-level settings update (per-company stock management toggle)
    # Expect POST keys: `stock_management_company_id` and optional `stock_management_on_delivery`
    # --------------------------------------------------------
    try:
        if request.method == "POST" and request.POST.get('stock_management_company_id'):
            cid = request.POST.get('stock_management_company_id')
            val = request.POST.get('stock_management_on_delivery') == 'on'
            # Update master DB Company row
            Company.objects.using('default').filter(pk=cid).update(stock_management_on_delivery=val)
            # If company has company DB, also update that DB record
            try:
                comp = Company.objects.using('default').filter(pk=cid).first()
                if comp and getattr(comp, 'db_name', None):
                    Company.objects.using(comp.db_name).filter(pk=cid).update(stock_management_on_delivery=val)
            except Exception:
                logger.exception('Failed to update company DB stock setting for company id %s', cid)

            messages.success(request, 'Company stock management setting updated.')
            settings_url = get_company_redirect_url(request, 'settings_page')
            return redirect(f"{settings_url}?tab=other_settings_tab")
    except Exception:
        logger.exception('Error processing company stock management POST')

    try:
        if request.method == "POST" and request.POST.get('printer_paper_size') is not None:
            paper_size = (request.POST.get('printer_paper_size') or 'A4').strip()
            if paper_size not in dict(Company.PRINT_PAPER_SIZE_CHOICES):
                paper_size = 'A4'

            # Prefer company identified by request/company_code (URL or middleware)
            company_obj = None
            company_code = getattr(request, 'company_code', None)
            if not company_code and hasattr(request, 'resolver_match'):
                company_code = (request.resolver_match.kwargs or {}).get('company_code')

            if company_code:
                company_obj = Company.objects.using('default').filter(company_code=company_code).first()

            # Fallback to companies_with_data or default first
            if company_obj is None and companies_with_data:
                company_obj = companies_with_data[0].company if hasattr(companies_with_data[0], 'company') else None

            if company_obj is None:
                company_obj = Company.objects.using('default').first()

            if company_obj:
                company_obj.print_paper_size = paper_size
                company_obj.save(update_fields=['print_paper_size'])
                messages.success(request, 'Printer paper size updated.')
                settings_url = get_company_redirect_url(request, 'settings_page')
                return redirect(f"{settings_url}?tab=other_settings_tab")
    except Exception:
        logger.exception('Error processing printer paper size POST')

    # --------------------------------------------------------
    # 11️⃣ CONTEXT
    # --------------------------------------------------------
    context = {
        # Default configs
        "email_obj": email_obj,
        "sms_obj": sms_obj,

        # Lists
        "email_list": email_list,
        "sms_list": sms_list,

        # Company + License + Modules
        "company_list": companies_with_data,

        # Templates
        "template_groups": template_groups,
        "grouped_sms_templates": grouped_sms_templates,

        # Modems
        "gsm": gsm,
        "mobile": mobile,
        "api": api,
        "all_gsm_modems": all_gsm_modems,

        # UI state
        "active_tab": active_tab,
        "active_sms_mode": active_sms_mode,
        "sales_quote_prefix": sales_quote_prefix,
        "sales_order_prefix": sales_order_prefix,
        "sales_invoice_prefix": sales_invoice_prefix,
        "purchase_order_prefix": purchase_order_prefix,
        "purchase_bill_prefix": purchase_bill_prefix,
        "backup_schedule": backup_schedule,
        "backup_storage_path": backup_storage_path,
        "backup_files": backup_files,

        # E-Way Bill settings
        "form": eway_bill_form,
        "cred": eway_bill_cred,
        "mock_mode": eway_bill_mock_mode,
        "active_mode": eway_bill_active_mode,
        "gsp_provider": eway_bill_gsp_provider,
        "gsp_client_id": eway_bill_gsp_client_id,
        "gsp_gstin": eway_bill_gsp_gstin,
        "gsp_username": eway_bill_gsp_username,
        "gsp_sandbox": eway_bill_gsp_sandbox,
        "gsp_configured": eway_bill_gsp_configured,
        "direct_configured": eway_bill_direct_configured,

        # Constants
        "TEMPLATE_NAME_CHOICES": TEMPLATE_NAME_CHOICES,
        "SMS_TEMPLATE_NAME_CHOICES": SMS_TEMPLATE_NAME_CHOICES,

        
        # Permission flags for template
        "can_view_email_config": can_view_email_config(request.user),
        "can_create_email_config": can_create_email_config(request.user),
        "can_view_sms_config": can_view_sms_config(request.user),
        "can_create_sms_config": can_create_sms_config(request.user),
        "can_view_email_templates": can_view_email_templates(request.user),
        "can_create_email_templates": can_create_email_templates(request.user),
        "can_view_sms_templates": can_view_sms_templates(request.user),
        "can_create_sms_templates": can_create_sms_templates(request.user),
        "can_view_period_lock": can_view_period_lock(request.user),
        "can_create_period_lock": can_create_period_lock(request.user),
        "can_view_other": can_view_other(request.user),
        "can_edit_other": can_edit_other(request.user),
        "auto_load_cash_customer": auto_load_cash_customer,
        "show_base_transaction_summary": show_base_transaction_summary,
    }

    # --------------------------------------------------------
    # 🔟 RENDER
    # --------------------------------------------------------
    return render(request, "system_settings/settings.html", context)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# by adarshlockPERIOD LOCKING FEATURE - Views for managing Fiscal Years and Period Locks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@login_required
def period_management(request):
    """
    Period Management Dashboard - Manage Fiscal Years & Period Locks
    Path: /system-settings/periods/
    """
    # Permission check - require view access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_period_lock(request.user)):
            messages.error(request, 'You do not have permission to view Period Lock.')
            return render(request, 'site_blocked.html', {
                'license_info': {'has_license': False},
                'company_code': getattr(request, 'company_code', None),
                'contact_email': 'support@lyra.com'
            }, status=403)
    except Exception:
        messages.error(request, 'You do not have permission to view Period Lock.')
        return render(request, 'site_blocked.html', {
            'license_info': {'has_license': False},
            'company_code': getattr(request, 'company_code', None),
            'contact_email': 'support@lyra.com'
        }, status=403)
    
    # 🔒 Use company database for ERP app data
    db = getattr(request, 'company_db', 'default')
    
    fiscal_years = FiscalYear.objects.using(db).all().order_by('-start_date')
    
    # Get lock history with pagination
    lock_history_all = PeriodLock.objects.using(db).select_related(
        'fiscal_year', 'performed_by'
    ).order_by('-performed_at')
    
    # Paginate lock history (10 items per page)
    paginator = Paginator(lock_history_all, 10)
    page_number = request.GET.get('page', 1)
    
    try:
        lock_history = paginator.page(page_number)
    except PageNotAnInteger:
        lock_history = paginator.page(1)
    except EmptyPage:
        lock_history = paginator.page(paginator.num_pages)
    
    # Get exemptions with related data
    exemptions = PeriodLockExemption.objects.using(db).select_related(
        'fiscal_year', 'user', 'granted_by'
    ).order_by('-granted_at')
    
    # Get all users for the add exemption dropdown
    from django.contrib.auth.models import User
    all_users = User.objects.filter(is_active=True).order_by('first_name', 'last_name')
    
    context = {
        'fiscal_years': fiscal_years,
        'lock_history': lock_history,
        'exemptions': exemptions,
        'all_users': all_users,
        'can_manage': True,
        'active_tab': request.GET.get('tab', 'periods'),
    }
    
    return render(request, 'system_settings/period_management.html', context)


@login_required
@require_http_methods(['GET', 'POST'])
def create_fiscal_year(request):
    """Create a new Fiscal Year"""
    # Permission check - require create access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_period_lock(request.user)):
            messages.error(request, 'You do not have permission to create Fiscal Year.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to create Fiscal Year.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    
    if request.method == 'POST':
        form = FiscalYearForm(request.POST)
        if form.is_valid():
            fiscal_year = form.save(commit=False)
            fiscal_year.created_by = request.user
            fiscal_year.save(using=db)
            messages.success(request, f'✅ Fiscal Year "{fiscal_year.name}" created successfully.')
            return redirect_with_company('period_management')
    else:
        form = FiscalYearForm()
    
    context = {
        'form': form,
        'action': 'Create',
    }
    return render(request, 'system_settings/fiscal_year_form.html', context)


@login_required
@require_http_methods(['GET', 'POST'])
def edit_fiscal_year(request, pk):
    """Edit an existing Fiscal Year"""
    # Permission check - require edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_period_lock(request.user)):
            messages.error(request, 'You do not have permission to edit Fiscal Year.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to edit Fiscal Year.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    
    if request.method == 'POST':
        form = FiscalYearForm(request.POST, instance=fiscal_year)
        if form.is_valid():
            fiscal_year = form.save()
            fiscal_year.save(using=db)
            messages.success(request, f'✅ Fiscal Year "{fiscal_year.name}" updated successfully.')
            return redirect_with_company('period_management')
    else:
        form = FiscalYearForm(instance=fiscal_year)
    
    context = {
        'form': form,
        'action': 'Edit',
        'fiscal_year': fiscal_year,
    }
    return render(request, 'system_settings/fiscal_year_form.html', context)


@login_required
@require_http_methods(['POST'])
@transaction.atomic
def lock_fiscal_year(request, pk):
    """Lock a Fiscal Year (prevent all edits)"""
    # Permission check - require create/edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_period_lock(request.user)):
            messages.error(request, 'You do not have permission to lock Period.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to lock Period.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    reason = request.POST.get('reason', '')
    lock_types = request.POST.getlist('lock_types')
    if not lock_types:
        lock_types = ['all']

    if fiscal_year.is_locked and 'all' in lock_types:
        messages.warning(request, f'Fiscal Year "{fiscal_year.name}" is already fully locked.')
        return redirect_with_company('period_management')

    # Lock the fiscal year / categories
    fiscal_year.lock(user=request.user, categories=lock_types)
    fiscal_year.save(using=db)

    # Create audit log
    PeriodLock.objects.using(db).create(
        fiscal_year=fiscal_year,
        action='lock',
        performed_by=request.user,
        reason=f"{reason} | lock_types: {','.join(lock_types)}",
        categories=','.join(lock_types)
    )
    
    if 'all' in lock_types:
        success_text = f'🔒 Fiscal Year "{fiscal_year.name}" is now LOCKED. All edits are prevented.'
    else:
        success_text = f'🔒 Fiscal Year "{fiscal_year.name}" is now PARTIALLY LOCKED: {", ".join(lock_types)}.'

    messages.success(request, success_text)
    return redirect_with_company('period_management')


@login_required
@require_http_methods(['POST'])
@transaction.atomic
def unlock_fiscal_year(request, pk):
    """Unlock a Fiscal Year (allow edits)"""
    # Permission check - require edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_period_lock(request.user)):
            messages.error(request, 'You do not have permission to unlock Period.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to unlock Period.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    reason = request.POST.get('reason', '')
    unlock_types = request.POST.getlist('lock_types')
    if not unlock_types:
        unlock_types = ['all']

    if not fiscal_year.is_locked and unlock_types == ['all'] and not fiscal_year.locked_categories:
        messages.warning(request, f'Fiscal Year "{fiscal_year.name}" is already unlocked.')
        return redirect_with_company('period_management')
    
    # Unlock the fiscal year / categories
    fiscal_year.unlock(categories=unlock_types)
    # Also change status back to 'active' if fully unlocked
    if unlock_types == ['all'] or 'all' in unlock_types:
        fiscal_year.status = 'active'
    fiscal_year.save(using=db)
    
    # Create audit log
    PeriodLock.objects.using(db).create(
        fiscal_year=fiscal_year,
        action='unlock',
        performed_by=request.user,
        reason=f"{reason} | unlock_types: {','.join(unlock_types)}",
        categories=','.join(unlock_types)
    )
    
    messages.success(
        request,
        f'🔓 Fiscal Year "{fiscal_year.name}" is now UNLOCKED. Editing is allowed.'
    )
    return redirect_with_company('period_management')


@login_required
def check_period_lock(request, date_str):
    """
    AJAX endpoint - Check if a date falls within a locked period
    Returns JSON: {is_locked: bool, fiscal_year: str, message: str}
    """
    db = getattr(request, 'company_db', 'default')
    
    try:
        check_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return JsonResponse({'is_locked': False, 'error': 'Invalid date format'})
    
    # Find fiscal year containing this date
    fiscal_year = FiscalYear.objects.using(db).filter(
        start_date__lte=check_date,
        end_date__gte=check_date
    ).first()
    
    if not fiscal_year:
        return JsonResponse({
            'is_locked': False,
            'fiscal_year': None,
            'message': 'No fiscal year defined for this date'
        })
    
    # Check if period is locked and if user is exempted
    is_locked = fiscal_year.is_locked
    is_exempted = False
    
    if is_locked and request.user.is_authenticated:
        is_exempted = PeriodLockExemption.objects.using(db).filter(
            fiscal_year=fiscal_year,
            user=request.user
        ).exists()
    
    return JsonResponse({
        'is_locked': is_locked,
        'is_exempted': is_exempted,
        'fiscal_year': fiscal_year.name,
        'lock_status': fiscal_year.get_lock_status(),
        'message': (
            f'Period is locked for {fiscal_year.name}. Cannot edit.'
            if (is_locked and not is_exempted)
            else f'Period is open for {fiscal_year.name}. You can edit.'
        )
    })


@login_required
def grant_exemption(request, pk):
    """Grant a user exemption from period locking"""
    # Permission check - require edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_period_lock(request.user)):
            messages.error(request, 'You do not have permission to grant exemptions.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to grant exemptions.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        reason = request.POST.get('reason', '')
        
        try:
            exemption, created = PeriodLockExemption.objects.using(db).get_or_create(
                fiscal_year=fiscal_year,
                user_id=user_id,
                defaults={'reason': reason, 'granted_by': request.user}
            )
            
            if created:
                messages.success(
                    request,
                    f'✅ Exemption granted for {exemption.user.username} in {fiscal_year.name}'
                )
            else:
                messages.info(
                    request,
                    f'User {exemption.user.username} already has exemption in {fiscal_year.name}'
                )
        except Exception as e:
            messages.error(request, f'Error granting exemption: {str(e)}')
    
    # Redirect with tab parameter to show exemptions list
    company_code = getattr(request, 'company_code', None)
    if company_code:
        return redirect(f'/{company_code}/system_settings/periods/?tab=exemptions')
    return redirect_with_company('period_management')


@login_required
@require_http_methods(['POST'])
def delete_exemption(request, pk):
    """Delete a period lock exemption"""
    # Permission check - require delete access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_period_lock(request.user)):
            messages.error(request, 'You do not have permission to delete exemptions.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to delete exemptions.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    
    try:
        exemption = PeriodLockExemption.objects.using(db).get(pk=pk)
        exemption.delete()
        messages.success(request, f'✅ Exemption revoked successfully.')
    except PeriodLockExemption.DoesNotExist:
        messages.error(request, '❌ Exemption not found.')
    except Exception as e:
        messages.error(request, f'❌ Error deleting exemption: {str(e)}')
    
    # Redirect with tab parameter to show exemptions list
    company_code = getattr(request, 'company_code', None)
    if company_code:
        return redirect(f'/{company_code}/system_settings/periods/?tab=exemptions')
    return redirect_with_company('period_management')


@login_required
def period_lock_reports(request):
    """Generate reports for period locking"""
    # Permission check - require view access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_period_lock(request.user)):
            messages.error(request, 'You do not have permission to view Period Lock reports.')
            return render(request, 'site_blocked.html', {
                'license_info': {'has_license': False},
                'company_code': getattr(request, 'company_code', None),
                'contact_email': 'support@lyra.com'
            }, status=403)
    except Exception:
        messages.error(request, 'You do not have permission to view Period Lock reports.')
        return render(request, 'site_blocked.html', {
            'license_info': {'has_license': False},
            'company_code': getattr(request, 'company_code', None),
            'contact_email': 'support@lyra.com'
        }, status=403)
    
    db = getattr(request, 'company_db', 'default')
    
    # Get all lock history
    lock_history = PeriodLock.objects.using(db).select_related(
        'fiscal_year', 'performed_by'
    ).order_by('-performed_at')
    
    # Get all exemptions
    exemptions = PeriodLockExemption.objects.using(db).select_related(
        'fiscal_year', 'user'
    )
    
    # Calculate statistics
    total_locks = lock_history.filter(action='lock').count()
    total_unlocks = lock_history.filter(action='unlock').count()
    total_lock_activities = lock_history.count()
    
    # Calculate percentages
    lock_percentage = 0
    unlock_percentage = 0
    if total_lock_activities > 0:
        lock_percentage = round((total_locks * 100) / total_lock_activities, 1)
        unlock_percentage = round((total_unlocks * 100) / total_lock_activities, 1)
    
    # Count by user
    lock_by_user = {}
    for lock in lock_history:
        user = lock.performed_by.username
        lock_by_user[user] = lock_by_user.get(user, 0) + 1
    lock_by_user = sorted(lock_by_user.items(), key=lambda x: x[1], reverse=True)
    
    # Count by fiscal year
    lock_by_fy = {}
    for lock in lock_history:
        fy = lock.fiscal_year.name
        lock_by_fy[fy] = lock_by_fy.get(fy, 0) + 1
    lock_by_fy = sorted(lock_by_fy.items(), key=lambda x: x[1], reverse=True)

    # Partial lock counts (lock actions with explicit categories)
    partial_locks = lock_history.filter(action='lock').exclude(categories__in=['', 'all']).count()

    # Category totals
    lock_by_category = {}
    for lock in lock_history.filter(action='lock'):
        cats = (lock.categories or 'all').split(',')
        for c in cats:
            c = c.strip()
            if c:
                lock_by_category[c] = lock_by_category.get(c, 0) + 1
    lock_by_category = sorted(lock_by_category.items(), key=lambda x: x[1], reverse=True)
    
    # Exemptions by fiscal year
    exemptions_by_fy = {}
    for ex in exemptions:
        fy = ex.fiscal_year.name
        exemptions_by_fy[fy] = exemptions_by_fy.get(fy, 0) + 1
    exemptions_by_fy = sorted(exemptions_by_fy.items(), key=lambda x: x[1], reverse=True)
    
    # Total exemptions
    total_exemptions = exemptions.count()
    
    # Get all fiscal years
    fiscal_years = FiscalYear.objects.using(db).all().order_by('-start_date')
    
    locked_count = fiscal_years.filter(is_locked=True).count()
    unlocked_count = fiscal_years.filter(is_locked=False).count()
    
    context = {
        'total_locks': total_locks,
        'total_unlocks': total_unlocks,
        'total_lock_activities': total_lock_activities,
        'lock_percentage': lock_percentage,
        'unlock_percentage': unlock_percentage,
        'total_exemptions': total_exemptions,
        'locked_fy_count': locked_count,
        'unlocked_fy_count': unlocked_count,
        'lock_by_user': lock_by_user,
        'lock_by_fy': lock_by_fy,
        'partial_locks': partial_locks,
        'lock_by_category': lock_by_category,
        'exemptions_by_fy': exemptions_by_fy,
        'lock_history': lock_history[:10],  # Last 10 activities
        'exemptions': exemptions,
    }
    
    return render(request, 'system_settings/lock_reports.html', context)


@login_required
@require_http_methods(['POST'])
def delete_period_lock(request, pk):
    """Delete a period lock history entry"""
    # Permission check - require delete access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_period_lock(request.user)):
            messages.error(request, 'You do not have permission to delete Period Lock entries.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to delete Period Lock entries.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    
    try:
        lock_entry = PeriodLock.objects.using(db).get(pk=pk)
        lock_entry.delete()
        messages.success(request, f'✅ Lock history entry deleted successfully.')
    except PeriodLock.DoesNotExist:
        messages.error(request, '❌ Lock entry not found.')
    except Exception as e:
        messages.error(request, f'❌ Error deleting lock entry: {str(e)}')
    
    company_code = getattr(request, 'company_code', None)
    tab = request.GET.get('tab', 'history')  # Get tab parameter from query string
    if company_code:
        return redirect(f'/{company_code}/system_settings/periods/?tab={tab}')
    return redirect_with_company('period_management')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BY ADARSH FISCAL YEAR CLOSING - Views for year-end closing process
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@login_required
def fiscal_year_closing_dashboard(request):
    """
    Show fiscal year closing dashboard and summary.
    Path: /system-settings/fiscal-closing/
    """
    # Permission check - require create/edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_period_lock(request.user)):
            messages.error(request, 'You do not have permission to close Fiscal Year.')
            return render(request, 'site_blocked.html', {
                'license_info': {'has_license': False},
                'company_code': getattr(request, 'company_code', None),
                'contact_email': 'support@lyra.com'
            }, status=403)
    except Exception:
        messages.error(request, 'You do not have permission to close Fiscal Year.')
        return render(request, 'site_blocked.html', {
            'license_info': {'has_license': False},
            'company_code': getattr(request, 'company_code', None),
            'contact_email': 'support@lyra.com'
        }, status=403)
    
    db = getattr(request, 'company_db', 'default')
    
    # Get all fiscal years
    fiscal_years = FiscalYear.objects.using(db).all().order_by('-start_date')
    
    # Get closing records (handle table not existing yet)
    from .models import FiscalYearClosing
    from django.db import ProgrammingError
    
    closing_records = []
    try:
        closing_records = FiscalYearClosing.objects.using(db).select_related(
            'fiscal_year', 'initiated_by', 'completed_by'
        ).order_by('-initiated_at')
    except ProgrammingError:
        # Table doesn't exist yet - migration pending for this company
        closing_records = []
    
    # Filter for potential closing (locked but not yet closing-recorded)
    closeable_years = []
    closing_years_ids = {cr.fiscal_year_id for cr in closing_records}
    for fy in fiscal_years:
        # Can close if:
        # 1. Fiscal year is locked (is_locked=True)
        # 2. No closing record exists yet
        if fy.is_locked and fy.id not in closing_years_ids:
            closeable_years.append(fy)
    
    context = {
        'fiscal_years': fiscal_years,
        'closeable_years': closeable_years,
        'closing_records': closing_records,
        'can_execute_closing': True,
    }
    
    return render(request, 'system_settings/fiscal_closing_dashboard.html', context)


@login_required
@require_http_methods(['GET', 'POST'])
def prepare_fiscal_year_closing(request, pk):
    """
    Prepare for fiscal year closing - show summary of transactions.
    Allows user to review before actual closing.
    Path: /system-settings/fiscal-closing/<id>/prepare/
    """
    # Permission check
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_period_lock(request.user)):
            messages.error(request, 'You do not have permission to close Fiscal Year.')
            return redirect_with_company('period_management')
    except Exception:
        messages.error(request, 'You do not have permission to close Fiscal Year.')
        return redirect_with_company('period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    
    # Check if already closed
    from .models import FiscalYearClosing
    if hasattr(fiscal_year, 'closing_record'):
        messages.warning(request, f'Fiscal Year {fiscal_year.name} is already closed.')
        return redirect_with_company('fiscal_year_closing_dashboard')
    
    # Import closing utilities
    from .fiscal_year_closing_utils import get_fiscal_year_totals, get_or_create_retained_earnings_account
    
    # Get financial summary
    totals = get_fiscal_year_totals(fiscal_year, db)
    
    # ✅ Get or auto-create Retained Earnings account
    # This is guaranteed to succeed
    retained_earnings = get_or_create_retained_earnings_account(db)
    
    # Get next fiscal year (if it exists)
    next_fy = FiscalYear.objects.using(db).filter(
        start_date__gt=fiscal_year.end_date
    ).order_by('start_date').first()
    
    # Calculate expected next FY year for error message
    from datetime import timedelta
    next_fy_date = fiscal_year.end_date + timedelta(days=1)
    next_fy_year = next_fy_date.year
    expected_next_fy_name = f"FY {next_fy_year}-{next_fy_year + 1 if next_fy_year != 2999 else 3000}"
    
    if request.method == 'POST':
        # Proceed to closing
        return redirect_with_company(request, 'execute_fiscal_year_closing', pk=fiscal_year.id)
    
    context = {
        'fiscal_year': fiscal_year,
        'next_fiscal_year': next_fy,
        'expected_next_fy_name': expected_next_fy_name,
        'totals': totals,
        'retained_earnings_account': retained_earnings,
        'can_proceed': next_fy is not None,  # ✅ Only need to check if next FY exists (Retained Earnings auto-created)
    }
    
    return render(request, 'system_settings/fiscal_closing_prepare.html', context)


@login_required
@transaction.atomic
def execute_fiscal_year_closing(request, pk):
    """
    Execute the actual fiscal year closing process.
    GET: Show final confirmation
    POST: Execute closing, create journals, lock year
    Path: /system-settings/fiscal-closing/<id>/execute/
    """
    # Permission check
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_period_lock(request.user)):
            messages.error(request, 'You do not have permission to close Fiscal Year.')
            return redirect_with_company(request, 'period_management')
    except Exception:
        messages.error(request, 'You do not have permission to close Fiscal Year.')
        return redirect_with_company(request, 'period_management')
    
    db = getattr(request, 'company_db', 'default')
    fiscal_year = get_object_or_404(FiscalYear.objects.using(db), pk=pk)
    
    # Check if already closed
    from .models import FiscalYearClosing
    if FiscalYearClosing.objects.using(db).filter(fiscal_year=fiscal_year).exists():
        messages.warning(request, f'Fiscal Year {fiscal_year.name} is already closed.')
        return redirect_with_company(request, 'fiscal_year_closing_dashboard')
    
    # Get next fiscal year
    next_fy = FiscalYear.objects.using(db).filter(
        start_date__gt=fiscal_year.end_date
    ).order_by('start_date').first()
    
    if not next_fy:
        messages.error(request, 'Next fiscal year not found. Please create it before closing.')
        return redirect_with_company(request, 'prepare_fiscal_year_closing', pk=fiscal_year.id)
    
    # Handle GET: Show confirmation page
    if request.method == 'GET':
        context = {
            'fiscal_year': fiscal_year,
            'next_fiscal_year': next_fy,
        }
        return render(request, 'system_settings/fiscal_closing_execute_confirmation.html', context)
    
    # Handle POST: Execute closing
    if request.method == 'POST':
        # Import and execute closing
        from .fiscal_year_closing_utils import execute_fiscal_year_closing as exec_closing
        
        success, message, closing_record = exec_closing(
            fiscal_year, next_fy, request=request, db=db
        )
        
        if success:
            messages.success(request, message)
        else:
            messages.error(request, message)
        
        return redirect_with_company(request, 'fiscal_year_closing_dashboard')
    
    return redirect_with_company(request, 'fiscal_year_closing_dashboard')


@login_required
def fiscal_year_closing_details(request, pk):
    """
    View details of a completed fiscal year closing.
    Shows the closing entries and financial summary.
    Path: /system-settings/fiscal-closing/<id>/details/
    """
    # Permission check
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_period_lock(request.user)):
            messages.error(request, 'You do not have permission to view Fiscal Closing details.')
            return render(request, 'site_blocked.html', {
                'license_info': {'has_license': False},
                'company_code': getattr(request, 'company_code', None),
                'contact_email': 'support@lyra.com'
            }, status=403)
    except Exception:
        messages.error(request, 'You do not have permission to view Fiscal Closing details.')
        return render(request, 'site_blocked.html', {
            'license_info': {'has_license': False},
            'company_code': getattr(request, 'company_code', None),
            'contact_email': 'support@lyra.com'
        }, status=403)
    
    db = getattr(request, 'company_db', 'default')
    
    # Import closing model
    from .models import FiscalYearClosing
    
    closing_record = get_object_or_404(
        FiscalYearClosing.objects.using(db).select_related(
            'fiscal_year', 'initiated_by', 'completed_by'
        ),
        pk=pk
    )
    
    # Get closing journal entries if they exist
    closing_journal = None
    opening_journal = None
    
    if closing_record.closing_journal_id:
        from journal.models import JournalEntry
        closing_journal = JournalEntry.objects.using(db).filter(
            id=closing_record.closing_journal_id
        ).prefetch_related('lines').first()
    
    if closing_record.opening_journal_id:
        from journal.models import JournalEntry
        opening_journal = JournalEntry.objects.using(db).filter(
            id=closing_record.opening_journal_id
        ).prefetch_related('lines').first()
    
    context = {
        'closing_record': closing_record,
        'fiscal_year': closing_record.fiscal_year,
        'closing_journal': closing_journal,
        'opening_journal': opening_journal,
    }
    
    return render(request, 'system_settings/fiscal_closing_details.html', context)


@login_required
def delete_fiscal_year_closing(request, pk):
    """
    Delete a fiscal year closing record to allow re-closing with corrected logic.
    Useful when corrections are needed to the closing entries.
    """
    # Permission check - require edit access
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_period_lock(request.user)):
            messages.error(request, 'You do not have permission to delete Fiscal Closing.')
            return redirect_with_company('fiscal_year_closing_dashboard')
    except Exception:
        messages.error(request, 'You do not have permission to delete Fiscal Closing.')
        return redirect_with_company('fiscal_year_closing_dashboard')
    
    db = getattr(request, 'company_db', 'default')
    
    from .models import FiscalYearClosing
    from journal.models import JournalEntry
    
    closing_record = get_object_or_404(
        FiscalYearClosing.objects.using(db),
        pk=pk
    )
    
    fiscal_year = closing_record.fiscal_year
    
    if request.method == 'POST':
        # Delete the journal entries first
        if closing_record.closing_journal_id:
            try:
                JournalEntry.objects.using(db).filter(id=closing_record.closing_journal_id).delete()
            except Exception:
                pass
        
        if closing_record.opening_journal_id:
            try:
                JournalEntry.objects.using(db).filter(id=closing_record.opening_journal_id).delete()
            except Exception:
                pass
        
        # Delete the closing record
        closing_record.delete(using=db)
        
        messages.success(
            request,
            f'✓ Fiscal Year closing record deleted. You can now close "{fiscal_year.name}" again with corrected entries.'
        )
        return redirect_with_company('fiscal_year_closing_dashboard')
    
    # GET request - show confirmation
    context = {
        'closing_record': closing_record,
        'fiscal_year': fiscal_year,
    }
    
    return render(request, 'system_settings/fiscal_closing_delete_confirm.html', context)

# BY ADARSH FISCAL YEAR CLOSING 
@login_required
def get_fiscal_year_for_date(date_obj, db='default'):
    """
    Helper function: Get FiscalYear for a given date
    Args:
        date_obj: Date object to search for
        db: Database name to query (default: 'default')
    Returns: FiscalYear object or None
    """
    return FiscalYear.objects.using(db).filter(
        start_date__lte=date_obj,
        end_date__gte=date_obj
    ).first()

#updt by neha on 4-3-26
def prefix_update(request):
    if request.method == 'POST':
        active_tab = request.POST.get('active_tab', 'other_settings_tab')
        db_alias = getattr(request, 'company_db', 'default')

        def set_prefix(Model, key):
            val = request.POST.get(key, '').strip()
            manager = Model.objects.using(db_alias)
            if val:
                obj = manager.first()
                if obj:
                    obj.prefix = val
                    obj.save(using=db_alias)
                else:
                    manager.create(prefix=val)
            else:
                # Clear the prefix if field left empty
                manager.all().delete()

        set_prefix(QuotePrefix,          'sales_quote_prefix')
        set_prefix(SalesOrderPrefix,     'sales_order_prefix')
        set_prefix(InvoicePrefix,        'sales_invoice_prefix')
        set_prefix(PurchaseOrderPrefix,  'purchase_order_prefix')
        set_prefix(BillPrefix,           'purchase_bill_prefix')

        # Save company-level boolean for auto-loading cash customer
        try:
            auto_load = request.POST.get('auto_load_cash_customer') == 'on'
            company_obj = Company.objects.using(db_alias).first()
            if company_obj is not None:
                company_obj.auto_load_cash_customer = auto_load
                company_obj.save(using=db_alias, update_fields=['auto_load_cash_customer'])
        except Exception:
            # Non-fatal: continue even if saving this flag fails
            pass
        try:
            show_summary = request.POST.get('show_base_transaction_summary') == 'on'
            company_obj = Company.objects.using(db_alias).first()
            if company_obj is not None:
                company_obj.show_base_transaction_summary = show_summary
                company_obj.save(using=db_alias, update_fields=['show_base_transaction_summary'])
        except Exception:
            pass
        try:
            rounding_method = request.POST.get('sales_rounding_method', 'none').strip().lower()
            if rounding_method not in {'none', 'whole', 'increment'}:
                rounding_method = 'none'
            rounding_increment = Decimal(request.POST.get('sales_rounding_increment', '0') or '0')
            if rounding_increment < 0:
                rounding_increment = Decimal('0')
            show_rounding = request.POST.get('show_sales_rounding_adjustment') == 'on'
            company_obj = Company.objects.using('default').filter(
                company_code=getattr(request, 'company_code', None),
            ).first()
            if company_obj is None and getattr(request, 'company_id', None):
                company_obj = Company.objects.using('default').filter(
                    pk=request.company_id,
                ).first()
            if company_obj is None:
                company_obj = Company.objects.using('default').first()

            if company_obj is not None:
                rounding_values = {
                    'sales_rounding_method': rounding_method,
                    'sales_rounding_increment': rounding_increment,
                    'show_sales_rounding_adjustment': show_rounding,
                }
                Company.objects.using('default').filter(pk=company_obj.pk).update(**rounding_values)
                if db_alias != 'default':
                    Company.objects.using(db_alias).filter(pk=company_obj.pk).update(**rounding_values)
        except (ArithmeticError, TypeError, ValueError):
            messages.error(request, 'Invalid sales rounding settings.')

        messages.success(request, 'Saved successfully.')
        settings_url = get_company_redirect_url(request, 'settings_page')
        return redirect(f"{settings_url}?tab={active_tab}")

    return redirect_with_company('settings_page')


@login_required
@require_http_methods(['POST'])
def backup_now(request):
    active_tab = request.POST.get('active_tab', 'other_settings_tab')
    db_alias = getattr(request, 'company_db', 'default')
    company_code = getattr(request, 'company_code', db_alias)

    try:
        backup_file = create_database_backup(db_alias=db_alias, company_code=company_code)

        response = FileResponse(open(backup_file, 'rb'), as_attachment=True, filename=backup_file.name)
        response['Content-Type'] = 'application/octet-stream'
        return response

    except FileNotFoundError:
        messages.error(request, 'Backup tool not found. Ensure mysqldump is installed and available in PATH.')
    except Exception as exc:
        messages.error(request, f'Backup failed: {exc}')

    settings_url = get_company_redirect_url(request, 'settings_page')
    return redirect(f"{settings_url}?tab={active_tab}")


@login_required
@require_http_methods(['POST'])
def backup_schedule_update(request):
    active_tab = request.POST.get('active_tab', 'other_settings_tab')
    db_alias = getattr(request, 'company_db', 'default')
    schedule = BackupSchedule.objects.using(db_alias).first()

    is_enabled = request.POST.get('is_enabled') == 'on'

    frequency = request.POST.get('frequency')
    if not frequency:
        frequency = schedule.frequency if schedule else BackupSchedule.FREQUENCY_DAILY

    run_time_str = request.POST.get('run_time')
    weekday = request.POST.get('weekday')
    month_day_raw = request.POST.get('month_day')

    try:
        if run_time_str:
            run_time = datetime.strptime(run_time_str, '%H:%M').time()
        else:
            run_time = schedule.run_time if schedule else datetime.strptime('01:00', '%H:%M').time()
    except ValueError:
        messages.error(request, 'Invalid backup time format.')
        settings_url = get_company_redirect_url(request, 'settings_page')
        return redirect(f"{settings_url}?tab={active_tab}")

    if frequency == BackupSchedule.FREQUENCY_WEEKLY:
        weekday = (weekday or (schedule.weekday if schedule else 'monday') or 'monday').lower()
        month_day = None
    elif frequency == BackupSchedule.FREQUENCY_MONTHLY:
        if month_day_raw:
            try:
                month_day = int(month_day_raw)
            except ValueError:
                messages.error(request, 'Invalid monthly backup date.')
                settings_url = get_company_redirect_url(request, 'settings_page')
                return redirect(f"{settings_url}?tab={active_tab}")
        elif schedule and schedule.month_day:
            month_day = schedule.month_day
        else:
            month_day = 1
        weekday = ''
    else:
        weekday = ''
        month_day = None

    if schedule:
        schedule.is_enabled = is_enabled
        schedule.frequency = frequency
        schedule.run_time = run_time
        schedule.weekday = weekday
        schedule.month_day = month_day
    else:
        schedule = BackupSchedule(
            is_enabled=is_enabled,
            frequency=frequency,
            run_time=run_time,
            weekday=weekday,
            month_day=month_day,
        )

    try:
        schedule.full_clean()
        schedule.save(using=db_alias)
        messages.success(request, 'Backup schedule saved successfully.')
    except Exception as exc:
        messages.error(request, f'Failed to save backup schedule: {exc}')

    settings_url = get_company_redirect_url(request, 'settings_page')
    return redirect(f"{settings_url}?tab={active_tab}")


def _get_backup_file_rows(company_code):
    backup_dir = get_backup_storage_path(company_code)
    if not backup_dir.exists():
        return []

    rows = []
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        stat = entry.stat()
        rows.append({
            "name": entry.name,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime),
        })

    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    return rows

@login_required
@require_http_methods(['GET'])
def backup_download(request, filename=None):
    filename = (filename or request.GET.get('file') or '').strip()
    if not filename:
        messages.error(request, 'No backup file selected.')
        settings_url = get_company_redirect_url(request, 'settings_page')
        return redirect(f"{settings_url}?tab=other_settings_tab")

    company_code = getattr(request, 'company_code', None) or getattr(request, 'company_db', 'default')
    backup_dir = get_backup_storage_path(company_code).resolve()
    target_file = (backup_dir / filename).resolve()

    # Allow download only for files inside the current company's backup directory.
    if target_file.parent != backup_dir:
        messages.error(request, 'Invalid backup file path.')
        settings_url = get_company_redirect_url(request, 'settings_page')
        return redirect(f"{settings_url}?tab=other_settings_tab")

    if not target_file.exists() or not target_file.is_file():
        messages.error(request, 'Backup file not found.')
        settings_url = get_company_redirect_url(request, 'settings_page')
        return redirect(f"{settings_url}?tab=other_settings_tab")

    response = FileResponse(open(target_file, 'rb'), as_attachment=True, filename=target_file.name)
    response['Content-Type'] = 'application/octet-stream'
    return response
