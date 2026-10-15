# from django.shortcuts import render

# Create your views here.
# purchases/views.py

from django.shortcuts import render, redirect, get_object_or_404
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_redirect_url
from .models import PurchaseOrder,OrderPrefix,PurchaseOrderItem,Bill,BillItem,PaymentMode,BillPaymentAttachment,BillPayment,BillPaymentAllocation,VendorAdvancePayment, BillPrefix,PurchaseReturn,PurchaseReturnItem,DeliveryNote
from .forms import PurchaseOrderForm,PurchaseOrderItemForm,BillForm, BillItemForm,BillPaymentForm, PurchaseReturnForm
from company.models import Company
from Tax.models import TaxGroup, Tax, TaxMaster, TdsMaster, TcsMaster
from django import forms
import io
import traceback
from email_templates.utils import replace_placeholders
from django.http import HttpResponse
from django.core.mail import get_connection, EmailMultiAlternatives, EmailMessage
from email_config.models import EmailConfiguration
from django.views.decorators.csrf import csrf_exempt
from .models import Vendor
from django.core.paginator import Paginator
from django.db.models import Q,Sum
from django.db import IntegrityError
from django.db.models.deletion import ProtectedError
from .forms import VendorForm, ContactPersonFormSet,VendorFormmodal
from django.views.decorators.http import require_POST
from django.forms import inlineformset_factory
from django.http import JsonResponse
from django.template.loader import render_to_string
from Items.forms import ItemForm 
from django.db import transaction
from django.db import connections
from Items.models import Item,Barcode,Uom
from unit.models import Unit
from django.utils.timezone import localdate
from django.utils import timezone
from company.utils import is_stock_management_on_delivery

import datetime
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from Tax.views import get_tax
from django.contrib import messages
from PayTerms.models import PayTerms
from django.forms import modelformset_factory
from django.forms import formset_factory
from Tax.models import TaxGroup, Tax
from django.db.models.functions import Concat
import re
from collections import defaultdict
from django.db.models import Value
from chart_of_accounts.models import ChartOfAccounts
from chart_of_accounts.services import (
    ensure_tds_tcs_accounts,
    get_company_tax_type,
    normalize_tax_code,
    resolve_tax_account,
    resolve_tds_tcs_account,
)
from currencies.services import refresh_document_total_base, scale_amount_for_journal
from django.db.models import Sum, Q, F
from django.contrib.auth.decorators import login_required
from journal.models import JournalEntry, JournalLine
import logging
logger = logging.getLogger(__name__)
from django.conf import settings


def _post_tds_tcs_journal_line(journal, document, using, sequence):
    tds_tcs_type = str(getattr(document, 'tds_tcs_type', '') or '').strip().lower()
    if tds_tcs_type not in ('tds', 'tcs'):
        return sequence

    # Scale TDS/TCS using explicit fx_rate_to_base to avoid relying on
    # `document.total_amount` vs `total_amount_base` ratio which may be inconsistent.
    try:
        amt = Decimal(str(getattr(document, 'tds_tcs_amount', 0) or 0))
        fx = getattr(document, 'fx_rate_to_base', None) or Decimal('1')
        amount = (amt * fx).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        amount = Decimal('0.00')
    if amount <= 0:
        return sequence

    ensure_tds_tcs_accounts(using=using)
    direction = 'payable' if tds_tcs_type == 'tds' else 'receivable'
    if tds_tcs_type == 'tcs':
        direction = 'receivable'

    account = resolve_tds_tcs_account(tds_tcs_type, direction=direction, using=using)
    if not account:
        return sequence

    debit_amount = amount if direction == 'receivable' else Decimal('0.00')
    credit_amount = amount if direction == 'payable' else Decimal('0.00')
    reference_number = getattr(document, 'bill_number', None) or getattr(document, 'order_number', None) or getattr(document, 'pk', '')
    JournalLine.objects.using(using).create(
        journal=journal,
        account=account,
        description=f"Bill {reference_number} - {account.name}",
        debit=debit_amount,
        credit=credit_amount,
        sequence=sequence,
    )
    return sequence + 10


def _get_purchase_company_country(request=None, using=None):
    """
    Return company country code for the active DB.
    In multi-company setups, each company has its own DB alias; don't always read from default.
    """
    try:
        db = using or getattr(request, 'company_db', None) or 'default'
    except Exception:
        db = using or 'default'
    company = Company.objects.using(db).order_by('id').first()
    return getattr(company, 'country', '') if company else ''


def _get_company_for_request(request):
    """
    Resolve active Company for the request (same logic used in sales.views).
    """
    try:
        if request is None:
            return Company.objects.filter(status=True).first() or Company.objects.first()

        company_db = getattr(request, 'company_db', None) or (request.session.get('company_db') if hasattr(request, 'session') else None)
        company_code = getattr(request, 'company_code', None) or (request.session.get('company_code') if hasattr(request, 'session') else None)

        if company_db and company_db != 'default':
            qs = Company.objects.using(company_db).filter(status=True)
            if company_code:
                obj = qs.filter(company_code=company_code).first()
                if obj:
                    return obj
            obj = qs.first()
            if obj:
                return obj

        qs = Company.objects.using('default').filter(status=True)
        if company_code:
            obj = qs.filter(company_code=company_code).first()
            if obj:
                return obj
        return qs.first() or Company.objects.using('default').first()
    except Exception:
        return Company.objects.filter(status=True).first() or Company.objects.first()


def _get_purchase_company_tax_type(request=None):
    try:
        db = getattr(request, 'company_db', None) or 'default'
        return (get_company_tax_type(using=db) or '').strip().upper()
    except Exception:
        company = _get_company_for_request(request)
        return (getattr(company, 'tax_type', '') or '').strip().upper()


def _is_indian_company_country(country):
    return str(country or '').strip().upper() == 'IN'


def _parse_decimal(value, default='0'):
    try:
        return Decimal(str(value or default))
    except Exception:
        return Decimal(str(default))


def _parse_positive_int(value):
    try:
        iv = int(value)
        return iv if iv >= 0 else None
    except Exception:
        return None


def _get_tax_type_code(value):
    return normalize_tax_code(value)


def _get_tax_type_code(value):
    return normalize_tax_code(value)


def _build_selected_tax_token(tax_group=None, tax_obj=None):
    if tax_group:
        return f"group:{tax_group.id}"
    if tax_obj:
        return f"tax:{tax_obj.id}"
    return None


def _resolve_selected_tax(selection_value):
    selection_value = (selection_value or '').strip()
    if not selection_value or selection_value == 'non_taxable':
        return Decimal('0.00'), None

    tax_group = None
    tax_obj = None

    if selection_value.startswith('group:'):
        tax_group = TaxGroup.objects.filter(
            id=selection_value.split(':', 1)[1]
        ).prefetch_related('taxes').first()
    elif selection_value.startswith('tax:'):
        tax_obj = Tax.objects.filter(
            id=selection_value.split(':', 1)[1]
        ).select_related('taxtype').first()
    else:
        tax_group = TaxGroup.objects.filter(
            id=selection_value
        ).prefetch_related('taxes').first()
        if not tax_group:
            tax_obj = Tax.objects.filter(id=selection_value).select_related('taxtype').first()

    if tax_group:
        total_rate = Decimal(sum(t.rate for t in tax_group.taxes.all()))
        return total_rate, tax_group.group_name

    if tax_obj:
        return Decimal(tax_obj.rate or 0), (getattr(tax_obj, 'display_name', None) or tax_obj.taxname)

    return Decimal('0.00'), None


def _normalize_tax_selection_token(selection_value, company_is_india=None):
    selection_value = (selection_value or '').strip()
    if not selection_value:
        return ''
    if selection_value == 'non_taxable':
        return ''
    if selection_value.startswith('group:') or selection_value.startswith('tax:'):
        return selection_value

    tax_group = TaxGroup.objects.filter(id=selection_value).first()
    tax_obj = Tax.objects.filter(id=selection_value).first()

    if company_is_india is True and tax_group:
        return _build_selected_tax_token(tax_group=tax_group)
    if company_is_india is False and tax_obj:
        return _build_selected_tax_token(tax_obj=tax_obj)
    if tax_group:
        return _build_selected_tax_token(tax_group=tax_group)
    if tax_obj:
        return _build_selected_tax_token(tax_obj=tax_obj)
    return selection_value


def _normalize_item_tax_tokens(post_data, company_is_india=None):
    for key in list(post_data.keys()):
        if key.startswith('form-'):
            if key.endswith('-prd_tax'):
                post_data[key] = _normalize_tax_selection_token(post_data.get(key), company_is_india)
            elif key.endswith('-price') or key.endswith('-o_price'):
                try:
                    val = post_data.get(key)
                    if val:
                        post_data[key] = str(Decimal(str(val)).quantize(Decimal('0.0001')))
                except:
                    pass
            elif key.endswith('-prd_disvalue'):
                try:
                    val = post_data.get(key)
                    if val:
                        post_data[key] = str(Decimal(str(val)).quantize(Decimal('0.01')))
                except:
                    pass
    return post_data


def _compact_purchase_item_formset_post_data(post_data):
    """
    Drop blank/deleted purchase item rows before binding the create formset.

    The purchase-order UI always renders at least one empty row and may also
    leave removed dynamic rows in POST. Those rows should not make the whole
    formset invalid.
    """
    total_forms_raw = post_data.get('form-TOTAL_FORMS', '0')
    try:
        total_forms = int(total_forms_raw)
    except (TypeError, ValueError):
        return post_data

    row_field_suffixes = [
        'id', 'product', 'prd_brcd', 'hsn_code', 'prd_disvalue', 'prd_distype',
        'quantity', 'price', 'prd_tax', 'description', 'gstinclude', 'o_price', 'DELETE',
    ]

    def _as_decimal(value, default='0'):
        try:
            return Decimal(str(value if value not in (None, '') else default))
        except Exception:
            return Decimal(str(default))

    def _should_keep_row(prefix):
        delete_value = str(post_data.get(f'{prefix}-DELETE', '') or '').strip().lower()
        if delete_value in {'on', 'true', '1'}:
            return False

        product = str(post_data.get(f'{prefix}-product', '') or '').strip()
        description = str(post_data.get(f'{prefix}-description', '') or '').strip()
        hsn_code = str(post_data.get(f'{prefix}-hsn_code', '') or '').strip()
        prd_tax = str(post_data.get(f'{prefix}-prd_tax', '') or '').strip()
        price = _as_decimal(post_data.get(f'{prefix}-price', '0'))
        qty = _as_decimal(post_data.get(f'{prefix}-quantity', '0'))
        discount = _as_decimal(post_data.get(f'{prefix}-prd_disvalue', '0'))
        o_price = _as_decimal(post_data.get(f'{prefix}-o_price', '0'))

        if product or description or hsn_code or prd_tax:
            return True
        if price != Decimal('0') or discount != Decimal('0') or o_price != Decimal('0'):
            return True
        if qty not in {Decimal('0'), Decimal('1')}:
            return True
        return False

    kept_rows = []
    for index in range(total_forms):
        prefix = f'form-{index}'
        if _should_keep_row(prefix):
            kept_rows.append(prefix)

    compacted = post_data.copy()
    for key in list(compacted.keys()):
        if key.startswith('form-'):
            compacted.pop(key, None)

    compacted['form-TOTAL_FORMS'] = str(len(kept_rows))
    compacted['form-INITIAL_FORMS'] = '0'
    compacted['form-MIN_NUM_FORMS'] = post_data.get('form-MIN_NUM_FORMS', '0')
    compacted['form-MAX_NUM_FORMS'] = post_data.get('form-MAX_NUM_FORMS', '1000')

    for new_index, old_prefix in enumerate(kept_rows):
        new_prefix = f'form-{new_index}'
        for suffix in row_field_suffixes:
            old_key = f'{old_prefix}-{suffix}'
            if old_key in post_data:
                compacted[f'{new_prefix}-{suffix}'] = post_data.get(old_key)

    return compacted
from stock.models import Stock
from .permissions import (
    can_view_purchase_dashboard,
    can_view_purchase_orders, can_create_purchase_orders, can_edit_purchase_orders, can_delete_purchase_orders,
    can_view_purchase_bills, can_create_purchase_bills, can_edit_purchase_bills, can_delete_purchase_bills,
    can_view_purchase_delivery, can_create_purchase_delivery, can_edit_purchase_delivery, can_delete_purchase_delivery,
    can_view_purchase_expenses, can_create_purchase_expenses, can_edit_purchase_expenses, can_delete_purchase_expenses,
    can_view_purchase_payments, can_create_purchase_payments, can_edit_purchase_payments, can_delete_purchase_payments,
    can_view_purchase_returns, can_create_purchase_returns, can_edit_purchase_returns, can_delete_purchase_returns,
    can_view_vendors, can_create_vendors, can_edit_vendors, can_delete_vendors
)
from warehouse.models import Warehouse
from xml.sax.saxutils import escape


from .models import DeliveryNote,DeliveryNoteItem,StockMovement
from .serializer import DeliveryNoteSerializer,StockMovementSerializer
from rest_framework.decorators import action

from rest_framework import viewsets
from django.views.generic import ListView, DetailView, CreateView, UpdateView
from .forms import DeliveryNoteForm, DeliveryNoteItemFormSet
from django.urls import reverse_lazy
from stock.models import Stock
import random
import json

#vendor
def safe(value):
    return escape(str(value)) if value else ""
#vendor


# def add_vendor(request):
#     if request.method == 'POST':
#         form = VendorForm(request.POST, request.FILES)
#         if form.is_valid():
#             form.save()
#             return redirect('vendor_list')
#     else:
#         form = VendorForm()

#     return render(request, 'vendor_form.html', {
#         'form': form
#     })
# from .models import Vendor
# from .forms import VendorForm
def vendor_list(request):
    # Permission: require view access to Vendor
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_vendors(request.user)):
            messages.error(request, 'You do not have permission to view vendors.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view vendors.')
        return redirect_with_company('index')
    
    search_query = request.GET.get('q', '')
    vendors = Vendor.objects.all().filter(is_active=True)  # Only show active vendors

    if search_query:
        vendors = vendors.filter(
            Q(vendor_code__icontains=search_query) |
            Q(company_name__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(phone__icontains=search_query)
        )
    vendors = vendors.order_by('-id')
    paginator = Paginator(vendors, 10)
    page_number = request.GET.get('page')
    vendors_page = paginator.get_page(page_number)

    return render(request, "Purchase/vendor_list.html", {
        "vendors": vendors_page,
        "search_query": search_query
    })
#by adarsh modified by neha on 31-01-26
def vendor_detail_ajax(request, pk):
    """Return JSON details for a vendor used by purchase order forms.

    Returns billing address (vendor details), a placeholder for shipping address, tax number and state.
    """
    try:
        v = Vendor.objects.get(pk=pk)
        
        # Get vendor name
        if v.vendor_type == 'company':
            vendor_name = v.company_name or str(v)
        else:
            vendor_name = f"{v.first_name or ''} {v.last_name or ''}".strip() or str(v)

        # Handle CountryField - it returns a Country object with .name and .code attributes
        billing_country = ''
        if v.country:
            # CountryField returns a Country object, use .name to get the country name
            billing_country = v.country.name if hasattr(v.country, 'name') else str(v.country)

        billing = {
            'name': vendor_name,
            'address_line_1': v.address_line_1 or '',
            'address_line_2': v.address_line_2 or '',
            'city': v.city or '',
            'state': v.state or '',
            'postal_code': v.postal_code or '',
            'country': billing_country,
            'tax_number': v.tax_number or '',
            'email': v.email or '',
            'phone': v.phone or v.mobile or '',
        }

        # Handle shipping CountryField
        shipping_country = ''
        if v.shipping_country:
            shipping_country = v.shipping_country.name if hasattr(v.shipping_country, 'name') else str(v.shipping_country)

        #  Check if vendor has actual shipping details before populating
        has_shipping_details = any([
            v.shipping_address_line_1,
            v.shipping_address_line_2,
            v.shipping_city,
            v.shipping_state,
            v.shipping_postal_code,
            v.shipping_country
        ])

        # Only populate shipping if vendor has shipping details saved
        if has_shipping_details:
            shipping = {
                'name': vendor_name,
                'email': v.email or '',
                'phone': v.phone or v.mobile or '',
                'shipping_address_line_1': v.shipping_address_line_1 or '',
                'shipping_address_line_2': v.shipping_address_line_2 or '',
                'shipping_city': v.shipping_city or '',
                'shipping_state': v.shipping_state or '',
                'shipping_postal_code': v.shipping_postal_code or '',
                'shipping_country': shipping_country,
            }
        else:
            # Return empty shipping data if no shipping details exist
            shipping = None

        data = {
            'id': v.id,
            'billing': billing,
            'shipping': shipping,
            'place_of_supply': v.state or '',
            'payment_terms': {
                'id': v.payment_terms_id,
                'name': v.payment_terms.name if v.payment_terms else '',
                'days': v.payment_terms.days if v.payment_terms else None,
            } if v.payment_terms_id else None,
        }
        return JsonResponse(data)
        
    except Vendor.DoesNotExist:
        return JsonResponse({'error': 'Vendor not found'}, status=404)
    except Exception as e:
        # Log the actual error for debugging
        import traceback
        print(f"Error in vendor_detail_ajax for vendor {pk}: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({'error': f'Server error: {str(e)}'}, status=500)



@login_required
def purchase_dashboard(request):
    # ── Permission check ──────────────────────────────────────────────────
    try:
        if not (
            getattr(request.user, 'is_superuser', False)
            or can_view_purchase_dashboard(request.user)
        ):
            messages.error(request, 'You do not have permission to view the Purchase dashboard.')
            return redirect_with_company('index')
    except Exception:
        pass
 
    company_db = getattr(request, 'company_db', 'default')
    today = timezone.localdate()

    # Fine-grained permission flags used by dashboard sections/cards
    can_po = getattr(request.user, 'is_superuser', False) or can_view_purchase_orders(request.user)
    can_bills = getattr(request.user, 'is_superuser', False) or can_view_purchase_bills(request.user)
    can_vendors = getattr(request.user, 'is_superuser', False) or can_view_vendors(request.user)
    can_payments = getattr(request.user, 'is_superuser', False) or can_view_purchase_payments(request.user)
    can_delivery = getattr(request.user, 'is_superuser', False) or can_view_purchase_delivery(request.user)
    can_returns = getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)
    can_expenses = getattr(request.user, 'is_superuser', False) or can_view_purchase_expenses(request.user)
 
    # ── Purchase Orders ───────────────────────────────────────────────────
    try:
        from Purchase.models import PurchaseOrder
        po_qs     = PurchaseOrder.objects.using(company_db).all()
        po_count  = po_qs.count()
        recent_pos = po_qs.select_related('vendor').order_by('-date')[:8]
    except Exception:
        po_count = 0
        recent_pos = []
 
    # ── Bills ─────────────────────────────────────────────────────────────
    from Purchase.models import Bill
    bill_qs = Bill.objects.using(company_db).all()
    bill_count = bill_qs.count()
    paid_bill_numbers = []
    pending_bill_numbers = []
    overdue_bill_numbers = []
    try:
        bill_records = list(bill_qs.select_related('payment_status', 'payment_term', 'vendor'))
        paid_bills = sum(
            1 for bill in bill_records
            if getattr(getattr(bill, 'payment_status', None), 'name', '').strip().lower() == 'paid'
        )
        pending_bills = bill_qs.filter(
            Q(payment_status__isnull=True) | Q(payment_status__name='Not Paid')| Q(payment_status__name='Partially Paid')
        ).count()

        overdue_records = []
        for bill in bill_records:
            payment_status = getattr(
                getattr(bill, 'payment_status', None), 'name', ''
            ).strip().lower()
            payment_term = getattr(bill, 'payment_term', None)
            payment_days = int(payment_term.days or 0) if payment_term else None
            due_date = (
                bill.date + timezone.timedelta(days=payment_days)
                if bill.date and payment_days is not None
                else None
            )
            if payment_status in {'', 'not paid', 'partially paid'} and due_date and due_date < today:
                overdue_records.append(bill)

        overdue_bills = len(overdue_records)
        paid_bill_numbers = [
            bill.bill_number for bill in bill_records
            if getattr(getattr(bill, 'payment_status', None), 'name', '').strip().lower() == 'paid'
        ]
        pending_bill_numbers = [
            bill.bill_number for bill in bill_records
            if getattr(getattr(bill, 'payment_status', None), 'name', '').strip().lower()
            in {'', 'not paid', 'partially paid'}
        ]
        overdue_bill_numbers = [bill.bill_number for bill in overdue_records]
 
        agg = bill_qs.aggregate(
            total=Sum('total_amount'),
        )
        total_payable    = agg['total']    or 0
        total_paid = BillPaymentAllocation.objects.using(company_db).filter(
            bill__in=bill_qs
        ).aggregate(total=Sum('amount'))['total'] or 0
        total_outstanding = float(total_payable) - float(total_paid)
        overdue_amount = sum((bill.total_amount or 0) for bill in overdue_records)
 
        recent_bills = bill_qs.select_related('vendor').order_by('-date')[:8]
    except Exception:
        paid_bills = pending_bills = overdue_bills = 0
        total_payable = total_paid = total_outstanding = overdue_amount = 0
        recent_bills = []
    print("bill count:", bill_count, "paid_bills:", paid_bills, "pending_bills:", pending_bills, "overdue_bills:", overdue_bills)
    # ── Vendors ───────────────────────────────────────────────────────────
    try:
        from Purchase.models import Vendor
        vendor_qs = Vendor.objects.using(company_db)
        vendor_count = vendor_qs.filter(is_active=True).count()
        top_vendors  = (
            vendor_qs
            .annotate(total=Sum('bill__total_amount'))
            .filter(total__isnull=False)
            .order_by('-total')[:6]
        )
    except Exception:
        vendor_count = 0
        top_vendors  = []
 
    # ── Payments made ─────────────────────────────────────────────────────
    try:
        from Purchase.models import BillPayment
        payment_count = BillPayment.objects.using(company_db).count()
    except Exception:
        payment_count = 0
 
    # ── Deliveries (GRN) ──────────────────────────────────────────────────
    try:
        from Purchase.models import DeliveryNote
        delivery_count = DeliveryNote.objects.using(company_db).count()
    except Exception:
        delivery_count = 0
 
    # ── Purchase Returns ──────────────────────────────────────────────────
    try:
        from Purchase.models import PurchaseReturn
        return_count = PurchaseReturn.objects.using(company_db).count()
    except Exception:
        return_count = 0

     # ── Expenses ─────────────────────────────────────────────────────────
    try:
        from expenses.models import Expense
        expense_count = Expense.objects.using(company_db).filter(status=True).count()
    except Exception:
        expense_count = 0
 
    # ── Top purchased items by qty ────────────────────────────────────────
    try:
        from Purchase.models import BillItem
        top_items = (
            BillItem.objects.using(company_db)
            .values('product__name')
            .annotate(qty=Sum('quantity'))
            .order_by('-qty')[:6]
        )
    except Exception:
        top_items = []
 
    # ── Monthly bills chart (last 6 months) ───────────────────────────────
    chart_labels, chart_data = [], []
    try:
        for i in range(5, -1, -1):
            yr, m = today.year, today.month - i
            while m <= 0:
                m += 12
                yr -= 1
            start      = date(yr, m, 1)
            next_start = date(yr + 1, 1, 1) if m == 12 else date(yr, m + 1, 1)
            month_total = (
                Bill.objects.using(company_db)
                .filter(date__gte=start, date__lt=next_start)
                .aggregate(t=Sum('total_amount'))['t'] or 0
            )
            chart_labels.append(start.strftime('%b %Y'))
            chart_data.append(float(month_total))
    except Exception:
        chart_labels, chart_data = [], []
 
    return render(request, 'Purchase/purchase_dashboard.html', {
        # KPI counts
        'po_count':       po_count,
        'bill_count':     bill_count,
        'vendor_count':   vendor_count,
        'payment_count':  payment_count,
        'delivery_count': delivery_count,
        'return_count':   return_count,
        'expense_count':  expense_count,
        # Bill status
        'paid_bills':    paid_bills,
        'pending_bills': pending_bills,
        'overdue_bills': overdue_bills,
        'paid_bill_numbers': paid_bill_numbers,
        'pending_bill_numbers': pending_bill_numbers,
        'overdue_bill_numbers': overdue_bill_numbers,
        # Payables
        'total_payable':     total_payable,
        'total_paid':        total_paid,
        'total_outstanding': total_outstanding,
        'overdue_amount':    overdue_amount,
        # Tables
        'recent_pos':   recent_pos,
        'recent_bills': recent_bills,
        'top_vendors':  top_vendors,
        'top_items':    top_items,
        # Chart
        'chart_labels': json.dumps(chart_labels),
        'chart_data':   json.dumps(chart_data),
        # Permission flags
        'can_po': can_po,
        'can_bills': can_bills,
        'can_vendors': can_vendors,
        'can_payments': can_payments,
        'can_delivery': can_delivery,
        'can_returns': can_returns,
        'can_expenses': can_expenses,
    })

#by adarsh
# def vendor_edit(request, pk):
#     vendor = get_object_or_404(Vendor, pk=pk)
#     if request.method == 'POST':
#         form = VendorForm(request.POST, request.FILES, instance=vendor)
#         if form.is_valid():
#             form.save()
#             return redirect('vendor_list')
#     else:
#         form = VendorForm(instance=vendor)
#     return render(request, 'Purchase/vendor_edit.html', {'form': form})

# @login_required
# def vendor_create(request):
#     # Permission: require Create on Vendor
#     try:
#         if not (getattr(request.user, 'is_superuser', False) or can_create_vendors(request.user)):
#             messages.error(request, "You do not have permission to create vendors.")
#             return redirect('vendor_list')
#     except Exception:
#         messages.error(request, "You do not have permission to create vendors.")
#         return redirect('vendor_list')

#         form = VendorForm(request.POST)
#         if form.is_valid():
#             vendor = form.save(commit=False)
#             vendor.created_by = request.user
#             vendor.updated_by = request.user
#             vendor.save()
            
#             # Save contact persons if provided
#             contact_names = request.POST.getlist('contact_name')
#             contact_emails = request.POST.getlist('contact_email')
#             contact_phones = request.POST.getlist('contact_phone')
#             # Clear existing contacts for new vendor (none) - just create
#             from .models import ContactPerson
#             for name, email, phone in zip(contact_names, contact_emails, contact_phones):
#                 if name.strip() or email.strip() or phone.strip():
#                     ContactPerson.objects.create(
#                         vendor=vendor,
#                         name=name.strip(),
#                         email=email.strip(),
#                         phone=phone.strip()
#                     )

#             # Create vendor if is_also_vendor is checked — after vendor contacts are saved
#             if vendor.is_customer:
#                 from Purchase.models import Vendor, ContactPerson as VendorContact
#                 try:
#                     vendor = Vendor.objects.create(
#                         vendor_code=vendor.vendor_code,
#                         vendor_type=vendor.vendor_type,
#                         first_name=vendor.first_name,
#                         last_name=vendor.last_name,
#                         company_name=vendor.company_name,
#                         email=vendor.email,
#                         phone=vendor.phone,
#                         mobile=vendor.mobile,
#                         tax_number=vendor.tax_number,
#                         address_line_1=vendor.address_line_1,
#                         address_line_2=vendor.address_line_2,
#                         city=vendor.city,
#                         postal_code=vendor.postal_code,
#                         state=vendor.state,
#                         country=vendor.country,
#                         shipping_address_line_1=vendor.shipping_address_line_1,
#                         shipping_address_line_2=vendor.shipping_address_line_2,
#                         shipping_city=vendor.shipping_city,
#                         shipping_postal_code=vendor.shipping_postal_code,
#                         shipping_state=vendor.shipping_state,
#                         shipping_country=vendor.shipping_country,
#                         is_active=vendor.is_active,
#                     )
#                     # Copy contact persons from vendor to vendor
#                     cust_contacts = ContactPerson.objects.filter(vendor=vendor)
#                     for cc in cust_contacts:
#                         VendorContact.objects.create(
#                             vendor=vendor,
#                             name=cc.name,
#                             email=cc.email,
#                             phone=cc.phone
#                         )
#                     messages.success(request, "Vendor and Vendor created successfully.")
#                 except Exception as e:
#                     # Log the error but don't fail the vendor creation
#                     print(f"Warning: Could not create vendor for vendor {vendor.id}: {str(e)}")
#                     messages.warning(request, f"Vendor created, but vendor creation failed: {str(e)}")
            
            
#             return redirect('vendor_list')  # Redirect to a list or detail page after saving
#             messages.success(request, "Vendor added successfully.")
            
#     else:
#         form = VendorForm()
        
#         # Check if this is an AJAX request for the modal
#         if request.headers.get('x-requested-with') == 'XMLHttpRequest':
#             # Return just the form content for AJAX requests
#             return render(request, 'Purchase/vendor_form.html', {
#                 'form': form,
#                 'title': 'Add New Vendor',
#             })

#     # For GET show/hide Save button based on permission
#     return render(request, 'Purchase/vendor_form.html', {
#         'form': form,
#         'title': 'Add New Vendor',
#         'can_create_vendors': can_create_vendors(request.user),
#         'can_edit_vendors': can_edit_vendors(request.user),
#     })


@login_required
def vendor_create(request):
    # Permission helpers
    try:
        from Purchase.permissions import can_create_vendors, can_edit_vendors
    except Exception:
        def can_create_vendors(u):
            return getattr(u, 'is_superuser', False)
        def can_edit_vendors(u):
            return getattr(u, 'is_superuser', False)

    if request.method == "POST":
        # Check if this is an AJAX request for the modal
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            # determine company for currency choices
            try:
                cid = request.session.get('company_id')
                company = None
                if cid:
                    company = Company.objects.filter(pk=cid).first()
                if not company:
                    company = Company.objects.order_by('id').first()
            except Exception:
                company = None

            form = VendorForm(request.POST, company=company)
            if form.is_valid():
                vendor = form.save(commit=False)
                vendor.created_by = request.user
                vendor.updated_by = request.user
                vendor.save()
                return JsonResponse({
                    'success': True, 
                    'id': vendor.id, 
                    'name': f"{vendor.first_name} {vendor.last_name}".strip()
                })
            else:
                return JsonResponse({'success': False, 'errors': form.errors}, status=400)
        
        # Block POST if no create permission
        if not can_create_vendors(request.user):
            messages.error(request, "You do not have permission to create vendors.")
            return redirect_with_company('vendor_list')

        # determine company for currency choices
        try:
            cid = request.session.get('company_id')
            company = None
            if cid:
                company = Company.objects.filter(pk=cid).first()
            if not company:
                company = Company.objects.order_by('id').first()
        except Exception:
            company = None

        form = VendorForm(request.POST, company=company)
        if form.is_valid():
            vendor = form.save(commit=False)
            vendor.created_by = request.user
            vendor.updated_by = request.user
            vendor.save()
            
            # Save contact persons if provided
            contact_names = request.POST.getlist('contact_name')
            contact_emails = request.POST.getlist('contact_email')
            contact_phones = request.POST.getlist('contact_phone')
            # Clear existing contacts for new vendor (none) - just create
            from .models import ContactPerson
            for name, email, phone in zip(contact_names, contact_emails, contact_phones):
                if name.strip() or email.strip() or phone.strip():
                    ContactPerson.objects.create(
                        vendor=vendor,
                        name=name.strip(),
                        email=email.strip(),
                        phone=phone.strip()
                    )

            # Create vendor if is_also_vendor is checked — after vendor contacts are saved
            if vendor.is_customer:
                from customer.models import Customer, ContactPerson as CustomerContact
                try:
                    # Check if customer with this email already exists
                    existing_customer = Customer.objects.filter(email=vendor.email).first()
                    
                    if not existing_customer:
                        customer = Customer.objects.create(
                            customer_code=vendor.vendor_code,
                            customer_type=vendor.vendor_type,
                            first_name=vendor.first_name,
                            last_name=vendor.last_name,
                            company_name=vendor.company_name,
                            email=vendor.email,
                            phone=vendor.phone,
                            mobile=vendor.mobile,
                            gst_number=vendor.tax_number,
                            address_line_1=vendor.address_line_1,
                            address_line_2=vendor.address_line_2,
                            city=vendor.city,
                            postal_code=vendor.postal_code,
                            state=vendor.state,
                            country=vendor.country,
                            shipping_address_line_1=vendor.shipping_address_line_1,
                            shipping_address_line_2=vendor.shipping_address_line_2,
                            shipping_city=vendor.shipping_city,
                            shipping_postal_code=vendor.shipping_postal_code,
                            shipping_state=vendor.shipping_state,
                            shipping_country=vendor.shipping_country,
                            pan_number=vendor.pan_number,
                            tax_preference= vendor.tax_preference,
                            exemption_reason=vendor.exemption_reason,
                            currency= vendor.currency,
                            payment_terms= vendor.payment_terms,
                            opening_balance=vendor.opening_balance,
                            gst_treatment=vendor.gst_treatment,
                            is_vendor=vendor.is_customer,
                            created_at=vendor.created_at,
                            updated_at=vendor.updated_at,
                            created_by=vendor.created_by,
                            updated_by=vendor.updated_by,
                            is_active=vendor.is_active,
                        )
                        # Copy contact persons from vendor to customer
                        vendor_contacts = ContactPerson.objects.filter(vendor=vendor)
                        for vc in vendor_contacts:
                            CustomerContact.objects.create(
                                customer=customer,
                                name=vc.name,
                                email=vc.email,
                                phone=vc.phone
                            )
                        messages.success(request, "Vendor and Customer created successfully.")
                    else:
                        if vendor.currency and existing_customer.currency != vendor.currency:
                            existing_customer.currency = vendor.currency
                            existing_customer.save(update_fields=['currency'])
                        messages.info(request, "Vendor created. Customer with this email already exists.")
                        
                except Exception as e:
                    # Log the error but don't fail the vendor creation
                    print(f"Warning: Could not create customer for vendor {vendor.id}: {str(e)}")
                    messages.warning(request, f"Vendor created, but customer creation failed: {str(e)}")
            else:
                messages.success(request, "Vendor added successfully.")
            return redirect_with_company('vendor_list')  # Redirect to a list or detail page after saving
            
            
    else:
        # determine company for currency choices
        try:
            cid = request.session.get('company_id')
            company = None
            if cid:
                company = Company.objects.filter(pk=cid).first()
            if not company:
                company = Company.objects.order_by('id').first()
        except Exception:
            company = None

        form = VendorForm(company=company)
        
        # Check if this is an AJAX request for the modal
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            # Return just the form content for AJAX requests
            return render(request, 'Purchase/vendor_form.html', {
                'form': form,
                'title': 'Add New Vendor',
                'can_create_vendors': can_create_vendors(request.user),
                'can_edit_vendors': can_edit_vendors(request.user),
            })

    # For GET show/hide Save button based on permission
    return render(request, 'Purchase/vendor_form.html', {
        'form': form,
        'title': 'Add New Vendor',
        'can_create_vendors': can_create_vendors(request.user),
        'can_edit_vendors': can_edit_vendors(request.user),
    })

    
@login_required
def vendor_edit(request, pk):
    # Permission: require Edit on Vendor
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_vendors(request.user)):
            messages.error(request, 'You do not have permission to edit vendors.')
            return redirect_with_company('vendor_list')
    except Exception:
        messages.error(request, 'You do not have permission to edit vendors.')
        return redirect_with_company('vendor_list')

    vendor = get_object_or_404(Vendor, pk=pk)
    # determine company for currency choices
    try:
        cid = request.session.get('company_id')
        company = None
        if cid:
            company = Company.objects.filter(pk=cid).first()
        if not company:
            company = Company.objects.order_by('id').first()
    except Exception:
        company = None
    
    if request.method == "POST":
        print("=== SHIPPING DEBUG ===")
        print("shipping_address_line_1:", request.POST.get('shipping_address_line_1'))
        print("shipping_city:", request.POST.get('shipping_city'))
        print("shipping_country:", request.POST.get('shipping_country'))
        print("All POST keys:", list(request.POST.keys()))
        # Block POST if no edit permission
        if not can_edit_vendors(request.user):
            messages.error(request, "You do not have permission to edit vendors.")
            return redirect_with_company('vendor_list')

        form = VendorForm(request.POST, instance=vendor, company=company)
        if form.is_valid():
            vendor = form.save(commit=False)
            vendor.updated_by = request.user
            # ✅ Store request context for activity logging to find correct company_db
            vendor._current_request = request
            vendor.save()
            
            # Update contact persons
            from .models import ContactPerson
            # Delete existing contacts
            ContactPerson.objects.filter(vendor=vendor).delete()
            
            # Add new/updated contacts
            contact_names = request.POST.getlist('contact_name')
            contact_emails = request.POST.getlist('contact_email')
            contact_phones = request.POST.getlist('contact_phone')
            
            for name, email, phone in zip(contact_names, contact_emails, contact_phones):
                if (name and name.strip()) or (email and email.strip()) or (phone and phone.strip()):
                    ContactPerson.objects.create(
                        vendor=vendor,
                        name=(name or '').strip(),
                        email=(email or '').strip(),
                        phone=(phone or '').strip()
                    )
            
            # Import Customer models (use different alias to avoid conflict)
            from customer.models import Customer, ContactPerson as CustomerContact
            
            if vendor.is_customer:
                # Sync with customer if is_customer is checked
                existing_customer = Customer.objects.filter(email=vendor.email).first()
                if not existing_customer:
                    try:
                        new_customer = Customer.objects.create(
                            customer_code=vendor.vendor_code,
                            customer_type=vendor.vendor_type,
                            first_name=vendor.first_name,
                            last_name=vendor.last_name,
                            company_name=vendor.company_name,
                            email=vendor.email,
                            phone=vendor.phone,
                            mobile=vendor.mobile,
                            gst_number=vendor.tax_number,
                            address_line_1=vendor.address_line_1,
                            address_line_2=vendor.address_line_2,
                            city=vendor.city,
                            postal_code=vendor.postal_code,
                            state=vendor.state,
                            country=vendor.country,
                            shipping_address_line_1=vendor.shipping_address_line_1,
                            shipping_address_line_2=vendor.shipping_address_line_2,
                            shipping_city=vendor.shipping_city,
                            shipping_postal_code=vendor.shipping_postal_code,
                            shipping_state=vendor.shipping_state,
                            shipping_country=vendor.shipping_country,
                            currency=vendor.currency,
                            is_active=vendor.is_active,
                        )
                    except Exception as e:
                        print(f"Warning: Could not create customer for vendor {vendor.id}: {str(e)}")
                        messages.warning(request, f"Vendor updated, but customer creation failed: {str(e)}")
                        return redirect_with_company('vendor_list')

                    # Copy vendor contact persons to customer contacts
                    try:
                        cust_contacts = ContactPerson.objects.filter(vendor=vendor)
                        for cc in cust_contacts:
                            CustomerContact.objects.create(
                                customer=new_customer,
                                name=cc.name,
                                email=cc.email,
                                phone=cc.phone
                            )
                    except Exception as e:
                        print(f"Warning: Could not sync customer contacts for vendor {vendor.id}: {str(e)}")
                        messages.warning(request, f"Vendor updated, but syncing customer contacts failed: {str(e)}")
                elif vendor.currency and existing_customer.currency != vendor.currency:
                    existing_customer.currency = vendor.currency
                    existing_customer.save(update_fields=['currency'])
            
            messages.success(request, "Vendor updated successfully.")
            return redirect_with_company('vendor_list')
    else:
        form = VendorForm(instance=vendor, company=company)
    
    # Get existing contact persons
    contacts = vendor.contact_persons.all()
    
    return render(request, 'Purchase/vendor_form.html', {
        'form': form,
        'title': 'Edit Vendor',
        'contacts': contacts,
        'can_edit_vendors': can_edit_vendors(request.user),
    })


@require_POST
def vendor_delete(request, pk):
    # Permission: require Delete on Vendor
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_vendors(request.user)):
            messages.error(request, 'You do not have permission to delete vendors.')
            return redirect_with_company('vendor_list')
    except Exception:
        messages.error(request, 'You do not have permission to delete vendors.')
        return redirect_with_company('vendor_list')
    
    vendor = get_object_or_404(Vendor, pk=pk)
    try:
        vendor.is_active = False
        vendor.save(update_fields=["is_active"])
        messages.success(request, 'Vendor deleted successfully.')
    except ProtectedError:
        messages.error(request, 'Cannot delete this vendor because it is used in other records.')
    except IntegrityError:
        messages.error(request, 'Cannot delete this vendor because it is linked to other data.')
    except Exception as e:
        messages.error(request, f'Unable to delete vendor: {str(e)}')
    return redirect_with_company('vendor_list')
        

# def vendor_create(request):
#     if request.method == "POST":
#         form = VendorForm(request.POST)
#         if form.is_valid():
#             form.save()
#             return redirect('vendor_list')
#     else:
#         form = VendorForm()
#     return render(request, 'Purchase/vendor_form.html', {'form': form})



# def create_purchase_order(request):
#     if request.method == 'POST':
#         po_form = PurchaseOrderForm(request.POST)
#         if po_form.is_valid():
#             po = po_form.save()
#             # Items would be added in another step, e.g. using formsets
#             return redirect('add_purchase_order_items', po_id=po.id)
#     else:
#         po_form = PurchaseOrderForm()
#     return render(request, 'Purchase/create_po.html', {'po_form': po_form})

# def add_purchase_order_items(request, po_id):
#     po = get_object_or_404(PurchaseOrder, id=po_id)
#     ItemFormSet = forms.inlineformset_factory(PurchaseOrder, PurchaseOrderItem, form=PurchaseOrderItemForm, extra=1)
#     if request.method == 'POST':
#         formset = ItemFormSet(request.POST, instance=po)
#         if formset.is_valid():
#             formset.save()
#             # update total_amount
#             po.total_amount = sum([item.line_total() for item in po.items.all()])
#             po.save()
#             return redirect('po_detail', po_id=po.id)
#     else:
#         formset = ItemFormSet(instance=po)
#     return render(request, 'Purchase/add_po_items.html', {'formset': formset, 'po': po})

# def create_purchase_invoice(request):
#     if request.method == 'POST':
#         form = PurchaseInvoiceForm(request.POST, request.FILES)
#         if form.is_valid():
#             form.save()
#             return redirect('invoice_list')
#     else:
#         form = PurchaseInvoiceForm()
#     return render(request, 'Purchase/create_invoice.html', {'form': form})
# def create_purchase_order(request):
#     ItemFormSet = inlineformset_factory(
#         PurchaseOrder, PurchaseOrderItem, form=PurchaseOrderItemForm, extra=1, can_delete=True
#     )

#     if request.method == "POST":
#         po_form = PurchaseOrderForm(request.POST)
#         formset = ItemFormSet(request.POST)
#         if po_form.is_valid():
#             po = po_form.save(commit=False)
#             # Save PO now so items can FK to it (but don't commit Transaction if needed)
#             po.save()
#             formset = ItemFormSet(request.POST, instance=po)
#             if formset.is_valid():
#                 formset.save()
#                 return redirect('purchase_order_list')
#     else:
#         po_form = PurchaseOrderForm()
#         formset = ItemFormSet()

#     return render(request, 'Purchase/create_po.html', {
#         'po_form': po_form,
#         'formset': formset,
#     })

def vendor_add_modal(request):
    if request.method == 'POST':
        try:
            cid = request.session.get('company_id')
            company = None
            if cid:
                company = Company.objects.filter(pk=cid).first()
            if not company:
                company = Company.objects.order_by('id').first()
        except Exception:
            company = None

        form = VendorForm(request.POST, company=company)
        if form.is_valid():
            try:
                vendor_instance = form.save()
            except IntegrityError as e:
                err = str(e)
                if 'vendor_code' in err or 'vendor.vendor_code' in err or 'purchase_vendor.vendor_code' in err:
                    form.add_error('vendor_code', 'Vendor code already exists')
                else:
                    form.add_error(None, 'Database error: %s' % err)

                # Prepare formset for returning template so errors can be displayed
                try:
                    from .forms import ContactPersonFormSet
                    formset = ContactPersonFormSet(request.POST or None, prefix='contactperson')
                except Exception:
                    formset = None

                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False,
                        'html_form': render_to_string('Purchase/partial_vendor_form.html', {'form': form, 'formset': formset}, request=request)
                    })
                else:
                    return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset})

            # If contact persons are included (formset), save them as well
            try:
                from .forms import ContactPersonFormSet
                # The partial uses prefix='contactperson' for management form names
                contact_formset = ContactPersonFormSet(request.POST, instance=vendor_instance, prefix='contactperson')
                if contact_formset.is_valid():
                    contact_formset.save()
                else:
                    # If AJAX, return the form html with errors so client can render
                    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse({
                            'success': False,
                            'html_form': render_to_string('Purchase/partial_vendor_form.html', {'form': form, 'formset': contact_formset}, request=request)
                        })
            except Exception:
                # If the formset import or save fails, ignore to avoid breaking vendor creation
                pass

            # If vendor should also be a customer, create/sync Customer record (AJAX path)
            try:
                if getattr(vendor_instance, 'is_customer', False):
                    try:
                        from customer.models import Customer, ContactPerson as CustomerContactPerson
                        # avoid duplicate customer by email
                        customer_exists = Customer.objects.filter(email=vendor_instance.email).first()
                        if not customer_exists:
                            try:
                                lookup = {'email': vendor_instance.email} if getattr(vendor_instance, 'email', None) else {'customer_code': vendor_instance.vendor_code}
                                cust_defaults = {
                                    'customer_code': vendor_instance.vendor_code,
                                    'customer_type': vendor_instance.vendor_type if hasattr(vendor_instance, 'vendor_type') else '',
                                    'first_name': vendor_instance.first_name,
                                    'last_name': vendor_instance.last_name,
                                    'company_name': vendor_instance.company_name if hasattr(vendor_instance, 'company_name') else '',
                                    'phone': vendor_instance.phone,
                                    'mobile': vendor_instance.mobile if hasattr(vendor_instance, 'mobile') else '',
                                    'address_line_1': vendor_instance.address_line_1 if hasattr(vendor_instance, 'address_line_1') else '',
                                    'address_line_2': vendor_instance.address_line_2 if hasattr(vendor_instance, 'address_line_2') else '',
                                    'city': vendor_instance.city if hasattr(vendor_instance, 'city') else '',
                                    'state': vendor_instance.state if hasattr(vendor_instance, 'state') else '',
                                    'postal_code': vendor_instance.postal_code if hasattr(vendor_instance, 'postal_code') else '',
                                    'country': vendor_instance.country if hasattr(vendor_instance, 'country') else '',
                                    'gst_number': getattr(vendor_instance, 'tax_number', '') or getattr(vendor_instance, 'tax_number', ''),
                                    'currency': getattr(vendor_instance, 'currency', '') or 'INR',
                                    'opening_balance': getattr(vendor_instance, 'opening_balance', 0) or 0,
                                    'is_active': getattr(vendor_instance, 'is_active', True),
                                    'is_vendor': True,
                                    'updated_by': request.user,
                                }
                                cust, created = Customer.objects.update_or_create(
                                    defaults=cust_defaults,
                                    **lookup
                                )
                                if created:
                                    try:
                                        cust.created_by = request.user
                                        cust.save()
                                    except Exception:
                                        pass
                            except Exception:
                                # If customer creation fails, continue without breaking vendor creation
                                pass
                            # copy vendor contact persons to customer
                            try:
                                for cp in vendor_instance.contact_persons.all():
                                    CustomerContactPerson.objects.create(
                                        customer=cust,
                                        name=cp.name,
                                        email=cp.email,
                                        phone=cp.phone,
                                    )
                            except Exception:
                                pass
                        elif getattr(vendor_instance, 'currency', None) and customer_exists.currency != vendor_instance.currency:
                            customer_exists.currency = vendor_instance.currency
                            customer_exists.save(update_fields=['currency'])
                    except Exception:
                        # If customer creation fails, continue without breaking vendor creation
                        pass
            except Exception:
                pass

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                # Prefer first+last name for individuals; fall back to company_name
                try:
                    display_name = f"{vendor_instance.first_name or ''} {vendor_instance.last_name or ''}".strip()
                except Exception:
                    display_name = ''
                if not display_name:
                    display_name = vendor_instance.company_name if getattr(vendor_instance, 'company_name', None) else str(vendor_instance)

                return JsonResponse({
                    'success': True, 
                    'vendor_id': vendor_instance.id,
                    'vendor_name': display_name
                })
            else:
                # For non-AJAX requests, redirect to vendor list
                return redirect_with_company('vendor_list')
        else:
            # Form is not valid
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                # For AJAX requests, return the form with errors
                return JsonResponse({
                    'success': False,
                    'html_form': render_to_string('Purchase/partial_vendor_form.html', {'form': form}, request=request)
                })
            else:
                # For non-AJAX requests, render the form with errors
                return render(request, 'Purchase/partial_vendor_form.html', {'form': form})
    else:
        form = VendorForm()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            # For AJAX requests, return just the form and an empty contact formset
            try:
                from .forms import ContactPersonFormSet
                formset = ContactPersonFormSet(prefix='contactperson')
            except Exception:
                formset = None
            return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset})
        else:
            # For non-AJAX requests, render the full page with an empty formset
            try:
                from .forms import ContactPersonFormSet
                formset = ContactPersonFormSet(prefix='contactperson')
            except Exception:
                formset = None
            return render(request, 'Purchase/vendor_form.html', {'form': form, 'formset': formset})

def vendor_choices_partial(request):
    new_vendor_id = request.GET.get('selected_vendor_id')
    form = PurchaseOrderForm(initial={'vendor': new_vendor_id} if new_vendor_id else {})
    return render(request, 'Purchase/vendor_choices_partial.html', {'form': form})


def product_add_modal(request):
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        form = ItemForm(request.POST)
        if form.is_valid():
            new_product = form.save()
            # Return JSON with success and new product ID to refresh the select options
            return JsonResponse({'success': True, 'new_product_id': new_product.id})
        else:
            # Render the form with errors and send HTML back to the frontend
            html_form = render_to_string('items/product_form.html', {'form': form}, request=request)
            return JsonResponse({'success': False, 'html_form': html_form})

    else:
        # GET request: render empty form in modal
        form = ItemForm()
        return render(request, 'Purchase/partial_item_form.html', {'form': form})
def product_choices_partial(request):
    products = Item.objects.all()
    return render(request, "Purchase/product_choices_partial.html", {"products": products})

def pur(request):
    vendors = Vendor.objects.all()
    items = Item.objects.all()
    return render(request, 'Purchase/new_purchase.html', {
        "vendors": vendors,
        "items": items,
        "today": localdate().isoformat(),
        "purchase_no": f"PO-{PurchaseOrder.objects.count() + 1:05d}"
    })

# def purchase_order_list(request):
#     # Fetch all purchase orders, order by date desc
#     purchase_orders = PurchaseOrder.objects.all().order_by('-date')
    
#     context = {
#         'purchase_orders': purchase_orders,
#     }
#     return render(request, 'Purchase/purchase_order_list.html', context)

# def purchase_order_list(request):
#     search_query = request.GET.get('q', '').strip()
#     purchase_orders_qs = PurchaseOrder.objects.all().order_by('-date')

#     if search_query:
#         purchase_orders_qs = purchase_orders_qs.filter(
#             Q(vendor__name__icontains=search_query) |
#             Q(id__icontains=search_query) |
#             Q(status__icontains=search_query) |
#             Q(total_amount__icontains=search_query)
#         )

#     paginator = Paginator(purchase_orders_qs, 10)
#     page_number = request.GET.get('page')
#     purchase_orders = paginator.get_page(page_number)

#     context = {
#         'purchase_orders': purchase_orders,
#         'search_query': search_query,
#     }

#     if request.headers.get('x-requested-with') == 'XMLHttpRequest':
#         # Render only the table and pagination (partial)
#         return render(request, 'Purchase/purchase_order_list.html', context)

#     # Full render for normal request
#     return render(request, 'Purchase/purchase_order_list.html', context)

def pur(request):
    vendors = Vendor.objects.all()
    items = Item.objects.all()
    return render(request, 'Purchase/new_purchase.html', {
        "vendors": vendors,
        "items": items,
        "today": localdate().isoformat(),
        "purchase_no": f"PO-{PurchaseOrder.objects.count() + 1:05d}"
    })


# def save_purchase_order(request):
#     if request.method == "POST":
#         print(request.POST.keys())

#         # Basic fields
#         vendor_id = request.POST.get('vendor')
#         bill_date = request.POST.get('bill_date')
#         delivery_date = request.POST.get('delivery_date')
#         pay_term_id = request.POST.get('pay-terms')
#         notes = request.POST.get('notes')
#         # Validate everything: vendor, pay_term
#         if not vendor_id or not bill_date:  # more checks as needed
#             return render(request, "error.html", {"msg": "Vendor and Bill Date required"})
#         vendor = Vendor.objects.get(pk=vendor_id)
#         pay_term = PayTerms.objects.get(pk=pay_term_id) if pay_term_id else None
#         # Create PurchaseOrder header
#         order = PurchaseOrder.objects.create(
#             vendor=vendor,
#             date=bill_date,
#             delivery_date=delivery_date,
#             payment_term=pay_term,
#             notes=notes,
#         )
#         # Loop item rows
#         i = 0
#         line_items = []
#         while f"items[{i}][id]" in request.POST:
#             # item_id = request.POST.get(f"items[{i}][id]")
#             item_str = request.POST.get(f"items[{i}][id]", "").strip() 
#             itm_id, brcd = item_str.split("_", 1)
#             desc = request.POST.get(f"items[{i}][description]")
#             # qty = request.POST.get(f"items[{i}][qty]")
#             # price = request.POST.get(f"items[{i}][price]")
#             qty = request.POST.get(f"items[{i}][qty]") or 0
#             price = request.POST.get(f"items[{i}][price]") or 0
#             tax_id = request.POST.get(f"items[{i}][tax]")
#             discount = request.POST.get(f"items[{i}][discount]") or 0
#             discount_type = request.POST.get(f"items[{i}][discount_type]")

#             if not item_str or float(qty) <= 0 or float(price) <= 0:
#                 i += 1
#                 continue

#             try:
#                 # item = Item.objects.get(pk=item_id)
#                 item = Item.objects.get(pk=int(itm_id))

#             except Item.DoesNotExist:
#                 i += 1
#                 continue

#             # if not item_id or not qty or not price:
#             #     i += 1; continue
#             # try:
#             #     item = Item.objects.get(pk=item_id)
#             # except:
#             #     i += 1; continue
#             qty = float(qty)
#             price = float(price)
#             discount = float(discount) if discount else 0
#             tax = TaxGroup.objects.get(pk=tax_id) if tax_id else None
#             # Do calculation logic as per your JS for final amount
#             # base = qty * price
#             # discounted = base - (base * discount / 100 if discount_type == "percent" else discount)
#             # tax_amt = discounted * (tax.rate / 100) if tax else 0
#             # amount = discounted + tax_amt
#             print("Saving item:", item_str, qty, price)
#             PurchaseOrderItem.objects.create(
#                 purchase_order=order,
#                 product=item,
#                 prd_brcd=brcd,
#                 prd_disvalue=discount,
#                 prd_distype=discount_type,
#                 quantity=int(qty),
#                 price=float(price),
#             )

#             i += 1
#         # Optionally: recalculate totals on order here, as security (not just trusting JS)
#         # Redirect or render success
#         # return redirect("purchase_order_success")
#         messages.success(request, "Purchase Order created successfully!")
#         return redirect('purchase_order_list')

#     return render(request, "Purchase/new_purchase.html")


def get_item(request):
    query = request.GET.get('q', '')
    results = []
    company_db = getattr(request, "company_db", "default")
    company_tax_type = (get_company_tax_type(using=company_db) or "").strip().upper()
    if query:
        # Check if query is a barcode, get matching item ids
        matching_item_ids = Barcode.objects.filter(barcode=query).values_list('item_id', flat=True)

        # Filter items by name or barcode match
        items = Item.objects.filter(Q(name__icontains=query) | Q(id__in=matching_item_ids),purchase_info=1).distinct()[:50]

        for h in items:
            price = Decimal(h.cost_price)
            o_price = Decimal(h.cost_price)
            total_tax_rate = Decimal("0.00")
            selected_tax_token = None
            selected_tax_name = ""
            if company_tax_type == "SALES":
                tax_obj = getattr(h, "purchase_tax", None)
                if tax_obj:
                    total_tax_rate = Decimal(tax_obj.rate or 0)
                    selected_tax_token = str(tax_obj.id)
                    selected_tax_name = getattr(tax_obj, "display_name", None) or getattr(tax_obj, "taxname", "") or ""
            else:
                if h.intra_tax:
                    total_tax_rate = h.intra_tax.taxes.aggregate(total=Sum("rate"))["total"] or Decimal("0.00")
                    selected_tax_token = f"group:{h.intra_tax.id}"
                    selected_tax_name = h.intra_tax.group_name if h.intra_tax else ""

            unit_name = ""
            if h.unit:
                try:
                    unit_name = Unit.objects.get(id=h.unit).unit_name
                except Unit.DoesNotExist:
                    unit_name = ""

            barcode = ""
            if h.main_barcode_id:
                try:
                    barcode = Barcode.objects.get(id=h.main_barcode_id).barcode
                except:
                    barcode = ""

            # If query matches a barcode, filter UOMs with that barcode only,
            # else include all UOMs for the item
            if Barcode.objects.filter(barcode=query).exists():
                item_uoms = Uom.objects.filter(
                    item_id=h.id,
                    barcode__barcode=query
                ).select_related('barcode')
            else:
                item_uoms = Uom.objects.filter(item_id=h.id).select_related('barcode')

            # Add main item only if query is not an exact barcode match for UOM
            # or if it's an exact barcode match but barcode is main barcode
            if not Barcode.objects.filter(barcode=query).exists() or barcode == query:
                # Adjust price if GST is included
                adjusted_price = price
                if h.taxincld_costprice:
                    tax_amount = price * (total_tax_rate / Decimal("100"))
                    adjusted_price = price - tax_amount

                results.append({
                    "id": h.id,
                    "name": h.name,
                    "o_price": float(o_price.quantize(Decimal("0.01"))),
                    "sl_price": float(adjusted_price.quantize(Decimal("0.01"))),
                    "unit": unit_name,
                    "barcode": barcode,
                    "sell_desc": h.purchase_desc,
                    'gstinclude': h.taxincld_costprice,
                    "tax_id": selected_tax_token,
                    "tax_name": selected_tax_name,
                    "tax_rate": float(total_tax_rate),
                })

            # Add matched UOMs
            for uom in item_uoms:
                price_uom = price * Decimal(uom.conversion_factor)
                o_price_uom = price_uom
                # Adjust price for GST if needed
                adjusted_price_uom = price_uom
                if h.taxincld_costprice:
                    tax_amount = price_uom * (total_tax_rate / Decimal("100"))
                    adjusted_price_uom = price_uom - tax_amount

                results.append({
                    "id": f"{h.id}_{uom.barcode.barcode if uom.barcode else ''}",
                    # "name": f"{h.name} ({uom.name_id})",
                    "name": f"{h.name}",

                    "o_price": float(o_price_uom.quantize(Decimal("0.01"))),

                    "sl_price": float(adjusted_price_uom.quantize(Decimal('0.01'))),
                    "unit": uom.name.name,
                    "barcode": uom.barcode.barcode if uom.barcode else '',
                    "sell_desc": h.purchase_desc,
                    'gstinclude': h.taxincld_costprice,
                    "tax_id": selected_tax_token,
                    "tax_name": selected_tax_name,
                    "tax_rate": float(total_tax_rate),

                })

    print(results)

    return JsonResponse(results, safe=False)
#added by neha on 17-12-25
def purchase_order_add(request):
    # Permission: require Create on Purchase Order
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_orders(request.user)):
            messages.error(request, 'You do not have permission to create purchase orders.')
            return redirect_with_company('purchase_order_list')
    except Exception:
        messages.error(request, 'You do not have permission to create purchase orders.')
        return redirect_with_company('purchase_order_list')

    # Create form instance for Salesorder
    order_form = PurchaseOrderForm()
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    # Create a formset for SalesorderItem if you plan multiple items
    PurchaseOrderItemFormSet = formset_factory(PurchaseOrderItemForm, extra=1)
    purchase_formset = PurchaseOrderItemFormSet()
    all_items = Item.objects.all()
    # If opened from a lead or opportunity, keep id so save can link back
    

    # Pass both to the template
    company = _get_company_for_request(request)
    from currencies.models import Currency
    company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
    company_tax_type = _get_purchase_company_tax_type(request)
    # Pass both to the template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    return render(request, 'Purchase/Purchaseorder_add.html', {
        'order_form': order_form,
        'item_formset': item_formset,
        'purchase_formset': purchase_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': f"PQ-{PurchaseOrder.objects.count() + 1:05d}",
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
                # ✅ Fetch TDS and TCS for quotation_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=company, is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=company, is_active=True),
        'show_base_transaction_summary': bool(getattr(company, 'show_base_transaction_summary', True)),
    })


#updt by neha on 5-2-26 for also listing companies
def vendor_search(request):
    query = request.GET.get('q', '').strip()
    results = []
    company_db = getattr(request, 'company_db', 'default')

    vendors = (
        Vendor.objects.using(company_db)
        .annotate(
            fullname=Concat('first_name', Value(' '), 'last_name')
        )
    )
    if query:
        vendors = vendors.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(company_name__icontains=query) |
            Q(fullname__icontains=query)
        )[:50]
    else:
        # No search term yet: show a default top-5 list so the dropdown isn't
        # empty as soon as it's opened (mirrors Items.views.vendor_search).
        vendors = vendors[:5]

    for v in vendors:
        # Display name based on vendor type
        if v.vendor_type == 'company':
            display_name = v.company_name or v.email or str(v.id)
        else:  # individual
            display_name = f"{v.first_name or ''} {v.last_name or ''}".strip() or v.email or str(v.id)

        results.append({
            "id": v.id,
            "name": display_name,
            "payment_terms_id": v.payment_terms_id,
            "payment_terms_name": v.payment_terms.name if v.payment_terms else "",
            "payment_terms_days": v.payment_terms.days if v.payment_terms else None,
        })

    return JsonResponse(results, safe=False)
    
def create_vendor_ajax(request):
    # Default currency to organisation base where possible
    try:
        cid = request.session.get('company_id')
        company = None
        if cid:
            company = Company.objects.filter(pk=cid).first()
        if not company:
            company = Company.objects.order_by('id').first()
        default_currency = (company.base_currency or '').strip().upper()[:10] if company else ''
    except Exception:
        default_currency = ''

    form = VendorFormmodal(company=company)
    context = {'vendor_form': form}
    # add other context as needed
    return render(request, 'Purchase/add_vendor.html', context)
    
#updt by neha on 5-2-26
def add_vendor(request):
    
    # determine company for currency choices
    try:
        cid = request.session.get('company_id')
        company = None
        if cid:
            company = Company.objects.filter(pk=cid).first()
        if not company:
            company = Company.objects.order_by('id').first()
    except Exception:
        company = None

    if request.method != "POST":
        form = VendorFormmodal(company=company)
        return render(request, 'Purchase/add_vendor.html', {'vendor_form': form})

    if request.method == "POST":
        form = VendorFormmodal(request.POST, company=company)
        if form.is_valid():
            vendor = form.save(commit=False)
            vendor.is_active = True  # Set default active status for new vendors
            # if not vendor.postal_code:
            #     vendor.postal_code = ''  # or generate a default
            # if not vendor.opening_balance:
            #     vendor.opening_balance = 0
            # if not vendor.city:
            #     vendor.city = ''
            # if not vendor.state:
            #     vendor.state = ''
            # if not vendor.country:
            #     vendor.country = ''

            try:
                vendor.created_by = request.user
                vendor._current_request = request

                vendor.updated_by = request.user
            except Exception:
                pass
            # Ensure vendor's currency defaults to organisation base currency if not provided
            try:
                from company.models import Company
                cid = request.session.get('company_id')
                company = None
                if cid:
                    company = Company.objects.filter(pk=cid).first()
                if not company:
                    company = Company.objects.order_by('id').first()
                if company and (not getattr(vendor, 'currency', '') or str(vendor.currency).strip() == ''):
                    vendor.currency = (company.base_currency or '').strip().upper()[:10]
            except Exception:
                pass
            vendor.save()

            # If is_vendor checked, create vendor record
            if getattr(vendor, 'is_customer', False) and getattr(vendor, 'email', None):
                try:
                    from sales.models import Customer
                    customer_exists = Customer.objects.filter(email=vendor.email).exists()
                    if not customer_exists:
                        Customer.objects.create(
                            customer_code=getattr(vendor, 'vendor_code', ''),
                            custoemer_type=getattr(vendor, 'vendor_type', ''),
                            first_name=vendor.first_name,
                            last_name=vendor.last_name,
                            company_name=getattr(vendor, 'company_name', ''),
                            email=vendor.email,
                            phone=vendor.phone,
                            mobile=getattr(vendor, 'mobile', ''),
                            address_line_1=getattr(vendor, 'address_line_1', ''),
                            address_line_2=getattr(vendor, 'address_line_2', ''),
                            city=getattr(vendor, 'city', ''),
                            state=getattr(vendor, 'state', ''),
                            postal_code=getattr(vendor, 'postal_code', ''),
                            country=getattr(vendor, 'country', ''),
                            gst_number=getattr(vendor, 'tax_number', ''),
                            currency=getattr(vendor, 'currency', '') or 'INR',
                            is_active=getattr(vendor, 'is_active', True),
                            is_vendor=True,
                            created_by=request.user,
                            updated_by=request.user,
                        )
                    else:
                        customer = Customer.objects.filter(email=vendor.email).first()
                        if customer and getattr(vendor, 'currency', None) and customer.currency != vendor.currency:
                            customer.currency = vendor.currency
                            customer.save(update_fields=['currency'])
                except Exception:
                    logger.exception('Failed to create vendor record for vendor via AJAX')

            # display_name = f"{getattr(vendor, 'first_name', '')} {getattr(vendor, 'last_name', '')}".strip() or getattr(vendor, 'company_name', '') or vendor.email or str(vendor.id)
            if vendor.vendor_type == 'company':
                display_name = vendor.company_name or vendor.email or str(vendor.id)
            else:  # individual
                display_name = f"{vendor.first_name or ''} {vendor.last_name or ''}".strip() or vendor.email or str(vendor.id)
            return JsonResponse({'success': True, 'id': vendor.id, 'name': display_name})
        else:
                    return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)


def get_item_purchase(request):
    query = request.GET.get('q', '')
    vendor_id = request.GET.get('vendor_id')
    results = []
    company_db = getattr(request, "company_db", "default")
    company_tax_type = (get_company_tax_type(using=company_db) or "").strip().upper()
    if query:
        # Check if query is a barcode, get matching item ids
        matching_item_ids = Barcode.objects.filter(barcode=query).values_list('item_id', flat=True)

        # Filter items by name or barcode match
        items = Item.objects.filter(
            Q(name__icontains=query) | Q(id__in=matching_item_ids),
            purchase_info=1,
            status=1,
        )
        if vendor_id:
            items = items.filter(preferred_vendor_id=vendor_id)
        items = items.distinct()[:50]

        for h in items:
            # price = Decimal(h.cost_price)
            price = Decimal(h.cost_price) 

            o_price = Decimal(h.cost_price) 
            total_tax_rate = Decimal("0.00")
            selected_tax_id = None
            selected_tax_name = ""
            if company_tax_type == "SALES":
                tax_obj = getattr(h, "purchase_tax", None)
                if tax_obj:
                    total_tax_rate = Decimal(tax_obj.rate or 0)
                    selected_tax_id = tax_obj.id
                    selected_tax_name = getattr(tax_obj, "display_name", None) or getattr(tax_obj, "taxname", "") or ""
            else:
                if h.intra_tax:
                    total_tax_rate = h.intra_tax.taxes.aggregate(total=Sum("rate"))["total"] or Decimal("0.00")
                    selected_tax_id = h.intra_tax.id
                    selected_tax_name = h.intra_tax.group_name if h.intra_tax else ""

            unit_name = ""
            if h.unit:
                try:
                    unit_name = Unit.objects.get(id=h.unit).unit_name
                except Unit.DoesNotExist:
                    unit_name = ""

            barcode = ""
            barcode_id = None
            barcode_str = ""

            # Try to get barcode object matching the query and the item (any barcode for that item)
            barcode_obj = Barcode.objects.filter(barcode=query, item_id=h.id).first()
            if barcode_obj:
                barcode_id = barcode_obj.id
                barcode_str = barcode_obj.barcode
            else:
                # fallback to main barcode of the item
                barcode_id = h.main_barcode_id
                if h.main_barcode_id:
                    try:
                        barcode = Barcode.objects.get(id=h.main_barcode_id).barcode
                        barcode_id = h.main_barcode_id
                    except:
                        barcode = ""
            # barcode_obj = Barcode.objects.filter(barcode=query, item_id=h.id).first()
            # # barcode_id = barcode_obj.id if barcode_obj else None
            # #print(f"Item {h.id} main_barcode_id: {h.main_barcode_id}")
            # #print(f"Searching Barcode for query: {query}, item_id: {h.id}")
            # #print(f"Found barcode_obj: {barcode_obj}")
            # If query matches a barcode, filter UOMs with that barcode only,
            # else include all UOMs for the item
            if Barcode.objects.filter(barcode=query).exists():
                item_uoms = Uom.objects.filter(
                    item_id=h.id,
                    barcode__barcode=query
                ).select_related('barcode')
            else:
                item_uoms = Uom.objects.filter(item_id=h.id).select_related('barcode')

            # Add main item only if query is not an exact barcode match for UOM
            # or if it's an exact barcode match but barcode is main barcode
            if not Barcode.objects.filter(barcode=query).exists() or barcode == query:
                # Adjust price if GST is included
                adjusted_price = price
                print("adjusted_price1",adjusted_price)

            
                if h.taxincld_costprice:
                    # tax_amount = price * (total_tax_rate / Decimal("100"))
                    # adjusted_price = price - tax_amount
                    gst_multiplier = Decimal("1") + (total_tax_rate / Decimal("100"))
                    adjusted_price = (price / gst_multiplier).quantize(Decimal("0.01"))
                    print("adjusted_price2",adjusted_price)

                results.append({
                    "id": h.id,
                    "name": h.name,
                    "o_price": float(adjusted_price.quantize(Decimal("0.01"))),
                    "sl_price": float(adjusted_price.quantize(Decimal("0.01"))),
                    "unit": unit_name,
                    "barcode": barcode,
                    "b_id": barcode_id,
                    "sell_desc": h.sales_desc,
                    'gstinclude': h.taxincld_costprice,
                    "tax_id": selected_tax_id,
                    "tax_name": selected_tax_name,
                    "tax_rate": float(total_tax_rate),
                    "tax_pref":h.tax_pref,
                })

            if vendor_id:
                continue

            # Add matched UOMs for generic item search.
            for uom in item_uoms:
                price_uom = price * Decimal(uom.conversion_factor)
                o_price_uom = price_uom
                # Adjust price for GST if needed
                adjusted_price_uom = price_uom
                if h.taxincld_costprice:
                    gst_multiplier = Decimal("1") + (total_tax_rate / Decimal("100"))
                    adjusted_price_uom = (price_uom / gst_multiplier).quantize(Decimal("0.01"))

                results.append({
                    "id": f"{h.id}",
                    "name": f"{h.name}",
                    "o_price": float(adjusted_price_uom.quantize(Decimal("0.01"))),
                    "sl_price": float(adjusted_price_uom.quantize(Decimal('0.01'))),
                    "unit": uom.name.name,
                    "barcode": uom.barcode.barcode if uom.barcode else '',
                    "b_id": uom.barcode.id if uom.barcode else None,
                    "sell_desc": h.sales_desc,
                    'gstinclude': h.taxincld_costprice,
                    "tax_id": selected_tax_id,
                    "tax_name": selected_tax_name,
                    "tax_rate": float(total_tax_rate),
                    "tax_pref":h.tax_pref,
                })

    else:
        # No search term yet: show a default top-5 list so the dropdown isn't
        # empty as soon as it's opened (mirrors search_unit / hsn_code_search).
        default_items = Item.objects.filter(purchase_info=1, status=1)
        if vendor_id:
            default_items = default_items.filter(preferred_vendor_id=vendor_id)
        default_items = default_items.distinct()[:5]

        for h in default_items:
            price = Decimal(h.cost_price)
            total_tax_rate = Decimal("0.00")
            selected_tax_id = None
            selected_tax_name = ""
            if company_tax_type == "SALES":
                tax_obj = getattr(h, "purchase_tax", None)
                if tax_obj:
                    total_tax_rate = Decimal(tax_obj.rate or 0)
                    selected_tax_id = tax_obj.id
                    selected_tax_name = getattr(tax_obj, "display_name", None) or getattr(tax_obj, "taxname", "") or ""
            else:
                if h.intra_tax:
                    total_tax_rate = h.intra_tax.taxes.aggregate(total=Sum("rate"))["total"] or Decimal("0.00")
                    selected_tax_id = h.intra_tax.id
                    selected_tax_name = h.intra_tax.group_name if h.intra_tax else ""

            unit_name = ""
            if h.unit:
                try:
                    unit_name = Unit.objects.get(id=h.unit).unit_name
                except Unit.DoesNotExist:
                    unit_name = ""

            adjusted_price = price
            if h.taxincld_costprice:
                gst_multiplier = Decimal("1") + (total_tax_rate / Decimal("100"))
                adjusted_price = (price / gst_multiplier).quantize(Decimal("0.01"))

            results.append({
                "id": h.id,
                "name": h.name,
                "o_price": float(adjusted_price.quantize(Decimal("0.01"))),
                "sl_price": float(adjusted_price.quantize(Decimal("0.01"))),
                "unit": unit_name,
                "barcode": "",
                "b_id": h.main_barcode_id,
                "sell_desc": h.sales_desc,
                'gstinclude': h.taxincld_costprice,
                "tax_id": selected_tax_id,
                "tax_name": selected_tax_name,
                "tax_rate": float(total_tax_rate),
                "tax_pref": h.tax_pref,
            })

    # #print(results)

    return JsonResponse(results, safe=False)

# def generate_order_number():
#     prefix_obj = OrderPrefix.objects.first()
#     prefix = prefix_obj.prefix if prefix_obj else "QN"

#     # Fetch all order_numbers
#     all_orders = PurchaseOrder.objects.values_list('order_number', flat=True)

#     max_number = 0
#     pattern = re.compile(r'(\d+)')  # Extract digits anywhere in the string

#     for q in all_orders:
#         match = pattern.search(q)
#         if match:
#             num = int(match.group(1))
#             if num > max_number:
#                 max_number = num

#     new_number = max_number + 1
#     return f"{prefix}{str(new_number).zfill(3)}"

def save_purchaseorder(request):
    
    if request.method == "POST":
        # Permission: require Create on Purchase Order
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_orders(request.user)):
                messages.error(request, 'You do not have permission to create orders.')
                return redirect_with_company('purchase_order_list')
        except Exception:
            messages.error(request, 'You do not have permission to create orders.')
            return redirect_with_company('purchase_order_list')
        try:
            # wrap entire POST handling to catch and log unexpected errors
            
            
            post_data = request.POST.copy()  # make mutable copy
            prd_brcd_map = {}
        except Exception as e:
            logger.exception("Unexpected error in save_purchaseorder: %s", e)
            messages.error(request, "An unexpected error occurred while saving the order first. See server log for details.")
            return redirect_with_company('purchase_order_list')
        
        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
        # post_data and prd_brcd_map were prepared above
        for key in post_data:
            # Identify product field keys
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                # #print(f"Key matched: {key} with value: '{value}'")
                if value:
                    parts = value.split("_", 1)
                    post_data[key] = parts[0]  # Save only item id for product field
                    # #print("Updated post_data[key]:", post_data[key])
                    if len(parts) > 1:
                        # Map barcode corresponding to this form prefix
                        prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
                        prd_brcd_map[prefix] = parts[1]
                        # #print("prd_brcd_map[prefix]:",prd_brcd_map[prefix])

        company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)
        # Remove blank / deleted rows posted by the client before binding formset
        post_data = _compact_purchase_item_formset_post_data(post_data)
        post_data = _compact_purchase_item_formset_post_data(post_data)

        vendor_id = request.POST.get('vendor')
        date = request.POST.get('date')
        
       
        notes = request.POST.get('notes', '')
        order_number = generate_order_number()
        # #print("order_number:",order_number)
        discount_value = request.POST.get('grand-discount-value', 0)
        discount_type = request.POST.get('discount_type', 'percent')
        
        

        if not vendor_id or not date:
            messages.error(request, "Vendor and order Date are required.")
            return redirect_with_company('purchase_order_list')

        try:
            vendor = Vendor.objects.get(pk=vendor_id)
        except Vendor.DoesNotExist:
            messages.error(request, "Selected vendor was not found.")
            return redirect_with_company('purchase_order_list')
        except Exception as e:
            logger.exception("Unexpected exception while loading PurchaseOrder vendor: %s", e)
            messages.error(request, "An unexpected error occurred while saving the order second. See server log for details.")
            return redirect_with_company('purchase_order_list')

        PurchaseOrderItemFormSet = modelformset_factory(
            PurchaseOrderItem, form=PurchaseOrderItemForm, extra=0, can_delete=True
        )

        if int(post_data.get('form-TOTAL_FORMS', '0') or '0') <= 0:
            messages.error(request, "Add at least one valid item to the purchase order.")
            return redirect_with_company('purchase_order_list')

        formset = PurchaseOrderItemFormSet(post_data, queryset=PurchaseOrderItem.objects.none())

        if formset.is_valid():
            try:
                with transaction.atomic():
                    order = PurchaseOrder.objects.create(
                        vendor=vendor,
                        date=date,
                        order_number=order_number,
                        total_amount=0,
                        notes=notes,
                        discount_value=discount_value,
                        discount_type=discount_type,
                        shipping_attention=request.POST.get('shipping_attention', ''),
                        shipping_email=request.POST.get('shipping_email', ''),
                        shipping_phone=request.POST.get('shipping_phone', ''),
                        shipping_country=request.POST.get('shipping_country', ''),
                        shipping_address1=request.POST.get('shipping_address1', ''),
                        shipping_address2=request.POST.get('shipping_address2', ''),
                        shipping_city=request.POST.get('shipping_city', ''),
                        shipping_state=request.POST.get('shipping_state', ''),
                        shipping_postal_code=request.POST.get('shipping_postal_code', ''),
                        place_of_supply=request.POST.get('place_of_supply', ''),
                        tds_tcs_type=request.POST.get('tds_tcs_type', 'tds'),
                        tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
                        tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
                        tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
                    )

                    order._current_user = request.user
                    order._current_request = request

                    pay_term_id = request.POST.get('payment_term')
                    if pay_term_id:
                        try:
                            order.payment_term = PayTerms.objects.get(pk=pay_term_id)
                        except PayTerms.DoesNotExist:
                            pass

                    items = formset.save(commit=False)
                    calculated_total = Decimal('0.00')
                    for index, item in enumerate(items):
                        
                        item.pk = None

                        prefix = f"form-{index}"
                        if prefix in prd_brcd_map:
                            item.prd_brcd = prd_brcd_map[prefix]
                        item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')

                        selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
                        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                        try:
                            item.o_price = Decimal(str(post_data.get(f'{prefix}-o_price', '0') or '0'))
                        except Exception:
                            item.o_price = Decimal('0.00')
                        item.purchase_order = order
                        item.save()

                        try:
                            qty = Decimal(item.quantity)
                            price = Decimal(item.price)
                        except Exception:
                            qty = Decimal('0')
                            price = Decimal('0')

                        base = qty * price
                        discount_val = Decimal(item.prd_disvalue or 0)
                        if item.prd_distype == 'percent':
                            discounted = base - (base * discount_val / Decimal('100'))
                        else:
                            discounted = base - discount_val
                        if discounted < 0:
                            discounted = Decimal('0')

                        tax_amt = Decimal('0')
                        if item.prd_tax:
                            tax_amt = (discounted * Decimal(item.prd_tax)) / Decimal('100')

                        line_total = discounted + tax_amt
                        calculated_total += line_total

                    for deleted_item in formset.deleted_objects:
                        deleted_item.delete()

                    grand_discount_value = Decimal(str(request.POST.get('grand-discount-value', 0)))
                    grand_discount_type = request.POST.get('discount_type', 'percent')
                    if grand_discount_type == 'percent':
                        grand_discount = calculated_total * grand_discount_value / Decimal('100')
                    else:
                        grand_discount = grand_discount_value
                    if grand_discount > calculated_total:
                        grand_discount = calculated_total

                    order.total_amount = calculated_total - grand_discount
                    order.discount_value = grand_discount_value
                    order.discount_type = grand_discount_type
                    order.save()

                    company = _get_company_for_request(request)
                    from currencies.services import apply_purchase_order_fx, parse_currency_fx_from_post

                    doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
                    apply_purchase_order_fx(
                        order,
                        company,
                        document_currency=doc_cur,
                        fx_rate_to_base_override=rate_override,
                        fx_rate_date_override=fx_date,
                    )
            except IntegrityError as e:
                if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
                    messages.error(request, f"Order Number '{order_number}' already exists. Please choose a different one.")
                else:
                    logger.exception("IntegrityError while creating PurchaseOrder: %s", e)
                    messages.error(request, "An error occurred while saving the order.")
                return redirect_with_company('purchase_order_list')
            except Exception:
                logger.exception('Failed to save purchase order after item validation')
                messages.error(request, "An unexpected error occurred while saving the order. See server log for details.")
                return redirect_with_company('purchase_order_list')
            messages.success(request, "Purchase order created successfully!")
            # If this order was created from a lead or opportunity, create a CRM Update linking them

            # Redirect to order detail page after save
            try:
                url = reverse('purchaseorder_detail', args=[order.pk])
                return redirect_with_company(url)
            except Exception:
                return redirect_with_company('purchase_order_list')
        else:
            # # #print("Formset errors:", formset.errors)
            # for form in formset:
            #     #print("Individual form errors:", form.errors)
            logger.error("Purchase order formset invalid: errors=%s non_form_errors=%s", formset.errors, formset.non_form_errors())
            messages.error(request, "There are errors with the items in the order. See server log for details.")
            return redirect_with_company('purchase_order_list')
    else:
        return redirect_with_company('purchase_order_list')

def purchaseorder_detail(request, pk):
    """Render a simple readonly detail page for a order."""
    # Permission: require view access to Purchase Order
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_orders(request.user)):
            messages.error(request, 'You do not have permission to view purchase orders.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view purchase orders.')
        return redirect_with_company('index')

    order = get_object_or_404(PurchaseOrder.objects.select_related('vendor', 'payment_term', 'document_currency'), pk=pk)
    if order.status == 'Sent':
        order.status = 'Issued'
        order.save(update_fields=['status'])
    elif order.status == 'Open':
        order.status = 'Issued'
        order.save(update_fields=['status'])
    elif order.status == 'Closed':
        order.status = 'Billed'
        order.save(update_fields=['status'])
    company = _get_company_for_request(request)
    try:
        from currencies.services import get_base_currency, resolve_currency_for_vendor
        document_currency = order.document_currency or resolve_currency_for_vendor(order.vendor, company)
        base_currency = get_base_currency(company)
    except Exception:
        document_currency = order.document_currency
        base_currency = None

    document_currency_symbol = (
        (getattr(document_currency, 'symbol', '') or getattr(document_currency, 'code', '')).strip()
        if document_currency else ''
    )
    document_currency_code = getattr(document_currency, 'code', '') if document_currency else ''
    company_base_currency_symbol = (
        (getattr(base_currency, 'symbol', '') or getattr(base_currency, 'code', '')).strip()
        if base_currency else '₹'
    )
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''

    try:
        fx_rate = Decimal(str(order.fx_rate_to_base or '1'))
    except Exception:
        fx_rate = Decimal('1.000000')
    if fx_rate <= 0:
        fx_rate = Decimal('1.000000')

    # ✅ same-currency detection as bill_detail: only show the base-currency
    # columns when the vendor's currency actually differs from the
    # company's base currency, to avoid two redundant, identical-looking
    # sets of columns when they match. Compare the actual currency records
    # first (most reliable — avoids any mismatch if duplicate/legacy
    # currency rows share a code or symbol), then fall back to code, then
    # symbol, then finally the fx rate itself.
    if document_currency is not None and base_currency is not None and getattr(document_currency, 'pk', None) == getattr(base_currency, 'pk', None):
        show_base_currency_columns = False
    elif document_currency_code and company_base_currency_code:
        show_base_currency_columns = (document_currency_code != company_base_currency_code)
    elif document_currency_symbol and company_base_currency_symbol:
        show_base_currency_columns = (document_currency_symbol != company_base_currency_symbol)
    else:
        show_base_currency_columns = abs(fx_rate - Decimal('1')) >= Decimal('0.000001')

    tds_tcs_type = (order.tds_tcs_type or '').strip().lower()
    tds_tcs_amount = Decimal(str(order.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate
    def _to_base(amount):
        try:
            return (Decimal(str(amount or 0)) * fx_rate).quantize(Decimal('0.01'))
        except Exception:
            return Decimal('0.00')

    # Use a simple formset to iterate items if needed in template
    PurchaseOrderItemFormSet = modelformset_factory(PurchaseOrderItem, form=PurchaseOrderItemForm, extra=0)
    existing_items_qs = PurchaseOrderItem.objects.filter(purchase_order=order)
    purchase_formset = PurchaseOrderItemFormSet(queryset=existing_items_qs)
    # Compute per-line amounts and totals (mirror save logic)
    items_info = []
    subtotal_calc = Decimal('0.00')
    subtotal_calc_base = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    total_item_discount = Decimal('0.00')
    total_item_discount_base = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)

        if item.prd_distype == 'percent':
            discounted = base - (base * discount_val / Decimal('100'))
        else:
            discounted = base - discount_val

        if discounted < 0:
            discounted = Decimal('0.00')

        # Calculate discount amount for display
        discount_amount = base - discounted

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        # line_total = discounted + tax_amount

        #  NEW: line_total is ONLY the discounted amount (no tax)
        line_total = discounted

        # subtotal_calc += line_total

        #  NEW: Subtotal accumulates discounted amounts (before tax)
        subtotal_calc += discounted
        
        base_unit_price = Decimal('0.00')
        try:
            base_unit_price = Decimal(str(getattr(item, 'o_price', 0) or 0))
        except Exception:
            base_unit_price = Decimal('0.00')
        if base_unit_price <= 0:
            base_unit_price = _to_base(price)
        if base_unit_price <= 0:
            try:
                base_unit_price = Decimal(str(getattr(item.product, 'cost_price', 0) or 0))
            except Exception:
                base_unit_price = Decimal('0.00')

        base_line_amount = base_unit_price * qty
        if item.prd_distype == 'percent':
            discount_amount_base = (base_line_amount * discount_val) / Decimal('100')
        else:
            discount_amount_base = _to_base(discount_val)
        if discount_amount_base > base_line_amount:
            discount_amount_base = base_line_amount

        line_total_base = base_line_amount - discount_amount_base
        if line_total_base < 0:
            line_total_base = Decimal('0.00')

        subtotal_calc_base += line_total_base

        total_tax += tax_amount
        tax_amount_base = (line_total_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
        total_tax_base += tax_amount_base
        total_item_discount += discount_amount
        total_item_discount_base += discount_amount_base

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'price_base': base_unit_price,
            'base': base,
            'discount_peritem':discount_val,
            'discount_amount': discount_amount,
            'discount_amount_base': discount_amount_base,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'line_total': line_total,
            'line_total_base': line_total_base,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    # NOTE: subtotal_calc_base is accumulated from individual line_total_base values
    
    # Split tax equally into CGST/SGST for display (simple assumption)
    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_cgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')
    total_sgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')

    #  NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax

    # Grand discount (order-level)
    grand_discount_value = Decimal(str(order.discount_value or 0))
    grand_discount_type = order.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount  * grand_discount_value) / Decimal('100')

        # grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    # if grand_discount > subtotal_calc:
    #     grand_discount = subtotal_calc

    if grand_discount > total_before_discount:
        grand_discount = total_before_discount

    # final_total = subtotal_calc - grand_discount

    final_total = order.total_amount
    total_before_discount_base = subtotal_calc_base + total_tax_base
    if grand_discount_type == 'percent':
        grand_discount_base = (total_before_discount_base * grand_discount_value) / Decimal('100')
    else:
        grand_discount_base = _to_base(grand_discount)
    if grand_discount_base > total_before_discount_base:
        grand_discount_base = total_before_discount_base
    total_discount_combined = total_item_discount + grand_discount
    total_discount_combined_base = total_item_discount_base + grand_discount_base
    final_total_base = total_before_discount_base - grand_discount_base
    if final_total_base < 0:
        final_total_base = Decimal('0.00')


    # # Get follow-ups for this order
    # from crm.models import FollowUp
    # followups = FollowUp.objects.filter(order=order).order_by('-followup_date')
    company_tax_type = _get_purchase_company_tax_type(request)
    context = {
        'order_form': PurchaseOrderForm(instance=order),
        'purchase_formset': purchase_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': order.order_number,
        'order': order,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax': total_tax,
        'total_tax_base': total_tax_base,
        'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
        'company_tax_type': company_tax_type,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst_base,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst_base,
        'total_vat': total_tax,
        'total_vat_base': total_tax_base,
        'total_item_discount': total_item_discount,
        'total_item_discount_base': total_item_discount_base,
        'grand_discount': grand_discount,
        'grand_discount_base': grand_discount_base,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'final_total_base': final_total_base,
        'total_discount_combined': total_discount_combined,
        'total_discount_combined_base': total_discount_combined_base,
        'document_currency_symbol': document_currency_symbol,
        'document_currency_code': document_currency_code,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'fx_rate_to_base': fx_rate,
        'fx_rate': fx_rate,
        'show_base_currency_columns': show_base_currency_columns,
        'bill_conversion_count': order.bills.count(),
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,
        
    }
    return render(request, 'Purchase/purchaseorder_detail.html', context)

def purchase_order_list(request):
    # Permission: require view access to Purchase Order
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_orders(request.user)):
            messages.error(request, 'You do not have permission to view purchase orders.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view purchase orders.')
        return redirect_with_company('index')
    
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    purchase_order_qs = PurchaseOrder.objects.select_related(
        'vendor', 'payment_term', 'document_currency'
    ).order_by('-id')

    if search_query:
        purchase_order_qs = purchase_order_qs.filter(
            Q(vendor__first_name__icontains=search_query) |

            # Q(id__icontains=search_query) |
            Q(order_number__icontains=search_query) |

            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )
    
    if status_filter:
        purchase_order_qs = purchase_order_qs.filter(status=status_filter)

    # Grouping logic
    # Assume order_number like "QT123-R1", "QT123-R2", "QT456"
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_orders = defaultdict(list)

    for order in purchase_order_qs:
        match = pattern.match(order.order_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_orders[base_number].append((revision_num, order))

    # Select latest revision per base_number
    latest_orders = []
    revisions_dict = {}

    for base_number, rev_list in grouped_orders.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_orders.append(latest)

    # Paginate latest orders
    paginator = Paginator(latest_orders, page_size)
    page_number = request.GET.get('page')
    purchase_orders = paginator.get_page(page_number)

    total_count = len(latest_orders)

    context = {
        'purchase_orders': purchase_orders,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': total_count,
        'page_size': page_size,
        'revisions_dict': revisions_dict,  # Pass older revisions mapped by latest order id
        'can_view_purchase_orders': can_view_purchase_orders(request.user),
        'can_create_purchase_orders': can_create_purchase_orders(request.user),
        'can_edit_purchase_orders': can_edit_purchase_orders(request.user),
        'can_delete_purchase_orders': can_delete_purchase_orders(request.user),
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'Purchase/purchase_order_list.html', context)

    return render(request, 'Purchase/purchase_order_list.html', context)

#correctly prefilled
def purchase_order_edit(request, pk):
    #for viewing only
    readonly = request.GET.get('readonly', 'false').lower() == 'true'
    # If user lacks edit permission force readonly
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_orders(request.user)):
            readonly = True
    except Exception:
        readonly = True
    order = get_object_or_404(PurchaseOrder, pk=pk)
    PurchaseOrderItemFormSet = modelformset_factory(PurchaseOrderItem, form=PurchaseOrderItemForm, extra=0,can_delete=True)

    if request.method == "POST"and not readonly:
        # Permission: require Edit on Purchase Order
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_orders(request.user)):
                messages.error(request, 'You do not have permission to edit orders.')
                return redirect_with_company('purchase_order_list')
        except Exception:
            messages.error(request, 'You do not have permission to edit orders.')
            return redirect_with_company('purchase_order_list')

        # Make a mutable copy of POST and convert legacy items[...] keys to formset-style keys
        post_data = request.POST.copy()
        from re import match
        item_pattern = r"^items\[(\d+)\]\[(.+)\]$"
        item_keys = [k for k in post_data.keys() if k.startswith('items[')]
        if item_keys:
            items_map = {}
            max_index = -1
            for key in item_keys:
                m = match(item_pattern, key)
                if not m:
                    continue
                idx = int(m.group(1))
                field = m.group(2)
                items_map.setdefault(idx, {})[field] = post_data.get(key)
                if idx > max_index:
                    max_index = idx

            if items_map:
                compacted = post_data.copy()
                for k in list(compacted.keys()):
                    if k.startswith('items['):
                        compacted.pop(k, None)

                field_map = {
                    'id': 'product',
                    'qty': 'quantity',
                    'price': 'price',
                    'description': 'description',
                    'tax': 'prd_tax',
                    'discount': 'prd_disvalue',
                    'discount_type': 'prd_distype',
                    'gstinclude': 'gstinclude',
                    'o_price': 'o_price',
                    'hsn_code': 'hsn_code'
                }

                kept = 0
                for i in range(0, max_index + 1):
                    if i not in items_map:
                        continue
                    row = items_map[i]
                    raw_id = (row.get('id') or '').strip()
                    qty = (row.get('qty') or '').strip() if row.get('qty') is not None else ''
                    price = (row.get('price') or '').strip() if row.get('price') is not None else ''
                    if not raw_id and (not qty and not price):
                        continue

                    prefix = f'form-{kept}'
                    if raw_id:
                        parts = raw_id.split('_', 1)
                        compacted[f'{prefix}-product'] = parts[0]
                        if len(parts) > 1:
                            compacted[f'{prefix}-prd_brcd'] = parts[1]

                    for client_name, form_name in field_map.items():
                        if client_name == 'id':
                            continue
                        val = row.get(client_name)
                        if val is None:
                            continue
                        compacted[f'{prefix}-{form_name}'] = val

                    # carry delete flag if present
                    del_flag = row.get('delete') or row.get('DELETE')
                    if del_flag is not None:
                        compacted[f'{prefix}-DELETE'] = del_flag

                    kept += 1

                compacted['form-TOTAL_FORMS'] = str(kept)
                compacted['form-INITIAL_FORMS'] = '0'
                compacted['form-MIN_NUM_FORMS'] = '0'
                compacted['form-MAX_NUM_FORMS'] = '1000'

                post_data = compacted

        prd_brcd_map = {}
        for key in list(post_data.keys()):
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data.get(key, '')
                if value:
                    parts = value.split("_", 1)
                    post_data[key] = parts[0]
                    if len(parts) > 1:
                        prefix = key.rsplit("-", 1)[0]
                        prd_brcd_map[prefix] = parts[1]

        company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        order_form = PurchaseOrderForm(post_data, instance=order)
        existing_items_qs = PurchaseOrderItem.objects.filter(purchase_order=order)
        purchase_formset = PurchaseOrderItemFormSet(post_data, queryset=existing_items_qs)

        if order_form.is_valid() and purchase_formset.is_valid():
            # Save form but also capture shipping fields from POST
            order_obj = order_form.save(commit=False)
            existing_base_prices = {
                existing_item.pk: getattr(existing_item, 'o_price', None)
                for existing_item in existing_items_qs
            }
            
            order_obj.tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
            order_obj.tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
            order_obj.tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
            order_obj.tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
            
            # Persist payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    order_obj.payment_term = pt
                except Exception:
                    pass
            
            # Capture shipping address fields from POST
            order_obj.shipping_attention = request.POST.get('shipping_attention', '')
            order_obj.shipping_email = request.POST.get('shipping_email', '')
            order_obj.shipping_phone = request.POST.get('shipping_phone', '')
            order_obj.shipping_country = request.POST.get('shipping_country', '')
            order_obj.shipping_address1 = request.POST.get('shipping_address1', '')
            order_obj.shipping_address2 = request.POST.get('shipping_address2', '')
            order_obj.shipping_city = request.POST.get('shipping_city', '')
            order_obj.shipping_state = request.POST.get('shipping_state', '')
            order_obj.shipping_postal_code = request.POST.get('shipping_postal_code', '')
            
            # Capture place of supply
            place_of_supply = request.POST.get('place_of_supply', '')
            if place_of_supply:
                order_obj.place_of_supply = place_of_supply
    


            order_obj._current_user = request.user

            order_obj._current_request = request
            
            order_obj.save()
            items = purchase_formset.save(commit=False)
            calculated_total = Decimal('0.00')

            for index, item in enumerate(items):
                prefix = f"form-{index}"
                if prefix in prd_brcd_map:
                    item.prd_brcd = prd_brcd_map[prefix]
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                try:
                    posted_o_price = Decimal(str(post_data.get(f'{prefix}-o_price', '0') or '0'))
                except Exception:
                    posted_o_price = Decimal('0.00')
                existing_o_price = existing_base_prices.get(item.pk)
                if existing_o_price not in (None, Decimal('0.00')) and posted_o_price <= Decimal('0.00'):
                    item.o_price = Decimal(str(existing_o_price))
                else:
                    item.o_price = posted_o_price
                item.purchase_order = order_obj
                item.save()

                try:
                    qty = Decimal(item.quantity)
                    price = Decimal(item.price)
                except Exception:
                    qty = Decimal('0')
                    price = Decimal('0')

                base = qty * price
                discount_val = Decimal(item.prd_disvalue or 0)
                if item.prd_distype == 'percent':
                    discounted = base - (base * discount_val / Decimal('100'))
                else:
                    discounted = base - discount_val
                if discounted < 0:
                    discounted = Decimal('0')

                tax_amt = Decimal('0')
                if item.prd_tax:
                    tax_amt = (discounted * Decimal(item.prd_tax)) / Decimal('100')

                calculated_total += discounted + tax_amt

            for deleted_item in purchase_formset.deleted_objects:
                deleted_item.delete()

            grand_discount_value = Decimal(str(request.POST.get('grand-discount-value', 0)))
            grand_discount_type = request.POST.get('discount_type', 'percent')
            if grand_discount_type == 'percent':
                grand_discount = calculated_total * grand_discount_value / Decimal('100')
            else:
                grand_discount = grand_discount_value
            if grand_discount > calculated_total:
                grand_discount = calculated_total
            order_obj.total_amount = calculated_total - grand_discount
            order_obj.discount_value = grand_discount_value
            order_obj.discount_type = grand_discount_type
            order_obj.save()

            # Apply document currency and FX values
            try:
                company = _get_company_for_request(request)
                from currencies.services import apply_purchase_order_fx, parse_currency_fx_from_post

                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
                apply_purchase_order_fx(
                    order_obj,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )
            except Exception:
                logger.exception('Failed to apply FX to purchase order %s', order_obj.pk)

            messages.success(request, "Purchase order updated successfully!")
            return redirect_with_company('purchase_order_list')
        else:
            # Log detailed debug info for invalid formset to diagnose client POST shape
            try:
                logger.error(
                    "Purchase order edit validation failed. order_form.errors=%s, formset.errors=%s, POST_keys=%s",
                    getattr(order_form, 'errors', None),
                    getattr(purchase_formset, 'errors', None),
                    list(post_data.keys()) if isinstance(post_data, dict) or hasattr(post_data, 'keys') else str(type(post_data))
                )
            except Exception:
                logger.exception('Failed to log purchase order edit validation details')

            # Render the edit page with bound forms so validation errors are visible
            messages.error(request, 'Item validation failed. See errors on the form.')
            # Include raw POST payload for debugging (list values to preserve duplicates)
            debug_post_map = {}
            try:
                if hasattr(post_data, 'keys'):
                    for k in post_data.keys():
                        try:
                            if hasattr(post_data, 'getlist'):
                                debug_post_map[k] = post_data.getlist(k)
                            else:
                                debug_post_map[k] = [post_data.get(k)]
                        except Exception:
                            debug_post_map[k] = [str(post_data.get(k))]
            except Exception:
                debug_post_map = {'error': 'failed to build debug_post_map'}

            # Log the full debug_post_map for easier inspection in server logs
            try:
                logger.error("Purchase edit debug_post_map: %s", debug_post_map)
            except Exception:
                logger.exception("Failed to log debug_post_map")

            context = {
                'order_form': order_form,
                'purchase_formset': purchase_formset,
                'all_items': Item.objects.all(),
                'today': localdate().isoformat(),
                'q_no': order.order_number,
                'order': order,
                'readonly': readonly,
                'tds_tax_master_items': TdsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
                'tcs_tax_master_items': TcsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
                'shipping_attention': order.shipping_attention or '',
                'shipping_email': order.shipping_email or '',
                'shipping_phone': order.shipping_phone or '',
                'shipping_country': order.shipping_country or '',
                'shipping_address1': order.shipping_address1 or '',
                'shipping_address2': order.shipping_address2 or '',
                'shipping_city': order.shipping_city or '',
                'shipping_state': order.shipping_state or '',
                'shipping_postal_code': order.shipping_postal_code or '',
                'place_of_supply': order.place_of_supply or '',
                'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
                'company_tax_type': _get_purchase_company_tax_type(request),
                'debug_post_keys': list(post_data.keys()) if isinstance(post_data, dict) or hasattr(post_data, 'keys') else [],
                'debug_post': debug_post_map,
                'show_base_transaction_summary': bool(getattr(_get_company_for_request(request), 'show_base_transaction_summary', True)),
            }
            try:
                company = _get_company_for_request(request)
                from currencies.models import Currency
                company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
                context['company_currencies'] = company_currencies
                base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
                context['company_base_currency_symbol'] = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
                context['company_base_currency_code'] = base_currency.code if base_currency else ''
            except Exception:
                context['company_currencies'] = []
                context['company_base_currency_symbol'] = '₹'
                context['company_base_currency_code'] = ''

            return render(request, 'Purchase/purchase_order_duplicate.html', context)
    else:
        existing_items_qs = PurchaseOrderItem.objects.filter(purchase_order=order)
        purchase_formset = PurchaseOrderItemFormSet(queryset=existing_items_qs)
        order_form = PurchaseOrderForm(instance=order)
        # #print("existing_items_qs:", existing_items_qs)
        item_display_list = []

        # Build UOM map keyed by both barcode id and barcode value
        uoms_by_barcode = {}
        uom_qs = Uom.objects.filter(
            item_id__in=existing_items_qs.values_list('product_id', flat=True),
            barcode_id__in=existing_items_qs.values_list('prd_brcd', flat=True).distinct()
        ).select_related('barcode', 'name')

        for u in uom_qs:
            # if barcode relation exists, add two keys:
            if getattr(u, 'barcode', None):
                # key using barcode record id (as int)
                try:
                    key_id = f"{u.item_id}_{int(u.barcode.id)}"
                    uoms_by_barcode[key_id] = u.name.name if u.name else ''
                except Exception:
                    pass

                # key using barcode value (string)
                try:
                    barcode_val = str(u.barcode.barcode)
                    key_val = f"{u.item_id}_{barcode_val}"
                    uoms_by_barcode[key_val] = u.name.name if u.name else ''
                except Exception:
                    pass
            else:
                # fallback: map by item id only (if needed)
                k = f"{u.item_id}_"
                uoms_by_barcode[k] = u.name.name if u.name else ''

            # #print("uoms_by_barcode[key]:", u.item_id, getattr(u.barcode, 'id', None), getattr(u.barcode, 'barcode', None), "=>", uoms_by_barcode.get(f"{u.item_id}_{getattr(u.barcode,'id','')}", ''))



        #  Fetch tax group names in bulk (to avoid N+1 queries)
        product_ids = existing_items_qs.values_list('product_id', flat=True)
        items_with_tax = Item.objects.filter(id__in=product_ids).select_related('intra_tax')

        # Build dict for quick lookup: { item_id: tax_group_name }
        # tax_map = {}
        tax_map = {itm.id: (itm.intra_tax.group_name if itm.intra_tax_id else '') for itm in items_with_tax}
        for itm in items_with_tax:
            tax_name = ''
            if itm.intra_tax_id:
                tax_group = TaxGroup.objects.filter(id=itm.intra_tax_id).first()
                if tax_group:
                    tax_name = tax_group.group_name
            tax_map[itm.id] = tax_name

        for form, item in zip(purchase_formset.forms, existing_items_qs):
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            # #print(f"Checking key {key} → matched UOM: {uom_name}")

            #  Force the dropdown to display the correct text for this specific instance
            if uom_name:
                display_label = f"{item.product.name} ({uom_name})"
            else:
                display_label = item.product.name

            #  Replace the field choices with the selected item label
            form.fields['product'].choices = [
                (item.product.id, display_label)
            ]
            form.initial['product'] = item.product.id
            form.item = item
            # Match invoice behavior: prefer saved base line price, fall back to price * FX only when missing.
            from decimal import Decimal
            try:
                saved_base_price = getattr(item, 'o_price', None)
                if saved_base_price in (None, '', Decimal('0.00'), 0):
                    fx_rate = Decimal(str(order.fx_rate_to_base or '1.000000'))
                    if fx_rate <= 0:
                        fx_rate = Decimal('1.000000')
                    saved_base_price = (
                        Decimal(str(getattr(item, 'price', 0) or 0)) * fx_rate
                    ).quantize(Decimal('0.01'))
                form.initial['o_price'] = saved_base_price
                form.fields['o_price'].initial = saved_base_price
            except Exception:
                fallback_base_price = getattr(item, 'o_price', None) or item.price or Decimal('0.00')
                form.initial['o_price'] = fallback_base_price
                form.fields['o_price'].initial = fallback_base_price
            # #print(f" Updated dropdown for {display_label}")


        # for item in existing_items_qs:
            # #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

        #  Combine product + UOM + Tax Group for display
        for item in existing_items_qs:
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            display_name = f"{item.product.name} ({uom_name})" if uom_name else item.product.name

            tax_group_name = tax_map.get(item.product_id, '')

            item_display_list.append({
                'combined_id': key,
                'display_name': display_name,
                'quantity': item.quantity,
                'price': item.price,
                'prd_brcd': item.prd_brcd,
                'tax_group_name': tax_group_name,
            })
            # #print("item_display_list:", item_display_list)
            # #print("tax_group_name:", tax_group_name, flush=True)


    all_items = Item.objects.all()

    context = {
        'order_form': order_form,
        'purchase_formset': purchase_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': order.order_number,
        'order': order,
        'readonly': readonly,
        'tds_tax_master_items': TdsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        # Shipping #by adarshaddress fields for prefilling
        'shipping_attention': order.shipping_attention or '',
        'shipping_email': order.shipping_email or '',
        'shipping_phone': order.shipping_phone or '',
        'shipping_country': order.shipping_country or '',
        'shipping_address1': order.shipping_address1 or '',
        'shipping_address2': order.shipping_address2 or '',
        'shipping_city': order.shipping_city or '',
        'shipping_state': order.shipping_state or '',
        'shipping_postal_code': order.shipping_postal_code or '',#by adarsh
        'place_of_supply': order.place_of_supply or '',
        'tds_tcs_type': order.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': order.tds_tcs_definition_id or '',
        'tds_tcs_rate': order.tds_tcs_rate or 0,
        'tds_tcs_amount': order.tds_tcs_amount or 0,
        'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
        'company_tax_type': _get_purchase_company_tax_type(request),
    }
    try:
        company = _get_company_for_request(request)
        from currencies.models import Currency
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
        context['company_currencies'] = company_currencies
        base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
        context['company_base_currency_symbol'] = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
        context['company_base_currency_code'] = base_currency.code if base_currency else ''
        context['show_base_transaction_summary'] = bool(getattr(company, 'show_base_transaction_summary', True))
    except Exception:
        context['company_currencies'] = []
        context['company_base_currency_symbol'] = '₹'
        context['company_base_currency_code'] = ''
        context['show_base_transaction_summary'] = True
    context['tds_tcs_type'] = order.tds_tcs_type or 'tds'
    context['tds_tcs_definition_id'] = order.tds_tcs_definition_id or ''
    context['tds_tcs_rate'] = order.tds_tcs_rate or 0
    context['tds_tcs_amount'] = order.tds_tcs_amount or 0
    if readonly:
        for form in purchase_formset.forms:
            for field_name, field in form.fields.items():
                form.fields['product'].widget.attrs['disabled'] = True
                form.fields['prd_tax'].widget.attrs['disabled'] = True

                widget = field.widget
                if widget.__class__.__name__ in ['Select', 'SelectMultiple', 'CheckboxInput', 'RadioSelect']:
                    widget.attrs['disabled'] = True  # disable selects and similar widgets
                else:
                    widget.attrs['readonly'] = True
        # Render a dedicated readonly template without edit actions/buttons
        return render(request, 'Purchase/purchase_order_duplicate.html', context)
    else:
        # Render the editable template
        return render(request, 'Purchase/purchase_order_duplicate.html', context)

def generate_revised_order_number(original_order_number):
    # Extract base order number without revision suffix (e.g. "SQN001" from "SQN001-R2")
    base_order = re.sub(r'-R\d+$', '', original_order_number)

    # Find all orders with this base plus revision suffix, e.g. "SQN001-R1", "SQN001-R2"
    existing_revisions = PurchaseOrder.objects.filter(
        order_number__startswith=base_order + '-R'
    ).order_by('-id')

    # Determine new revision number
    if existing_revisions.exists():
        last_order_number = existing_revisions.first().order_number
        match = re.search(r'-R(\d+)$', last_order_number)
        last_rev_num = int(match.group(1)) if match else 0
        new_rev_num = last_rev_num + 1
    else:
        new_rev_num = 1

    # Create new order number with revision suffix
    new_order_number = f"{base_order}-R{new_rev_num}"
    return new_order_number

# the real view to save purchase order edit
# def purchase_order_duplicate(request, pk):
#     original_order = get_object_or_404(PurchaseOrder, pk=pk)
#     original_order_number = original_order.order_number
#     # #print("original_order_number:", original_order_number)
#     if request.method == "POST":
#         post_data = request.POST.copy()  # make mutable copy
#         db = getattr(request, 'company_db', 'default')
#         prd_brcd_map = {}

#         # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
#         for key in post_data:
#             # Identify product field keys
#             if key.startswith("form-") and key.endswith("-product"):
#                 value = post_data[key]
#                 # #print(f"Key matched: {key} with value: '{value}'")
#                 if value:
                    # parts = value.split("_", 1)
                    # item_id = (parts[0] or '').strip()
                    # # Only digits are valid Item PKs; anything else becomes empty.
                    # if not item_id.isdigit():
                    #     post_data[key] = ''
                    #     continue

                    # post_data[key] = item_id  # Save only item id for product field

                    # if len(parts) > 1:
                    #     # Map barcode corresponding to this form prefix (used for prd_brcd).
                    #     barcode_part = (parts[1] or '').strip()
                    #     if barcode_part:
                    #         prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
                    #         prd_brcd_map[prefix] = barcode_part
        # company_country = _get_current_company_country(request)
        # company_is_india = _is_indian_company_country(company_country)
        # post_data = _normalize_item_tax_tokens(post_data, company_is_india)

#         total_amount = request.POST.get('grandTotal')
#         vendor_id = request.POST.get('vendor')
#         date = request.POST.get('date')
#         notes = request.POST.get('notes', '')
#         # order_number = generate_order_number()
#         order_number = generate_revised_order_number(original_order_number)
#         # #print("order_number:",order_number)
        # try:
        #     discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
        #     discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        # except (InvalidOperation, ValueError, TypeError):
        #     discount_value = Decimal('0.00')
        # discount_type = request.POST.get('discount_type', 'percent')

#         if not vendor_id or not date:
#             messages.error(request, "Vendor and order Date are required.")
#             return redirect('purchase_order_list')

#         try:
#             vendor = Vendor.objects.get(pk=vendor_id)
            # Handle currency - use original quotation's currency or customer's default currency
            # document_currency = original_order.document_currency
            # fx_rate_to_base = original_order.fx_rate_to_base or Decimal('1.000000')
            # fx_rate_date = original_order.fx_rate_date
            
            # if not document_currency and vendor.currency:
            #     try:
            #         document_currency = Currency.objects.get(code__iexact=vendor.currency)
            #     except Currency.DoesNotExist:
            #         pass

#             order = PurchaseOrder.objects.create(
#                 vendor=vendor,
#                 date=date,
#                 order_number=order_number,
#                 total_amount=total_amount,
#                 notes=notes,
#                 discount_value=discount_value,
                # discount_type=discount_type,
                # place_of_supply=request.POST.get('place_of_supply', ''),
                # shipping_attention=request.POST.get('shipping_attention', ''),
                # shipping_email=request.POST.get('shipping_email', ''),
                # shipping_phone=request.POST.get('shipping_phone', ''),
                # shipping_country=request.POST.get('shipping_country', ''),
                # shipping_address1=request.POST.get('shipping_address1', ''),
                # shipping_address2=request.POST.get('shipping_address2', ''),
                # shipping_city=request.POST.get('shipping_city', ''),
                # shipping_state=request.POST.get('shipping_state', ''),
                # shipping_postal_code=request.POST.get('shipping_postal_code', ''),
                # tds_tcs_type=request.POST.get('tds_tcs_type', original_quote.tds_tcs_type or 'tds'),
                # tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
                # tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
                # tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
                # document_currency=document_currency,
                # fx_rate_to_base=fx_rate_to_base,
                # fx_rate_date=fx_rate_date,
                
#             )
#             # Attach payment term if provided
#             pay_term_id = request.POST.get('payment_term')
#             if pay_term_id:
#                 try:
#                     pt = PayTerms.objects.get(pk=pay_term_id)
#                     order.payment_term = pt
#                     order.save()
#                 except PayTerms.DoesNotExist:
#                     pass
#         except IntegrityError as e:
#             if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
#                 messages.error(request, f"Order Number '{order_number}' already exists. Please choose a different one.")
#             else:
#                 logger.exception("IntegrityError while creating revised purchase order: %s", e)
#                 messages.error(request, "An error occurred while saving the order.")
#             return redirect('purchase_order_list')

#         PurchaseOrderItemFormSet = modelformset_factory(
#             PurchaseOrderItem, form=PurchaseOrderItemForm, extra=0, can_delete=True
#         )

#         formset = PurchaseOrderItemFormSet(post_data, queryset=PurchaseOrderItem.objects.none())

#         if formset.is_valid():
#             items = formset.save(commit=False)
#             # accumulate totals for journal posting
#             taxable_total = Decimal(0)
#             tax_totals = {}  # map of taxtype -> Decimal amount (e.g., 'CGST'->amount)
#             saved_any = False
#             for index, item in enumerate(items):
#                 #added for edit save
#                 item.pk = None
                
#                 prefix = f"form-{index}"              # formset form key pattern
                # Skip blank/invalid rows
                # if not getattr(item, 'product_id', None):
                #     continue
                # saved_any = True
#                 if prefix in prd_brcd_map:            # check if barcode was extracted
#                     item.prd_brcd = prd_brcd_map[prefix]
#                 # capture HSN code from the hidden input posted by template
#                 item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
#                 # capture HSN code from the hidden input posted by template
#                 item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                # selected_tax = post_data.get(f'form-{index}-prd_tax', '')
                # item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                # item.purchase_order = order          # set foreign key
                # item.save()
            # for deleted_item in formset.deleted_objects:
            #     deleted_item.delete()
#           if not saved_any:
    #             order.delete()
    #             messages.error(request, "Please add at least one valid item in the order.")
    #             return redirect_with_company('purchase_order_list')
                

    #         messages.success(request, "Purchase Order created successfully!")
    #         # Redirect to order detail page after save
    #         try:
    #             url = reverse('purchase_order_detail', args=[order.pk])
    #             return redirect_with_company(url)
    #         except Exception:
    #             return redirect_with_company('purchase_order_list')
    #     else:
    #         # #print("Formset errors:", formset.errors)
    #         # for form in formset:
    #         #     #print("Individual form errors:", form.errors)
    #         # Prevent half-created revised quotation header with total_amount=0
    #         try:
    #             order.delete()
    #         except Exception:
    #             logger.exception("Failed to delete PurchaseOrder (revision) after invalid formset")

    #         messages.error(request, "There are errors with the items in the order.")
    #         return redirect_with_company('purchase_order_list')
    # else:
    #     return redirect_with_company('purchase_order_list')

@transaction.atomic
def duplicate_purchase_order(request, pk):
    print("purchase_order_duplicate called with pk:", pk)
    old_order = get_object_or_404(PurchaseOrder, pk=pk)

    # ------------------ PERMISSION CHECK ------------------
    try:
        if not (request.user.is_superuser or can_create_purchase_orders(request.user)):
            messages.error(request, "You do not have permission to create Orders.")
            return redirect_with_company('purchase_order_list')
    except Exception:
        messages.error(request, "You do not have permission to create Orders.")
        return redirect_with_company('purchase_order_list')

    try:
        original_order_id = old_order.pk
        original_items = list(PurchaseOrderItem.objects.filter(purchase_order=old_order))

        new_order = old_order
        new_order.pk = None
        new_order.id = None
        new_order.order_number = generate_order_number()
        new_order.status = 'Draft'
        new_order.save()

        for item in original_items:
            item.pk = None
            item.id = None
            item.purchase_order = new_order
            item.save()

        messages.success(request, f"Purchase order duplicated! New order: {new_order.order_number}")
        return redirect_with_company('purchaseorder_detail', pk=new_order.pk)
    except Exception as e:
        logger.exception("Error duplicating purchase order %s: %s", old_order.pk or pk, e)
        messages.error(request, "Failed to duplicate purchase order.")
        return redirect_with_company('purchaseorder_detail', pk=original_order_id if 'original_order_id' in locals() else pk)

    # ------------------ DELETE OLD ORDER ------------------
    try:
        PurchaseOrderItem.objects.filter(purchase_order=old_order).delete()
        old_order.delete()
    except Exception as e:
        logger.exception("Error deleting old order: %s", e)
        messages.error(request, "Unable to replace the existing order.")
        return redirect_with_company('purchase_order_list')

    # ------------------ PREPARE POST DATA ------------------
    post_data = request.POST.copy()
    prd_brcd_map = {}

    # ------------------ FIX PRODUCT + BARCODE ------------------
    for key in post_data:
        if key.startswith("form-") and key.endswith("-product"):
            value = post_data[key]
            if value:
                parts = value.split("_", 1)
                post_data[key] = parts[0]
                if len(parts) > 1:
                    prd_brcd_map[key.rsplit("-", 1)[0]] = parts[1]

    company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
    post_data = _normalize_item_tax_tokens(post_data, company_is_india)

    # ------------------ HEADER DATA ------------------
    vendor_id = post_data.get('vendor')
    date = post_data.get('date')
    notes = post_data.get('notes', '')
    # order_number = post_data.get('order_number')
    order_number = old_order.order_number

    discount_value = post_data.get('grand-discount-value', 0)
    discount_type = post_data.get('discount_type', 'percent')

    if not vendor_id or not date :
        messages.error(request, "Vendor and Date are required.")
        return redirect_with_company('purchase_order_list')

    try:
        vendor = Vendor.objects.get(pk=vendor_id)
    except Vendor.DoesNotExist:
        messages.error(request, "Invalid Vendor.")
        return redirect_with_company('purchase_order_list')

    # ------------------ CREATE NEW ORDER ------------------
    try:
        order = PurchaseOrder.objects.create(
            vendor=vendor,
            date=date,
            order_number=order_number,
            notes=notes,
            total_amount=0,
            discount_value=discount_value,
            discount_type=discount_type,
        )

        pay_term_id = post_data.get('payment_term')
        if pay_term_id:
            order.payment_term = PayTerms.objects.filter(pk=pay_term_id).first()
            order.save()
        
        # Capture shipping address fields
        order.shipping_attention = post_data.get('shipping_attention', '')
        order.shipping_email = post_data.get('shipping_email', '')
        order.shipping_phone = post_data.get('shipping_phone', '')
        order.shipping_country = post_data.get('shipping_country', '')
        order.shipping_address1 = post_data.get('shipping_address1', '')
        order.shipping_address2 = post_data.get('shipping_address2', '')
        order.shipping_city = post_data.get('shipping_city', '')
        order.shipping_state = post_data.get('shipping_state', '')
        order.shipping_postal_code = post_data.get('shipping_postal_code', '')
        
        # Capture place of supply
        place_of_supply = post_data.get('place_of_supply', '')
        if place_of_supply:
            order.place_of_supply = place_of_supply
        
        order.save()

    except IntegrityError:
        messages.error(request, f"Order Number '{order_number}' already exists.")
        return redirect_with_company('purchase_order_list')

    # ------------------ ORDER ITEMS ------------------
    # Support two client-side POST formats:
    # - Django formset keys: form-0-product, form-0-price, etc.
    # - Legacy JS array keys: items[0][id], items[0][price], etc.
    # If client posted using `items[...]` style, convert to formset-style keys.
    from re import match
    converted = False
    item_pattern = r"^items\[(\d+)\]\[(.+)\]$"
    item_keys = [k for k in post_data.keys() if k.startswith('items[')]
    if item_keys:
        # Build a map of index -> fieldname -> value
        items_map = {}
        max_index = -1
        for key in item_keys:
            m = match(item_pattern, key)
            if not m:
                continue
            idx = int(m.group(1))
            field = m.group(2)
            items_map.setdefault(idx, {})[field] = post_data.get(key)
            if idx > max_index:
                max_index = idx

        if items_map:
            compacted = post_data.copy()
            # remove original items[...] keys
            for k in list(compacted.keys()):
                if k.startswith('items['):
                    compacted.pop(k, None)

            # map item fields to form-<i>-<field>
            field_map = {
                'id': 'product',
                'qty': 'quantity',
                'price': 'price',
                'description': 'description',
                'tax': 'prd_tax',
                'discount': 'prd_disvalue',
                'discount_type': 'prd_distype',
                'gstinclude': 'gstinclude',
                'o_price': 'o_price',
                'hsn_code': 'hsn_code'
            }

            kept = 0
            for i in range(0, max_index + 1):
                if i not in items_map:
                    continue
                row = items_map[i]
                # skip empty rows where id or price/qty missing
                raw_id = (row.get('id') or '').strip()
                qty = (row.get('qty') or '').strip() if row.get('qty') is not None else ''
                price = (row.get('price') or '').strip() if row.get('price') is not None else ''
                # treat empty/defaults as skip
                if not raw_id and (not qty and not price):
                    continue

                prefix = f'form-{kept}'
                # if id contains barcode suffix like "<id>_<barcode>", split
                if raw_id:
                    parts = raw_id.split('_', 1)
                    compacted[f'{prefix}-product'] = parts[0]
                    if len(parts) > 1:
                        compacted[f'{prefix}-prd_brcd'] = parts[1]

                for client_name, form_name in field_map.items():
                    if client_name == 'id':
                        continue
                    val = row.get(client_name)
                    if val is None:
                        continue
                    compacted[f'{prefix}-{form_name}'] = val

                kept += 1

            compacted['form-TOTAL_FORMS'] = str(kept)
            compacted['form-INITIAL_FORMS'] = '0'
            compacted['form-MIN_NUM_FORMS'] = '0'
            compacted['form-MAX_NUM_FORMS'] = '1000'

            post_data = compacted
            converted = True

    PurchaseOrderItemFormSet = modelformset_factory(
        PurchaseOrderItem,
        form=PurchaseOrderItemForm,
        extra=0,
        can_delete=True
    )

    formset = PurchaseOrderItemFormSet(post_data, queryset=PurchaseOrderItem.objects.none())

    if not formset.is_valid():
        logger.error("Formset errors: %s", formset.errors)
        messages.error(request, "Item validation failed.")
        return redirect_with_company('purchase_order_list')

    calculated_total = Decimal('0.00')

    for index, item in enumerate(formset.save(commit=False)):
        item.pk = None
        prefix = f"form-{index}"

        if prefix in prd_brcd_map:
            item.prd_brcd = prd_brcd_map[prefix]

        item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')

        selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)

        item.purchase_order = order
        item.save()

        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        disc = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discounted = base - (base * disc / Decimal('100'))
        else:
            discounted = base - disc

        discounted = max(discounted, Decimal('0'))
        tax_amt = (discounted * item.prd_tax / Decimal('100')) if item.prd_tax else Decimal('0')

        calculated_total += discounted + tax_amt

    # ------------------ GRAND DISCOUNT ------------------
    grand_discount_value = Decimal(str(discount_value))
    if discount_type == 'percent':
        grand_discount = calculated_total * grand_discount_value / Decimal('100')
    else:
        grand_discount = grand_discount_value

    grand_discount = min(grand_discount, calculated_total)

    order.total_amount = calculated_total - grand_discount
    order.discount_value = grand_discount_value
    order.discount_type = discount_type
    order.save()

    # Apply document currency and FX values
    try:
        company = _get_company_for_request(request)
        from currencies.services import apply_purchase_order_fx, parse_currency_fx_from_post

        doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
        apply_purchase_order_fx(
            order,
            company,
            document_currency=doc_cur,
            fx_rate_to_base_override=rate_override,
            fx_rate_date_override=fx_date,
        )
    except Exception:
        logger.exception('Failed to apply FX to duplicated purchase order %s', order.pk)

    messages.success(request, "Order edited successfully.")
    return redirect_with_company('purchase_order_list')



#view to save edited purchase order
@transaction.atomic
def purchase_order_duplicate(request, pk):
    old_order = get_object_or_404(PurchaseOrder, pk=pk)
    order_number = old_order.order_number

    if request.method != "POST":
        return redirect_with_company('purchase_order_list')

    # ---------- PERMISSION ----------
    if not (request.user.is_superuser or can_create_purchase_orders(request.user)):
        messages.error(request, "You do not have permission to create Orders.")
        return redirect_with_company('purchase_order_list')

    # ---------- DELETE OLD ORDER (FREE UNIQUE NUMBER) ----------
    try:
        PurchaseOrderItem.objects.filter(purchase_order=old_order).delete()
        old_order.delete()
    except Exception as e:
        logger.exception("Error deleting old order: %s", e)
        messages.error(request, "Unable to replace the existing order.")
        return redirect_with_company('purchase_order_list')

    # ---------- PREPARE POST ----------
    post_data = request.POST.copy()
    # ---------- REMOVE OLD FORM IDS ----------
    for key in list(post_data.keys()):
        if key.endswith('-id'):
            del post_data[key]

    prd_brcd_map = {}

    for key in post_data:
        if key.startswith("form-") and key.endswith("-product"):
            value = post_data[key]
            if value:
                parts = value.split("_", 1)
                post_data[key] = parts[0]
                if len(parts) > 1:
                    prd_brcd_map[key.rsplit("-", 1)[0]] = parts[1]
    company_country = _get_purchase_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    post_data = _normalize_item_tax_tokens(post_data, company_is_india)
    # ---------- HEADER ----------
    vendor_id = post_data.get('vendor')
    date = post_data.get('date')

    if not vendor_id or not date:
        messages.error(request, "Vendor and Date are required.")
        return redirect_with_company('purchase_order_list')

    vendor = get_object_or_404(Vendor, pk=vendor_id)
   
    try:
        discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
        discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        discount_value = Decimal('0.00')
    discount_type = request.POST.get('discount_type', 'percent')



    # ---------- CREATE NEW ORDER ----------
    order = PurchaseOrder.objects.create(
        vendor=vendor,
        date=date,
        order_number=order_number,
        total_amount=0,
        notes=post_data.get('notes', ''),
        discount_value=discount_value,
        discount_type=discount_type,
        place_of_supply=request.POST.get('place_of_supply', ''),
        shipping_attention=request.POST.get('shipping_attention', ''),
        shipping_email=request.POST.get('shipping_email', ''),
        shipping_phone=request.POST.get('shipping_phone', ''),
        shipping_country=request.POST.get('shipping_country', ''),
        shipping_address1=request.POST.get('shipping_address1', ''),
        shipping_address2=request.POST.get('shipping_address2', ''),
        shipping_city=request.POST.get('shipping_city', ''),
        shipping_state=request.POST.get('shipping_state', ''),
        shipping_postal_code=request.POST.get('shipping_postal_code', ''),
        tds_tcs_type=request.POST.get('tds_tcs_type', old_order.tds_tcs_type or 'tds'),
        tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
        tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
        tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
    )

    pay_term_id = post_data.get('payment_term')
    if pay_term_id:
        order.payment_term = PayTerms.objects.filter(pk=pay_term_id).first()
        order.save()

    # ---------- ITEMS (FORMSET) ----------
    PurchaseOrderItemFormSet = modelformset_factory(
        PurchaseOrderItem, form=PurchaseOrderItemForm, extra=0, can_delete=True
    )
    formset = PurchaseOrderItemFormSet(post_data, queryset=PurchaseOrderItem.objects.none())

    if not formset.is_valid():
        logger.error("Formset errors: %s", formset.errors)
        messages.error(request, "Item validation failed.")
        order.delete()
        return redirect_with_company('purchase_order_list')

    calculated_total = Decimal('0.00')

    for index, item in enumerate(formset.save(commit=False)):
        if not getattr(item, 'product_id', None):
            continue

        prefix = f"form-{index}"

        if prefix in prd_brcd_map:
            item.prd_brcd = prd_brcd_map[prefix]

        item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
        selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
        item.purchase_order = order
        item.save()

        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        disc = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discounted = base - (base * disc / Decimal('100'))
        else:
            discounted = base - disc

        discounted = max(discounted, Decimal('0'))
        tax_amt = (discounted * item.prd_tax / Decimal('100')) if item.prd_tax else Decimal('0')

        calculated_total += discounted + tax_amt

    # ---------- GRAND DISCOUNT ----------
    gd_val = Decimal(str(discount_value))
    if discount_type == 'percent':
        grand_discount = calculated_total * gd_val / Decimal('100')
    else:
        grand_discount = gd_val

    grand_discount = min(grand_discount, calculated_total)
    order.total_amount = calculated_total - grand_discount
    order.discount_value = gd_val
    order.discount_type = discount_type
    order.save()

    # Apply document currency and FX values
    try:
        company = _get_company_for_request(request)
        from currencies.services import apply_purchase_order_fx, parse_currency_fx_from_post

        doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
        apply_purchase_order_fx(
            order,
            company,
            document_currency=doc_cur,
            fx_rate_to_base_override=rate_override,
            fx_rate_date_override=fx_date,
        )
    except Exception:
        logger.exception('Failed to apply FX to purchase order %s', order.pk)

    messages.success(request, "Order edited successfully.")
    return redirect_with_company('purchase_order_list')
    

@require_POST
def delete_purchase_order(request, order_id):
    # Permission: require Delete on Purchase Order
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_purchase_orders(request.user)):
            messages.error(request, 'You do not have permission to delete orders.')
            return redirect_with_company('purchase_order_list')
    except Exception:
        messages.error(request, 'You do not have permission to delete orders.')
        return redirect_with_company('purchase_order_list')

    order = get_object_or_404(PurchaseOrder, pk=order_id)
    order.delete()
    messages.success(request, f"Purchase order '{order.order_number}' deleted successfully.")
    return redirect_with_company('purchase_order_list')

def delete_bill(request, bill_id):
    # Permission: require Delete on Purchase Bills
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_purchase_bills(request.user)):
            error_msg = 'You do not have permission to delete bills.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect_with_company('bill_list')
    except Exception:
        error_msg = 'You do not have permission to delete bills.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, error_msg)
        return redirect_with_company('bill_list')

    bill = get_object_or_404(Bill, pk=bill_id)
    
    # ✅ by adarshlockCHECK PERIOD LOCK BEFORE DELETING BILL
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    try:
        PeriodLockEnforcer.check_can_edit(bill.date, request.user, db=db, transaction_type='bill')
    except PermissionDenied as e:
        error_msg = str(e)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, f"Cannot delete bill: {error_msg}")
        return redirect_with_company('bill_list')
    
    bill_number = bill.bill_number
    
    # Delete associated journal entries
    JournalEntry.objects.filter(reference=bill_number).delete()
    
    bill.delete()
    success_msg = f"Bill '{bill_number}' deleted successfully."
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': success_msg})
    
    messages.success(request, success_msg)
    return redirect_with_company('bill_list')

def generate_order_number():
    prefix_obj = OrderPrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "PO"

    # Fetch all order_numbers
    all_orders = PurchaseOrder.objects.values_list('order_number', flat=True)

    max_number = 0
    pattern = re.compile(r'(\d+)')  # Extract digits anywhere in the string

    for o in all_orders:
        match = pattern.search(o)
        if match:
            num = int(match.group(1))
            if num > max_number:
                max_number = num

    new_number = max_number + 1
    return f"{prefix}{str(new_number).zfill(3)}"

def generate_bill_number():
    prefix_obj = BillPrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "BN"

    # Fetch all order_numbers
    all_bills = Bill.objects.values_list('bill_number', flat=True)

    max_number = 0
    pattern = re.compile(r'(\d+)')  # Extract digits anywhere in the string

    for o in all_bills:
        match = pattern.search(o)
        if match:
            num = int(match.group(1))
            if num > max_number:
                max_number = num

    new_number = max_number + 1
    return f"{prefix}{str(new_number).zfill(3)}"


@transaction.atomic
def convert_purchase_order_to_bill(request, order_id):
    order = get_object_or_404(PurchaseOrder, pk=order_id)
    #updt by nehA on 23-2-26
    if order.status == 'Sent':
        order.status = 'Issued'
        order.save(update_fields=['status'])
    elif order.status == 'Open':
        order.status = 'Issued'
        order.save(update_fields=['status'])
    elif order.status == 'Closed':
        order.status = 'Billed'
        order.save(update_fields=['status'])

    # Status rule: only Issued orders can be converted to bill.
    if order.status != 'Issued':
        messages.error(request, 'Only Issued orders can be converted to Bill.')
        return redirect_with_company('purchaseorder_detail', pk=order.pk)
    # Generate a unique order_number (example: prefix 'SO' + order id + timestamp)
    import datetime
    # order_number = f"SO{order.id}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
    # bill_number =f"BILL-{order.id}-3"
    bill_number = generate_bill_number()

    # Create SalesOrder from Salesorder
    purchase_bill = Bill.objects.create(
        vendor=order.vendor,
        order_number=order,
        bill_number=bill_number,
        date=timezone.now(),
        notes=order.notes,
        payment_term=order.payment_term,
        discount_value=order.discount_value,
        discount_type=order.discount_type,
        status='Open',
        total_amount=order.total_amount,
        
    )

    # Apply document currency and FX values based on vendor/company
    try:
        company = _get_company_for_request(request)
        from currencies.services import apply_bill_fx

        apply_bill_fx(purchase_bill, company)
    except Exception:
        logger.exception('Failed to apply FX to bill %s', getattr(purchase_bill, 'pk', None))

    # Copy SalesorderItems to SalesOrderItems
    order_items = PurchaseOrderItem.objects.filter(purchase_order=order)
    for item in order_items:
        BillItem.objects.create(
            bill=purchase_bill,
            product=item.product,
            prd_brcd=item.prd_brcd,
            hsn_code=item.hsn_code,
            prd_tax=item.prd_tax,
            prd_taxgroup=item.prd_taxgroup,
            prd_disvalue=item.prd_disvalue,
            prd_distype=item.prd_distype,
            quantity=item.quantity,
            price=item.price,
            o_price=item.o_price,
        )

    # Post to journal if status is 'Open'
    if purchase_bill.status == 'Open':
        post_bill_to_journal(purchase_bill, user=request.user if hasattr(request, 'user') else None)

    # (Optional) Update order status or notify user here
    # Mark source purchase order as billed once it has been converted to a bill.
    if order.status != 'Billed':
        order.status = 'Billed'
        order.save(update_fields=['status'])
    # Redirect to sales order detail or list (replace 'sales_order_detail' accordingly)
    return redirect_with_company('bill_list')




def update_purchase_order_status(request, order_id):
    """Update order status via AJAX."""
    if request.method == 'POST':
        try:
            order = get_object_or_404(PurchaseOrder, pk=order_id)
            new_status = request.POST.get('status', '').strip()
            current_status = (order.status or '').strip()
            # Legacy cleanup before validating transitions.updt byy neha on 23-2-26
            if current_status == 'Sent':
                current_status = 'Issued'
                order.status = 'Issued'
                order.save(update_fields=['status'])
            elif current_status == 'Open':
                current_status = 'Issued'
                order.status = 'Issued'
                order.save(update_fields=['status'])
            elif current_status == 'Closed':
                current_status = 'Billed'
                order.status = 'Billed'
                order.save(update_fields=['status'])

            logger.debug(f"Attempting to update order {order_id} status to: {new_status}")
            
            # Validate status against allowed choices
            valid_statuses = [choice[0] for choice in PurchaseOrder.STATUS_CHOICES]
            logger.debug(f"Valid statuses: {valid_statuses}")
            
            if new_status not in valid_statuses:
                logger.warning(f"Invalid status '{new_status}' provided. Valid options: {valid_statuses}")
                return JsonResponse({'success': False, 'error': f'Invalid status. Valid options: {", ".join(valid_statuses)}'}, status=400)
            #added by neha on 23-2-26 for status transition rules
            allowed_transitions = {
                'Draft': {'Issued', 'Cancelled'},
                'Issued': {'Cancelled'},
                'Billed': set(),
                'Cancelled': set(),
            }

            if new_status == current_status:
                return JsonResponse({
                    'success': True,
                    'message': f"Order status is already '{current_status}'.",
                    'status': order.status
                })

            if new_status == 'Billed':
                return JsonResponse({
                    'success': False,
                    'error': "Status 'Billed' is set automatically when bill is created."
                }, status=400)

            allowed_next = allowed_transitions.get(current_status, set())
            if new_status not in allowed_next:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid status transition: {current_status} -> {new_status}."
                }, status=400)
            order.status = new_status
            order.save()
            logger.debug(f"Successfully updated order {order_id} status to: {new_status}")
            
            return JsonResponse({
                'success': True,
                'message': f"order status updated to '{new_status}'.",
                'status': order.status
            })
        except Exception as e:
            logger.exception("Error updating order status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)


def bill_list(request):
    # Permission: require view access to Purchase Bills
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_bills(request.user)):
            messages.error(request, 'You do not have permission to view bills.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view bills.')
        return redirect_with_company('index')
    
    company = _get_company_for_request(request)
    company_db = getattr(request, 'company_db', 'default')

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    payment_status_filter = request.GET.get('payment_status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    bill_qs = Bill.objects.using(company_db).order_by('-date', '-id')

    if search_query:
        bill_qs = bill_qs.filter(
            Q(vendor__first_name__icontains=search_query) |

            # Q(id__icontains=search_query) |
            Q(order_number__icontains=search_query) |

            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )
    
    if status_filter:
        bill_qs = bill_qs.filter(status=status_filter)

    # Grouping logic
    # Assume order_number like "QT123-R1", "QT123-R2", "QT456"
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_bills = defaultdict(list)

    for bill in bill_qs:
        match = pattern.match(bill.bill_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_bills[base_number].append((revision_num, bill))

    # Select latest revision per base_number
    latest_bills = []
    revisions_dict = {}

    for base_number, rev_list in grouped_bills.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_bills.append(latest)
    
    # Resolve currency symbols for display
    from currencies.services import get_base_currency
    from currencies.models import Currency
    currencies = {c.code: c for c in Currency.objects.filter(company=company, is_active=True)}
    base_currency = get_base_currency(company)

    for bill in latest_bills:
        # Safe access to document_currency (attribute might be missing if migration pending)
        doc_cur = getattr(bill, 'document_currency', None)
        if not doc_cur:
            # Fallback to vendor currency code
            v_code = (getattr(bill.vendor, 'currency', '') or '').strip().upper()[:3]
            doc_cur = currencies.get(v_code, base_currency)
        
        bill.resolved_currency_symbol = (getattr(doc_cur, 'symbol', '') or getattr(doc_cur, 'code', '') or '₹').strip()
        
        # Also attach to older revisions if any
        if hasattr(bill, 'older_revisions'):
            for rev in bill.older_revisions:
                rdc = getattr(rev, 'document_currency', None)
                if not rdc:
                    rv_code = (getattr(rev.vendor, 'currency', '') or '').strip().upper()[:3]
                    rdc = currencies.get(rv_code, base_currency)
                rev.resolved_currency_symbol = (getattr(rdc, 'symbol', '') or getattr(rdc, 'code', '') or '₹').strip()

    # Filter by payment status
    if payment_status_filter:
        if payment_status_filter == 'outstanding':
            # Home dashboard "Pending Bills": everything not fully paid, drafts excluded
            latest_bills = [
                b for b in latest_bills
                if b.status != 'Draft' and not (b.payment_status and (b.payment_status.name or '').strip().lower() == 'paid')
            ]
        elif payment_status_filter == 'Paid':
            latest_bills = [b for b in latest_bills if b.payment_status and b.payment_status.name == 'Paid']
        elif payment_status_filter == 'Partially Paid':
            latest_bills = [b for b in latest_bills if b.payment_status and b.payment_status.name == 'Partially Paid']
        elif payment_status_filter == 'Not Paid':
            latest_bills = [b for b in latest_bills if not b.payment_status or b.payment_status.name == 'Not Paid']

    # Paginate latest orders
    paginator = Paginator(latest_bills, page_size)
    page_number = request.GET.get('page')
    bills = paginator.get_page(page_number)

    total_count = len(latest_bills)

    context = {
        'bills': bills,
        'search_query': search_query,
        'status_filter': status_filter,
        'payment_status_filter': payment_status_filter,
        'total_count': total_count,
        'page_size': page_size,
        'revisions_dict': revisions_dict,  # Pass older revisions mapped by latest order id
        'can_view_purchase_bills': can_view_purchase_bills(request.user),
        'can_create_purchase_bills': can_create_purchase_bills(request.user),
        'can_edit_purchase_bills': can_edit_purchase_bills(request.user),
        'can_delete_purchase_bills': can_delete_purchase_bills(request.user),
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'Purchase/bill_list.html', context)

    return render(request, 'Purchase/bill_list.html', context)

def bill_add(request):
    # Permission: require Create on Purchase Bills
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_bills(request.user)):
            messages.error(request, 'You do not have permission to create bills.')
            return redirect_with_company('bill_list')
    except Exception:
        messages.error(request, 'You do not have permission to create bills.')
        return redirect_with_company('bill_list')

    # Create form instance for Salesorder
    # bill_form = BillForm()
    bill_form = BillForm(initial={'bill_number': generate_bill_number()})
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    # Create a formset for SalesorderItem if you plan multiple items
    BillItemFormSet = formset_factory(BillItemForm, extra=1)
    bill_formset = BillItemFormSet()
    all_items = Item.objects.all()
    # If opened from a lead or opportunity, keep id so save can link back
   

    # Pass both to the template
    company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
    company_tax_type = _get_purchase_company_tax_type(request)
    
    try:
        company = _get_company_for_request(request)
        from currencies.models import Currency
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
        base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
        company_base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else 'INR'
        company_base_currency_code = base_currency.code if base_currency else ''
    except Exception:
        company_currencies = []
        company_base_currency_symbol = 'INR'
        company_base_currency_code = ''
    # Pass both to the template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    return render(request, 'Purchase/bill_add.html', {
        'bill_form': bill_form,
        'item_formset': item_formset,
        'bill_formset': bill_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': f"PQ-{Bill.objects.count() + 1:05d}",
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'company_currencies': company_currencies,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        # ✅ Fetch TDS and TCS for quotation_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=company, is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=company, is_active=True),
        'show_base_transaction_summary': bool(getattr(company, 'show_base_transaction_summary', True)),
        
    })



def save_bill(request):
    
    if request.method == "POST":
        # Permission: require Create on Purchase Bills
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_bills(request.user)):
                messages.error(request, 'You do not have permission to create bills.')
                return redirect_with_company('bill_list')
        except Exception:
            messages.error(request, 'You do not have permission to create bills.')
            return redirect_with_company('bill_list')
        
        # ✅ byadarsh lockCHECK PERIOD LOCK BEFORE CREATING BILL
        from datetime import datetime
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        date_str = request.POST.get('date')
        if date_str:
            try:
                bill_date = datetime.strptime(date_str, '%Y-%m-%d').date() if isinstance(date_str, str) else date_str
            except:
                bill_date = None
            
            if bill_date:
                try:
                    PeriodLockEnforcer.check_can_edit(bill_date, request.user, db=db, transaction_type='bill')
                except PermissionDenied as e:
                    # For AJAX requests, return JSON error response
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return JsonResponse({'success': False, 'error': f"❌ Cannot create bill: {str(e)}"}, status=403)
                    # Render form page with error modal instead of redirecting
                    bill_form = BillForm()
                    BillItemFormSet = modelformset_factory(BillItem, form=BillItemForm, extra=3, can_delete=True)
                    item_formset = BillItemFormSet(queryset=BillItem.objects.none())
                    all_items = Item.objects.all()
                    return render(request, 'Purchase/bill_add.html', {
                        'bill_form': bill_form,
                        'item_formset': item_formset,
                        'all_items': all_items,
                        'today': localdate().isoformat(),
                        'error_message': str(e),
                        'show_error_modal': True,
                        'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
                        'company_tax_type': _get_purchase_company_tax_type(request),
                    })
        
        try:
            # wrap entire POST handling to catch and log unexpected errors
            
            
            post_data = request.POST.copy()  # make mutable copy
            prd_brcd_map = {}
        except Exception as e:
            logger.exception("Unexpected error in save_bill: %s", e)
            messages.error(request, "An unexpected error occurred while saving the bill first. See server log for details.")
            return redirect_with_company('bill_list')
        
        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
        # post_data and prd_brcd_map were prepared above
        for key in post_data:
            # Identify product field keys
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                # #print(f"Key matched: {key} with value: '{value}'")
                if value:
                    parts = value.split("_", 1)
                    post_data[key] = parts[0]  # Save only item id for product field
                    # #print("Updated post_data[key]:", post_data[key])
                    if len(parts) > 1:
                        # Map barcode corresponding to this form prefix
                        prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
                        prd_brcd_map[prefix] = parts[1]
                        # #print("prd_brcd_map[prefix]:",prd_brcd_map[prefix])

        company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        total_amount = request.POST.get('grandTotal')
        vendor_id = request.POST.get('vendor')
        date = request.POST.get('date')
        
       
        notes = request.POST.get('notes', '')
        # bill_number = request.POST.get('bill_number')
        bill_number = generate_bill_number()
        order_number = request.POST.get('order')

        # if not order_number:
        #     messages.error(request, "Order Number is required.")
        #     return redirect('bill_list')

        # try:
        #     order = PurchaseOrder.objects.get(pk=order_number_id)
        # except PurchaseOrder.DoesNotExist:
        #     messages.error(request, "Selected Order does not exist.")
        #     return redirect('bill_list')
        # #print("bill_number:",bill_number)
        discount_value = request.POST.get('grand-discount-value', 0)
        discount_type = request.POST.get('discount_type', 'percent')
        
        

        if not vendor_id or not date:
            #added by neha on 17-2-26
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': 'Vendor and Bill Date are required.'
                }, status=400)
            messages.error(request, "Vendor and bill Date are required.")
            return redirect_with_company('bill_list')

        try:
            vendor = Vendor.objects.get(pk=vendor_id)
            # sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None

            wh= None
            # Attach warehouse if provided
            ware_house_id = request.POST.get('warehouse')
            print("ware_house_id",ware_house_id)
            if ware_house_id:
                try:
                    wh = Warehouse.objects.get(pk=ware_house_id)
                    # bill.warehouse = wh
                    # bill.save()
                except Warehouse.DoesNotExist:
                    messages.error(request, "Selected warehouse does not exist.")
                    return redirect_with_company('bill_list')
            # ✅ FIXED: Extract and log TDS/TCS values
            tds_tcs_type_val = request.POST.get('tds_tcs_type', 'tds')
            tds_tcs_def_id_val = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
            tds_tcs_rate_val = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
            tds_tcs_amount_val = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
            
            print(f"DEBUG: TDS/TCS values from POST:")
            print(f"  tds_tcs_type (raw): '{request.POST.get('tds_tcs_type')}'")
            print(f"  tds_tcs_type (parsed): '{tds_tcs_type_val}'")
            print(f"  tds_tcs_definition_id (raw): '{request.POST.get('tds_tcs_definition_id')}'")
            print(f"  tds_tcs_definition_id (parsed): {tds_tcs_def_id_val}")
            print(f"  tds_tcs_rate (raw): '{request.POST.get('tds_tcs_rate')}'")
            print(f"  tds_tcs_rate (parsed): {tds_tcs_rate_val}")
            print(f"  tds_tcs_amount (raw): '{request.POST.get('tds_tcs_amount')}'")
            print(f"  tds_tcs_amount (parsed): {tds_tcs_amount_val}")
            
            # Create bill with zero total for now; we'll recalculate after items are processed
            bill = Bill.objects.create(
                vendor=vendor,
                date=date,
                order=order_number, 
                bill_number=bill_number,
                
                total_amount=0,
                notes=notes,
                discount_value=discount_value,
                discount_type=discount_type,
                warehouse=wh,
                # Capture#by adarsh shipping address fields
                shipping_attention=request.POST.get('shipping_attention', ''),
                shipping_email=request.POST.get('shipping_email', ''),
                shipping_phone=request.POST.get('shipping_phone', ''),
                shipping_country=request.POST.get('shipping_country', ''),
                shipping_address1=request.POST.get('shipping_address1', ''),
                shipping_address2=request.POST.get('shipping_address2', ''),
                shipping_city=request.POST.get('shipping_city', ''),
                shipping_state=request.POST.get('shipping_state', ''),
                shipping_postal_code=request.POST.get('shipping_postal_code', ''),#by adarsh
                place_of_supply=request.POST.get('place_of_supply', ''),
                # ✅ FIXED: Save TDS/TCS values from the form
                tds_tcs_type=tds_tcs_type_val,
                tds_tcs_definition_id=tds_tcs_def_id_val,
                tds_tcs_rate=tds_tcs_rate_val,
                tds_tcs_amount=tds_tcs_amount_val,
            )
            
            print(f"DEBUG: Bill created with ID {bill.id}")
            print(f"  Stored tds_tcs_type: '{bill.tds_tcs_type}'")
            print(f"  Stored tds_tcs_definition_id: {bill.tds_tcs_definition_id}")
            print(f"  Stored tds_tcs_rate: {bill.tds_tcs_rate}")
            print(f"  Stored tds_tcs_amount: {bill.tds_tcs_amount}")

            bill._current_user = request.user
            bill._current_request = request
            bill.save()

            # Apply document currency and FX values based on vendor/company
            doc_cur = None
            try:
                company = _get_company_for_request(request)
                from currencies.services import apply_bill_fx, parse_currency_fx_from_post

                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
                apply_bill_fx(
                    bill,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )
            except Exception:
                logger.exception('Failed to apply FX to bill %s', getattr(bill, 'pk', None))

            # Attach payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    bill.payment_term = pt
                except PayTerms.DoesNotExist:
                    pass
            
            # ✅ CRITICAL FIX: Re-save TDS/TCS values after all modifications
            # These may have been lost during apply_bill_fx or other operations
            bill.tds_tcs_type = tds_tcs_type_val
            bill.tds_tcs_definition_id = tds_tcs_def_id_val
            bill.tds_tcs_rate = tds_tcs_rate_val
            bill.tds_tcs_amount = tds_tcs_amount_val
            bill.save()
            
            print(f"DEBUG: Bill saved AFTER all modifications")
            print(f"  Final tds_tcs_type: '{bill.tds_tcs_type}'")
            print(f"  Final tds_tcs_definition_id: {bill.tds_tcs_definition_id}")
            print(f"  Final tds_tcs_rate: {bill.tds_tcs_rate}")
            print(f"  Final tds_tcs_amount: {bill.tds_tcs_amount}")
        except IntegrityError as e:
            
            if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False,
                        'error': 'Bill Number already exists. Please choose a different one.'
                    }, status=400)
                messages.error(request, f"Bill Number '{bill_number}' already exists. Please choose a different one.")
            else:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False,
                        'error': 'An error occurred while saving the bill.'
                    }, status=400)
                logger.exception("IntegrityError while creating Bill: %s", e)
                messages.error(request, "An error occurred while saving the bill.")
            return redirect_with_company('bill_list')
        except Exception as e:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': 'An unexpected error occurred while saving the bill.'
                }, status=500)
            logger.exception("Unexpected exception while creating Bill header: %s", e)
            messages.error(request, "An unexpected error occurred while saving the bill second. See server log for details.")
            return redirect_with_company('bill_list')

        BillItemFormSet = modelformset_factory(
            BillItem, form=BillItemForm, extra=0, can_delete=True
        )

        formset = BillItemFormSet(post_data, queryset=BillItem.objects.none())

        if formset.is_valid():
            items = formset.save(commit=False)
            # initialize accumulators for journal posting
            taxable_total = Decimal(0)
            tax_totals = {}
            # initialize accumulators for journal posting
            taxable_total = Decimal(0)
            tax_totals = {}
            calculated_total = Decimal('0.00')
            for index, item in enumerate(items):
                if not item.product:
                    continue
                #added for edit save
                item.pk = None

                prefix = f"form-{index}"              # formset form key pattern
                if prefix in prd_brcd_map:            # check if barcode was extracted
                    item.prd_brcd = prd_brcd_map[prefix]
                # capture HSN code from the hidden input posted by template
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                # capture HSN code from the hidden input posted by template
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                
                # Save base currency price (high precision)
                try:
                    item.o_price = Decimal(str(post_data.get(f'{prefix}-o_price', 0) or 0))
                except Exception:
                    item.o_price = Decimal('0.0000')
                    
                selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                item.bill = bill          # set foreign key
                item.save()                
                # Server-side calculation of line total (mirror frontend logic):
                try:
                    qty = Decimal(item.quantity)
                    price = Decimal(item.price)
                except Exception:
                    qty = Decimal('0')
                    price = Decimal('0')

                base = qty * price
                # apply item discount
                discount_val = Decimal(item.prd_disvalue or 0)
                if item.prd_distype == 'percent':
                    discounted = base - (base * discount_val / Decimal('100'))
                else:
                    discounted = base - discount_val
                if discounted < 0:
                    discounted = Decimal('0')

                tax_amt = Decimal('0')
                if item.prd_tax:
                    tax_amt = (discounted * Decimal(item.prd_tax)) / Decimal('100')

                # if gst is included on price, frontend may treat differently; here assume price is base
                line_total = discounted + tax_amt
                calculated_total += line_total
            for deleted_item in formset.deleted_objects:
                deleted_item.delete()
            # --- FIX: Subtract grand discount from calculated_total ---
            grand_discount_value = Decimal(str(request.POST.get('grand-discount-value', 0)))
            grand_discount_type = request.POST.get('discount_type', 'percent')
            if grand_discount_type == 'percent':
                grand_discount = calculated_total * grand_discount_value / Decimal('100')
            else:
                grand_discount = grand_discount_value
            if grand_discount > calculated_total:
                grand_discount = calculated_total
            final_total = calculated_total - grand_discount
            
            # ✅ FIXED: Adjust total_amount for TDS/TCS
            # TDS (Tax Deducted at Source): Reduces the amount payable
            # TCS (Tax Collected at Source): Increases the amount payable
            if tds_tcs_amount_val and tds_tcs_amount_val > 0:
                if tds_tcs_type_val == 'tds':
                    final_total = final_total - tds_tcs_amount_val
                    print(f"DEBUG: Adjusted total for TDS: {final_total} = {calculated_total - grand_discount} - {tds_tcs_amount_val} (TDS)")
                elif tds_tcs_type_val == 'tcs':
                    final_total = final_total + tds_tcs_amount_val
                    print(f"DEBUG: Adjusted total for TCS: {final_total} = {calculated_total - grand_discount} + {tds_tcs_amount_val} (TCS)")
            
            bill.total_amount = final_total
            bill.discount_value = grand_discount_value
            bill.discount_type = grand_discount_type
            bill.save()

            try:
                company = _get_company_for_request(request)
                from currencies.services import apply_bill_fx, parse_currency_fx_from_post

                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
                apply_bill_fx(
                    bill,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )
            except Exception:
                logger.exception('Failed to refresh FX totals for bill %s', getattr(bill, 'pk', None))
            
            # ✅ SAFETY: Re-preserve TDS/TCS values after FX application
            bill.tds_tcs_type = tds_tcs_type_val
            bill.tds_tcs_definition_id = tds_tcs_def_id_val
            bill.tds_tcs_rate = tds_tcs_rate_val
            bill.tds_tcs_amount = tds_tcs_amount_val
            bill.save()
            
            print(f"DEBUG: Bill after FX and final TDS/TCS preservation:")
            print(f"  tds_tcs_type: '{bill.tds_tcs_type}'")
            print(f"  tds_tcs_amount: {bill.tds_tcs_amount}")
            print(f"  total_amount: {bill.total_amount}")

            # Post to journal if status is 'Open'
            if bill.status == 'Open':
                post_bill_to_journal(bill, user=request.user)

            messages.success(request, "Bill created successfully!")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'bill_id': bill.id,
                    'bill_number': bill.bill_number,
                    'vendor_name': (
                        bill.vendor.company_name
                        if bill.vendor and bill.vendor.vendor_type == 'company'
                        else (
                            f"{bill.vendor.first_name} "
                            f"{bill.vendor.last_name or ''}"
                        ).strip()
                        if bill.vendor
                        else ''
                    ),
                    'total_amount': str(bill.total_amount),
                    'currency_symbol': (getattr(getattr(bill, 'document_currency', None), 'symbol', '') or getattr(getattr(bill, 'document_currency', None), 'code', '') or getattr(doc_cur, 'symbol', '') or getattr(doc_cur, 'code', '') or '₹').strip(),
                    'message': 'Bill created successfully!'
                })
            # If this bill was created from a lead or opportunity, create a CRM Update linking them
            

            # Redirect to bill detail page after save
            try:
                return redirect_with_company('bill_list')
                return redirect_with_company(url)
            except Exception:
                return redirect_with_company('bill_list')
        else:
            # # #print("Formset errors:", formset.errors)
            # for form in formset:
            #     #print("Individual form errors:", form.errors)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': 'There are errors with the items in the bill. Please correct them and try again.'
                }, status=400)
            logger.error("Bill formset invalid: %s", formset.errors)
            messages.error(request, "There are errors with the items in the bill. See server log for details.")
            return redirect_with_company('bill_list')
    else:
        return redirect_with_company('bill_list')


def bill_detail(request, pk):
    """Render a simple readonly detail page for a bill."""
    # Permission: require view access to Purchase Bills
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_bills(request.user)):
            messages.error(request, 'You do not have permission to view bills.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view bills.')
        return redirect_with_company('index')
    
    bill = get_object_or_404(Bill, pk=pk)
    # bill_payment= get_object_or_404(BillPayment, pk=pk)
    # Use a simple formset to iterate items if needed in template
    BillItemFormSet = modelformset_factory(BillItem, form=BillItemForm, extra=0)
    existing_items_qs = BillItem.objects.filter(bill=bill)
    bill_formset = BillItemFormSet(queryset=existing_items_qs)
    company = _get_company_for_request(request)
    try:
        from currencies.services import get_base_currency, resolve_currency_for_vendor
        doc_cur_val = getattr(bill, 'document_currency', None)
        document_currency = doc_cur_val or resolve_currency_for_vendor(bill.vendor, company)
        base_currency = get_base_currency(company)
    except Exception:
        document_currency = getattr(bill, 'document_currency', None)
        base_currency = None

    document_currency_symbol = (
        (getattr(document_currency, 'symbol', '') or getattr(document_currency, 'code', '')).strip()
        if document_currency else ''
    )
    document_currency_code = getattr(document_currency, 'code', '') if document_currency else ''
    company_base_currency_symbol = (
        (getattr(base_currency, 'symbol', '') or getattr(base_currency, 'code', '')).strip()
        if base_currency else '₹'
    )
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''

    try:
        # Only trust the saved fx_rate if the bill has a document_currency (meaning it was saved with new logic)
        saved_doc_cur = getattr(bill, 'document_currency', None)
        fx_rate_val = getattr(bill, 'fx_rate_to_base', None)
        
        if saved_doc_cur is not None and fx_rate_val is not None:
            fx_rate = Decimal(str(fx_rate_val))
        else:
            # Fallback for old bills or missing currency data
            from currencies.services import get_effective_rate_to_base
            fx_rate = get_effective_rate_to_base(document_currency, bill.date)
    except Exception:
        fx_rate = Decimal('1.000000')
    if fx_rate <= 0:
        fx_rate = Decimal('1.000000')

    # ✅ FIX: when the vendor's currency is the same as the company's base
    # currency, the document-currency and base-currency figures are
    # identical, so showing both "Price"/"Price (Base)" columns and both
    # "Document"/"Base" total columns is pure, confusing duplication. Only
    # show the base-currency columns when they'd actually differ, matching
    # the same same-currency detection already used on the add/edit forms
    # (see shouldHideBaseCurrencyUi() in purchase_order.js). Compare the
    # actual currency records first (most reliable — avoids any mismatch if
    # duplicate/legacy currency rows share a code or symbol), then fall
    # back to code, then symbol, then finally the fx rate itself.
    if document_currency is not None and base_currency is not None and getattr(document_currency, 'pk', None) == getattr(base_currency, 'pk', None):
        show_base_currency_columns = False
    elif document_currency_code and company_base_currency_code:
        show_base_currency_columns = (document_currency_code != company_base_currency_code)
    elif document_currency_symbol and company_base_currency_symbol:
        show_base_currency_columns = (document_currency_symbol != company_base_currency_symbol)
    else:
        show_base_currency_columns = abs(fx_rate - Decimal('1')) >= Decimal('0.000001')

    tds_tcs_type = (bill.tds_tcs_type or '').strip().lower()
    tds_tcs_amount = Decimal(str(bill.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate

    def _resolve_bill_item_base_price(item_obj, bill_obj, current_fx_rate):
        from decimal import Decimal

        zero_values = {None, '', 0, Decimal('0.00'), Decimal('0.0000')}
        try:
            saved_base = getattr(item_obj, 'o_price', None)
            saved_base_dec = Decimal(str(saved_base)) if saved_base not in zero_values else Decimal('0.0000')
        except Exception:
            saved_base_dec = Decimal('0.0000')

        # ✅ FIX: trust a properly saved base price outright (see matching fix
        # in bill_edit's copy of this helper) instead of discarding it in
        # favor of the item's live master cost price whenever it happens to
        # agree with price*fx — the common, correct case.
        if saved_base_dec > 0:
            return saved_base_dec

        try:
            doc_price_dec = Decimal(str(getattr(item_obj, 'price', 0) or 0))
        except Exception:
            doc_price_dec = Decimal('0.0000')

        derived_from_doc = (doc_price_dec * current_fx_rate).quantize(Decimal('0.01'))

        if getattr(bill_obj, 'order_number_id', None):
            linked_order_item = (
                PurchaseOrderItem.objects
                .filter(
                    purchase_order_id=bill_obj.order_number_id,
                    product_id=item_obj.product_id,
                    prd_brcd=item_obj.prd_brcd,
                )
                .order_by('pk')
                .first()
            )
            if linked_order_item and getattr(linked_order_item, 'o_price', None) not in zero_values:
                try:
                    return Decimal(str(linked_order_item.o_price))
                except Exception:
                    pass

        if derived_from_doc > 0:
            return derived_from_doc

        try:
            product_cost = Decimal(str(getattr(item_obj.product, 'cost_price', 0) or 0))
        except Exception:
            product_cost = Decimal('0.0000')
        if product_cost > 0:
            return product_cost

        return Decimal('0.0000')

    def _to_base(amount):
        try:
            return (Decimal(str(amount or 0)) * fx_rate)
        except Exception:
            return Decimal('0.00')

    # Compute per-line amounts and totals (mirror save logic)
    items_info = []
    subtotal_calc = Decimal('0.00')
    subtotal_calc_base = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    total_item_discount = Decimal('0.00')
    total_item_discount_base = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)

        if item.prd_distype == 'percent':
            discounted = base - (base * discount_val / Decimal('100'))
        else:
            discounted = base - discount_val
        if discounted < 0:
            discounted = Decimal('0.00')

        discount_amount = base - discounted

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        line_total = discounted

        subtotal_calc += discounted

        base_unit_price = _resolve_bill_item_base_price(item, bill, fx_rate)

        base_line_amount = base_unit_price * qty
        if item.prd_distype == 'percent':
            discount_amount_base = (base_line_amount * discount_val) / Decimal('100')
        else:
            discount_amount_base = _to_base(discount_val)
            
        if discount_amount_base > base_line_amount:
            discount_amount_base = base_line_amount

        line_total_base = base_line_amount - discount_amount_base
        if line_total_base < 0:
            line_total_base = Decimal('0.00')

        subtotal_calc_base += line_total_base

        total_tax += tax_amount
        tax_amount_base = (line_total_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
        total_tax_base += tax_amount_base
        total_item_discount += discount_amount
        total_item_discount_base += discount_amount_base

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'price_base': base_unit_price,
            'base': base,
            'discount_peritem':discount_val,
            'discount_amount': discount_amount,
            'discount_amount_base': discount_amount_base,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'line_total': line_total,
            'line_total_base': line_total_base,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    # Split tax equally into CGST/SGST for display (simple assumption)
    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_cgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')
    total_sgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')

    #  NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax
    total_before_discount_base = subtotal_calc_base + total_tax_base

    # Grand discount (bill-level)
    grand_discount_value = Decimal(str(bill.discount_value or 0))
    grand_discount_type = bill.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount  * grand_discount_value) / Decimal('100')
        grand_discount_base = (total_before_discount_base * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
        grand_discount_base = _to_base(grand_discount)

    if grand_discount > total_before_discount:
        grand_discount = total_before_discount
    if grand_discount_base > total_before_discount_base:
        grand_discount_base = total_before_discount_base

    final_total = bill.total_amount
    final_total_base = total_before_discount_base - grand_discount_base
    if final_total_base < 0:
        final_total_base = Decimal('0.00')
        
    total_discount_combined = total_item_discount + grand_discount
    total_discount_combined_base = total_item_discount_base + grand_discount_base
    # # Get follow-ups for this bill
    # from crm.models import FollowUp
    # followups = FollowUp.objects.filter(bill=bill).bill_by('-followup_date')
    total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')

    remaining_amount = bill.total_amount - total_paid

    is_fully_paid = remaining_amount <= Decimal('0.00')

    # Calculate returned quantity per bill item (pending or received returns)
    try:
        for idx, item in enumerate(existing_items_qs):
            returned_total = PurchaseReturnItem.objects.filter(bill_item=item, purchase_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
            items_info[idx]['returned_qty'] = int(returned_total)
    except Exception:
        for idx in range(len(items_info)):
            items_info[idx]['returned_qty'] = 0

    # Determine if bill is fully returned
    def bill_is_fully_returned(bill_obj):
        try:
            items_qs = BillItem.objects.filter(bill=bill_obj)
            # If there are no bill items, do not treat the bill as fully returned
            if not items_qs.exists():
                setattr(bill_obj, 'is_fully_returned', False)
                return

            for itm in items_qs:
                returned = PurchaseReturnItem.objects.filter(bill_item__id=itm.id, purchase_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
                if int(returned) < int(itm.quantity):
                    setattr(bill_obj, 'is_fully_returned', False)
                    break
            else:
                setattr(bill_obj, 'is_fully_returned', True)
        except Exception:
            setattr(bill_obj, 'is_fully_returned', False)

    bill_is_fully_returned(bill)
    is_fully_returned = getattr(bill, 'is_fully_returned', False)

    # Related activity for detail view sections
    bill_payment_allocations = (
        BillPaymentAllocation.objects.filter(bill=bill)
        .select_related('payment', 'payment__payment_mode')
        .order_by('-payment__payment_date', '-payment__created_at')
    )
    purchase_returns = (
        PurchaseReturn.objects.filter(bill=bill)
        .select_related('warehouse')
        .order_by('-date', '-created_at')
    )
    delivery_notes = (
        DeliveryNote.objects.filter(bill=bill)
        .select_related('warehouse')
        .order_by('-delivery_date', '-created_at')
    )
    company_tax_type = _get_purchase_company_tax_type(request)
    context = {
        'bill_form': BillForm(instance=bill),
        'total_paid': total_paid,
        'bill_formset': bill_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': bill.bill_number,
        'o_no': bill.order_number,
        'bill': bill,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax': total_tax,
        'total_tax_base': total_tax_base,
        'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
        'company_tax_type': company_tax_type,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst_base,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst_base,
        'total_vat': total_tax,
        'total_vat_base': total_tax_base,
        'total_item_discount': total_item_discount,
        'total_item_discount_base': total_item_discount_base,
        'grand_discount': grand_discount,
        'grand_discount_base': grand_discount_base,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'final_total_base': final_total_base,
        'total_discount_combined': total_discount_combined,
        'total_discount_combined_base': total_discount_combined_base,
        'document_currency_symbol': document_currency_symbol,
        'document_currency_code': document_currency_code,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'fx_rate_to_base': fx_rate,
        'show_base_currency_columns': show_base_currency_columns,
        'is_fully_paid': is_fully_paid,
        'is_fully_returned': is_fully_returned,
        'bill_payment_allocations': bill_payment_allocations,
        'bill_payment_allocations_count': bill_payment_allocations.count(),
        'purchase_returns': purchase_returns,
        'purchase_returns_count': purchase_returns.count(),
        'delivery_notes': delivery_notes,
        'delivery_notes_count': delivery_notes.count(),
        'can_create_purchase_returns': can_create_purchase_returns(request.user),
        'can_create_orders': can_create_purchase_payments(request.user),
        'can_edit_orders': can_edit_purchase_bills(request.user),
        'can_delete_orders': can_delete_purchase_bills(request.user),
        'can_view_purchase_payments': can_view_purchase_payments(request.user),
        'can_edit_purchase_payments': can_edit_purchase_payments(request.user),
        'can_delete_purchase_payments': can_delete_purchase_payments(request.user),
        'can_view_purchase_returns': can_view_purchase_returns(request.user),
        'can_edit_purchase_returns': can_edit_purchase_returns(request.user),
        'can_view_purchase_delivery': can_view_purchase_delivery(request.user),
        'can_edit_purchase_delivery': can_edit_purchase_delivery(request.user),
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,
        
    }
    return render(request, 'Purchase/bill_detail.html', context)


def bill_edit(request, pk):
    #for viewing only
    readonly = request.GET.get('readonly', 'false').lower() == 'true'
    # If user lacks edit permission force readonly
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_bills(request.user)):
            readonly = True
    except Exception:
        readonly = True
    bill = get_object_or_404(Bill, pk=pk)
    if bill.status == 'Closed' and not readonly:
        message = 'Closed bills cannot be edited.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': message}, status=400)
        messages.error(request, message)
        return redirect_with_company('bill_list')
    BillItemFormSet = modelformset_factory(BillItem, form=BillItemForm, extra=0,can_delete=True)

    def _resolve_bill_item_base_price(item_obj, bill_obj):
        zero_values = {None, '', 0, Decimal('0.00'), Decimal('0.0000')}
        try:
            saved_base = getattr(item_obj, 'o_price', None)
            saved_base_dec = Decimal(str(saved_base)) if saved_base not in zero_values else Decimal('0.0000')
        except Exception:
            saved_base_dec = Decimal('0.0000')

        # ✅ FIX: a properly saved base price (o_price) is the accurate
        # historical record of what was actually billed for this line, so it
        # should always be trusted outright once it exists — never
        # second-guessed against the item's *current* master cost price,
        # which can legitimately drift (get repriced) long after this bill
        # was created. The previous logic only trusted this value when it
        # *disagreed* with price*fx, which meant the common, perfectly
        # consistent case fell through to the live product cost price below
        # and silently replaced the correct saved price on every reload.
        if saved_base_dec > 0:
            return saved_base_dec

        try:
            current_fx_rate = Decimal(str(getattr(bill_obj, 'fx_rate_to_base', None) or '1.000000'))
        except Exception:
            current_fx_rate = Decimal('1.000000')
        if current_fx_rate <= 0:
            current_fx_rate = Decimal('1.000000')

        try:
            doc_price_dec = Decimal(str(getattr(item_obj, 'price', 0) or 0))
        except Exception:
            doc_price_dec = Decimal('0.0000')

        derived_from_doc = (doc_price_dec * current_fx_rate).quantize(Decimal('0.01'))

        # Only reached when o_price was never saved (e.g. legacy bills from
        # before this field existed). Try to recover the best approximation
        # available, in order of accuracy: a linked PO's own saved price,
        # then this bill's own price*fx, then finally the item's current
        # master cost price as a last resort.
        if getattr(bill_obj, 'order_number_id', None):
            linked_order_item = (
                PurchaseOrderItem.objects
                .filter(
                    purchase_order_id=bill_obj.order_number_id,
                    product_id=item_obj.product_id,
                    prd_brcd=item_obj.prd_brcd,
                )
                .order_by('pk')
                .first()
            )
            if linked_order_item and getattr(linked_order_item, 'o_price', None) not in zero_values:
                try:
                    return Decimal(str(linked_order_item.o_price))
                except Exception:
                    pass

        if derived_from_doc > 0:
            return derived_from_doc

        try:
            product_cost = Decimal(str(getattr(item_obj.product, 'cost_price', 0) or 0))
        except Exception:
            product_cost = Decimal('0.0000')
        if product_cost > 0:
            return product_cost

        return Decimal('0.0000')

    if request.method == "POST"and not readonly:
        # Permission: require Edit on Purchase Bills
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_bills(request.user)):
                messages.error(request, 'You do not have permission to edit bills.')
                return redirect_with_company('bill_list')
        except Exception:
            messages.error(request, 'You do not have permission to edit bills.')
            return redirect_with_company('bill_list')
        
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer

        company_db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(
                request.POST.get('date') or bill.date,
                request.user,
                db=company_db,
                transaction_type='bill'
            )
        except PermissionDenied as e:
            error_message = str(e)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_message}, status=403)
            messages.error(request, f"Cannot save bill: {error_message}")
            return redirect_with_company('bill_list')

        post_data = request.POST.copy()
        prd_brcd_map = {}
        for key in list(post_data.keys()):
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data.get(key, '')
                if value:
                    parts = value.split("_", 1)
                    post_data[key] = parts[0]
                    if len(parts) > 1:
                        prefix = key.rsplit("-", 1)[0]
                        prd_brcd_map[prefix] = parts[1]

        company_is_india = _is_indian_company_country(_get_purchase_company_country(request))
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        bill_form = BillForm(post_data, instance=bill)
        existing_items_qs = BillItem.objects.filter(bill=bill).order_by('pk')
        bill_formset = BillItemFormSet(post_data, queryset=BillItem.objects.none())

        if bill_form.is_valid() and bill_formset.is_valid():
            original_bill_number = bill.bill_number
            old_journal_entry = (
                JournalEntry.objects.using(company_db).filter(reference=original_bill_number)
                .exclude(narration__startswith='Reversal of')
                .prefetch_related('lines')
                .order_by('-id')
                .first()
            )

            with transaction.atomic():
                bill_obj = bill_form.save(commit=False)
                bill_obj.tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
                bill_obj.tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
                bill_obj.tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
                bill_obj.tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))

                pay_term_id = request.POST.get('payment_term')
                if pay_term_id:
                    try:
                        bill_obj.payment_term = PayTerms.objects.get(pk=pay_term_id)
                    except Exception:
                        bill_obj.payment_term = None
                else:
                    bill_obj.payment_term = None

                bill_obj.shipping_attention = request.POST.get('shipping_attention', '')
                bill_obj.shipping_email = request.POST.get('shipping_email', '')
                bill_obj.shipping_phone = request.POST.get('shipping_phone', '')
                bill_obj.shipping_country = request.POST.get('shipping_country', '')
                bill_obj.shipping_address1 = request.POST.get('shipping_address1', '')
                bill_obj.shipping_address2 = request.POST.get('shipping_address2', '')
                bill_obj.shipping_city = request.POST.get('shipping_city', '')
                bill_obj.shipping_state = request.POST.get('shipping_state', '')
                bill_obj.shipping_postal_code = request.POST.get('shipping_postal_code', '')
                bill_obj.place_of_supply = request.POST.get('place_of_supply', '') or ''
                bill_obj._current_request = request
                bill_obj._current_user = request.user
                bill_obj.save()

                calculated_total = Decimal('0.00')
                retained_items = []
                existing_items = list(existing_items_qs)
                active_form_count = 0

                for form in bill_formset.forms:
                    cleaned = getattr(form, 'cleaned_data', None) or {}
                    if not cleaned or cleaned.get('DELETE'):
                        continue

                    prefix = form.prefix
                    item = existing_items[active_form_count] if active_form_count < len(existing_items) else BillItem(bill=bill_obj)
                    active_form_count += 1

                    item.product = cleaned.get('product')
                    item.quantity = cleaned.get('quantity') or 0
                    item.price = cleaned.get('price') or Decimal('0.0000')
                    item.description = cleaned.get('description') or ''
                    item.prd_disvalue = cleaned.get('prd_disvalue') or Decimal('0.00')
                    item.prd_distype = cleaned.get('prd_distype') or 'flat'
                    item.prd_brcd = prd_brcd_map.get(prefix, getattr(item, 'prd_brcd', '') or '')
                    item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')

                    selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
                    item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                    try:
                        posted_o_price = Decimal(str(post_data.get(f'{prefix}-o_price', '0') or '0'))
                    except Exception:
                        posted_o_price = Decimal('0.0000')
                    if posted_o_price <= Decimal('0.0000') and getattr(item, 'o_price', None):
                        item.o_price = item.o_price
                    else:
                        item.o_price = posted_o_price

                    item.bill = bill_obj
                    item.save()
                    retained_items.append(item.pk)

                    qty = Decimal(item.quantity or 0)
                    price = Decimal(item.price or 0)
                    base = qty * price
                    discount_val = Decimal(item.prd_disvalue or 0)
                    if item.prd_distype == 'percent':
                        discounted = base - (base * discount_val / Decimal('100'))
                    else:
                        discounted = base - discount_val
                    if discounted < 0:
                        discounted = Decimal('0')

                    tax_amt = Decimal('0')
                    if item.prd_tax:
                        tax_amt = (discounted * Decimal(item.prd_tax)) / Decimal('100')

                    calculated_total += discounted + tax_amt

                stale_items = existing_items_qs.exclude(pk__in=retained_items)
                for stale_item in stale_items:
                    stale_item.delete()

                grand_discount_value = Decimal(str(request.POST.get('grand-discount-value', 0) or 0))
                grand_discount_type = request.POST.get('discount_type', 'percent')
                if grand_discount_type == 'percent':
                    grand_discount = calculated_total * grand_discount_value / Decimal('100')
                else:
                    grand_discount = grand_discount_value
                if grand_discount > calculated_total:
                    grand_discount = calculated_total

                bill_obj.total_amount = calculated_total - grand_discount
                bill_obj.discount_value = grand_discount_value
                bill_obj.discount_type = grand_discount_type
                bill_obj.save()

                try:
                    company = _get_company_for_request(request)
                    from currencies.services import apply_bill_fx, parse_currency_fx_from_post

                    doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
                    apply_bill_fx(
                        bill_obj,
                        company,
                        document_currency=doc_cur,
                        fx_rate_to_base_override=rate_override,
                        fx_rate_date_override=fx_date,
                    )
                except Exception:
                    logger.exception('Failed to apply FX to bill %s', bill_obj.pk)

                try:
                    if old_journal_entry:
                        last = JournalEntry.objects.using(company_db).order_by('-id').first()
                        if last and last.entry_number and last.entry_number.startswith('JV-'):
                            try:
                                last_num = int(last.entry_number.split('-')[1])
                            except Exception:
                                last_num = 0
                        else:
                            last_num = 0
                        next_num = last_num + 1
                        while JournalEntry.objects.using(company_db).filter(entry_number=f"JV-{str(next_num).zfill(5)}").exists():
                            next_num += 1

                        reversal_entry = JournalEntry.objects.using(company_db).create(
                            entry_number=f"JV-{str(next_num).zfill(5)}",
                            date=bill_obj.date,
                            reference=original_bill_number,
                            narration=f"Reversal of {old_journal_entry.entry_number}",
                            created_by=request.user,
                            updated_by=request.user,
                            status='posted'
                        )
                        for line in old_journal_entry.lines.all():
                            JournalLine.objects.using(company_db).create(
                                journal=reversal_entry,
                                account=line.account,
                                description=f"Reversal: {line.description}",
                                debit=line.credit,
                                credit=line.debit,
                                sequence=line.sequence
                            )
                        try:
                            reversal_entry.total_debit = sum(l.debit or 0 for l in reversal_entry.lines.all())
                            reversal_entry.total_credit = sum(l.credit or 0 for l in reversal_entry.lines.all())
                            reversal_entry.save(update_fields=['total_debit', 'total_credit'])
                        except Exception:
                            logger.exception('Failed to update totals for reversal entry %s', getattr(reversal_entry, 'id', None))

                    post_bill_to_journal(bill_obj, user=request.user)
                except Exception:
                    logger.exception('Failed to repost journal for bill %s', getattr(bill_obj, 'pk', None))

            messages.success(request, "Bill updated successfully!")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'bill_id': bill_obj.id,
                    'bill_number': bill_obj.bill_number,
                    'vendor_name': (
                        bill_obj.vendor.company_name
                        if bill_obj.vendor and bill_obj.vendor.vendor_type == 'company'
                        else (
                            f"{bill_obj.vendor.first_name} "
                            f"{bill_obj.vendor.last_name or ''}"
                        ).strip()
                        if bill_obj.vendor
                        else ''
                    ),
                    'total_amount': str(bill_obj.total_amount),
                    'currency_symbol': (
                        (getattr(getattr(bill_obj, 'document_currency', None), 'symbol', '') or
                         getattr(getattr(bill_obj, 'document_currency', None), 'code', '') or '₹').strip()
                    ),
                    'message': 'Bill updated successfully!'
                })
            return redirect_with_company('bill_list')
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                error_parts = []
                if bill_form.errors:
                    error_parts.extend(
                        f"{field}: {'; '.join(errors)}"
                        for field, errors in bill_form.errors.items()
                    )
                if bill_formset.errors:
                    for idx, form_errors in enumerate(bill_formset.errors, start=1):
                        if not form_errors:
                            continue
                        error_parts.extend(
                            f"Item {idx} {field}: {'; '.join(errors)}"
                            for field, errors in form_errors.items()
                        )
                if bill_formset.non_form_errors():
                    error_parts.extend(str(err) for err in bill_formset.non_form_errors())
                return JsonResponse({
                    'success': False,
                    'error': '\n'.join(error_parts) or 'Unable to validate this bill edit.'
                }, status=400)
    else:
        existing_items_qs = BillItem.objects.filter(bill=bill)
        bill_formset = BillItemFormSet(queryset=existing_items_qs)
        bill_form = BillForm(instance=bill)
        # #print("existing_items_qs:", existing_items_qs)
        item_display_list = []

        # Build UOM map keyed by both barcode id and barcode value
        uoms_by_barcode = {}
        uom_qs = Uom.objects.filter(
            item_id__in=existing_items_qs.values_list('product_id', flat=True),
            barcode_id__in=existing_items_qs.values_list('prd_brcd', flat=True).distinct()
        ).select_related('barcode', 'name')

        for u in uom_qs:
            # if barcode relation exists, add two keys:
            if getattr(u, 'barcode', None):
                # key using barcode record id (as int)
                try:
                    key_id = f"{u.item_id}_{int(u.barcode.id)}"
                    uoms_by_barcode[key_id] = u.name.name if u.name else ''
                except Exception:
                    pass

                # key using barcode value (string)
                try:
                    barcode_val = str(u.barcode.barcode)
                    key_val = f"{u.item_id}_{barcode_val}"
                    uoms_by_barcode[key_val] = u.name.name if u.name else ''
                except Exception:
                    pass
            else:
                # fallback: map by item id only (if needed)
                k = f"{u.item_id}_"
                uoms_by_barcode[k] = u.name.name if u.name else ''

            # #print("uoms_by_barcode[key]:", u.item_id, getattr(u.barcode, 'id', None), getattr(u.barcode, 'barcode', None), "=>", uoms_by_barcode.get(f"{u.item_id}_{getattr(u.barcode,'id','')}", ''))



        #  Fetch tax group names in bulk (to avoid N+1 queries)
        product_ids = existing_items_qs.values_list('product_id', flat=True)
        items_with_tax = Item.objects.filter(id__in=product_ids).select_related('intra_tax')

        # Build dict for quick lookup: { item_id: tax_group_name }
        # tax_map = {}
        tax_map = {itm.id: (itm.intra_tax.group_name if itm.intra_tax_id else '') for itm in items_with_tax}
        for itm in items_with_tax:
            tax_name = ''
            if itm.intra_tax_id:
                tax_group = TaxGroup.objects.filter(id=itm.intra_tax_id).first()
                if tax_group:
                    tax_name = tax_group.group_name
            tax_map[itm.id] = tax_name

        for form, item in zip(bill_formset.forms, existing_items_qs):
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            # #print(f"Checking key {key} → matched UOM: {uom_name}")

            #  Force the dropdown to display the correct text for this specific instance
            if uom_name:
                display_label = f"{item.product.name} ({uom_name})"
            else:
                display_label = item.product.name

            #  Replace the field choices with the selected item label
            form.fields['product'].choices = [
                (item.product.id, display_label)
            ]
            form.initial['product'] = item.product.id
            form.item = item
            try:
                saved_base_price = _resolve_bill_item_base_price(item, bill)
                form.initial['o_price'] = saved_base_price
                form.fields['o_price'].initial = saved_base_price
            except Exception:
                fallback_base_price = getattr(item, 'o_price', None) or item.price or Decimal('0.00')
                form.initial['o_price'] = fallback_base_price
                form.fields['o_price'].initial = fallback_base_price
            # #print(f" Updated dropdown for {display_label}")


        # for item in existing_items_qs:
            # #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

        #  Combine product + UOM + Tax Group for display
        for item in existing_items_qs:
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            display_name = f"{item.product.name} ({uom_name})" if uom_name else item.product.name

            tax_group_name = tax_map.get(item.product_id, '')

            item_display_list.append({
                'combined_id': key,
                'display_name': display_name,
                'quantity': item.quantity,
                'price': item.price,
                'prd_brcd': item.prd_brcd,
                'tax_group_name': tax_group_name,
            })
            # #print("item_display_list:", item_display_list)
            # #print("tax_group_name:", tax_group_name, flush=True)


    all_items = Item.objects.all()
    # return render(request, 'sales/order_duplicate.html', {
    #     'order_form': order_form,
    #     'sales_formset': sales_formset,
    #     'all_items': all_items,
    #     'today': localdate().isoformat(),
    #     'q_no': order.order_number,
    #     # 'item_display_list': item_display_list,
    #     'order': order, 
    #     'readonly': readonly,
        
    # })
    company=_get_company_for_request(request)
    # Pass both to the template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    context = {
        'bill_form': bill_form,
        'bill_formset': bill_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': bill.bill_number,
        'bill': bill,
        'readonly': readonly,
        # Shipping #by adarshaddress fields for prefilling
        'shipping_attention': bill.shipping_attention or '',
        'shipping_email': bill.shipping_email or '',
        'shipping_phone': bill.shipping_phone or '',
        'shipping_country': bill.shipping_country or '',
        'shipping_address1': bill.shipping_address1 or '',
        'shipping_address2': bill.shipping_address2 or '',
        'shipping_city': bill.shipping_city or '',
        'shipping_state': bill.shipping_state or '',
        'shipping_postal_code': bill.shipping_postal_code or '',#by adarsh
        'place_of_supply': bill.place_of_supply or '',
        'company_is_india': _is_indian_company_country(_get_purchase_company_country(request)),
        'company_tax_type': _get_purchase_company_tax_type(request),
        'tds_tcs_type': bill.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': bill.tds_tcs_definition_id or '',
        'tds_tcs_rate': bill.tds_tcs_rate or 0,
        'tds_tcs_amount': bill.tds_tcs_amount or 0,
                # ✅ Fetch TDS and TCS for quotation_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=company, is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=company, is_active=True),
        'show_base_transaction_summary': bool(getattr(company, 'show_base_transaction_summary', True)),
    }
    try:
        company = _get_company_for_request(request)
        from currencies.models import Currency
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
        context['company_currencies'] = company_currencies
        base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
        context['company_base_currency_symbol'] = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
        context['company_base_currency_code'] = base_currency.code if base_currency else ''
    except Exception:
        context['company_currencies'] = []
        context['company_base_currency_symbol'] = '₹'
        context['company_base_currency_code'] = ''
    if readonly:
        for form in bill_formset.forms:
            for field_name, field in form.fields.items():
                form.fields['product'].widget.attrs['disabled'] = True
                form.fields['prd_tax'].widget.attrs['disabled'] = True

                widget = field.widget
                if widget.__class__.__name__ in ['Select', 'SelectMultiple', 'CheckboxInput', 'RadioSelect']:
                    widget.attrs['disabled'] = True  # disable selects and similar widgets
                else:
                    widget.attrs['readonly'] = True
        # Render a dedicated readonly template without edit actions/buttons
        return render(request, 'Purchase/bill_duplicate.html', context)
    else:
        # Render the editable template
        return render(request, 'Purchase/bill_duplicate.html', context)


@transaction.atomic
def bill_duplicate(request, pk):
    print("bill duplicate clled")
    company_db = getattr(request, 'company_db', 'default')
    old_bill = get_object_or_404(Bill.objects.using(company_db), pk=pk)
    original_payment_status = old_bill.payment_status
    original_allocations = list(
        BillPaymentAllocation.objects.filter(bill=old_bill)
        .values('payment_id', 'amount', 'payment_made_on')
    )

    # Capture journal data BEFORE deleting old bill
    bill_number = old_bill.bill_number
    old_journal_entry = JournalEntry.objects.using(company_db).filter(
        reference=bill_number
    ).exclude(
        narration__startswith='Reversal of'
    ).prefetch_related('lines').order_by('-id').first()

    # ------------------ PERMISSION CHECK ------------------
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_bills(request.user)):
            messages.error(request, 'You do not have permission to create bills.')
            return redirect_with_company('bill_list')
    except Exception:
        #Added by neha on 17-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'You do not have permission to create bills.'
            }, status=403)
        messages.error(request, 'You do not have permission to create bills.')
        return redirect_with_company('bill_list')

    # ✅ CHECK PERIOD LOCK BEFORE EDITING/CREATING BILL
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    try:
        original_bill_id = old_bill.pk
        original_items = list(BillItem.objects.using(company_db).filter(bill=old_bill))

        prefix_obj = BillPrefix.objects.first()
        prefix = prefix_obj.prefix if prefix_obj else "BN"
        max_number = 0
        for number in Bill.objects.using(company_db).values_list('bill_number', flat=True):
            match = re.search(r'(\d+)', number or '')
            if match:
                max_number = max(max_number, int(match.group(1)))
        new_bill_number = f"{prefix}{str(max_number + 1).zfill(3)}"

        new_bill = old_bill
        new_bill.pk = None
        new_bill.id = None
        new_bill.bill_number = new_bill_number
        new_bill.status = 'Draft'
        new_bill.save(using=company_db)

        for item in original_items:
            item.pk = None
            item.id = None
            item.bill = new_bill
            item.save(using=company_db)

        messages.success(request, f"Bill duplicated! New bill: {new_bill.bill_number}")
        return redirect_with_company('bill_detail', pk=new_bill.pk)
    except Exception as e:
        logger.exception("Error duplicating bill %s: %s", old_bill.pk or pk, e)
        messages.error(request, "Failed to duplicate bill.")
        return redirect_with_company('bill_detail', pk=original_bill_id if 'original_bill_id' in locals() else pk)

    bill_date = request.POST.get('date') or old_bill.date
    try:
        PeriodLockEnforcer.check_can_edit(bill_date, request.user, db=db, transaction_type='bill')
    except PermissionDenied as e:
        error_message = str(e)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_message}, status=403)
        messages.error(request, f"Cannot save bill: {error_message}")
        return redirect_with_company('bill_list')

    # ------------------ DELETE OLD BILL ------------------
    try:
        BillItem.objects.using(company_db).filter(bill=old_bill).delete()
        Bill.objects.using(company_db).filter(pk=old_bill.pk).delete()
    except Exception as e:
        logger.exception("Error deleting old bill: %s", e)
        #Added by neha on 17-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Unable to replace the existing bill.'
            }, status=400)
        messages.error(request, "Unable to replace the existing bill.")
        return redirect_with_company('bill_list')

    # ------------------ PREPARE POST DATA ------------------
    try:
        post_data = request.POST.copy()
        prd_brcd_map = {}
    except Exception as e:
        logger.exception("POST data error: %s", e)
        messages.error(request, "Unexpected error occurred.")
        return redirect_with_company('bill_list')

    # ------------------ FIX PRODUCT + BARCODE ------------------
    for key in post_data:
        if key.startswith("form-") and key.endswith("-product"):
            value = post_data[key]
            if value:
                parts = value.split("_", 1)
                post_data[key] = parts[0]
                if len(parts) > 1:
                    prefix = key.rsplit("-", 1)[0]
                    prd_brcd_map[prefix] = parts[1]

    # ------------------ HEADER DATA ------------------
    vendor_id = request.POST.get('vendor')
    date = request.POST.get('date')
    notes = request.POST.get('notes', '')
    bill_number = request.POST.get('bill_number')
    order_number = request.POST.get('order')

    discount_value = request.POST.get('grand-discount-value', 0)
    discount_type = request.POST.get('discount_type', 'percent')

    if not vendor_id or not date :
        #Added by neha on 16-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Vendor and Date are required.'
            }, status=400)
        messages.error(request, "Vendor and Date are required.")
        return redirect_with_company('bill_list')

    try:
        vendor = Vendor.objects.using(company_db).get(pk=vendor_id)
        # order = PurchaseOrder.objects.get(pk=order_number_id)
    except Exception:
        messages.error(request, "Invalid Vendor.")
        return redirect_with_company('bill_list')
    
    # Initialize warehouse as None (optional field)
    wh = None
    
    # Attach warehouse if provided
    ware_house_id = request.POST.get('warehouse')
    print("ware_house_id",ware_house_id)
    if ware_house_id:
        try:
            wh = Warehouse.objects.using(company_db).get(pk=ware_house_id)
                    
        except Warehouse.DoesNotExist:
            messages.error(request, "Selected warehouse does not exist.")
            return redirect_with_company('bill_list')

    # ------------------ CREATE NEW BILL ------------------
    try:
        bill = Bill.objects.using(company_db).create(
            vendor=vendor,
            date=date,
            order=order_number,
            bill_number=bill_number,
            payment_status=original_payment_status,
            total_amount=0,
            notes=notes,
            discount_value=discount_value,
            discount_type=discount_type,
            warehouse=wh,
            # Capture shipping address fields
            shipping_attention=request.POST.get('shipping_attention', ''),
            shipping_email=request.POST.get('shipping_email', ''),
            shipping_phone=request.POST.get('shipping_phone', ''),
            shipping_country=request.POST.get('shipping_country', ''),
            shipping_address1=request.POST.get('shipping_address1', ''),
            shipping_address2=request.POST.get('shipping_address2', ''),
            shipping_city=request.POST.get('shipping_city', ''),
            shipping_state=request.POST.get('shipping_state', ''),
            shipping_postal_code=request.POST.get('shipping_postal_code', ''),
            place_of_supply=request.POST.get('place_of_supply', ''),
            tds_tcs_type=request.POST.get('tds_tcs_type', 'tds'),
            tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
            tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
            tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
        )

        pay_term_id = request.POST.get('payment_term')
        if pay_term_id:
            bill.payment_term = PayTerms.objects.using(company_db).filter(pk=pay_term_id).first()
            bill.save(using=company_db)

        # Attach request context so posting uses correct company DB
        bill._current_request = request
        bill._current_user = request.user

        doc_cur = None
        # Apply document currency and FX values based on vendor/company
        try:
            company = _get_company_for_request(request)
            from currencies.services import apply_bill_fx, parse_currency_fx_from_post
            doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
            apply_bill_fx(
                bill,
                company,
                document_currency=doc_cur,
                fx_rate_to_base_override=rate_override,
                fx_rate_date_override=fx_date,
            )
        except Exception:
            logger.exception('Failed to apply FX to bill %s', getattr(bill, 'pk', None))

    except Exception as e:
        logger.exception("Bill header creation failed: %s", e)
        messages.error(request, "Error creating bill.")
        return redirect_with_company('bill_list')

    # ------------------ BILL ITEMS ------------------
    BillItemFormSet = modelformset_factory(
        BillItem, form=BillItemForm, extra=0, can_delete=True
    )

    formset = BillItemFormSet(post_data, queryset=BillItem.objects.using(company_db).none())

    if not formset.is_valid():
        logger.error("Formset errors: %s", formset.errors)
        messages.error(request, "Item validation failed.")
        return redirect_with_company('bill_list')

    calculated_total = Decimal('0.00')

    for index, item in enumerate(formset.save(commit=False)):
        item.pk = None
        prefix = f"form-{index}"

        if prefix in prd_brcd_map:
            item.prd_brcd = prd_brcd_map[prefix]

        item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')

        selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)

        item.bill = bill
        item.save(using=company_db)

        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        disc = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discounted = base - (base * disc / Decimal('100'))
        else:
            discounted = base - disc

        discounted = max(discounted, Decimal('0'))
        tax_amt = (discounted * item.prd_tax / Decimal('100')) if item.prd_tax else Decimal('0')
        calculated_total += discounted + tax_amt

    # ------------------ GRAND DISCOUNT ------------------
    grand_discount_value = Decimal(str(discount_value))
    if discount_type == 'percent':
        grand_discount = calculated_total * grand_discount_value / Decimal('100')
    else:
        grand_discount = grand_discount_value

    grand_discount = min(grand_discount, calculated_total)

    bill.total_amount = calculated_total - grand_discount
    bill.discount_value = grand_discount_value
    bill.discount_type = discount_type
    bill.save()

    # === JOURNAL REVERSAL & POSTING ===
    try:
        with transaction.atomic():
            # Step 1: Create reversal for old journal entry
            if old_journal_entry:
                last = JournalEntry.objects.using(company_db).order_by('-id').first()
                if last and last.entry_number and last.entry_number.startswith('JV-'):
                    try:
                        last_num = int(last.entry_number.split('-')[1])
                    except Exception:
                        last_num = 0
                else:
                    last_num = 0
                next_num = last_num + 1
                while JournalEntry.objects.using(company_db).filter(entry_number=f"JV-{str(next_num).zfill(5)}").exists():
                    next_num += 1
                reversal_entry_number = f"JV-{str(next_num).zfill(5)}"
                
                reversal_entry = JournalEntry.objects.using(company_db).create(
                    entry_number=reversal_entry_number,
                    date=bill.date,
                    reference=old_journal_entry.reference,
                    narration=f"Reversal of {old_journal_entry.entry_number}",
                    created_by=request.user,
                    updated_by=request.user,
                    status='posted'
                )
                
                for line in old_journal_entry.lines.all():
                    JournalLine.objects.using(company_db).create(
                        journal=reversal_entry,
                        account=line.account,
                        description=f"Reversal: {line.description}",
                        debit=line.credit,
                        credit=line.debit,
                        sequence=line.sequence
                    )
                # Recalculate and persist totals for the reversal entry so list shows correct amounts
                try:
                    reversal_entry.total_debit = sum(l.debit or 0 for l in reversal_entry.lines.all())
                    reversal_entry.total_credit = sum(l.credit or 0 for l in reversal_entry.lines.all())
                    reversal_entry.save(update_fields=['total_debit', 'total_credit'])
                except Exception:
                    logger.exception('Failed to update totals for reversal entry %s', getattr(reversal_entry, 'id', None))
            
            # Step 2: Post new journal entry
            post_bill_to_journal(bill, user=request.user)
    except Exception as e:
        import traceback
        traceback.print_exc()

    # Restore invoice allocations that were deleted with the old invoice.
    for allocation in original_allocations:
        payment_id = allocation.get('payment_id')
        if not payment_id:
            continue
        if BillPayment.objects.using(company_db).filter(pk=payment_id).exists():
            BillPaymentAllocation.objects.using(company_db).create(
                payment_id=payment_id,
                bill=bill,
                amount=allocation.get('amount') or Decimal('0.00'),
                payment_made_on=allocation.get('payment_made_on')
            )

    messages.success(request, "Bill edited successfully.")
    #Added by neha on 16-2-26 for ajax request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'bill_id': bill.id,
            'bill_number': bill.bill_number,
            'vendor_name': (
                        bill.vendor.company_name
                        if bill.vendor and bill.vendor.vendor_type == 'company'
                        else (
                            f"{bill.vendor.first_name} "
                            f"{bill.vendor.last_name or ''}"
                        ).strip()
                        if bill.vendor
                        else ''
                    ),
            'total_amount': str(bill.total_amount),
            'currency_symbol': (getattr(getattr(bill, 'document_currency', None), 'symbol', '') or getattr(getattr(bill, 'document_currency', None), 'code', '') or getattr(doc_cur, 'symbol', '') or getattr(doc_cur, 'code', '') or '₹').strip(),
            'message': 'Bill edited successfully!'
        })
    return redirect_with_company('bill_list')



def record_payment_view(request, pk):
    """Render a simple readonly detail page for a bill."""
    # Permission: require view access to Purchase Payments Made
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_payments(request.user)):
            messages.error(request, 'You do not have permission to view payments.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view payments.')
        return redirect_with_company('index')

    return add_payment(request, pk)

    bill = get_object_or_404(Bill, pk=pk)
    
    # Compute final_total only (mirror your save logic)
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    
    for item in BillItem.objects.filter(bill=bill):
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price
        
        # Discount
        discount_val = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discount_amount = (base * discount_val) / Decimal('100')
        else:
            discount_amount = discount_val
        if discount_amount > base:
            discount_amount = base
        discounted = max(base - discount_amount, Decimal('0.00'))
        
        # Tax
        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
        line_total = discounted + tax_amount
        
        subtotal_calc += line_total
        total_tax += tax_amount

    total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')

    final_total = bill.total_amount - total_paid

    # Get vendor's advance balance
    advance_balance = get_vendor_advance_balance(bill.vendor.id)
    print(f"DEBUG: Vendor {bill.vendor.id} advance balance: {advance_balance}")
    
    # Calculate net amount to pay (after advance deduction)
    advance_to_use = min(advance_balance, final_total)
    net_amount = max(final_total - advance_balance, Decimal('0.00'))
    remaining_advance = advance_balance - advance_to_use
    
    print(f"DEBUG: Bill amount: {final_total}, Advance to use: {advance_to_use}, Net to pay: {net_amount}")

    # Load payment accounts and modes changed by neha on 18-2-26
    payment_account_headers = ChartOfAccounts.objects.filter(
        active=True,
        is_header=True,
        id__in=[17, 89, 66]
    )
    payment_accounts = ChartOfAccounts.objects.filter(
        active=True,
        status=True
        ).filter(
            Q(id__in=payment_account_headers.values('id')) |
            Q(parent__in=payment_account_headers)
    ).order_by('name')

    payment_accounts_grouped = []
    for header in payment_account_headers.order_by('name'):
        children = ChartOfAccounts.objects.filter(
            active=True,
            status=True,
            parent=header
        ).order_by('name')
        
        payment_accounts_grouped.append({
            'header': header,
            'children': children,
        })
    payment_account_header_ids = list(payment_account_headers.values_list('id', flat=True))

    # Load payment modes dynamically
    # payment_modes = PaymentMode.objects.all().order_by('name')
    
    # Generate next payment number
    next_payment_number = BillPayment.generate_payment_number()

    context = {
        'bill_number': bill.bill_number,
        'final_total': final_total,
        'bill': bill,
        'payment_accounts': payment_accounts,
        'payment_accounts_grouped': payment_accounts_grouped,
        'payment_account_header_ids': payment_account_header_ids,
        # 'payment_modes': payment_modes,
        'next_payment_number': next_payment_number,
        # Add advance-related context
        'advance_balance': advance_balance,
        'has_advance': advance_balance > 0,
        'advance_to_use': advance_to_use,
        'net_amount_to_pay': net_amount,
        'remaining_advance': remaining_advance,
    }
    return render(request, 'Purchase/record_payment.html', context)


# @login_required
# @transaction.atomic
# def record_payment(request, pk):
#     """Record payment for a bill"""
#     bill = get_object_or_404(Bill, pk=pk)
    
#     # Calculate final total (your existing logic)
#     subtotal_calc = Decimal('0.00')
#     total_tax = Decimal('0.00')
    
#     for item in BillItem.objects.filter(bill=bill):
#         qty = Decimal(item.quantity or 0)
#         price = Decimal(item.price or 0)
#         base = qty * price
        
#         # Discount
#         discount_val = Decimal(item.prd_disvalue or 0)
#         if item.prd_distype == 'percent':
#             discount_amount = (base * discount_val) / Decimal('100')
#         else:
#             discount_amount = discount_val
#         if discount_amount > base:
#             discount_amount = base
#         discounted = max(base - discount_amount, Decimal('0.00'))
        
#         # Tax
#         tax_rate = Decimal(item.prd_tax or 0)
#         tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
#         line_total = discounted + tax_amount
        
#         subtotal_calc += line_total
#         total_tax += tax_amount
    
#     # Grand discount
#     grand_discount_value = Decimal(str(bill.discount_value or 0))
#     grand_discount_type = bill.discount_type or 'percent'
#     if grand_discount_type == 'percent':
#         grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
#     else:
#         grand_discount = grand_discount_value
#     if grand_discount > subtotal_calc:
#         grand_discount = subtotal_calc
    
#     final_total = subtotal_calc - grand_discount
    
#     # Calculate remaining amount to pay
#     total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
#         total=Sum('amount')
#     )['total'] or Decimal('0.00')
#     remaining_amount = bill.total_amount - total_paid
    
#     # Load payment accounts and modes
#     payment_accounts = ChartOfAccounts.objects.filter(
#         active=1,
#         is_header=0,
#         name__in=[
#             'Cash',
#             'Bank Account',
#             'Bank Overdraft Account',
#             'Loans and Advances (Assets)'
#         ]
#     ).order_by('name')
    
#     payment_modes = PaymentMode.objects.all().order_by('name')
    
#     # Generate next payment number
#     next_payment_number = BillPayment.generate_payment_number()
    
#     # Handle POST request - Save payment
#     if request.method == 'POST':
#         print(f"DEBUG FILES: {request.FILES}")
#         print(f"DEBUG FILES.getlist('attachments'): {request.FILES.getlist('attachments')}")
#         try:
#             with transaction.atomic():
#                 # Extract form data
#                 amount = request.POST.get('amount')
#                 payment_mode_id = request.POST.get('payment_mode')
#                 payment_date = request.POST.get('payment_date')
#                 payment_number = request.POST.get('payment_number')
#                 payment_made_on = request.POST.get('payment_made_on')
#                 paid_through_id = request.POST.get('paid_through')
#                 reference = request.POST.get('reference', '')
#                 notes = request.POST.get('notes', '')
#                 send_email = request.POST.get('send_email') == '1'
#                 # Get vendor_id from form or automatically from bill
#                 vendor_id = request.POST.get('vendor_id') or bill.vendor_id
                
#                 print(f"DEBUG record_payment: amount={amount}, payment_number={payment_number}, bill={bill.id}")
                
#                 # Validate required fields
#                 if not all([amount, payment_mode_id, payment_date, payment_number, paid_through_id]):
#                     messages.error(request, 'Please fill in all required fields.')
#                     return render(request, 'Purchase/record_payment.html', {
#                         'bill': bill,
#                         'final_total': final_total,
#                         'remaining_amount': remaining_amount,
#                         'payment_accounts': payment_accounts,
#                         'payment_modes': payment_modes,
#                         'next_payment_number': next_payment_number,
#                     })
                
#                 # Get foreign key objects
#                 vendor = get_object_or_404(Vendor, pk=vendor_id)
#                 payment_mode = get_object_or_404(PaymentMode, pk=payment_mode_id)
#                 paid_through = get_object_or_404(ChartOfAccounts, pk=paid_through_id, active=1)
                
#                 # Validate amount
#                 amount_decimal = Decimal(amount)
#                 if amount_decimal <= 0:
#                     messages.error(request, 'Payment amount must be greater than zero.')
#                     return render(request, 'Purchase/record_payment.html', {
#                         'bill': bill,
#                         'final_total': final_total,
#                         'remaining_amount': remaining_amount,
#                         'payment_accounts': payment_accounts,
#                         'payment_modes': payment_modes,
#                         'next_payment_number': next_payment_number,
#                     })
                
#                 # Allow overpayment (will be treated as advance)
#                 # No need to check if payment exceeds bill amount
                
#                 print(f"DEBUG: Creating BillPayment record...")
                
#                 # Create payment record (NO bill field!)
#                 payment = BillPayment.objects.create(
#                     vendor=vendor,
#                     payment_mode=payment_mode,
#                     paid_through=paid_through,
#                     amount=amount_decimal,
#                     payment_number=payment_number,
#                     payment_date=payment_date,
#                     payment_made_on=payment_made_on if payment_made_on else None,
#                     reference=reference,
#                     notes=notes,
#                     send_email=send_email,
#                     created_by=request.user,
#                     updated_by=request.user
#                 )
                
#                 print(f"DEBUG: Payment created with ID={payment.id}")
                
#                 # Create allocation linking payment to bill
#                 allocation_amount = min(amount_decimal, remaining_amount)
                
#                 print(f"DEBUG: Creating allocation for ₹{allocation_amount}")
                
#                 allocation = BillPaymentAllocation.objects.create(
#                     payment=payment,
#                     bill=bill,
#                     amount=allocation_amount,
#                     payment_made_on=payment_made_on if payment_made_on else None
#                 )
                
#                 print(f"DEBUG: Allocation created with ID={allocation.id}")
                
#                 # Update bill status
#                 new_total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
#                     total=Sum('amount')
#                 )['total'] or Decimal('0.00')
                
#                 if new_total_paid >= bill.total_amount:
#                     bill.status = 'Paid'
#                 elif new_total_paid > 0:
#                     bill.status = 'Partial'
#                 else:
#                     bill.status = 'Open'
#                 bill.save()
                
#                 print(f"DEBUG: Bill status updated to {bill.status}")
                
#                 # If payment exceeds bill amount, note it as advance
#                 advance_amount = amount_decimal - allocation_amount
                
#                 # ----------------- HANDLE FILE ATTACHMENTS -----------------
#                 files = request.FILES.getlist('attachments')

#                 # Max 5 files validation
#                 if len(files) > 5:
#                     messages.error(request, 'You can upload a maximum of 5 files.')
#                     raise ValueError("Too many attachments")

#                 allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png', '.doc', '.docx')
#                 max_file_size = 10 * 1024 * 1024  # 10MB

#                 uploaded_count = 0
#                 skipped_files = []

#                 for file in files:
#                     # Extension validation
#                     if not file.name.lower().endswith(allowed_extensions):
#                         skipped_files.append({
#                             'name': file.name,
#                             'reason': 'unsupported file type'
#                         })
#                         continue

#                     # File size validation
#                     if file.size > max_file_size:
#                         skipped_files.append({
#                             'name': file.name,
#                             'reason': 'exceeds 10MB limit'
#                         })
#                         continue

#                     try:
#                         # Save attachment
#                         BillPaymentAttachment.objects.create(
#                             payment=payment,
#                             file=file,
#                             uploaded_by=request.user
#                         )
#                         uploaded_count += 1
#                         print(f"DEBUG: Attachment uploaded: {file.name}")
#                     except Exception as e:
#                         skipped_files.append({
#                             'name': file.name,
#                             'reason': f'upload error: {str(e)}'
#                         })

#                 # Provide user feedback
#                 if uploaded_count > 0:
#                     messages.success(
#                         request,
#                         f'{uploaded_count} file(s) uploaded successfully.'
#                     )

#                 if skipped_files:
#                     for skipped in skipped_files:
#                         messages.warning(
#                             request,
#                             f'File "{skipped["name"]}" was skipped: {skipped["reason"]}.'
#                         )
                
#                 # Optional: Send email notification
#                 if send_email:
#                     try:
#                         # TODO: Implement email sending logic
#                         # send_payment_notification_email(payment)
#                         pass
#                     except Exception as e:
#                         logger.error(f"Failed to send email notification: {e}")
#                         messages.warning(request, 'Payment saved but email notification failed.')
                
#                 # Success message
#                 if advance_amount > 0:
#                     messages.success(
#                         request, 
#                         f'Payment #{payment.payment_number} recorded successfully! '
#                         f'Bill Payment: ₹{allocation_amount:.2f}, Advance: ₹{advance_amount:.2f}'
#                     )
#                 else:
#                     messages.success(
#                         request, 
#                         f'Payment #{payment.payment_number} of ₹{payment.amount} recorded successfully.'
#                     )
                
#                 print(f"SUCCESS: Transaction committed")
#                 return redirect('bill_detail', pk=bill.id)  # or 'payment_list'
            
#         except Exception as e:
#             print(f"ERROR in record_payment: {str(e)}")
#             import traceback
#             traceback.print_exc()
#             messages.error(request, f'Error recording payment: {str(e)}')
#             return render(request, 'Purchase/record_payment.html', {
#                 'bill': bill,
#                 'final_total': final_total,
#                 'remaining_amount': remaining_amount,
#                 'payment_accounts': payment_accounts,
#                 'payment_modes': payment_modes,
#                 'next_payment_number': next_payment_number,
#             })
    
#     # GET request - Display form
#     context = {
#         'bill_number': bill.bill_number,
#         'bill': bill,
#         'final_total': final_total,
#         'remaining_amount': remaining_amount,
#         'payment_accounts': payment_accounts,
#         'payment_modes': payment_modes,
#         'next_payment_number': next_payment_number,
#     }
#     return render(request, 'Purchase/record_payment.html', context)

@login_required
@transaction.atomic
def record_payment(request, pk):
    """
    Record payment for a specific bill with automatic advance management.
    This function delegates to save_payment which handles all the advance logic.
    """
    if request.method == 'POST':
        # Delegate to save_payment which handles advance automatically
        return save_payment(request, pk)
    
    # GET request - show the form (handled by record_payment_view)
    return record_payment_view(request, pk)

    
def payment_list(request):
    # Permission: require view access to Purchase Payments Made
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_payments(request.user)):
            messages.error(request, 'You do not have permission to view payments.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view payments.')
        return redirect_with_company('index')
    
    search_query = request.GET.get('q', '')
    payment_status_filter = request.GET.get('payment_status', '')
    
    payments = BillPayment.objects.all()
    
    if search_query:
        payments = payments.filter(
            Q(amount__icontains=search_query) |
            Q(vendor__first_name__icontains=search_query) |
            Q(vendor__company_name__icontains=search_query) |
            Q(payment_number__icontains=search_query) |
            Q(reference__icontains=search_query)
        )
    
    # Filter by payment status (partial or full)
    if payment_status_filter:
        all_payments = list(payments.order_by('-payment_date', '-id'))
        
        if payment_status_filter == 'full':
            # Get payments where all allocated bills are FULLY PAID
            all_payments = [p for p in all_payments 
                           if p.bill_allocations.exists() and 
                           all(alloc.bill.payment_status and alloc.bill.payment_status.name == 'Paid' for alloc in p.bill_allocations.all())]
        elif payment_status_filter == 'partial':
            # Get payments where at least one allocated bill is NOT fully paid
            all_payments = [p for p in all_payments 
                           if p.bill_allocations.exists() and 
                           any(not alloc.bill.payment_status or alloc.bill.payment_status.name != 'Paid' for alloc in p.bill_allocations.all())]
        
        payments = all_payments
    else:
        payments = payments.order_by('-payment_date', '-id')
    
    paginator = Paginator(payments, 10)
    page_number = request.GET.get('page')
    payment_page = paginator.get_page(page_number)

    return render(request, "Purchase/payment_list.html", {
        "payments": payment_page,
        "search_query": search_query,
        "payment_status_filter": payment_status_filter,
        "can_view_purchase_payments": (getattr(request.user, 'is_superuser', False) or can_view_purchase_payments(request.user)),
        "can_create_purchase_payments": (getattr(request.user, 'is_superuser', False) or can_create_purchase_payments(request.user)),
        "can_edit_purchase_payments": (getattr(request.user, 'is_superuser', False) or can_edit_purchase_payments(request.user)),
        "can_delete_purchase_payments": (getattr(request.user, 'is_superuser', False) or can_delete_purchase_payments(request.user)),
    })


# def add_payment(request, pk=None):
#     """
#     Render payment recording page and handle payment submission.
#     If pk is provided, it's for a specific bill.
#     If pk is None, show vendor selection first.
#     """
    
#     # Handle POST request - save payment
#     if request.method == 'POST':
#         return save_payment(request, pk)
    
#     # Load payment accounts and modes (needed for both cases)
#     payment_accounts = ChartOfAccounts.objects.filter(
#         active=1,
#         is_header=1,
#         name__in=[
#             'Cash',
#             'Bank Account',
#             'Bank Overdraft Account',
#             'Loans and Advances (Assets)'
#         ]
#     ).order_by('name')
    
#     payment_modes = PaymentMode.objects.all().order_by('name')
#     next_payment_number = BillPayment.generate_payment_number()
    
#     # Case 1: New payment without specific bill - show vendor selection
#     if pk is None:
#         vendors = Vendor.objects.filter(is_active=1).order_by('id')
#         print(f"Number of vendors found: {vendors.count()}") 
#         context = {
#             'vendors': vendors,
#             'payment_accounts': payment_accounts,
#             'payment_modes': payment_modes,
#             'next_payment_number': next_payment_number,
#             'is_new_payment': True,
#         }
#         return render(request, 'Purchase/add_payment.html', context)
    
#     # Case 2: Payment for specific bill
#     bill = get_object_or_404(Bill, pk=pk)
    
#     # Calculate remaining amount for this bill using allocations
#     total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
#         total=Sum('amount')
#     )['total'] or Decimal('0.00')
    
#     final_total = bill.total_amount - total_paid
    
#     context = {
#         'bill_number': bill.bill_number,
#         'final_total': final_total,
#         'bill': bill,
#         'payment_accounts': payment_accounts,
#         'payment_modes': payment_modes,
#         'next_payment_number': next_payment_number,
#         'is_new_payment': False,
#     }
#     return render(request, 'Purchase/add_payment.html', context)


def add_payment(request, pk=None):
    """
    Render payment recording page with advance balance information.
    """
    if request.method == 'POST':
        print("calling save_payment")
        return save_payment(request, pk)
    
    # Load payment accounts and modes changed by neha on 18-2-26
    payment_account_headers = ChartOfAccounts.objects.filter(
        active=True,
        is_header=True,
        id__in=[17, 89, 66]
    )
    payment_accounts = ChartOfAccounts.objects.filter(
        active=True,
        status=True
        ).filter(
            Q(id__in=payment_account_headers.values('id')) |
            Q(parent__in=payment_account_headers)
    ).order_by('name')

    payment_accounts_grouped = []
    for header in payment_account_headers.order_by('name'):
        children = ChartOfAccounts.objects.filter(
            active=True,
            status=True,
            parent=header
        ).order_by('name')
        
        payment_accounts_grouped.append({
            'header': header,
            'children': children,
        })
    payment_account_header_ids = list(payment_account_headers.values_list('id', flat=True))
    
    # payment_modes = PaymentMode.objects.all().order_by('name')
    next_payment_number = BillPayment.generate_payment_number()
    
    # Case 1: New payment - show vendor selection
    if pk is None:
        vendors = Vendor.objects.filter(is_active=True).order_by('vendor_code')
        
        context = {
            'vendors': vendors,
            'payment_accounts': payment_accounts,
            'payment_account_header_ids': payment_account_header_ids,
            'payment_accounts_grouped': payment_accounts_grouped,
            # 'payment_modes': payment_modes,
            'next_payment_number': next_payment_number,
            'is_new_payment': True,
            'payment_currency_symbol': '₹',
        }
        return render(request, 'Purchase/add_payment.html', context)
    
    # Case 2: Payment for specific bill
    bill = get_object_or_404(Bill, pk=pk)
    
    # Calculate remaining amount
    total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')
    final_total = bill.total_amount - total_paid
    
    # Get vendor's advance balance
    advance_balance = get_vendor_advance_balance(bill.vendor.id)
    print("advance_balance",advance_balance)
    
    # Calculate net amount to pay
    advance_to_use = min(advance_balance, final_total)
    print("advance_to_use",advance_to_use)
    net_amount = max(final_total - advance_balance, Decimal('0.00'))
    remaining_advance = advance_balance - advance_to_use
    
    context = {
        'bill_number': bill.bill_number,
        'final_total': final_total,
        'bill': bill,
        'payment_accounts': payment_accounts,
        'payment_account_header_ids': payment_account_header_ids,
        'payment_accounts_grouped': payment_accounts_grouped,
        # 'payment_modes': payment_modes,
        'next_payment_number': next_payment_number,
        'is_new_payment': False,
        'advance_balance': advance_balance,
        'has_advance': advance_balance > 0,
        'advance_to_use': advance_to_use,
        'net_amount_to_pay': net_amount,
        'remaining_advance': remaining_advance,
        'payment_currency_symbol': bill.get_currency_symbol(),
    }
    return render(request, 'Purchase/add_payment.html', context)

    #by adarsh
def _resolve_purchase_currency_symbol(vendor=None, bill=None, payment=None):
    if payment is not None:
        try:
            return payment.get_currency_symbol()
        except Exception:
            pass
    if bill is not None:
        try:
            return bill.get_currency_symbol()
        except Exception:
            pass
    if vendor is not None and getattr(vendor, 'currency', None):
        try:
            from currencies.models import Currency
            currency_obj = Currency.objects.filter(code=vendor.currency, is_active=True).first()
            if currency_obj:
                return currency_obj.symbol or currency_obj.code
        except Exception:
            pass
        return vendor.currency
    return '₹'


def _format_purchase_currency(amount, symbol):
    try:
        normalized = Decimal(str(amount or 0)).quantize(Decimal('0.01'))
    except Exception:
        normalized = Decimal('0.00')
    return f"{symbol}{normalized:.2f}"


def _get_bill_payment_journal_base_amount(payment):
    from currencies.services import get_effective_rate_to_base, resolve_currency_for_vendor, scale_amount_for_journal

    payment_amount = Decimal(str(payment.amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    allocations = list(payment.bill_allocations.select_related('bill').all())
    if allocations:
        total_allocated = sum(Decimal(str(allocation.amount or 0)) for allocation in allocations)
        if total_allocated > 0:
            advance_added = sum(
                Decimal(str(txn.amount or 0))
                for txn in payment.advance_transactions.filter(amount__gt=0)
            )
            cash_applied_to_bills = payment_amount - advance_added
            if cash_applied_to_bills <= 0:
                cash_applied_to_bills = min(payment_amount, total_allocated)

            remaining_cash = cash_applied_to_bills
            journal_amount = Decimal('0.00')

            for index, allocation in enumerate(allocations, start=1):
                allocation_amount = Decimal(str(allocation.amount or 0))
                if index == len(allocations):
                    proportional_cash = remaining_cash
                else:
                    proportional_cash = (
                        cash_applied_to_bills * allocation_amount / total_allocated
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    remaining_cash -= proportional_cash

                if proportional_cash <= 0:
                    continue

                bill = allocation.bill
                if getattr(bill, 'total_amount_base', None):
                    base_amount = scale_amount_for_journal(proportional_cash, bill)
                else:
                    base_amount = proportional_cash.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                journal_amount += base_amount

            if journal_amount > 0:
                return journal_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    request_obj = getattr(payment, '_current_request', None)
    company = _get_company_for_request(request_obj)
    vendor_currency = resolve_currency_for_vendor(payment.vendor, company) if company else None
    rate = get_effective_rate_to_base(vendor_currency, payment.payment_date) if vendor_currency else Decimal('1')
    return (payment_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _resolve_exchange_gain_loss_account(company_db='default'):
    """
    Locate the account used for realized FX differences on bill settlement.
    Reuse a single gain/loss account when available, similar to Zoho's behavior.
    """
    search_terms = (
        'Exchange Gain/Loss',
        'Exchange Gain',
        'Exchange Loss',
        'Forex Gain/Loss',
        'Foreign Exchange Gain/Loss',
    )

    for term in search_terms:
        acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact=term).first()
        if acct:
            return acct

    for term in search_terms:
        acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains=term).first()
        if acct:
            return acct

    return ChartOfAccounts.objects.using(company_db).filter(code='40205').first()


def _get_bill_payment_journal_components(payment):
    """
    Return payable settlement and cash movement in base currency.

    For allocated bill payments:
    - payable_base clears AP at the original bill posting rate
    - cash_base credits bank at the payment-date rate
    - the delta is the realized FX gain/loss

    For non-allocated payments, keep the existing fallback behavior.
    """
    from currencies.services import get_effective_rate_to_base, resolve_currency_for_vendor, scale_amount_for_journal

    payment_amount = Decimal(str(payment.amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    allocations = list(payment.bill_allocations.select_related('bill__document_currency').all())

    if allocations:
        total_allocated = sum(Decimal(str(allocation.amount or 0)) for allocation in allocations)
        if total_allocated > 0:
            advance_added = sum(
                Decimal(str(txn.amount or 0))
                for txn in payment.advance_transactions.filter(amount__gt=0)
            )
            cash_applied_to_bills = payment_amount - advance_added
            if cash_applied_to_bills <= 0:
                cash_applied_to_bills = min(payment_amount, total_allocated)

            remaining_cash = cash_applied_to_bills
            payable_base = Decimal('0.00')
            cash_base = Decimal('0.00')
            request_obj = getattr(payment, '_current_request', None)
            company = _get_company_for_request(request_obj)
            vendor_currency = resolve_currency_for_vendor(payment.vendor, company) if company else None

            for index, allocation in enumerate(allocations, start=1):
                allocation_amount = Decimal(str(allocation.amount or 0))
                if index == len(allocations):
                    proportional_cash = remaining_cash
                else:
                    proportional_cash = (
                        cash_applied_to_bills * allocation_amount / total_allocated
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    remaining_cash -= proportional_cash

                if proportional_cash <= 0:
                    continue

                bill = allocation.bill
                payable_base += scale_amount_for_journal(proportional_cash, bill)

                bill_currency = getattr(bill, 'document_currency', None) or vendor_currency
                payment_rate_date = allocation.payment_made_on or payment.payment_date
                payment_rate = get_effective_rate_to_base(bill_currency, payment_rate_date) if bill_currency else Decimal('1')
                cash_base += (proportional_cash * payment_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            payable_base = payable_base.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            cash_base = cash_base.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            return {
                'payable_base': payable_base,
                'cash_base': cash_base,
                'fx_difference': (payable_base - cash_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
            }

    request_obj = getattr(payment, '_current_request', None)
    company = _get_company_for_request(request_obj)
    vendor_currency = resolve_currency_for_vendor(payment.vendor, company) if company else None
    rate = get_effective_rate_to_base(vendor_currency, payment.payment_date) if vendor_currency else Decimal('1')
    base_amount = (payment_amount * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {
        'payable_base': base_amount,
        'cash_base': base_amount,
        'fx_difference': Decimal('0.00'),
    }


def _get_purchase_return_reversal_proportion(purchase_return, bill=None, company_db='default'):
    bill = bill or getattr(purchase_return, 'bill', None)
    if not bill:
        return Decimal('1.0000')

    try:
        bill_total_base = Decimal(str(getattr(bill, 'total_amount_base', None) or 0))
        refund_total_base = Decimal(str(getattr(purchase_return, 'refund_amount_base', None) or 0))
        if bill_total_base > 0 and refund_total_base > 0:
            proportion = (refund_total_base / bill_total_base).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            return min(max(proportion, Decimal('0.0000')), Decimal('1.0000'))
    except Exception:
        pass

    try:
        bill_total_doc = Decimal(str(getattr(bill, 'total_amount', None) or 0))
        refund_total_doc = Decimal(str(getattr(purchase_return, 'refund_amount', None) or 0))
        if bill_total_doc > 0 and refund_total_doc > 0:
            proportion = (refund_total_doc / bill_total_doc).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
            return min(max(proportion, Decimal('0.0000')), Decimal('1.0000'))
    except Exception:
        pass

    try:
        total_qty = BillItem.objects.using(company_db).filter(bill=bill).aggregate(total=Sum('quantity'))['total'] or 0
        returned_qty = PurchaseReturnItem.objects.using(company_db).filter(
            purchase_return=purchase_return
        ).aggregate(total=Sum('quantity_returned'))['total'] or 0
        if total_qty and returned_qty:
            proportion = (Decimal(str(returned_qty)) / Decimal(str(total_qty))).quantize(
                Decimal('0.0001'),
                rounding=ROUND_HALF_UP,
            )
            return min(max(proportion, Decimal('0.0000')), Decimal('1.0000'))
    except Exception:
        pass

    return Decimal('1.0000')


def _delete_purchase_return_reversal_entries(purchase_return, company_db='default'):
    bill = getattr(purchase_return, 'bill', None)
    if not bill:
        return 0

    entries = JournalEntry.objects.using(company_db).filter(
        Q(reference=f"Reversal-Return-{purchase_return.return_number}-Bill-{bill.bill_number}") |
        Q(narration__icontains=f"Purchase Return {purchase_return.return_number}")
    ).order_by('-id')

    deleted_count = 0
    for entry in entries:
        try:
            entry.lines.all().delete()
            entry.delete()
            deleted_count += 1
        except Exception:
            logger.exception(
                'Failed to delete purchase return reversal journal %s for return %s',
                getattr(entry, 'id', None),
                getattr(purchase_return, 'id', None),
            )
    return deleted_count


def post_bill_payment_journal_entry(payment, request_user):
    """
    Post journal entry for payment to vendor.
    
    When a payment is made to a vendor:
    - Debit: Creditors/Accounts Payable (payment.vendor)
    - Credit: Bank/Cash Account (from paid_through)
    
    This reverses the original bill entry that credited Creditors/AP.
    """
    import sys
    print(f"\n{'='*80}", flush=True, file=sys.stdout)
    print(f"BILL PAYMENT JOURNAL POSTING FUNCTION CALLED", flush=True, file=sys.stdout)
    print(f"Payment Number: {payment.payment_number}", flush=True, file=sys.stdout)
    print(f"Payment Amount: {payment.amount}", flush=True, file=sys.stdout)
    vendor_name = str(payment.vendor) if payment.vendor else "NO VENDOR"
    print(f"Vendor: {vendor_name}", flush=True, file=sys.stdout)
    print(f"Paid Through: {payment.paid_through.name if payment.paid_through else 'NO ACCOUNT'}", flush=True, file=sys.stdout)
    print(f"{'='*80}", flush=True, file=sys.stdout)
    
    try:
        company_db = getattr(getattr(payment, '_current_request', None), 'company_db', 'default')
        print(f"\n{'='*80}")
        print(f"POSTING JOURNAL ENTRY FOR BILL PAYMENT #{payment.payment_number}")
        print(f"{'='*80}")
        
        # Get vendor name based on vendor type
        if payment.vendor.vendor_type == 'company':
            vendor_name = payment.vendor.company_name or "Company"
        else:
            vendor_name = f"{payment.vendor.first_name or ''} {payment.vendor.last_name or ''}".strip() or "Vendor"
        
        # Generate next JV number
        last = JournalEntry.objects.using(company_db).order_by('-id').first()
        if last and last.entry_number and last.entry_number.startswith('JV-'):
            try:
                last_num = int(last.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        next_num = last_num + 1
        while JournalEntry.objects.using(company_db).filter(entry_number=f"JV-{str(next_num).zfill(5)}").exists():
            next_num += 1
        entry_number = f"JV-{str(next_num).zfill(5)}"
        
        # Create journal entry header
        journal = JournalEntry.objects.using(company_db).create(
            entry_number=entry_number,
            date=payment.payment_date,
            reference=f"Payment #{payment.payment_number}",
            narration=f"Payment to vendor {vendor_name} - Payment #{payment.payment_number}",
            created_by=request_user,
            updated_by=request_user,
            status='posted'
        )
        
        journal_components = _get_bill_payment_journal_components(payment)
        payable_base = journal_components['payable_base']
        cash_base = journal_components['cash_base']
        fx_difference = journal_components['fx_difference']

        print(f"Created Journal Entry: {entry_number}")
        print(f"Vendor: {vendor_name}")
        print(f"Document Amount: {payment.amount}")
        print(f"Payable/Base Amount: {payable_base}")
        print(f"Cash/Base Amount: {cash_base}")
        print(f"FX Difference: {fx_difference}")
        print(f"Paid From: {payment.paid_through.name}")
        
        # DEBIT: Creditors/Accounts Payable
        creditor_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Creditors').first()
        if not creditor_acct:
            creditor_acct = ChartOfAccounts.objects.using(company_db).filter(code='2020101').first()
        
        # If still not found, log error and raise exception
        if not creditor_acct:
            error_msg = "Creditors/Accounts Payable account not found in Chart of Accounts"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        
        debit_amount = payable_base
        
        JournalLine.objects.using(company_db).create(
            journal=journal,
            account=creditor_acct,
            description=f"Payment #{payment.payment_number} - {vendor_name}",
            debit=debit_amount,
            credit=Decimal('0.00'),
            sequence=10
        )
        
        print(f"\n✓ DEBIT: {creditor_acct.name} - ₹{debit_amount:.2f}")
        
        # CREDIT: Bank/Cash/Payment Account (paid_through)
        credit_acct = payment.paid_through
        
        # If paid_through is not set, raise error
        if not credit_acct:
            error_msg = "Payment account (paid_through) not found"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        
        credit_amount = cash_base
        
        JournalLine.objects.using(company_db).create(
            journal=journal,
            account=credit_acct,
            description=f"Payment #{payment.payment_number} - {vendor_name}",
            debit=Decimal('0.00'),
            credit=credit_amount,
            sequence=20
        )
        
        print(f"✓ CREDIT: {credit_acct.name} - ₹{credit_amount:.2f}")

        if fx_difference != Decimal('0.00'):
            exchange_acct = _resolve_exchange_gain_loss_account(company_db)
            if exchange_acct:
                if fx_difference > 0:
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=exchange_acct,
                        description=f"Payment #{payment.payment_number} - Exchange Gain",
                        debit=Decimal('0.00'),
                        credit=fx_difference,
                        sequence=30
                    )
                    print(f"✓ FX GAIN: {exchange_acct.name} - ₹{fx_difference:.2f}")
                else:
                    fx_loss = abs(fx_difference)
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=exchange_acct,
                        description=f"Payment #{payment.payment_number} - Exchange Loss",
                        debit=fx_loss,
                        credit=Decimal('0.00'),
                        sequence=30
                    )
                    print(f"✓ FX LOSS: {exchange_acct.name} - ₹{fx_loss:.2f}")
            else:
                logger.warning(
                    'Exchange Gain/Loss account not found for payment %s; FX difference %s will fall through to rounding.',
                    payment.payment_number,
                    fx_difference,
                )
        
        # Calculate and update totals
        journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
        journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        
        # Check for imbalance and handle rounding
        diff = (Decimal(journal.total_debit) - Decimal(journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        print(f"\nTotal Debit: ₹{journal.total_debit:.2f}")
        print(f"Total Credit: ₹{journal.total_credit:.2f}")
        print(f"Difference: ₹{abs(diff):.2f}")
        
        if diff != Decimal('0.00'):
            round_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Rounded Off').first()
            if not round_acct:
                round_acct = ChartOfAccounts.objects.using(company_db).filter(code='40217').first()
            
            if round_acct:
                if diff > 0:
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=round_acct,
                        description=f"Payment #{payment.payment_number} - Rounded Off",
                        debit=Decimal('0.00'),
                        credit=diff,
                        sequence=30
                    )
                    print(f"✓ ROUNDING: {round_acct.name} (Credit) - ₹{diff:.2f}")
                else:
                    abs_diff = abs(diff)
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=round_acct,
                        description=f"Payment #{payment.payment_number} - Rounded Off",
                        debit=abs_diff,
                        credit=Decimal('0.00'),
                        sequence=30
                    )
                    print(f"✓ ROUNDING: {round_acct.name} (Debit) - ₹{abs_diff:.2f}")
                
                # Recalculate totals
                journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
                journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        
        journal.save()
        
        print(f"\n✅ JOURNAL ENTRY POSTED SUCCESSFULLY")
        print(f"Entry Number: {entry_number}")
        print(f"Final Total Debit: ₹{journal.total_debit:.2f}")
        print(f"Final Total Credit: ₹{journal.total_credit:.2f}")
        print(f"{'='*80}\n")
        
        return journal
        
    except Exception as e:
        import sys
        print(f"\n{'#'*80}", flush=True, file=sys.stderr)
        print(f"❌ ERROR POSTING BILL PAYMENT JOURNAL ENTRY", flush=True, file=sys.stderr)
        print(f"Payment Number: {payment.payment_number}", flush=True, file=sys.stderr)
        print(f"Error: {str(e)}", flush=True, file=sys.stderr)
        print(f"Error Type: {type(e).__name__}", flush=True, file=sys.stderr)
        print(f"{'#'*80}", flush=True, file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        logger.exception(f"Failed to create journal entry for bill payment #{payment.payment_number}")
        # Print all debug info
        print(f"DEBUG - Payment Object: {payment}", flush=True, file=sys.stderr)
        print(f"DEBUG - Payment Amount: {payment.amount}", flush=True, file=sys.stderr)
        print(f"DEBUG - Paid Through: {payment.paid_through}", flush=True, file=sys.stderr)
        print(f"DEBUG - Vendor: {payment.vendor}", flush=True, file=sys.stderr)
        # Don't raise - allow payment to be saved even if journaling fails
        return None
        #by adarsh

def send_payment_made_email(payment):
    """
    Send a payment made notification email to the vendor.
    Returns (success: bool, message: str).
    """
    try:
        import re, traceback
        vendor = payment.vendor
        recipient = (getattr(vendor, 'email', '') or '').strip()
        if not recipient:
            first_alloc = payment.bill_allocations.select_related('bill').first()
            if first_alloc and first_alloc.bill:
                recipient = (getattr(first_alloc.bill, 'shipping_email', '') or '').strip()
        if not recipient:
            return False, "Vendor email not found."

        # Build placeholder context
        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        vendor_name = (
            getattr(vendor, 'company_name', '') or
            f"{getattr(vendor, 'first_name', '')} {getattr(vendor, 'last_name', '')}".strip() or
            "Vendor"
        )

        placeholder_ctx = {
            'vendor': vendor_name,
            'payment_number': getattr(payment, 'payment_number', ''),
            'date': getattr(payment, 'payment_date', '') if getattr(payment, 'payment_date', None) else '',
            'amount': str(getattr(payment, 'amount', '')),
            'currency_symbol': _resolve_purchase_currency_symbol(payment=payment),
            'currency_code': (getattr(getattr(payment, 'vendor', None), 'currency', '') or '').strip().upper(),
            'company': company_name,
            'logo': company_logo,
        }

        # Try to use EmailTemplateStyle if available
        try:
            from email_templates.models import EmailTemplateStyle
            template_style = None
            try_names = ["Payment Made", "Bill", "Order", "Quotation"]
            for tn in try_names:
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    is_default=True,
                    status=True
                ).first()
                if template_style:
                    break
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    status=True
                ).first()
                if template_style:
                    break

            if template_style:
                subject = replace_placeholders(template_style.subject or "Payment Made", placeholder_ctx)
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)
                # remove any leftover payment number placeholders
                try:
                    subject = re.sub(r"\[\[payment_number\]\]", '', subject)
                    message_html = re.sub(r"\[\[payment_number\]\]", '', message_html)
                except Exception:
                    pass
                try:
                    message_html = re.sub(r'style="[^"]*(?:background(?:-color)?|color)[^"]*"', '', message_html, flags=re.I)
                    message_html = re.sub(r'<font[^>]*>', '', message_html, flags=re.I)
                    message_html = re.sub(r'</font>', '', message_html, flags=re.I)
                except Exception:
                    pass

                email_config = EmailConfiguration.objects.filter(
                    usage_types__icontains=template_style.template_name,
                    status=True
                ).first()
                if not email_config:
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                if not email_config:
                    email_config = EmailConfiguration.objects.filter(status=True).first()
                if not email_config:
                    return False, "No active email configuration found."

                connection = get_connection(
                    host=email_config.host,
                    port=email_config.port,
                    username=email_config.host_user,
                    password=email_config.host_password,
                    use_tls=email_config.use_tls,
                    fail_silently=False
                )
                from_email = email_config.default_from_email or email_config.host_user
                msg = EmailMultiAlternatives(
                    subject,
                    '',
                    from_email,
                    [recipient],
                    connection=connection
                )
                try:
                    msg.attach_alternative(message_html, 'text/html')
                except Exception:
                    pass
                # Attach only the payment receipt PDF (do not attach individual bill PDFs)
                try:
                    try:
                        receipt_bytes = generate_bill_payment_receipt_pdf_bytes(payment.id)
                        if receipt_bytes:
                            msg.attach(f'payment_{getattr(payment, "payment_number", payment.id)}.pdf', receipt_bytes, 'application/pdf')
                            logger.info('Attached payment receipt PDF for payment %s (%d bytes)', getattr(payment, 'payment_number', payment.id), len(receipt_bytes))
                    except Exception:
                        logger.exception('Failed to generate payment receipt PDF for payment %s', getattr(payment, 'pk', None))
                except Exception:
                    pass
                msg.send(fail_silently=False)
                return True, f"Email sent to {recipient} successfully."

        except Exception:
            # If email_templates isn't available or fails, fall through to simple send below
            pass

        # Try hardcoded default templates from email_templates if DB template not found
        try:
            from email_templates.views import get_default_email_template
            default_template = get_default_email_template("Payment Made")
            if default_template:
                subject = replace_placeholders(default_template.get("subject", "Payment Made"), placeholder_ctx)
                message_html = replace_placeholders(default_template.get("body", ""), placeholder_ctx)
                try:
                    subject = re.sub(r"\[\[payment_number\]\]", '', subject)
                    message_html = re.sub(r"\[\[payment_number\]\]", '', message_html)
                except Exception:
                    pass
                attach_pdf_flag = default_template.get("attach_pdf", False)
                try:
                    m = re.search(r"\[\[attach_pdf:(true|false)\]\]", message_html, flags=re.I)
                    if m:
                        attach_pdf_flag = m.group(1).lower() == 'true'
                        message_html = re.sub(r"\[\[attach_pdf:(?:true|false)\]\]", '', message_html, flags=re.I)
                    message_html = re.sub(r'<div\s+class\s*=\s*"attach-option"[\s\S]*?<\/div>', '', message_html, flags=re.I)
                except Exception:
                    pass

                # Determine email configuration
                email_config = EmailConfiguration.objects.filter(usage_types__icontains="Payment Made", status=True).first()
                if not email_config:
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                if not email_config:
                    email_config = EmailConfiguration.objects.filter(status=True).first()
                if not email_config:
                    return False, "No active email configuration found."

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    from_email = email_config.default_from_email or email_config.host_user
                    msg = EmailMultiAlternatives(
                        subject,
                        '',
                        from_email,
                        [recipient],
                        connection=connection
                    )
                    try:
                        msg.attach_alternative(message_html, 'text/html')
                    except Exception:
                        pass
                    # Attach only the payment receipt PDF (do not attach individual bill PDFs)
                    try:
                        try:
                            receipt_bytes = generate_bill_payment_receipt_pdf_bytes(payment.id)
                            if receipt_bytes:
                                fname = f'payment_{getattr(payment, "payment_number", payment.id)}.pdf'
                                msg.attach(fname, receipt_bytes, 'application/pdf')
                                logger.info('Attached payment receipt PDF %s (%d bytes)', fname, len(receipt_bytes))
                        except Exception:
                            logger.exception('Failed to generate payment receipt PDF for payment %s', getattr(payment, 'pk', None))
                    except Exception:
                        logger.exception('Failed while attaching payment receipt PDF for payment email %s', getattr(payment, 'pk', None))
                    msg.send(fail_silently=False)
                    return True, f"Email sent to {recipient} successfully."
                except Exception as e:
                    traceback.print_exc()
                    return False, f"Email failed: {str(e)}"
        except Exception:
            pass

        # Fallback: simple built-in message if no template applied
        subject = "Payment Made"
        symbol = _resolve_purchase_currency_symbol(payment=payment)
        amt_disp = f"{symbol}{payment.amount:.2f}" if symbol else str(payment.amount)
        plain_body = (
            f"Dear {vendor_name},\n\n"
            f"We have recorded your payment.\n"
            f"Payment Date: {payment.payment_date}\n"
            f"Amount: {amt_disp}\n\n"
            f"Thank you."
        )
        html_body = (
            f"<p>Dear {vendor_name},</p>"
            f"<p>We have recorded your payment.</p>"
            f"<p><strong>Payment Date:</strong> {payment.payment_date}<br>"
            f"<strong>Amount:</strong> {amt_disp}</p>"
            "<p>Thank you.</p>"
        )

        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
        if not email_config:
            email_config = EmailConfiguration.objects.filter(status=True).first()
        if not email_config:
            return False, "No active email configuration found."

        connection = get_connection(
            host=email_config.host,
            port=email_config.port,
            username=email_config.host_user,
            password=email_config.host_password,
            use_tls=email_config.use_tls,
            fail_silently=False
        )
        from_email = email_config.default_from_email or email_config.host_user
        msg = EmailMultiAlternatives(
            subject,
            plain_body,
            from_email,
            [recipient],
            connection=connection
        )
        msg.attach_alternative(html_body, 'text/html')
        # Attach only the payment receipt PDF (do not attach individual bill PDFs)
        try:
            try:
                receipt_bytes = generate_bill_payment_receipt_pdf_bytes(payment.id)
                if receipt_bytes:
                    msg.attach(f'payment_{getattr(payment, "payment_number", payment.id)}.pdf', receipt_bytes, 'application/pdf')
                    logger.info('Attached payment receipt PDF for payment %s (%d bytes)', getattr(payment, 'payment_number', payment.id), len(receipt_bytes))
            except Exception:
                logger.exception('Failed to generate payment receipt PDF for payment %s', getattr(payment, 'pk', None))
        except Exception:
            pass
        msg.send(fail_silently=False)
        return True, f"Email sent to {recipient} successfully."
    except Exception as e:
        logger.exception("Failed to send payment made email for payment #%s", getattr(payment, 'payment_number', ''))
        return False, f"Email failed: {str(e)}"

def generate_bill_payment_receipt_pdf_bytes(payment_id):
    """Generate a styled bill payment receipt PDF for a BillPayment, matching PO PDF style."""
    import os
    import io
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    payment = get_object_or_404(BillPayment, pk=payment_id)

    # ── Font registration ──────────────────────────────────────────────────────
    try:
        from django.conf import settings
        static_font_path = os.path.join(settings.BASE_DIR, 'static', 'fonts', 'DejaVuSans.ttf')
        font_registered = False
        if os.path.exists(static_font_path):
            pdfmetrics.registerFont(TTFont('DejaVuSans', static_font_path))
            font_registered = True
        else:
            for fp in [
                'C:/Windows/Fonts/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/System/Library/Fonts/Supplemental/DejaVuSans.ttf',
            ]:
                if os.path.exists(fp):
                    pdfmetrics.registerFont(TTFont('DejaVuSans', fp))
                    font_registered = True
                    break
    except Exception:
        font_registered = False

    font_name = 'DejaVuSans' if font_registered else 'Helvetica'
    # Resolve currency for bill PDF: prefer document currency symbol, then company base
    currency = None
    try:
        currency = (context.get('document_currency_symbol') or context.get('document_currency_code') or
                    context.get('company_base_currency_symbol') or _resolve_purchase_currency_symbol(bill=bill))
    except Exception:
        currency = None
    if not currency:
        currency = '₹' if font_registered else 'Rs.'
    CODE_TO_SYMBOL = {'USD': '$', 'EUR': '€', 'GBP': '£', 'AUD': '$', 'CAD': '$', 'JPY': '¥', 'INR': '₹'}
    if isinstance(currency, str) and len(currency) == 3 and currency.isalpha():
        currency = CODE_TO_SYMBOL.get(currency.upper(), currency)
    # Resolve currency symbol for this payment (prefer payment -> bill -> vendor)
    try:
        currency = _resolve_purchase_currency_symbol(payment=payment)
    except Exception:
        currency = '₹' if font_registered else 'Rs.'
    # Map common ISO codes to glyphs if needed
    CODE_TO_SYMBOL = {'USD': '$', 'EUR': '€', 'GBP': '£', 'AUD': '$', 'CAD': '$', 'JPY': '¥', 'INR': '₹'}
    if len(currency) == 3 and currency.isalpha():
        currency = CODE_TO_SYMBOL.get(currency.upper(), currency)

    # ── Document setup ─────────────────────────────────────────────────────────
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=10*mm, leftMargin=10*mm,
        topMargin=10*mm,   bottomMargin=10*mm,
        title=f'Bill Payment Receipt {payment.payment_number}',
        author='LyraERP',
    )

    styles   = getSampleStyleSheet()
    elements = []

    # ── Shared paragraph styles ────────────────────────────────────────────────
    def _ps(name, base='Normal', **kw):
        kw.setdefault('fontName', font_name)
        return ParagraphStyle(name, parent=styles[base], **kw)

    title_style          = _ps('Title',          base='Heading1',
                                fontSize=18, textColor=colors.HexColor('#1a1a1a'),
                                spaceAfter=0, fontName='Helvetica-Bold', alignment=TA_LEFT)
    badge_style          = _ps('Badge',
                                fontSize=16, textColor=colors.HexColor('#2c3e50'),
                                fontName='Helvetica-Bold', alignment=TA_RIGHT)
    company_header_style = _ps('CompanyHeader',
                                fontSize=8, textColor=colors.HexColor('#555555'), leading=10)
    meta_label_style     = _ps('MetaLabel',
                                fontSize=7, textColor=colors.HexColor('#7f8c8d'),
                                fontName='Helvetica-Bold')
    meta_value_style     = _ps('MetaValue',
                                fontSize=7, textColor=colors.HexColor('#2c3e50'))
    label_style          = _ps('Label',
                                fontSize=7, textColor=colors.HexColor('#ffffff'),
                                fontName='Helvetica-Bold')
    value_style          = _ps('Value',
                                fontSize=7, textColor=colors.HexColor('#2c3e50'))
    amount_style         = _ps('Amount',
                                fontSize=7, textColor=colors.HexColor('#2c3e50'),
                                alignment=TA_RIGHT)
    totals_label_style   = _ps('TotalLabel',
                                fontSize=7, textColor=colors.HexColor('#2c3e50'),
                                fontName='Helvetica', alignment=TA_RIGHT)
    grand_total_style    = _ps('GrandTotal',
                                fontSize=8, textColor=colors.HexColor('#2c3e50'),
                                alignment=TA_RIGHT)

    # ── Company ────────────────────────────────────────────────────────────────
    company      = Company.objects.filter(status=True).first() or Company.objects.first()
    legal_name   = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name = safe(legal_name or (getattr(company, 'name', '') or '').strip() if company else '')

    # ── HEADER ─────────────────────────────────────────────────────────────────
    header_left = Paragraph(f'<b>{company_name}</b>', title_style)
    show_logo   = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
    try:
        if show_logo and company and getattr(company, 'logo', None) and os.path.exists(company.logo.path):
            header_left = Image(company.logo.path, width=25*mm, height=8*mm)
    except Exception:
        pass

    header_table = Table(
        [[header_left, Paragraph('Bill Payment Receipt', badge_style)]],
        colWidths=[310, 210],
    )
    header_table.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',         (1, 0), (1,  -1), 'RIGHT'),
        ('BORDER',        (0, 0), (-1, -1), 1,   colors.HexColor('#e0e0e0')),
        ('LINEWIDTH',     (0, 0), (-1, -1), 1.5),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))

    # ── Company info block ─────────────────────────────────────────────────────
    if company:
        company_info = (
            f"<font color='#2c3e50'><b>{company_name}</b></font><br/>"
            f"<font size='6' color='#555555'>"
            f"{safe(company.address_line1)}<br/>"
            f"{safe(company.address_line2)}<br/>"
            f"{safe(company.city)}, {safe(company.state)}, {safe(company.country)}, {safe(company.postal_code)}<br/>"
            f"GSTIN: {safe(company.tax_id)}<br/>"
            f"{safe(company.email)}"
            f"</font>"
        )
        elements.append(Paragraph(company_info, company_header_style))
        elements.append(Spacer(1, 6*mm))

    # ── Meta info (4-column grid) ──────────────────────────────────────────────
    vendor_name = (
        payment.vendor.company_name
        if getattr(payment.vendor, 'vendor_type', '') == 'company'
        else f"{payment.vendor.first_name or ''} {payment.vendor.last_name or ''}".strip()
    )

    meta_data = [
        [
            Paragraph('<b>Receipt No:</b>',       meta_label_style),
            Paragraph(str(payment.payment_number), meta_value_style),
            Paragraph('<b>Payment Date:</b>',      meta_label_style),
            Paragraph(str(payment.payment_date),   meta_value_style),
        ],
        [
            Paragraph('<b>Vendor:</b>',            meta_label_style),
            Paragraph(vendor_name or '-',          meta_value_style),
            Paragraph('<b>Paid Through:</b>',      meta_label_style),
            Paragraph(getattr(payment.paid_through, 'name', '-'), meta_value_style),
        ],
        [
            Paragraph('<b>Reference:</b>',         meta_label_style),
            Paragraph(payment.reference or '-',    meta_value_style),
            Paragraph('',                          meta_label_style),
            Paragraph('',                          meta_value_style),
        ],
    ]
    meta_table = Table(meta_data, colWidths=[80, 130, 100, 210])
    meta_table.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, -1), font_name),
        ('FONTSIZE',      (0, 0), (-1, -1), 7),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
        ('GRID',          (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('BACKGROUND',    (0, 0), (0, -1),  colors.HexColor('#f8f9fa')),
        ('BACKGROUND',    (2, 0), (2, -1),  colors.HexColor('#f8f9fa')),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 5*mm))

    # ── Amount highlight box ───────────────────────────────────────────────────
    amt_table = Table(
        [[
            Paragraph('<b>Amount Paid</b>', _ps('AmtLabel',
                fontSize=9, fontName='Helvetica-Bold',
                textColor=colors.HexColor('#ffffff'),
            )),
            Paragraph(
                f'<b>{currency}\u00A0{payment.amount:.2f}</b>',
                _ps('AmtVal',
                    fontSize=11, textColor=colors.HexColor('#ffffff'),
                    alignment=TA_RIGHT,
                ),
            ),
        ]],
        colWidths=[371, 150],
    )
    amt_table.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor('#2c3e50')),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
    ]))
    elements.append(amt_table)
    elements.append(Spacer(1, 5*mm))

    # ── Allocations table ──────────────────────────────────────────────────────
    allocations = payment.bill_allocations.select_related('bill').all()
    if allocations.exists():
        alloc_rows = [[
            Paragraph('<b>#</b>',               label_style),
            Paragraph('<b>Bill No.</b>',         label_style),
            Paragraph('<b>Bill Date</b>',        label_style),
            Paragraph('<b>Amount Allocated</b>', label_style),
        ]]
        for idx, a in enumerate(allocations, 1):
            bill      = a.bill
            bill_no   = getattr(bill, 'bill_number', str(bill.pk))
            bill_date = getattr(bill, 'date', '')
            alloc_rows.append([
                Paragraph(str(idx),        value_style),
                Paragraph(f'#{bill_no}',   value_style),
                Paragraph(str(bill_date),  value_style),
                Paragraph(f'{currency}\u00A0{a.amount:.2f}', amount_style),
            ])

        alloc_table = Table(alloc_rows, colWidths=[25, 185, 150, 160])
        alloc_table.setStyle(TableStyle([
            ('FONTNAME',       (0, 0), (-1, -1), font_name),
            ('FONTSIZE',       (0, 0), (-1, -1), 7),
            ('BACKGROUND',     (0, 0), (-1,  0), colors.HexColor('#2c3e50')),
            ('TEXTCOLOR',      (0, 0), (-1,  0), colors.HexColor('#ffffff')),
            ('ALIGN',          (0, 0), (0,  -1), 'CENTER'),
            ('ALIGN',          (3, 1), (3,  -1), 'RIGHT'),
            ('VALIGN',         (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',     (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',  (0, 0), (-1, -1), 5),
            ('LEFTPADDING',    (0, 0), (-1, -1), 4),
            ('RIGHTPADDING',   (0, 0), (-1, -1), 4),
            ('GRID',           (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8f9fa')]),
        ]))
        elements.append(alloc_table)
        elements.append(Spacer(1, 5*mm))

        # ── Totals block ───────────────────────────────────────────────────────
        total_allocated = sum(a.amount for a in allocations)
        unallocated     = payment.amount - total_allocated

        totals_data = [
            [
                Paragraph('Total Allocated', totals_label_style),
                Paragraph(f'{currency}\u00A0{float(total_allocated):.2f}', amount_style),
            ],
        ]
        if abs(unallocated) > 0.005:
            totals_data.append([
                Paragraph('Unallocated', totals_label_style),
                Paragraph(f'{currency}\u00A0{float(unallocated):.2f}', amount_style),
            ])
        totals_data.append([
            Paragraph('<b>Amount Paid</b>', _ps('GrandTotalLabel',
                fontSize=8, fontName='Helvetica-Bold',
                textColor=colors.HexColor('#2c3e50'), alignment=TA_RIGHT,
            )),
            Paragraph(f'<b>{currency}\u00A0{payment.amount:.2f}</b>', grand_total_style),
        ])

        totals_table = Table(totals_data, colWidths=[371, 150])
        totals_table.setStyle(TableStyle([
            ('FONTNAME',      (0, 0), (-1, -1), font_name),
            ('ALIGN',         (0, 0), (-1, -1), 'RIGHT'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING',   (0, 0), (-1, -1), 5),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
            ('BACKGROUND',    (0,  0), (-1, -2), colors.HexColor('#ffffff')),
            ('BACKGROUND',    (0, -1), (-1, -1), colors.HexColor('#f8f9fa')),
            ('GRID',          (0,  0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ]))
        elements.append(totals_table)
        elements.append(Spacer(1, 5*mm))

    # ── Notes & signature ──────────────────────────────────────────────────────
    notes_text = safe((payment.notes or '').strip() or '-').replace('\n', '<br/>')
    terms_text = safe((getattr(company, 'terms_and_conditions', None) or '').strip() or '-').replace('\n', '<br/>')

    bottom_table = Table(
        [[
            Paragraph(
                f'<b>Notes</b><br/><font size=6>{notes_text}</font><br/><br/>'
                f'<b>Terms &amp; Conditions</b><br/><font size=6>{terms_text}</font>',
                value_style,
            ),
            Paragraph(
                f'<b>Authorized Signatory</b><br/><br/>For {company_name}',
                value_style,
            ),
        ]],
        colWidths=[330, 190],
    )
    bottom_table.setStyle(TableStyle([
        ('FONTNAME',      (0, 0), (-1, -1), font_name),
        ('FONTSIZE',      (0, 0), (-1, -1), 7),
        ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 4),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 4),
        ('GRID',          (0, 0), (-1, -1), 1, colors.grey),
    ]))
    elements.append(bottom_table)

    # ── Footer ─────────────────────────────────────────────────────────────────
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, 'POWERED BY LyraERP')
        canvas.drawRightString(570, 20, f'Page {canvas.getPageNumber()}')
        canvas.restoreState()

    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)

    pdf = buffer.getvalue()
    buffer.close()
    return pdf


def download_bill_payment_receipt(request, payment_id):
    """View to download payment receipt PDF for a bill payment."""
    pdf_bytes = generate_bill_payment_receipt_pdf_bytes(payment_id)
    payment = get_object_or_404(BillPayment, pk=payment_id)
    filename = f"payment_{payment.payment_number}.pdf"
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def bill_payment_print(request, payment_id):
    """Render a printable HTML page for a bill payment."""
    payment = get_object_or_404(BillPayment, pk=payment_id)

    allocations = BillPaymentAllocation.objects.filter(
        payment=payment
    ).select_related('bill').order_by('bill__bill_number')

    total_allocated = allocations.aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')

    advance_used = Decimal('0.00')
    advance_added = Decimal('0.00')
    # Attempt to get advance transactions if model exists
    try:
        from .models import VendorAdvancePayment
        adv_txns = VendorAdvancePayment.objects.filter(payment=payment).order_by('created_at')
        for txn in adv_txns:
            if txn.amount < 0:
                advance_used += abs(txn.amount)
            else:
                advance_added += txn.amount
    except Exception:
        adv_txns = []

    attachments = payment.attachments.all()

    allocated_bills = []
    for allocation in allocations:
        bill = allocation.bill
        allocated_bills.append({
            'bill': bill,
            'allocated_amount': allocation.amount,
            'payment_made_on': allocation.payment_made_on,
        })

    context = {
        'payment': payment,
        'allocated_bills': allocated_bills,
        'total_allocated': total_allocated,
        'advance_used': advance_used,
        'advance_added': advance_added,
        'attachments': attachments,
        'has_allocations': allocations.exists(),
        'company': Company.objects.first(),
        'show_logo_in_print': True,
    }

    # Provide resolved currency symbol for templates
    try:
        context['payment_currency_symbol'] = _resolve_purchase_currency_symbol(payment=payment)
    except Exception:
        context['payment_currency_symbol'] = payment.get_currency_symbol() if hasattr(payment, 'get_currency_symbol') else '₹'

    return render(request, 'Purchase/payment_print.html', context)



def save_payment(request, pk=None):
    print("reached in payment save")
    """
    Handle payment submission with automatic advance management.
    
    Scenarios:
    1. No bills selected → Pure advance payment
    2. No advance + Bills selected → Direct bill payment (full or partial)
    3. Advance available + Bills selected → Use advance first, then cash
    4. Advance insufficient → Use full advance + cash (full or partial payment)
    """
    try:
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        def _json_response(success, message, redirect_url=None, status=200, **extra):
            payload = {'success': success, 'message': message}
            if redirect_url:
                payload['redirect_url'] = redirect_url
            payload.update(extra)
            return JsonResponse(payload, status=status)

        # ✅ CHECK PERIOD LOCK BEFORE CREATING PAYMENT
        from datetime import datetime
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        payment_date_str = request.POST.get('payment_date')
        if payment_date_str:
            try:
                payment_date = datetime.strptime(payment_date_str, '%Y-%m-%d').date() if isinstance(payment_date_str, str) else payment_date_str
            except:
                payment_date = None
            
            if payment_date:
                try:
                    PeriodLockEnforcer.check_can_edit(payment_date, request.user, db=db, transaction_type='payment')
                except PermissionDenied as e:
                    if is_ajax:
                        return _json_response(False, str(e), status=403)
                    # Build full context to render form with error modal
                    # Handle both: generic payment (pk=None) and bill-specific payment (pk=bill_id)
                    
                    payment_account_headers = ChartOfAccounts.objects.filter(
                        active=True,
                        is_header=True,
                        id__in=[17, 89, 66]
                    )
                    payment_accounts = ChartOfAccounts.objects.filter(
                        active=True,
                        status=True
                        ).filter(
                            Q(id__in=payment_account_headers.values('id')) |
                            Q(parent__in=payment_account_headers)
                    ).order_by('name')
                    
                    payment_accounts_grouped = []
                    for header in payment_account_headers.order_by('name'):
                        children = ChartOfAccounts.objects.filter(
                            active=True,
                            status=True,
                            parent=header
                        ).order_by('name')
                        
                        payment_accounts_grouped.append({
                            'header': header,
                            'children': children,
                        })
                    payment_account_header_ids = list(payment_account_headers.values_list('id', flat=True))
                    next_payment_number = BillPayment.generate_payment_number()
                    
                    # Case 1: Generic payment (no specific bill)
                    if pk is None:
                        vendors = Vendor.objects.filter(is_active=True).order_by('vendor_code')
                        context = {
                            'vendors': vendors,
                            'payment_accounts': payment_accounts,
                            'payment_account_header_ids': payment_account_header_ids,
                            'payment_accounts_grouped': payment_accounts_grouped,
                            'next_payment_number': next_payment_number,
                            'is_new_payment': True,
                            'payment_currency_symbol': '₹',
                            'error_message': str(e),
                            'show_error_modal': True,
                        }
                        return render(request, 'Purchase/add_payment.html', context)
                    
                    # Case 2: Bill-specific payment
                    bill = get_object_or_404(Bill, pk=pk)
                    subtotal_calc = Decimal('0.00')
                    total_tax = Decimal('0.00')
                    
                    for item in BillItem.objects.filter(bill=bill):
                        qty = Decimal(item.quantity or 0)
                        price = Decimal(item.price or 0)
                        base = qty * price
                        
                        discount_val = Decimal(item.prd_disvalue or 0)
                        if item.prd_distype == 'percent':
                            discount_amount = (base * discount_val) / Decimal('100')
                        else:
                            discount_amount = discount_val
                        if discount_amount > base:
                            discount_amount = base
                        discounted = max(base - discount_amount, Decimal('0.00'))
                        
                        tax_rate = Decimal(item.prd_tax or 0)
                        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
                        line_total = discounted + tax_amount
                        
                        subtotal_calc += line_total
                        total_tax += tax_amount
                    
                    total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
                        total=Sum('amount')
                    )['total'] or Decimal('0.00')
                    
                    final_total = bill.total_amount - total_paid
                    advance_balance = get_vendor_advance_balance(bill.vendor.id)
                    advance_to_use = min(advance_balance, final_total)
                    net_amount = max(final_total - advance_balance, Decimal('0.00'))
                    remaining_advance = advance_balance - advance_to_use
                    
                    context = {
                        'bill_number': bill.bill_number,
                        'final_total': final_total,
                        'bill': bill,
                        'payment_accounts': payment_accounts,
                        'payment_account_header_ids': payment_account_header_ids,
                        'payment_accounts_grouped': payment_accounts_grouped,
                        'next_payment_number': next_payment_number,
                        'is_new_payment': False,
                        'advance_balance': advance_balance,
                        'has_advance': advance_balance > 0,
                        'advance_to_use': advance_to_use,
                        'net_amount_to_pay': net_amount,
                        'remaining_advance': remaining_advance,
                        'payment_currency_symbol': bill.get_currency_symbol(),
                        'error_message': str(e),
                        'show_error_modal': True,
                    }
                    return render(request, 'Purchase/add_payment.html', context)

        with transaction.atomic():
            print("\n" + "="*80)
            print("FORM DATA RECEIVED:")
            
            # Get form data
            vendor_id = request.POST.get('vendor_id')
            payment_amount = Decimal(request.POST.get('amount', '0'))
            payment_date = request.POST.get('payment_date')
            payment_number = int(request.POST.get('payment_number'))
            paid_through_id = request.POST.get('paid_through')
            reference = request.POST.get('reference', '')
            notes = request.POST.get('notes', '')
            send_email = request.POST.get('send_email') == '1'
            
            print(f"  vendor_id: {vendor_id}")
            print(f"  amount: {payment_amount}")
            print(f"  payment_date: {payment_date}")
            print(f"  payment_number: {payment_number}")
            print(f"  paid_through_id: {paid_through_id}")
            
            # Get bill IDs
            if pk:
                print(f"DEBUG: Single bill payment mode - pk={pk}")
                bill_ids = [int(pk)]
            else:
                print("DEBUG: Multiple bills payment mode")
                bill_ids_str = request.POST.get('bill_ids', '')
                print(f"DEBUG: bill_ids_str from POST: '{bill_ids_str}'")
                
                if bill_ids_str:
                    bill_ids = [int(bid) for bid in bill_ids_str.split(',') if bid.strip()]
                else:
                    bill_ids = []
            
            # Print all bill-related POST data
            for key, value in request.POST.items():
                if key.startswith('bill_'):
                    print(f"  {key}: {value}")
            print("="*80 + "\n")
            
            print(f"DEBUG: Payment Amount={payment_amount}, Bill IDs={bill_ids}")
            
            # Validate required fields
            if vendor_id is None or not payment_date or payment_number is None or paid_through_id is None:
                msg = 'Please fill all required fields.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                return redirect_with_company('add_payment') if not pk else redirect('record_payment', pk=pk)
            
            # Validate payment amount
            if payment_amount < 0:
                msg = 'Payment amount cannot be negative.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                return redirect_with_company('add_payment') if not pk else redirect('record_payment', pk=pk)
            
            # Get objects
            vendor = get_object_or_404(Vendor, id=vendor_id)
            paid_through = get_object_or_404(ChartOfAccounts, id=paid_through_id)
            payment_currency_symbol = _resolve_purchase_currency_symbol(vendor=vendor)
            
            # Get current advance balance
            current_advance = get_vendor_advance_balance(vendor_id)
            print(f"DEBUG: Current Advance Balance = ₹{current_advance}")
            
            # Create payment record
            payment = BillPayment.objects.create(
                vendor=vendor,
                amount=payment_amount,
                payment_date=payment_date,
                payment_number=payment_number,
                paid_through=paid_through,
                reference=reference,
                notes=notes,
                send_email=send_email,
                created_by=request.user if request.user.is_authenticated else None,
            )
            payment._current_request = request
            
            print(f"DEBUG: Payment #{payment_number} created with ID={payment.id}")
            
            # Handle attachments
            attachments = request.FILES.getlist('attachments')
            for attachment in attachments:
                BillPaymentAttachment.objects.create(
                    payment=payment,
                    file=attachment
                )
                print(f"DEBUG: Attachment saved: {attachment.name}")
            
            # SCENARIO 1: No bills selected → Pure advance payment
            # ========================================================================
            if not bill_ids:
                print("\n" + "="*60)
                print("SCENARIO 1: No bills selected - Pure advance payment")
                print("="*60)
                
                if payment_amount <= 0:
                    msg = 'Payment amount must be greater than 0 for advance payments.'
                    if is_ajax:
                        return _json_response(False, msg, status=400)
                    messages.error(request, msg)
                    payment.delete()
                    return redirect_with_company('add_payment')
                
                VendorAdvancePayment.add_advance(vendor, payment_amount, payment)
                print(f"DEBUG: Added advance payment of {payment_currency_symbol}{payment_amount}")

                print("\n" + "="*60)
                print("POSTING JOURNAL ENTRY FOR BILL PAYMENT")
                print("="*60)
                journal_entry = post_bill_payment_journal_entry(payment, request.user)
                if journal_entry:
                    print(f"✅ Journal Entry Posted: {journal_entry.entry_number}")
                else:
                    print("⚠️  Payment saved but journal entry posting encountered an error")
                
                success_msg = f'Advance payment of {_format_purchase_currency(payment_amount, payment_currency_symbol)} recorded successfully! Payment #{payment_number}'
                email_msg = ''
                if send_email:
                    email_ok, email_msg = send_payment_made_email(payment)
                    if not email_ok:
                        raise Exception(email_msg)

                if is_ajax:
                    return _json_response(
                        True,
                        success_msg,
                        redirect_url=get_company_redirect_url(request, 'payment_list'),
                        email_sent=bool(send_email),
                        email_message=email_msg
                    )

                messages.success(request, success_msg)
                return redirect_with_company('payment_list')
            
            # ========================================================================
            # SCENARIO 2, 3, 4: Bills selected - Process with or without advance
            # ========================================================================
            
            # Prepare bills data
            bills_data = []
            total_bills_due = Decimal('0.00')
            
            for bill_id in bill_ids:
                bill = get_object_or_404(Bill, id=bill_id)
                
                # Calculate remaining amount for this bill
                total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
                    total=Sum('amount')
                )['total'] or Decimal('0.00')
                bill_remaining = bill.total_amount - total_paid
                
                print(f"DEBUG: Bill #{bill.bill_number} - Total: ₹{bill.total_amount}, Paid: ₹{total_paid}, Remaining: ₹{bill_remaining}")
                
                # Skip fully paid bills
                if bill_remaining <= 0:
                    print(f"DEBUG: Skipping Bill #{bill.bill_number} - Already fully paid")
                    continue
                
                bills_data.append({
                    'bill': bill,
                    'bill_id': bill_id,
                    'amount_to_pay': bill_remaining,
                    'bill_remaining': bill_remaining
                })
                total_bills_due += bill_remaining
            
            if not bills_data:
                msg = 'No valid bills to process. All selected bills are already paid.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                payment.delete()
                return redirect_with_company('add_payment') if not pk else redirect('record_payment', pk=pk)
            
            print(f"\nDEBUG: Total Bills Due = ₹{total_bills_due} (across {len(bills_data)} bill(s))")
            
            # STEP 1: Calculate advance distribution for each bill
            remaining_advance = current_advance
            total_advance_used = Decimal('0.00')
            
            for bill_data in bills_data:
                if remaining_advance <= 0:
                    bill_data['advance_used'] = Decimal('0.00')
                    bill_data['cash_needed'] = bill_data['amount_to_pay']
                else:
                    # Use advance for this bill (up to bill amount)
                    advance_for_this_bill = min(remaining_advance, bill_data['amount_to_pay'])
                    bill_data['advance_used'] = advance_for_this_bill
                    bill_data['cash_needed'] = bill_data['amount_to_pay'] - advance_for_this_bill
                    
                    remaining_advance -= advance_for_this_bill
                    total_advance_used += advance_for_this_bill
                
                print(f"DEBUG: Bill #{bill_data['bill'].bill_number}")
                print(f"  - Amount to Pay: ₹{bill_data['amount_to_pay']}")
                print(f"  - Advance Used: ₹{bill_data['advance_used']}")
                print(f"  - Cash Needed: ₹{bill_data['cash_needed']}")
            
            # STEP 2: Determine allocation based on payment sufficiency
            total_cash_needed = sum(bd['cash_needed'] for bd in bills_data)
            print(f"\nDEBUG: Total Cash Needed = ₹{total_cash_needed}, Cash Available = ₹{payment_amount}")
            print(f"DEBUG: Total Advance Used = ₹{total_advance_used}")
            
            has_advance = total_advance_used > 0
            
            if payment_amount < total_cash_needed:
                # ================================================================
                # INSUFFICIENT PAYMENT - Partial payment scenario
                # ================================================================
                print("\n" + "="*60)
                print("SCENARIO: INSUFFICIENT PAYMENT - Partial Distribution")
                print(f"Has Advance: {has_advance}")
                print("="*60)
                
                if has_advance:
                    # SCENARIO 3/4: With advance + insufficient cash
                    # Use FULL advance + distribute cash proportionally
                    print("Strategy: Full advance + proportional cash distribution")
                    
                    proportion = payment_amount / total_cash_needed if total_cash_needed > 0 else Decimal('0')
                    print(f"Cash proportion: {proportion:.4f}")
                    
                    for bill_data in bills_data:
                        bill_data['cash_allocated'] = bill_data['cash_needed'] * proportion
                        bill_data['total_allocated'] = bill_data['advance_used'] + bill_data['cash_allocated']
                        
                        print(f"\nBill #{bill_data['bill'].bill_number}:")
                        print(f"  - Advance Used: ₹{bill_data['advance_used']:.2f}")
                        print(f"  - Cash Allocated: ₹{bill_data['cash_allocated']:.2f}")
                        print(f"  - Total Allocated: ₹{bill_data['total_allocated']:.2f}")
                else:
                    # SCENARIO 2: No advance + insufficient cash
                    # Distribute cash proportionally across all bills
                    print("Strategy: No advance - proportional cash distribution across bills")
                    
                    proportion = payment_amount / total_bills_due if total_bills_due > 0 else Decimal('0')
                    print(f"Cash proportion: {proportion:.4f}")
                    
                    for bill_data in bills_data:
                        bill_data['cash_allocated'] = bill_data['amount_to_pay'] * proportion
                        bill_data['total_allocated'] = bill_data['cash_allocated']
                        bill_data['advance_used'] = Decimal('0.00')
                        
                        print(f"\nBill #{bill_data['bill'].bill_number}:")
                        print(f"  - Cash Allocated: ₹{bill_data['cash_allocated']:.2f}")
                        print(f"  - Total Allocated: ₹{bill_data['total_allocated']:.2f}")
                    
                    total_advance_used = Decimal('0.00')
                
                excess_cash = Decimal('0.00')
                
            else:
                # ================================================================
                # SUFFICIENT PAYMENT - Full payment scenario
                # ================================================================
                print("\n" + "="*60)
                print("SCENARIO: SUFFICIENT PAYMENT - Full Bill Payment")
                print(f"Has Advance: {has_advance}")
                print("="*60)
                
                if has_advance:
                    # SCENARIO 3: With advance + sufficient cash
                    print("Strategy: Full advance + full cash for bills + excess as new advance")
                else:
                    # SCENARIO 2: No advance + sufficient cash
                    print("Strategy: Full cash for bills + excess as new advance")
                
                for bill_data in bills_data:
                    bill_data['cash_allocated'] = bill_data['cash_needed']
                    bill_data['total_allocated'] = bill_data['amount_to_pay']
                    
                    print(f"\nBill #{bill_data['bill'].bill_number}:")
                    if has_advance:
                        print(f"  - Advance Used: ₹{bill_data['advance_used']:.2f}")
                    print(f"  - Cash Allocated: ₹{bill_data['cash_allocated']:.2f}")
                    print(f"  - Total Allocated: ₹{bill_data['total_allocated']:.2f}")
                
                excess_cash = payment_amount - total_cash_needed
                print(f"\nExcess cash for new advance: ₹{excess_cash:.2f}")
            
            # STEP 3: Create bill payment allocations
            print("\n" + "="*60)
            print("CREATING BILL PAYMENT ALLOCATIONS")
            print("="*60)
            
            allocations_created = 0
            for bill_data in bills_data:
                bill = bill_data['bill']
                payment_currency_symbol = _resolve_purchase_currency_symbol(vendor=vendor, bill=bill, payment=payment)
                total_allocation = bill_data['total_allocated']
                
                # Get payment made on date
                bill_payment_made_on = request.POST.get(f'bill_payment_made_on_{bill_data["bill_id"]}')
                
                print(f"\nBill #{bill.bill_number}:")
                print(f"  - Allocating: {payment_currency_symbol}{total_allocation:.2f}")
                print(f"  - Payment made on: {bill_payment_made_on}")
                
                # Create allocation
                allocation = BillPaymentAllocation.objects.create(
                    payment=payment,
                    bill=bill,
                    amount=total_allocation,
                    payment_made_on=bill_payment_made_on if bill_payment_made_on else None
                )
                allocations_created += 1
                print(f"  ✓ Allocation created: ID={allocation.id}")
                
                # Update bill status
                new_total_paid = BillPaymentAllocation.objects.filter(bill=bill).aggregate(
                    total=Sum('amount')
                )['total'] or Decimal('0.00')
                
                old_status = bill.status
                if new_total_paid >= bill.total_amount:
                    bill.status = 'Open'
                    bill.payment_status_id = 3
                elif new_total_paid > 0:
                    # bill.status = 'Partial'
                    bill.payment_status_id = 2
                bill.save()
                
                print(f"  ✓ Status: {old_status} → {bill.status} (Paid: {payment_currency_symbol}{new_total_paid:.2f}/{payment_currency_symbol}{bill.total_amount:.2f})")
                # If stock management is configured to happen on payment instead
                # of on delivery, update stock when the bill is fully paid.
                try:
                    if not is_stock_management_on_delivery(request=request) and bill.payment_status_id == 3:
                        update_stock_from_bill(bill, request.user, request)
                except Exception:
                    logger.exception('Failed to update stock on payment for bill %s', getattr(bill, 'bill_number', None))
            
            print(f"\n✓ Created {allocations_created} allocation(s)")
            
            # STEP 4: Record advance transactions
            print("\n" + "="*60)
            print("RECORDING ADVANCE TRANSACTIONS")
            print("="*60)
            
            if total_advance_used > 0:
                # Debit advance (used for bills)
                advance_debit = VendorAdvancePayment.use_advance(vendor, total_advance_used, payment)
                if advance_debit:
                    print(f"✓ Used advance: -₹{total_advance_used:.2f}")
                else:
                    print(f"✗ Failed to use advance (insufficient balance)")
                    messages.error(request, 'Insufficient advance balance.')
                    raise Exception("Failed to use advance")
            
            # STEP 5: Handle excess cash (new advance)
            if excess_cash > 0:
                VendorAdvancePayment.add_advance(vendor, excess_cash, payment)
                print(f"✓ Added new advance: +{payment_currency_symbol}{excess_cash:.2f}")

            print("\n" + "="*60)
            print("POSTING JOURNAL ENTRY FOR BILL PAYMENT")
            print("="*60)
            journal_entry = post_bill_payment_journal_entry(payment, request.user)
            if journal_entry:
                print(f"✅ Journal Entry Posted: {journal_entry.entry_number}")
            else:
                print("⚠️  Payment saved but journal entry posting encountered an error")
            
            # Get final balance
            final_balance = get_vendor_advance_balance(vendor_id)
            
            print("\n" + "="*80)
            print("PAYMENT SUMMARY")
            print("="*80)
            print(f"Initial Advance Balance:  {payment_currency_symbol}{current_advance:.2f}")
            print(f"Advance Used for Bills:   -{payment_currency_symbol}{total_advance_used:.2f}")
            print(f"Cash Payment Made:        {payment_currency_symbol}{payment_amount:.2f}")
            print(f"New Advance Added:        +{payment_currency_symbol}{excess_cash:.2f}")
            print(f"Final Advance Balance:    {payment_currency_symbol}{final_balance:.2f}")
            print(f"Bill Allocations Created: {allocations_created}")
            print("="*80 + "\n")
            
            # Build success message
            msg_parts = [f'Payment #{payment_number} recorded successfully!']
            
            if total_advance_used > 0 and payment_amount > 0:
                msg_parts.append(
                    f'Advance Used: {_format_purchase_currency(total_advance_used, payment_currency_symbol)} + '
                    f'Cash Paid: {_format_purchase_currency(payment_amount, payment_currency_symbol)}'
                )
            elif total_advance_used > 0:
                msg_parts.append(f'Paid using Advance: {_format_purchase_currency(total_advance_used, payment_currency_symbol)}')
            elif payment_amount > 0:
                msg_parts.append(f'Cash Paid: {_format_purchase_currency(payment_amount, payment_currency_symbol)}')
            
            if excess_cash > 0:
                msg_parts.append(f'New Advance: {_format_purchase_currency(excess_cash, payment_currency_symbol)}')
            
            msg_parts.append(f'Current Advance Balance: {_format_purchase_currency(final_balance, payment_currency_symbol)}')
            
            email_msg = ''
            if send_email:
                email_ok, email_msg = send_payment_made_email(payment)
                if not email_ok:
                    raise Exception(email_msg)

            final_msg = ' | '.join(msg_parts)
            if is_ajax:
                redirect_url = (
                    get_company_redirect_url(request, 'bill_detail', pk=pk)
                    if pk else
                    get_company_redirect_url(request, 'payment_list')
                )
                return _json_response(
                    True,
                    final_msg,
                    redirect_url=redirect_url,
                    email_sent=bool(send_email),
                    email_message=email_msg
                )

            messages.success(request, final_msg)
            return redirect_with_company('bill_detail', pk=pk) if pk else redirect_with_company('payment_list')
                
    except Exception as e:
        import traceback
        traceback.print_exc()
        error_msg = f'Error recording payment: {str(e)}'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect_with_company('add_payment') if not pk else redirect_with_company('record_payment', pk=pk)


def get_vendor_advance_balance(vendor_id):
    """
    Get vendor's current advance balance from VendorAdvancePayment table.
    """
    # return VendorAdvancePayment.get_vendor_advance_balance(vendor_id)
    balance = VendorAdvancePayment.get_vendor_advance_balance(vendor_id)
    print(f"[DEBUG] Vendor ID: {vendor_id}, Advance Balance: {balance}")
    return balance


def get_vendor_unpaid_bills(request):
    """
    AJAX endpoint to get unpaid bills for a vendor along with advance balance.
    Now calculates net amounts after advance deduction for each bill.
    """
    vendor_id = request.GET.get('vendor_id')
    
    if not vendor_id:
        return JsonResponse({'error': 'Vendor ID is required'}, status=400)
    
    try:
        vendor = Vendor.objects.get(pk=vendor_id, is_active=1)
        payment_currency_symbol = _resolve_purchase_currency_symbol(vendor=vendor)
        
        # Get unpaid bills
        bills = Bill.objects.filter(
            vendor=vendor,
            status__in=['Open', 'Partial'],
            payment_status_id__in=[1, 2]
        ).select_related('vendor', 'payment_term','payment_status').order_by('-date')
        
        bills_data = []
        total_due = Decimal('0.00')
        
        for bill in bills:
            # Calculate remaining amount
            total_paid = BillPaymentAllocation.objects.filter(
                bill=bill
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            
            remaining = bill.total_amount - total_paid
            print("remaining",remaining)
            if remaining > 0:
                bills_data.append({
                    'id': bill.id,
                    'bill_number': bill.bill_number,
                    'date': bill.date.strftime('%Y-%m-%d'),
                    'date_formatted': bill.date.strftime('%d %b %Y'),
                    'currency_symbol': bill.get_currency_symbol(),
                    'total_amount': str(bill.total_amount),
                    'total_amount_float': float(bill.total_amount),
                    'paid_amount': str(total_paid),
                    'remaining_amount': str(remaining),
                    'remaining_amount_float': float(remaining),
                })
                
                total_due += remaining
        
        # Get vendor's advance balance
        advance_balance = get_vendor_advance_balance(vendor_id)
        
        # Calculate how advance will be distributed across bills
        remaining_advance = advance_balance
        bills_with_advance = []
        total_net_amount = Decimal('0.00')
        
        for bill in bills_data:
            bill_due = Decimal(str(bill['remaining_amount_float']))
            
            if remaining_advance > 0:
                print("has remaining advance")
                # Deduct advance from this bill
                advance_used_for_bill = min(remaining_advance, bill_due)
                print("advance_used_for_bill",advance_used_for_bill)
                net_amount_for_bill = bill_due - advance_used_for_bill
                print("net_amount_for_bill",net_amount_for_bill)
                remaining_advance -= advance_used_for_bill
            else:
                # No advance left
                print("No advance left")
                advance_used_for_bill = Decimal('0.00')
                print("advance_used_for_bill",advance_used_for_bill)
                net_amount_for_bill = bill_due
            
            bills_with_advance.append({
                **bill,
                'advance_used': str(advance_used_for_bill),
                'advance_used_float': float(advance_used_for_bill),
                'net_amount': str(net_amount_for_bill),
                'net_amount_float': float(net_amount_for_bill),
            })
            
            total_net_amount += net_amount_for_bill
        
        # Calculate totals
        total_advance_will_use = advance_balance - remaining_advance
        print("total_advance_will_use",total_advance_will_use)
        print("advance_balance",advance_balance)
        print("remaining_advance",remaining_advance)
        
        return JsonResponse({
            'success': True,
            'bills': bills_with_advance,
            'total_bills': len(bills_with_advance),
            'currency_symbol': payment_currency_symbol,
            'total_due': str(total_due),
            'total_due_float': float(total_due),
            'advance_balance': str(advance_balance),
            'advance_balance_float': float(advance_balance),
            'has_advance': advance_balance > 0,
            'total_advance_will_use': str(total_advance_will_use),
            'total_advance_will_use_float': float(total_advance_will_use),
            'total_net_amount': str(total_net_amount),
            'total_net_amount_float': float(total_net_amount),
            'net_amount_to_pay_float': float(total_net_amount),
            'remaining_advance': str(remaining_advance),
            'remaining_advance_float': float(remaining_advance),
        })
    
    except Vendor.DoesNotExist:
        return JsonResponse({'error': 'Vendor not found or inactive'}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)

def calculate_vendor_excess_amount(vendor_id):
    """
    Calculate the excess amount (advance/credit) available for a vendor.
    This is: Total payments made - Total bills allocated
    
    Args:
        vendor_id: ID of the vendor
        
    Returns:
        Decimal: Excess amount available for the vendor
    """
    try:
        vendor = Vendor.objects.get(pk=vendor_id)
    except Vendor.DoesNotExist:
        return Decimal('0.00')
    
    # Get total payments made to this vendor
    total_payments = BillPayment.objects.filter(
        vendor=vendor
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    
    # Get total amount allocated to bills
    total_allocated = BillPaymentAllocation.objects.filter(
        payment__vendor=vendor
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    
    # Excess amount is the difference
    excess_amount = total_payments - total_allocated
    
    return excess_amount


def get_vendor_info(request):
    """
    API endpoint to get vendor information including excess amount.
    Called via AJAX when vendor is selected.
    
    GET parameters:
        vendor_id: ID of the vendor
        
    Returns:
        JSON response with vendor information and excess amount
    """
    vendor_id = request.GET.get('vendor_id')
    
    if not vendor_id:
        return JsonResponse({
            'error': 'Vendor ID is required'
        }, status=400)
    
    try:
        vendor = Vendor.objects.get(pk=vendor_id, is_active=True)
        
        # Calculate vendor's excess amount
        excess_amount = calculate_vendor_excess_amount(vendor_id)
        
        # Get vendor's display name
        if vendor.vendor_type == 'company':
            vendor_name = vendor.company_name or str(vendor)
        else:
            vendor_name = f"{vendor.first_name} {vendor.last_name or ''}".strip()
        
        # Get count of unpaid bills
        unpaid_bills_count = Bill.objects.filter(
            vendor=vendor,
            status='Open'
        ).count()
        
        return JsonResponse({
            'success': True,
            'vendor_id': vendor.id,
            'vendor_name': vendor_name,
            'vendor_code': vendor.vendor_code,
            'excess_amount': str(excess_amount),
            'excess_amount_float': float(excess_amount),
            'has_excess': excess_amount > 0,
            'unpaid_bills_count': unpaid_bills_count,
        })
    
    except Vendor.DoesNotExist:
        return JsonResponse({
            'error': 'Vendor not found or inactive'
        }, status=404)
    
    except Exception as e:
        return JsonResponse({
            'error': f'An error occurred: {str(e)}'
        }, status=500)

def edit_payment(request, payment_id):
    """
    Edit an existing payment.
    Pre-fills all payment details and allows modification.
    """
    payment = get_object_or_404(BillPayment, pk=payment_id)
    print("payment is:",payment)
    
    if request.method == 'POST':
        return update_payment(request, payment_id)
    
    # Load payment accounts and modes changed by neha on 18-2-26
    payment_account_headers = ChartOfAccounts.objects.filter(
        active=True,
        is_header=True,
        id__in=[17, 89, 66]
    )
    payment_accounts = ChartOfAccounts.objects.filter(
        active=True,
        status=True
        ).filter(
            Q(id__in=payment_account_headers.values('id')) |
            Q(parent__in=payment_account_headers)
    ).order_by('name')

    payment_accounts_grouped = []
    for header in payment_account_headers.order_by('name'):
        children = ChartOfAccounts.objects.filter(
            active=True,
            status=True,
            parent=header
        ).order_by('name')
        
        payment_accounts_grouped.append({
            'header': header,
            'children': children,
        })
    payment_account_header_ids = list(payment_account_headers.values_list('id', flat=True))

    
    # Get all vendors for the dropdown
    vendors = Vendor.objects.filter(is_active=True).order_by('vendor_code')
    
    # Get vendor's current advance balance
    advance_balance = get_vendor_advance_balance(payment.vendor.id)
    # Get allocated bills for this payment - try direct query first
    # allocations = BillPaymentAllocation.objects.filter(
    #     payment=payment
    # ).select_related('bill').all()
    allocations = BillPaymentAllocation.objects.filter(payment=payment)
    # Debug: Print to console
    print(f"Payment ID from URL: {payment_id}")
    print(f"Payment object ID: {payment.id}")
    print(f"Payment number: {payment.payment_number}")
    print(f"Number of allocations found: {allocations.count()}")
    
    
    # Get allocated bills for this payment
    allocated_bills = []
    for allocation in allocations:
        bill = allocation.bill
        
        print(f"Processing bill: {bill.bill_number}, Allocation amount: {allocation.amount}")
        
        
        # Calculate total paid for this bill (excluding this payment to get the remaining before this payment)
        other_payments_total = BillPaymentAllocation.objects.filter(
            bill=bill
        ).exclude(
            payment=payment
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Calculate remaining amount (before this payment)
        remaining_before_payment = bill.total_amount - other_payments_total
        
        allocated_bills.append({
            'id': bill.id,
            'bill_number': bill.bill_number,
            'date': bill.date.strftime('%Y-%m-%d'),
            'date_formatted': bill.date.strftime('%d %b %Y'),
            'total_amount': float(bill.total_amount),
            'remaining_amount': float(remaining_before_payment),
            'allocated_amount': float(allocation.amount),
            'payment_amount': float(allocation.payment.amount),
            'payment_made_on': allocation.payment_made_on.strftime('%Y-%m-%d') if allocation.payment_made_on else '',
        })
    
    print(f"Total allocated bills to send to template: {len(allocated_bills)}")
    print(f"Allocated bills data: {allocated_bills}")
    
    context = {
        'payment': payment,
        'payment_id': payment_id,
        'vendors': vendors,
        'payment_accounts': payment_accounts,
        'payment_account_header_ids': payment_account_header_ids,
        'payment_accounts_grouped': payment_accounts_grouped,
        'is_new_payment': True,  # Use new payment form style
        'is_edit_mode': True,  # Flag to indicate edit mode
        'advance_balance': advance_balance,
        'has_advance': advance_balance > 0,
        'allocated_bills': allocated_bills,
        'payment_currency_symbol': payment.get_currency_symbol(),
    }
    
    return render(request, 'Purchase/add_payment.html', context)
    
    print(f"Total allocated bills to send to template: {len(allocated_bills)}")
    print(f"Allocated bills data: {allocated_bills}")
    
    context = {
        'payment': payment,
        'payment_id': payment_id,
        'vendors': vendors,
        'payment_accounts': payment_accounts,
        'payment_account_header_ids': payment_account_header_ids,
        'payment_accounts_grouped': payment_accounts_grouped,
        'is_new_payment': True,  # Use new payment form style
        'is_edit_mode': True,  # Flag to indicate edit mode
        'advance_balance': advance_balance,
        'has_advance': advance_balance > 0,
        'allocated_bills': allocated_bills,
    }
    
    return render(request, 'Purchase/payment_list.html', context)


@transaction.atomic
def update_payment(request, payment_id):
    """
    Update an existing payment with new details.
    Handles advance balance adjustments and bill allocations.
    """
    payment = get_object_or_404(BillPayment, pk=payment_id)
    
    # ✅ CHECK PERIOD LOCK BEFORE UPDATING PAYMENT
    from datetime import datetime
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    payment_date_str = request.POST.get('payment_date') or (payment.payment_date.strftime('%Y-%m-%d') if payment.payment_date else None)
    if payment_date_str:
        try:
            payment_date = datetime.strptime(str(payment_date_str), '%Y-%m-%d').date() if isinstance(payment_date_str, str) else payment_date_str
        except:
            payment_date = None
        
        if payment_date:
            try:
                PeriodLockEnforcer.check_can_edit(payment_date, request.user, db=db, transaction_type='payment')
            except PermissionDenied as e:
                # Build full context like edit_payment does
                payment_account_headers = ChartOfAccounts.objects.filter(
                    active=True,
                    is_header=True,
                    id__in=[17, 89, 66]
                )
                payment_accounts = ChartOfAccounts.objects.filter(
                    active=True,
                    status=True
                    ).filter(
                        Q(id__in=payment_account_headers.values('id')) |
                        Q(parent__in=payment_account_headers)
                ).order_by('name')

                payment_accounts_grouped = []
                for header in payment_account_headers.order_by('name'):
                    children = ChartOfAccounts.objects.filter(
                        active=True,
                        status=True,
                        parent=header
                    ).order_by('name')
                    
                    payment_accounts_grouped.append({
                        'header': header,
                        'children': children,
                    })
                payment_account_header_ids = list(payment_account_headers.values_list('id', flat=True))
                
                vendors = Vendor.objects.filter(is_active=True).order_by('vendor_code')
                advance_balance = get_vendor_advance_balance(payment.vendor.id)
                
                allocations = BillPaymentAllocation.objects.filter(payment=payment)
                allocated_bills = []
                for allocation in allocations:
                    bill = allocation.bill
                    other_payments_total = BillPaymentAllocation.objects.filter(
                        bill=bill
                    ).exclude(
                        payment=payment
                    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
                    
                    remaining_before_payment = bill.total_amount - other_payments_total
                    
                    allocated_bills.append({
                        'id': bill.id,
                        'bill_number': bill.bill_number,
                        'date': bill.date.strftime('%Y-%m-%d'),
                        'date_formatted': bill.date.strftime('%d %b %Y'),
                        'total_amount': float(bill.total_amount),
                        'remaining_amount': float(remaining_before_payment),
                        'allocated_amount': float(allocation.amount),
                        'payment_amount': float(allocation.payment.amount),
                        'payment_made_on': allocation.payment_made_on.strftime('%Y-%m-%d') if allocation.payment_made_on else '',
                    })
                
                context = {
                    'payment': payment,
                    'payment_id': payment_id,
                    'vendors': vendors,
                    'payment_accounts': payment_accounts,
                    'payment_account_header_ids': payment_account_header_ids,
                    'payment_accounts_grouped': payment_accounts_grouped,
                    'is_new_payment': True,
                    'is_edit_mode': True,
                    'advance_balance': advance_balance,
                    'has_advance': advance_balance > 0,
                    'allocated_bills': allocated_bills,
                    'payment_currency_symbol': payment.get_currency_symbol(),
                    'error_message': str(e),
                    'show_error_modal': True,
                }
                return render(request, 'Purchase/add_payment.html', context)

    
    try:
        # Get form data
        vendor_id = request.POST.get('vendor_id')
        amount = Decimal(request.POST.get('amount', 0))
        payment_date = request.POST.get('payment_date')
        paid_through_id = request.POST.get('paid_through')
        reference = request.POST.get('reference', '').strip()
        notes = request.POST.get('notes', '').strip()
        send_email = request.POST.get('send_email') == '1'
        bill_ids = request.POST.get('bill_ids', '').strip()
        
        # Validate required fields
        if not vendor_id or not payment_date or not paid_through_id:
            messages.error(request, 'Please fill all required fields.')
            return redirect_with_company('edit_payment', payment_id=payment_id)
        
        if amount <= 0:
            messages.error(request, 'Payment amount must be greater than zero.')
            return redirect_with_company('edit_payment', payment_id=payment_id)
        
        # Get vendor and paid through account
        vendor = get_object_or_404(Vendor, pk=vendor_id, is_active=True)
        paid_through = get_object_or_404(ChartOfAccounts, pk=paid_through_id)
        
        # Store old allocated bills before deletion
        old_allocated_bills = list(payment.bill_allocations.values_list('bill_id', flat=True))

        
        # Create reversal journal entries for the existing payment (before modifications)
        try:
            orig_journal = JournalEntry.objects.filter(reference=f"Payment #{payment.payment_number}").order_by('-id').first()
            if orig_journal:
                # generate next JV number
                last = JournalEntry.objects.order_by('-id').first()
                if last and last.entry_number and last.entry_number.startswith('JV-'):
                    try:
                        last_num = int(last.entry_number.split('-')[1])
                    except Exception:
                        last_num = 0
                else:
                    last_num = 0
                entry_number = f"JV-{str(last_num + 1).zfill(5)}"

                rev = JournalEntry.objects.create(
                    entry_number=entry_number,
                    date=payment.payment_date or timezone.now().date(),
                    reference=f"Reversal-Payment-{payment.payment_number}",
                    narration=f"Reversal of {orig_journal.entry_number} for Payment {payment.payment_number} (Edit)",
                    created_by=request.user,
                    updated_by=request.user,
                    status='posted'
                )

                for line in orig_journal.lines.all():
                    try:
                        JournalLine.objects.create(
                            journal=rev,
                            account=line.account,
                            description=f"Reversal: {line.description}",
                            debit=(line.credit or Decimal('0.00')),
                            credit=(line.debit or Decimal('0.00')),
                            sequence=line.sequence
                        )
                    except Exception:
                        logger.exception('Failed to create reversal journal line for payment %s', payment.payment_number)

                try:
                    rev.total_debit = sum(Decimal(str(l.debit or 0)) for l in rev.lines.all())
                    rev.total_credit = sum(Decimal(str(l.credit or 0)) for l in rev.lines.all())
                    rev.save(update_fields=['total_debit', 'total_credit'])
                    logger.info('Created reversal journal %s for payment %s', rev.entry_number, payment.payment_number)
                except Exception:
                    logger.exception('Failed to update totals for reversal journal %s', getattr(rev, 'id', None))
        except Exception:
            logger.exception('Failed to create reversal journal for payment %s before update', getattr(payment, 'payment_number', None))
        
        
        # Reverse old advance transactions related to this payment
        old_advance_transactions = VendorAdvancePayment.objects.filter(payment=payment)
        for txn in old_advance_transactions:
            # Create reverse entry
            VendorAdvancePayment.objects.create(
                vendor=vendor,
                amount=-txn.amount,  # Reverse the transaction
                payment=None  # Not linked to any payment (adjustment)
            )
        # Delete old advance transactions
        old_advance_transactions.delete()
        
        # Delete old bill allocations
        payment.bill_allocations.all().delete()
        
        # Update payment details
        payment.vendor = vendor
        payment.amount = amount
        payment.payment_date = payment_date
        payment.paid_through = paid_through
        payment.reference = reference
        payment.notes = notes
        payment.send_email = send_email
        payment.updated_by = request.user
        payment.save()
        
        # Get new selected bill IDs
        new_selected_bill_ids = []
        if bill_ids:
            new_selected_bill_ids = [int(id.strip()) for id in bill_ids.split(',') if id.strip()]
        
        # Process bill allocations
        if new_selected_bill_ids:
            # Get current advance balance
            current_advance = get_vendor_advance_balance(vendor.id)
            remaining_advance = current_advance
            
            # Get bills and calculate distribution
            bills = Bill.objects.filter(id__in=new_selected_bill_ids, vendor=vendor)
            
            total_bill_amount = Decimal('0.00')
            bill_allocations = []
            
            for bill in bills:
                # Get custom amount from form
                bill_amount_key = f'bill_amount_{bill.id}'
                bill_payment_date_key = f'bill_payment_made_on_{bill.id}'
                
                bill_amount = Decimal(request.POST.get(bill_amount_key, 0))
                bill_payment_date = request.POST.get(bill_payment_date_key)
                
                if bill_amount > 0:
                    # Calculate advance usage for this bill
                    advance_for_bill = min(remaining_advance, bill_amount)
                    remaining_advance -= advance_for_bill
                    
                    bill_allocations.append({
                        'bill': bill,
                        'amount': bill_amount,
                        'payment_made_on': bill_payment_date,
                        'advance_used': advance_for_bill
                    })
                    total_bill_amount += bill_amount
            
            # Validate payment amount covers bills
            if amount < total_bill_amount - current_advance:
                messages.error(
                    request,
                    f'Payment amount ₹{amount} is insufficient. Need at least ₹{total_bill_amount - current_advance} after advance.'
                )
                raise ValueError('Insufficient payment amount')
            
            # Create allocations and use advance
            total_advance_used = Decimal('0.00')
            for allocation_data in bill_allocations:
                BillPaymentAllocation.objects.create(
                    payment=payment,
                    bill=allocation_data['bill'],
                    amount=allocation_data['amount'],
                    payment_made_on=allocation_data['payment_made_on'] or payment_date
                )
                
                # Track advance usage
                if allocation_data['advance_used'] > 0:
                    total_advance_used += allocation_data['advance_used']
            
            # Record advance usage if any
            if total_advance_used > 0:
                VendorAdvancePayment.use_advance(vendor, total_advance_used, payment)
            
            # Calculate excess amount (if payment > bills - advance used)
            allocated_cash = total_bill_amount - total_advance_used
            excess = amount - allocated_cash
            
            if excess > 0:
                VendorAdvancePayment.add_advance(vendor, excess, payment)
            
            # Update bill statuses for newly allocated bills
            for allocation_data in bill_allocations:
                bill = allocation_data['bill']
                update_bill_status(bill)
                try:
                    if not is_stock_management_on_delivery(request=request) and bill.payment_status_id == 3:
                        update_stock_from_bill(bill, request.user, request)
                except Exception:
                    logger.exception(
                        'Failed to update stock on payment update for bill %s',
                        getattr(bill, 'bill_number', None),
                    )
        
        else:
            # Pure advance payment (no bills selected)
            VendorAdvancePayment.add_advance(vendor, amount, payment)
        
        # Update status for bills that were removed from this payment
        removed_bill_ids = set(old_allocated_bills) - set(new_selected_bill_ids)
        if removed_bill_ids:
            removed_bills = Bill.objects.filter(id__in=removed_bill_ids)
            for bill in removed_bills:
                update_bill_status(bill)
        
        # Handle file attachments
        deleted_attachments = request.POST.get('deleted_attachments', '').strip()
        if deleted_attachments:
            deleted_ids = [int(id) for id in deleted_attachments.split(',') if id]
            BillPaymentAttachment.objects.filter(
                id__in=deleted_ids,
                payment=payment
            ).delete()
        
        # Add new attachments
        files = request.FILES.getlist('attachments')
        for file in files:
            BillPaymentAttachment.objects.create(
                payment=payment,
                file=file,
                uploaded_by=request.user
            )

        
        # Post updated journal entry for the modified payment
        try:
            journal_entry = post_bill_payment_journal_entry(payment, request.user)
            if journal_entry:
                logger.info('Posted updated journal %s for payment %s', journal_entry.entry_number, payment.payment_number)
            else:
                logger.warning('Posting updated journal failed for payment %s', payment.payment_number)
        except Exception:
            logger.exception('Failed to post updated journal for payment %s', payment.payment_number)    
        
        messages.success(request, f'Payment #{payment.payment_number} updated successfully!')
        # return redirect('payment_detail', payment_id=payment.id)
        return redirect_with_company('payment_list')
    
    except Exception as e:
        messages.error(request, f'Error updating payment: {str(e)}')
        return redirect_with_company('edit_payment', payment_id=payment_id)


def update_bill_status(bill):
    """
    Update bill status based on payment + delivery completion.
    Keep the bill open even when fully paid and fully delivered.
    """
    total_paid = BillPaymentAllocation.objects.filter(
        bill=bill
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

    is_fully_paid = total_paid >= (bill.total_amount or Decimal('0.00'))

    print(f"Total Paid for Bill {bill.bill_number}: {total_paid}")
    print(f"Is Fully Paid for Bill {bill.bill_number}: {is_fully_paid}")

    # Delivery completion: remaining quantity must be zero for all bill items.
    is_fully_delivered = True
    for bill_item in bill.items.all():
        delivered = DeliveryNoteItem.objects.filter(
            bill_item=bill_item,
            delivery_note__stock_updated=True
        ).aggregate(total=Sum('quantity_delivered'))['total'] or 0

        if (bill_item.quantity - delivered) > 0:
            is_fully_delivered = False
            break

    if is_fully_paid and is_fully_delivered:
        bill.status = 'Open'
        bill.payment_status_id = 3  # Paid
        # If this bill originated from a converted purchase order, mark it as billed.
        if bill.order_number_id and bill.order_number.status != 'Billed':
            bill.order_number.status = 'Billed'
            bill.order_number.save(update_fields=['status'])
    elif is_fully_paid:
        bill.status = 'Open'
        bill.payment_status_id = 3  # paid
    elif total_paid > 0:
        bill.status = 'Open'
        bill.payment_status_id = 2  # Partial
    else:
        bill.status = 'Open'
        bill.payment_status_id = 1  # Open
    
    bill.save()


def post_bill_to_journal(bill, user=None):
    """
    Post a Purchase Bill to the Journal (Chart of Accounts).
    Mirrors the sales invoice posting logic.
    
    Journal Entry Structure:
    Dr: Stock In Hand / Inventory / Expense (from item.purchase_account)
    Dr: Input Tax CGST / SGST / IGST (GST only)
    Dr: Input VAT (VAT only)
    Dr: Stock In Hand / Inventory / Expense including purchase tax (US sales tax)
        Cr: Accounts Payable / Creditors
        Cr: Rounded Off (if applicable)
    
    Only posts if status is 'Open' or higher. Returns True if posted, False if already posted.
    """
    # Check if most recent entry for this bill is a reversal (if so, allow new posting)
    # Otherwise, check if any active (non-reversal) entry exists (if so, skip)
    # Prefer the DB where the `bill` instance was written (`_state.db`). This
    # is the most reliable indicator of the correct company DB during the
    # current request. Fall back to any request/company attributes or 'default'.
    company_db = None
    try:
        company_db = getattr(getattr(bill, '_state', None), 'db', None)
    except Exception:
        company_db = None

    if not company_db:
        company_db = getattr(getattr(bill, '_current_request', None), 'company_db', None) or getattr(bill, 'company_db', None) or 'default'

    # Ensure target DB actually has the journal tables. If the company DB is missing
    # the `journal` tables (common in newly created company DBs), fall back to
    # the master/default DB to avoid ProgrammingError (missing table).
    try:
        table_name = JournalEntry._meta.db_table
        try:
            existing_tables = connections[company_db].introspection.table_names()
        except Exception:
            existing_tables = []

        if table_name not in existing_tables:
            logger.warning(
                "Journal table %s not found in DB '%s', falling back to default DB",
                table_name,
                company_db,
            )
            company_db = 'default'
    except Exception:
        # If introspection fails for any reason, keep using the resolved company_db
        pass

    from django.db import ProgrammingError

    try:
        most_recent_entry = JournalEntry.objects.using(company_db).filter(
            reference=bill.bill_number
        ).order_by('-id').first()
    except ProgrammingError as pe:
        logger.warning(
            "Journal query failed on DB '%s' with ProgrammingError: %s — retrying on default DB",
            company_db,
            pe,
        )
        try:
            most_recent_entry = JournalEntry.objects.using('default').filter(
                reference=bill.bill_number
            ).order_by('-id').first()
            company_db = 'default'
        except Exception:
            # If this also fails, re-raise the original error to surface it.
            raise
    
    if most_recent_entry:
        # If most recent is NOT a reversal, skip posting (already posted before)
        if not most_recent_entry.narration.startswith('Reversal of'):
            return False  # Already posted and not reversed
        # If most recent IS a reversal, allow posting new entry (this is an edit scenario)
    
    try:
        refresh_document_total_base(bill)
        bill_items = BillItem.objects.using(company_db).filter(bill=bill)
        
        if not bill_items.exists():
            logger.warning(f"Bill {bill.bill_number} has no items, skipping journal posting")
            return False

        company_tax_type = get_company_tax_type(using=company_db)
        purchase_tax_is_recoverable = company_tax_type in ("GST", "VAT")
        
        # Calculate tax totals
        taxable_total = Decimal('0.00')
        tax_totals = {}  # {'CGST': ..., 'SGST': ...}
        
        for item in bill_items:
            qty = Decimal(item.quantity or 0)
            price = Decimal(item.price or 0)
            base = qty * price
            
            # Apply item discount
            discount_val = Decimal(item.prd_disvalue or 0)
            if item.prd_distype == 'percent':
                discount_amount = (base * discount_val) / Decimal('100')
            else:
                discount_amount = discount_val
            if discount_amount > base:
                discount_amount = base
            
            discounted = base - discount_amount
            if discounted < 0:
                discounted = Decimal('0.00')
            
            taxable_total += discounted
            
            # Parse tax group to get individual tax types
            if item.prd_taxgroup:
                tg = TaxGroup.objects.using(company_db).filter(group_name=item.prd_taxgroup).prefetch_related('taxes').first()
                if tg:
                    for t in tg.taxes.all():
                        taxtype = _get_tax_type_code(getattr(t, 'taxtype', None))
                        tax_amount = (discounted * (t.rate or Decimal(0))) / Decimal(100)
                        tax_amount = tax_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        tax_totals[taxtype] = tax_totals.get(taxtype, Decimal(0)) + tax_amount
        
        # Apply bill-level discount
        grand_discount_value = Decimal(str(bill.discount_value or 0))
        grand_discount_type = bill.discount_type or 'percent'
        if grand_discount_type == 'percent':
            grand_discount = (taxable_total * grand_discount_value) / Decimal('100')
        else:
            grand_discount = grand_discount_value
        if grand_discount > taxable_total:
            grand_discount = taxable_total
        
        # Final taxable (before tax)
        final_taxable = taxable_total - grand_discount
        final_taxable = final_taxable.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        # Recalculate taxes on final amount
        tax_totals = {}
        for item in bill_items:
            qty = Decimal(item.quantity or 0)
            price = Decimal(item.price or 0)
            base = qty * price
            
            discount_val = Decimal(item.prd_disvalue or 0)
            if item.prd_distype == 'percent':
                discount_amount = (base * discount_val) / Decimal('100')
            else:
                discount_amount = discount_val
            if discount_amount > base:
                discount_amount = base
            
            discounted = base - discount_amount
            if discounted < 0:
                discounted = Decimal('0.00')
            
            # Apply proportional grand discount
            item_proportion = discounted / taxable_total if taxable_total > 0 else 0
            item_grand_discount = grand_discount * item_proportion
            item_final_base = discounted - item_grand_discount
            
            if item.prd_taxgroup:
                tg = TaxGroup.objects.using(company_db).filter(group_name=item.prd_taxgroup).prefetch_related('taxes').first()
                if tg:
                    for t in tg.taxes.all():
                        taxtype = _get_tax_type_code(getattr(t, 'taxtype', None))
                        tax_amount = (item_final_base * (t.rate or Decimal(0))) / Decimal(100)
                        tax_amount = tax_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        tax_totals[taxtype] = tax_totals.get(taxtype, Decimal(0)) + tax_amount
        
        # Generate Journal Entry Number
        last = JournalEntry.objects.using(company_db).order_by('-id').first()
        if last and last.entry_number and last.entry_number.startswith('JV-'):
            try:
                last_num = int(last.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        next_num = last_num + 1
        while JournalEntry.objects.using(company_db).filter(entry_number=f"JV-{str(next_num).zfill(5)}").exists():
            next_num += 1
        entry_number = f"JV-{str(next_num).zfill(5)}"
        
        # Create Journal Entry
        journal = JournalEntry.objects.using(company_db).create(
            entry_number=entry_number,
            date=bill.date,
            reference=bill.bill_number,
            narration=f"Purchase Bill {bill.bill_number}",
            created_by=user,
            updated_by=user,
            status='posted'
        )
        
        seq = 10
        total_debit = Decimal('0.00')
        total_credit = Decimal('0.00')
        
        # Debit: Inventory/Stock/Expense accounts (one line per item type)
        item_accounts = {}  # Group items by purchase account
        
        for item in bill_items:
            # Get purchase account from item, default to 'Stock In Hand'
            purchase_acct_name = getattr(item.product, 'purchase_account', None)
            if not purchase_acct_name:
                purchase_acct_name = 'Stock In Hand'
            
            if purchase_acct_name not in item_accounts:
                item_accounts[purchase_acct_name] = Decimal('0.00')
            
            # Calculate this item's contribution
            qty = Decimal(item.quantity or 0)
            price = Decimal(item.price or 0)
            base = qty * price
            
            discount_val = Decimal(item.prd_disvalue or 0)
            if item.prd_distype == 'percent':
                discount_amount = (base * discount_val) / Decimal('100')
            else:
                discount_amount = discount_val
            if discount_amount > base:
                discount_amount = base
            
            discounted = base - discount_amount
            if discounted < 0:
                discounted = Decimal('0.00')
            
            # Apply proportional grand discount
            item_proportion = discounted / taxable_total if taxable_total > 0 else 0
            item_grand_discount = grand_discount * item_proportion
            item_final_base = discounted - item_grand_discount

            item_account_amount = item_final_base
            if not purchase_tax_is_recoverable and company_tax_type == "SALES":
                item_tax_rate = Decimal(str(item.prd_tax or 0))
                item_tax_amount = (item_final_base * item_tax_rate / Decimal('100')).quantize(
                    Decimal('0.01'),
                    rounding=ROUND_HALF_UP,
                )
                item_account_amount += item_tax_amount

            item_accounts[purchase_acct_name] += item_account_amount
        
        # Create debit lines for each purchase account
        for acct_name, amount in item_accounts.items():
            amount_base = scale_amount_for_journal(amount, bill)
            if amount_base == Decimal('0.00'):
                continue
            
            # Find or create the account - prioritize exact matches first
            acct = None
            
            # 1. First try exact name match
            acct = ChartOfAccounts.objects.using(company_db).filter(name__exact=acct_name).first()
            
            # 2. Then case-insensitive match
            if not acct:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact=acct_name).first()
            
            # 3. If still not found and looking for Stock In Hand, search specifically for it
            if not acct and 'Stock In Hand' in acct_name:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__exact='Stock In Hand').first()
            
            # 4. If still not found, try case-insensitive Stock In Hand
            if not acct:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact='Stock In Hand').first()
            
            # 5. Last resort: any stock-related account
            if not acct:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Stock In Hand').first()
            if not acct:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Stock').first()
            
            if acct:
                JournalLine.objects.using(company_db).create(
                    journal=journal,
                    account=acct,
                    description=f"Bill {bill.bill_number} – {acct.name}",
                    debit=amount_base,
                    credit=Decimal('0.00'),
                    sequence=seq
                )
                total_debit += amount_base
                seq += 10
        
        # Debit: Input Tax accounts
        if company_tax_type == "GST":
            tax_account_map = {
                'CGST': 'Input Tax CGST',
                'SGST': 'Input Tax SGST',
                'IGST': 'Input Tax IGST',
            }

            for ttype, amount in tax_totals.items():
                amount_base = scale_amount_for_journal(amount, bill)
                if amount_base == Decimal('0.00'):
                    continue

                acct_name = tax_account_map.get(ttype, None)
                acct = None
                if acct_name:
                    acct = ChartOfAccounts.objects.using(company_db).filter(name__exact=acct_name).first()
                if not acct and acct_name:
                    acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact=acct_name).first()
                if not acct:
                    acct = ChartOfAccounts.objects.using(company_db).exclude(name__icontains='Refund').filter(name__icontains=f'Input Tax {ttype}').first()

                if acct:
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=acct,
                        description=f"Bill {bill.bill_number} – {acct.name}",
                        debit=amount_base,
                        credit=Decimal('0.00'),
                        sequence=seq
                    )
                    total_debit += amount_base
                    seq += 10
        elif company_tax_type == "VAT":
            # VAT purchase tax is recoverable, so keep it out of item cost.
            tax_total = sum((Decimal(v or 0) for v in tax_totals.values()), Decimal('0.00'))
            tax_total_base = scale_amount_for_journal(tax_total, bill)
            if tax_total_base != Decimal('0.00'):
                acct = resolve_tax_account(None, direction='input', using=company_db)
                if acct:
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=acct,
                        description=f"Bill {bill.bill_number} – {acct.name}",
                        debit=tax_total_base,
                        credit=Decimal('0.00'),
                        sequence=seq
                    )
                    total_debit += tax_total_base
                    seq += 10
        # US sales tax purchase tax is not recoverable, so it is already included
        # in the item account debit above instead of being posted to Input Tax.
        
        # Credit: Accounts Payable / Creditors
        creditor_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Creditors').first()
        if not creditor_acct:
            creditor_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Accounts Payable').first()
        
        payable_amount = Decimal(str(getattr(bill, 'total_amount_base', None) or 0)).quantize(
            Decimal('0.01'),
            rounding=ROUND_HALF_UP,
        )
        if payable_amount == Decimal('0.00'):
            payable_amount = scale_amount_for_journal(Decimal(str(getattr(bill, 'total_amount', None) or 0)), bill)
        
        if creditor_acct:
            JournalLine.objects.using(company_db).create(
                journal=journal,
                account=creditor_acct,
                description=f"Bill {bill.bill_number} – {creditor_acct.name}",
                debit=Decimal('0.00'),
                credit=payable_amount,
                sequence=seq
            )
            total_credit += payable_amount
            seq += 10

        seq = _post_tds_tcs_journal_line(journal, bill, company_db, seq)

        # Recompute totals after any TDS/TCS journal line has been added.
        total_debit = sum(line.debit or Decimal('0.00') for line in journal.lines.all())
        total_credit = sum(line.credit or Decimal('0.00') for line in journal.lines.all())

        diff = (total_debit - total_credit).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if diff != Decimal('0.00'):
            rounded_off_acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact='Rounded Off').first()
            if not rounded_off_acct:
                rounded_off_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Rounding').first()
            
            if rounded_off_acct:
                if diff > 0:  # Debit more, so credit the difference
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=rounded_off_acct,
                        description=f"Bill {bill.bill_number} – {rounded_off_acct.name}",
                        debit=Decimal('0.00'),
                        credit=abs(diff),
                        sequence=seq
                    )
                else:  # Credit more, so debit the difference
                    JournalLine.objects.using(company_db).create(
                        journal=journal,
                        account=rounded_off_acct,
                        description=f"Bill {bill.bill_number} – {rounded_off_acct.name}",
                        debit=abs(diff),
                        credit=Decimal('0.00'),
                        sequence=seq
                    )

        journal.total_debit = sum(line.debit or Decimal('0.00') for line in journal.lines.all())
        journal.total_credit = sum(line.credit or Decimal('0.00') for line in journal.lines.all())
        journal.save()
        
        logger.info(f"Posted Bill {bill.bill_number} to Journal {entry_number}")
        return True
        
    except Exception as e:
        logger.exception(f"Error posting Bill {bill.bill_number} to journal: {e}")
        return False

def reverse_purchase_bill_journal_for_return(purchase_return, user=None, company_db='default'):
    """
    Create reverse journal entries for a purchase return.
    Mirrors the original bill's journal entries with reversed debits/credits.
    
    This function:
    1. Reverses the original bill journal entry (proportionally for partial returns)
    2. Reverses payment allocations (if any)
    
    For partial returns, amounts are scaled by the return proportion.
    """
    try:
        bill = purchase_return.bill
        
        # Helper function to get next journal entry number
        def _next_jv():
            last = JournalEntry.objects.using(company_db).order_by('-id').first()
            if last and last.entry_number and last.entry_number.startswith('JV-'):
                try:
                    last_num = int(last.entry_number.split('-')[1])
                except Exception:
                    last_num = 0
            else:
                last_num = 0
            return f"JV-{str(last_num + 1).zfill(5)}"
        
        # Use the return date for reversal journal
        rev_date = getattr(purchase_return, 'date', None) or timezone.now().date()
        
        proportion = _get_purchase_return_reversal_proportion(
            purchase_return,
            bill=bill,
            company_db=company_db,
        )
        
        # --- Reverse original bill journal entry ---
        old_bill_entry = JournalEntry.objects.using(company_db).filter(
            reference=bill.bill_number
        ).exclude(narration__startswith='Reversal of').prefetch_related('lines').order_by('-id').first()
        
        if old_bill_entry:
            entry_number = _next_jv()
            rev = JournalEntry.objects.using(company_db).create(
                entry_number=entry_number,
                date=rev_date,
                reference=f"Reversal-Return-{purchase_return.return_number}-Bill-{bill.bill_number}",
                narration=f"Reversal of {old_bill_entry.entry_number} for Purchase Return {purchase_return.return_number}",
                created_by=user,
                updated_by=user,
                status='posted'
            )
            
            for line in old_bill_entry.lines.all():
                # Scale amounts by proportion for partial returns
                try:
                    # Reverse: debit becomes credit, credit becomes debit
                    rev_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    rev_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                except Exception:
                    rev_debit = line.credit
                    rev_credit = line.debit
                
                JournalLine.objects.using(company_db).create(
                    journal=rev,
                    account=line.account,
                    description=f"Reversal: {line.description}",
                    debit=rev_debit,
                    credit=rev_credit,
                    sequence=line.sequence
                )
            
            try:
                rev.total_debit = sum(l.debit or 0 for l in rev.lines.all())
                rev.total_credit = sum(l.credit or 0 for l in rev.lines.all())
                rev.save(update_fields=['total_debit', 'total_credit'])
            except Exception:
                logger.exception('Failed to update totals for bill reversal %s', getattr(rev, 'id', None))
        
        # --- Reverse payment allocations for this bill ---
        allocations = BillPaymentAllocation.objects.using(company_db).filter(
            bill=bill
        ).select_related('payment')
        
        try:
            remaining_refund = Decimal(str(getattr(purchase_return, 'refund_amount', None) or 0))
        except Exception:
            remaining_refund = None

        for alloc in allocations:
            try:
                amt = alloc.amount or Decimal('0.00')
                if amt <= 0:
                    continue
                
                payment_obj = getattr(alloc, 'payment', None)
                paid_through_acct = getattr(payment_obj, 'paid_through', None) if payment_obj else None
                
                # Get creditors/accounts payable account
                cred_acct = ChartOfAccounts.objects.using(company_db).filter(
                    name__icontains='Creditors'
                ).first()
                if not cred_acct:
                    cred_acct = ChartOfAccounts.objects.using(company_db).filter(
                        code='2020101'
                    ).first()
                
                if paid_through_acct and cred_acct:
                    try:
                        if remaining_refund is not None:
                            if remaining_refund <= 0:
                                alloc_reverse_amt = Decimal('0.00')
                            else:
                                alloc_reverse_amt = min(Decimal(str(amt)), remaining_refund)
                                remaining_refund = (remaining_refund - alloc_reverse_amt).quantize(
                                    Decimal('0.01'),
                                    rounding=ROUND_HALF_UP,
                                )
                        else:
                            alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    except Exception:
                        alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    if alloc_reverse_amt and alloc_reverse_amt > 0:
                        alloc_reverse_base = scale_amount_for_journal(alloc_reverse_amt, bill)
                        entry_number = _next_jv()
                        pay_rev = JournalEntry.objects.using(company_db).create(
                            entry_number=entry_number,
                            date=rev_date,
                            reference=f"Reversal-PAY-{getattr(payment_obj, 'payment_number', '')}-BILL-{bill.bill_number}",
                            narration=f"Reversal of payment allocation for Bill {bill.bill_number} - Purchase Return {purchase_return.return_number}",
                            created_by=user,
                            updated_by=user,
                            status='posted'
                        )
                        
                        # Debit Bank/Cash (reduce paid amount)
                        JournalLine.objects.using(company_db).create(
                            journal=pay_rev,
                            account=paid_through_acct,
                            description=f"Reversal alloc: Payment #{getattr(payment_obj, 'payment_number', '')}",
                            debit=alloc_reverse_base,
                            credit=Decimal('0.00'),
                            sequence=10
                        )
                        
                        # Credit Creditors (increase payable)
                        JournalLine.objects.using(company_db).create(
                            journal=pay_rev,
                            account=cred_acct,
                            description=f"Reversal alloc: Bill {bill.bill_number}",
                            debit=Decimal('0.00'),
                            credit=alloc_reverse_base,
                            sequence=20
                        )
                        
                        try:
                            pay_rev.total_debit = sum(l.debit or 0 for l in pay_rev.lines.all())
                            pay_rev.total_credit = sum(l.credit or 0 for l in pay_rev.lines.all())
                            pay_rev.save(update_fields=['total_debit', 'total_credit'])
                        except Exception:
                            logger.exception('Failed to update totals for payment reversal %s', getattr(pay_rev, 'id', None))
            except Exception:
                logger.exception('Failed to create reversal for allocation %s', getattr(alloc, 'id', None))
        
        return True
        
    except Exception as e:
        logger.exception(f"Error creating reverse journal entries for purchase return {getattr(purchase_return, 'id', None)}: {e}")
        return False



def payment_detail(request, payment_id):
    """
    Display detailed information about a specific payment.
    """
    # Permission: require view access to Purchase Payments Made
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_payments(request.user)):
            messages.error(request, 'You do not have permission to view payments.')
            return redirect_with_company('index')
    except Exception:
        messages.error(request, 'You do not have permission to view payments.')
        return redirect_with_company('index')
    
    payment = get_object_or_404(BillPayment, pk=payment_id)
    
    # Get all allocations for this payment
    allocations = BillPaymentAllocation.objects.filter(
        payment=payment
    ).select_related('bill').order_by('bill__bill_number')
    
    # Calculate totals
    total_allocated = allocations.aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')
    
    # Get advance transactions related to this payment
    advance_transactions = VendorAdvancePayment.objects.filter(
        payment=payment
    ).order_by('created_at')
    
    # Calculate advance used and added
    advance_used = Decimal('0.00')
    advance_added = Decimal('0.00')
    
    for txn in advance_transactions:
        if txn.amount < 0:
            advance_used += abs(txn.amount)
        else:
            advance_added += txn.amount
    
    # Get attachments
    attachments = payment.attachments.all()
    
    # Prepare allocated bills data
    allocated_bills = []
    for allocation in allocations:
        bill = allocation.bill
        allocated_bills.append({
            'bill': bill,
            'allocated_amount': allocation.amount,
            'payment_made_on': allocation.payment_made_on,
        })
    
    context = {
        'payment': payment,
        'allocated_bills': allocated_bills,
        'total_allocated': total_allocated,
        'advance_used': advance_used,
        'advance_added': advance_added,
        'attachments': attachments,
        'has_allocations': allocations.exists(),
        'payment_currency_symbol': payment.get_currency_symbol(),
    }
    
    return render(request, 'Purchase/payment_detail.html', context)


# fn created by sree on 08-01-2026 to update bill status
def update_purchase_bill_status(request, bill_id):
    """Update bill status via AJAX."""
    if request.method == 'POST':
        try:
            bill = get_object_or_404(Bill, pk=bill_id)
            old_status = bill.status

            new_status = request.POST.get('status', '').strip()
            
            logger.debug(f"Attempting to update bill {bill_id} status to: {new_status}")
            
            # Validate status against allowed choices
            valid_statuses = [choice[0] for choice in Bill.STATUS_CHOICES]
            logger.debug(f"Valid statuses: {valid_statuses}")
            
            if new_status not in valid_statuses:
                logger.warning(f"Invalid status '{new_status}' provided. Valid options: {valid_statuses}")
                return JsonResponse({'success': False, 'error': f'Invalid status. Valid options: {", ".join(valid_statuses)}'}, status=400)
            
            # Manual status changes are allowed between Open and Closed.
            if new_status not in {'Open', 'Closed'}:
                return JsonResponse({
                    'success': False,
                    'error': "Only 'Open' or 'Closed' can be set manually."
                }, status=400)

            bill.status = new_status
            bill.save()
            logger.info(f"Successfully updated bill {bill_id} status to: {new_status}")
            
            return JsonResponse({
                'success': True,
                'message': f"Bill status updated to '{new_status}'.",
                'status': bill.status
            })
        except Exception as e:
            logger.exception("Error updating bill status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)



# fn created by sree on 08-01-26
def _legacy_update_stock_from_bill(bill, user):
    """
    Update stock when bill is closed.
    Increases stock quantity for all items in the bill.
    
    Args:
        bill: Bill instance
        user: User performing the action
    
    Returns:
        bool: True if successful, False otherwise
    """
    # from Items.models import Stock, Warehouse
    
    # Get the default warehouse or the warehouse from purchase order
    warehouse = None
    
    # Try to get warehouse from the related purchase order
    # if hasattr(bill, 'order_number') and bill.order_number:
    if hasattr(bill, 'warehouse'):
            warehouse = bill.warehouse
            print("warehouse selected from related purchase order")
            print("warehouse is",warehouse)

    
    # If no warehouse found, get the default warehouse
    if not warehouse:
        warehouse = Warehouse.objects.filter(is_default=True).first()
        print("warehouse selected from default warehouse")
        print("warehouse is",warehouse)


    
    # If still no warehouse, get the first warehouse
    if not warehouse:
        warehouse = Warehouse.objects.first()
        print("warehouse selected from first warehouse")
        print("warehouse is",warehouse)


    
    if not warehouse:
        print("not getting warehouse")
        logger.error(f"No warehouse found for bill {bill.id}")
        return False
    
    # Update stock for each item in the bill
    for bill_item in bill.items.all():
        try:
            # Get or create stock record for this item in the warehouse
            stock, created = Stock.objects.get_or_create(
                item=bill_item.product,
                warehouse=warehouse,
                defaults={
                    'quantity': 0,
                    'opening_stock': 0,
                    'created_by': user
                }
            )
            
            # Increase stock quantity
            old_quantity = stock.quantity
            stock.quantity += bill_item.quantity
            stock.save()
            
            logger.info(
                f"Stock updated for item {bill_item.product.name}: "
                f"{old_quantity} → {stock.quantity} (added {bill_item.quantity})"
            )
            
            
        
        except Exception as e:
            logger.exception(f"Error updating stock for item {bill_item.product.id}: {e}")
            return False
    
    return True


def _get_purchase_stock_db(bill=None, request=None):
    if request is not None:
        request_db = getattr(request, 'company_db', None)
        if request_db:
            return request_db

    try:
        bill_db = getattr(getattr(bill, '_state', None), 'db', None)
        if bill_db:
            return bill_db
    except Exception:
        pass

    return 'default'


def _get_purchase_stock_warehouse(bill, request=None):
    db = _get_purchase_stock_db(bill=bill, request=request)

    if getattr(bill, 'warehouse_id', None):
        warehouse = Warehouse.objects.using(db).filter(pk=bill.warehouse_id).first()
        if warehouse:
            return warehouse

    warehouse = Warehouse.objects.using(db).filter(is_default=True).first()
    if warehouse:
        return warehouse

    return Warehouse.objects.using(db).first()


def update_stock_from_bill(bill, user, request=None):
    """
    Update stock when a purchase bill is fully paid and stock is managed on payment.
    Increases stock quantity for bill items that have not already been stocked.
    """
    if request is not None and is_stock_management_on_delivery(request=request):
        return False

    db = _get_purchase_stock_db(bill=bill, request=request)
    warehouse = _get_purchase_stock_warehouse(bill, request=request)

    if not warehouse:
        logger.error("No warehouse found for bill %s", bill.id)
        return False

    updated = False
    payment_stock_exists = StockMovement.objects.using(db).filter(
        reference_type='purchase_bill_payment',
        reference_id=bill.id,
        movement_type='in',
    ).exists()

    if not payment_stock_exists:
        delivered_notes = DeliveryNote.objects.using(db).filter(
            bill_id=bill.id,
            status='delivered',
            stock_updated=False,
        ).select_related('warehouse')

        for delivery_note in delivered_notes:
            try:
                delivery_note.update_stock(user=user)
                updated = True
            except Exception:
                logger.exception(
                    "Failed to update stock from delivered Purchase DeliveryNote %s",
                    getattr(delivery_note, 'delivery_note_number', None),
                )

    bill_quantities = defaultdict(Decimal)
    products = {}

    for bill_item in BillItem.objects.using(db).filter(bill_id=bill.id).select_related('product'):
        product = bill_item.product
        if not getattr(product, 'track_inventory', False):
            continue

        bill_quantities[product.id] += Decimal(bill_item.quantity or 0)
        products[product.id] = product

    with transaction.atomic():
        for product_id, bill_quantity in bill_quantities.items():
            product = products[product_id]
            delivered_qty = DeliveryNoteItem.objects.using(db).filter(
                bill_item__bill_id=bill.id,
                bill_item__product_id=product_id,
                delivery_note__status='delivered',
                delivery_note__stock_updated=True,
            ).aggregate(total=Sum('quantity_delivered'))['total'] or Decimal('0')

            paid_stock_qty = StockMovement.objects.using(db).filter(
                stock__item_id=product_id,
                reference_type='purchase_bill_payment',
                reference_id=bill.id,
                movement_type='in',
            ).aggregate(total=Sum('quantity'))['total'] or Decimal('0')

            remaining_qty = bill_quantity - Decimal(delivered_qty or 0) - Decimal(paid_stock_qty or 0)
            if remaining_qty <= 0:
                continue

            stock, created = Stock.objects.using(db).get_or_create(
                item=product,
                warehouse=warehouse,
                batch_number=None,
                serial_number=None,
                defaults={
                    'quantity': Decimal('0.00'),
                    'opening_stock': Decimal('0.00'),
                    'created_by': user,
                },
            )

            old_quantity = stock.quantity
            stock.quantity += remaining_qty
            stock.updated_by = user
            stock.save(using=db)

            StockMovement.objects.using(db).create(
                stock=stock,
                movement_type='in',
                quantity=remaining_qty,
                reference_type='purchase_bill_payment',
                reference_id=bill.id,
                delivery_note=None,
                notes=f"Stock in from Purchase Bill Payment {bill.bill_number}",
                created_by=user,
            )

            logger.info(
                "Stock updated for item %s from bill %s: %s -> %s (added %s)",
                product.name,
                bill.bill_number,
                old_quantity,
                stock.quantity,
                remaining_qty,
            )
            updated = True

    return updated


# fn created by sree on 08-01-26
def reverse_stock_from_bill(bill, user):
    """
    Reverse stock changes when bill status changes from 'Closed' to another status.
    Decreases stock quantity for all items in the bill.
    
    Args:
        bill: Bill instance
        user: User performing the action
    
    Returns:
        bool: True if successful, False otherwise
    """
    # from Items.models import Stock, Warehouse
    
    # Get the warehouse (same logic as update_stock_from_bill)
    warehouse = None
    
    if hasattr(bill, 'order_number') and bill.order_number:
        if hasattr(bill.order_number, 'warehouse'):
            warehouse = bill.order_number.warehouse
    
    if not warehouse:
        warehouse = Warehouse.objects.filter(is_default=True).first()
    
    if not warehouse:
        warehouse = Warehouse.objects.first()
    
    if not warehouse:
        logger.error(f"No warehouse found for bill {bill.id}")
        return False
    
    # Reverse stock for each item in the bill
    for bill_item in bill.items.all():
        try:
            # Get stock record
            stock = Stock.objects.filter(
                item=bill_item.product,
                warehouse=warehouse
            ).first()
            
            if not stock:
                logger.warning(
                    f"No stock record found for item {bill_item.product.name} "
                    f"in warehouse {warehouse.name}"
                )
                continue
            
            # Check if we have enough stock to reverse
            if stock.quantity < bill_item.quantity:
                logger.warning(
                    f"Insufficient stock to reverse for item {bill_item.product.name}. "
                    f"Current: {stock.quantity}, Needed: {bill_item.quantity}"
                )
                # Still proceed with reversal, but log the warning
            
            # Decrease stock quantity
            old_quantity = stock.quantity
            stock.quantity -= bill_item.quantity
            
            # Prevent negative stock
            if stock.quantity < 0:
                stock.quantity = 0
            
            stock.save()
            
            logger.info(
                f"Stock reversed for item {bill_item.product.name}: "
                f"{old_quantity} → {stock.quantity} (removed {bill_item.quantity})"
            )
            
            # Log the stock change
            # try:
            #     from activity_log.utils import log_activity
            #     log_activity(
            #         user=user,
            #         action='UPDATE',
            #         content_object=stock,
            #         description=f'Stock decreased due to bill {bill.bill_number} status reversal',
            #         changes={
            #             'quantity': {
            #                 'old': str(old_quantity),
            #                 'new': str(stock.quantity)
            #             }
            #         },
            #         related_object=bill
            #     )
            # except Exception as log_error:
            #     logger.warning(f"Failed to log stock activity: {log_error}")
        
        except Exception as e:
            logger.exception(f"Error reversing stock for item {bill_item.product.id}: {e}")
            return False
    
    return True


def purchaseorder_pdf_view(request, pk):
    """Generate a PDF for the quotation using ReportLab with proper rupee symbol support."""
    order = get_object_or_404(PurchaseOrder, pk=pk)
    pdf = generate_porder_pdf_bytes(pk)
    
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="Purchase_order_{order.order_number}.pdf"'
    return response

def generate_porder_pdf_bytes(pk):
    """Generate PDF bytes for quotation using ReportLab - can be used for display or email."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    
    order = get_object_or_404(PurchaseOrder, pk=pk)
    context = build_porder_context(pk)
    
    # Register DejaVu Sans font (supports ₹ symbol)
    try:
        from django.conf import settings
        static_font_path = os.path.join(settings.BASE_DIR, 'static', 'fonts', 'DejaVuSans.ttf')
        
        font_registered = False
        
        # Try static folder first
        if os.path.exists(static_font_path):
            pdfmetrics.registerFont(TTFont('DejaVuSans', static_font_path))
            font_registered = True
        else:
            # Fallback to system fonts
            font_paths = [
                'C:/Windows/Fonts/DejaVuSans.ttf',  # Windows
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',  # Linux
                '/System/Library/Fonts/Supplemental/DejaVuSans.ttf',  # macOS
            ]
            
            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))
                    font_registered = True
                    break
    except:
        font_registered = False
    
    font_name = 'DejaVuSans' if font_registered else 'Helvetica'
    
    # Create PDF buffer and document
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                          rightMargin=10*mm, leftMargin=10*mm,
                          topMargin=10*mm, bottomMargin=10*mm,
                          title=f'Purchase Order {pk}',
                          author='LyraERP')
    
    # Container for PDF elements
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom styles with smaller sizes for single page fit
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=0,
        fontName='Helvetica-Bold',
        alignment=TA_LEFT
    )
    
    order_badge_style = ParagraphStyle(
        'Badge',
        parent=styles['Normal'],
        fontSize=16,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT
    )
    
    company_header_style = ParagraphStyle(
        'CompanyHeader',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#555555'),
        fontName=font_name,
        leading=10
    )
    
    meta_label_style = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#7f8c8d'),
        fontName='Helvetica-Bold'
    )
    
    meta_value_style = ParagraphStyle(
        'MetaValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name
    )
    
    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#ffffff'),
        fontName='Helvetica-Bold',
        alignment=TA_LEFT
    )
    
    label_style = ParagraphStyle(
        'Label',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#ffffff'),
        fontName='Helvetica-Bold'
    )
    
    value_style = ParagraphStyle(
        'Value',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name
    )
    
    amount_style = ParagraphStyle(
        'Amount',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    company = Company.objects.filter(status=True).first() or Company.objects.first()
    # HEADER with professional styling
    legal_name = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name_value = legal_name or ((getattr(company, 'name', '') or '').strip() if company else '')
    company_name = safe(company_name_value)
    header_left_cell = Paragraph(f"<b>{company_name}</b>", title_style)
    show_logo_in_print_pdf = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
    try:
        if show_logo_in_print_pdf and company and getattr(company, 'logo', None) and company.logo.path and os.path.exists(company.logo.path):
            header_left_cell = Image(company.logo.path, width=45*mm, height=14*mm)
    except Exception:
        pass

    
    header_data = [
        [header_left_cell, Paragraph("Purchase Order", order_badge_style)],
    ]
    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    # COMPANY INFO - Professional styling
    company_header_style.leftIndent = 10 
    
    company_info = f"""
    <font color='#2c3e50'><b>{company_name}</b></font><br/>
    <font size='6' color='#555555'>
    {safe(company.address_line1)}<br/>
    {safe(company.address_line2)}<br/>
    {safe(company.city)}, {safe(company.state)}, {safe(company.country)}, {safe(company.postal_code)}<br/>
    GSTIN: {safe(company.tax_id)}<br/>
    {safe(company.email)}
    </font>
    """
    elements.append(Paragraph(company_info, company_header_style))
    elements.append(Spacer(1, 6*mm))
    
    # META INFO - Professional styling
    
    payment_term_display = "-"
    if getattr(order, 'payment_term', None):
        payment_term_display = getattr(order.payment_term, 'name', None) or str(order.payment_term)
    else:
        payment_term_id = getattr(order, 'payment_term_id', None)
        if payment_term_id:
            payment_status_name = PaymentStatus.objects.filter(pk=payment_term_id).values_list('name', flat=True).first()
            if payment_status_name:
                payment_term_display = payment_status_name

    meta_data = [
        [Paragraph("<b>Order No:</b>", meta_label_style), Paragraph(str(context.get('o_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(order.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply:</b>", meta_label_style), Paragraph(str(order.place_of_supply or '-'), meta_value_style),
        Paragraph("<b>Payment Terms:</b>", meta_label_style), Paragraph(str(payment_term_display), meta_value_style)],
    ]
    meta_table = Table(meta_data, colWidths=[141, 125, 125, 125])
    meta_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f8f9fa')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 5*mm))
    
    # ADDRESSES - Professional styling
    bill_to = "<b color='#2c3e50' size='15'>Bill To:</b><br/>"
    if order.vendor:
        bill_lines = []
        customer_type = (getattr(order.vendor, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(order.vendor, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(order.vendor, 'first_name', '') or '').strip()
            last_name = (getattr(order.vendor, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)


        if getattr(order.vendor, 'address_line_1', None):
            bill_lines.append(order.vendor.address_line_1)

        if getattr(order.vendor, 'state', None):
            bill_lines.append(str(order.vendor.state))

        if getattr(order.vendor, 'country', None):
            bill_lines.append(str(order.vendor.country))

        gst = getattr(order.vendor, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b color='#2c3e50' size='10'>Ship To:</b><br/>"
    if context.get('order'):
        order_obj = context['order']
        if order.shipping_attention:
            ship_lines = []

            if getattr(order_obj, 'shipping_attention', None):
                ship_lines.append(order_obj.shipping_attention)

            if getattr(order_obj, 'shipping_address1', None):
                ship_lines.append(order_obj.shipping_address1)

            if ship_lines:
                ship_to += "<font size='7' color='#2c3e50'>"
                ship_to += "<br/>".join(ship_lines)
                ship_to += "</font>"
        else:
            ship_to += "-"
    
    addr_data = [
        [Paragraph(bill_to, value_style), Paragraph(ship_to, value_style)]
    ]
    addr_table = Table(addr_data, colWidths=[260, 260])
    addr_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
    ]))
    elements.append(addr_table)
    elements.append(Spacer(1, 6*mm))
    
    # ITEMS TABLE - Professional styling
    # Prefer document currency symbol from context, fall back to company base symbol, then to rupee
    document_symbol = context.get('document_currency_symbol')
    company_symbol = context.get('company_base_currency_symbol')
    currency = (document_symbol or company_symbol) if (document_symbol or company_symbol) else ("₹" if font_registered else "Rs.")
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', 'GST')
    if company_tax_type == 'GST':
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style),
            Paragraph("<b>CGST</b>", label_style),
            Paragraph("<b>SGST</b>", label_style)]
        ]
    elif company_tax_type in ('VAT', 'SALES'):
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style),
            Paragraph("<b>Tax Rate</b>", label_style)]
        ]
    elif company_tax_type in ('TURNOVER', 'NONE'):
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style)]
        ]
    
    for idx, item in enumerate(context.get('items_info', []), 1):
        tax_rate = Decimal(str(item.get('tax_rate', 0)))
        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        cgst_amount = tax_amount / 2
        sgst_amount = tax_amount / 2
        
        if company_tax_type == 'GST':
            # Split tax equally for India (CGST/SGST)
            cgst_rate = tax_rate / 2
            sgst_rate = tax_rate / 2
            cgst_amount = tax_amount / 2
            sgst_amount = tax_amount / 2
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                Paragraph(f"{cgst_rate:.2f}%<br/>{currency}\u00A0{float(cgst_amount):.2f}", amount_style),
                Paragraph(f"{sgst_rate:.2f}%<br/>{currency}\u00A0{float(sgst_amount):.2f}", amount_style),
            ])
        elif company_tax_type in ('VAT', 'SALES'):
            # Show single VAT column for non-India
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                Paragraph(f"{tax_rate:.2f}%", amount_style),
               
            ])
        elif company_tax_type in ('TURNOVER', 'NONE'):
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                
               
            ])
    
    items_table = Table(items_data, colWidths=[25, 135, 59, 30, 90, 90, 90])
    items_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#ffffff')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 1), (7, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8f9fa')]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 5*mm))
    
    # TOTALS - Professional styling
    # Calculate correct subtotal (without tax) for PDF display
    pdf_subtotal = Decimal('0.00')
    for item in context.get('items_info', []):
        pdf_subtotal += Decimal(str(item.get('line_total', 0)))
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    company_is_india = context.get('company_is_india', True)
    
    if company_tax_type == 'GST':
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
            [Paragraph("CGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_cgst', 0):.2f}", amount_style)],
            [Paragraph("SGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_sgst', 0):.2f}", amount_style)],
        ]
    elif company_tax_type in ('VAT', 'SALES', 'TURNOVER'):
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
            [Paragraph("Tax", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_vat', 0):.2f}", amount_style)],
        ]
    else:
        totals_data = [
        ]
    # Add round off if it exists
    order_obj = context.get('order')
    round_off_value = getattr(order_obj, 'round_off', None) if order_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}", amount_style)])
    
    grand_total_style = ParagraphStyle(
        'GrandTotal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2c3e50'),
        fontName='DejaVuSans',
        alignment=TA_RIGHT
    )
    
    totals_data.append([Paragraph("<b>Grand Total</b>", ParagraphStyle('GrandTotalLabel', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#2c3e50'))), 
                       Paragraph(f"<b>{currency}\u00A0{context.get('final_total', 0):.2f}</b>", grand_total_style)])
    
    totals_table = Table(totals_data, colWidths=[371, 150])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-2, -1), 7),
        ('FONTSIZE', (-2, -1), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('BACKGROUND', (0, 0), (-1, -2), colors.HexColor('#ffffff')),
        ('BACKGROUND', (-2, -1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 5*mm))
    
    # NOTES & SIGNATURE
    order_obj = context.get('order')
    notes_text = (getattr(order_obj, 'notes', None) or '').strip() or '-'
    notes_text = safe(notes_text).replace('\n', '<br/>')
    terms_text = (getattr(company, 'terms_and_conditions', None) or '').strip() or '-'
    terms_text = safe(terms_text).replace('\n', '<br/>')
    bottom_data = [
        [Paragraph(
            f"<b>Notes</b><br/><font size=6>{notes_text}</font><br/><br/>"
            f"<b>Terms & Conditions</b><br/><font size=6>{terms_text}</font>",
            value_style
        ),
         Paragraph(f"<b>Authorized Signatory</b><br/><br/>For {company_name}", value_style)]
    ]
    bottom_table = Table(bottom_data, colWidths=[330, 190])  # ADJUST: [Notes Width, Signature Width]
    bottom_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('LINEWIDTH', (0, 0), (-1, -1), 1),
    ]))
    elements.append(bottom_table)
    
    # FOOTER - Use Canvas for page numbering
    from reportlab.pdfgen.canvas import Canvas
    
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, "POWERED BY LyraERP")
        page_num = canvas.getPageNumber()
        canvas.drawRightString(570, 20, f"Page {page_num} of {doc.page if hasattr(doc, 'page') else page_num}")
        canvas.restoreState()
    
    # Build PDF with footer
    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    
    # Get PDF value
    pdf = buffer.getvalue()
    buffer.close()
    
    return pdf


@csrf_exempt
def send_purchaseorder_message(request, pk):
    """Send Email for a SalesOrder.

    Behavior mirrors `send_message` for quotations: prefers an EmailTemplateStyle
    (using names like 'Order Email' / 'Sales Order') and associated
    EmailConfiguration. Falls back to rendering 'sales/order_print.html' and
    using the generic `email_config.utils.send_email` helper.

    Expects POST and query param `method=email`.
    """
    try:
        print("inside try")
        if request.method != 'POST':
            print("Invalid request method")
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        if any(m not in ("email",) for m in methods):
            return JsonResponse({"success": False, "message": "Invalid method"}, status=400)

        order = PurchaseOrder.objects.select_related('vendor').filter(pk=pk).first()
        if not order:
            print("we are here ")
            return JsonResponse({"success": False, "message": "Order not found"}, status=404)

        context = build_porder_context(pk)

        # company info (same approach as quotation)
        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        placeholder_ctx = {
            'vendor': f"{getattr(order.vendor, 'first_name', '')} {getattr(order.vendor, 'last_name', '')}".strip(),
            'order_number': order.order_number,
            'date': order.date.strftime("%d %B %Y") if getattr(order, 'date', None) else '',
            'total': str(order.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
        }
        # aliases for templates that use different placeholder names
        placeholder_ctx['employee'] = placeholder_ctx['vendor']
        placeholder_ctx['amount'] = placeholder_ctx['total']

        messages_sent = []

        if 'email' in methods:
            template_style = None
            subject = None
            message_html = None
            try_names = ["Order Details", "Order Email", "Sales Order", "Order"]
            from email_templates.models import EmailTemplateStyle
            for tn in try_names:
                # First try to find a default one by template_name
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    is_default=True,
                    status=True
                ).first()
                if template_style:
                    break
                # If no default, get any active one
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    status=True
                ).first()
                if template_style:
                    break

            if template_style:
                subject = replace_placeholders(template_style.subject or f"Order {order.order_number}", placeholder_ctx)
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)

                # sanitize template HTML: strip inline color/background and font tags that cause dark backgrounds
                try:
                    message_html = re.sub(r'style="[^"]*(?:background(?:-color)?|color)[^"]*"', '', message_html, flags=re.I)
                    message_html = re.sub(r'<font[^>]*>', '', message_html, flags=re.I)
                    message_html = re.sub(r'</font>', '', message_html, flags=re.I)
                except Exception:
                    pass

                # Do not inline the full order HTML into the template message.
                # We'll attach the order as a PDF (preferred) or HTML file instead.

                email_config = EmailConfiguration.objects.filter(
                    usage_types__icontains=template_style.template_name,
                    status=True
                ).first()

                if not email_config:
                    # Fallback to default email configuration
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                
                if not email_config:
                    return JsonResponse({"success": False, "message": f"No email configuration found for Order, Go to settings and add email configuration."}, status=400)

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    recipient = order.shipping_email or (getattr(order.vendor, 'email', None) if order.vendor else None)
                    if not recipient:
                        return JsonResponse({"success": False, "message": "vendor email not found."}, status=400)

                    # Generate PDF using ReportLab
                    pdf_bytes = generate_porder_pdf_bytes(pk)

                    from_email = email_config.default_from_email or email_config.host_user
                    msg = EmailMultiAlternatives(
                        subject,
                        f"Please find attached order {order.order_number}.",
                        from_email,
                        [recipient],
                        connection=connection
                    )

                    # attach template body if present
                    try:
                        if 'message_html' in locals() and message_html:
                            msg.attach_alternative(message_html, 'text/html')
                    except Exception:
                        pass

                    if pdf_bytes:
                        msg.attach(f'order_{order.pk}.pdf', pdf_bytes, 'application/pdf')

                    msg.send(fail_silently=False)

                    # mark order as Sent
                    if order.status == 'Draft':
                        order.status = 'Issued'
                        order.save(update_fields=['status'])

                    messages_sent.append(f"Email sent to {recipient} successfully!")
                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
            else:
                # Try hardcoded default templates from email_templates if DB template not found
                from email_templates.views import get_default_email_template
                default_template = get_default_email_template("Order")
                if default_template:
                    subject = replace_placeholders(default_template.get("subject", f"Order {order.order_number}"), placeholder_ctx)
                    message_html = replace_placeholders(default_template.get("body", ""), placeholder_ctx)
                    attach_pdf_flag = default_template.get("attach_pdf", True)
                    try:
                        m = re.search(r"\[\[attach_pdf:(true|false)\]\]", message_html, flags=re.I)
                        if m:
                            attach_pdf_flag = m.group(1).lower() == 'true'
                            message_html = re.sub(r"\[\[attach_pdf:(?:true|false)\]\]", '', message_html, flags=re.I)
                        message_html = re.sub(r'<div\s+class\s*=\s*"attach-option"[\s\S]*?<\/div>', '', message_html, flags=re.I)
                    except Exception:
                        pass

                    # Determine email configuration: prefer one matching 'Order'
                    email_config = EmailConfiguration.objects.filter(usage_types__icontains="Order", status=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True).first()
                    if not email_config:
                        return JsonResponse({"success": False, "message": f"No email configuration found for Order, Go to settings and add email configuration."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        recipient = order.shipping_email or (getattr(order.vendor, 'email', None) if order.vendor else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "vendor email not found."}, status=400)

                        # Generate PDF using ReportLab
                        pdf_bytes = generate_porder_pdf_bytes(pk)

                        from_email = email_config.default_from_email or email_config.host_user
                        plain_body = f"Please find attached order {order.order_number}."
                        msg = EmailMultiAlternatives(
                            subject or f"Order {order.order_number}",
                            plain_body,
                            from_email,
                            [recipient],
                            connection=connection
                        )
                        try:
                            msg.attach_alternative(message_html, 'text/html')
                        except Exception:
                            pass

                        if pdf_bytes:
                            filename = f'order_{order.pk}.pdf'
                            msg.attach(filename, pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)

                        if order.status == 'Draft':
                            order.status = 'Issued'
                            order.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    # fallback: render print template
                    try:
                        full_bill_html = render_to_string('sales/order_print.html', context)
                        recipient = order.shipping_email or (getattr(order.vendor, 'email', None) if order.vendor else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Vendor email not found."}, status=400)

                        # Pick default active email configuration
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                        if not email_config:
                            email_config = EmailConfiguration.objects.filter(status=True).first()
                        if not email_config:
                            return JsonResponse({"success": False, "message": "No email configuration found for Order, Go to settings and add email configuration."}, status=400)

                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        pdf_bytes = None
                        try:
                            if HAVE_XHTML2PDF:
                                result_buf = io.BytesIO()
                                pisa_status = pisa.CreatePDF(io.StringIO(full_bill_html), dest=result_buf)
                                if not pisa_status.err:
                                    pdf_bytes = result_buf.getvalue()
                        except Exception:
                            pdf_bytes = None

                        from_email = email_config.default_from_email or email_config.host_user
                        msg = EmailMultiAlternatives(
                            f"Order {order.order_number}",
                            f"Please find attached order {order.order_number}.",
                            from_email,
                            [recipient],
                            connection=connection
                        )
                        if pdf_bytes:
                            msg.attach(f'order_{order.pk}.pdf', pdf_bytes, 'application/pdf')
                        else:
                            try:
                                msg.attach(f'order_{order.pk}.html', full_bill_html.encode('utf-8'), 'text/html')
                            except Exception:
                                pass

                        msg.send(fail_silently=False)
                        if order.status == 'Draft':
                            order.status = 'Issued'
                            order.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)

        if not messages_sent:
            messages_sent.append("No messages sent.")

        return JsonResponse({"success": True, "message": " | ".join(messages_sent)})

    except Exception as e:
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Internal server error: {str(e)}"}, status=500)



def purchaseorder_print_view(request, pk):
    """Render a printable HTML page for the sales order (for printing in browser)."""
    context = build_porder_context(pk)
    return render(request, 'Purchase/purchaseorder_print.html', context)


def build_porder_context(pk,request=None):
    order = get_object_or_404(PurchaseOrder, pk=pk)
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    existing_items_qs = PurchaseOrderItem.objects.filter(purchase_order=order)
    items_info = []
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_item_discount = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discount_amount = (base * discount_val) / Decimal('100')
        else:
            discount_amount = discount_val
        if discount_amount > base:
            discount_amount = base

        discounted = base - discount_amount
        if discounted < 0:
            discounted = Decimal('0.00')

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        line_total = discounted

        subtotal_calc += discounted
        total_tax += tax_amount
        total_item_discount += discount_amount

        # Split tax equally between CGST and SGST
        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        cgst_amount = tax_amount / 2
        sgst_amount = tax_amount / 2

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'base': base,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'vat_rate': tax_rate,
            'vat_amount': tax_amount,
            'taxable_value': discounted,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_vat = total_tax

    grand_discount_value = Decimal(order.discount_value or 0)
    grand_discount_type = order.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > subtotal_calc:
        grand_discount = subtotal_calc

    final_total = order.total_amount
    
    company_is_india = _is_indian_company_country(company.country.code if hasattr(company, 'country') and hasattr(company.country, 'code') else '')
    # Resolve document and base currencies to provide symbols for templates/PDFs
    try:
        from currencies.services import get_base_currency, resolve_currency_for_vendor
        document_currency = order.document_currency or resolve_currency_for_vendor(order.vendor, company)
        base_currency = get_base_currency(company)
    except Exception:
        document_currency = order.document_currency
        base_currency = None

    document_currency_symbol = (
        (getattr(document_currency, 'symbol', '') or getattr(document_currency, 'code', '')).strip()
        if document_currency else ''
    )
    document_currency_code = getattr(document_currency, 'code', '') if document_currency else ''
    company_base_currency_symbol = (
        (getattr(base_currency, 'symbol', '') or getattr(base_currency, 'code', '')).strip()
        if base_currency else '₹'
    )
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''
    company_tax_type = _get_purchase_company_tax_type(request)
    context = {
        'order': order,
        'o_no': order.order_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'total_tax': total_tax,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_vat': total_vat,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'total_discount_combined': total_item_discount + grand_discount,
        # Company details
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'company_is_india': company_is_india,
        'document_currency_symbol': document_currency_symbol,
        'document_currency_code': document_currency_code,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'company_tax_type': company_tax_type,
    }
    return context




def bill_pdf_view(request, pk):
    """Generate a PDF for the quotation using ReportLab with proper rupee symbol support."""
    bill = get_object_or_404(Bill, pk=pk)
    pdf = generate_bill_pdf_bytes(pk, request)
    
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="bill_{bill.bill_number}.pdf"'
    return response

def generate_bill_pdf_bytes(pk, request=None):
    """Generate PDF bytes for quotation using ReportLab - can be used for display or email."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    
    bill = get_object_or_404(Bill, pk=pk)
    context = build_bill_context(pk, request)
    
    # Register DejaVu Sans font (supports ₹ symbol)
    try:
        from django.conf import settings
        static_font_path = os.path.join(settings.BASE_DIR, 'static', 'fonts', 'DejaVuSans.ttf')
        
        font_registered = False
        
        # Try static folder first
        if os.path.exists(static_font_path):
            pdfmetrics.registerFont(TTFont('DejaVuSans', static_font_path))
            font_registered = True
        else:
            # Fallback to system fonts
            font_paths = [
                'C:/Windows/Fonts/DejaVuSans.ttf',  # Windows
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',  # Linux
                '/System/Library/Fonts/Supplemental/DejaVuSans.ttf',  # macOS
            ]
            
            for font_path in font_paths:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))
                    font_registered = True
                    break
    except:
        font_registered = False
    
    font_name = 'DejaVuSans' if font_registered else 'Helvetica'
    
    # Create PDF buffer and document
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                          rightMargin=10*mm, leftMargin=10*mm,
                          topMargin=10*mm, bottomMargin=10*mm,
                          title=f'Bill {pk}',
                          author='LyraERP')
    
    # Container for PDF elements
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom styles with smaller sizes for single page fit
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=0,
        fontName='Helvetica-Bold',
        alignment=TA_LEFT
    )
    
    bill_badge_style = ParagraphStyle(
        'Badge',
        parent=styles['Normal'],
        fontSize=16,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT
    )
    
    company_header_style = ParagraphStyle(
        'CompanyHeader',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#555555'),
        fontName=font_name,
        leading=10
    )
    
    meta_label_style = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#7f8c8d'),
        fontName='Helvetica-Bold'
    )
    
    meta_value_style = ParagraphStyle(
        'MetaValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name
    )
    
    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#ffffff'),
        fontName='Helvetica-Bold',
        alignment=TA_LEFT
    )
    
    label_style = ParagraphStyle(
        'Label',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#ffffff'),
        fontName='Helvetica-Bold'
    )
    
    value_style = ParagraphStyle(
        'Value',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name
    )
    
    amount_style = ParagraphStyle(
        'Amount',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    company = Company.objects.filter(status=True).first() or Company.objects.first()
    # HEADER with professional styling
    legal_name = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name_value = legal_name or ((getattr(company, 'name', '') or '').strip() if company else '')
    company_name = safe(company_name_value)
    header_left_cell = Paragraph(f"<b>{company_name}</b>", title_style)
    show_logo_in_print_pdf = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
    try:
        if show_logo_in_print_pdf and company and getattr(company, 'logo', None) and company.logo.path and os.path.exists(company.logo.path):
            header_left_cell = Image(company.logo.path, width=45*mm, height=14*mm)
    except Exception:
        pass

    
    header_data = [
        [header_left_cell, Paragraph("Bill", bill_badge_style)],
    ]

    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    # COMPANY INFO - Professional styling
    company_header_style.leftIndent = 10 
    
    company_info = f"""
    <font color='#2c3e50'><b>{company_name}</b></font><br/>
    <font size='6' color='#555555'>
    {safe(company.address_line1)}<br/>
    {safe(company.address_line2)}<br/>
    {safe(company.city)}, {safe(company.state)}, {safe(company.country)}, {safe(company.postal_code)}<br/>
    GSTIN: {safe(company.tax_id)}<br/>
    {safe(company.email)}
    </font>
    """
    elements.append(Paragraph(company_info, company_header_style))
    elements.append(Spacer(1, 6*mm))
    
    # META INFO - Professional styling
    payment_term_display = "-"
    if getattr(bill, 'payment_term', None):
        payment_term_display = getattr(bill.payment_term, 'name', None) or str(bill.payment_term)
    else:
        payment_term_id = getattr(bill, 'payment_term_id', None)
        if payment_term_id:
            payment_status_name = PaymentStatus.objects.filter(pk=payment_term_id).values_list('name', flat=True).first()
            if payment_status_name:
                payment_term_display = payment_status_name

    meta_data = [
        [Paragraph("<b>Bill No:</b>", meta_label_style), Paragraph(str(context.get('b_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(bill.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply:</b>", meta_label_style), Paragraph(str(bill.place_of_supply or '-'), meta_value_style),
        Paragraph("<b>Payment Terms:</b>", meta_label_style), Paragraph(str(payment_term_display), meta_value_style)],
    ]

    meta_table = Table(meta_data, colWidths=[141, 125, 125, 125])
    meta_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f8f9fa')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 5*mm))
    
    # ADDRESSES - Professional styling
    bill_to = "<b color='#2c3e50' size='15'>Bill To:</b><br/>"
    if bill.vendor:
        bill_lines = []
        customer_type = (getattr(bill.vendor, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(bill.vendor, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(bill.vendor, 'first_name', '') or '').strip()
            last_name = (getattr(bill.vendor, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)


        if getattr(bill.vendor, 'address_line_1', None):
            bill_lines.append(bill.vendor.address_line_1)

        if getattr(bill.vendor, 'state', None):
            bill_lines.append(str(bill.vendor.state))

        if getattr(bill.vendor, 'country', None):
            bill_lines.append(str(bill.vendor.country))

        gst = getattr(bill.vendor, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b color='#2c3e50' size='10'>Ship To:</b><br/>"
    if context.get('bill'):
        bill_obj = context['bill']
        if bill.shipping_attention:
            ship_lines = []

            if getattr(bill_obj, 'shipping_attention', None):
                ship_lines.append(bill_obj.shipping_attention)

            if getattr(bill_obj, 'shipping_address1', None):
                ship_lines.append(bill_obj.shipping_address1)

            if ship_lines:
                ship_to += "<font size='7' color='#2c3e50'>"
                ship_to += "<br/>".join(ship_lines)
                ship_to += "</font>"
        else:
            ship_to += "-"
    
    addr_data = [
        [Paragraph(bill_to, value_style), Paragraph(ship_to, value_style)]
    ]
    addr_table = Table(addr_data, colWidths=[260, 260])
    addr_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
    ]))
    elements.append(addr_table)
    elements.append(Spacer(1, 6*mm))
    
    # ITEMS TABLE - Professional styling
    # Prefer document currency symbol from context, fall back to company base symbol, then to rupee
    document_symbol = context.get('document_currency_symbol')
    company_symbol = context.get('company_base_currency_symbol')
    currency = (document_symbol or company_symbol) if (document_symbol or company_symbol) else ("₹" if font_registered else "Rs.")
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', 'GST')
    print(f"Company tax type for bill: {company_tax_type}")
    if company_tax_type == 'GST':
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style),
            Paragraph("<b>CGST</b>", label_style),
            Paragraph("<b>SGST</b>", label_style)]
        ]
    elif company_tax_type in ('VAT', 'SALES'):
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style),
            Paragraph("<b>Tax Rate</b>", label_style)]
        ]
    elif company_tax_type in ('TURNOVER', 'NONE'):
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
            Paragraph("<b>Item & Description</b>", label_style),
            Paragraph("<b>HSN/SAC</b>", label_style),
            Paragraph("<b>Qty</b>", label_style),
            Paragraph("<b>Rate</b>", label_style)]
        ]
    
    for idx, item in enumerate(context.get('items_info', []), 1):
        tax_rate = Decimal(str(item.get('tax_rate', 0)))
        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        cgst_amount = tax_amount / 2
        sgst_amount = tax_amount / 2
        
        if company_tax_type == 'GST':
            # Split tax equally for India (CGST/SGST)
            cgst_rate = tax_rate / 2
            sgst_rate = tax_rate / 2
            cgst_amount = tax_amount / 2
            sgst_amount = tax_amount / 2
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                Paragraph(f"{cgst_rate:.2f}%<br/>{currency}\u00A0{float(cgst_amount):.2f}", amount_style),
                Paragraph(f"{sgst_rate:.2f}%<br/>{currency}\u00A0{float(sgst_amount):.2f}", amount_style),
            ])
        elif company_tax_type in ('VAT', 'SALES'):
            # Show single VAT column for non-India
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                Paragraph(f"{tax_rate:.2f}%", amount_style),
               
            ])
        elif company_tax_type in ('TURNOVER', 'NONE'):
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                
               
            ])
    
    items_table = Table(items_data, colWidths=[25, 135, 59, 30, 90, 90, 90])
    items_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#ffffff')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 1), (7, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8f9fa')]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 5*mm))
    
    # TOTALS - Professional styling
    # Calculate correct subtotal (without tax) for PDF display
    pdf_subtotal = Decimal('0.00')
    for item in context.get('items_info', []):
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) 
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    if company_tax_type in ('GST','VAT', 'SALES','TURNOVER'):
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
        ]
    elif company_tax_type == 'NONE':
         totals_data = []
    company_is_india = context.get('company_is_india', True)
    if company_tax_type == 'GST':
        totals_data.append([Paragraph("CGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_cgst', 0):.2f}", amount_style)])
        totals_data.append([Paragraph("SGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_sgst', 0):.2f}", amount_style)])
    elif company_tax_type in ('VAT', 'SALES', 'TURNOVER'):
        totals_data.append([Paragraph("Tax", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_vat', 0):.2f}", amount_style)])
    elif company_tax_type == 'NONE':
        pass
    # Add round off if it exists
    bill_obj = context.get('bill')
    round_off_value = getattr(bill_obj, 'round_off', None) if bill_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}", amount_style)])
    
    grand_total_style = ParagraphStyle(
        'GrandTotal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2c3e50'),
        fontName='DejaVuSans',
        alignment=TA_RIGHT
    )
    
    totals_data.append([Paragraph("<b>Grand Total</b>", ParagraphStyle('GrandTotalLabel', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#2c3e50'))), 
                       Paragraph(f"<b>{currency}\u00A0{context.get('final_total', 0):.2f}</b>", grand_total_style)])
    
    totals_table = Table(totals_data, colWidths=[371, 150])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-2, -1), 7),
        ('FONTSIZE', (-2, -1), (-1, -1), 8),
        ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('BACKGROUND', (0, 0), (-1, -2), colors.HexColor('#ffffff')),
        ('BACKGROUND', (-2, -1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('LINEWIDTH', (0, 0), (-1, -1), 0.5),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 5*mm))
    
    # NOTES & SIGNATURE
    bill_obj = context.get('bill')
    notes_text = (getattr(bill_obj, 'notes', None) or '').strip() or '-'
    notes_text = safe(notes_text).replace('\n', '<br/>')
    terms_text = (getattr(company, 'terms_and_conditions', None) or '').strip() or '-'
    terms_text = safe(terms_text).replace('\n', '<br/>')
    bottom_data = [
        [Paragraph(
            f"<b>Notes</b><br/><font size=6>{notes_text}</font><br/><br/>"
            f"<b>Terms & Conditions</b><br/><font size=6>{terms_text}</font>",
            value_style
        ),
         Paragraph(f"<b>Authorized Signatory</b><br/><br/>For {company_name}", value_style)]
    ]

    bottom_table = Table(bottom_data, colWidths=[330, 190])  # ADJUST: [Notes Width, Signature Width]
    bottom_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('LINEWIDTH', (0, 0), (-1, -1), 1),
    ]))
    elements.append(bottom_table)
    
    # FOOTER - Use Canvas for page numbering
    from reportlab.pdfgen.canvas import Canvas
    
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, "POWERED BY LyraERP")
        page_num = canvas.getPageNumber()
        canvas.drawRightString(570, 20, f"Page {page_num} of {doc.page if hasattr(doc, 'page') else page_num}")
        canvas.restoreState()
    
    # Build PDF with footer
    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    
    # Get PDF value
    pdf = buffer.getvalue()
    buffer.close()
    
    return pdf


def build_bill_context(pk, request=None):
    bill = get_object_or_404(Bill, pk=pk)
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    existing_items_qs = BillItem.objects.filter(bill=bill)
    items_info = []
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_item_discount = Decimal('0.00')
    company_tax_type = _get_purchase_company_tax_type(request)  # request now available
    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)
        if item.prd_distype == 'percent':
            discount_amount = (base * discount_val) / Decimal('100')
        else:
            discount_amount = discount_val
        if discount_amount > base:
            discount_amount = base

        discounted = base - discount_amount
        if discounted < 0:
            discounted = Decimal('0.00')

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        line_total = discounted

        subtotal_calc += discounted
        total_tax += tax_amount
        total_item_discount += discount_amount

        # Split tax equally between CGST and SGST
        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        cgst_amount = tax_amount / 2
        sgst_amount = tax_amount / 2

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'base': base,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'vat_rate': tax_rate,
            'vat_amount': tax_amount,
            'taxable_value': discounted,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')

    grand_discount_value = Decimal(str(bill.discount_value or 0))
    grand_discount_type = bill.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > subtotal_calc:
        grand_discount = subtotal_calc

    final_total = subtotal_calc - grand_discount + total_tax
    
    company_is_india = _is_indian_company_country(company.country.code if hasattr(company, 'country') and hasattr(company.country, 'code') else '')

    # Resolve document and base currencies to provide symbols for templates/PDFs
    document_currency = None
    base_currency = None
    try:
        from currencies.services import get_base_currency, resolve_currency_for_vendor
        try:
            base_currency = get_base_currency(company)
        except Exception:
            base_currency = None
        try:
            document_currency = bill.document_currency or resolve_currency_for_vendor(bill.vendor, company)
        except Exception:
            document_currency = getattr(bill, 'document_currency', None) or base_currency
    except Exception:
        document_currency = getattr(bill, 'document_currency', None)
        base_currency = None

    # Prefer symbol, then ISO code, else empty
    def _symbol_or_code(cur):
        if not cur:
            return ''
        s = (getattr(cur, 'symbol', '') or '').strip()
        if s:
            return s
        c = (getattr(cur, 'code', '') or '').strip()
        return c

    document_currency_symbol = _symbol_or_code(document_currency)
    document_currency_code = (getattr(document_currency, 'code', '') or '').strip().upper() if document_currency else ''
    company_base_currency_symbol = _symbol_or_code(base_currency) or '₹'
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''

    # Fallback: map common currency codes to glyphs when symbol is not set
    CODE_TO_SYMBOL = {
        'USD': '$',
        'EUR': '€',
        'GBP': '£',
        'AUD': '$',
        'CAD': '$',
        'JPY': '¥',
        'INR': '₹',
    }

    if not document_currency_symbol:
        # try document code, then vendor.currency string
        code = document_currency_code or (getattr(bill, 'vendor', None) and getattr(bill.vendor, 'currency', '') or '')
        code = (code or '').strip().upper()
        if code and code in CODE_TO_SYMBOL:
            document_currency_symbol = CODE_TO_SYMBOL[code]
        else:
            # use ISO code as visible fallback if present
            document_currency_symbol = code or ''

    if not company_base_currency_symbol:
        base_code = (getattr(base_currency, 'code', '') or '').strip().upper() if base_currency else ''
        company_base_currency_symbol = CODE_TO_SYMBOL.get(base_code, base_code) or '₹'

    context = {
        'b_no': bill.bill_number,
        'bill': bill,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'total_tax': total_tax,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_vat': total_tax,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'total_discount_combined': total_item_discount + grand_discount,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type, 
        'document_currency_symbol': document_currency_symbol,
        'company_base_currency_symbol': company_base_currency_symbol,
        'document_currency_code': document_currency_code,
        'document_currency': document_currency,
        'base_currency': base_currency,
    }
    try:
        logger.info('build_bill_context: bill=%s, document_currency=%s, document_currency_symbol=%s, company_base_currency_symbol=%s',
                    getattr(bill, 'pk', None),
                    getattr(document_currency, 'code', str(document_currency) if document_currency else ''),
                    document_currency_symbol,
                    company_base_currency_symbol)
    except Exception:
        pass
    return context


def bill_print_view(request, pk):
    """Render a printable HTML page for the sales order (for printing in browser)."""
    context = build_bill_context(pk)
    return render(request, 'Purchase/bill_print.html', context)

@csrf_exempt
def send_bill_message(request, pk):
    """Send Email for a Bill.

    Behavior mirrors `send_message` for quotations: prefers an EmailTemplateStyle
    (using names like 'Bill Email' / 'Sales Bill') and associated
    EmailConfiguration. Falls back to rendering 'sales/bill_print.html' and
    using the generic `email_config.utils.send_email` helper.

    Expects POST and query param `method=email`.
    """
    try:
        print("inside try")
        if request.method != 'POST':
            print("Invalid request method")
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        if any(m not in ("email",) for m in methods):
            return JsonResponse({"success": False, "message": "Invalid method"}, status=400)

        bill = Bill.objects.select_related('vendor').filter(pk=pk).first()
        if not bill:
            print("we are here ")
            return JsonResponse({"success": False, "message": "Bill not found"}, status=404)

        context = build_bill_context(pk)

        # company info (same approach as quotation)
        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        placeholder_ctx = {
            'vendor': f"{getattr(bill.vendor, 'first_name', '')} {getattr(bill.vendor, 'last_name', '')}".strip(),
            'bill_number': bill.bill_number,
            'date': bill.date.strftime("%d %B %Y") if getattr(bill, 'date', None) else '',
            'total': str(bill.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
        }
        # aliases for templates that use different placeholder names
        placeholder_ctx['employee'] = placeholder_ctx['vendor']
        placeholder_ctx['amount'] = placeholder_ctx['total']
        # include currency symbols for templates
        placeholder_ctx['document_currency_symbol'] = context.get('document_currency_symbol', '')
        placeholder_ctx['company_base_currency_symbol'] = context.get('company_base_currency_symbol', '')
        placeholder_ctx['document_currency_code'] = context.get('document_currency_code', '')

        messages_sent = []

        if 'email' in methods:
            template_style = None
            subject = None
            message_html = None
            try_names = ["Bill Details", "Bill Email", "Bill", "Bill"]
            from email_templates.models import EmailTemplateStyle
            for tn in try_names:
                # First try to find a default one by template_name
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    is_default=True,
                    status=True
                ).first()
                if template_style:
                    break
                # If no default, get any active one
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=tn,
                    status=True
                ).first()
                if template_style:
                    break

            if template_style:
                subject = replace_placeholders(template_style.subject or f"Bill {bill.bill_number}", placeholder_ctx)
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)

                # sanitize template HTML: strip inline color/background and font tags that cause dark backgrounds
                try:
                    message_html = re.sub(r'style="[^"]*(?:background(?:-color)?|color)[^"]*"', '', message_html, flags=re.I)
                    message_html = re.sub(r'<font[^>]*>', '', message_html, flags=re.I)
                    message_html = re.sub(r'</font>', '', message_html, flags=re.I)
                except Exception:
                    pass

                # Do not inline the full bill HTML into the template message.
                # We'll attach the bill as a PDF (preferred) or HTML file instead.

                email_config = EmailConfiguration.objects.filter(
                    usage_types__icontains=template_style.template_name,
                    status=True
                ).first()

                if not email_config:
                    # Fallback to default email configuration
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                
                if not email_config:
                    return JsonResponse({"success": False, "message": f"No email configuration found for Bill, Go to settings and add email configuration."}, status=400)

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    recipient = bill.shipping_email or (getattr(bill.vendor, 'email', None) if bill.vendor else None)
                    if not recipient:
                        return JsonResponse({"success": False, "message": "vendor email not found."}, status=400)

                    # Generate PDF using ReportLab
                    pdf_bytes = generate_bill_pdf_bytes(pk)

                    from_email = email_config.default_from_email or email_config.host_user
                    msg = EmailMultiAlternatives(
                        subject,
                        f"Please find attached bill {bill.bill_number}.",
                        from_email,
                        [recipient],
                        connection=connection
                    )

                    # attach template body if present
                    try:
                        if 'message_html' in locals() and message_html:
                            msg.attach_alternative(message_html, 'text/html')
                    except Exception:
                        pass

                    if pdf_bytes:
                        msg.attach(f'bill_{bill.pk}.pdf', pdf_bytes, 'application/pdf')

                    msg.send(fail_silently=False)

                    # mark bill as Sent
                    bill.status = 'Sent'
                    bill.save(update_fields=['status'])

                    messages_sent.append(f"Email sent to {recipient} successfully!")
                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
            else:
                # Try hardcoded default templates from email_templates if DB template not found
                from email_templates.views import get_default_email_template
                default_template = get_default_email_template("Bill")
                if default_template:
                    subject = replace_placeholders(default_template.get("subject", f"Bill {bill.bill_number}"), placeholder_ctx)
                    message_html = replace_placeholders(default_template.get("body", ""), placeholder_ctx)
                    attach_pdf_flag = default_template.get("attach_pdf", True)
                    try:
                        m = re.search(r"\[\[attach_pdf:(true|false)\]\]", message_html, flags=re.I)
                        if m:
                            attach_pdf_flag = m.group(1).lower() == 'true'
                            message_html = re.sub(r"\[\[attach_pdf:(?:true|false)\]\]", '', message_html, flags=re.I)
                        message_html = re.sub(r'<div\s+class\s*=\s*"attach-option"[\s\S]*?<\/div>', '', message_html, flags=re.I)
                    except Exception:
                        pass

                    # Determine email configuration: prefer one matching 'Bill'
                    email_config = EmailConfiguration.objects.filter(usage_types__icontains="Bill", status=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True).first()
                    if not email_config:
                        return JsonResponse({"success": False, "message": f"No email configuration found for Bill, Go to settings and add email configuration."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        recipient = bill.shipping_email or (getattr(bill.vendor, 'email', None) if bill.vendor else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "vendor email not found."}, status=400)

                        # Generate PDF using ReportLab
                        pdf_bytes = generate_bill_pdf_bytes(pk)

                        from_email = email_config.default_from_email or email_config.host_user
                        plain_body = f"Please find attached bill {bill.bill_number}."
                        msg = EmailMultiAlternatives(
                            subject or f"Bill {bill.bill_number}",
                            plain_body,
                            from_email,
                            [recipient],
                            connection=connection
                        )
                        try:
                            msg.attach_alternative(message_html, 'text/html')
                        except Exception:
                            pass

                        if pdf_bytes:
                            filename = f'bill_{bill.pk}.pdf'
                            msg.attach(filename, pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)

                        bill.status = 'Sent'
                        bill.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    # fallback: render print template
                    try:
                        full_bill_html = render_to_string('Purchase/bill_print.html', context)
                        recipient = bill.shipping_email or (getattr(bill.vendor, 'email', None) if bill.vendor else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Vendor email not found."}, status=400)

                        # Pick default active email configuration
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                        if not email_config:
                            email_config = EmailConfiguration.objects.filter(status=True).first()
                        if not email_config:
                            return JsonResponse({"success": False, "message": "No email configuration found for Bill, Go to settings and add email configuration."}, status=400)

                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        pdf_bytes = None
                        try:
                            if HAVE_XHTML2PDF:
                                result_buf = io.BytesIO()
                                pisa_status = pisa.CreatePDF(io.StringIO(full_bill_html), dest=result_buf)
                                if not pisa_status.err:
                                    pdf_bytes = result_buf.getvalue()
                        except Exception:
                            pdf_bytes = None

                        from_email = email_config.default_from_email or email_config.host_user
                        msg = EmailMultiAlternatives(
                            f"Bill {bill.bill_number}",
                            f"Please find attached bill {bill.bill_number}.",
                            from_email,
                            [recipient],
                            connection=connection
                        )
                        if pdf_bytes:
                            msg.attach(f'bill_{bill.pk}.pdf', pdf_bytes, 'application/pdf')
                        else:
                            try:
                                msg.attach(f'bill_{bill.pk}.html', full_bill_html.encode('utf-8'), 'text/html')
                            except Exception:
                                pass

                        msg.send(fail_silently=False)
                        bill.status = 'Sent'
                        bill.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)

        if not messages_sent:
            messages_sent.append("No messages sent.")

        return JsonResponse({"success": True, "message": " | ".join(messages_sent)})

    except Exception as e:
        traceback.print_exc()
        return JsonResponse({"success": False, "message": f"Internal server error: {str(e)}"}, status=500)


# fns added by sree on 27-01-26 for delivery module

class DeliveryNoteViewSet(viewsets.ModelViewSet):
    queryset = DeliveryNote.objects.all()
    serializer_class = DeliveryNoteSerializer
    
    def get_queryset(self):
        queryset = DeliveryNote.objects.all()
        bill_id = self.request.query_params.get('bill', None)
        status_filter = self.request.query_params.get('status', None)
        stock_updated = self.request.query_params.get('stock_updated', None)
        
        if bill_id:
            queryset = queryset.filter(bill_id=bill_id)
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if stock_updated is not None:
            queryset = queryset.filter(stock_updated=stock_updated.lower() == 'true')
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def mark_delivered(self, request, pk=None):
        """
        Mark delivery note as delivered and update stock
        """
        delivery_note = self.get_object()
        
        if delivery_note.status == 'delivered':
            return Response(
                {'error': 'Delivery note is already marked as delivered'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            with transaction.atomic():
                delivery_note.status = 'delivered'
                delivery_note.save()
                if is_stock_management_on_delivery(request=request):
                    delivery_note.update_stock(user=request.user)
            
            serializer = self.get_serializer(delivery_note)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['post'])
    def cancel_delivery(self, request, pk=None):
        """
        Cancel delivery note and reverse stock
        """
        delivery_note = self.get_object()
        
        if delivery_note.status == 'cancelled':
            return Response(
                {'error': 'Delivery note is already cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            with transaction.atomic():
                delivery_note.status = 'cancelled'
                delivery_note.save()
                if delivery_note.stock_updated:
                    delivery_note.reverse_stock(user=request.user)
            
            serializer = self.get_serializer(delivery_note)
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=False, methods=['get'])
    def pending_deliveries(self, request):
        """
        Get all pending deliveries
        """
        pending = DeliveryNote.objects.filter(status='pending')
        serializer = self.get_serializer(pending, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stock_impact(self, request, pk=None):
        """
        Get stock impact report for this delivery
        """
        delivery_note = self.get_object()
        warehouse = delivery_note.warehouse
        
        impact = []
        for item in delivery_note.items.all():
            try:
                stock = Stock.objects.get(
                    item=item.bill_item.product,
                    warehouse=warehouse,
                    batch_number=None,
                    serial_number=None
                )
                current_stock = stock.quantity
            except Stock.DoesNotExist:
                current_stock = 0
            
            impact.append({
                'product': item.bill_item.product.name,
                'quantity_delivered': item.quantity_delivered,
                'current_stock': current_stock,
                'stock_after_delivery': current_stock + (
                    item.quantity_delivered if not delivery_note.stock_updated else 0
                )
            })
        
        return Response(impact)
    
class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only viewset for stock movements (audit trail)
    """
    queryset = StockMovement.objects.all()
    serializer_class = StockMovementSerializer
    
    def get_queryset(self):
        queryset = StockMovement.objects.all()
        stock_id = self.request.query_params.get('stock', None)
        movement_type = self.request.query_params.get('movement_type', None)
        delivery_note_id = self.request.query_params.get('delivery_note', None)
        
        if stock_id:
            queryset = queryset.filter(stock_id=stock_id)
        if movement_type:
            queryset = queryset.filter(movement_type=movement_type)
        if delivery_note_id:
            queryset = queryset.filter(delivery_note_id=delivery_note_id)
        
        return queryset
    

class DeliveryNoteListView(ListView):
    model = DeliveryNote
    template_name = 'Purchase/delivery_notes/delivery_note_list.html'
    context_object_name = 'delivery_notes'
    paginate_by = 20
    
    def dispatch(self, request, *args, **kwargs):
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_delivery(request.user)):
                messages.error(request, 'You do not have permission to view delivery notes.')
                return redirect_with_company('index')
        except Exception:
            messages.error(request, 'You do not have permission to view delivery notes.')
            return redirect_with_company('index')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        queryset = DeliveryNote.objects.select_related(
            'bill', 'bill__vendor', 'bill__warehouse'
        ).prefetch_related('items')
        
        # Search filter
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(delivery_note_number__icontains=search)
        
        # Bill filter
        bill = self.request.GET.get('bill')
        if bill:
            queryset = queryset.filter(bill__bill_number__icontains=bill)
        
        # Status filter
        status = self.request.GET.get('status')
        if status:
            queryset = queryset.filter(status=status)
        
        # Stock updated filter
        stock_updated = self.request.GET.get('stock_updated')
        if stock_updated == 'true':
            queryset = queryset.filter(stock_updated=True)
        elif stock_updated == 'false':
            queryset = queryset.filter(stock_updated=False)
        
        return queryset.order_by('-delivery_date', '-created_at')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Statistics
        all_deliveries = DeliveryNote.objects.all()
        context['total_count'] = all_deliveries.count()
        context['pending_count'] = all_deliveries.filter(status='pending').count()
        context['delivered_count'] = all_deliveries.filter(status='delivered').count()
        context['cancelled_count'] = all_deliveries.filter(status='cancelled').count()
        
        # Add permission context
        context['can_view_purchase_delivery'] = can_view_purchase_delivery(self.request.user)
        context['can_create_purchase_delivery'] = can_create_purchase_delivery(self.request.user)
        context['can_edit_purchase_delivery'] = can_edit_purchase_delivery(self.request.user)
        context['can_delete_purchase_delivery'] = can_delete_purchase_delivery(self.request.user)
        
        return context
    
class DeliveryNoteDetailView(DetailView):
    model = DeliveryNote
    template_name = 'Purchase/delivery_notes/delivery_note_detail.html'
    context_object_name = 'delivery_note'
    
    def dispatch(self, request, *args, **kwargs):
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_delivery(request.user)):
                messages.error(request, 'You do not have permission to view delivery notes.')
                return redirect_with_company('index')
        except Exception:
            messages.error(request, 'You do not have permission to view delivery notes.')
            return redirect_with_company('index')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        return DeliveryNote.objects.select_related(
            'bill', 'bill__vendor', 'bill__warehouse', 'bill__order_number'
        ).prefetch_related(
            'items__bill_item__product',
            'stock_movements__stock__item',
            'stock_movements__stock__warehouse'
        )
    

class DeliveryNoteCreateView(CreateView):
    model = DeliveryNote
    form_class = DeliveryNoteForm
    template_name = 'Purchase/delivery_notes/delivery_note_form.html'
    success_url = 'delivery_note_list'
    
    def dispatch(self, request, *args, **kwargs):
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_delivery(request.user)):
                messages.error(request, 'You do not have permission to create delivery notes.')
                return redirect_with_company('index')
        except Exception:
            messages.error(request, 'You do not have permission to create delivery notes.')
            return redirect_with_company('index')
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['formset'] = None
        
        # Check if period is locked for Purchase Delivery (for current date pre-fill)
        from system_settings.models import FiscalYear
        db = getattr(self.request, 'company_db', 'default')
        period_locked = False
        fiscal_year_name = ''
        
        try:
            fiscal_year = FiscalYear.objects.using(db).filter(
                start_date__lte=timezone.now().date(),
                end_date__gte=timezone.now().date(),
                status='active'
            ).first()
            
            if fiscal_year and fiscal_year.lock_purchase_delivery:
                period_locked = True
                fiscal_year_name = fiscal_year.name
        except Exception:
            pass
            
        context['period_locked'] = period_locked
        context['fiscal_year_name'] = fiscal_year_name
        return context
    
    def form_valid(self, form):
        # Check period lock before saving
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        delivery_date = form.cleaned_data.get('delivery_date')
        db = getattr(self.request, 'company_db', 'default')
        
        try:
            PeriodLockEnforcer.check_can_edit(delivery_date, self.request.user, db=db, transaction_type='purchase_delivery')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=delivery_date,
                    end_date__gte=delivery_date,
                    status='active'
                ).first()
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked: locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_delivery',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception: pass

            if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'message': str(e), 'period_lock_error': period_lock_error}, status=400)
            
            messages.error(self.request, str(e))
            return self.form_invalid(form)

        with transaction.atomic():
            # Save the delivery note
            self.object = form.save(commit=False)
            self.object.save()
            
            print(f"Delivery Note created: {self.object.delivery_note_number}, Status: {self.object.status}")
            
            # Process items from POST data
            items_created = self.save_delivery_items()
            
            if items_created == 0:
                messages.error(self.request, 'Please select at least one item to deliver.')
                self.object.delete()
                return self.form_invalid(form)
            
            print(f"Items created: {items_created}")
            
            # Update stock if status is delivered and delivery-based stock management is enabled
            if self.object.status == 'delivered' and is_stock_management_on_delivery(request=self.request):
                print(f"Calling update_stock() for {self.object.delivery_note_number}")
                self.object.update_stock(user=self.request.user)
                print(f"Stock updated: {self.object.stock_updated}")
            
            messages.success(
                self.request,
                f'Delivery Note {self.object.delivery_note_number} created successfully with {items_created} items!'
            )
            
            if self.object.stock_updated:
                messages.success(self.request, 'Stock has been updated in the warehouse.')
            
            return redirect_with_company(self.success_url)
    
    def save_delivery_items(self):
        """Process the custom item data from POST"""
        items_created = 0
        
        # Get all item indices from POST data
        item_indices = set()
        for key in self.request.POST.keys():
            if key.startswith('items[') and '][bill_item]' in key:
                index = key.split('[')[1].split(']')[0]
                item_indices.add(index)
        
        print(f"Found item indices: {item_indices}")
        
        # Process each item
        for index in item_indices:
            selected_key = f'items[{index}][selected]'
            bill_item_key = f'items[{index}][bill_item]'
            quantity_key = f'items[{index}][quantity_delivered]'
            
            # Check if item is selected
            if selected_key in self.request.POST:
                bill_item_id = self.request.POST.get(bill_item_key)
                quantity = self.request.POST.get(quantity_key)
                
                print(f"Processing item {index}: bill_item_id={bill_item_id}, quantity={quantity}")
                
                if bill_item_id and quantity:
                    try:
                        bill_item = BillItem.objects.get(id=bill_item_id)
                        delivery_item = DeliveryNoteItem.objects.create(
                            delivery_note=self.object,
                            bill_item=bill_item,
                            quantity_delivered=int(quantity)
                        )
                        print(f"Created DeliveryNoteItem: {delivery_item}")
                        items_created += 1
                    except (BillItem.DoesNotExist, ValueError) as e:
                        print(f"Error creating item: {str(e)}")
                        messages.warning(self.request, f'Error adding item: {str(e)}')
        
        return items_created
            

class DeliveryNoteUpdateView(UpdateView):
    model = DeliveryNote
    form_class = DeliveryNoteForm
    template_name = 'Purchase/delivery_notes/delivery_note_form.html'
    success_url = 'delivery_note_list'
    
    def dispatch(self, request, *args, **kwargs):
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_delivery(request.user)):
                messages.error(request, 'You do not have permission to edit delivery notes.')
                return redirect_with_company('index')
        except Exception:
            messages.error(request, 'You do not have permission to edit delivery notes.')
            return redirect_with_company('index')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        # Only allow editing if stock hasn't been updated
        return DeliveryNote.objects.filter(stock_updated=False)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['formset'] = None
        # Pass existing items to template
        context['existing_items'] = self.object.items.all()
        
        # Check if period is locked for Purchase Delivery (for current object's date)
        from system_settings.models import FiscalYear
        db = getattr(self.request, 'company_db', 'default')
        period_locked = False
        fiscal_year_name = ''
        
        try:
            fiscal_year = FiscalYear.objects.using(db).filter(
                start_date__lte=self.object.delivery_date,
                end_date__gte=self.object.delivery_date,
                status='active'
            ).first()
            
            if fiscal_year and fiscal_year.lock_purchase_delivery:
                period_locked = True
                fiscal_year_name = fiscal_year.name
        except Exception:
            pass
            
        context['period_locked'] = period_locked
        context['fiscal_year_name'] = fiscal_year_name
        return context
    
    def form_valid(self, form):
        # Check period lock before saving
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        delivery_date = form.cleaned_data.get('delivery_date')
        db = getattr(self.request, 'company_db', 'default')
        
        try:
            PeriodLockEnforcer.check_can_edit(delivery_date, self.request.user, db=db, transaction_type='purchase_delivery')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=delivery_date,
                    end_date__gte=delivery_date,
                    status='active'
                ).first()
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked: locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_delivery',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception: pass

            if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'message': str(e), 'period_lock_error': period_lock_error}, status=400)
            
            messages.error(self.request, str(e))
            return self.form_invalid(form)

        old_status = DeliveryNote.objects.get(pk=self.object.pk).status
        
        with transaction.atomic():
            self.object = form.save()
            
            # Delete existing items and create new ones
            self.object.items.all().delete()
            items_created = self.save_delivery_items()
            
            if items_created == 0:
                messages.error(self.request, 'Please select at least one item to deliver.')
                return self.form_invalid(form)
            
            # Update stock if status changed to delivered and delivery-based stock management is enabled
            if self.object.status == 'delivered' and old_status != 'delivered' and is_stock_management_on_delivery(request=self.request):
                self.object.update_stock(user=self.request.user)
            
            messages.success(
                self.request,
                f'Delivery Note {self.object.delivery_note_number} updated successfully!'
            )
            # return super().form_valid(form)
            return redirect_with_company(self.success_url)

    
    def save_delivery_items(self):
        """Process the custom item data from POST"""
        items_created = 0
        
        # Get all item indices from POST data
        item_indices = set()
        for key in self.request.POST.keys():
            if key.startswith('items[') and '][bill_item]' in key:
                index = key.split('[')[1].split(']')[0]
                item_indices.add(index)
        
        # Process each item
        for index in item_indices:
            selected_key = f'items[{index}][selected]'
            bill_item_key = f'items[{index}][bill_item]'
            quantity_key = f'items[{index}][quantity_delivered]'
            
            # Check if item is selected
            if selected_key in self.request.POST:
                bill_item_id = self.request.POST.get(bill_item_key)
                quantity = self.request.POST.get(quantity_key)
                
                if bill_item_id and quantity:
                    try:
                        bill_item = BillItem.objects.get(id=bill_item_id)
                        DeliveryNoteItem.objects.create(
                            delivery_note=self.object,
                            bill_item=bill_item,
                            quantity_delivered=int(quantity)
                        )
                        items_created += 1
                    except (BillItem.DoesNotExist, ValueError) as e:
                        messages.warning(self.request, f'Error adding item: {str(e)}')
        
        return items_created
            
def delivery_note_mark_delivered(request, pk):
    """Mark delivery note as delivered"""
    delivery_note = get_object_or_404(DeliveryNote, pk=pk)
    
    if request.method == 'POST':
        # Check period lock before marking delivered
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(delivery_note.delivery_date, request.user, db=db, transaction_type='purchase_delivery')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=delivery_note.delivery_date,
                    end_date__gte=delivery_note.delivery_date,
                    status='active'
                ).first()
                
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked:
                        locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_delivery',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            # Check if AJAX request
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': str(e),
                    'period_lock_error': period_lock_error
                }, status=400)
            
            messages.error(request, str(e))
            return redirect_with_company('delivery_note_detail', pk=pk)

        if delivery_note.status == 'delivered':
            messages.warning(request, 'This delivery is already marked as delivered.')
        else:
            try:
                with transaction.atomic():
                    delivery_note.status = 'delivered'
                    delivery_note.save()
                    if is_stock_management_on_delivery(request=request):
                        delivery_note.update_stock(user=request.user)
                
                messages.success(
                    request,
                    f'Delivery Note {delivery_note.delivery_note_number} marked as delivered and stock updated!'
                )
            except Exception as e:
                messages.error(request, f'Error updating delivery: {str(e)}')
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    
    return redirect_with_company('delivery_note_detail', pk=pk)


def delivery_note_cancel(request, pk):
    """Cancel delivery note"""
    delivery_note = get_object_or_404(DeliveryNote, pk=pk)
    
    if request.method == 'POST':
        # Check period lock before canceling
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(delivery_note.delivery_date, request.user, db=db, transaction_type='purchase_delivery')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=delivery_note.delivery_date,
                    end_date__gte=delivery_note.delivery_date,
                    status='active'
                ).first()
                
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked:
                        locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_delivery',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            # Check if AJAX request
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': str(e),
                    'period_lock_error': period_lock_error
                }, status=400)
            
            messages.error(request, str(e))
            return redirect_with_company('delivery_note_detail', pk=pk)

        if delivery_note.status == 'cancelled':
            messages.warning(request, 'This delivery is already cancelled.')
        else:
            try:
                with transaction.atomic():
                    delivery_note.status = 'cancelled'
                    delivery_note.save()
                    
                    # Reverse stock if it was updated
                    if delivery_note.stock_updated:
                        delivery_note.reverse_stock(user=request.user)
                
                messages.success(
                    request,
                    f'Delivery Note {delivery_note.delivery_note_number} cancelled successfully!'
                )
            except Exception as e:
                messages.error(request, f'Error cancelling delivery: {str(e)}')
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
        
    return redirect_with_company('delivery_note_detail', pk=pk)

# API views for AJAX calls
def get_bill_items(request, bill_id):
    """Get items for a specific bill with delivery status"""
    bill = get_object_or_404(Bill, pk=bill_id)
    items = []
    
    for bill_item in bill.items.all():
        # Calculate already delivered quantity
        delivered = DeliveryNoteItem.objects.filter(
            bill_item=bill_item,
            delivery_note__stock_updated=True
        ).aggregate(total=Sum('quantity_delivered'))['total'] or 0
        
        items.append({
            'id': bill_item.id,
            'product_name': bill_item.product.name,
            'hsn_code': bill_item.hsn_code,
            'quantity': bill_item.quantity,
            'delivered_quantity': delivered,
            'remaining_quantity': bill_item.quantity - delivered,
            'price': str(bill_item.price)
        })
    
    return JsonResponse(items, safe=False)


def get_bill_details(request, bill_id):
    """Get bill details"""
    bill = get_object_or_404(Bill, pk=bill_id)
    
    data = {
        'vendor_name': bill.vendor.first_name if bill.vendor else 'N/A',
        'warehouse_name': Warehouse.warehouse_name,
        'order_number': str(bill.order_number),
        'bill_number': bill.bill_number
    }
    
    return JsonResponse(data)


@transaction.atomic
def delete_payment_made(request, payment_id):
    print(f"Initiating deletion of payment ID: {payment_id} by user: {request.user.username}")
    """
    Delete a payment and reverse all associated transactions.
    
    This will:
    1. Reverse advance transactions (debit → credit, credit → debit)
    2. Delete bill allocations
    3. Update bill statuses
    4. Delete payment attachments
    5. Delete the payment record
    """
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect_with_company('payment_list')
    
    payment = get_object_or_404(BillPayment, pk=payment_id)

    # ✅ CHECK PERIOD LOCK BEFORE DELETING PAYMENT
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    try:
        PeriodLockEnforcer.check_can_edit(payment.payment_date, request.user, db=db, transaction_type='payment')
    except PermissionDenied as e:
        # Return JSON error for AJAX requests, render template for regular requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=400)
        else:
            return render(request, 'Purchase/payment_add.html', {
                'title': 'Delete Payment',
                'error_message': str(e),
                'show_error_modal': True,
            })
    
    
    try:
        # Store payment info for success message
        payment_number = payment.payment_number
        vendor = payment.vendor
        amount = payment.amount
        
        # Get all bills that were allocated from this payment
        allocated_bills = list(
            BillPaymentAllocation.objects.filter(payment=payment)
            .values_list('bill_id', flat=True)
        )
        
        # Step 1: Reverse advance transactions
        advance_transactions = VendorAdvancePayment.objects.filter(payment=payment)
        
        for txn in advance_transactions:
            # Create reverse entry
            VendorAdvancePayment.objects.create(
                vendor=vendor,
                amount=-txn.amount,  # Reverse the transaction
                payment=None  # Not linked to any payment (adjustment entry)
            )
        
        # Delete the original advance transactions
        advance_transactions.delete()
        
        # Step 2: Delete bill allocations
        BillPaymentAllocation.objects.filter(payment=payment).delete()
        
        # Step 3: Update bill statuses for affected bills
        if allocated_bills:
            bills = Bill.objects.filter(id__in=allocated_bills)
            for bill in bills:
                update_bill_status(bill)
        
        # Step 4: Delete attachments
        # Get all attachment file paths before deleting
        attachments = BillPaymentAttachment.objects.filter(payment=payment)
        for attachment in attachments:
            # Delete the actual file from storage
            if attachment.file:
                try:
                    attachment.file.delete(save=False)
                except Exception as e:
                    print(f"Error deleting file: {e}")
        
        # Delete attachment records
        attachments.delete()
        
        # Step 5: Delete associated journal entry
        journal_entry = JournalEntry.objects.filter(
            reference=f"Payment #{payment.payment_number}"
        ).first()
        
        if journal_entry:
            journal_entry.delete()
            print(f"Deleted journal entry for payment {payment.payment_number}")
        
        # Step 6: Delete the payment record
        payment.delete()
        
        success_msg = (
            f'Payment #{payment_number} for vendor {vendor} (₹{amount:.2f}) has been deleted successfully. '
            f'All advance transactions and bill allocations have been reversed.'
        )

        # Return JSON for AJAX requests, redirect for regular requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': success_msg
            })
        
        messages.success(request, success_msg)
        
        return redirect_with_company('payment_list')
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        error_msg = f'Error deleting payment: {str(e)}'
        
        # Return JSON for AJAX requests, redirect for regular requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': error_msg
            }, status=400)
        
        messages.error(request, error_msg)
        return redirect_with_company('payment_list')


@login_required
def generate_vendor_code(request):

    print(f"METHOD: {request.method}")          # ← add this
    print(f"User authenticated: {request.user.is_authenticated}")
    print(f"Session key: {request.session.session_key}")
    print(f"POST data: {request.POST}")   

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if not name:
            return JsonResponse({'success': False, 'code': '', 'message': 'Name is required'})
        
        # Generate code: first 3 letters uppercased + 3 random digits
        prefix = ''.join([c.upper() for c in name[:3] if c.isalpha()])  # e.g., "ABC"
        if len(prefix) < 3:
            prefix = prefix.ljust(3, 'X')[:3]
        
        # Check existing codes with this prefix and find next number
        base_codes = Vendor.objects.filter(
            vendor_code__startswith=prefix
        ).values_list('vendor_code', flat=True)
        
        numbers = []
        for code in base_codes:
            if len(code) >= 6 and code[:3] == prefix:
                try:
                    numbers.append(int(code[3:]))
                except ValueError:
                    pass
        
        next_num = 1
        if numbers:
            next_num = max(numbers) + 1
        
        code = f"{prefix}{next_num:03d}"  # e.g., "ABC001", "ABC999"
        
        # Ensure uniqueness (fallback random suffix if collision)
        while Vendor.objects.filter(vendor_code=code).exists():
            suffix = str(random.randint(100, 999))
            code = f"{prefix}{suffix}"
            next_num += 1
            if next_num > 999:
                break
        
        return JsonResponse({'success': True, 'code': code})
    
    return JsonResponse({'success': False, 'message': 'Invalid request'})


def get_vendor_preferred_items(request, vendor_id):
    items = Item.objects.filter(
        preferred_vendor_id=vendor_id,
        status=True
    ).select_related('intra_tax', 'main_barcode')
    
    result = []
    for item in items:
        unit_name = item.unit or ''
        if item.unit:
            try:
                unit_name = Unit.objects.get(id=item.unit).unit_name
            except (Unit.DoesNotExist, ValueError, TypeError):
                unit_name = item.unit or ''

        # Sum quantity across all warehouses for this item
        current_stock = Stock.objects.filter(
            item=item,
            status=True
        ).aggregate(total=Sum('quantity'))['total'] or 0        
        print("current stock of item",current_stock)

        # # Only include items with stock less than 10
        # if current_stock >= 6:
        #     continue

        # Use item-wise minimum stock level (configured in Item form)
        min_stock_level = item.min_stock
        if min_stock_level is None:
            continue

        # Include item only when current stock is below its own threshold
        if current_stock >= min_stock_level:
            continue


        tax_rate = float(item.total_tax_rate) if item.total_tax_rate else 0
        cost_price = float(item.cost_price) if item.cost_price else 0
        
        # Calculate tax-exclusive price if price is GST inclusive
        if item.taxincld_costprice and tax_rate > 0:
            o_price = round(cost_price / (1 + tax_rate / 100), 2)
        else:
            o_price = cost_price

        result.append({
            'id': str(item.id) + '_' + (item.barcode or ''),
            'name': item.name,
            'unit': unit_name,
            'price': o_price,  # base-currency, tax-exclusive price consumed by loadVendorPreferredItems()
            'cost_price': cost_price,
            'o_price': o_price,
            'current_stock': float(current_stock),  # send stock for tooltip

            'gstinclude': 'true' if item.taxincld_costprice else 'false',
            'tax_id': item.intra_tax.id if item.intra_tax else '',
            'tax_name': str(item.intra_tax) if item.intra_tax else '',
            'tax_rate': tax_rate,
            'tax_pref': item.tax_pref or 'taxable',
            'hsn_code': item.hsn_code or '',
            'description': item.purchase_desc or '',
        })
            
    return JsonResponse(result, safe=False)


def delivery_note_print(request, pk):
    """Print delivery note in professional format"""
    from company.models import Company
    delivery_note = get_object_or_404(DeliveryNote, pk=pk)
    
    # Fetch company settings
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    
    # Calculate totals
    total_line_items = delivery_note.items.count()
    total_units_delivered = sum(item.bill_item.quantity for item in delivery_note.items.all() if hasattr(item, 'bill_item'))
    
    return render(request, 'purchase/delivery_notes/delivery_note_print.html', {
        'delivery_note': delivery_note,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'total_line_items': total_line_items,
        'total_units_delivered': total_units_delivered,
    })


def _get_purchase_return_currency_snapshot(purchase_return=None, bill=None):
    bill_obj = bill or getattr(purchase_return, 'bill', None)
    doc_currency = getattr(purchase_return, 'document_currency', None) or getattr(bill_obj, 'document_currency', None)
    fx_rate = getattr(purchase_return, 'fx_rate_to_base', None)
    if fx_rate in (None, ''):
        fx_rate = getattr(bill_obj, 'fx_rate_to_base', Decimal('1.000000'))
    try:
        fx_rate = Decimal(str(fx_rate or '1.000000'))
    except Exception:
        fx_rate = Decimal('1.000000')
    if fx_rate <= Decimal('0'):
        fx_rate = Decimal('1.000000')
    fx_rate_date = getattr(purchase_return, 'fx_rate_date', None) or getattr(bill_obj, 'fx_rate_date', None)
    return doc_currency, fx_rate, fx_rate_date


def _recalculate_purchase_return_amounts(purchase_return, company_db='default'):
    doc_currency, fx_rate, fx_rate_date = _get_purchase_return_currency_snapshot(purchase_return=purchase_return)
    computed_refund = Decimal('0.00')
    for ritem in PurchaseReturnItem.objects.using(company_db).filter(purchase_return=purchase_return).select_related('bill_item'):
        try:
            price = Decimal(str(ritem.bill_item.price or 0))
            qty = Decimal(str(ritem.quantity_returned or 0))
            tax_rate = Decimal(str(getattr(ritem.bill_item, 'prd_tax', 0) or 0))
            line_amount = (price * qty) * (Decimal('1.00') + (tax_rate / Decimal('100.00')))
            computed_refund += line_amount
        except Exception:
            continue
    purchase_return.document_currency = doc_currency
    purchase_return.fx_rate_to_base = fx_rate
    purchase_return.fx_rate_date = fx_rate_date
    purchase_return.refund_amount = computed_refund.quantize(Decimal('0.01'))
    purchase_return.refund_amount_base = (purchase_return.refund_amount * fx_rate).quantize(Decimal('0.01'))
    return purchase_return


def _get_purchase_return_fixed_warehouse(bill, company_db='default'):
    fixed_warehouse = getattr(bill, 'warehouse', None)
    if fixed_warehouse is not None:
        return fixed_warehouse

    bill_item_product_ids = list(
        BillItem.objects.using(company_db)
        .filter(bill_id=bill.id)
        .values_list('product_id', flat=True)
    )
    if not bill_item_product_ids:
        return None

    stock_row = (
        Stock.objects.using(company_db)
        .filter(item_id__in=bill_item_product_ids, warehouse__isnull=False)
        .select_related('warehouse')
        .order_by('-id')
        .first()
    )
    return stock_row.warehouse if stock_row and stock_row.warehouse_id else None


def _get_purchase_bill_paid_total(bill):
    try:
        return bill.payment_allocations.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    except Exception:
        return Decimal('0.00')


def _get_returnable_bill_items(bill, company_db='default'):
    items = []
    for bill_item in BillItem.objects.using(company_db).filter(bill=bill).select_related('product'):
        returned_total = PurchaseReturnItem.objects.using(company_db).filter(
            bill_item=bill_item,
            purchase_return__status__in=['pending', 'received']
        ).aggregate(total=Sum('quantity_returned'))['total'] or 0

        try:
            ordered_qty = int(bill_item.quantity or 0)
            returned_qty = int(returned_total or 0)
        except Exception:
            ordered_qty = 0
            returned_qty = 0

        remaining_qty = max(ordered_qty - returned_qty, 0)
        if remaining_qty <= 0:
            continue

        items.append({
            'id': bill_item.id,
            'product_name': bill_item.product.name if bill_item.product else '',
            'hsn_code': bill_item.hsn_code or '',
            'quantity': ordered_qty,
            'already_returned': returned_qty,
            'remaining_quantity': remaining_qty,
            'price': str(bill_item.price or 0),
            'tax_rate': str(getattr(bill_item, 'prd_tax', 0) or 0),
        })
    return items


@login_required
def purchase_return_create(request):
    if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_returns(request.user)):
        messages.error(request, 'You do not have permission to create Purchase Returns.')
        return redirect_with_company('purchase_return_list')

    company_db = getattr(request, 'company_db', 'default')
    bills = Bill.objects.using(company_db).select_related('vendor').order_by('-date', '-id')
    return render(request, 'Purchase/return_create_form.html', {
        'bills': bills,
    })


@login_required
def purchase_return_bill_items(request, bill_id):
    if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_returns(request.user)):
        return JsonResponse({'success': False, 'message': 'Permission denied'}, status=403)

    company_db = getattr(request, 'company_db', 'default')
    bill = get_object_or_404(Bill.objects.using(company_db).select_related('vendor'), pk=bill_id)
    total_paid = _get_purchase_bill_paid_total(bill)
    fixed_warehouse = _get_purchase_return_fixed_warehouse(bill, company_db=company_db)
    document_currency = getattr(bill, 'document_currency', None)

    return JsonResponse({
        'success': True,
        'bill': {
            'id': bill.id,
            'bill_number': bill.bill_number,
            'vendor_name': str(bill.vendor or ''),
            'status': bill.status,
            'paid': str(total_paid),
            'can_return': total_paid > Decimal('0.00'),
            'warehouse_name': fixed_warehouse.warehouse_name if fixed_warehouse else '',
            'currency_symbol': (
                getattr(document_currency, 'symbol', '')
                or getattr(document_currency, 'code', '')
                or '₹'
            ),
        },
        'items': _get_returnable_bill_items(bill, company_db=company_db),
    })


def purchase_return_print(request, pk):
    from .models import PurchaseReturn, PurchaseReturnItem
    from company.models import Company
    from currencies.models import Currency
    
    # Permission: require View on Purchase Returns
    if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)):
        messages.error(request, 'You do not have permission to view Purchase Returns.')
        return redirect_with_company('bill_list')
    
    company_db = getattr(request, 'company_db', 'default')
    purchase_return = get_object_or_404(PurchaseReturn.objects.using(company_db), pk=pk)
    items = PurchaseReturnItem.objects.filter(purchase_return=purchase_return).select_related('bill_item__product')
    purchase_return_currency_symbol = purchase_return.get_currency_symbol()
    purchase_return_currency_code = (
        getattr(getattr(purchase_return, 'document_currency', None), 'code', '')
        or (purchase_return.bill.document_currency.code if getattr(purchase_return.bill, 'document_currency', None) else '')
        or ''
    )
    
    # Load company information from settings
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    company_base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''
    
    return render(request, 'purchase/return_print.html', {
        'purchase_return': purchase_return,
        'items': items,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'purchase_return_currency_symbol': purchase_return_currency_symbol,
        'purchase_return_currency_code': purchase_return_currency_code,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
    })



def purchase_return_view(request, pk):
    """Create a PurchaseReturn for the given bill. Handles GET (form) and POST (process).

    POST expects:
    - quantities for items as items[<bill_item_id>]=<qty>
    - warehouse (optional)
    - notes (optional)
    """
    from .models import PurchaseReturn, PurchaseReturnItem, BillPaymentAllocation

    company_db = getattr(request, 'company_db', 'default')
    bill = get_object_or_404(Bill.objects.using(company_db), pk=pk)
    fixed_warehouse = _get_purchase_return_fixed_warehouse(bill, company_db=company_db)
    total_paid = _get_purchase_bill_paid_total(bill)

    if request.method == 'GET':
        # Permission: require View on Purchase Returns
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)):
            messages.error(request, 'You do not have permission to view Purchase Returns.')
            return redirect_with_company('bill_list')
    
        if total_paid <= Decimal('0.00'):
            messages.error(request, 'Returns are allowed only after a payment has been made to this vendor for this bill.')
            return redirect_with_company(request, 'bill_detail', pk=bill.pk)

        # Check if period is locked for Purchase Return
        from system_settings.models import FiscalYear
        from django.utils import timezone
        period_locked = False
        fiscal_year_name = ''
        
        try:
            fiscal_year = FiscalYear.objects.using(company_db).filter(
                start_date__lte=timezone.now().date(),
                end_date__gte=timezone.now().date(),
                status='active'
            ).first()
            
            if fiscal_year and fiscal_year.lock_purchase_return:
                period_locked = True
                fiscal_year_name = fiscal_year.name
        except Exception:
            pass

        # Exclude bill items that are already fully returned
        items = []
        for bi in BillItem.objects.using(company_db).filter(bill=bill):
            returned_total = PurchaseReturnItem.objects.using(company_db).filter(bill_item=bi, purchase_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
            try:
                remaining = int(bi.quantity or 0) - int(returned_total or 0)
            except Exception:
                remaining = 0
            if remaining > 0:
                # set the displayed quantity to remaining so template shows correct prefilled qty
                bi.quantity = remaining
                items.append(bi)
        return render(request, 'Purchase/return_form.html', {
            'bill': bill, 
            'items': items, 
            'fixed_warehouse': fixed_warehouse,
            'total_paid': total_paid,
            'period_locked': period_locked,
            'fiscal_year_name': fiscal_year_name,
            'purchase_return_currency_symbol': (
                getattr(getattr(bill, 'document_currency', None), 'symbol', '')
                or getattr(getattr(bill, 'document_currency', None), 'code', '')
                or '₹'
            ),
        })

    # POST - Create
    # Permission: require Create on Purchase Returns
    if not (getattr(request.user, 'is_superuser', False) or can_create_purchase_returns(request.user)):
        return JsonResponse({'success': False, 'message': 'You do not have permission to create Purchase Return.'}, status=403)
    
    try:
        # Check period lock before processing the return
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        return_date = request.POST.get('date') or timezone.now().date()
        try:
            from datetime import datetime
            return_date = datetime.strptime(return_date, '%Y-%m-%d').date() if isinstance(return_date, str) else return_date
        except Exception:
            return_date = timezone.now().date()
        
        try:
            PeriodLockEnforcer.check_can_edit(return_date, request.user, db=company_db, transaction_type='purchase_return')
        except PermissionDenied as e:
            error_msg = str(e)
            try:
                _, fiscal_year, _ = PeriodLockEnforcer.is_period_locked(return_date, request.user, db=company_db, transaction_type='purchase_return')
                locked_by = fiscal_year.locked_by.username if fiscal_year and fiscal_year.locked_by else 'Admin'
                lock_date = fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year and fiscal_year.lock_date else 'N/A'
                locked_fields = []
                if fiscal_year and getattr(fiscal_year, 'is_locked', False):
                    locked_fields.append('all')
                if fiscal_year:
                    locked_fields.extend(getattr(fiscal_year, 'locked_categories', []) or [])
                locked_areas = ', '.join(sorted(set(locked_fields)))
                period_lock_error = {
                    'locked_by': locked_by,
                    'lock_date': lock_date,
                    'locked_areas': locked_areas,
                }
            except Exception:
                period_lock_error = None

            return JsonResponse({'success': False, 'message': error_msg, 'period_lock_error': period_lock_error}, status=403)

        quantities = {}
        for key, val in request.POST.items():
            if key.startswith('items[') and key.endswith(']'):
                try:
                    iid = int(key[6:-1])
                    q = int(val) if val else 0
                    if q > 0:
                        quantities[iid] = q
                except Exception:
                    continue

        if total_paid <= Decimal('0.00'):
            return JsonResponse({'success': False, 'message': 'Returns are allowed only after a payment has been made to this vendor for this bill.'}, status=400)

        if not quantities:
            return JsonResponse({'success': False, 'message': 'No items selected for return.'}, status=400)

        for bill_item_id, qty in quantities.items():
            try:
                bill_item = BillItem.objects.using(company_db).get(pk=bill_item_id, bill=bill)
            except BillItem.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Invalid bill item selected.'}, status=400)

            returned_total = PurchaseReturnItem.objects.using(company_db).filter(
                bill_item=bill_item,
                purchase_return__status__in=['pending', 'received']
            ).aggregate(total=Sum('quantity_returned'))['total'] or 0
            remaining_qty = int(bill_item.quantity or 0) - int(returned_total or 0)
            if qty > remaining_qty:
                item_name = bill_item.product.name if bill_item.product else 'Selected item'
                return JsonResponse({
                    'success': False,
                    'message': f'{item_name} has only {remaining_qty} quantity remaining for return.'
                }, status=400)

        warehouse_id = fixed_warehouse.pk if fixed_warehouse else None
        status = request.POST.get('status') or 'pending'
        received_by = request.POST.get('received_by') or ''

        with transaction.atomic():
            pr = PurchaseReturn.objects.using(company_db).create(
                bill=bill,
                vendor=bill.vendor,
                warehouse_id=warehouse_id if warehouse_id else None,
                document_currency=getattr(bill, 'document_currency', None),
                fx_rate_to_base=getattr(bill, 'fx_rate_to_base', Decimal('1.000000')) or Decimal('1.000000'),
                fx_rate_date=getattr(bill, 'fx_rate_date', None),
                refund_amount=Decimal('0.00'),
                refund_amount_base=Decimal('0.00'),
                notes=request.POST.get('notes',''),
                received_by=received_by,
                status=status,
            )

            # create return items and update stock
            for bill_item_id, qty in quantities.items():
                try:
                    bill_item = BillItem.objects.get(pk=bill_item_id, bill=bill)
                except BillItem.DoesNotExist:
                    raise

                PurchaseReturnItem.objects.using(company_db).create(
                    purchase_return=pr,
                    bill_item=bill_item,
                    quantity_returned=qty
                )

                # decrease stock (if Stock entry exists for same warehouse)
                try:
                    if warehouse_id and status == 'received':
                        try:
                            stock = Stock.objects.using(company_db).get(item=bill_item.product, warehouse_id=warehouse_id)
                            # reduce stock quantity for purchase return
                            before_qty = Decimal(str(stock.quantity or 0))
                            try:
                                dec_qty = Decimal(str(qty))
                            except Exception:
                                dec_qty = Decimal('0')
                            stock.quantity = max(before_qty - dec_qty, Decimal('0'))
                            stock.save()
                        except Stock.DoesNotExist:
                            # Create stock entry if it doesn't exist
                            try:
                                dec_qty = Decimal(str(qty))
                                stock = Stock.objects.using(company_db).create(
                                    item=bill_item.product,
                                    warehouse_id=warehouse_id,
                                    quantity=Decimal('0')  # Stock decreased by qty
                                )
                            except Exception:
                                stock = None

                        # record stock movement (out) for this return (always create)
                        if stock:
                            try:
                                StockMovement.objects.using(company_db).create(
                                    stock=stock,
                                    movement_type='out',
                                    quantity=dec_qty,
                                    reference_type='purchase_return',
                                    reference_id=pr.id,
                                    delivery_note=None,
                                    purchase_return=pr,
                                    notes=f"Outward for Purchase Return {pr.return_number}",
                                    created_by=request.user
                                )
                            except Exception:
                                logger.exception('Failed to create stock movement for purchase return')
                except Exception:
                    pass

            # Mark stock_updated=True if status is 'received'
            if status == 'received':
                pr.stock_updated = True
                pr.save(update_fields=['stock_updated'])

            try:
                _recalculate_purchase_return_amounts(pr, company_db=company_db)
                pr.save(update_fields=['document_currency', 'fx_rate_to_base', 'fx_rate_date', 'refund_amount', 'refund_amount_base'])
            except Exception:
                logger.exception('Failed to compute/save refund amount for PurchaseReturn %s', getattr(pr, 'id', None))

        if pr.status == 'received':
            try:
                reverse_purchase_bill_journal_for_return(
                    purchase_return=pr,
                    user=request.user,
                    company_db=company_db
                )
            except Exception:
                logger.exception('Failed to create reverse journal entries for purchase return %s', getattr(pr, 'id', None))

        messages.success(request, f'Purchase Return {pr.return_number} created successfully.')
        return JsonResponse({
            'success': True, 
            'message': f'Purchase Return {pr.return_number} created successfully.',
            'return_id': pr.pk
        })

    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error creating return: {str(e)}'}, status=500)


def purchase_return_detail(request, pk):
    """Display details of a purchase return."""
    from .models import PurchaseReturn
    from company.models import Company
    from currencies.models import Currency

    # Permission: require view access to Purchase Returns
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)):
            messages.error(request, 'You do not have permission to view purchase returns.')
            return redirect_with_company('bill_list')
    except Exception:
        messages.error(request, 'You do not have permission to view purchase returns.')
        return redirect_with_company('bill_list')

    company_db = getattr(request, 'company_db', 'default')
    purchase_return = get_object_or_404(PurchaseReturn.objects.using(company_db), pk=pk)
    purchase_return_currency_symbol = purchase_return.get_currency_symbol()
    purchase_return_currency_code = (
        getattr(getattr(purchase_return, 'document_currency', None), 'code', '')
        or (getattr(getattr(purchase_return, 'bill', None), 'document_currency', None).code if getattr(getattr(purchase_return, 'bill', None), 'document_currency', None) else '')
    )
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    company_base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    company_base_currency_code = getattr(base_currency, 'code', '') if base_currency else ''
    items_info = []
    total_amount = Decimal('0.00')

    for item in purchase_return.items.using(company_db).all():
        qty = Decimal(item.quantity_returned or 0)
        price = Decimal(item.bill_item.price or 0)
        line_total = qty * price
        total_amount += line_total

        items_info.append({
            'product_name': getattr(item.bill_item.product, 'name', ''),
            'description': getattr(item.bill_item, 'description', ''),
            'quantity': int(qty),
            'price': price,
            'line_total': line_total,
        })

    context = {
        'purchase_return': purchase_return,
        'bill': purchase_return.bill,
        'purchase_return_currency_symbol': purchase_return_currency_symbol,
        'purchase_return_currency_code': purchase_return_currency_code,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'items_info': items_info,
        'items': PurchaseReturnItem.objects.using(company_db).filter(purchase_return=purchase_return).select_related('bill_item__product'),
        'total_amount': total_amount,
        'movements': StockMovement.objects.using(company_db).filter(purchase_return=purchase_return).select_related('stock__item').order_by('-created_at'),
        'can_edit_purchase_returns': can_edit_purchase_returns(request.user),
        'can_delete_purchase_returns': can_delete_purchase_returns(request.user),
    }
    return render(request, 'Purchase/return_detail.html', context)


def purchase_return_cancel(request, pk):
    """Cancel a purchase return: set status and reverse stock if updated."""
    # Permission: require Delete on Purchase Returns
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_purchase_returns(request.user)):
            messages.error(request, 'You do not have permission to cancel Purchase Return.')
            return redirect_with_company(request, 'purchase_return_detail', pk=pk)
    except Exception:
        messages.error(request, 'You do not have permission to cancel Purchase Return.')
        return redirect_with_company(request, 'purchase_return_detail', pk=pk)

    company_db = getattr(request, 'company_db', 'default')
    pr = get_object_or_404(PurchaseReturn.objects.using(company_db), pk=pk)

    if request.method == 'POST':
        # Check period lock before canceling
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        try:
            PeriodLockEnforcer.check_can_edit(pr.date, request.user, db=company_db, transaction_type='purchase_return')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(company_db).filter(
                    start_date__lte=pr.date,
                    end_date__gte=pr.date,
                    status='active'
                ).first()
                
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked:
                        locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_return',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            # Check if AJAX request
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': str(e),
                    'period_lock_error': period_lock_error
                }, status=400)
            
            messages.error(request, str(e))
            return redirect_with_company(request, 'purchase_return_detail', pk=pk)

        if pr.status == 'cancelled':
            messages.warning(request, 'This return is already cancelled.')
        else:
            try:
                with transaction.atomic():
                    pr.status = 'cancelled'
                    pr.refund_amount = 0
                    pr.save()

                    # Reverse journal entries created for this return (reverse the reversals)
                    if pr.bill:
                        bill = pr.bill
                        try:
                            from journal.models import JournalEntry, JournalLine
                            
                            def _next_jv():
                                """Generate the next journal entry number."""
                                last_entry = JournalEntry.objects.using(company_db).filter(
                                    entry_number__startswith='JV-'
                                ).order_by('-id').first()
                                if last_entry and last_entry.entry_number:
                                    try:
                                        last_num = int(last_entry.entry_number.split('-')[1])
                                    except (IndexError, ValueError):
                                        last_num = 0
                                else:
                                    last_num = 0
                                return f"JV-{str(last_num + 1).zfill(5)}"
                            
                            # Find the most recent reversal entries for bill and payment
                            # COGS reversals are only for sales, not purchases
                            
                            # Get most recent bill reversal
                            # The reference format is: Reversal-Return-{return_number}-Bill-{bill_number}
                            bill_reversal = JournalEntry.objects.using(company_db).filter(
                                reference__icontains=f'Reversal-Return',
                                narration__icontains='Reversal'
                            ).exclude(
                                narration__icontains='Reversal of Reversal'
                            ).filter(
                                reference__icontains=bill.bill_number
                            ).order_by('-id').first()
                            
                            # Get most recent payment allocation reversal
                            payment_reversal = JournalEntry.objects.using(company_db).filter(
                                reference__icontains='Reversal-PAY',
                                narration__icontains='Reversal of payment'
                            ).filter(
                                reference__icontains=bill.bill_number
                            ).exclude(
                                narration__icontains='Reversal of Reversal'
                            ).order_by('-id').first()
                            
                            reversal_entries = [e for e in [bill_reversal, payment_reversal] if e]
                            logger.info(f'Found {len(reversal_entries)} reversal entry types for bill {bill.bill_number}')
                            
                            if reversal_entries:
                                for rev_entry in reversal_entries:
                                    try:
                                        logger.info(f'Creating reversal for journal {rev_entry.entry_number} (ref: {rev_entry.reference})')
                                        entry_number = _next_jv()
                                        
                                        cancel_entry = JournalEntry.objects.using(company_db).create(
                                            entry_number=entry_number,
                                            date=pr.date,
                                            reference=f"Cancel-{pr.return_number}-{bill.bill_number}",
                                            narration=f"Reversal of Reversal (Cancelled Return {pr.return_number}) - {rev_entry.entry_number}",
                                            created_by=request.user,
                                            updated_by=request.user,
                                            status='posted'
                                        )
                                        logger.info(f'Created cancel entry {cancel_entry.entry_number}')
                                        
                                        # Mirror the lines but swap debit/credit to reverse
                                        for line in rev_entry.lines.all():
                                            JournalLine.objects.using(company_db).create(
                                                journal=cancel_entry,
                                                account=line.account,
                                                description=f"Cancel: {line.description}",
                                                debit=line.credit or Decimal('0'),
                                                credit=line.debit or Decimal('0'),
                                                sequence=line.sequence
                                            )
                                        
                                        try:
                                            cancel_entry.total_debit = sum(Decimal(str(l.debit or 0)) for l in cancel_entry.lines.all())
                                            cancel_entry.total_credit = sum(Decimal(str(l.credit or 0)) for l in cancel_entry.lines.all())
                                            cancel_entry.save(update_fields=['total_debit', 'total_credit'])
                                            logger.info(f'Updated totals for cancel entry: debit={cancel_entry.total_debit}, credit={cancel_entry.total_credit}')
                                        except Exception as e:
                                            logger.exception(f'Failed to update totals for cancel journal: {str(e)}')
                                    except Exception as e:
                                        logger.exception(f'Failed to create reversal for journal entry {rev_entry.entry_number}: {str(e)}')
                            else:
                                logger.warning(f'No reversal entries found for bill {bill.bill_number}')
                        except Exception as e:
                            logger.exception(f'Failed to reverse journal entries for purchase return cancellation: {str(e)}')

                    # Reverse stock if it was updated (add back the qty removed by return)
                    if pr.stock_updated:
                        for ritem in pr.items.using(company_db).select_related('bill_item'):
                            try:
                                # use ids to avoid cross-db FK object issues
                                prod_id = getattr(ritem.bill_item, 'product_id', None)
                                wh_id = getattr(pr, 'warehouse_id', None)
                                stock = Stock.objects.using(company_db).get(item_id=prod_id, warehouse_id=wh_id)
                                # increase stock (restore returned qty)
                                try:
                                    qty = Decimal(str(ritem.quantity_returned or 0))
                                except Exception:
                                    qty = Decimal('0')
                                try:
                                    before_qty = Decimal(str(stock.quantity or 0))
                                except Exception:
                                    before_qty = Decimal('0')
                                stock.quantity = before_qty + qty
                                stock.save()

                                # record stock movement (in)
                                try:
                                    StockMovement.objects.using(company_db).create(
                                        stock=stock,
                                        movement_type='in',
                                        quantity=ritem.quantity_returned,
                                        reference_type='purchase_return_reversal',
                                        reference_id=pr.id,
                                        purchase_return=pr,
                                        notes=f"Stock in for cancelled Purchase Return {pr.return_number}",
                                        created_by=request.user
                                    )
                                except Exception:
                                    logger.exception('Failed to create stock movement for purchase return cancellation')
                            except Exception:
                                logger.exception('Failed to reverse stock for purchase return item')

                        pr.stock_updated = False
                        pr.save(update_fields=['stock_updated'])

                messages.success(request, f'Purchase Return {pr.return_number} cancelled successfully!')
            except Exception as e:
                messages.error(request, f'Error cancelling return: {str(e)}')

    return redirect_with_company(request, 'purchase_return_detail', pk=pk)


def purchase_return_mark_received(request, pk):
    """Mark a purchase return as received (decrease warehouse stock)."""
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_returns(request.user)):
            messages.error(request, 'You do not have permission to mark Purchase Return as received.')
            return redirect_with_company(request, 'purchase_return_detail', pk=pk)
    except Exception:
        messages.error(request, 'You do not have permission to mark Purchase Return as received.')
        return redirect_with_company(request, 'purchase_return_detail', pk=pk)

    company_db = getattr(request, 'company_db', 'default')
    pr = get_object_or_404(PurchaseReturn.objects.using(company_db), pk=pk)

    if request.method == 'POST':
        # Check period lock before marking received
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        try:
            PeriodLockEnforcer.check_can_edit(pr.date, request.user, db=company_db, transaction_type='purchase_return')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(company_db).filter(
                    start_date__lte=pr.date,
                    end_date__gte=pr.date,
                    status='active'
                ).first()
                
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked:
                        locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    
                    period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_return',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            # Check if AJAX request
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': str(e),
                    'period_lock_error': period_lock_error
                }, status=400)
            
            messages.error(request, str(e))
            return redirect_with_company(request, 'purchase_return_detail', pk=pk)

        if pr.status == 'received':
            messages.warning(request, 'This return is already marked as received.')
        else:
            try:
                with transaction.atomic():
                    pr.status = 'received'
                    
                    # Calculate refund amount from returned items
                    refund_total = Decimal('0')
                    for ritem in pr.items.using(company_db).select_related('bill_item'):
                        try:
                            qty = Decimal(str(ritem.quantity_returned or 0))
                            price = Decimal(str(ritem.bill_item.price or 0))
                            tax_rate = Decimal(str(ritem.bill_item.prd_tax or 0))
                            # amount = quantity * price * (1 + tax/100)
                            item_amount = qty * price * (1 + (tax_rate / 100))
                            refund_total += item_amount
                        except Exception:
                            pass
                    
                    pr.refund_amount = refund_total
                    pr.save()

                    success_count = 0
                    fail_count = 0
                    for ritem in pr.items.using(company_db).select_related('bill_item'):
                        try:
                            if not pr.warehouse:
                                logger.warning('No warehouse for PurchaseReturn %s, skipping stock update', pr.id)
                                fail_count += 1
                                continue
                            prod_id = getattr(ritem.bill_item, 'product_id', None)
                            wh_id = getattr(pr, 'warehouse_id', None)
                            stock, created = Stock.objects.using(company_db).get_or_create(
                                item_id=prod_id,
                                warehouse_id=wh_id,
                                defaults={'quantity': 0}
                            )
                            try:
                                qty = Decimal(str(ritem.quantity_returned or 0))
                            except Exception:
                                qty = Decimal('0')

                            try:
                                before_qty = Decimal(str(stock.quantity or 0))
                            except Exception:
                                before_qty = Decimal('0')

                            # decrease stock for purchase return
                            stock.quantity = max(before_qty - qty, Decimal('0'))
                            stock.save()

                            try:
                                StockMovement.objects.using(company_db).create(
                                    stock=stock,
                                    movement_type='out',
                                    quantity=ritem.quantity_returned,
                                    reference_type='purchase_return',
                                    reference_id=pr.id,
                                    purchase_return=pr,
                                    notes=f"Outward for Purchase Return {pr.return_number}",
                                    created_by=request.user
                                )
                                success_count += 1
                            except Exception:
                                fail_count += 1
                                logger.exception('Failed to create stock movement for purchase return receive for return id=%s item=%s', pr.id, getattr(ritem.bill_item.product, 'id', None))
                        except Exception:
                            fail_count += 1
                            logger.exception('Failed to update stock for purchase return item for return id=%s', pr.id)

                    pr.stock_updated = True
                    pr.save(update_fields=['stock_updated'])

                    # Create reversal journal entries when marked as received
                    if pr.bill:
                        try:
                            reverse_purchase_bill_journal_for_return(
                                purchase_return=pr,
                                user=request.user,
                                company_db=company_db,
                            )
                        except Exception as e:
                            logger.exception(f'Failed to create reversal journal entries for received return: {str(e)}')

                if success_count > 0:
                    messages.success(request, f'Purchase Return {pr.return_number} marked as received and {success_count} stock movement(s) created.')
                else:
                    messages.warning(request, f'Purchase Return {pr.return_number} marked as received but no stock movements were created.')

                if fail_count > 0:
                    messages.error(request, f'{fail_count} stock update/movement operation(s) failed — check server logs.')
            except Exception as e:
                messages.error(request, f'Error updating return: {str(e)}')

    return redirect_with_company(request, 'purchase_return_detail', pk=pk)



# @login_required
# def purchase_return_list(request):
#     """List Purchase Returns with proper permission checks."""
#     # Permission: require View on Purchase Returns
#     if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)):
#         messages.error(request, 'You do not have permission to view Purchase Returns.')
#         return redirect_with_company('bill_list')
    
#     # Allow searching by return number or vendor name
#     company_db = getattr(request, 'company_db', 'default')
#     q = request.GET.get('q', '').strip()
#     returns = PurchaseReturn.objects.using(company_db).all().order_by('-id')
#     if q:
#         returns = returns.filter(
#             Q(return_number__icontains=q) |
#             Q(vendor__company_name__icontains=q) |
#             Q(vendor__first_name__icontains=q) |
#             Q(vendor__last_name__icontains=q)
#         )

#     paginator = Paginator(returns, 10)
#     page_number = request.GET.get('page')
#     returns_page = paginator.get_page(page_number)

#     # Get status counts for stats cards
#     all_returns = PurchaseReturn.objects.using(company_db).all()
#     pending_count = all_returns.filter(status='pending').count()
#     returned_count = all_returns.filter(status='received').count()
#     cancelled_count = all_returns.filter(status='cancelled').count()

#     return render(request, "Purchase/return_list.html", {
#         "returns": returns_page,
#         "search_query": q,
#         "pending_count": pending_count,
#         "returned_count": returned_count,
#         "cancelled_count": cancelled_count,
#     })


class PurchaseReturnUpdateView(UpdateView):
    model = PurchaseReturn
    form_class = PurchaseReturnForm
    template_name = 'Purchase/purchase_return_form.html'
    success_url = 'purchase_return_list'
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require Edit on Purchase Returns
        if not (getattr(request.user, 'is_superuser', False) or can_edit_purchase_returns(request.user)):
            messages.error(request, 'You do not have permission to edit Purchase Returns.')
            return redirect_with_company('purchase_return_list')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        # Only allow editing if stock hasn't been updated
        return PurchaseReturn.objects.filter(stock_updated=False)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Warehouse is fixed for purchase return; do not allow changing on edit.
        if 'warehouse' in form.fields:
            form.fields['warehouse'].disabled = True
            form.fields['warehouse'].required = False
        return form
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Check if period is locked for Purchase Return
        from system_settings.models import FiscalYear
        db = getattr(self.request, 'company_db', 'default')
        
        # Check if there was a period lock error from form submission
        if hasattr(self.request, 'period_lock_error') and self.request.period_lock_error:
            context['period_lock_error'] = self.request.period_lock_error
        else:
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=self.object.date,
                    end_date__gte=self.object.date,
                    status='active'
                ).first()
                
                if fiscal_year and fiscal_year.lock_purchase_return:
                    context['period_locked'] = True
                    context['fiscal_year_name'] = fiscal_year.name
                else:
                    context['period_locked'] = False
            except Exception:
                context['period_locked'] = False
        
        # Get all bill items for this return's bill
        bill_items = BillItem.objects.filter(bill=self.object.bill)
        
        # Create a mapping of bill_item_id to return quantity for quick lookup
        return_items_map = {}
        for ri in self.object.items.all():
            return_items_map[ri.bill_item.id] = ri
        
        # Calculate already returned quantities from OTHER returns (not this one)
        already_returned_map = {}
        for bi in bill_items:
            # Sum all returned quantities from other purchase returns (excluding this one)
            other_returns = PurchaseReturnItem.objects.filter(
                bill_item=bi,
                purchase_return__status__in=['pending', 'received']
            ).exclude(purchase_return=self.object).aggregate(total=Sum('quantity_returned'))['total'] or 0
            already_returned_map[bi.id] = other_returns
        
        # For each bill item, check if it has a return record in THIS return
        # If it does, use that PurchaseReturnItem; otherwise create a placeholder
        items_for_template = []
        for bi in bill_items:
            if bi.id in return_items_map:
                # Use the existing PurchaseReturnItem
                item_obj = return_items_map[bi.id]
            else:
                # Create a temporary PurchaseReturnItem-like object with 0 quantity for display
                item_obj = PurchaseReturnItem(
                    purchase_return=self.object,
                    bill_item=bi,
                    quantity_returned=0
                )
            # Attach the already_returned info (without underscore for template compatibility)
            item_obj.already_returned_qty = already_returned_map[bi.id]
            items_for_template.append(item_obj)
        
        context['existing_items'] = items_for_template
        context['purchase_return_currency_symbol'] = self.object.get_currency_symbol()
        context['purchase_return_currency_code'] = (
            getattr(getattr(self.object, 'document_currency', None), 'code', '')
            or (self.object.bill.document_currency.code if getattr(self.object.bill, 'document_currency', None) else '')
        )
        return context
    
    def form_valid(self, form):
        from stock.models import Stock
        import json
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        persisted_pr = PurchaseReturn.objects.get(pk=self.object.pk)
        old_status = persisted_pr.status
        old_stock_updated = persisted_pr.stock_updated
        
        # Check period lock before updating
        db = getattr(self.request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(self.object.date, self.request.user, db=db, transaction_type='purchase_return')
        except PermissionDenied as e:
            # Extract lock details for modal display
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=self.object.date,
                    end_date__gte=self.object.date,
                    status='active'
                ).first()
                
                if fiscal_year:
                    locked_fields = []
                    if fiscal_year.is_locked:
                        locked_fields.append('all')
                    locked_fields.extend(fiscal_year.locked_categories)
                    
                    self.request.period_lock_error = {
                        'fiscal_year': fiscal_year.name,
                        'start_date': fiscal_year.start_date.strftime('%d %b %Y'),
                        'end_date': fiscal_year.end_date.strftime('%d %b %Y'),
                        'transaction_type': 'purchase_return',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            return self.form_invalid(form)
        
        with transaction.atomic():
            # Enforce fixed warehouse from persisted return record.
            self.object.warehouse = persisted_pr.warehouse
            self.object.document_currency = persisted_pr.document_currency or getattr(persisted_pr.bill, 'document_currency', None)
            self.object.fx_rate_to_base = persisted_pr.fx_rate_to_base or getattr(persisted_pr.bill, 'fx_rate_to_base', Decimal('1.000000')) or Decimal('1.000000')
            self.object.fx_rate_date = persisted_pr.fx_rate_date or getattr(persisted_pr.bill, 'fx_rate_date', None)
            self.object = form.save()
            
            # Get selected items and quantities from the form submission
            selected_items_json = self.request.POST.get('selected_items', '[]')
            return_quantities_json = self.request.POST.get('return_quantities', '{}')
            
            try:
                selected_items = json.loads(selected_items_json)
                return_quantities = json.loads(return_quantities_json)
            except (json.JSONDecodeError, TypeError):
                selected_items = []
                return_quantities = {}
            
            # Get bill items to check against selected items
            bill = self.object.bill
            all_bill_items = BillItem.objects.filter(bill=bill)
            
            # Create a map of existing return items by bill_item_id
            existing_return_items = {}
            for ri in self.object.items.all():
                existing_return_items[ri.bill_item.id] = ri
            
            # Process selected items - create or update PurchaseReturnItem records
            for bill_item in all_bill_items:
                if str(bill_item.id) in [str(x) for x in selected_items]:
                    # This item was selected for return
                    new_qty = int(return_quantities.get(str(bill_item.id), 0))
                    
                    if bill_item.id in existing_return_items:
                        # Update existing return item
                        return_item = existing_return_items[bill_item.id]
                        return_item.quantity_returned = new_qty
                        return_item.save()
                    else:
                        # Create new return item
                        PurchaseReturnItem.objects.create(
                            purchase_return=self.object,
                            bill_item=bill_item,
                            quantity_returned=new_qty
                        )
                else:
                    # This item was NOT selected - delete or set to 0
                    if bill_item.id in existing_return_items:
                        # Delete the return item if it existed
                        existing_return_items[bill_item.id].delete()
            
            try:
                _recalculate_purchase_return_amounts(self.object, company_db=db)
                self.object.save(update_fields=['document_currency', 'fx_rate_to_base', 'fx_rate_date', 'refund_amount', 'refund_amount_base'])

                # If status changed from cancelled/pending to received
                # We need to handle stock movements
                # If transitioning to 'received': Creates Outward movements (-qty)
                # Purchase return = stock reduction in warehouse
                if self.object.status == 'received' and old_status != 'received':
                    success_count = 0
                    fail_count = 0
                    
                    for ritem in self.object.items.all():
                        # Skip items with no quantity_returned (not selected)
                        if not ritem.quantity_returned or ritem.quantity_returned == 0:
                            continue
                        try:
                            if not self.object.warehouse:
                                logger.warning('No warehouse for PurchaseReturn %s, skipping stock update', self.object.id)
                                fail_count += 1
                                continue
                            
                            stock, created = Stock.objects.get_or_create(
                                item=ritem.bill_item.product,
                                warehouse=self.object.warehouse,
                                defaults={'quantity': 0}
                            )
                            
                            try:
                                qty = Decimal(str(ritem.quantity_returned or 0))
                            except Exception:
                                qty = Decimal('0')
                            
                            try:
                                before_qty = Decimal(str(stock.quantity or 0))
                            except Exception:
                                before_qty = Decimal('0')
                            
                            # Decrease stock for purchase return
                            stock.quantity = max(before_qty - qty, Decimal('0'))
                            stock.save()
                            
                            # Create stock movement record
                            try:
                                StockMovement.objects.create(
                                    stock=stock,
                                    movement_type='out',
                                    quantity=ritem.quantity_returned,
                                    reference_type='purchase_return',
                                    reference_id=self.object.id,
                                    purchase_return=self.object,
                                    notes=f"Outward for Purchase Return {self.object.return_number}",
                                    created_by=self.request.user
                                )
                                success_count += 1
                            except Exception:
                                fail_count += 1
                                logger.exception('Failed to create stock movement for purchase return id=%s', self.object.id)
                        except Exception:
                            fail_count += 1
                            logger.exception('Failed to update stock for purchase return item for return id=%s', self.object.id)
                    
                    self.object.stock_updated = True
                    self.object.save(update_fields=['stock_updated'])
                    
                    if success_count > 0:
                        messages.success(self.request, f'Purchase Return {self.object.return_number} updated and {success_count} stock movement(s) created.')
                    if fail_count > 0:
                        messages.warning(self.request, f'{fail_count} stock update/movement operation(s) had issues.')

                    # Create reversal journal entries for this purchase return
                    try:
                        company_db = getattr(self.request, 'company_db', 'default')
                        reverse_purchase_bill_journal_for_return(
                            purchase_return=self.object,
                            user=self.request.user,
                            company_db=company_db
                        )
                    except Exception:
                        logger.exception('Failed to create reverse journal entries for purchase return %s', getattr(self.object, 'id', None))
                
                # If status changed back to pending/cancelled from received
                elif self.object.status != 'received' and old_status == 'received' and old_stock_updated:
                    # Reverse stock movements only for items that have them
                    for ritem in self.object.items.all():
                        # Skip items with no quantity_returned (they weren't part of the return)
                        if not ritem.quantity_returned or ritem.quantity_returned == 0:
                            continue
                        try:
                            stock = Stock.objects.get(
                                item=ritem.bill_item.product,
                                warehouse=self.object.warehouse
                            )
                            
                            try:
                                qty = Decimal(str(ritem.quantity_returned or 0))
                            except Exception:
                                qty = Decimal('0')
                            
                            try:
                                before_qty = Decimal(str(stock.quantity or 0))
                            except Exception:
                                before_qty = Decimal('0')
                            
                            stock.quantity = before_qty + qty
                            stock.save()
                            
                            # Create reversal stock movement record
                            try:
                                StockMovement.objects.create(
                                    stock=stock,
                                    movement_type='in',
                                    quantity=ritem.quantity_returned,
                                    reference_type='purchase_return_reversal',
                                    reference_id=self.object.id,
                                    purchase_return=self.object,
                                    notes=f"Stock reversal for cancelled/pending Purchase Return {self.object.return_number}",
                                    created_by=self.request.user
                                )
                            except Exception:
                                logger.exception('Failed to create reversal stock movement for purchase return id=%s', self.object.id)
                        except Exception:
                            logger.exception('Failed to reverse stock for purchase return item for return id=%s', self.object.id)
                    
                    self.object.stock_updated = False
                    self.object.save(update_fields=['stock_updated'])
                    try:
                        company_db = getattr(self.request, 'company_db', 'default')
                        _delete_purchase_return_reversal_entries(self.object, company_db=company_db)
                    except Exception:
                        logger.exception(
                            'Failed to delete reversal journal entries for purchase return %s',
                            getattr(self.object, 'id', None),
                        )
                    messages.success(self.request, f'Purchase Return {self.object.return_number} updated and stock movements reversed.')
                else:
                    messages.success(self.request, f'Purchase Return {self.object.return_number} updated successfully!')
                
                # Check if AJAX request
                if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    # Get the redirect URL
                    from django.urls import reverse
                    try:
                        if hasattr(self.request, 'company_code') and self.request.company_code:
                            list_url = reverse('purchase_return_list', args=[self.request.company_code])
                        else:
                            list_url = reverse('purchase_return_list')
                    except:
                        list_url = '/purchase/returns/'
                    
                    return JsonResponse({
                        'success': True,
                        'message': f'Purchase Return {self.object.return_number} updated successfully!',
                        'redirect_url': list_url
                    })
                
            except Exception as e:
                messages.error(self.request, f'Error updating return: {str(e)}')
                logger.exception('Error in PurchaseReturnUpdateView form_valid')
                return self.form_invalid(form)
            
            return redirect_with_company(self.success_url)
    
    def form_invalid(self, form):
        """Handle invalid form submission with AJAX support."""
        # Check if there's a period lock error
        if hasattr(self.request, 'period_lock_error') and self.request.period_lock_error:
            error_msg = (
                f"❌ Period Locked: The fiscal year is locked for editing (purchase_return).<br>"
                f"<i class='bi bi-lock-fill'></i> Locked by: {self.request.period_lock_error.get('locked_by', 'Admin')}<br>"
                f"<i class='bi bi-calendar'></i> Lock Date: {self.request.period_lock_error.get('lock_date', 'N/A')}<br>"
                f"<i class='bi bi-info-circle'></i> Locked areas: {self.request.period_lock_error.get('locked_areas', 'N/A')}<br><br>"
                f" Contact your administrator to unlock this period if needed."
            )
        else:
            # Collect form errors
            errors = []
            for field, field_errors in form.errors.items():
                for error in field_errors:
                    errors.append(f"{field}: {error}")
            error_msg = "<br>".join(errors) if errors else "Please correct the errors and try again."
        
        # Check if AJAX request
        if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': error_msg
            }, status=400)
        
        return super().form_invalid(form)


@login_required
def purchase_return_list(request):
    """List Purchase Returns with proper permission checks."""
    # Permission: require View on Purchase Returns
    if not (getattr(request.user, 'is_superuser', False) or can_view_purchase_returns(request.user)):
        return redirect_with_company('license_restricted')
    
    # Allow searching by return number or vendor name
    company_db = getattr(request, 'company_db', 'default')
    q = request.GET.get('q', '').strip()
    returns = PurchaseReturn.objects.using(company_db).all().order_by('-id')
    if q:
        returns = returns.filter(
            Q(return_number__icontains=q) |
            Q(vendor__company_name__icontains=q) |
            Q(vendor__first_name__icontains=q) |
            Q(vendor__last_name__icontains=q)
        )

    paginator = Paginator(returns, 10)
    page_number = request.GET.get('page')
    returns_page = paginator.get_page(page_number)

    # Get status counts for stats cards
    all_returns = PurchaseReturn.objects.using(company_db).all()
    pending_count = all_returns.filter(status='pending').count()
    returned_count = all_returns.filter(status='received').count()
    cancelled_count = all_returns.filter(status='cancelled').count()

    return render(request, "Purchase/return_list.html", {
        "returns": returns_page,
        "search_query": q,
        "pending_count": pending_count,
        "returned_count": returned_count,
        "cancelled_count": cancelled_count,
        "can_create_purchase_returns": getattr(request.user, 'is_superuser', False) or can_create_purchase_returns(request.user),
    })
