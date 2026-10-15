from django.shortcuts import render, redirect, get_object_or_404
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_redirect_url
from django.db import IntegrityError,transaction
from django.contrib.auth.decorators import login_required
from Items.models import Item
from company.models import Company #added on 20-1-26 neha
from company.utils import get_company_logo_base64
from customer.models import Customer
from currencies.models import Currency
from currencies.services import scale_amount_for_journal
from django.utils.timezone import localdate
from .models import (
    SalesQuotation, SalesPerson, SalesQuotationItem, QuotePrefix,
    SalesOrder, SalesOrderItem, OrderPrefix,
    SalesInvoice, SalesInvoiceItem, InvoicePrefix,
    InvPaymentAllocation, InvPayment, InvPaymentAttachment, CustomerAdvancePayment,
    SalesReturn, SalesReturnItem, InvoiceTax, PerformaInvoice, PerformaInvoiceItem, PerformaInvoicePrefix,EWayBill,
)
from .models import SalesStockMovement
from Purchase.models import PaymentMode
from chart_of_accounts.models import ChartOfAccounts
from chart_of_accounts.services import (
    ensure_tds_tcs_accounts,
    get_company_tax_type,
    normalize_tax_code,
    resolve_tax_account,
    resolve_tds_tcs_account,
)
try:
    from taxation.services.tax_engine import calculate_tax
except ImportError:
    calculate_tax = None
from journal.models import JournalEntry, JournalLine
from Tax.models import TaxGroup, Tax, TaxMaster, TdsMaster, TcsMaster
from django.core.paginator import Paginator
from django.db.models import Max
from django.db.models import OuterRef, Subquery, F
from django.db import transaction
from django.utils import timezone
import re
from datetime import date
from company.utils import is_stock_management_on_delivery
from django.conf import settings
from collections import defaultdict
from django.views.decorators.http import require_POST
# from django.views.generic import DetailView


from django.http import HttpResponse
from django.http import JsonResponse
from django.db.models import Q,Sum
from django.db.models import Value
from django.db.models.functions import Concat
from django.urls import reverse
from .forms import CustomerForm,SalesQuotationForm,SalesQuotationItemForm, SalesPersonForm,SalesOrderForm,SalesOrderItemForm,SalesInvoiceForm,SalesInvoiceItemForm,PerformaInvoiceForm,PerformaInvoiceItemForm
from django.forms import formset_factory
from Items.forms import ItemForm
from django.forms import modelformset_factory
from django.forms import inlineformset_factory

import logging
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django.template.loader import render_to_string
import io
from django.views.decorators.csrf import csrf_exempt
import traceback
from email_templates.utils import replace_placeholders
from email_config.models import EmailConfiguration
from django.core.mail import send_mail as django_send_mail
from django.core.mail import get_connection, EmailMultiAlternatives, EmailMessage
# Note: avoid importing `get_default_email_template` at module import time to prevent circular imports.
from django_countries import countries
try:
    # prefer xhtml2pdf if available
    from xhtml2pdf import pisa
    HAVE_XHTML2PDF = True
except Exception:
    HAVE_XHTML2PDF = False
from Tax.models import Tax,TaxGroup
from PayTerms.models import PayTerms
from unit.models import Unit
from Items.models import Item,Barcode,Uom,Uom_name
from django.contrib import messages
logger = logging.getLogger(__name__)


def _get_current_salesperson(request):
    """Return a SalesPerson instance matching the current logged-in user, if any.

    Try matching by email first, then by common fullname/username fallbacks.
    """
    try:
        if not getattr(request, 'user', None):
            return None
        db_alias = getattr(request, 'company_db', 'default')
        # try common email attributes
        user_email = getattr(request.user, 'email', None) or getattr(request.user, 'usr_mail', None)
        if user_email:
            sp = SalesPerson.objects.using(db_alias).filter(email__iexact=(user_email or '')).first()
            if sp:
                return sp

        # try full name / username fallbacks
        full_name = ''
        try:
            if callable(getattr(request.user, 'get_full_name', None)):
                full_name = request.user.get_full_name() or ''
        except Exception:
            full_name = ''

        if not full_name:
            full_name = ' '.join(filter(None, [getattr(request.user, 'first_name', ''), getattr(request.user, 'last_name', '')])).strip()
        if not full_name:
            full_name = getattr(request.user, 'username', None) or getattr(request.user, 'usr_name', None) or ''

        if full_name:
            sp = SalesPerson.objects.using(db_alias).filter(name__iexact=full_name).first()
            if sp:
                return sp
    except Exception:
        logger.debug('Failed to resolve current SalesPerson for user', exc_info=True)
    return None


def _post_tds_tcs_journal_line(journal, document, using, sequence, posting_role):
    tds_tcs_type = str(getattr(document, 'tds_tcs_type', '') or '').strip().lower()
    if tds_tcs_type not in ('tds', 'tcs'):
        return sequence

    # Scale TDS/TCS using explicit fx_rate to avoid distortions when
    # `total_amount` (document net) and `total_amount_base` (gross base) differ.
    amount = _scaled_tds_tcs_amount(document)
    if amount <= 0:
        return sequence

    ensure_tds_tcs_accounts(using=using)
    if posting_role == 'sales':
        direction = 'receivable' if tds_tcs_type == 'tds' else 'payable'
    else:
        direction = 'payable' if tds_tcs_type == 'tds' else 'receivable'

    account = resolve_tds_tcs_account(tds_tcs_type, direction=direction, using=using)
    if not account:
        return sequence

    debit_amount = amount if direction == 'receivable' else Decimal('0.00')
    credit_amount = amount if direction == 'payable' else Decimal('0.00')
    reference_number = getattr(document, 'inv_number', None) or getattr(document, 'bill_number', None) or getattr(document, 'quote_number', None) or getattr(document, 'performa_inv_number', None) or getattr(document, 'number', None) or getattr(document, 'pk', '')
    journal_prefix = 'Invoice' if posting_role == 'sales' else 'Bill'
    JournalLine.objects.create(
        journal=journal,
        account=account,
        description=f"{journal_prefix} {reference_number} - {account.name}",
        debit=debit_amount,
        credit=credit_amount,
        sequence=sequence,
    )
    return sequence + 10


def _scaled_tds_tcs_amount(document):
    """Return TDS/TCS amount expressed in base currency using explicit fx_rate_to_base.

    We avoid using `scale_amount_for_journal` here because that helper
    relies on `document.total_amount` vs `total_amount_base` ratio which may
    be inconsistent when TDS/TCS is present (net vs gross mismatch).
    """
    try:
        amt = Decimal(str(getattr(document, 'tds_tcs_amount', 0) or 0))
        fx = getattr(document, 'fx_rate_to_base', None) or Decimal('1')
        return (amt * fx).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')
from .permissions import (
    can_create_quotations, can_edit_quotations, can_delete_quotations,
    can_create_orders, can_create_invoices
)
from .permissions import (
    can_create_orders, can_edit_orders, can_delete_orders,
    can_create_invoices, can_edit_invoices, can_delete_invoices,
    can_view_quotations, can_view_orders, can_view_invoices,
    can_view_payments_received,
    can_view_delivery, can_create_delivery, can_edit_delivery, can_delete_delivery,
    can_view_sales_dashboard,
    can_view_performa_invoice, can_create_performa_invoice, can_edit_performa_invoice, can_delete_performa_invoice,
    can_view_returns, can_create_returns, can_edit_returns, can_delete_returns,
    can_view_eway_bill, can_create_eway_bill, can_edit_eway_bill, can_delete_eway_bill,
)
from .cogs_utils import create_cogs_transfer
from .rounding import apply_sales_rounding
#added on 20-1-26 neha
from xml.sax.saxutils import escape
def safe(value):
    return escape(str(value)) if value else ""

def _get_current_company_country(request):
    # Handle None request - just get the default company
    if request is None:
        company = Company.objects.filter(status=1).first() or Company.objects.first()
        if not company or not getattr(company, 'country', None):
            return ''
        try:
            if getattr(company.country, 'code', None):
                return company.country.code or company.country.name or str(company.country)
            return company.country.name or str(company.country)
        except Exception:
            return str(company.country)
    
    company_code = getattr(request, 'company_code', None) or request.session.get('company_code')
    company_db = getattr(request, 'company_db', None)
    session_company_db = request.session.get('company_db') if hasattr(request, 'session') else None
    company = None

    if company_db and company_db != 'default':
        tenant_companies = Company.objects.using(company_db)
        if company_code:
            company = tenant_companies.filter(company_code=company_code).only('country').first()
        if company is None:
            company = tenant_companies.only('country').order_by('id').first()

    if company is None and company_code:
        company = Company.objects.using('default').filter(company_code=company_code).only('country').first()

    if company is None and session_company_db and session_company_db != 'default':
        company = Company.objects.using('default').filter(db_name=session_company_db).only('country').first()

    if not company or not getattr(company, 'country', None):
        return ''

    try:
        if getattr(company.country, 'code', None):
            return company.country.code or company.country.name or str(company.country)
        return company.country.name or str(company.country)
    except Exception:
        return str(company.country)

def _is_indian_company_country(company_country):
    company_tax_type = ""
    normalized_country = (company_country or '').strip().lower()
    try:
        company = Company.objects.filter(country__iexact=str(company_country or "").strip()).order_by("id").first()
        company_tax_type = str(getattr(company, "tax_type", "") or "").strip().upper() if company else ""
    except Exception:
        company_tax_type = ""
    if company_tax_type:
        return company_tax_type == "GST"

    normalized_country = (company_country or '').strip().lower()
    return normalized_country in {'india', 'in', 'ind'}

def get_customer_display_name(customer):
    if not customer:
        return ''
    if getattr(customer, 'customer_type', '') == 'company':
        return customer.company_name or ''
    return f"{customer.first_name} {customer.last_name or ''}".strip()


def _parse_decimal(value, default='0'):
    try:
        return Decimal(str(value or default))
    except Exception:
        return Decimal(str(default))


def _parse_positive_int(value):
    try:
        integer_value = int(value)
        return integer_value if integer_value >= 0 else None
    except Exception:
        return None

from django.views.generic import ListView, DetailView, CreateView, UpdateView
from .models import SalesDeliveryNote,SalesDeliveryNoteItem
from .forms import SalesDeliveryNoteForm, SalesReturnForm
from django.urls import reverse_lazy
from warehouse.models import Warehouse
from stock.models import Stock
from django.core.exceptions import ValidationError


def get_company_default_warehouse(request=None):
    db = getattr(request, 'company_db', 'default')
    warehouse = Warehouse.objects.using(db).filter(is_default=True).first()
    if not warehouse:
        warehouse = Warehouse.objects.using(db).first()
    return warehouse


def update_stock_from_paid_invoice(inv, user, request):
    """
    Update stock for a sales invoice when payment is completed and stock is managed on payment.
    Uses delivered notes if present, otherwise consumes invoice items from the company's default warehouse.
    """
    if is_stock_management_on_delivery(request=request):
        return False

    db = getattr(request, 'company_db', 'default')
    updated = False

    # Apply stock for already delivered notes first.
    delivered_notes = inv.delivery_notes.filter(status='delivered', stock_updated=False).select_related('warehouse')
    for delivery_note in delivered_notes:
        try:
            delivery_note.update_stock()
            updated = True
        except Exception:
            logger.exception(
                'Failed to update stock from delivered SalesDeliveryNote %s',
                getattr(delivery_note, 'delivery_note_number', None)
            )

    if updated:
        # If any delivered notes were updated, we still need to process any remaining
        # invoice quantity that has not been covered by stock-updated delivery notes.
        pass

    warehouse = get_company_default_warehouse(request)
    if not warehouse:
        logger.error(
            'No warehouse found for company db %s when updating stock for paid invoice %s',
            db,
            getattr(inv, 'inv_number', None)
        )
        return False

    with transaction.atomic():
        for item in inv.items.all():
            product = item.product
            if not getattr(product, 'track_inventory', False):
                continue

            delivered_qty = SalesDeliveryNoteItem.objects.using(db).filter(
                invoice_item=item,
                delivery_note__status='delivered',
                delivery_note__stock_updated=True
            ).aggregate(total=Sum('quantity_delivered'))['total'] or Decimal('0')

            remaining_qty = Decimal(item.quantity or 0) - Decimal(delivered_qty or 0)
            if remaining_qty <= 0:
                continue

            stock, _ = Stock.objects.using(db).get_or_create(
                item=product,
                warehouse=warehouse,
                batch_number=None,
                serial_number=None,
                defaults={'quantity': Decimal('0.00')}
            )

            if stock.quantity < remaining_qty:
                logger.warning(
                    'Stock for %s going negative. Available: %s, Required: %s',
                    product.name, stock.quantity, remaining_qty
                )

            old_quantity = stock.quantity
            stock.quantity -= remaining_qty
            stock.save()

            SalesStockMovement.objects.create(
                stock=stock,
                movement_type='out',
                quantity=remaining_qty,
                reference_type='sales_invoice_payment',
                reference_id=inv.id,
                delivery_note=None,
                notes=f"Outward from Sales Invoice Payment {inv.inv_number}"
            )
            updated = True

    return updated


# Create your views here.
# def quotation_add(request):
#     customers = Customer.objects.all()
#     items = Item.objects.all()
#     return render(request, 'sales/quotation_add.html', {
#         "customers": customers,
#         "items": items,
#         "today": localdate().isoformat(),
#         "q_no": f"SQ-{SalesQuotation.objects.count() + 1:05d}"
#     })

def quotation_add(request):
    # Permission: require Create on Quotations
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_quotations(request.user)):
            messages.error(request, 'You do not have permission to create Quotations.')
            return redirect_with_company('sales_quote_list')
    except Exception:
        messages.error(request, 'You do not have permission to create Quotations.')
        return redirect_with_company('sales_quote_list')

    # Get company for currency field
    try:
        cid = request.session.get('company_id')
        company = None
        if cid:
            company = Company.objects.filter(pk=cid).first()
        if not company:
            company = Company.objects.order_by('id').first()
    except Exception:
        company = None
    resolved_company = _get_company_for_request(request)
    resolved_company_tax_type = (str(getattr(resolved_company, 'tax_type', '') or '').strip().upper() if resolved_company else '')

    try:
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code') if company else Currency.objects.none()
    except Exception:
        company_currencies = Currency.objects.none()
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = (base_currency.code or '').strip() if base_currency else ''

    # Create form instance for SalesQuotation
    # If company setting enabled, preselect Cash customer
    try:
        db_alias = getattr(request, 'company_db', 'default')
        resolved_company = _get_company_for_request(request)
        auto_load = bool(getattr(resolved_company, 'auto_load_cash_customer', False)) if resolved_company else False
        cash_customer = None
        if auto_load:
            cash_customer = Customer.objects.using(db_alias).filter(customer_code__iexact='CASH').first()
        sp = _get_current_salesperson(request)
        initial = {'customer': cash_customer.pk if cash_customer else None}
        if sp:
            initial['sales_person'] = sp.pk
        quotation_form = SalesQuotationForm(company=company, initial=initial)
    except Exception:
        sp = _get_current_salesperson(request)
        initial = {}
        if sp:
            initial['sales_person'] = sp.pk
        quotation_form = SalesQuotationForm(company=company, initial=initial if initial else None)
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    # Create a formset for SalesQuotationItem if you plan multiple items
    SalesQuotationItemFormSet = formset_factory(SalesQuotationItemForm, extra=1)
    sales_formset = SalesQuotationItemFormSet()
    all_items = Item.objects.all()
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    
    # Setup currency context (matching invoice_add)
    company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    if not base_currency_symbol:
        base_currency_symbol = base_currency.code if base_currency and base_currency.code else '₹'
    base_currency_code = base_currency.code if base_currency else ''
    
    # If opened from a lead or opportunity, keep id so save can link back
    lead_id = request.GET.get('lead_id')
    opportunity_id = request.GET.get('opportunity_id')

    # Pass both to the template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()

    return render(request, 'sales/quotation_add.html', {
        'quotation_form': quotation_form,
        'item_formset': item_formset,
        'sales_formset': sales_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': f"SQ-{SalesQuotation.objects.count() + 1:05d}",
        'lead_id': lead_id,
        'opportunity_id': opportunity_id,
        'company_country': company_country,
        'company_is_india': company_is_india,
        'company_tax_type': resolved_company_tax_type,
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'base_currency': base_currency,
        'tds_tax_master_items': tds_tax_master_items,
        'tcs_tax_master_items': tcs_tax_master_items,
        'show_base_transaction_summary': bool(getattr(resolved_company, 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(resolved_company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(resolved_company, 'sales_rounding_increment', 0),
    })

#updt by neha on 6-02-26 for listing companies
def customer_search(request):
    query = request.GET.get('q', '').strip()
    results = []

    # Only return active customers
    base_qs = Customer.objects.filter(is_active=True, is_draft=False).annotate(
        fullname=Concat('first_name', Value(' '), 'last_name')
    )

    if query:
        customers = base_qs.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(company_name__icontains=query) |
            Q(fullname__icontains=query)
        )[:50]
    else:
        # If no query, return only the first 5 so the dropdown isn't dumping
        # a huge list as soon as it's opened; typing reveals up to 50 matches.
        customers = base_qs.order_by('customer_code')[:5]

    def _clean_part(val):
        if not val:
            return ''
        try:
            s = str(val).strip()
        except Exception:
            return ''
        return '' if s.lower() == 'none' else s

    results = []
    for c in customers:
        if c.company_name and str(c.company_name).strip().lower() != 'none':
            name = str(c.company_name).strip()
        else:
            first = _clean_part(c.first_name)
            last = _clean_part(c.last_name)
            name = f"{first} {last}".strip()
        payment_terms = getattr(c, 'payment_terms', None)
        results.append({
            "id": c.id,
            "name": name,
            "payment_terms_id": payment_terms.id if payment_terms else None,
            "payment_terms_name": payment_terms.name if payment_terms else "",
            "payment_terms_days": payment_terms.days if payment_terms else None,
        })

    return JsonResponse(results, safe=False)

#updt by neha on 31-01-26 to fetch shippping from customer if available
def customer_detail_ajax(request, pk):
    """Return JSON details for a customer used by quotation/order/invoice forms.

    Returns billing address, shipping address (only if exists),
    GST number and place_of_supply (state).
    """
    try:
        c = Customer.objects.get(pk=pk)
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Customer not found'}, status=404)
    exclude_invoice_id = request.GET.get('exclude_invoice')

    customer_name = (c.company_name or getattr(c, 'fullname', '') or str(c)).strip()
    if not customer_name:
        customer_name = f"{(c.first_name or '').strip()} {(c.last_name or '').strip()}".strip()

    def country_name(country_val):
        if not country_val:
            return ''
        try:
            return country_val.name or str(country_val)
        except Exception:
            return str(country_val)

    billing = {
        'name': customer_name,
        'address_line_1': c.address_line_1 or '',
        'address_line_2': c.address_line_2 or '',
        'city': c.city or '',
        'state': c.state or '',
        'postal_code': c.postal_code or '',
        'country': country_name(c.country),
        'gst_number': c.gst_number or '',
        'email': c.email or '',
        'phone': c.phone or c.mobile or '',
    }

    # — Check if customer has actual shipping details
    has_shipping_details = any([
        c.shipping_address_line_1,
        c.shipping_address_line_2,
        c.shipping_city,
        c.shipping_state,
        c.shipping_postal_code,
        c.shipping_country
    ])
    print("has_shipping_details:",has_shipping_details)
    # Only populate shipping if customer has shipping details saved
    if has_shipping_details:
        shipping = {
            'name': customer_name,
            'email': c.email or '',
            'phone': c.phone or c.mobile or '',
            'shipping_address_line_1': c.shipping_address_line_1 or '',
            'shipping_address_line_2': c.shipping_address_line_2 or '',
            'shipping_city': c.shipping_city or '',
            'shipping_state': c.shipping_state or '',
            'shipping_postal_code': c.shipping_postal_code or '',
            'shipping_country': country_name(c.shipping_country),
        }
    else:
        shipping = None

    data = {
        'id': c.id,
        'billing': billing,
        'shipping': shipping,
        'place_of_supply': c.state or '',
        'credit': get_customer_credit_snapshot(c, exclude_invoice=exclude_invoice_id),
        'payment_terms': {
            'id': c.payment_terms_id,
            'name': c.payment_terms.name if c.payment_terms else '',
            'days': c.payment_terms.days if c.payment_terms else None,
        } if c.payment_terms_id else None,
        'is_cash_customer': (getattr(c, 'customer_code', '') or '').strip().upper() == 'CASH',
    }
    print("shipping data:",shipping)

    return JsonResponse(data)


def _to_decimal(value, default='0.00'):
    try:
        if value in (None, ''):
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)

def _resolve_selected_tax(selection_value):
    selection_value = (selection_value or '').strip()
    if not selection_value:
        return Decimal('0.00'), None

    tax_group = None
    tax_obj = None

    if selection_value.startswith('group:'):
        tax_group = TaxGroup.objects.filter(
            id=selection_value.split(':', 1)[1]
        ).prefetch_related('taxes').first()
    elif selection_value.startswith('tax:'):
        tax_obj = Tax.objects.filter(id=selection_value.split(':', 1)[1]).select_related('taxtype').first()
    else:
        tax_group = TaxGroup.objects.filter(id=selection_value).prefetch_related('taxes').first()
        if not tax_group:
            tax_obj = Tax.objects.filter(id=selection_value).select_related('taxtype').first()

    if tax_group:
        total_rate = Decimal(sum(t.rate for t in tax_group.taxes.all()))
        return total_rate, tax_group.group_name

    if tax_obj:
        return Decimal(tax_obj.rate or 0), (getattr(tax_obj, 'display_name', None) or tax_obj.taxname)

    return Decimal('0.00'), None

def _build_selected_tax_token(tax_group=None, tax_obj=None):
    if tax_group:
        return f"group:{tax_group.id}"
    if tax_obj:
        return f"tax:{tax_obj.id}"
    return None


def _normalize_tax_selection_token(selection_value, company_is_india=None):
    selection_value = (selection_value or '').strip()
    if not selection_value:
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
    # If we cannot resolve the selection to a TaxGroup/Tax, treat it as empty.
    # This prevents formset validation from failing with "Select a valid choice".
    return ''

def _normalize_item_tax_tokens(post_data, company_is_india=None):
    for key in list(post_data.keys()):
        if key.startswith('form-') and key.endswith('-prd_tax'):
            post_data[key] = _normalize_tax_selection_token(post_data.get(key), company_is_india)
    return post_data


def _get_company_for_tax():
    return Company.objects.order_by('id').first()


def _calculate_tax_for_invoice_item(item, customer, company, tax_override=None):
    product = getattr(item, 'product', None) or item
    # Always compute journal/tax source amounts in document currency.
    # `o_price` may hold base-currency value for FX documents; using it here
    # causes tax/sales amounts to be scaled to base twice during posting.
    unit_price = _to_decimal(
        getattr(item, 'price', None) or getattr(item, 'o_price', None),
        default='0.00'
    )
    quantity = getattr(item, 'quantity', 0) or 0
    tax_inclusive = getattr(product, 'taxincld_slprice', False)
    if calculate_tax:
        return calculate_tax(
            product,
            customer,
            company,
            quantity,
            unit_price=unit_price,
            tax_override=tax_override,
            tax_inclusive=tax_inclusive,
            raise_on_missing=False,
        )

    base_amount = (unit_price * Decimal(quantity)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    tax_rate = Decimal(str(item.prd_tax or 0))
    tax_amount = (base_amount * tax_rate / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {
        'total_rate': tax_rate,
        'tax_name': getattr(item, 'prd_taxgroup', '') or '',
        'total_tax': tax_amount,
        'base_amount': base_amount,
        'breakdown': [{
            'name': getattr(item, 'prd_taxgroup', '') or 'Tax',
            'rate': tax_rate,
            'amount': tax_amount,
        }],
    }


def get_customer_outstanding_amount(customer, exclude_invoice=None):
    if not customer:
        return Decimal('0.00')
    outstanding = _to_decimal(getattr(customer, 'opening_balance', 0))
    invoices = SalesInvoice.objects.filter(customer=customer)
    if exclude_invoice:
        exclude_id = exclude_invoice.pk if hasattr(exclude_invoice, 'pk') else exclude_invoice
        invoices = invoices.exclude(pk=exclude_id)

    for invoice in invoices.only('id', 'total_amount'):
        total_paid = InvPaymentAllocation.objects.filter(inv=invoice).aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0.00')
        remaining = _to_decimal(invoice.total_amount) - _to_decimal(total_paid)
        if remaining > Decimal('0.00'):
            outstanding += remaining

    return outstanding.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def get_customer_credit_snapshot(customer, invoice_amount=Decimal('0.00'), exclude_invoice=None):
    if not customer:
        return {
            'credit_limit': None,
            'outstanding_amount': '0.00',
            'projected_outstanding': str(_to_decimal(invoice_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'available_credit': None,
            'invoice_amount': str(_to_decimal(invoice_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'is_limit_exceeded': False,
            'exceeded_by': '0.00',
        }
    credit_limit = getattr(customer, 'credit_limit', None)
    if credit_limit in (None, ''):
        credit_limit = None
    outstanding = get_customer_outstanding_amount(customer, exclude_invoice=exclude_invoice)
    invoice_amount = _to_decimal(invoice_amount)

    snapshot = {
        'credit_limit': None,
        'outstanding_amount': str(outstanding),
        'projected_outstanding': str((outstanding + invoice_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
        'available_credit': None,
        'invoice_amount': str(invoice_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
        'is_limit_exceeded': False,
        'exceeded_by': '0.00',
    }

    if credit_limit is None:
        return snapshot

    credit_limit = _to_decimal(credit_limit)
    projected = (outstanding + invoice_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    available = (credit_limit - outstanding).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    exceeded_by = max(projected - credit_limit, Decimal('0.00')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    snapshot.update({
        'credit_limit': str(credit_limit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
        'available_credit': str(available),
        'is_limit_exceeded': projected > credit_limit,
        'exceeded_by': str(exceeded_by),
    })
    return snapshot


def build_customer_credit_limit_message(customer, invoice_amount, exclude_invoice=None):
    if not customer:
        return None
    snapshot = get_customer_credit_snapshot(
        customer,
        invoice_amount=invoice_amount,
        exclude_invoice=exclude_invoice,
    )
    if not snapshot['is_limit_exceeded']:
        return None

    customer_name = (customer.company_name or f"{customer.first_name or ''} {customer.last_name or ''}".strip() or customer.customer_code)
    return (
        f"Credit limit exceeded for {customer_name}. "
        f"Credit limit: Rs. {snapshot['credit_limit']}, "
        f"current outstanding: Rs. {snapshot['outstanding_amount']}, "
        f"this invoice: Rs. {snapshot['invoice_amount']}, "
        f"exceeded by: Rs. {snapshot['exceeded_by']}."
    )


def credit_limit_block_response(request, message, redirect_name, **redirect_kwargs):
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': False, 'error': message}, status=400)
    messages.error(request, message)
    return redirect_with_company(redirect_name, **redirect_kwargs)

def create_customer_ajax(request):
    # Try to default currency to organisation base
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

    # Allow optional name prefill from GET (used when opening modal via AJAX with a typed name)
    initial = {}
    try:
        name_prefill = (request.GET.get('name') or '').strip()
        if name_prefill:
            initial['company_name'] = name_prefill
    except Exception:
        name_prefill = ''

    form = CustomerForm(request.POST or None, initial=initial or None, company=company)
    # Build a simple country list for the template: list of (code, name)
    try:
        country_list = list(countries)
    except Exception:
        country_list = []

    context = {'customer_form': form, 'country_list': country_list, 'initial_name': name_prefill}
    return render(request, 'sales/add_customer.html', context)

def create_saleperson_ajax(request):
    form = SalesPersonForm(request.POST)
    context = {'salesperson_form': form}
    # add other context as needed
    return render(request, 'sales/add_salesperson.html', context)


#updt by neha on 2-2-26
def add_customer(request):
    
    if request.method == "POST":
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

        form = CustomerForm(request.POST, company=company)
        if form.is_valid():
            customer = form.save(commit=False)
            customer.is_active = True  # Set default active status for new customers
            # if not customer.postal_code:
            #     customer.postal_code = ''  # or generate a default
            # if not customer.opening_balance:
            #     customer.opening_balance = 0
            # if not customer.city:
            #     customer.city = ''
            # if not customer.state:
            #     customer.state = ''
            # if not customer.country:
            #     customer.country = ''

            try:
                customer.created_by = request.user
                customer.updated_by = request.user
            except Exception:
                pass
            # Ensure currency from the form (ModelChoice) is stored as the currency code
            try:
                sel_currency = form.cleaned_data.get('currency')
                if sel_currency:
                    # sel_currency may be a Currency instance; store its code
                    if hasattr(sel_currency, 'code'):
                        customer.currency = sel_currency.code.strip()
                    else:
                        # Could be a PK or string; try to resolve
                        try:
                            cur = Currency.objects.filter(pk=sel_currency).first()
                            if cur:
                                customer.currency = cur.code.strip()
                            else:
                                # try by code
                                cur = Currency.objects.filter(code__iexact=str(sel_currency)).first()
                                if cur:
                                    customer.currency = cur.code.strip()
                                else:
                                    customer.currency = str(sel_currency).strip()
                        except Exception:
                            customer.currency = str(sel_currency).strip()
                else:
                    # fallback: check raw POST value
                    raw_cur = request.POST.get('currency')
                    if raw_cur:
                        try:
                            cur = Currency.objects.filter(pk=raw_cur).first()
                            if cur:
                                customer.currency = cur.code.strip()
                            else:
                                cur = Currency.objects.filter(code__iexact=raw_cur).first()
                                if cur:
                                    customer.currency = cur.code.strip()
                                else:
                                    customer.currency = raw_cur.strip()
                        except Exception:
                            customer.currency = raw_cur.strip()
            except Exception:
                pass

            # Default customer's currency to organisation base if not provided
            try:
                cid = request.session.get('company_id')
                company = None
                if cid:
                    company = Company.objects.filter(pk=cid).first()
                if not company:
                    company = Company.objects.order_by('id').first()
                if company and (not getattr(customer, 'currency', '') or str(customer.currency).strip() == ''):
                    customer.currency = (company.base_currency or '').strip().upper()[:10]
            except Exception:
                pass
            customer.save()

            # If is_vendor checked, create vendor record
            if getattr(customer, 'is_vendor', False) and getattr(customer, 'email', None):
                try:
                    from Purchase.models import Vendor
                    vendor_exists = Vendor.objects.filter(email=customer.email).exists()
                    if not vendor_exists:
                        Vendor.objects.create(
                            vendor_code=getattr(customer, 'customer_code', ''),
                            vendor_type=getattr(customer, 'customer_type', ''),
                            first_name=customer.first_name,
                            last_name=customer.last_name,
                            company_name=getattr(customer, 'company_name', ''),
                            email=customer.email,
                            phone=customer.phone,
                            mobile=getattr(customer, 'mobile', ''),
                            address_line_1=getattr(customer, 'address_line_1', ''),
                            address_line_2=getattr(customer, 'address_line_2', ''),
                            city=getattr(customer, 'city', ''),
                            state=getattr(customer, 'state', ''),
                            postal_code=getattr(customer, 'postal_code', ''),
                            country=getattr(customer, 'country', ''),
                            tax_number=getattr(customer, 'gst_number', ''),
                            is_active=getattr(customer, 'is_active', True),
                            is_customer=True,
                            created_by=request.user,
                            updated_by=request.user,
                        )
                except Exception:
                    logger.exception('Failed to create vendor record for customer via AJAX')

            # Build display name safely and treat literal 'None' as empty
            def _clean_part(v):
                if not v:
                    return ''
                try:
                    s = str(v).strip()
                except Exception:
                    return ''
                return '' if s.lower() == 'none' else s

            if customer.customer_type == 'company':
                comp = _clean_part(customer.company_name)
                display_name = comp or _clean_part(customer.email) or str(customer.id)
            else:  # individual
                first = _clean_part(getattr(customer, 'first_name', ''))
                last = _clean_part(getattr(customer, 'last_name', ''))
                display_name = (' '.join([p for p in (first, last) if p])).strip() or _clean_part(customer.email) or str(customer.id)

            return JsonResponse({'success': True, 'id': customer.id, 'name': display_name})
        else:
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

def add_salesperson(request):
    if request.method == "POST":
        form = SalesPersonForm(request.POST)
        if form.is_valid():
            salesPersons = form.save()  # saving new vendor
            return JsonResponse({'success': True, 'id': salesPersons.id, 'name': salesPersons.name})
        else:
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

def sale_person_search(request):
    query = request.GET.get('q', '')
    if query:
        salesPersons = SalesPerson.objects.filter(name__icontains=query)[:50]
    else:
        # If no query, show only the first 5 on open; typing reveals up to 50 matches.
        salesPersons = SalesPerson.objects.all()[:5]

    results = [{"id": h.id, "name": h.name} for h in salesPersons]
    return JsonResponse(results, safe=False)


# def save_salesquote(request):
#     if request.method == 'POST':
#         form = SalesQuotationForm(request.POST)
        
#         # Parse item details from POST manually (since custom naming like items[0][id])
#         items_data = []
#         for key in request.POST:
#             if key.startswith('items['):
#                 import re
#                 m = re.match(r'items\[(\d+)\]\[(.+)\]', key)
#                 if m:
#                     idx, field = m.groups()
#                     while len(items_data) <= int(idx):
#                         items_data.append({})
#                     items_data[int(idx)][field] = request.POST.get(key)
#                     #print("items_data:",items_data)

        

#         if form.is_valid():
#             #print("Inside POST handling", flush=True)
#             logger.info("Inside POST handling")
#             total_amount = Decimal('0.00')
#             for item in items_data:
#                 quantity = int(item.get('qty', 1))
#                 price = Decimal(item.get('price', 0))
#                 total_amount += quantity * price

#             sales_quotation = form.save(commit=False)

#             # Set missing required fields
#             sales_quotation.total_amount = total_amount
#             if not sales_quotation.status:
#                 sales_quotation.status = 'DRAFT'  # default status value

#             sales_quotation.save()

#             # Clear existing items
#             SalesQuotationItem.objects.filter(Sales_quotation=sales_quotation).delete()

#             # Save quotation items
#             for item in items_data:
#                 product_id = item.get('id', '').split('_')[0]
#                 quantity = int(item.get('qty', 1))
#                 price = Decimal(item.get('price', 0))

#                 SalesQuotationItem.objects.create(
#                     Sales_quotation=sales_quotation,
#                     product_id=product_id,
#                     quantity=quantity,
#                     price=price,
#                 )

#             return redirect('quotation_add')
#         else:
#             #print("Form is not valid", flush=True)
#             #print("Form errors:", form.errors, flush=True)
#             logger.error(f"Form errors: {form.errors}")
#             # Handle form errors if needed
#             return render(request, 'sales/quotation_add.html', {'quotation_form': form, 'purchase_items': items_data})
#     else:
#         form = SalesQuotationForm()
#         return render(request, 'sales/quotation_add.html', {'quotation_form': form})

def get_item_sales(request):
    query = request.GET.get('q', '')
    results = []
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    company_db = getattr(request, "company_db", "default")
    company_tax_type = (get_company_tax_type(using=company_db) or "").strip().upper()
    # Check if query is a barcode, get matching item ids
    matching_item_ids = Barcode.objects.filter(barcode=query).values_list('item_id', flat=True)

    # Filter items by name or barcode match. name__icontains('') matches
    # everything, so this already covers the "dropdown just opened, no query
    # yet" case — just show fewer results (5) until the user starts typing,
    # then allow up to 50 matches.
    limit = 50 if query else 5
    items = Item.objects.filter(Q(name__icontains=query) | Q(id__in=matching_item_ids),sales_info=1,status=1).distinct()[:limit]

    for h in items:
        # price = Decimal(h.cost_price)
        price = Decimal(h.selling_price) 

        o_price = Decimal(h.selling_price) 
        total_tax_rate = Decimal("0.00")

        tax_name = ""
        selected_tax_token = None
        if company_tax_type == "SALES":
            tax_obj = getattr(h, "sales_tax", None)
            if tax_obj:
                total_tax_rate = Decimal(tax_obj.rate or 0)
                selected_tax_token = tax_obj.id
                tax_name = getattr(tax_obj, "display_name", None) or getattr(tax_obj, "taxname", "") or ""
        else:
            tax_group = h.intra_tax
            if tax_group:
                total_tax_rate = tax_group.taxes.aggregate(total=Sum("rate"))["total"] or Decimal("0.00")
                selected_tax_token = _build_selected_tax_token(tax_group=tax_group)
                tax_name = tax_group.group_name or ""
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
            barcode = barcode_obj.barcode
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
            if h.taxincld_slprice:
                # tax_amount = price * (total_tax_rate / Decimal("100"))
                # adjusted_price = price - tax_amount
                gst_multiplier = Decimal("1") + (total_tax_rate / Decimal("100"))
                adjusted_price = (price / gst_multiplier).quantize(Decimal("0.000001"))

            results.append({
                "id": h.id,
                "name": h.name,
                "o_price": float(adjusted_price.quantize(Decimal("0.000001"))),
                "sl_price": float(adjusted_price.quantize(Decimal("0.000001"))),
                "unit": unit_name,
                "barcode": barcode,
                "b_id": barcode_id,
                "sell_desc": h.sales_desc,
                'gstinclude': h.taxincld_slprice,
                "tax_id": selected_tax_token,
                "tax_name": tax_name,
                "tax_rate": float(total_tax_rate),
                "tax_pref":h.tax_pref,
            })

        # Add matched UOMs
        for uom in item_uoms:
            price_uom = price * Decimal(uom.conversion_factor)
            # Adjust price for GST if needed
            adjusted_price_uom = price_uom
            if h.taxincld_slprice:
                gst_multiplier = Decimal("1") + (total_tax_rate / Decimal("100"))
                adjusted_price_uom = (price_uom / gst_multiplier).quantize(Decimal("0.000001"))

            results.append({
                "id": f"{h.id}",
                # "name": f"{h.name} ({uom.name_id})",
                "name": f"{h.name}",

                "o_price": float(adjusted_price_uom.quantize(Decimal("0.000001"))),

                "sl_price": float(adjusted_price_uom.quantize(Decimal('0.000001'))),
                "unit": uom.name.name,
                "barcode": uom.barcode.barcode if uom.barcode else '',
                "b_id": uom.barcode.id if uom.barcode else None,
                "sell_desc": h.sales_desc,
                'gstinclude': h.taxincld_slprice,
                "tax_id": selected_tax_token,
                "tax_name": tax_name,
                "tax_rate": float(total_tax_rate),
                "tax_pref":h.tax_pref,




            })

    # #print(results)

    return JsonResponse(results, safe=False)

# def generate_quote_number():
#     # Get prefix from DB, or fallback to default e.g. 'QN'
#     prefix_obj = QuotePrefix.objects.first()
#     prefix = prefix_obj.prefix if prefix_obj else "QN"

#     last_quote = SalesQuotation.objects.aggregate(max_num=Max('quote_number'))
#     last_number = 0

#     if last_quote['max_num']:
#         # Extract numeric part after prefix
#         num_part = ''.join(filter(str.isdigit, last_quote['max_num']))
#         last_number = int(num_part) if num_part.isdigit() else 0

#     return f"{prefix}{str(last_number + 1).zfill(3)}"

def generate_quote_number():
    prefix_obj = QuotePrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "QN"

    # Fetch all quote_numbers
    all_quotes = SalesQuotation.objects.values_list('quote_number', flat=True)

    max_number = 0
    pattern = re.compile(r'(\d+)')  # Extract digits anywhere in the string

    for q in all_quotes:
        match = pattern.search(q)
        if match:
            num = int(match.group(1))
            if num > max_number:
                max_number = num

    new_number = max_number + 1
    return f"{prefix}{str(new_number).zfill(3)}"


def generate_revised_quote_number(original_quote_number):
    # Extract base quote number without revision suffix (e.g. "SQN001" from "SQN001-R2")
    base_quote = re.sub(r'-R\d+$', '', original_quote_number)

    # Find all quotes with this base plus revision suffix, e.g. "SQN001-R1", "SQN001-R2"
    existing_revisions = SalesQuotation.objects.filter(
        quote_number__startswith=base_quote + '-R'
    ).order_by('-id')

    # Determine new revision number
    if existing_revisions.exists():
        last_quote_number = existing_revisions.first().quote_number
        match = re.search(r'-R(\d+)$', last_quote_number)
        last_rev_num = int(match.group(1)) if match else 0
        new_rev_num = last_rev_num + 1
    else:
        new_rev_num = 1

    # Create new quote number with revision suffix
    new_quote_number = f"{base_quote}-R{new_rev_num}"
    return new_quote_number


def generate_revised_order_number(original_order_number):
    # Extract base quote number without revision suffix (e.g. "SQN001" from "SQN001-R2")
    base_order = re.sub(r'-R\d+$', '', original_order_number)

    # Find all quotes with this base plus revision suffix, e.g. "SQN001-R1", "SQN001-R2"
    existing_revisions = SalesOrder.objects.filter(
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

    # Create new quote number with revision suffix
    new_order_number = f"{base_order}-R{new_rev_num}"
    return new_order_number

import json
@login_required
def sales_dashboard(request):
    # Permission: require View on Sales Dashboard
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_sales_dashboard(request.user)):
            messages.error(request, 'You do not have permission to view Sales Dashboard.')
            return redirect('/')
    except Exception:
        messages.error(request, 'You do not have permission to view Sales Dashboard.')
        return redirect('/')
    today = timezone.localdate()
    company_db = getattr(request, 'company_db', 'default')

    # Fine-grained permission flags used by dashboard sections/cards
    can_quotes = getattr(request.user, 'is_superuser', False) or can_view_quotations(request.user)
    can_orders = getattr(request.user, 'is_superuser', False) or can_view_orders(request.user)
    can_invoices = getattr(request.user, 'is_superuser', False) or can_view_invoices(request.user)
    can_payments = getattr(request.user, 'is_superuser', False) or can_view_payments_received(request.user)
    can_delivery = getattr(request.user, 'is_superuser', False) or can_view_delivery(request.user)
    can_returns = getattr(request.user, 'is_superuser', False) or can_view_returns(request.user)
 
    invoice_count = notpaid_invoices = paid_invoices = overdue_invoices = 0
    total_revenue = total_collected = total_outstanding = overdue_amount = 0
    recent_invoices = []
    try:
        from sales.models import SalesInvoice
        all_inv = SalesInvoice.objects.using(company_db).select_related(
            'payment_status', 'payment_term', 'customer'
        ).all()
        invoice_records = list(all_inv)
        invoice_numbers = [invoice.inv_number for invoice in invoice_records]
        invoice_bases = {
            re.sub(r'-R\d+$', '', invoice_number or '')
            for invoice_number in invoice_numbers
        }
        invoice_count    = len(invoice_bases)
        paid_invoices = sum(
            1 for invoice in invoice_records
            if getattr(getattr(invoice, 'payment_status', None), 'name', '').strip().lower() == 'paid'
        )
        notpaid_invoices = sum(
            1 for invoice in invoice_records
            if getattr(getattr(invoice, 'payment_status', None), 'name', '').strip().lower()
            in {'not paid', 'partially paid'}
        )

        overdue_records = []
        for invoice in invoice_records:
            payment_status = getattr(getattr(invoice, 'payment_status', None), 'name', '').strip().lower()
            payment_term = getattr(invoice, 'payment_term', None)
            payment_days = int(payment_term.days or 0) if payment_term else None
            print(f"Invoice {invoice.inv_number}: payment_status={payment_status}, payment_days={payment_days}")
            due_date = (
                invoice.date + timezone.timedelta(days=payment_days)
                if invoice.date and payment_days is not None
                else None
            )
            if payment_status in {'not paid', 'partially paid'} and due_date and due_date < today:
                overdue_records.append(invoice)
        print(f"Overdue records : overdue_records={overdue_records}")
        overdue_invoices = len(overdue_records)

        paid_invoice_numbers = [
            invoice.inv_number for invoice in invoice_records
            if getattr(getattr(invoice, 'payment_status', None), 'name', '').strip().lower() == 'paid'
        ]
        notpaid_invoice_numbers = [
            invoice.inv_number for invoice in invoice_records
            if getattr(getattr(invoice, 'payment_status', None), 'name', '').strip().lower()
            in {'not paid', 'partially paid'}
        ]
        overdue_invoice_numbers = [invoice.inv_number for invoice in overdue_records]
 
        total_revenue = sum((invoice.total_amount or 0) for invoice in invoice_records)
        total_collected = InvPaymentAllocation.objects.using(company_db).filter(
            inv__in=invoice_records
        ).aggregate(collected=Sum('amount'))['collected'] or 0
        total_outstanding = float(total_revenue) - float(total_collected)
 
        overdue_amount = sum((invoice.total_amount or 0) for invoice in overdue_records)
 
        recent_invoices = sorted(invoice_records, key=lambda invoice: invoice.date or today, reverse=True)[:8]
    except Exception:
        logger.exception('Failed to build Sales Dashboard invoice metrics')
 
    try:
        from sales.models import SalesOrder
        order_count = SalesOrder.objects.using(company_db).count()
    except Exception:
        order_count = 0
 
    #  Quotations 
    try:
        from sales.models import SalesQuotation
        quotation_count = SalesQuotation.objects.using(company_db).count()
    except Exception:
        quotation_count = 0
 
    try:
        from sales.models import InvPayment
        payment_count = InvPayment.objects.using(company_db).count()
    except Exception:
        payment_count = 0
    
    try:
        from sales.models import SalesDeliveryNote
        delivery_count = SalesDeliveryNote.objects.using(company_db).count()
    except Exception:
        delivery_count = 0
    
    try:
        from sales.models import SalesReturn
        return_count = SalesReturn.objects.using(company_db).count()
    except Exception:
        return_count = 0
 
    try:
        from customer.models import Customer
        top_customers = (
            Customer.objects.using(company_db)
            .annotate(total=Sum('salesinvoice__total_amount'))
            .filter(total__isnull=False)
            .order_by('-total')[:6]
        )
    except Exception:
        top_customers = []
 
    try:
        from sales.models import SalesInvoiceItem
        top_items = (
            SalesInvoiceItem.objects.using(company_db)
            .values('product__name')
            .annotate(qty=Sum('quantity'))
            .order_by('-qty')[:6]
        )
    except Exception:
        top_items = []
    print(f"Top items: {top_items}")
    chart_labels, chart_data = [], []
    try:
        for i in range(5, -1, -1):
            yr, m = today.year, today.month - i
            while m <= 0:
                m += 12
                yr -= 1
            start = date(yr, m, 1)
            next_start = date(yr + 1, 1, 1) if m == 12 else date(yr, m + 1, 1)
            month_total = (
                SalesInvoice.objects.using(company_db)
                .filter(date__gte=start, date__lt=next_start)
                .aggregate(t=Sum('total_amount'))['t'] or 0
            )
            chart_labels.append(start.strftime('%b %Y'))
            chart_data.append(float(month_total))
    except Exception:
        chart_labels, chart_data = [], []
    
 
    return render(request, 'sales/sales_dashboard.html', {
        # KPI counts
        'invoice_count':    invoice_count,
        'order_count':      order_count,
        'quotation_count':  quotation_count,
        'payment_count':    payment_count,
        'delivery_count':   delivery_count,
        'return_count':     return_count,
        # Invoice status
        'paid_invoices':    paid_invoices,
        'notpaid_invoices':    notpaid_invoices,
        'overdue_invoices': overdue_invoices,
        'paid_invoice_numbers': paid_invoice_numbers,
        'notpaid_invoice_numbers': notpaid_invoice_numbers,
        'overdue_invoice_numbers': overdue_invoice_numbers,
        # Revenue
        'total_revenue':     total_revenue,
        'total_collected':   total_collected,
        'total_outstanding': total_outstanding,
        'overdue_amount':    overdue_amount,
        # Tables
        'recent_invoices': recent_invoices,
        'top_customers':   top_customers,
        'top_items':       top_items,
        # Chart
        'chart_labels': json.dumps(chart_labels),
        'chart_data':   json.dumps(chart_data),
        # Permission flags
        'can_quotes': can_quotes,
        'can_orders': can_orders,
        'can_invoices': can_invoices,
        'can_payments': can_payments,
        'can_delivery': can_delivery,
        'can_returns': can_returns,
    })

def save_salesquote(request):
    if request.method == "POST":
        # Permission: require Create on Quotations
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_quotations(request.user)):
                messages.error(request, 'You do not have permission to create Quotations.')
                return redirect_with_company('sales_quote_list')
        except Exception:
            messages.error(request, 'You do not have permission to create Quotations.')
            return redirect_with_company('sales_quote_list')
        try:
            # wrap entire POST handling to catch and log unexpected errors
            
            
            post_data = request.POST.copy()  # make mutable copy
            prd_brcd_map = {}
        except Exception as e:
            logger.exception("Unexpected error in save_salesquote: %s", e)
            messages.error(request, "An unexpected error occurred while saving the quotation. See server log for details.")
            return redirect_with_company('sales_quote_list')
        
        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
        # post_data and prd_brcd_map were prepared above
        for key in post_data:
            # Identify product field keys
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                # #print(f"Key matched: {key} with value: '{value}'")
                if value:
                    parts = value.split("_", 1)
                    item_id = (parts[0] or '').strip()
                    # Only digits are valid Item PKs; anything else becomes empty.
                    if not item_id.isdigit():
                        post_data[key] = ''
                        continue

                    post_data[key] = item_id  # Save only item id for product field

                    if len(parts) > 1:
                        # Map barcode corresponding to this form prefix (used for prd_brcd).
                        barcode_part = (parts[1] or '').strip()
                        if barcode_part:
                            prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
                            prd_brcd_map[prefix] = barcode_part
        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        total_amount = request.POST.get('grandTotal')
        customer_id = request.POST.get('customer')
        date = request.POST.get('date')
        sales_person_id = request.POST.get('sales_person')
        place_of_supply_val = request.POST.get('place_of_supply', '')
        notes = request.POST.get('notes', '')
        quote_number = generate_quote_number()
        # #print("quote_number:",quote_number)
        try:
            discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
            discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_value = Decimal('0.00')
        discount_type = request.POST.get('discount_type', 'percent')
        
        # Capture shipping address fields
        shipping_attention = request.POST.get('shipping_attention', '')
        shipping_email = request.POST.get('shipping_email', '')
        shipping_phone = request.POST.get('shipping_phone', '')
        shipping_country = request.POST.get('shipping_country', '')
        shipping_address1 = request.POST.get('shipping_address1', '')
        shipping_address2 = request.POST.get('shipping_address2', '')
        shipping_city = request.POST.get('shipping_city', '')
        shipping_state = request.POST.get('shipping_state', '')
        shipping_postal_code = request.POST.get('shipping_postal_code', '')
        tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
        tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
        tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
        tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
        
        # Capture currency fields
        currency_id = request.POST.get('document_currency', '')
        fx_rate_str = request.POST.get('fx_rate_to_base', '')
        fx_rate_date = request.POST.get('fx_rate_date', '')

        if not customer_id or not date:
            messages.error(request, "Customer and Quotation Date are required.")
            return redirect_with_company('sales_quote_list')

        try:
            customer = Customer.objects.get(pk=customer_id)
            sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None
            company = _get_company_for_request(request)
            company_tax_type = _get_request_company_tax_type(request, company)
            turnover_tax_obj = None
            if company_tax_type == 'TURNOVER':
                turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                if turnover_tax_id:
                    turnover_tax_obj = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first()

            # Handle currency - use provided currency or customer's default currency
            document_currency = None
            fx_rate_to_base = Decimal('1.000000')
            
            if currency_id:
                try:
                    document_currency = Currency.objects.get(pk=currency_id)
                except Currency.DoesNotExist:
                    pass
            
            # If no currency provided, try to use customer's currency
            if not document_currency and customer.currency:
                try:
                    document_currency = Currency.objects.get(code__iexact=customer.currency)
                except Currency.DoesNotExist:
                    pass
            
            # Parse FX rate
            if fx_rate_str:
                try:
                    fx_rate_to_base = Decimal(str(fx_rate_str))
                except Exception:
                    fx_rate_to_base = Decimal('1.000000')

            # Create quote with zero total for now; we'll recalculate after items are processed
            quote = SalesQuotation.objects.create(
                customer=customer,
                date=date,
                sales_person=sales_person,
                quote_number=quote_number,
                total_amount=0,
                notes=notes,
                discount_value=discount_value,
                discount_type=discount_type,
                place_of_supply=place_of_supply_val,
                shipping_attention=shipping_attention,
                shipping_email=shipping_email,
                shipping_phone=shipping_phone,
                shipping_country=shipping_country,
                shipping_address1=shipping_address1,
                shipping_address2=shipping_address2,
                shipping_city=shipping_city,
                shipping_state=shipping_state,
                shipping_postal_code=shipping_postal_code,
                tds_tcs_type=tds_tcs_type,
                tds_tcs_definition_id=tds_tcs_definition_id,
                tds_tcs_rate=tds_tcs_rate,
                tds_tcs_amount=tds_tcs_amount,
                document_currency=document_currency,
                fx_rate_to_base=fx_rate_to_base,
                fx_rate_date=fx_rate_date if fx_rate_date else None,
                turnover_tax=turnover_tax_obj,
            )
            quote._current_user = request.user
            quote._current_request = request
            # Attach payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    quote.payment_term = pt
                    quote.save()
                except PayTerms.DoesNotExist:
                    pass

            # Link quotation to CRM opportunity if provided
            opportunity_id = request.POST.get('opportunity_id')
            if opportunity_id:
                try:
                    from crm.models import Opportunity, Update as CRMUpdate
                    opportunity = Opportunity.objects.get(pk=opportunity_id)
                    
                    # Create a CRM Update record linking the quotation
                    crm_update = CRMUpdate.objects.create(
                        opportunity=opportunity,
                        quotation=quote,
                        update_type='Internal Note',
                        description=f'Quotation {quote.quote_number} created for this opportunity.',
                        created_by=request.user,
                    )
                    
                    # Update opportunity status to "Quotation Created"
                    if opportunity.status != 'Quotation Created':
                        opportunity.status = 'Quotation Created'
                        opportunity.save()
                    
                    messages.success(request, f'Quotation linked to opportunity successfully!')
                except Opportunity.DoesNotExist:
                    logger.warning(f"Opportunity {opportunity_id} not found for quotation linkage")
                except Exception as e:
                    logger.warning(f"Error linking quotation to opportunity: {e}")

        except IntegrityError as e:
            if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
                messages.error(request, f"Quote Number '{quote_number}' already exists. Please choose a different one.")
            else:
                logger.exception("IntegrityError while creating SalesQuotation: %s", e)
                messages.error(request, "An error occurred while saving the quotation.")
            return redirect_with_company('sales_quote_list')
        except Exception as e:
            logger.exception("Unexpected exception while creating SalesQuotation header: %s", e)
            messages.error(request, "An unexpected error occurred while saving the quotation. See server log for details.")
            return redirect_with_company('sales_quote_list')

        SalesQuotationItemFormSet = modelformset_factory(
            SalesQuotationItem, form=SalesQuotationItemForm, extra=0, can_delete=True
        )

        formset = SalesQuotationItemFormSet(post_data, queryset=SalesQuotationItem.objects.none())

        if formset.is_valid():
            items = formset.save(commit=False)
            # initialize accumulators for journal posting
            # taxable_total = Decimal(0)
            # tax_totals = {}
            # initialize accumulators for journal posting
            taxable_total = Decimal(0)
            tax_totals = {}
            calculated_total = Decimal('0.00')
            saved_any = False
            for index, item in enumerate(items):
                #added for edit save
                item.pk = None

                prefix = f"form-{index}"              # formset form key pattern
                # Skip blank/invalid rows (common when one extra line exists but product wasn't chosen)
                if not getattr(item, 'product_id', None):
                    continue

                saved_any = True
                if prefix in prd_brcd_map:            # check if barcode was extracted
                    item.prd_brcd = prd_brcd_map[prefix]
                # capture HSN code from the hidden input posted by template
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                # capture HSN code from the hidden input posted by template
                # item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                
                tax_group_id = post_data.get(f'form-{index}-prd_tax', '').strip()

                # Initialize tax fields with defaults
                selected_tax = post_data.get(f'form-{index}-prd_tax', '')
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                try:
                    item.o_price = Decimal(str(post_data.get(f'{prefix}-o_price', '0') or '0'))
                except (InvalidOperation, ValueError, TypeError):
                    item.o_price = Decimal('0.00')
                item.Sales_quotation = quote          # set foreign key
                item.save()

                # if tax_group_id:
                #     try:
                #         tax_group = TaxGroup.objects.filter(id=tax_group_id).prefetch_related('taxes').first()
                #         if tax_group:
                #             total_rate = Decimal(sum(t.rate for t in tax_group.taxes.all()))
                #             item.prd_tax = total_rate
                #             item.prd_taxgroup = tax_group.group_name
                #         # else:
                #         #     item.prd_tax = Decimal(0)
                #         #     item.prd_taxgroup = 
                #     except Exception as e:
                #         logger.warning(f"Error processing tax group {tax_group_id} for item {index}: {e}")
                # # else:
                # #     item.prd_tax = Decimal(0)
                # #     item.prd_taxgroup = None
                # item.Sales_quotation = quote          # set foreign key
                # item.save()                
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

            if not saved_any:
                # Avoid saving an empty quotation header when all rows were blank.
                quote.delete()
                messages.error(request, "Please add at least one valid item in the quotation.")
                return redirect_with_company('sales_quote_list')
            # --- FIX: Subtract grand discount from calculated_total ---
            try:
                discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
                grand_discount_value = Decimal(discount_raw)
            except (InvalidOperation, ValueError, TypeError):
                grand_discount_value = Decimal('0.00')
            discount_type = request.POST.get('discount_type', 'percent')
            if discount_type == 'percent':
                grand_discount = calculated_total * grand_discount_value / Decimal('100')
            else:
                grand_discount = grand_discount_value
            if grand_discount > calculated_total:
                grand_discount = calculated_total
            final_total = calculated_total - grand_discount
            if company_tax_type == 'TURNOVER':
                turnover_rate = Decimal(str(getattr(turnover_tax_obj, 'rate', 0) or 0))
                if turnover_rate:
                    final_total += (final_total * turnover_rate / Decimal('100'))

            tds_tcs_signed_amount = tds_tcs_amount if tds_tcs_type == 'tcs' else -tds_tcs_amount
            final_total += tds_tcs_signed_amount
            if final_total < Decimal('0.00'):
                final_total = Decimal('0.00')

            try:
                posted_grand_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
                if posted_grand_total not in (None, ''):
                    raw_total_amount = Decimal(str(posted_grand_total))
                else:
                    raw_total_amount = final_total
            except (InvalidOperation, ValueError, TypeError):
                raw_total_amount = final_total

            rounding_company = _get_company_for_request(request)
            quote.total_amount, quote.round_off = apply_sales_rounding(raw_total_amount, rounding_company)

            quote.total_amount_base = (
                Decimal(str(quote.total_amount or 0)) * Decimal(str(quote.fx_rate_to_base or 1))
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            
            quote.discount_value = grand_discount_value
            quote.discount_type = discount_type
            quote.save()

            messages.success(request, "Sales Quotation created successfully!")
            
            # Confirm draft customer when quotation is saved
            customer_id = request.POST.get('customer')
            if customer_id:
                try:
                    draft_customer = Customer.objects.get(pk=customer_id, is_draft=True)
                    draft_customer.is_draft = False
                    draft_customer.save()
                    logger.info(f"Customer {draft_customer.customer_code} confirmed from draft")
                except Customer.DoesNotExist:
                    pass  # Already confirmed or not a draft
                except Exception as e:
                    logger.warning(f"Error confirming draft customer {customer_id}: {e}")

            # If this quotation was created from a lead or opportunity, create a CRM Update linking them
            lead_id = request.POST.get('lead_id')
            opportunity_id = request.POST.get('opportunity_id')
            if lead_id or opportunity_id:
                try:
                    from crm.models import Opportunity, Lead, Update
                    if lead_id:
                        lead = Lead.objects.filter(pk=lead_id).first()
                        if lead:
                            Update.objects.create(
                                lead=lead,
                                quotation=quote,
                                update_type='Internal Note',
                                description=f'Quotation {quote.quote_number} created from lead {lead.customer_name}',
                                created_by=request.user
                            )
                    if opportunity_id:
                        opportunity = Opportunity.objects.filter(pk=opportunity_id).first()
                        if opportunity:
                            Update.objects.create(
                                opportunity=opportunity,
                                quotation=quote,
                                update_type='Internal Note',
                                description=f'Quotation {quote.quote_number} created from opportunity',
                                created_by=request.user
                            )
                except Exception:
                    logger.exception('Failed to create CRM Update linking quotation to lead/opportunity')

            # Redirect to quotation detail page after save
            try:
                url = reverse('quotation_detail', args=[quote.pk])
                return redirect_with_company(url)
            except Exception:
                return redirect_with_company('sales_quote_list')
        else:
            # # #print("Formset errors:", formset.errors)
            # for form in formset:
            #     #print("Individual form errors:", form.errors)
            logger.error("SalesQuotation formset invalid: %s", formset.errors)
            # Prevent saving a half-created quotation header with total_amount=0
            try:
                quote.delete()
            except Exception:
                logger.exception("Failed to delete SalesQuotation after invalid formset")

            # Show a compact message based on first validation error
            first_field = None
            first_messages = None
            try:
                # formset.errors returns list[dict] or dict depending on Django version
                errs = formset.errors
                if isinstance(errs, list):
                    for form_err in errs:
                        if isinstance(form_err, dict) and form_err:
                            first_field = next(iter(form_err.keys()))
                            first_messages = form_err.get(first_field)
                            break
                elif isinstance(errs, dict) and errs:
                    first_field = next(iter(errs.keys()))
                    first_messages = errs.get(first_field)
            except Exception:
                pass

            if first_field and first_messages:
                try:
                    msg_text = ", ".join(first_messages)
                except Exception:
                    msg_text = str(first_messages)
                messages.error(
                    request,
                    f"There are errors with the items in the quotation. First issue: {first_field}: {msg_text}"
                )
            else:
                messages.error(request, "There are errors with the items in the quotation. See server log for details.")
            return redirect_with_company('sales_quote_list')
    else:
        return redirect_with_company('sales_quote_list')


# def sales_quote_list(request):
#     search_query = request.GET.get('q', '').strip()
#     page_size = int(request.GET.get('page_size', 10))

#     sales_quote_qs = SalesQuotation.objects.all().order_by('-id')

#     if search_query:
#         sales_quote_qs = sales_quote_qs.filter(
#             Q(salesperson__name__icontains=search_query) |
#             Q(id__icontains=search_query) |
#             Q(status__icontains=search_query) |
#             Q(total_amount__icontains=search_query)
#         )
#     total_count = sales_quote_qs.count()  # Total matching purchase orders

#     paginator = Paginator(sales_quote_qs,  page_size)
#     page_number = request.GET.get('page')
#     sales_quotes = paginator.get_page(page_number)

#     context = {
#         'sales_quotes': sales_quotes,
#         'search_query': search_query,
#         'total_count': total_count,
#         'page_size': page_size,
#     }

#     if request.headers.get('x-requested-with') == 'XMLHttpRequest':
#         # Render only the table and pagination (partial)
#         return render(request, 'sales/sales_quote_list.html', context)

#     # Full render for normal request
#     return render(request, 'sales/sales_quote_list.html', context)


def sales_quote_list(request):
    # Check if user has Sales Quotation permission
    from user.utils import has_permission
    from Lyraerp.utils.redirect_utils import redirect_with_company
    
    if not has_permission(request.user, 'Sales Quotation', 'View'):
        return redirect_with_company('/')
    
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    sales_quote_qs = SalesQuotation.objects.all().order_by('-id')

    if search_query:
        sales_quote_qs = sales_quote_qs.filter(
            Q(sales_person__name__icontains=search_query) |
            Q(customer__first_name__icontains=search_query) |

            # Q(id__icontains=search_query) |
            Q(quote_number__icontains=search_query) |

            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )

    if status_filter:
        sales_quote_qs = sales_quote_qs.filter(status=status_filter)

    # Grouping logic
    # Assume quotation_number like "QT123-R1", "QT123-R2", "QT456"
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_quotes = defaultdict(list)

    for quote in sales_quote_qs:
        match = pattern.match(quote.quote_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_quotes[base_number].append((revision_num, quote))

    # Select latest revision per base_number
    latest_quotes = []
    revisions_dict = {}

    for base_number, rev_list in grouped_quotes.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_quotes.append(latest)

    # Paginate latest quotations
    paginator = Paginator(latest_quotes, page_size)
    page_number = request.GET.get('page')
    sales_quotes = paginator.get_page(page_number)

    total_count = len(latest_quotes)

    context = {
        'sales_quotes': sales_quotes,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': total_count,
        'page_size': page_size,
        'revisions_dict': revisions_dict,  # Pass older revisions mapped by latest quote id
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'sales/sales_quote_list.html', context)

    return render(request, 'sales/sales_quote_list.html', context)


def quotation_detail(request, pk):
    """Render a simple readonly detail page for a quotation."""

    # Check if user has Sales Quotation permission
    from user.utils import has_permission
    from Lyraerp.utils.redirect_utils import redirect_with_company
    
    if not has_permission(request.user, 'Sales Quotation', 'View'):
        return redirect_with_company('/')
    
    quote = get_object_or_404(SalesQuotation, pk=pk)
    # Use a simple formset to iterate items if needed in template
    SalesQuotationItemFormSet = modelformset_factory(SalesQuotationItem, form=SalesQuotationItemForm, extra=0)
    existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
    sales_formset = SalesQuotationItemFormSet(queryset=existing_items_qs)
    # Compute per-line amounts and totals (mirror save logic)
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
            # Flat discount is total for the line row
            discount_amount = discount_val
            
        if discount_amount > base:
            discount_amount = base

        discounted = base - discount_amount
        if discounted < Decimal('0'):
            discounted = Decimal('0.00')


        # if discounted < 0:
        #     discounted = Decimal('0.00')

        # Calculate discount amount for display
        discount_amount = base - discounted

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        # — NEW: line_total is ONLY the discounted amount (no tax)
        line_total = discounted
        # line_total = discounted + tax_amount

        # — NEW: Subtotal accumulates discounted amounts (before tax)
        subtotal_calc += discounted
        # subtotal_calc += line_total
        total_tax += tax_amount
        total_item_discount += discount_amount

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'o_price': getattr(item, 'o_price', None),
            'base': base,
            'discount_peritem':discount_val,

            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
            'returned_qty': 0,
        })

    # Split tax equally into CGST/SGST for display (simple assumption)
    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')


    # — NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax

    # Grand discount (quote-level)
    grand_discount_value = Decimal(str(quote.discount_value or 0))
    grand_discount_type = quote.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount  * grand_discount_value) / Decimal('100')

        # grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > total_before_discount:
        grand_discount = total_before_discount

    # if grand_discount > subtotal_calc:
    #     grand_discount = subtotal_calc

    # final_total = subtotal_calc - grand_discount
    # — NEW: Final total = (subtotal + tax) - grand_discount
    final_total = total_before_discount  - grand_discount

    total_discount_combined = total_item_discount + grand_discount
    

    # — NEW: Currency handling for quotations - calculate base currency amounts
    company = _get_company_for_request(request)
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    # Document / quotation currency
    doc_currency = quote.document_currency
    if doc_currency:
        doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip()
        doc_currency_code = doc_currency.code
    else:
        # Use base currency if document currency not set
        doc_currency_symbol = base_currency_symbol
        doc_currency_code = base_currency_code

    # Whether the document currency and company base currency are the same.
    # When true, the template shows a single (base) currency section instead
    # of separate document/base columns for price and totals.
    same_currency = True
    if doc_currency and base_currency:
        same_currency = (doc_currency.pk == base_currency.pk) or (
            bool(doc_currency_code) and bool(base_currency_code)
            and doc_currency_code.strip().upper() == base_currency_code.strip().upper()
        )
    elif doc_currency and not base_currency:
        same_currency = False

    # FX rate from document currency to base (quotation.fx_rate_to_base)
    fx_rate = Decimal(quote.fx_rate_to_base or Decimal('1'))
    
    tds_tcs_type = (quote.tds_tcs_type or '').strip().lower()
    tds_tcs_amount = Decimal(str(quote.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate

    subtotal_calc_base = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    total_item_discount_base = Decimal('0.00')
    for itm in items_info:
        price = Decimal(itm.get('price') or 0)
        qty = Decimal(itm.get('quantity') or 0)
        price_base = Decimal(itm.get('o_price') or 0) or (price * fx_rate)
        discount_value = Decimal(itm.get('discount_peritem') or 0)
        discount_type = itm.get('discount_type') or 'flat'
        line_base = price_base * qty
        if discount_type == 'percent':
            discount_amount_base = (line_base * discount_value) / Decimal('100')
        else:
            discount_amount_base = discount_value * fx_rate
        if discount_amount_base > line_base:
            discount_amount_base = line_base
        line_total_base = line_base - discount_amount_base
        tax_rate = Decimal(itm.get('tax_rate') or 0)
        tax_amount_base = (line_total_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        itm['price_base'] = price_base
        itm['line_total_base'] = line_total_base
        itm['discount_amount_base'] = discount_amount_base
        itm['tax_amount_base'] = tax_amount_base

        subtotal_calc_base += line_total_base
        total_tax_base += tax_amount_base
        total_item_discount_base += discount_amount_base

    total_cgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')
    total_sgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')

    total_before_discount_base = subtotal_calc_base + total_tax_base
    if grand_discount_type == 'percent':
        grand_discount_base = (total_before_discount_base * grand_discount_value) / Decimal('100')
    else:
        grand_discount_base = grand_discount_value * fx_rate
    if grand_discount_base > total_before_discount_base:
        grand_discount_base = total_before_discount_base

    final_total_base = total_before_discount_base - grand_discount_base



    
    total_discount_combined_base = total_item_discount_base + grand_discount_base
    company_tax_type = _get_request_company_tax_type(request, company)
    
    
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER' and getattr(quote, 'turnover_tax', None):
        turnover_rate = Decimal(str(getattr(quote.turnover_tax, 'rate', 0) or 0))
        if turnover_rate:
            turnover_tax_amount = (final_total * turnover_rate) / Decimal('100')
            turnover_tax_amount_base = (final_total_base * turnover_rate) / Decimal('100')

    tds_tcs_signed_amount_base = tds_tcs_amount_base if tds_tcs_type == 'tcs' else -tds_tcs_amount_base
    tds_tcs_signed_amount = tds_tcs_amount if tds_tcs_type == 'tcs' else -tds_tcs_amount
    final_total += turnover_tax_amount + tds_tcs_signed_amount
    final_total_base += turnover_tax_amount_base + tds_tcs_signed_amount_base
    if final_total < Decimal('0.00'):
        final_total = Decimal('0.00')
    if final_total_base < Decimal('0.00'):
        final_total_base = Decimal('0.00')

    # Get older revisions (same logic as sales_quote_list)
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    
    # Find all quotes with same base number
    base_match = pattern.match(quote.quote_number)
    if base_match:
        base_number = base_match.group('base')
        all_revisions = SalesQuotation.objects.filter(
            quote_number__startswith=base_number
        ).exclude(pk=pk).order_by('-id')
        
        # Filter only actual revisions (matching the pattern)
        older_revisions = []
        for rev_quote in all_revisions:
            rev_match = pattern.match(rev_quote.quote_number)
            if rev_match and rev_match.group('base') == base_number:
                older_revisions.append(rev_quote)
    else:
        older_revisions = []

    # Get follow-ups for this quotation
    from crm.models import FollowUp
    followups = FollowUp.objects.filter(quotation=quote).order_by('-followup_date')

    context = {
        'quotation_form': SalesQuotationForm(instance=quote),
        'sales_formset': sales_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': quote.quote_number,
        'quote': quote,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax': total_tax,
        'total_tax_base': total_tax_base,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst_base,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst_base,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'final_total_base': final_total_base,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'total_discount_combined': total_discount_combined,
        'total_discount_combined_base': total_discount_combined_base,
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,

        'followups': followups,
        'older_revisions': older_revisions,
        'has_revisions': len(older_revisions) > 0,
        'current_revision': quote.quote_number,
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': company_tax_type,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'same_currency': same_currency,
        'fx_rate': fx_rate,
        'fx_rate_to_base': fx_rate,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),

    }
    return render(request, 'sales/quotation_detail.html', context)

def quotation_print_view(request, pk):
    """Render a printable HTML page for the quotation (for printing in browser)."""
    context = build_quotation_context(pk, request)
    return render(request, 'sales/quotation_print.html', context)


def generate_quotation_pdf_bytes(pk, request=None):
    """Generate PDF bytes for quotation using ReportLab - can be used for display or email."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    
    quote = get_object_or_404(SalesQuotation, pk=pk)
    context = build_quotation_context(pk, request)
    
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
                          title=f'Quotation {pk}',
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
    
    quote_badge_style = ParagraphStyle(
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
    #updt by neha on 20-1-26 to fetch company details from db
    company = Company.objects.filter(status=True).first() or Company.objects.first()
    # HEADER with professional styling
    legal_name = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name_value = legal_name or ((getattr(company, 'name', '') or '').strip() if company else '')
    company_name = safe(company_name_value)
    header_left_cell = Paragraph(f"<b>{company_name}</b>", title_style)
    show_logo_in_pdf = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
    try:
        if (
            show_logo_in_pdf
            and company
            and getattr(company, 'logo', None)
            and company.logo.path
            and os.path.exists(company.logo.path)
        ):
            header_left_cell = Image(company.logo.path, width=45*mm, height=14*mm)
    except Exception:
        pass

    
    header_data = [
        [header_left_cell, Paragraph("QUOTATION", quote_badge_style)],
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
    #edited by neha on 20-1-26
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
    
    sales_person_obj = getattr(quote, 'sales_person', None)
    sales_person_name = (getattr(sales_person_obj, 'name', None) or '').strip()
    sales_person_phone = (getattr(sales_person_obj, 'phone', None) or '').strip()
    if sales_person_name and sales_person_phone:
        sales_person_display = f"{sales_person_name} ({sales_person_phone})"
    elif sales_person_name:
        sales_person_display = sales_person_name
    else:
        sales_person_display = '-'
    meta_data = [
        [Paragraph("<b>Quote No</b>", meta_label_style), Paragraph(str(context.get('q_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(quote.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(quote.place_of_supply or '-'), meta_value_style),
         Paragraph("<b>Sales Person</b>", meta_label_style), Paragraph(str(sales_person_display), meta_value_style)],
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
    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if quote.customer:
        bill_lines = []
        customer_type = (getattr(quote.customer, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(quote.customer, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(quote.customer, 'first_name', '') or '').strip()
            last_name = (getattr(quote.customer, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)

        if getattr(quote.customer, 'address_line_1', None):
            bill_lines.append(quote.customer.address_line_1)

        # Use customer's state (billing address)
        state = getattr(quote.customer, 'state', None)
        if state:
            bill_lines.append(state)

        # Use customer's country (billing address)
        country = getattr(quote.customer, 'country', None)
        if country:
            bill_lines.append(str(country))

        gst = getattr(quote.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if quote.shipping_address1 or quote.shipping_city:
        ship_lines = []
        
        if getattr(quote, 'shipping_attention', None):
            ship_lines.append(f"Attention To: {quote.shipping_attention}")
        
        if getattr(quote, 'shipping_address1', None):
            ship_lines.append(quote.shipping_address1)
        
        if getattr(quote, 'shipping_address2', None):
            ship_lines.append(quote.shipping_address2)
        
        if getattr(quote, 'shipping_city', None):
            city_line = quote.shipping_city
            if getattr(quote, 'shipping_postal_code', None):
                city_line += f" - {quote.shipping_postal_code}"
            ship_lines.append(city_line)
        
        if getattr(quote, 'shipping_state', None):
            ship_lines.append(quote.shipping_state)
        
        if getattr(quote, 'shipping_country', None):
            ship_lines.append(str(quote.shipping_country))
        
        if getattr(quote, 'shipping_email', None):
            ship_lines.append(f"Email: {quote.shipping_email}")
        
        if getattr(quote, 'shipping_phone', None):
            ship_lines.append(f"Phone: {quote.shipping_phone}")
        
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
    
    # ITEMS TABLE - Professional styling with conditional VAT/CGST-SGST
    currency = context.get('document_currency_symbol') or ("₹" if font_registered else "Rs.")
    base_currency = context.get('company_base_currency_symbol') or ("₹" if font_registered else "Rs.")
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', '')
    
    print("Company Tax Type for quotation:", company_tax_type)  # Debug print
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{float(turnover_amt):.2f}", amount_style)])
    
    # Conditional table headers based on country
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
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        
        base_price_str = ""
        tax_base_str = ""
        sub_base_str = ""

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
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
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
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) - Decimal(str(item.get('tax_amount', 0)))
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    totals_value_style = ParagraphStyle(
        'TotalValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    
    totals_style_final = ParagraphStyle(
        'TotalFinal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#333333'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    
    # Always show Sub Total and item-level tax totals (CGST/SGST for India, VAT for others)
    if company_tax_type in ('GST', 'VAT', 'SALES', 'TURNOVER'):
        totals_data = [

            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('subtotal_calc', 0):.2f}", totals_value_style)],
        ]
    elif company_tax_type == 'NONE':
        totals_data = []
    
    if company_tax_type == 'GST':
        totals_data.append([Paragraph("CGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_cgst', 0):.2f}", totals_value_style)])
        totals_data.append([Paragraph("SGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_sgst', 0):.2f}", totals_value_style)])
    elif company_tax_type in ('VAT', 'SALES'):
        totals_data.append([Paragraph("Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_tax', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'TURNOVER':
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('turnover_tax_amount', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'NONE':
        pass  # No tax rows for NONE type
    # Add round off if it exists
    quote_obj = context.get('quote')
    round_off_value = getattr(quote_obj, 'round_off', None) if quote_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}", amount_style)])
    # Show Turnover Tax row only for companies registered with TURNOVER tax type
    company_tax_type = context.get('company_tax_type', '')
    print("Company Tax Type:", company_tax_type)  # Debug print
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{float(turnover_amt):.2f}", amount_style)])
    

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
    quote_obj = context.get('quote')
    notes_text = (getattr(quote_obj, 'notes', None) or '').strip() or '-'
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
    
    # FOOTER with page numbers on each page
    elements.append(Spacer(1, 5*mm))
    
    # Build PDF with footer
    class FooterCanvas(Canvas):
        def __init__(self, *args, **kwargs):
            Canvas.__init__(self, *args, **kwargs)
            self.pages = []
        
        def showPage(self):
            self.pages.append(dict(self.__dict__))
            self._startPage()
        
        def save(self):
            page_count = len(self.pages)
            for page_num, page in enumerate(self.pages, 1):
                self.__dict__.update(page)
                self.draw_footer(page_num, page_count)
                Canvas.showPage(self)
            Canvas.save(self)
        
        def draw_footer(self, page_num, total_pages):
            self.setFont(font_name, 6)
            self.setFillColor(colors.grey)
            self.drawString(30, 20, f"POWERED BY LyraERP")
            self.drawRightString(570, 20, f"Page {page_num} of {total_pages}")
    
    doc.build(elements, canvasmaker=FooterCanvas)
    
    # Get PDF value
    pdf = buffer.getvalue()
    buffer.close()
    
    return pdf


def generate_payment_receipt_pdf_bytes(payment_id):
    """Generate a styled payment receipt PDF for an InvPayment, matching PO PDF style."""
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

    payment = get_object_or_404(InvPayment, pk=payment_id)

    # — Font registration ======================================================
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
    currency = (payment.get_currency_symbol() or '').strip()
    if not currency:
        currency = '\u20B9' if font_registered else 'Rs.'
    elif currency == '\u20B9' and not font_registered:
        currency = 'Rs.'

    # — Document setup =========================================================
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=10*mm, leftMargin=10*mm,
        topMargin=10*mm,   bottomMargin=10*mm,
        title=f'Payment Receipt {payment.payment_number}',
        author='LyraERP',
    )

    styles   = getSampleStyleSheet()
    elements = []

    # — Shared paragraph styles ================================================
    def _ps(name, base='Normal', **kw):
        kw.setdefault('fontName', font_name)   # only set font_name if caller didn't supply one
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

    # — Company ================================================================
    company      = Company.objects.filter(status=True).first() or Company.objects.first()
    legal_name   = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name = safe(legal_name or (getattr(company, 'name', '') or '').strip() if company else '')

    # — HEADER =================================================================
    header_left = Paragraph(f'<b>{company_name}</b>', title_style)
    show_logo   = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
    try:
        if show_logo and company and getattr(company, 'logo', None) and os.path.exists(company.logo.path):
            header_left = Image(company.logo.path, width=45*mm, height=14*mm)
    except Exception:
        pass

    header_table = Table(
        [[header_left, Paragraph('Payment Receipt', badge_style)]],
        colWidths=[310, 210],
    )
    header_table.setStyle(TableStyle([
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',         (1, 0), (1,  -1), 'RIGHT'),
        ('BORDER',        (0, 0), (-1, -1), 1,   colors.HexColor('#e0e0e0')),
        ('LINEWIDTH',     (0, 0), (-1, -1), 1.5),
        ('TOPPADDING',    (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
        ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))

    # — Company info block =====================================================
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

    # — Meta info (4-column grid) ==============================================
    customer_name = (
        payment.customer.company_name
        if getattr(payment.customer, 'customer_type', '') == 'company'
        else f"{payment.customer.first_name or ''} {payment.customer.last_name or ''}".strip()
    )

    meta_data = [
        [
            Paragraph('<b>Receipt No:</b>',    meta_label_style),
            Paragraph(str(payment.payment_number), meta_value_style),
            Paragraph('<b>Payment Date:</b>',  meta_label_style),
            Paragraph(str(payment.payment_date), meta_value_style),
        ],
        [
            Paragraph('<b>Customer:</b>',      meta_label_style),
            Paragraph(customer_name or '-',    meta_value_style),
            Paragraph('<b>Paid Through:</b>',  meta_label_style),
            Paragraph(getattr(payment.paid_through, 'name', '-'), meta_value_style),
        ],
        [
            Paragraph('<b>Reference:</b>',     meta_label_style),
            Paragraph(payment.reference or '-', meta_value_style),
            Paragraph('',                      meta_label_style),
            Paragraph('',                      meta_value_style),
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

    # — Amount highlight box ===================================================
    amt_table = Table(
        [[
            Paragraph('<b>Amount Received</b>', _ps('AmtLabel',
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

    # — Allocations table =====================================================
    allocations = payment.inv_allocations.select_related('inv').all()
    if allocations.exists():
        alloc_rows = [[
            Paragraph('<b>#</b>',                label_style),
            Paragraph('<b>Invoice No.</b>',      label_style),
            Paragraph('<b>Invoice Date</b>',     label_style),
            Paragraph('<b>Amount Allocated</b>', label_style),
        ]]
        for idx, a in enumerate(allocations, 1):
            inv      = a.inv
            inv_no   = getattr(inv, 'inv_number', str(inv.pk))
            inv_date = getattr(inv, 'date', '')
            alloc_rows.append([
                Paragraph(str(idx),      value_style),
                Paragraph(f'#{inv_no}',  value_style),
                Paragraph(str(inv_date), value_style),
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

        # Totals block
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
            Paragraph('<b>Amount Received</b>', _ps('GrandTotalLabel',
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
            ('BACKGROUND',    (0, 0),  (-1, -2), colors.HexColor('#ffffff')),
            ('BACKGROUND',    (0, -1), (-1, -1), colors.HexColor('#f8f9fa')),
            ('GRID',          (0, 0),  (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ]))
        elements.append(totals_table)
        elements.append(Spacer(1, 5*mm))

    # — Notes & signature ======================================================
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

    # — Footer ================================================================
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

def download_payment_receipt(request, payment_id):
    """View to download payment receipt PDF for a payment."""
    pdf_bytes = generate_payment_receipt_pdf_bytes(payment_id)
    payment = get_object_or_404(InvPayment, pk=payment_id)
    filename = f"payment_{payment.payment_number}.pdf"
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

def quotation_pdf_view(request, pk):
    """Generate a PDF for the quotation using ReportLab with proper rupee symbol support."""
    quote = get_object_or_404(SalesQuotation, pk=pk)
    pdf = generate_quotation_pdf_bytes(pk, request)
    
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="quotation_{quote.quote_number}.pdf"'
    return response

@transaction.atomic
def duplicate_quotation(request, pk):
    original = get_object_or_404(SalesQuotation, pk=pk)

    # clone header
    original.pk = None          # or original.id = None
    original.quote_number = generate_quote_number()  # or generate a new number here
    # quote_number = generate_quote_number()

    original.status = 'Draft'     # usually new quote goes to Draft
    original.save()
    new_quote = original

    # clone items
    items = SalesQuotationItem.objects.filter(Sales_quotation=pk)
    for item in items:
        item.pk = None
        item.Sales_quotation = new_quote
        item.save()
    messages.success(request, "Sales Quotation created successfully!")
    return redirect_with_company('quotation_detail', pk=new_quote.pk)

def build_quotation_context(pk, request=None):
    quote = get_object_or_404(SalesQuotation, pk=pk)
    #added by neha on 20-1-26
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    
    # Currency and FX setup
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    company_base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else ''
    company_base_currency_code = base_currency.code if base_currency else ''

    doc_currency = getattr(quote, 'document_currency', None)
    doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip() if doc_currency else ''
    doc_currency_code = doc_currency.code if doc_currency else ''

    fx_rate = Decimal(str(getattr(quote, 'fx_rate_to_base', 1) or 1))

    existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
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

        # — NEW: line_total is ONLY the discounted amount (no tax)
        line_total = discounted
        # — NEW: Subtotal accumulates discounted amounts (before tax)
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
            'price_base': price * fx_rate,
            'base': base,
            'discount_amount': discount_amount,
            'discount_amount_base': discount_amount * fx_rate,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'tax_amount_base': tax_amount * fx_rate,
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'taxable_value': discounted,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')

    # — NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax

    grand_discount_value = Decimal(str(quote.discount_value or 0))
    grand_discount_type = quote.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > total_before_discount:
        grand_discount = total_before_discount

    # — NEW: Final total = (subtotal + tax) - grand_discount
    final_total = total_before_discount - grand_discount

    # Turnover tax: if company uses TURNOVER tax type, derive the turnover amount
    company_tax_type = _get_request_company_tax_type(request, company) if request is not None else (getattr(company, 'tax_type', '') or '').upper()
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = final_total
        stored_final_total = Decimal(str(quote.total_amount or pre_turnover_total))
        turnover_tax_amount = stored_final_total - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')
        final_total = stored_final_total

        pre_turnover_total_base = (total_before_discount * fx_rate) - (grand_discount * fx_rate)
        stored_final_total_base = Decimal(str(getattr(quote, 'total_amount_base', None) or (stored_final_total * fx_rate)))
        turnover_tax_amount_base = stored_final_total_base - pre_turnover_total_base
        if turnover_tax_amount_base < Decimal('0.00'):
            turnover_tax_amount_base = Decimal('0.00')

    context = {
        'q_no': quote.quote_number,
        'quote': quote,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc * fx_rate,
        'total_tax': total_tax,
        'total_tax_base': total_tax * fx_rate,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst * fx_rate,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst * fx_rate,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_base': grand_discount * fx_rate,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'final_total_base': final_total * fx_rate,
        'total_discount_combined': total_item_discount + grand_discount,
        # Currency data
        'fx_rate': fx_rate,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        #added by neha on 20-1-26
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'company_is_india': _is_indian_company_country(company.country.code if hasattr(company, 'country') and hasattr(company.country, 'code') else ''),
        'company_tax_type': company_tax_type,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }
    return context


@csrf_exempt
def send_message(request, pk):
    """Send Email for a SalesQuotation similar to HR.send_message.

    Expects POST and query param `method=email`.
    """
    try:
        if request.method != 'POST':
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        for m in methods:
            if m not in ("email",):
                return JsonResponse({"success": False, "message": f"Invalid method '{m}'"}, status=400)

        quote = SalesQuotation.objects.select_related('customer').filter(pk=pk).first()
        if not quote:
            return JsonResponse({"success": False, "message": "Quotation not found"}, status=404)

        # Build context for placeholders and for rendering print template
        context = build_quotation_context(pk, request)

        # Company details if available
        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        placeholder_ctx = {
            'customer': f"{getattr(quote.customer, 'first_name', '')} {getattr(quote.customer, 'last_name', '')}".strip(),
            'quote_number': quote.quote_number,
            'date': quote.date.strftime("%d %B %Y") if getattr(quote, 'date', None) else '',
            'total': str(quote.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
        }
        # alias `employee` to customer for templates that use [[employee]]
        placeholder_ctx['employee'] = placeholder_ctx['customer']

        messages_sent = []

        # EMAIL
        if 'email' in methods:
            # Prefer EmailTemplateStyle named for quotation if exists
            template_style = None
            subject = None
            message_html = None
            try_names = ["Quatation Details", "Quotation Details", "Quotation Email", "Sales Quotation", "Quotation", "Sales Quotation Email"]
            from email_templates.models import EmailTemplateStyle
            for tn in try_names:
                # First try to find a default one (by template_name choice)
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
                # Prepare subject and body from DB template
                subject = replace_placeholders(template_style.subject or f"Quotation {quote.quote_number}", placeholder_ctx)
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)

                # allow template to control PDF attachment via marker [[attach_pdf:true]] or [[attach_pdf:false]]
                attach_pdf_flag = True
                try:
                    m = re.search(r"\[\[attach_pdf:(true|false)\]\]", message_html, flags=re.I)
                    if m:
                        attach_pdf_flag = m.group(1).lower() == 'true'
                        message_html = re.sub(r"\[\[attach_pdf:(?:true|false)\]\]", '', message_html, flags=re.I)
                    # remove any visual attach-option block inserted for template previews
                    message_html = re.sub(r'<div\s+class\s*=\s*"attach-option"[\s\S]*?<\/div>', '', message_html, flags=re.I)
                except Exception:
                    attach_pdf_flag = True

                # sanitize template HTML
                try:
                    message_html = re.sub(r'style="[^"]*(?:background(?:-color)?|color)[^"]*"', '', message_html, flags=re.I)
                    message_html = re.sub(r'<font[^>]*>', '', message_html, flags=re.I)
                    message_html = re.sub(r'</font>', '', message_html, flags=re.I)
                except Exception:
                    pass

                # Determine email configuration: prefer one matching the template name
                email_config = EmailConfiguration.objects.filter(usage_types__icontains=template_style.template_name, status=True).first()
                if not email_config:
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                if not email_config:
                    return JsonResponse({"success": False, "message": f"No email configuration found for quotation."}, status=400)

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    recipient = quote.shipping_email or (getattr(quote.customer, 'email', None) if quote.customer else None)
                    if not recipient:
                        return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                    # Generate PDF for attachment if needed
                    pdf_bytes = None
                    try:
                        if attach_pdf_flag:
                            # Use new ReportLab-based PDF generation
                            pdf_bytes = generate_quotation_pdf_bytes(pk, request)
                    except Exception:
                        pdf_bytes = None

                    from_email = email_config.default_from_email or email_config.host_user
                    plain_body = f"Please find attached quotation {quote.quote_number}."
                    msg = EmailMultiAlternatives(
                        subject or f"Quotation {quote.quote_number}",
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
                        filename = f'quotation_{quote.pk}.pdf'
                        msg.attach(filename, pdf_bytes, 'application/pdf')

                    msg.send(fail_silently=False)

                    quote.status = 'Sent'
                    quote.save(update_fields=['status'])
                    messages_sent.append(f"Email sent to {recipient} successfully!")
                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
            else:
                # Try hardcoded default templates from email_templates if DB template not found
                from email_templates.views import get_default_email_template
                default_template = get_default_email_template("Quotation")
                if default_template:
                    subject = replace_placeholders(default_template.get("subject", f"Quotation {quote.quote_number}"), placeholder_ctx)
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

                    # Determine email configuration: prefer one matching 'Quotation'
                    email_config = EmailConfiguration.objects.filter(usage_types__icontains="Quotation", status=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True).first()
                    if not email_config:
                        return JsonResponse({"success": False, "message": f"No email configuration found for quotation."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        recipient = quote.shipping_email or (getattr(quote.customer, 'email', None) if quote.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        # Generate PDF for attachment if enabled
                        pdf_bytes = None
                        try:
                            if attach_pdf_flag:
                                # Use new ReportLab-based PDF generation
                                pdf_bytes = generate_quotation_pdf_bytes(pk, request)
                        except Exception:
                            pdf_bytes = None

                        from_email = email_config.default_from_email or email_config.host_user
                        plain_body = f"Please find attached quotation {quote.quote_number}."
                        msg = EmailMultiAlternatives(
                            subject or f"Quotation {quote.quote_number}",
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
                            filename = f'quotation_{quote.pk}.pdf'
                            msg.attach(filename, pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)

                        quote.status = 'Sent'
                        quote.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    # Final fallback: render print template and use default email configs
                    try:
                        full_bill_html = render_to_string('sales/quotation_print.html', context)
                        recipient = quote.shipping_email or (getattr(quote.customer, 'email', None) if quote.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                        if not email_config:
                            email_config = EmailConfiguration.objects.filter(status=True).first()
                        if not email_config:
                            return JsonResponse({"success": False, "message": "No email configuration found."}, status=400)

                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        # Try to create PDF for attachment
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
                        plain_body = f"Please find attached quotation {quote.quote_number}."
                        msg = EmailMultiAlternatives(
                            subject or f"Quotation {quote.quote_number}",
                            plain_body,
                            from_email,
                            [recipient],
                            connection=connection
                        )

                        if pdf_bytes:
                            filename = f'quotation_{quote.pk}.pdf'
                            msg.attach(filename, pdf_bytes, 'application/pdf')
                        else:
                            try:
                                html_bytes = full_bill_html.encode('utf-8')
                                msg.attach(f'quotation_{quote.pk}.html', html_bytes, 'text/html')
                            except Exception:
                                pass

                        try:
                            if 'message_html' in locals() and message_html:
                                msg.attach_alternative(message_html, 'text/html')
                        except Exception:
                            pass

                        msg.send(fail_silently=False)

                        quote.status = 'Sent'
                        quote.save(update_fields=['status'])
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


@csrf_exempt
def generate_order_pdf_bytes(pk, request=None):
    """Generate PDF bytes for order using ReportLab - can be used for display or email."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfgen.canvas import Canvas
    
    order = get_object_or_404(SalesOrder, pk=pk)
    context = build_order_context(pk, request)
    
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
                          title=f'Sales Order {pk}',
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
    #added on 20-1-26 neha
    # company = Company.objects.filter(status=True).first()
    company = Company.objects.filter(status=True).first() or Company.objects.first() #edit by sisira -28/2
    if company is None:
        all_companies = Company.objects.all().values('id', 'name', 'status')
        logger.error(f"[PDF] No active company found. All records: {list(all_companies)}")
        raise ValueError(f"No active company found in DB. Records: {list(all_companies)}")
    # HEADER with professional styling
    header_data = [
        [Paragraph(f"<b>{safe(company.name)}</b>", title_style),
         Paragraph("ORDER", order_badge_style)],
    ]
    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    # COMPANY INFO - Professional styling 
    #added on 20-1-26 neha
    company_header_style.leftIndent = 10 
    
    company_info = f"""
    <font color='#2c3e50'><b>{safe(company.name)}</b></font><br/>
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
    meta_data = [
        [Paragraph("<b>Order No</b>", meta_label_style), Paragraph(str(context.get('o_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(str(order.date), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(order.place_of_supply or '-'), meta_value_style), '', ''],
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
    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if order.customer:
        bill_lines = []

        name = f"{order.customer.first_name} {order.customer.last_name}".strip()
        if name:
            bill_lines.append(name)

        if getattr(order.customer, 'address_line_1', None):
            bill_lines.append(order.customer.address_line_1)

        if getattr(order.customer, 'state', None):
            bill_lines.append(order.customer.state)

        if getattr(order.customer, 'country', None):
            bill_lines.append(str(order.customer.country))

        gst = getattr(order.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if order.shipping_address1 or order.shipping_city:
        ship_lines = []
        
        if getattr(order, 'shipping_attention', None):
            ship_lines.append(f"Attention To: {order.shipping_attention}")
        
        if getattr(order, 'shipping_address1', None):
            ship_lines.append(order.shipping_address1)
        
        if getattr(order, 'shipping_address2', None):
            ship_lines.append(order.shipping_address2)
        
        if getattr(order, 'shipping_city', None):
            city_line = order.shipping_city
            if getattr(order, 'shipping_postal_code', None):
                city_line += f" - {order.shipping_postal_code}"
            ship_lines.append(city_line)
        
        if getattr(order, 'shipping_state', None):
            ship_lines.append(order.shipping_state)
        
        if getattr(order, 'shipping_country', None):
            ship_lines.append(str(order.shipping_country))
        
        if getattr(order, 'shipping_email', None):
            ship_lines.append(f"Email: {order.shipping_email}")
        
        if getattr(order, 'shipping_phone', None):
            ship_lines.append(f"Phone: {order.shipping_phone}")
        
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
    
    # ITEMS TABLE - Professional styling with conditional VAT/CGST-SGST
    currency = context.get('document_currency_symbol') or ('₹' if font_registered else 'Rs.')
    base_currency = context.get('company_base_currency_symbol') or ('₹' if font_registered else 'Rs.')
    fx_rate = context.get('fx_rate', 1)
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', '')
    print("Company Tax Type for order:", company_tax_type)  # Debug print
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{float(turnover_amt):.2f}", amount_style)])
    
    # Conditional table headers based on country
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
    elif company_tax_type == 'VAT' or company_tax_type == 'TURNOVER':
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
             Paragraph("<b>Item & Description</b>", label_style),
             Paragraph("<b>HSN/SAC</b>", label_style),
             Paragraph("<b>Qty</b>", label_style),
             Paragraph("<b>Rate</b>", label_style),
             Paragraph("<b>Tax Rate</b>", label_style),
             Paragraph("<b>Tax Amount</b>", label_style)]
        ]
    else :
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
             Paragraph("<b>Item & Description</b>", label_style),
             Paragraph("<b>HSN/SAC</b>", label_style),
             Paragraph("<b>Qty</b>", label_style),
             Paragraph("<b>Rate</b>", label_style),
            ]
        ]
    
    for idx, item in enumerate(context.get('items_info', []), 1):
        tax_rate = Decimal(str(item.get('tax_rate', 0)))
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        
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
        elif company_tax_type == 'VAT' or company_tax_type == 'TURNOVER':
            # Show single VAT column for non-India
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}", amount_style),
                Paragraph(f"{tax_rate:.2f}%", amount_style),
                Paragraph(f"{currency}\u00A0{float(tax_amount):.2f}", amount_style),
            ])
        else :
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
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
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
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) - Decimal(str(item.get('tax_amount', 0)))
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    if company_tax_type == 'GST':
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
            [Paragraph("CGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_cgst', 0):.2f}", amount_style)],
            [Paragraph("SGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_sgst', 0):.2f}", amount_style)],
        ]
    elif company_tax_type == 'VAT' or company_tax_type == 'TURNOVER':
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
            [Paragraph("Tax", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_tax', 0):.2f}", amount_style)],
        ]
    else :
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}", amount_style)],
           
        ]
    # Add Round Off and Turnover Tax rows when applicable
    order_obj = context.get('order')
    round_off_value = getattr(order_obj, 'round_off', None) if order_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}", amount_style)])
    company_tax_type = context.get('company_tax_type', '')
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{float(turnover_amt):.2f}", amount_style)])
    
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
    notes_text = order_obj.notes if order_obj and order_obj.notes else "Prices are exclusive of any additional duties or levies, if applicable."
    bottom_data = [
        [Paragraph(f"<b>Notes / Terms & Conditions</b><br/><font size=6>{notes_text}</font>", value_style),
         Paragraph(f"<b>Authorized Signatory</b><br/><br/>For {safe(company.name)}", value_style)]
    ]
    bottom_table = Table(bottom_data, colWidths=[330, 190])
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
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, "POWERED BY LyraERP")
        page_num = canvas.getPageNumber()
        canvas.drawRightString(570, 20, f"Page {page_num} of {doc.page if hasattr(doc, 'page') else page_num}")
        canvas.restoreState()
    
    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    
    # Get PDF value
    pdf = buffer.getvalue()
    buffer.close()
    
    return pdf


@csrf_exempt
def send_order_message(request, pk):
    """Send Email for a SalesOrder.

    Behavior mirrors `send_message` for quotations: prefers an EmailTemplateStyle
    (using names like 'Order Email' / 'Sales Order') and associated
    EmailConfiguration. Falls back to rendering 'sales/order_print.html' and
    using the generic `email_config.utils.send_email` helper.

    Expects POST and query param `method=email`.
    """
    try:
        if request.method != 'POST':
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        if any(m not in ("email",) for m in methods):
            return JsonResponse({"success": False, "message": "Invalid method"}, status=400)

        order = SalesOrder.objects.select_related('customer').filter(pk=pk).first()
        if not order:
            return JsonResponse({"success": False, "message": "Order not found"}, status=404)

        context = build_order_context(pk, request)

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
            'customer': f"{getattr(order.customer, 'first_name', '')} {getattr(order.customer, 'last_name', '')}".strip(),
            'order_number': order.order_number,
            'date': order.date.strftime("%d %B %Y") if getattr(order, 'date', None) else '',
            'total': str(order.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
        }
        # aliases for templates that use different placeholder names
        placeholder_ctx['employee'] = placeholder_ctx['customer']
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
                    return JsonResponse({"success": False, "message": f"No email configuration found for order."}, status=400)

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    recipient = order.shipping_email or (getattr(order.customer, 'email', None) if order.customer else None)
                    if not recipient:
                        return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                    # Generate PDF using ReportLab
                    pdf_bytes = generate_order_pdf_bytes(pk)

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
                    order.status = 'Sent'
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
                        return JsonResponse({"success": False, "message": f"No email configuration found for order."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        recipient = order.shipping_email or (getattr(order.customer, 'email', None) if order.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        # Generate PDF using ReportLab
                        pdf_bytes = generate_order_pdf_bytes(pk)

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

                        order.status = 'Sent'
                        order.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    # fallback: render print template
                    try:
                        full_bill_html = render_to_string('sales/order_print.html', context)
                        recipient = order.shipping_email or (getattr(order.customer, 'email', None) if order.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        # Pick default active email configuration
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                        if not email_config:
                            email_config = EmailConfiguration.objects.filter(status=True).first()
                        if not email_config:
                            return JsonResponse({"success": False, "message": "No email configuration found."}, status=400)

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
                        order.status = 'Sent'
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


@csrf_exempt
def generate_invoice_pdf_bytes(pk, request=None):
    """Generate PDF bytes for invoice using ReportLab - can be used for display or email."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.pdfgen.canvas import Canvas
    
    inv = get_object_or_404(SalesInvoice, pk=pk)
    context = build_invoice_context(pk, request)
    
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
                          title=f'Invoice {pk}',
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
    
    invoice_badge_style = ParagraphStyle(
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
    #added by neha on 20-1-26
    company = Company.objects.filter(status=True).first() or Company.objects.first()
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

    # HEADER with professional styling
    header_data = [
        [header_left_cell, Paragraph("INVOICE", invoice_badge_style)],
    ]
    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    # edited by neha on 20-1-26
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
    sales_person_obj = getattr(inv, 'sales_person', None)
    sales_person_name = (getattr(sales_person_obj, 'name', None) or '').strip()
    sales_person_phone = (getattr(sales_person_obj, 'phone', None) or '').strip()
    if sales_person_name and sales_person_phone:
        sales_person_display = f"{sales_person_name} ({sales_person_phone})"
    elif sales_person_name:
        sales_person_display = sales_person_name
    else:
        sales_person_display = '-'
    meta_data = [
        [Paragraph("<b>inv No</b>", meta_label_style), Paragraph(str(context.get('q_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(inv.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(inv.place_of_supply or '-'), meta_value_style),
         Paragraph("<b>Sales Person</b>", meta_label_style), Paragraph(str(sales_person_display), meta_value_style)],
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
    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if inv.customer:
        bill_lines = []
        customer_type = (getattr(inv.customer, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(inv.customer, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(inv.customer, 'first_name', '') or '').strip()
            last_name = (getattr(inv.customer, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)

        # Use shipping address for billing if it exists, otherwise use customer address
        if inv.shipping_address1 or inv.shipping_city:
            if getattr(inv, 'shipping_address1', None):
                bill_lines.append(inv.shipping_address1)
            if getattr(inv, 'shipping_address2', None):
                bill_lines.append(inv.shipping_address2)
            if getattr(inv, 'shipping_city', None):
                bill_lines.append(inv.shipping_city)
            if getattr(inv, 'shipping_postal_code', None):
                bill_lines.append(inv.shipping_postal_code)
            if getattr(inv, 'shipping_state', None):
                bill_lines.append(inv.shipping_state)
            if getattr(inv, 'shipping_country', None):
                bill_lines.append(str(inv.shipping_country))
        else:
            if getattr(inv.customer, 'address_line_1', None):
                bill_lines.append(inv.customer.address_line_1)
            if getattr(inv.customer, 'state', None):
                bill_lines.append(inv.customer.state)
            if getattr(inv.customer, 'country', None):
                bill_lines.append(str(inv.customer.country))

        gst = getattr(inv.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if inv.shipping_address1 or inv.shipping_city:
        ship_lines = []

        if getattr(inv, 'shipping_attention', None):
            ship_lines.append(inv.shipping_attention)

        if getattr(inv, 'shipping_address1', None):
            ship_lines.append(inv.shipping_address1)

        if getattr(inv, 'shipping_address2', None):
            ship_lines.append(inv.shipping_address2)

        if getattr(inv, 'shipping_city', None):
            ship_lines.append(inv.shipping_city)

        if getattr(inv, 'shipping_postal_code', None):
            ship_lines.append(inv.shipping_postal_code)

        if getattr(inv, 'shipping_state', None):
            ship_lines.append(inv.shipping_state)

        if getattr(inv, 'shipping_country', None):
            ship_lines.append(str(inv.shipping_country))

        if getattr(inv, 'shipping_email', None):
            ship_lines.append(inv.shipping_email)

        if getattr(inv, 'shipping_phone', None):
            ship_lines.append(inv.shipping_phone)

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
    # Fetch currency symbols from context
    currency = context.get('document_currency_symbol') or ("₹" if font_registered else "Rs.")
    base_currency = context.get('company_base_currency_symbol') or ("₹" if font_registered else "Rs.")
    company_is_india = context.get('company_is_india', True)
    
    # Conditional table headers based on country
    if company_is_india:
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
             Paragraph("<b>Item & Description</b>", label_style),
             Paragraph("<b>HSN/SAC</b>", label_style),
             Paragraph("<b>Qty</b>", label_style),
             Paragraph("<b>Rate</b>", label_style),
             Paragraph("<b>CGST</b>", label_style),
             Paragraph("<b>SGST</b>", label_style)]
        ]
    else:
        items_data = [
            [Paragraph("<b>#</b>", label_style), 
             Paragraph("<b>Item & Description</b>", label_style),
             Paragraph("<b>HSN/SAC</b>", label_style),
             Paragraph("<b>Qty</b>", label_style),
             Paragraph("<b>Rate</b>", label_style),
             Paragraph("<b>Tax Rate</b>", label_style),
             Paragraph("<b>Tax Amount</b>", label_style)]
        ]
    
    for idx, item in enumerate(context.get('items_info', []), 1):
        tax_rate = Decimal(str(item.get('tax_rate', 0)))
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        
        base_price_str = ""
        tax_base_str = ""

        if company_is_india:
            # Split tax equally for India (CGST/SGST)
            cgst_rate = tax_rate / 2
            sgst_rate = tax_rate / 2
            cgst_amount = tax_amount / 2
            sgst_amount = tax_amount / 2

            cgst_base_str = ""
            sgst_base_str = ""

            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}{base_price_str}", amount_style),
                Paragraph(f"{cgst_rate:.2f}%<br/>{currency}\u00A0{float(cgst_amount):.2f}{cgst_base_str}", amount_style),
                Paragraph(f"{sgst_rate:.2f}%<br/>{currency}\u00A0{float(sgst_amount):.2f}{sgst_base_str}", amount_style),
            ])
        else:
            # Show single VAT column for non-India
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{item.get('price', 0):.2f}{base_price_str}", amount_style),
                Paragraph(f"{tax_rate:.2f}%", amount_style),
                Paragraph(f"{currency}\u00A0{float(tax_amount):.2f}{tax_base_str}", amount_style),
            ])
    
    items_table = Table(items_data, colWidths=[25, 135, 59, 30, 90, 90, 90])
    items_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#ffffff')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
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
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) - Decimal(str(item.get('tax_amount', 0)))
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    subtotal_base = context.get('subtotal_calc_base', 0)
    total_cgst_base = context.get('total_cgst_base', 0)
    total_sgst_base = context.get('total_sgst_base', 0)
    total_tax_base = context.get('total_tax_base', 0)
    final_total_base = context.get('final_total_base', 0)

    sub_base_str = ""
    cgst_base_str = ""
    sgst_base_str = ""
    tax_base_str = ""
    final_base_str = ""
    
    if company_is_india:
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}{sub_base_str}", amount_style)],
            [Paragraph("CGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_cgst', 0):.2f}{cgst_base_str}", amount_style)],
            [Paragraph("SGST", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_sgst', 0):.2f}{sgst_base_str}", amount_style)],
        ]
    else:
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}{sub_base_str}", amount_style)],
            [Paragraph("VAT", totals_label_style), Paragraph(f"{currency}\u00A0{context.get('total_tax', 0):.2f}{tax_base_str}", amount_style)],
        ]
    # Add Round Off and Turnover Tax rows when applicable
    inv_obj = context.get('invoice')
    round_off_value = getattr(inv_obj, 'round_off', None) if inv_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}{''}", amount_style)])
    company_tax_type = context.get('company_tax_type', '')
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{Decimal(str(turnover_amt)):.2f}", amount_style)])

    grand_total_style = ParagraphStyle(
        'GrandTotal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2c3e50'),
        fontName='DejaVuSans',
        alignment=TA_RIGHT
    )
    
    totals_data.append([Paragraph("<b>Grand Total</b>", ParagraphStyle('GrandTotalLabel', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#ffffff'))), 
                       Paragraph(f"<b>{currency}\u00A0{context.get('final_total', 0):.2f}</b>{final_base_str}", grand_total_style)])
    
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
    inv_obj = context.get('invoice')
    notes_text = (getattr(inv_obj, 'notes', None) or '').strip() or '-'
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
    bottom_table = Table(bottom_data, colWidths=[330, 190])
    bottom_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('LINEWIDTH', (0, 0), (-1, -1), 1),
    ]))
    elements.append(bottom_table)
    
    # FOOTER - Use Canvas for page numbering
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, "POWERED BY LyraERP")
        page_num = canvas.getPageNumber()
        canvas.drawRightString(570, 20, f"Page {page_num} of {doc.page if hasattr(doc, 'page') else page_num}")
        canvas.restoreState()
    
    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    
    # Get PDF value
    pdf = buffer.getvalue()
    buffer.close()
    
    return pdf


@csrf_exempt
def send_invoice_message(request, pk):
    """Send Email for a SalesInvoice.

    Mirrors quotation send behavior. Uses EmailTemplateStyle names like
    'Invoice Email'/'Sales Invoice' if present; otherwise falls back to
    rendering 'sales/invoice_print.html'.
    """
    try:
        if request.method != 'POST':
            return JsonResponse({"success": False, "message": "Invalid request method"}, status=400)

        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        if any(m not in ("email",) for m in methods):
            return JsonResponse({"success": False, "message": "Invalid method"}, status=400)

        invoice = SalesInvoice.objects.select_related('customer').filter(pk=pk).first()
        if not invoice:
            return JsonResponse({"success": False, "message": "Invoice not found"}, status=404)

        context = build_invoice_context(pk, request)

        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        placeholder_ctx = {
            'customer': f"{getattr(invoice.customer, 'first_name', '')} {getattr(invoice.customer, 'last_name', '')}".strip(),
            'invoice_number': invoice.invoice_number if getattr(invoice, 'invoice_number', None) else getattr(invoice, 'inv_number', ''),
            'date': invoice.date.strftime("%d %B %Y") if getattr(invoice, 'date', None) else '',
            'total': str(invoice.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
        }
        # aliases for templates
        placeholder_ctx['employee'] = placeholder_ctx['customer']
        placeholder_ctx['amount'] = placeholder_ctx['total']
        placeholder_ctx['code'] = placeholder_ctx.get('invoice_number', '')

        messages_sent = []

        if 'email' in methods:
            template_style = None
            subject = None
            message_html = None
            try_names = ["Invoice Details", "Invoice Email", "Sales Invoice", "Invoice"]
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
                subject = replace_placeholders(template_style.subject or f"Invoice {placeholder_ctx.get('invoice_number','')}", placeholder_ctx)
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)

                # sanitize template HTML: strip inline color/background and font tags that cause dark backgrounds
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
                    # Fallback to default email configuration
                    email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                
                if not email_config:
                    return JsonResponse({"success": False, "message": f"No email configuration found for invoice. Go to settings and set up email configuration."}, status=400)

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False
                    )

                    recipient = invoice.shipping_email or (getattr(invoice.customer, 'email', None) if invoice.customer else None)
                    if not recipient:
                        return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                    # Generate PDF using ReportLab
                    pdf_bytes = generate_invoice_pdf_bytes(pk, request)

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

                    if pdf_bytes:
                        msg.attach(f'invoice_{invoice.pk}.pdf', pdf_bytes, 'application/pdf')

                    msg.send(fail_silently=False)

                    # invoice.status = 'Sent'
                    # invoice.save(update_fields=['status'])
                    messages_sent.append(f"Email sent to {recipient} successfully!")
                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
            else:
                # Try hardcoded default templates from email_templates if DB template not found
                from email_templates.views import get_default_email_template
                default_template = get_default_email_template("Invoice")
                if default_template:
                    subject = replace_placeholders(default_template.get("subject", f"Invoice {placeholder_ctx.get('invoice_number','')}"), placeholder_ctx)
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

                    # Determine email configuration: prefer one matching 'Invoice'
                    email_config = EmailConfiguration.objects.filter(usage_types__icontains="Invoice", status=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                    if not email_config:
                        email_config = EmailConfiguration.objects.filter(status=True).first()
                    if not email_config:
                        return JsonResponse({"success": False, "message": f"No email configuration found for invoice. Go to settings and set up email configuration."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        recipient = invoice.shipping_email or (getattr(invoice.customer, 'email', None) if invoice.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        # Generate PDF using ReportLab
                        pdf_bytes = generate_invoice_pdf_bytes(pk, request)

                        from_email = email_config.default_from_email or email_config.host_user
                        plain_body = f"Please find attached invoice {placeholder_ctx.get('invoice_number','')}."
                        msg = EmailMultiAlternatives(
                            subject or f"Invoice {placeholder_ctx.get('invoice_number','')}",
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
                            filename = f'invoice_{invoice.pk}.pdf'
                            msg.attach(filename, pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)

                        # invoice.status = 'Sent'
                        # invoice.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    try:
                        recipient = invoice.shipping_email or (getattr(invoice.customer, 'email', None) if invoice.customer else None)
                        if not recipient:
                            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

                        # Pick default active email configuration
                        email_config = EmailConfiguration.objects.filter(status=True, is_default=True).first()
                        if not email_config:
                            email_config = EmailConfiguration.objects.filter(status=True).first()
                        if not email_config:
                            return JsonResponse({"success": False, "message": "No email configuration found."}, status=400)

                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False
                        )

                        # Generate PDF using ReportLab
                        pdf_bytes = generate_invoice_pdf_bytes(pk, request)

                        from_email = email_config.default_from_email or email_config.host_user
                        msg = EmailMultiAlternatives(
                            f"Invoice {placeholder_ctx.get('invoice_number','')}",
                            f"Please find attached invoice {placeholder_ctx.get('invoice_number','')}.",
                            from_email,
                            [recipient],
                            connection=connection
                        )

                        # attach the template message as HTML so it appears in the email body
                        try:
                            if 'message_html' in locals() and message_html:
                                msg.attach_alternative(message_html, 'text/html')
                        except Exception:
                            pass

                        if pdf_bytes:
                            msg.attach(f'invoice_{invoice.pk}.pdf', pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)
                        # invoice.status = 'Sent'
                        # invoice.save(update_fields=['status'])
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
# def quotation_edit(request, pk):
#     quote = get_object_or_404(SalesQuotation, pk=pk)
#     ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
#     # SalesQuotationItemFormSet = formset_factory(SalesQuotationItemForm, extra=0)
#     SalesQuotationItemFormSet = modelformset_factory(SalesQuotationItem, form=SalesQuotationItemForm, extra=0)
#     existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
#     sales_formset = SalesQuotationItemFormSet(request.POST or None, queryset=existing_items_qs)


#     if request.method == "POST":
#         # quotation_form = SalesQuotationForm(request.POST, instance=quote)
#         # sales_formset = SalesQuotationItemFormSet(request.POST)
#         quotation_form = SalesQuotationForm(request.POST, instance=quote)
#         existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
#         sales_formset = SalesQuotationItemFormSet(request.POST, queryset=existing_items_qs)
#         #print("sales_formset:",sales_formset)

#         if quotation_form.is_valid() and sales_formset.is_valid():
#             quotation_form.save()
#             sales_formset.save()
#             # Delete existing items, then add new ones (or handle updates as needed)
#             SalesQuotationItem.objects.filter(Sales_quotation=quote).delete()
            
#             for form in sales_formset:
#                 cd = form.cleaned_data
#                 if cd and cd.get('product'):
#                     SalesQuotationItem.objects.create(
#                         Sales_quotation=quote,
#                         product=cd['product'],
#                         prd_brcd=cd.get('prd_brcd', ''),
#                         prd_disvalue=cd.get('prd_disvalue', 0),
#                         prd_distype=cd.get('prd_distype', 'flat'),
#                         quantity=cd.get('quantity', 1),
#                         price=cd.get('price', 0),
#                     )
#             messages.success(request, "Sales Quotation updated successfully!")
#             return redirect('sales_quote_list')
#     else:
#         quotation_form = SalesQuotationForm(instance=quote)

#         # Prepare initial data for items formset from existing quotation items
#         existing_items = list(quote.items.all())
#         #print("existing_items:", existing_items)
#         purchase_items  = [{
#             'product': item.product,
#             'prd_brcd': item.prd_brcd,
#             'prd_disvalue': item.prd_disvalue,
#             'prd_distype': item.prd_distype,
#             'quantity': item.quantity,
#             'price': item.price,
#         } for item in existing_items]
#         #print("initial_data:", purchase_items )
#         sales_formset = SalesQuotationItemFormSet(initial=purchase_items )

#     all_items = Item.objects.all()

#     return render(request, 'sales/quotation_add.html', {
#         'quotation_form': quotation_form,
#         'sales_formset': sales_formset,
#         'all_items': all_items,
#         'today': localdate().isoformat(),
#         'q_no': quote.quote_number,
#     })



#correctly prefilled
def quotation_edit(request, pk):
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    #for viewing only
    readonly = request.GET.get('readonly', 'false').lower() == 'true'
    # If user lacks edit permission force readonly
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_quotations(request.user)):
            readonly = True
    except Exception:
        readonly = True
    quote = get_object_or_404(SalesQuotation, pk=pk)
    
    # Get company for currency field
    try:
        cid = request.session.get('company_id')
        company = None
        if cid:
            company = Company.objects.filter(pk=cid).first()
        if not company:
            company = Company.objects.order_by('id').first()
    except Exception:
        company = None
    
    try:
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code') if company else Currency.objects.none()
    except Exception:
        company_currencies = Currency.objects.none()
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = (base_currency.code or '').strip() if base_currency else ''

    SalesQuotationItemFormSet = modelformset_factory(SalesQuotationItem, form=SalesQuotationItemForm, extra=0,can_delete=True)

    if request.method == "POST"and not readonly:
        # Permission: require Edit on Quotations
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_quotations(request.user)):
                messages.error(request, 'You do not have permission to edit Quotations.')
                return redirect_with_company('sales_quote_list')
        except Exception:
            messages.error(request, 'You do not have permission to edit Quotations.')
            return redirect_with_company('sales_quote_list')

        post_data = request.POST.copy()
        grand_total_value = (post_data.get('unroundedGrandTotal') or post_data.get('grandTotal') or '').strip()
        grand_discount_raw = (post_data.get('grand-discount-value') or '').strip()
        posted_discount_type = (post_data.get('discount_type') or '').strip()

        def _coerce_bound_value(field_name, value):
            if value is None:
                return ''
            if hasattr(value, 'pk'):
                return str(value.pk)
            if hasattr(value, 'isoformat'):
                return value.isoformat()
            if field_name in ('total_amount', 'total_amount_base', 'round_off') and grand_total_value:
                # Placeholder so the form validates; the real value is computed
                # from apply_sales_rounding() further below and overwrites this.
                return grand_total_value
            return str(value)

        preserved_fields = [
            'quote_number',
            'status',
            'total_amount',
            'total_amount_base',
            'discount_value',
            'discount_type',
            'round_off',
        ]
        for field_name in preserved_fields:
            if field_name not in post_data:
                post_data[field_name] = _coerce_bound_value(field_name, getattr(quote, field_name, ''))
        if grand_discount_raw:
            post_data['discount_value'] = grand_discount_raw
        if posted_discount_type:
            post_data['discount_type'] = posted_discount_type

        quotation_form = SalesQuotationForm(post_data, instance=quote, company=company)
        existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
        sales_formset = SalesQuotationItemFormSet(post_data, queryset=existing_items_qs)

        if quotation_form.is_valid() and sales_formset.is_valid():
            # Save form but also capture shipping fields from POST
            quote_obj = quotation_form.save(commit=False)
            quote_obj.shipping_attention = request.POST.get('shipping_attention', '')
            quote_obj.shipping_email = request.POST.get('shipping_email', '')
            quote_obj.shipping_phone = request.POST.get('shipping_phone', '')
            quote_obj.shipping_country = request.POST.get('shipping_country', '')
            quote_obj.shipping_address1 = request.POST.get('shipping_address1', '')
            quote_obj.shipping_address2 = request.POST.get('shipping_address2', '')
            quote_obj.shipping_city = request.POST.get('shipping_city', '')
            quote_obj.shipping_state = request.POST.get('shipping_state', '')
            quote_obj.shipping_postal_code = request.POST.get('shipping_postal_code', '')
            quote_obj.tds_tcs_type = request.POST.get('tds_tcs_type', quote_obj.tds_tcs_type or 'tds')
            quote_obj.tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
            quote_obj.tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
            quote_obj.tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
            # Persist place of supply if provided from the form
            quote_obj.place_of_supply = request.POST.get('place_of_supply', quote_obj.place_of_supply or '')
            quote_obj._current_user = request.user

            quote_obj._current_request = request
            if _get_request_company_tax_type(request, company) == 'TURNOVER':
                turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                quote_obj.turnover_tax = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first() if turnover_tax_id else None
            else:
                quote_obj.turnover_tax = None
            grand_total = (request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal') or '').strip()
            if grand_total:
                try:
                    rounding_company = _get_company_for_request(request)
                    quote_obj.total_amount, quote_obj.round_off = apply_sales_rounding(grand_total, rounding_company)
                    quote_obj.total_amount_base = (
                        quote_obj.total_amount * Decimal(str(quote_obj.fx_rate_to_base or 1))
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                except (InvalidOperation, ValueError, TypeError):
                    pass
            
            # Persist payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    quote_obj.payment_term = pt
                except Exception:
                    pass
            # Persist sales person selection if provided
            sales_person_id = request.POST.get('sales_person')
            if sales_person_id:
                try:
                    sp = SalesPerson.objects.get(pk=sales_person_id)
                    quote_obj.sales_person = sp
                except Exception:
                    pass
                    try:
                        discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
                        quote_obj.discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    except (InvalidOperation, ValueError, TypeError):
                        quote_obj.discount_value = Decimal('0.00')
                        quote_obj.discount_type = request.POST.get('discount_type', 'percent')
            quote_obj.save()
            items = sales_formset.save(commit=False)
            for deleted_item in sales_formset.deleted_objects:
                deleted_item.delete()
            for form in sales_formset.forms:
                if (not getattr(form, 'cleaned_data', None) or
                    form.cleaned_data.get('DELETE') or
                    not form.cleaned_data.get('product')):
                    continue
                item = form.save(commit=False)
                selected_tax = form.cleaned_data.get('prd_tax') or request.POST.get(f'{form.prefix}-prd_tax', '')
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                try:
                    item.o_price = Decimal(str(request.POST.get(f'{form.prefix}-o_price', '0') or '0'))
                except (InvalidOperation, ValueError, TypeError):
                    item.o_price = Decimal('0.00')
                item.Sales_quotation = quote_obj
                item.save()
            if not quote_obj.total_amount_base:
                quote_obj.total_amount_base = _calculate_document_base_total(
                    quote_obj.items.all(),
                    quote_obj.fx_rate_to_base,
                    quote_obj.discount_value,
                    quote_obj.discount_type,
                )
            quote_obj.save(update_fields=['total_amount_base'])
            messages.success(request, "Sales Quotation updated successfully!")
            return redirect_with_company('sales_quote_list')
    else:
        existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
        sales_formset = SalesQuotationItemFormSet(queryset=existing_items_qs)
        sp = _get_current_salesperson(request)
        if not getattr(quote, 'sales_person', None) and sp:
            quotation_form = SalesQuotationForm(instance=quote, company=company, initial={'sales_person': sp.pk})
        else:
            quotation_form = SalesQuotationForm(instance=quote, company=company)
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



        # — Fetch tax group names in bulk (to avoid N+1 queries)
        product_ids = existing_items_qs.values_list('product_id', flat=True)
        items_with_tax = Item.objects.filter(id__in=product_ids).select_related('intra_tax', 'inter_tax_group')

        # Build dict for quick lookup: { item_id: tax_group_name }
        tax_map = {}
        for itm in items_with_tax:
            tax_name = ''
            if company_is_india:
                if itm.intra_tax_id:
                    tax_group = TaxGroup.objects.filter(id=itm.intra_tax_id).first()
                    if tax_group:
                        tax_name = tax_group.group_name
            else:
                if itm.inter_tax_group_id:
                    tax_group = TaxGroup.objects.filter(id=itm.inter_tax_group_id).first()
                    if tax_group:
                        tax_name = tax_group.group_name
            tax_map[itm.id] = tax_name

        for form, item in zip(sales_formset.forms, existing_items_qs):
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            # #print(f"Checking key {key} ₹ matched UOM: {uom_name}")

            # — Force the dropdown to display the correct text for this specific instance
            if uom_name:
                display_label = f"{item.product.name} ({uom_name})"
            else:
                display_label = item.product.name

            # — Replace the field choices with the selected item label
            form.fields['product'].choices = [
                (item.product.id, display_label)
            ]
            form.initial['product'] = item.product.id
            # #print(f"₹ Updated dropdown for {display_label}")


        # for item in existing_items_qs:
            # #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

        # — Combine product + UOM + Tax Group for display
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
    # return render(request, 'sales/quotation_duplicate.html', {
    #     'quotation_form': quotation_form,
    #     'sales_formset': sales_formset,
    #     'all_items': all_items,
    #     'today': localdate().isoformat(),
    #     'q_no': quote.quote_number,
    #     # 'item_display_list': item_display_list,
    #     'quote': quote, 
    #     'readonly': readonly,
        
    # })
    context = {
        'quotation_form': quotation_form,
        'sales_formset': sales_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': quote.quote_number,
        'quote': quote,
        'readonly': readonly,
        # Shipping #by adarshaddress fields for prefilling
        'shipping_attention': quote.shipping_attention or '',
        'shipping_email': quote.shipping_email or '',
        'shipping_phone': quote.shipping_phone or '',
        'shipping_country': quote.shipping_country or '',
        'shipping_address1': quote.shipping_address1 or '',
        'shipping_address2': quote.shipping_address2 or '',
        'shipping_city': quote.shipping_city or '',
        'shipping_state': quote.shipping_state or '',
        'shipping_postal_code': quote.shipping_postal_code or '',#by adarsh
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': _get_request_company_tax_type(request, company),
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'selected_currency_id': quote.document_currency_id or '',
        'fx_rate_to_base': quote.fx_rate_to_base,
        'fx_rate_date': quote.fx_rate_date,
        'tds_tcs_type': quote.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': quote.tds_tcs_definition_id or '',
        'tds_tcs_rate': quote.tds_tcs_rate or 0,
        'tds_tcs_amount': quote.tds_tcs_amount or 0,
        # — Fetch TDS and TCS for quotation_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=company, is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=company, is_active=True),
        'show_base_transaction_summary': bool(getattr(_get_company_for_request(request), 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(_get_company_for_request(request), 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(_get_company_for_request(request), 'sales_rounding_increment', 0),
    }
    
    return render(request, 'sales/quotation_duplicate.html', context)


def _get_line_base_unit_price(item, fx_rate=Decimal('1')):
    explicit_base = _to_decimal(getattr(item, 'o_price', None), default='0.00')
    if explicit_base > Decimal('0.00'):
        return explicit_base
    document_price = _to_decimal(getattr(item, 'price', None), default='0.00')
    fx_rate = _to_decimal(fx_rate, default='1.00')
    return (document_price * fx_rate).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)


def _calculate_line_base_totals(item, fx_rate=Decimal('1')):
    quantity = _to_decimal(getattr(item, 'quantity', 0), default='0.00')
    unit_base_price = _get_line_base_unit_price(item, fx_rate=fx_rate)
    line_base = (quantity * unit_base_price).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    discount_value = _to_decimal(getattr(item, 'prd_disvalue', 0), default='0.00')
    discount_type = getattr(item, 'prd_distype', 'flat') or 'flat'
    if discount_type == 'percent':
        discount_amount_base = (line_base * discount_value / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        fx_rate = _to_decimal(fx_rate, default='1.00')
        discount_amount_base = (discount_value * fx_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if discount_amount_base > line_base:
        discount_amount_base = line_base

    discounted_base = (line_base - discount_amount_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    tax_rate = _to_decimal(getattr(item, 'prd_tax', 0), default='0.00')
    tax_amount_base = (discounted_base * tax_rate / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if tax_rate else Decimal('0.00')

    return {
        'unit_base_price': unit_base_price,
        'line_base': line_base,
        'discount_amount_base': discount_amount_base,
        'discounted_base': discounted_base,
        'tax_amount_base': tax_amount_base,
        'line_total_base': discounted_base,
    }


def _calculate_document_base_total(items, fx_rate, discount_value, discount_type):
    subtotal_base = Decimal('0.00')
    total_tax_base = Decimal('0.00')

    for item in items:
        line_totals = _calculate_line_base_totals(item, fx_rate=fx_rate)
        subtotal_base += line_totals['line_total_base']
        total_tax_base += line_totals['tax_amount_base']

    total_before_discount_base = subtotal_base + total_tax_base
    discount_value = _to_decimal(discount_value, default='0.00')
    fx_rate = _to_decimal(fx_rate, default='1.00')

    if (discount_type or 'flat') == 'percent':
        grand_discount_base = (total_before_discount_base * discount_value / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        grand_discount_base = (discount_value * fx_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    if grand_discount_base > total_before_discount_base:
        grand_discount_base = total_before_discount_base

    return (total_before_discount_base - grand_discount_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _build_base_tax_posting_amounts(invoice, tax_totals):
    """
    Build journal posting amounts directly in base currency so journal lines match
    the UI's "Base Currency Transaction" section.
    Returns (sales_base_credit, {tax_code: base_amount}).
    """
    subtotal_base = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    fx_rate = getattr(invoice, 'fx_rate_to_base', Decimal('1.00'))

    for item in invoice.items.all():
        line_totals = _calculate_line_base_totals(item, fx_rate=fx_rate)
        subtotal_base += line_totals.get('line_total_base', Decimal('0.00'))
        total_tax_base += line_totals.get('tax_amount_base', Decimal('0.00'))

    subtotal_base = subtotal_base.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_tax_base = total_tax_base.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # Keep journal balanced to invoice base total by deriving sales from total base.
    sales_base_credit = (Decimal(invoice.total_amount_base or 0) - total_tax_base).quantize(
        Decimal('0.01'),
        rounding=ROUND_HALF_UP
    )

    normalized_doc_taxes = {}
    for ttype, amount in (tax_totals or {}).items():
        code = normalize_tax_code(ttype)
        normalized_doc_taxes[code] = normalized_doc_taxes.get(code, Decimal('0.00')) + Decimal(amount or 0)

    base_tax_totals = {}
    if 'CGST' in normalized_doc_taxes and 'SGST' in normalized_doc_taxes:
        cgst_base = (total_tax_base / Decimal('2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        sgst_base = (total_tax_base - cgst_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        base_tax_totals['CGST'] = cgst_base
        base_tax_totals['SGST'] = sgst_base
    elif normalized_doc_taxes:
        total_doc_tax = sum(normalized_doc_taxes.values())
        allocated = Decimal('0.00')
        keys = list(normalized_doc_taxes.keys())
        for idx, code in enumerate(keys):
            if idx == len(keys) - 1:
                amt = (total_tax_base - allocated).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            else:
                share = (normalized_doc_taxes[code] / total_doc_tax) if total_doc_tax else Decimal('0')
                amt = (total_tax_base * share).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                allocated += amt
            base_tax_totals[code] = amt

    return sales_base_credit, base_tax_totals
    if readonly:
        for form in sales_formset.forms:
            for field_name, field in form.fields.items():
                form.fields['product'].widget.attrs['disabled'] = True
                form.fields['prd_tax'].widget.attrs['disabled'] = True

                widget = field.widget
                if widget.__class__.__name__ in ['Select', 'SelectMultiple', 'CheckboxInput', 'RadioSelect']:
                    widget.attrs['disabled'] = True  # disable selects and similar widgets
                else:
                    widget.attrs['readonly'] = True
        # Render a dedicated readonly template without edit actions/buttons
        return render(request, 'sales/invoice_detail.html', context)
    else:
        # Render the editable template
        return render(request, 'sales/quotation_duplicate.html', context)


#correctly prefilled
# def quotation_edit(request, pk):
#     quote = get_object_or_404(SalesQuotation, pk=pk)
#     SalesQuotationItemFormSet = modelformset_factory(SalesQuotationItem, form=SalesQuotationItemForm, extra=0)

#     if request.method == "POST":
#         quotation_form = SalesQuotationForm(request.POST, instance=quote)
#         existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
#         sales_formset = SalesQuotationItemFormSet(request.POST, queryset=existing_items_qs)

#         if quotation_form.is_valid() and sales_formset.is_valid():
#             quotation_form.save()
#             sales_formset.save()
#             messages.success(request, "Sales Quotation updated successfully!")
#             return redirect('sales_quote_list')
#     else:
#         existing_items_qs = SalesQuotationItem.objects.filter(Sales_quotation=quote)
#         sales_formset = SalesQuotationItemFormSet(queryset=existing_items_qs)
#         quotation_form = SalesQuotationForm(instance=quote)
#         #print("existing_items_qs:", existing_items_qs)
#         item_display_list = []

#         # Build UOM map keyed by both barcode id and barcode value
#         uoms_by_barcode = {}
#         uom_qs = Uom.objects.filter(
#             item_id__in=existing_items_qs.values_list('product_id', flat=True),
#             barcode_id__in=existing_items_qs.values_list('prd_brcd', flat=True).distinct()
#         ).select_related('barcode', 'name')

#         for u in uom_qs:
#             # if barcode relation exists, add two keys:
#             if getattr(u, 'barcode', None):
#                 # key using barcode record id (as int)
#                 try:
#                     key_id = f"{u.item_id}_{int(u.barcode.id)}"
#                     uoms_by_barcode[key_id] = u.name.name if u.name else ''
#                 except Exception:
#                     pass

#                 # key using barcode value (string)
#                 try:
#                     barcode_val = str(u.barcode.barcode)
#                     key_val = f"{u.item_id}_{barcode_val}"
#                     uoms_by_barcode[key_val] = u.name.name if u.name else ''
#                 except Exception:
#                     pass
#             else:
#                 # fallback: map by item id only (if needed)
#                 k = f"{u.item_id}_"
#                 uoms_by_barcode[k] = u.name.name if u.name else ''

#             #print("uoms_by_barcode[key]:", u.item_id, getattr(u.barcode, 'id', None), getattr(u.barcode, 'barcode', None), "=>", uoms_by_barcode.get(f"{u.item_id}_{getattr(u.barcode,'id','')}", ''))



#         # — Fetch tax group names in bulk (to avoid N+1 queries)
#         product_ids = existing_items_qs.values_list('product_id', flat=True)
#         items_with_tax = Item.objects.filter(id__in=product_ids).select_related('intra_tax')

#         # Build dict for quick lookup: { item_id: tax_group_name }
#         # tax_map = {}
#         tax_map = {itm.id: (itm.intra_tax.group_name if itm.intra_tax_id else '') for itm in items_with_tax}
#         for itm in items_with_tax:
#             tax_name = ''
#             if itm.intra_tax_id:
#                 tax_group = TaxGroup.objects.filter(id=itm.intra_tax_id).first()
#                 if tax_group:
#                     tax_name = tax_group.group_name
#             tax_map[itm.id] = tax_name

#         for form, item in zip(sales_formset.forms, existing_items_qs):
#             key = f"{item.product_id}_{item.prd_brcd}"
#             uom_name = uoms_by_barcode.get(key, '')
#             #print(f"Checking key {key} ₹ matched UOM: {uom_name}")

#             # — Force the dropdown to display the correct text for this specific instance
#             if uom_name:
#                 display_label = f"{item.product.name} ({uom_name})"
#             else:
#                 display_label = item.product.name

#             # — Replace the field choices with the selected item label
#             form.fields['product'].choices = [
#                 (item.product.id, display_label)
#             ]
#             form.initial['product'] = item.product.id
#             #print(f"₹ Updated dropdown for {display_label}")


#         for item in existing_items_qs:
#             #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

#         # — Combine product + UOM + Tax Group for display
#         for item in existing_items_qs:
#             key = f"{item.product_id}_{item.prd_brcd}"
#             uom_name = uoms_by_barcode.get(key, '')
#             display_name = f"{item.product.name} ({uom_name})" if uom_name else item.product.name

#             tax_group_name = tax_map.get(item.product_id, '')

#             item_display_list.append({
#                 'combined_id': key,
#                 'display_name': display_name,
#                 'quantity': item.quantity,
#                 'price': item.price,
#                 'prd_brcd': item.prd_brcd,
#                 'tax_group_name': tax_group_name,
#             })
#             #print("item_display_list:", item_display_list)
#             # #print("tax_group_name:", tax_group_name, flush=True)


#     all_items = Item.objects.all()
#     return render(request, 'sales/quotation_add.html', {
#         'quotation_form': quotation_form,
#         'sales_formset': sales_formset,
#         'all_items': all_items,
#         'today': localdate().isoformat(),
#         'q_no': quote.quote_number,
#         'item_display_list': item_display_list,
#         'quote': quote, 
        
#     })


# SalesQuotationItemFormSet = modelformset_factory(SalesQuotationItem, form=SalesQuotationItemForm, extra=0)
# def quotation_duplicate(request, pk):
#     #print("Duplicate an existing quotation and save it as a new one.")
#     old_quotation = get_object_or_404(SalesQuotation, pk=pk)
#     old_items = SalesQuotationItem.objects.filter(Sales_quotation=old_quotation)

#     if request.method == 'POST':
#         #print("inside post")

#         # — Generate a new quote number using your helper
#         new_quote_number = generate_quote_number()
#         #print("new_quote_number:",new_quote_number)
#         # — Duplicate the quotation
#         new_quotation = SalesQuotation.objects.create(
#             customer=old_quotation.customer,
#             date=timezone.now().date(),  # or old_quotation.date if you prefer
#             sales_person=old_quotation.sales_person,
#             quote_number=new_quote_number,
#             total_amount=old_quotation.total_amount,
#             notes=old_quotation.notes,
#             discount_value=old_quotation.discount_value,
#             discount_type=old_quotation.discount_type,
#             status=old_quotation.status,
#         )
#         #print("new_quotation:",new_quotation)
#         # — Duplicate all quotation items
#         for item in old_items:
#             item.pk = None  # remove primary key
#             item.Sales_quotation = new_quotation
#             item.save()

#         messages.success(request, f"Quotation {new_quote_number} duplicated successfully.")
#         return redirect('sales_quote_list')

#     else:
#         # Just show confirmation screen (optional)
#         return render(request, 'sales/quotation_duplicate_confirm.html', {
#             'old_quotation': old_quotation,
#             'old_items': old_items,
#         })


def quotation_duplicate(request, pk):
    original_quote = get_object_or_404(SalesQuotation, pk=pk)
    original_quote_number = original_quote.quote_number
    # #print("original_quote_number:", original_quote_number)
    if request.method == "POST":
        # print("inside post", )
        post_data = request.POST.copy()  # make mutable copy
        db = getattr(request, 'company_db', 'default')
        prd_brcd_map = {}

        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
        for key in post_data:
            # Identify product field keys
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                # #print(f"Key matched: {key} with value: '{value}'")
                if value:
                    parts = value.split("_", 1)
                    item_id = (parts[0] or '').strip()
                    # Only digits are valid Item PKs; anything else becomes empty.
                    if not item_id.isdigit():
                        post_data[key] = ''
                        continue

                    post_data[key] = item_id  # Save only item id for product field

                    if len(parts) > 1:
                        # Map barcode corresponding to this form prefix (used for prd_brcd).
                        barcode_part = (parts[1] or '').strip()
                        if barcode_part:
                            prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
                            prd_brcd_map[prefix] = barcode_part


        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)
        
        raw_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
        try:
            raw_total_amount = (
                Decimal(str(raw_total).strip())
                if raw_total not in (None, '')
                else Decimal('0')
            )
        except Exception:
            raw_total_amount = Decimal('0')
        rounding_company = _get_company_for_request(request)
        total_amount, rounding_adjustment = apply_sales_rounding(raw_total_amount, rounding_company)
        customer_id = request.POST.get('customer')
        date = request.POST.get('date')
        sales_person_id = request.POST.get('sales_person')
        notes = request.POST.get('notes', '')
        # quote_number = generate_quote_number()
        quote_number = generate_revised_quote_number(original_quote_number)
        # #print("quote_number:",quote_number)
        try:
            discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
            discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_value = Decimal('0.00')
        discount_type = request.POST.get('discount_type', 'percent')
        # place_of_supply=request.POST.get('place_of_supply', '')
        # shipping_attention=request.POST.get('shipping_attention', '')
        # shipping_email=request.POST.get('shipping_email', '')
        # shipping_phone=request.POST.get('shipping_phone', '')
        # shipping_country=request.POST.get('shipping_country', '')
        # shipping_address1=request.POST.get('shipping_address1', '')
        # shipping_address2=request.POST.get('shipping_address2', '')
        # shipping_city=request.POST.get('shipping_city', '')
        # shipping_state=request.POST.get('shipping_state', '')
        # shipping_postal_code=request.POST.get('shipping_postal_code', '')
        # print("shipping_address1:",shipping_address1)
        if not customer_id or not date:
            messages.error(request, "Customer and Quotation Date are required.")
            return redirect_with_company('sales_quote_list')

        try:
            customer = Customer.objects.get(pk=customer_id)
            sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None

            # Handle currency - use original quotation's currency or customer's default currency
            document_currency = original_quote.document_currency
            fx_rate_to_base = original_quote.fx_rate_to_base or Decimal('1.000000')
            fx_rate_date = original_quote.fx_rate_date
            
            if not document_currency and customer.currency:
                try:
                    document_currency = Currency.objects.get(code__iexact=customer.currency)
                except Currency.DoesNotExist:
                    pass

            quote = SalesQuotation.objects.create(
                customer=customer,
                date=date,
                sales_person=sales_person,
                quote_number=quote_number,
                total_amount=total_amount,
                round_off=rounding_adjustment,
                notes=notes,
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
                tds_tcs_type=request.POST.get('tds_tcs_type', original_quote.tds_tcs_type or 'tds'),
                tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
                tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
                tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
                document_currency=document_currency,
                fx_rate_to_base=fx_rate_to_base,
                fx_rate_date=fx_rate_date,
            )
            # Attach payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    quote.payment_term = pt
                    quote.save()
                except PayTerms.DoesNotExist:
                    pass
        except IntegrityError as e:
            if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
                messages.error(request, f"Quote Number '{quote_number}' already exists. Please choose a different one.")
            else:
                logger.exception("IntegrityError while creating revised SalesQuotation: %s", e)
                messages.error(request, "An error occurred while saving the quotation.")
            return redirect_with_company('sales_quote_list')

        SalesQuotationItemFormSet = modelformset_factory(
            SalesQuotationItem, form=SalesQuotationItemForm, extra=0, can_delete=True
        )

        formset = SalesQuotationItemFormSet(post_data, queryset=SalesQuotationItem.objects.none())

        if formset.is_valid():
            items = formset.save(commit=False)
            # accumulate totals for journal posting
            taxable_total = Decimal(0)
            tax_totals = {}  # map of taxtype -> Decimal amount (e.g., 'CGST'->amount)
            saved_any = False
            for index, item in enumerate(items):
                #added for edit save
                item.pk = None
                
                prefix = f"form-{index}"              # formset form key pattern
                # Skip blank/invalid rows
                if not getattr(item, 'product_id', None):
                    continue
                saved_any = True
                if prefix in prd_brcd_map:            # check if barcode was extracted
                    item.prd_brcd = prd_brcd_map[prefix]
                # capture HSN code from the hidden input posted by template
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                # capture HSN code from the hidden input posted by template
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')
                selected_tax = post_data.get(f'form-{index}-prd_tax', '')
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                item.Sales_quotation = quote          # set foreign key
                item.save()                
            for deleted_item in formset.deleted_objects:
                deleted_item.delete()

            if not saved_any:
                quote.delete()
                messages.error(request, "Please add at least one valid item in the quotation.")
                return redirect_with_company('sales_quote_list')
                

            messages.success(request, "Sales Quotation created successfully!")
            # Redirect to quotation detail page after save
            try:
                url = reverse('quotation_detail', args=[quote.pk])
                return redirect_with_company(url)
            except Exception:
                return redirect_with_company('sales_quote_list')
        else:
            # #print("Formset errors:", formset.errors)
            # for form in formset:
            #     #print("Individual form errors:", form.errors)
            # Prevent half-created revised quotation header with total_amount=0
            try:
                quote.delete()
            except Exception:
                logger.exception("Failed to delete SalesQuotation (revision) after invalid formset")

            messages.error(request, "There are errors with the items in the quotation.")
            return redirect_with_company('sales_quote_list')
    else:
        return redirect_with_company('sales_quote_list')

def generate_order_number():
    prefix_obj = OrderPrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "ON"

    # Fetch all quote_numbers
    all_orders = SalesOrder.objects.values_list('order_number', flat=True)

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


@transaction.atomic
def convert_quotation_to_order(request, quotation_id):
    quotation = get_object_or_404(SalesQuotation, pk=quotation_id)
    # Status rule: quotation must be Accepted before converting to order.updt by neha on 23-2-26
    if quotation.status != 'Accepted':
        messages.error(request, 'Only Accepted quotations can be converted to Sales Order.')
        return redirect_with_company('quotation_detail', pk=quotation.pk)

    # Generate a unique order_number (example: prefix 'SO' + quotation id + timestamp)
    import datetime
    # order_number = f"SO{quotation.id}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
    order_number = generate_order_number()

    # Create SalesOrder from SalesQuotation
    sales_order = SalesOrder.objects.create(
        customer=quotation.customer,
        order_number=order_number,
        date=timezone.now(),
        sales_person=quotation.sales_person,
        notes=quotation.notes,
        payment_term=quotation.payment_term,
        discount_value=quotation.discount_value,
        discount_type=quotation.discount_type,
        status='DRAFT',
        total_amount=quotation.total_amount,
        place_of_supply=quotation.place_of_supply,
        shipping_attention=quotation.shipping_attention,
        shipping_email=quotation.shipping_email,
        shipping_phone=quotation.shipping_phone,
        shipping_country=quotation.shipping_country,
        shipping_address1=quotation.shipping_address1,
        shipping_address2=quotation.shipping_address2,
        shipping_city=quotation.shipping_city,
        shipping_state=quotation.shipping_state,
        shipping_postal_code=quotation.shipping_postal_code,
        tds_tcs_type=quotation.tds_tcs_type,
        tds_tcs_definition_id=quotation.tds_tcs_definition_id,
        tds_tcs_rate=quotation.tds_tcs_rate,
        tds_tcs_amount=quotation.tds_tcs_amount,
        document_currency=quotation.document_currency,
        fx_rate_to_base=quotation.fx_rate_to_base,
        fx_rate_date=quotation.fx_rate_date,
        total_amount_base=quotation.total_amount_base,
    )

    # Copy SalesQuotationItems to SalesOrderItems
    quotation_items = SalesQuotationItem.objects.filter(Sales_quotation=quotation)
    for item in quotation_items:
        SalesOrderItem.objects.create(
            sales_order=sales_order,
            product=item.product,
            prd_brcd=item.prd_brcd,
            hsn_code=item.hsn_code,
            prd_tax=item.prd_tax,
            prd_taxgroup=item.prd_taxgroup,
            prd_disvalue=item.prd_disvalue,
            prd_distype=item.prd_distype,
            quantity=item.quantity,
            price=item.price,
        )

    # (Optional) Update quotation status or notify user here

    # Redirect to sales order detail or list (replace 'sales_order_detail' accordingly)
    return redirect_with_company('sales_order_list')


@require_POST
def delete_sales_quotation(request, quotation_id):
    # Permission: require Delete on Quotations
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_quotations(request.user)):
            messages.error(request, 'You do not have permission to delete Quotations.')
            return redirect_with_company('sales_quote_list')
    except Exception:
        messages.error(request, 'You do not have permission to delete Quotations.')
        return redirect_with_company('sales_quote_list')

    quotation = get_object_or_404(SalesQuotation, pk=quotation_id)
    quotation.delete()
    messages.success(request, f"Sales Quotation '{quotation.quote_number}' deleted successfully.")
    return redirect_with_company('sales_quote_list')


def update_quotation_status(request, quotation_id):
    """Update quotation status via AJAX."""
    if request.method == 'POST':
        try:
            quotation = get_object_or_404(SalesQuotation, pk=quotation_id)
            new_status = request.POST.get('status', '').strip()
            current_status = (quotation.status or '').strip()
            
            logger.debug(f"Attempting to update quotation {quotation_id} status to: {new_status}")
            
            # Validate status against allowed choices
            valid_statuses = [choice[0] for choice in SalesQuotation.STATUS_CHOICES]
            logger.debug(f"Valid statuses: {valid_statuses}")
            
            if new_status not in valid_statuses:
                logger.warning(f"Invalid status '{new_status}' provided. Valid options: {valid_statuses}")
                return JsonResponse({'success': False, 'error': f'Invalid status. Valid options: {", ".join(valid_statuses)}'}, status=400)
            #updt by neha on 23-2-26
            allowed_transitions = {
                'Draft': {'Sent','Accepted', 'Rejected'},
                'Sent': {'Accepted', 'Rejected'},
                'Accepted': set(),
                'Rejected': set(),
                'Invoiced': set(),
            }

            if new_status == current_status:
                return JsonResponse({
                    'success': True,
                    'message': f"Quotation status is already '{current_status}'.",
                    'status': quotation.status
                })

            if new_status == 'Invoiced':
                return JsonResponse({
                    'success': False,
                    'error': "Status 'Invoiced' is set automatically when invoice is created."
                }, status=400)

            allowed_next = allowed_transitions.get(current_status, set())
            if new_status not in allowed_next:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid status transition: {current_status} -> {new_status}."
                }, status=400)

            quotation.status = new_status
            quotation.save()
            logger.info(f"Successfully updated quotation {quotation_id} status to: {new_status}")
            
            return JsonResponse({
                'success': True,
                'message': f"Quotation status updated to '{new_status}'.",
                'status': quotation.status
            })
        except Exception as e:
            logger.exception("Error updating quotation status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)

#for sales order

def update_order_status(request, order_id):
    """Update order status via AJAX (accepts either code or display label)."""
    if request.method == 'POST':
        try:
            order = get_object_or_404(SalesOrder, pk=order_id)
            new_status = request.POST.get('status', '').strip()
            current_status = (order.status or '').strip()
            logger.debug(f"Attempting to update order {order_id} status to: {new_status}")

            # Prepare valid codes and labels
            codes = [choice[0] for choice in SalesOrder.STATUS_CHOICES]
            labels = [choice[1] for choice in SalesOrder.STATUS_CHOICES]
            normalized_lookup = {}
            for code, label in SalesOrder.STATUS_CHOICES:
                normalized_lookup[code.strip().lower()] = code
                normalized_lookup[label.strip().lower()] = code
            normalized_lookup.update({
                'draft': 'Draft',
                'sent': 'Sent',
                'accepted': 'Accepted',
                'rejected': 'Rejected',
                'invoiced': 'Invoiced',
                'confirmed': 'Accepted',
            })

            current_status = normalized_lookup.get(current_status.lower(), current_status)
            new_status = normalized_lookup.get(new_status.lower(), new_status)
            # Determine which value to set: prefer codes, but accept labels
            if new_status in codes:
                code_to_set = new_status
            elif new_status in labels:
                idx = labels.index(new_status)
                code_to_set = codes[idx]
            else:
                # If not recognized, accept as-is but log a warning
                logger.warning(f"Unrecognized status '{new_status}' for order {order_id}; setting raw value.")
                code_to_set = new_status

            allowed_transitions = {
                'Draft': {'Sent', 'Accepted', 'Rejected'},
                'Sent': {'Accepted', 'Rejected'},
                'Accepted': set(),
                'Rejected': set(),
                'Invoiced': set(),
            }

            if code_to_set == current_status:
                return JsonResponse({
                    'success': True,
                    'message': f"Order status is already '{current_status}'.",
                    'status': order.status
                })

            if code_to_set == 'Invoiced':
                return JsonResponse({
                    'success': False,
                    'error': "Status 'Invoiced' is set automatically when invoice is created."
                }, status=400)

            allowed_next = allowed_transitions.get(current_status, set())
            if code_to_set not in allowed_next:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid status transition: {current_status} -> {code_to_set}."
                }, status=400)

            order.status = code_to_set
            order.save()
            logger.info(f"Successfully updated order {order_id} status to: {order.status}")

            return JsonResponse({
                'success': True,
                'message': f"Order status updated to '{order.status}'.",
                'status': order.status
            })
        except Exception as e:
            logger.exception("Error updating order status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)



def update_invoice_status(request, invoice_id):
    """Update invoice status via AJAX (accept codes or labels)."""
    if request.method == 'POST':
        try:
            invoice = get_object_or_404(SalesInvoice, pk=invoice_id)
            new_status = request.POST.get('status', '').strip()
            current_status = (invoice.status or '').strip()
            logger.debug(f"Attempting to update invoice {invoice_id} status to: {new_status}")

            codes = [choice[0] for choice in SalesInvoice.STATUS_CHOICES]
            labels = [choice[1] for choice in SalesInvoice.STATUS_CHOICES]

            if new_status in codes:
                code_to_set = new_status
            elif new_status in labels:
                idx = labels.index(new_status)
                code_to_set = codes[idx]
            else:
                logger.warning(f"Unrecognized status '{new_status}' for invoice {invoice_id}; setting raw value.")
                code_to_set = new_status
            allowed_transitions = {
                'Draft': {'Open'},
                'Open': {'Open'},
                'Sent': {'Open'},
                'Closed': set(),
            }
            if code_to_set == current_status:
                return JsonResponse({
                    'success': True,
                    'message': f"Invoice status is already '{current_status}'.",
                    'status': invoice.status
                })

            allowed_next = allowed_transitions.get(current_status, set())
            if code_to_set not in allowed_next:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid status transition: {current_status} -> {code_to_set}. Only 'Open' can be set manually; 'Closed' is payment-driven."
                }, status=400)
            invoice.status = code_to_set
            invoice.save()
            logger.info(f"Successfully updated invoice {invoice_id} status to: {invoice.status}")

            return JsonResponse({
                'success': True,
                'message': f"Invoice status updated to '{invoice.status}'.",
                'status': invoice.status
            })
        except Exception as e:
            logger.exception("Error updating invoice status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)
    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)

def sales_Order_list(request):
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    sales_order_qs = SalesOrder.objects.all().select_related('document_currency', 'customer', 'sales_person').order_by('-id')

    if search_query:
        sales_order_qs = sales_order_qs.filter(
            Q(sales_person__name__icontains=search_query) |
            Q(customer__first_name__icontains=search_query) |
            Q(order_number__icontains=search_query) |
            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )

    if status_filter:
        sales_order_qs = sales_order_qs.filter(status=status_filter)

    # Grouping logic
    # Assume quotation_number like "QT123-R1", "QT123-R2", "QT456"
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_orders = defaultdict(list)

    for quote in sales_order_qs:
        match = pattern.match(quote.order_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_orders[base_number].append((revision_num, quote))

    # Select latest revision per base_number
    latest_orders = []
    revisions_dict = {}

    for base_number, rev_list in grouped_orders.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_orders.append(latest)

    # Paginate latest quotations
    paginator = Paginator(latest_orders, page_size)
    page_number = request.GET.get('page')
    sales_orders = paginator.get_page(page_number)

    total_count = len(latest_orders)

    context = {
        'sales_orders': sales_orders,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': total_count,
        'page_size': page_size,
        'revisions_dict': revisions_dict,  # Pass older revisions mapped by latest quote id
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'sales/sales_order_list.html', context)

    return render(request, 'sales/sales_order_list.html', context)

def order_add(request):
    # Permission: require Create on Orders
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_orders(request.user)):
            messages.error(request, 'You do not have permission to create Sales Orders.')
            return redirect_with_company('sales_order_list')
    except Exception:
        # if permission check fails, deny by default
        messages.error(request, 'You do not have permission to create Sales Orders.')
        return redirect_with_company('sales_order_list')

    # Create form instance for SalesOrder
    try:
        db_alias = getattr(request, 'company_db', 'default')
        resolved_company = _get_company_for_request(request)
        auto_load = bool(getattr(resolved_company, 'auto_load_cash_customer', False)) if resolved_company else False
        cash_customer = None
        if auto_load:
            cash_customer = Customer.objects.using(db_alias).filter(customer_code__iexact='CASH').first()
        sp = _get_current_salesperson(request)
        initial = {'customer': cash_customer.pk if cash_customer else None}
        if sp:
            initial['sales_person'] = sp.pk
        order_form = SalesOrderForm(initial=initial)
    except Exception:
        sp = _get_current_salesperson(request)
        initial = {}
        if sp:
            initial['sales_person'] = sp.pk
        order_form = SalesOrderForm(initial=initial if initial else None)
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    # Create a formset for SalesQuotationItem if you plan multiple items
    SalesOrderItemFormSet = formset_factory(SalesOrderItemForm, extra=1)
    sales_formset = SalesOrderItemFormSet()
    all_items = Item.objects.all()
    # Currency context for order_add template
    try:
        company_currencies = Currency.objects.all()
        base_cur = Currency.objects.filter(is_base=True).first()
        company_base_currency_symbol = (getattr(base_cur, 'symbol', None) or '₹') if base_cur else '₹'
        company_base_currency_code = getattr(base_cur, 'code', '') if base_cur else ''
    except Exception:
        company_currencies = []
        company_base_currency_symbol = '₹'
        company_base_currency_code = ''
        base_cur = None

    # Pass both to the template
    resolved_company = _get_company_for_request(request)
    company_tax_type = (str(getattr(resolved_company, 'tax_type', '') or '').strip().upper() if resolved_company else '')
    
    # — Fetch TDS and TCS for order_add template
    company = resolved_company
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    
    return render(request, 'sales/order_add.html', {
        'order_form': order_form,
        'item_formset': item_formset,
        'sales_formset': sales_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': f"SQ-{SalesOrder.objects.count() + 1:05d}",
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': company_tax_type,
        'company_currencies': company_currencies,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'base_currency': base_cur,
        'tds_tax_master_items': tds_tax_master_items,
        'tcs_tax_master_items': tcs_tax_master_items,
        'show_base_transaction_summary': bool(getattr(_get_company_for_request(request), 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(resolved_company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(resolved_company, 'sales_rounding_increment', 0),
    })

def _extract_line_items(post_data, prd_brcd_map):
    rows = []

    line_items_json = (post_data.get('line_items_json') or '').strip()
    if line_items_json:
        try:
            payload_rows = json.loads(line_items_json)
        except Exception:
            payload_rows = []
        for row in payload_rows:
            if not isinstance(row, dict):
                continue
            raw_value = str(row.get('product_id') or '').strip()
            if not raw_value:
                continue
            product_id = raw_value.split('_', 1)[0]
            if not product_id:
                continue
            rows.append({
                'product_id': product_id,
                'prd_brcd': str(row.get('prd_brcd') or ''),
                'hsn_code': str(row.get('hsn_code') or ''),
                'discount': str(row.get('discount') or '0'),
                'discount_type': str(row.get('discount_type') or 'flat'),
                'quantity': str(row.get('quantity') or row.get('qty') or '0'),
                'price': str(row.get('price') or row.get('unit_price') or '0'),
                'o_price': str(row.get('o_price') or '0'),
                'tax_token': str(row.get('tax_token') or row.get('tax') or row.get('prd_tax') or ''),
            })
        if rows:
            return rows

    def _value_at(key, position=0, default=''):
        values = post_data.getlist(key)
        if not values:
            return default
        if position < len(values):
            return values[position]
        return values[-1]

    form_prefixes = sorted(
        {
            match.group(1)
            for key in post_data.keys()
            for match in [re.match(r'^(form-\d+)-product$', key)]
            if match
        },
        key=lambda prefix: int(prefix.split('-', 1)[1]),
    )

    for prefix in form_prefixes:
        product_values = [str(v).strip() for v in post_data.getlist(f"{prefix}-product") if str(v).strip()]
        if not product_values:
            continue
        for position, raw_value in enumerate(product_values):
            product_id = raw_value.split("_", 1)[0]
            if not product_id:
                continue
            rows.append({
                'product_id': product_id,
                'prd_brcd': prd_brcd_map.get(prefix, '') or _value_at(f'{prefix}-prd_brcd', position, ''),
                'hsn_code': _value_at(f'{prefix}-hsn_code', position, ''),
                'discount': _value_at(f'{prefix}-prd_disvalue', position, '0'),
                'discount_type': _value_at(f'{prefix}-prd_distype', position, 'flat'),
                'quantity': _value_at(f'{prefix}-quantity', position, '0'),
                'price': _value_at(f'{prefix}-price', position, '0'),
                'o_price': _value_at(f'{prefix}-o_price', position, '0'),
                'tax_token': _value_at(f'{prefix}-prd_tax', position, ''),
            })

    item_indices = sorted(
        {
            int(match.group(1))
            for key in post_data.keys()
            for match in [re.match(r'^items\[(\d+)\]\[(?:id|product)\]$', key)]
            if match
        }
    )

    for index in item_indices:
        product_values = [
            str(v).strip()
            for v in (post_data.getlist(f'items[{index}][id]') or post_data.getlist(f'items[{index}][product]'))
            if str(v).strip()
        ]
        if not product_values:
            continue
        for position, raw_value in enumerate(product_values):
            product_id = raw_value.split('_', 1)[0]
            if not product_id:
                continue
            rows.append({
                'product_id': product_id,
                'prd_brcd': _value_at(f'items[{index}][prd_brcd]', position, ''),
                'hsn_code': _value_at(f'items[{index}][hsn_code]', position, ''),
                'discount': _value_at(f'items[{index}][discount]', position, _value_at(f'items[{index}][prd_disvalue]', position, '0')),
                'discount_type': _value_at(f'items[{index}][discount_type]', position, _value_at(f'items[{index}][prd_distype]', position, 'flat')),
                'quantity': _value_at(f'items[{index}][qty]', position, _value_at(f'items[{index}][quantity]', position, '0')),
                'price': _value_at(f'items[{index}][price]', position, '0'),
                'o_price': _value_at(f'items[{index}][o_price]', position, '0'),
                'tax_token': _value_at(f'items[{index}][tax]', position, _value_at(f'items[{index}][prd_tax]', position, '')),
            })

    return rows


def save_salesorder(request):
    if request.method == "POST":
        # Permission: require Create on Orders
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_orders(request.user)):
                messages.error(request, 'You do not have permission to create Sales Orders.')
                return redirect_with_company('sales_order_list')
        except Exception:
            messages.error(request, 'You do not have permission to create Sales Orders.')
            return redirect_with_company('sales_order_list')
        post_data = request.POST.copy()  # make mutable copy
        db = getattr(request, 'company_db', 'default')
        prd_brcd_map = {}

        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
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


        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        raw_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
        try:
            raw_total_amount = (
                Decimal(str(raw_total).strip())
                if raw_total not in (None, '')
                else Decimal('0')
            )
        except Exception:
            raw_total_amount = Decimal('0')
        rounding_company = _get_company_for_request(request)
        total_amount, rounding_adjustment = apply_sales_rounding(raw_total_amount, rounding_company)
        customer_id = request.POST.get('customer')
        date = request.POST.get('date')
        sales_person_id = request.POST.get('sales_person')
        notes = request.POST.get('notes', '')
        order_number = generate_order_number()
        # #print("quote_number:",quote_number)
        try:
            discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
            discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_value = Decimal('0.00')
        discount_type = request.POST.get('discount_type', 'percent')

        # Capture currency fields
        currency_id = request.POST.get('document_currency', '')
        fx_rate_str = request.POST.get('fx_rate_to_base', '')
        fx_rate_date = request.POST.get('fx_rate_date', '')

        if not customer_id or not date:
            messages.error(request, "Customer and Order Date are required.")
            return redirect_with_company('sales_order_list')

        # Keep create parsing aligned with edit flow: prefer explicit form-* rows
        # over serialized line_items_json when both are present.
        has_form_rows = any(
            re.match(r'^form-\d+-product$', key or '')
            for key in post_data.keys()
        )
        if has_form_rows:
            post_data_for_items = post_data.copy()
            post_data_for_items['line_items_json'] = ''
            item_rows = _extract_line_items(post_data_for_items, prd_brcd_map)
        else:
            item_rows = _extract_line_items(post_data, prd_brcd_map)

        if not item_rows:
            messages.error(request, "At least one item is required for a sales order.")
            return redirect_with_company('sales_order_list')

        try:
            with transaction.atomic(using=db):
                customer = Customer.objects.get(pk=customer_id)
                sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None
                place_of_supply = request.POST.get('place_of_supply', '')
                company = _get_company_for_request(request)
                company_tax_type = _get_request_company_tax_type(request, company)
                turnover_tax_obj = None
                if company_tax_type == 'TURNOVER':
                    turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                    if turnover_tax_id:
                        turnover_tax_obj = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first()

                # Handle currency - use provided currency or customer's default currency
                document_currency = None
                fx_rate_to_base = Decimal('1.000000')
                
                if currency_id:
                    try:
                        document_currency = Currency.objects.get(pk=currency_id)
                    except Currency.DoesNotExist:
                        pass
                
                # If no currency provided, try to use customer's currency
                if not document_currency and customer.currency:
                    try:
                        document_currency = Currency.objects.get(code__iexact=customer.currency)
                    except Currency.DoesNotExist:
                        pass
                
                # Parse FX rate
                if fx_rate_str:
                    try:
                        fx_rate_to_base = Decimal(str(fx_rate_str))
                    except Exception:
                        fx_rate_to_base = Decimal('1.000000')
                tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
                tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
                tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
                tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))

                order = SalesOrder.objects.create(
                    customer=customer,
                    date=date,
                    sales_person=sales_person,
                    order_number=order_number,
                    total_amount=total_amount,
                    round_off=rounding_adjustment,
                    notes=notes,
                    discount_value=discount_value,
                    discount_type=discount_type,
                    place_of_supply=place_of_supply,
                    shipping_attention=request.POST.get('shipping_attention', ''),
                    shipping_email=request.POST.get('shipping_email', ''),
                    shipping_phone=request.POST.get('shipping_phone', ''),
                    shipping_country=request.POST.get('shipping_country', ''),
                    shipping_address1=request.POST.get('shipping_address1', ''),
                    shipping_address2=request.POST.get('shipping_address2', ''),
                    shipping_city=request.POST.get('shipping_city', ''),
                    shipping_state=request.POST.get('shipping_state', ''),
                    shipping_postal_code=request.POST.get('shipping_postal_code', ''),
                    tds_tcs_type=tds_tcs_type,
                    tds_tcs_definition_id=tds_tcs_definition_id,
                    tds_tcs_rate=tds_tcs_rate,
                    tds_tcs_amount=tds_tcs_amount,
                    document_currency=document_currency,
                    fx_rate_to_base=fx_rate_to_base,
                    fx_rate_date=fx_rate_date if fx_rate_date else None,
                    turnover_tax=turnover_tax_obj,
                )
                order._current_user = request.user
                order._current_request = request
                order.save()

                pay_term_id = request.POST.get('payment_term')
                if pay_term_id:
                    try:
                        pt = PayTerms.objects.get(pk=pay_term_id)
                        order.payment_term = pt
                        order.save()
                    except PayTerms.DoesNotExist:
                        pass

                for row in item_rows:
                    try:
                        quantity = Decimal(str(row['quantity'])) if row['quantity'] else Decimal('0')
                    except Exception:
                        quantity = Decimal('0')
                    try:
                        price = Decimal(str(row['price'])) if row['price'] else Decimal('0')
                    except Exception:
                        price = Decimal('0')

                    item = SalesOrderItem.objects.create(
                        sales_order=order,
                        product_id=row['product_id'],
                        prd_brcd=row.get('prd_brcd', ''),
                        hsn_code=row.get('hsn_code', ''),
                        prd_disvalue=row.get('discount', '0'),
                        prd_distype=row.get('discount_type', 'flat'),
                        quantity=quantity,
                        price=price,
                        o_price=Decimal(str(row.get('o_price') or '0')),
                    )
                    item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(row.get('tax_token', ''))
                    item.save()

                # Calculate total_amount_base using the FX rate
                order.total_amount_base = _calculate_document_base_total(
                    order.items.all(),
                    order.fx_rate_to_base,
                    order.discount_value,
                    order.discount_type,
                )
                if company_tax_type == 'TURNOVER':
                    order.total_amount_base = (
                        Decimal(order.total_amount or 0) * Decimal(order.fx_rate_to_base or 1)
                    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                order.save()
        except IntegrityError as e:
            if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
                messages.error(request, f"Order Number '{order_number}' already exists. Please choose a different one.")
            else:
                messages.error(request, "An error occurred while saving the order.")
            return redirect_with_company('sales_order_list')

        messages.success(request, "Sales Order created successfully!")
        return redirect_with_company('sales_order_list')
    else:
        return redirect_with_company('sales_order_list')


def order_detail(request, pk):
    """Render a readonly detail page for a sales order."""
    order = get_object_or_404(SalesOrder, pk=pk)
    company = _get_company_for_request(request)
    # Use a simple formset to iterate items if needed in template
    SalesOrderItemFormSet = modelformset_factory(SalesOrderItem, form=SalesOrderItemForm, extra=0)
    existing_items_qs = SalesOrderItem.objects.filter(sales_order=order)
    sales_formset = SalesOrderItemFormSet(queryset=existing_items_qs)
    # Compute per-line amounts and totals (mirror save logic)
    items_info = []
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_item_discount = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)

        # — NEW: Apply discount per unit
        if item.prd_distype == 'percent':
            discount_amount = (base * discount_val) / Decimal('100')
        else:
            # Flat discount is total for the line
            discount_amount = discount_val
        
        if discount_amount > base:
            discount_amount = base

        # Calculate discounted amount
        discounted = base - discount_amount
        if discounted < Decimal('0'):
            discounted = Decimal('0.00')

        # Calculate discount amount for display
        discount_amount = base - discounted

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        # line_total = discounted + tax_amount

        # — NEW: line_total is ONLY the discounted amount (no tax)
        line_total = discounted

        # subtotal_calc += line_total

        # — NEW: Subtotal accumulates discounted amounts (before tax)
        subtotal_calc += discounted
        total_tax += tax_amount
        total_item_discount += discount_amount

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'o_price': getattr(item, 'o_price', None),
            'base': base,
            'discount_peritem':discount_val,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
            'invoice_item_pk': item.pk,
        })

    # Split tax equally into CGST/SGST for display (simple assumption)
    total_cgst = total_tax / Decimal('2')
    total_sgst = total_tax / Decimal('2')

    # — NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax


    # Grand discount handling
    grand_discount_value = Decimal(order.discount_value or 0)
    grand_discount_type = order.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount  * grand_discount_value) / Decimal('100')
        # grand_discount = subtotal_calc * grand_discount_value / Decimal('100')
    else:
        grand_discount = grand_discount_value
    # if grand_discount > subtotal_calc:
    #     grand_discount = subtotal_calc

    if grand_discount > total_before_discount:
        grand_discount = total_before_discount

    total_discount_combined = total_item_discount + grand_discount
    final_total = order.total_amount

    # final_total = total_before_discount  - grand_discount

    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)

    # Currency context
    doc_currency = order.document_currency
    doc_currency_symbol = (getattr(doc_currency, 'symbol', None) or getattr(doc_currency, 'code', None) or '₹') if doc_currency else '₹'
    doc_currency_code = getattr(doc_currency, 'code', '') if doc_currency else ''
    fx_rate = Decimal(order.fx_rate_to_base or Decimal('1'))

    tds_tcs_type = (order.tds_tcs_type or '').strip().lower()
    tds_tcs_amount = Decimal(str(order.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate

    # Base currency info
    company_obj = None
    try:
        from currencies.models import Currency as CurrencyModel
        company_obj = company
        base_cur = (
            CurrencyModel.objects.filter(company=company_obj, is_base=True).first()
            or CurrencyModel.objects.filter(company=company_obj).first()
        )
        company_base_currency_symbol = (getattr(base_cur, 'symbol', None) or '₹') if base_cur else '₹'
        company_base_currency_code = getattr(base_cur, 'code', '') if base_cur else ''
    except Exception:
        company_base_currency_symbol = '₹'
        company_base_currency_code = ''

    if not doc_currency and order.customer and getattr(order.customer, 'currency', None):
        try:
            if company_obj:
                doc_currency = Currency.objects.filter(company=company_obj, code__iexact=order.customer.currency).first()
            else:
                doc_currency = Currency.objects.filter(code__iexact=order.customer.currency).first()
        except Exception:
            doc_currency = None

    if doc_currency:
        doc_currency_symbol = (getattr(doc_currency, 'symbol', None) or getattr(doc_currency, 'code', None) or '').strip()
        doc_currency_code = getattr(doc_currency, 'code', '') or ''
    else:
        doc_currency_symbol = company_base_currency_symbol
        doc_currency_code = company_base_currency_code

    if doc_currency and not getattr(doc_currency, 'is_base', False) and fx_rate == Decimal('1'):
        try:
            from currencies.services import get_effective_rate_to_base
            fx_rate = get_effective_rate_to_base(doc_currency, order.fx_rate_date or order.date)
        except Exception:
            fx_rate = Decimal('1')

    subtotal_calc_base = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    total_item_discount_base = Decimal('0.00')
    for itm in items_info:
        price = Decimal(itm.get('price') or 0)
        qty = Decimal(itm.get('quantity') or 0)
        price_base = Decimal(itm.get('o_price') or 0) or (price * fx_rate)
        discount_value = Decimal(itm.get('discount_peritem') or 0)
        discount_type = itm.get('discount_type') or 'flat'
        line_base = price_base * qty
        if discount_type == 'percent':
            discount_amount_base = (line_base * discount_value) / Decimal('100')
        else:
            discount_amount_base = discount_value * fx_rate
        if discount_amount_base > line_base:
            discount_amount_base = line_base
        line_total_base = line_base - discount_amount_base
        tax_rate = Decimal(itm.get('tax_rate') or 0)
        tax_amount_base = (line_total_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        itm['price_base'] = price_base
        itm['line_total_base'] = line_total_base
        itm['discount_amount_base'] = discount_amount_base
        itm['tax_amount_base'] = tax_amount_base

        subtotal_calc_base += line_total_base
        total_tax_base += tax_amount_base
        total_item_discount_base += discount_amount_base

    total_cgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')
    total_sgst_base = (total_tax_base / 2) if total_tax_base else Decimal('0.00')

    total_before_discount_base = subtotal_calc_base + total_tax_base
    if grand_discount_type == 'percent':
        grand_discount_base = (total_before_discount_base * grand_discount_value) / Decimal('100')
    else:
        grand_discount_base = grand_discount_value * fx_rate
    if grand_discount_base > total_before_discount_base:
        grand_discount_base = total_before_discount_base

    final_total_base = total_before_discount_base - grand_discount_base
    total_discount_combined_base = total_item_discount_base + grand_discount_base
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = total_before_discount - grand_discount
        final_total = Decimal(order.total_amount or pre_turnover_total)
        turnover_tax_amount = final_total - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')

        pre_turnover_total_base = final_total_base
        final_total_base = Decimal(order.total_amount_base or (final_total * fx_rate))
        turnover_tax_amount_base = final_total_base - pre_turnover_total_base
        if turnover_tax_amount_base < Decimal('0.00'):
            turnover_tax_amount_base = Decimal('0.00')

    context = {
        'order': order,
        'o_no': order.order_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax': total_tax,
        'total_tax_base': total_tax_base,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst_base,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst_base,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'total_discount_combined': total_discount_combined,
        'total_discount_combined_base': total_discount_combined_base,
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,
        'final_total': final_total,
        'final_total_base': final_total_base,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'company_country': company_country,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_cur,
        'fx_rate_to_base': fx_rate,
        'fx_rate': fx_rate,
        'total_amount_base': order.total_amount_base,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }

    return render(request, 'sales/order_detail.html', context)


def order_print_view(request, pk):
    """Render a printable HTML page for the sales order (for printing in browser)."""
    context = build_order_context(pk, request)
    return render(request, 'sales/order_print.html', context)


def order_pdf_view(request, pk):
    """generate a PDF for the sales order using ReportLab with proper rupee symbol support."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    
    order = get_object_or_404(SalesOrder, pk=pk)
    context = build_order_context(pk, request)
    
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
                          title=f'Sales Order {pk}',
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
    #added on 20-1-26 neha
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
    # HEADER with professional styling
    header_data = [
        [header_left_cell, Paragraph("ORDER", order_badge_style)],
    ]
    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    # COMPANY INFO - Professional styling
    #added on 20-1-26 neha
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
    sales_person_obj = getattr(order, 'sales_person', None)
    sales_person_name = (getattr(sales_person_obj, 'name', None) or '').strip()
    sales_person_phone = (getattr(sales_person_obj, 'phone', None) or '').strip()
    if sales_person_name and sales_person_phone:
        sales_person_display = f"{sales_person_name} ({sales_person_phone})"
    elif sales_person_name:
        sales_person_display = sales_person_name
    else:
        sales_person_display = '-'
    meta_data = [
        [Paragraph("<b>Order No</b>", meta_label_style), Paragraph(str(context.get('o_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(order.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(order.place_of_supply or '-'), meta_value_style),
         Paragraph("<b>Sales Person</b>", meta_label_style), Paragraph(str(sales_person_display), meta_value_style)],
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
    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if order.customer:
        bill_lines = []
        customer_type = (getattr(order.customer, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(order.customer, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(order.customer, 'first_name', '') or '').strip()
            last_name = (getattr(order.customer, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)

        if getattr(order.customer, 'address_line_1', None):
            bill_lines.append(order.customer.address_line_1)

        # Use shipping_state when shipping address exists, otherwise use customer state
        if order.shipping_address1 or order.shipping_city:
            if getattr(order, 'shipping_state', None):
                bill_lines.append(order.shipping_state)
        else:
            if getattr(order.customer, 'state', None):
                bill_lines.append(order.customer.state)

        # Use customer.country 
        if getattr(order.customer, 'country', None):
            bill_lines.append(str(order.customer.country))

        gst = getattr(order.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if order.shipping_address1 or order.shipping_city:
        ship_lines = []
        
        if getattr(order, 'shipping_attention', None):
            ship_lines.append(f"Attention To: {order.shipping_attention}")
        
        if getattr(order, 'shipping_address1', None):
            ship_lines.append(order.shipping_address1)
        
        if getattr(order, 'shipping_address2', None):
            ship_lines.append(order.shipping_address2)
        
        if getattr(order, 'shipping_city', None):
            city_line = order.shipping_city
            if getattr(order, 'shipping_postal_code', None):
                city_line += f" - {order.shipping_postal_code}"
            ship_lines.append(city_line)
        
        if getattr(order, 'shipping_state', None):
            ship_lines.append(order.shipping_state)
        
        if getattr(order, 'shipping_country', None):
            ship_lines.append(str(order.shipping_country))
        
        if getattr(order, 'shipping_email', None):
            ship_lines.append(f"Email: {order.shipping_email}")
        
        if getattr(order, 'shipping_phone', None):
            ship_lines.append(f"Phone: {order.shipping_phone}")
        
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
    
    # ITEMS TABLE - Professional styling with conditional VAT/CGST-SGST
    currency = context.get('document_currency_symbol') or ('₹' if font_registered else 'Rs.')
    base_currency = context.get('company_base_currency_symbol') or ('₹' if font_registered else 'Rs.')
    fx_rate = context.get('fx_rate', 1)

    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', 'GST')
    print("company_tax_type for order is:", company_tax_type)
    # Conditional table headers based on country
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
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        
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
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
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
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) - Decimal(str(item.get('tax_amount', 0)))
    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    totals_value_style = ParagraphStyle(
        'TotalValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    
    totals_style_final = ParagraphStyle(
        'TotalFinal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#333333'),
        fontName=font_name,
        alignment=TA_RIGHT
    )
    if company_tax_type in ('GST', 'VAT', 'SALES', 'TURNOVER'):
        totals_data = [

            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('subtotal_calc', 0):.2f}", totals_value_style)],
        ]
    elif company_tax_type == 'NONE':
        totals_data = []
    
    if company_tax_type == 'GST':
        totals_data.append([Paragraph("CGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_cgst', 0):.2f}", totals_value_style)])
        totals_data.append([Paragraph("SGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_sgst', 0):.2f}", totals_value_style)])
    elif company_tax_type in ('VAT', 'SALES'):
        totals_data.append([Paragraph("Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_tax', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'TURNOVER':
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('turnover_tax_amount', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'NONE':
        pass  # No tax rows for NONE type
    round_off = getattr(order, 'round_off', None)
    if round_off:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}&nbsp;{float(round_off):.2f}", totals_value_style)])
    # Show Turnover Tax row only for companies registered with TURNOVER tax type
    company_tax_type = context.get('company_tax_type', '')
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{float(turnover_amt):.2f}", totals_value_style)])
    
    totals_data.append([Paragraph("<b>Grand Total</b>", totals_label_style), Paragraph(f"<b>{currency}&nbsp;{context.get('final_total', 0):.2f}</b>", totals_style_final)])
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
    bottom_table = Table(bottom_data, colWidths=[330, 190])
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
    
    order = get_object_or_404(SalesOrder, pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="order_{order.order_number}.pdf"'
    return response



def build_order_context(pk, request=None):
    order = get_object_or_404(SalesOrder, pk=pk)
    #added by neha on 20-1-26
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    
    # Currency and FX setup
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else ''
    base_currency_code = base_currency.code if base_currency else ""

    doc_currency = order.document_currency
    doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip() if doc_currency else ''
    doc_currency_code = doc_currency.code if doc_currency else ""

    fx_rate = Decimal(getattr(order, 'fx_rate_to_base', 1) or 1)

    existing_items_qs = SalesOrderItem.objects.filter(sales_order=order)
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
        base_totals = _calculate_line_base_totals(item, fx_rate=fx_rate)

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'o_price': getattr(item, 'o_price', None),
            'price_base': base_totals['unit_base_price'],
            'base': base,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'tax_amount_base': base_totals['tax_amount_base'],
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'cgst_amount_base': (base_totals['tax_amount_base'] / 2) if base_totals['tax_amount_base'] else Decimal('0.00'),
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'sgst_amount_base': (base_totals['tax_amount_base'] / 2) if base_totals['tax_amount_base'] else Decimal('0.00'),
            'taxable_value': discounted,
            'line_total': line_total,
            'line_total_base': base_totals['line_total_base'],
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')

    grand_discount_value = Decimal(order.discount_value or 0)
    grand_discount_type = order.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > subtotal_calc:
        grand_discount = subtotal_calc

    final_total = order.total_amount

    # — Add company_is_india for conditional tax display
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = subtotal_calc - grand_discount
        turnover_tax_amount = Decimal(order.total_amount or 0) - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')
        turnover_tax_amount_base = turnover_tax_amount * fx_rate

    company_country_code = ''
    if company and hasattr(company, 'country'):
        if hasattr(company.country, 'code'):
            company_country_code = company.country.code
        elif isinstance(company.country, str):
            company_country_code = company.country
    
    company_is_india = _is_indian_company_country(company_country_code)

    context = {
        'order': order,
        'o_no': order.order_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'total_tax': total_tax,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'total_discount_combined': total_item_discount + grand_discount,
        'subtotal_calc_base': subtotal_calc * fx_rate,
        'total_tax_base': total_tax * fx_rate,
        'total_cgst_base': total_cgst * fx_rate,
        'total_sgst_base': total_sgst * fx_rate,
        'final_total_base': (final_total * fx_rate) if final_total is not None else Decimal('0.00'),
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'fx_rate': fx_rate,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }
    return context


def order_edit(request, pk):
    #for viewing only
    readonly = request.GET.get('readonly', 'false').lower() == 'true'
    # If user doesn't have edit permission, force readonly mode
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_orders(request.user)):
            readonly = True
    except Exception:
        readonly = True
    order = get_object_or_404(SalesOrder, pk=pk)
    SalesOrderItemFormSet = modelformset_factory(SalesOrderItem, form=SalesOrderItemForm, extra=0,can_delete=True)

    if request.method == "POST"and not readonly:
        # Permission: require Edit on Orders
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_orders(request.user)):
                messages.error(request, 'You do not have permission to edit Sales Orders.')
                return redirect_with_company('sales_order_list')
        except Exception:
            messages.error(request, 'You do not have permission to edit Sales Orders.')
            return redirect_with_company('sales_order_list')
        post_data = request.POST.copy()
        prd_brcd_map = {}
        for key in post_data:
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                if value:
                    parts = value.split("_", 1)
                    item_id = (parts[0] or '').strip()
                    if not item_id.isdigit():
                        post_data[key] = ''
                        continue
                    post_data[key] = item_id
                    if len(parts) > 1:
                        prd_brcd_map[key.rsplit("-", 1)[0]] = parts[1]

        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)
        grand_total_value = (post_data.get('grandTotal') or '').strip()
        grand_discount_raw = (post_data.get('grand-discount-value') or '').strip()
        posted_discount_type = (post_data.get('discount_type') or '').strip()

        def _coerce_bound_value(field_name, value):
            if value is None:
                return ''
            try:
                field = SalesOrderForm().fields.get(field_name)
            except Exception:
                field = None
            if hasattr(value, 'pk'):
                return str(value.pk)
            if hasattr(value, 'isoformat'):
                return value.isoformat()
            if field_name in ('total_amount', 'total_amount_base') and grand_total_value:
                return grand_total_value
            return str(value)

        preserved_fields = [
            'order_number',
            'status',
            'total_amount',
            'total_amount_base',
            'discount_value',
            'discount_type',
            'round_off',
        ]
        for field_name in preserved_fields:
            if field_name not in post_data:
                post_data[field_name] = _coerce_bound_value(field_name, getattr(order, field_name, ''))
        if grand_discount_raw:
            post_data['discount_value'] = grand_discount_raw
        if posted_discount_type:
            post_data['discount_type'] = posted_discount_type

        order_form = SalesOrderForm(post_data, instance=order)
        existing_items_qs = SalesOrderItem.objects.filter(sales_order=order)
        sales_formset = SalesOrderItemFormSet(post_data, queryset=existing_items_qs)

        if order_form.is_valid() and sales_formset.is_valid():
            # attach user/request so activity log picks it up even though the model lacks
            order_form._current_user = request.user
            order_form._current_request = request

            order_obj = order_form.save(commit=False)
            try:
                discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
                order_obj.discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            except (InvalidOperation, ValueError, TypeError):
                order_obj.discount_value = Decimal('0.00')
            order_obj.discount_type = request.POST.get('discount_type', 'percent')
            order_obj.tds_tcs_type = request.POST.get('tds_tcs_type', order_obj.tds_tcs_type or 'tds')
            order_obj.tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
            order_obj.tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
            order_obj.tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
            company = _get_company_for_request(request)
            if _get_request_company_tax_type(request, company) == 'TURNOVER':
                turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                order_obj.turnover_tax = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first() if turnover_tax_id else None
            else:
                order_obj.turnover_tax = None

            # Currency fields
            currency_id = request.POST.get('document_currency', '')
            fx_rate_str = request.POST.get('fx_rate_to_base', '')
            fx_rate_date = request.POST.get('fx_rate_date', '')
            if currency_id:
                try:
                    order_obj.document_currency = Currency.objects.get(pk=currency_id)
                except Currency.DoesNotExist:
                    pass
            if fx_rate_str:
                try:
                    order_obj.fx_rate_to_base = Decimal(str(fx_rate_str))
                except Exception:
                    pass
            order_obj.fx_rate_date = fx_rate_date if fx_rate_date else None

            order_obj.save()

            # Update total_amount from grandTotal (the JS-calculated value)
            grand_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal', '')
            if grand_total:
                try:
                    rounding_company = _get_company_for_request(request)
                    order_obj.total_amount, order_obj.round_off = apply_sales_rounding(grand_total, rounding_company)
                    if order_obj.fx_rate_to_base and order_obj.fx_rate_to_base != Decimal('1.000000'):
                        order_obj.total_amount_base = (order_obj.total_amount * order_obj.fx_rate_to_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    else:
                        order_obj.total_amount_base = order_obj.total_amount
                    order_obj.save(update_fields=['total_amount', 'total_amount_base', 'round_off'])
                except Exception:
                    pass

            instances = sales_formset.save(commit=False)
            for deleted_item in sales_formset.deleted_objects:
                deleted_item.delete()

            for form in sales_formset.forms:
                if not hasattr(form, 'cleaned_data') or not form.cleaned_data:
                    continue
                if form.cleaned_data.get('DELETE'):
                    continue
                if not form.cleaned_data.get('product'):
                    continue

                item = form.save(commit=False)
                item.sales_order = order_obj
                try:
                    item.o_price = Decimal(str(post_data.get(f'{form.prefix}-o_price', '0') or '0'))
                except (InvalidOperation, ValueError, TypeError):
                    item.o_price = Decimal('0.00')

                prefix = form.prefix
                if prefix in prd_brcd_map:
                    item.prd_brcd = prd_brcd_map[prefix]
                item.hsn_code = post_data.get(f'{prefix}-hsn_code', '') or item.hsn_code

                selected_tax = (post_data.get(f'{prefix}-prd_tax', '') or '').strip()
                item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                item.save()

            order_obj.total_amount_base = _calculate_document_base_total(
                order_obj.items.all(),
                order_obj.fx_rate_to_base,
                order_obj.discount_value,
                order_obj.discount_type,
            )
            if _get_request_company_tax_type(request, company) == 'TURNOVER':
                order_obj.total_amount_base = (Decimal(order_obj.total_amount or 0) * Decimal(order_obj.fx_rate_to_base or 1)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            order_obj.save(update_fields=['total_amount_base'])
            sales_formset.save_m2m()
            messages.success(request, "Sales Order updated successfully!")
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                response_currency = order_obj.document_currency
                currency_symbol = (
                    getattr(response_currency, 'symbol', None)
                    or getattr(response_currency, 'code', None)
                    or '₹'
                ) if response_currency else '₹'
                return JsonResponse({
                    'success': True,
                    'order_id': order_obj.id,
                    'order_number': order_obj.order_number,
                    'customer_name': get_customer_display_name(order_obj.customer) if order_obj.customer else 'N/A',
                    'total_amount': str(order_obj.total_amount),
                    'currency_symbol': currency_symbol,
                    'message': "Sales Order updated successfully!",
                })
            return redirect_with_company('sales_order_list')
        elif request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            error_msg = 'Please correct the highlighted order details.'
            if order_form.errors:
                try:
                    first_field = next(iter(order_form.errors))
                    first_error = order_form.errors[first_field][0]
                    field_label = order_form.fields[first_field].label if first_field in order_form.fields else first_field
                    error_msg = f'{field_label}: {first_error}'
                except Exception:
                    pass
            elif sales_formset.non_form_errors():
                try:
                    error_msg = sales_formset.non_form_errors()[0]
                except Exception:
                    pass
            else:
                for form_errors in sales_formset.errors:
                    if form_errors:
                        try:
                            first_field = next(iter(form_errors))
                            first_error = form_errors[first_field][0]
                            error_msg = f'{first_field}: {first_error}'
                        except Exception:
                            pass
                        break
            return JsonResponse({
                'success': False,
                'error': str(error_msg),
                'order_errors': order_form.errors,
                'formset_errors': sales_formset.errors,
                'non_form_errors': sales_formset.non_form_errors(),
            }, status=400)
    else:
        existing_items_qs = SalesOrderItem.objects.filter(sales_order=order)
        sales_formset = SalesOrderItemFormSet(queryset=existing_items_qs)
        sp = _get_current_salesperson(request)
        if not getattr(order, 'sales_person', None) and sp:
            order_form = SalesOrderForm(instance=order, initial={'sales_person': sp.pk})
        else:
            order_form = SalesOrderForm(instance=order)
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



        # — Fetch tax group names in bulk (to avoid N+1 queries)
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

        for form, item in zip(sales_formset.forms, existing_items_qs):
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            # #print(f"Checking key {key} ₹ matched UOM: {uom_name}")

            # — Force the dropdown to display the correct text for this specific instance
            if uom_name:
                display_label = f"{item.product.name} ({uom_name})"
            else:
                display_label = item.product.name

            # — Replace the field choices with the selected item label
            form.fields['product'].choices = [
                (item.product.id, display_label)
            ]
            form.initial['product'] = item.product.id
            # #print(f"₹ Updated dropdown for {display_label}")


        # for item in existing_items_qs:
            # #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

        # — Combine product + UOM + Tax Group for display
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
    # return render(request, 'sales/quotation_duplicate.html', {
    #     'quotation_form': quotation_form,
    #     'sales_formset': sales_formset,
    #     'all_items': all_items,
    #     'today': localdate().isoformat(),
    #     'q_no': quote.quote_number,
    #     # 'item_display_list': item_display_list,
    #     'quote': quote, 
    #     'readonly': readonly,
        
    # })
    # Currency context for edit form
    try:
        company_currencies = Currency.objects.all()
        base_cur = Currency.objects.filter(is_base=True).first()
        company_base_currency_symbol = (getattr(base_cur, 'symbol', None) or '\u20b9') if base_cur else '\u20b9'
        company_base_currency_code = getattr(base_cur, 'code', '') if base_cur else ''
    except Exception:
        company_currencies = []
        company_base_currency_symbol = '\u20b9'
        company_base_currency_code = ''
        base_cur = None

    context = {
        'order_form': order_form,
        'sales_formset': sales_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': order.order_number,
        'order': order,
        'readonly': readonly,
        'shipping_attention': order.shipping_attention or '',
        'shipping_email': order.shipping_email or '',
        'shipping_phone': order.shipping_phone or '',
        'shipping_country': order.shipping_country or '',
        'shipping_address1': order.shipping_address1 or '',
        'shipping_address2': order.shipping_address2 or '',
        'shipping_city': order.shipping_city or '',
        'shipping_state': order.shipping_state or '',
        'shipping_postal_code': order.shipping_postal_code or '',
        'company_country': _get_current_company_country(request),
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': _get_request_company_tax_type(request, _get_company_for_request(request)),
        'company_currencies': company_currencies,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'base_currency': base_cur,
        'selected_currency_id': order.document_currency_id or '',
        'fx_rate_to_base': order.fx_rate_to_base,
        'fx_rate_date': order.fx_rate_date,
        'tds_tcs_type': order.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': order.tds_tcs_definition_id or '',
        'tds_tcs_rate': order.tds_tcs_rate or 0,
        'tds_tcs_amount': order.tds_tcs_amount or 0,
        # — Fetch TDS and TCS for order_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        'show_base_transaction_summary': bool(getattr(_get_company_for_request(request), 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(_get_company_for_request(request), 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(_get_company_for_request(request), 'sales_rounding_increment', 0),
    }
    if readonly:
        for form in sales_formset.forms:
            for field_name, field in form.fields.items():
                form.fields['product'].widget.attrs['disabled'] = True
                form.fields['prd_tax'].widget.attrs['disabled'] = True

                widget = field.widget
                if widget.__class__.__name__ in ['Select', 'SelectMultiple', 'CheckboxInput', 'RadioSelect']:
                    widget.attrs['disabled'] = True  # disable selects and similar widgets
                else:
                    widget.attrs['readonly'] = True
        # Render a dedicated readonly template without edit actions/buttons
        return render(request, 'sales/order_view.html', context)
    else:
        # Render the editable template
        return render(request, 'sales/order_duplicate.html', context)


# def order_duplicate(request, pk):
#     original_order = get_object_or_404(SalesOrder, pk=pk)
#     original_order_number = original_order.order_number
#     # #print("original_quote_number:", original_quote_number)
#     if request.method == "POST":
#         post_data = request.POST.copy()  # make mutable copy
#         prd_brcd_map = {}

#         # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
#         for key in post_data:
#             # Identify product field keys
#             if key.startswith("form-") and key.endswith("-product"):
#                 value = post_data[key]
#                 # #print(f"Key matched: {key} with value: '{value}'")
#                 if value:
#                     parts = value.split("_", 1)
#                     post_data[key] = parts[0]  # Save only item id for product field
#                     # #print("Updated post_data[key]:", post_data[key])
#                     if len(parts) > 1:
#                         # Map barcode corresponding to this form prefix
#                         prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
#                         prd_brcd_map[prefix] = parts[1]
#                         # #print("prd_brcd_map[prefix]:",prd_brcd_map[prefix])


#         total_amount = request.POST.get('grandTotal')
#         customer_id = request.POST.get('customer')
#         date = request.POST.get('date')
#         sales_person_id = request.POST.get('sales_person')
#         notes = request.POST.get('notes', '')
#         # quote_number = generate_quote_number()
#         order_number = generate_revised_order_number(original_order_number)
#         # #print("quote_number:",quote_number)
    #         try:
    #             discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
    #             discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    #         except (InvalidOperation, ValueError, TypeError):
    #             discount_value = Decimal('0.00')
    #         discount_type = request.POST.get('discount_type', 'percent')

#         if not customer_id or not date:
#             messages.error(request, "Customer and Quotation Date are required.")
#             return redirect('sales_quote_list')

#         try:
#             customer = Customer.objects.get(pk=customer_id)
#             sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None

#             order = SalesOrder.objects.create(
#                 customer=customer,
#                 date=date,
#                 sales_person=sales_person,
#                 order_number=order_number,
#                 total_amount=total_amount,
                # fx_rate_to_base=Decimal('1.000000'),
#                 notes=notes,
#                 discount_value=discount_value,
#                 discount_type=discount_type,
#                 place_of_supply=request.POST.get('place_of_supply', ''),
#                 shipping_attention=request.POST.get('shipping_attention', ''),
#                 shipping_email=request.POST.get('shipping_email', ''),
#                 shipping_phone=request.POST.get('shipping_phone', ''),
#                 shipping_country=request.POST.get('shipping_country', ''),
#                 shipping_address1=request.POST.get('shipping_address1', ''),
#                 shipping_address2=request.POST.get('shipping_address2', ''),
#                 shipping_city=request.POST.get('shipping_city', ''),
#                 shipping_state=request.POST.get('shipping_state', ''),
#                 shipping_postal_code=request.POST.get('shipping_postal_code', ''),
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
#                 messages.error(request, "An error occurred while saving the quotation.")
#             return redirect('sales_order_list')

#         SalesOrderItemFormSet = modelformset_factory(
#             SalesOrderItem, form=SalesOrderItemForm, extra=0, can_delete=True
#         )

#         formset = SalesOrderItemFormSet(post_data, queryset=SalesOrderItem.objects.none())

#         if formset.is_valid():
#             items = formset.save(commit=False)
#             for index, item in enumerate(items):
#                 #added for edit save
#                 item.pk = None
                
#                 prefix = f"form-{index}"              # formset form key pattern
#                 if prefix in prd_brcd_map:            # check if barcode was extracted
#                     item.prd_brcd = prd_brcd_map[prefix]
                
#                 tax_group_id = post_data.get(f'form-{index}-prd_tax', '').strip()
#                 if tax_group_id:
#                     tax_group = TaxGroup.objects.filter(id=tax_group_id).prefetch_related('taxes').first()
#                     if tax_group:
#                         total_rate = Decimal(sum(t.rate for t in tax_group.taxes.all()))
#                         item.prd_tax = total_rate
#                         item.prd_taxgroup = tax_group.group_name
#                     else:
#                         item.prd_tax = Decimal(0)
#                         item.prd_taxgroup = None
#                 else:
#                     item.prd_tax = Decimal(0)
#                     item.prd_taxgroup = None
#                 item.sales_order = order          # set foreign key
#                 item.save()                
#             for deleted_item in formset.deleted_objects:
#                 deleted_item.delete()

#             messages.success(request, "Sales Order created successfully!")
#             return redirect('sales_order_list')
#         else:
#             # #print("Formset errors:", formset.errors)
#             # for form in formset:
#             #     #print("Individual form errors:", form.errors)
#             print("Formset errors:", formset.errors)
#             for i, form in enumerate(formset.forms):
#                 print(f"Errors in form {i}:", form.errors)
#             messages.error(request, "There are errors with the items in the order duplicate.")
#             return redirect('sales_order_list')
#     else:
#         return redirect('sales_order_list')


@transaction.atomic
def order_duplicate(request, pk):
    old_order = get_object_or_404(SalesOrder, pk=pk)
    order_number = old_order.order_number

    if request.method != "POST":
        return redirect_with_company('sales_order_list')

    # ---------- PERMISSION ----------
    if not (request.user.is_superuser or can_create_orders(request.user)):
        messages.error(request, "You do not have permission to create Orders.")
        return redirect_with_company('sales_order_list')

    # ---------- DELETE OLD ORDER (FREE UNIQUE NUMBER) ----------
    try:
        SalesOrderItem.objects.filter(sales_order=old_order).delete()
        old_order.delete()
    except Exception as e:
        logger.exception("Error deleting old order: %s", e)
        messages.error(request, "Unable to replace the existing order.")
        return redirect_with_company('sales_order_list')

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
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    post_data = _normalize_item_tax_tokens(post_data, company_is_india)
    # ---------- HEADER ----------
    customer_id = post_data.get('customer')
    date = post_data.get('date')

    if not customer_id or not date:
        messages.error(request, "Customer and Date are required.")
        return redirect_with_company('sales_order_list')

    customer = get_object_or_404(Customer, pk=customer_id)
    credit_limit_error = build_customer_credit_limit_message(customer, post_data.get('grandTotal'))
    if credit_limit_error:
        return credit_limit_block_response(request, credit_limit_error, 'sales_order_list')

    try:
        discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
        discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        discount_value = Decimal('0.00')
    discount_type = request.POST.get('discount_type', 'percent')
    sales_person_id = post_data.get('sales_person')
    sales_person = None
    if sales_person_id:
        sales_person = SalesPerson.objects.filter(pk=sales_person_id).first()


    # ---------- CREATE NEW ORDER ----------
    order = SalesOrder.objects.create(
        customer=customer,
        date=date,
        sales_person=sales_person,
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

    # ---------- ITEMS ----------
    item_rows = _extract_line_items(post_data, prd_brcd_map)
    if not item_rows:
        messages.error(request, "At least one item is required for a sales order.")
        raise Exception("No order items found")

    calculated_total = Decimal('0.00')

    for row in item_rows:
        quantity = Decimal(str(row.get('quantity') or '0'))
        price = Decimal(str(row.get('price') or '0'))

        item = SalesOrderItem.objects.create(
            sales_order=order,
            product_id=row['product_id'],
            prd_brcd=row.get('prd_brcd', ''),
            hsn_code=row.get('hsn_code', ''),
            prd_disvalue=row.get('discount', '0'),
            prd_distype=row.get('discount_type', 'flat'),
            quantity=quantity,
            price=price,
        )
        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(row.get('tax_token', ''))
        item.save(update_fields=['prd_tax', 'prd_taxgroup'])

        base = quantity * price
        disc = Decimal(str(item.prd_disvalue or 0))
        discounted = (
            base - (base * disc / Decimal('100'))
            if item.prd_distype == 'percent'
            else base - disc
        )

        discounted = max(discounted, Decimal('0'))
        tax_amt = discounted * Decimal(item.prd_tax or 0) / Decimal('100')

        calculated_total += discounted + tax_amt

    # ---------- GRAND DISCOUNT ----------
    gd_val = Decimal(discount_value)
    grand_discount = (
        calculated_total * gd_val / 100
        if discount_type == 'percent'
        else gd_val
    )

    order.total_amount = calculated_total - min(grand_discount, calculated_total)
    order.save()

    messages.success(request, "Order edited successfully.")
    return redirect_with_company('sales_order_list')


@require_POST
def delete_sales_order(request, order_id):
    # Permission: require Delete on Orders
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_orders(request.user)):
            messages.error(request, 'You do not have permission to delete Sales Orders.')
            return redirect_with_company('sales_order_list')
    except Exception:
        messages.error(request, 'You do not have permission to delete Sales Orders.')
        return redirect_with_company('sales_order_list')

    order = get_object_or_404(SalesOrder, pk=order_id)
    order.delete()
    messages.success(request, f"Sales Order '{order.order_number}' deleted successfully.")
    return redirect_with_company('sales_order_list')

def get_financial_year(date_obj=None):
    """Return fiscal year string like '2025-26' based on given date or today.
    Financial year assumed Apr (4) - Mar (3).
    """
    try:
        d = date_obj or localdate()
        # accept datetime/date or ISO date string
        if isinstance(d, str):
            try:
                import datetime as _dt
                d = _dt.date.fromisoformat(d)
            except Exception:
                d = localdate()
    except Exception:
        d = localdate()

    year = getattr(d, 'year', localdate().year)
    month = getattr(d, 'month', localdate().month)
    if month >= 4:
        start = year
        end = year + 1
    else:
        start = year - 1
        end = year
    return f"{start}-{str(end)[-2:]}"


def generate_inv_number(date_obj=None):
    """Generate invoice number including the fiscal year suffix.

    Examples: IN001-2025-26
    If `date_obj` is provided (date/datetime or ISO string), fiscal year is derived from it.
    """
    prefix_obj = InvoicePrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "IN"

    # Fetch all existing invoice numbers
    all_invs = SalesInvoice.objects.values_list('inv_number', flat=True)

    max_number = 0
    pattern = re.compile(r'(\d+)')  # Extract first numeric group

    for q in all_invs:
        if not q:
            continue
        match = pattern.search(q)
        if match:
            try:
                num = int(match.group(1))
            except Exception:
                continue
            if num > max_number:
                max_number = num

    new_number = max_number + 1
    fy = get_financial_year(date_obj)
    return f"{prefix}{str(new_number).zfill(3)}-{fy}"


def generate_performa_number(date_obj=None):
    """Generate performa invoice number including the fiscal year suffix."""
    prefix_obj = PerformaInvoicePrefix.objects.first()
    prefix = prefix_obj.prefix if prefix_obj else "PI"

    all_docs = PerformaInvoice.objects.values_list('inv_number', flat=True)
    max_number = 0
    pattern = re.compile(r'(\d+)')

    for number in all_docs:
        if not number:
            continue
        match = pattern.search(number)
        if not match:
            continue
        try:
            numeric_part = int(match.group(1))
        except Exception:
            continue
        if numeric_part > max_number:
            max_number = numeric_part

    new_number = max_number + 1
    fy = get_financial_year(date_obj)
    return f"{prefix}{str(new_number).zfill(3)}-{fy}"

@transaction.atomic
def convert_quotation_to_inv(request, quotation_id):
    quotation = get_object_or_404(SalesQuotation, pk=quotation_id)

    # Status rule: quotation must be Accepted before converting to invoice.updt on 23-2-26 by neha
    if quotation.status != 'Accepted':
        messages.error(request, 'Only Accepted quotations can be converted to Invoice.')
        return redirect_with_company('quotation_detail', pk=quotation.pk)

    # Allow forcing a re-invoice via query param `force=1`.
    existing = SalesInvoice.objects.filter(origin_quote=quotation).first()
    force = str(request.GET.get('force', '')).lower() in ('1', 'true', 'yes')
    if existing and not force:
        messages.warning(request, 'This quote has already been invoiced.')
        return redirect_with_company('quotation_detail', pk=quotation.pk)
    credit_limit_error = build_customer_credit_limit_message(quotation.customer, quotation.total_amount)
    if credit_limit_error:
        return credit_limit_block_response(request, credit_limit_error, 'quotation_detail', pk=quotation.pk)

    # Generate a unique invoice number (include fiscal year based on conversion date)
    inv_number = generate_inv_number(timezone.now().date())

    # Create SalesOrder from SalesQuotation
    sales_inv = SalesInvoice.objects.create(
        customer=quotation.customer,
        inv_number=inv_number,
        date=timezone.now(),
        origin_quote=quotation,
        sales_person=quotation.sales_person,
        notes=quotation.notes,
        payment_term=quotation.payment_term,
        discount_value=quotation.discount_value,
        discount_type=quotation.discount_type,
        status='Open',
        total_amount=quotation.total_amount,
        fx_rate_to_base=Decimal('1.000000'),
        place_of_supply=quotation.place_of_supply,
        shipping_attention=quotation.shipping_attention,
        shipping_email=quotation.shipping_email,
        shipping_phone=quotation.shipping_phone,
        shipping_country=quotation.shipping_country,
        shipping_address1=quotation.shipping_address1,
        shipping_address2=quotation.shipping_address2,
        shipping_city=quotation.shipping_city,
        shipping_state=quotation.shipping_state,
        shipping_postal_code=quotation.shipping_postal_code,
        tds_tcs_type=quotation.tds_tcs_type,
        tds_tcs_definition_id=quotation.tds_tcs_definition_id,
        tds_tcs_rate=quotation.tds_tcs_rate,
        tds_tcs_amount=quotation.tds_tcs_amount,
    )

    try:
        company = _get_company_for_request(request)
        from currencies.services import apply_sales_invoice_fx

        apply_sales_invoice_fx(sales_inv, company)
    except Exception:
        logger.exception('Failed to apply FX to invoice %s', getattr(sales_inv, 'pk', None))

    # Copy SalesQuotationItems to SalesOrderItems
    quotation_items = SalesQuotationItem.objects.filter(Sales_quotation=quotation)
    for item in quotation_items:
        SalesInvoiceItem.objects.create(
            sales_inv=sales_inv,
            product=item.product,
            prd_brcd=item.prd_brcd,
            hsn_code=item.hsn_code,
            prd_tax=item.prd_tax,
            prd_taxgroup=item.prd_taxgroup,
            prd_disvalue=item.prd_disvalue,
            prd_distype=item.prd_distype,
            quantity=item.quantity,
            price=item.price,
        )
    # Mark source quotation as invoiced after successful conversion.updt on 23-2-26 by neha
    if quotation.status != 'Invoiced':
        quotation.status = 'Invoiced'
        quotation.save(update_fields=['status'])
    # (Optional) Update quotation status or notify user here
    # Create Journal Entry for this invoice similar to `save_salesinvoice`
    try:
        taxable_total = Decimal(0)
        tax_totals = {}
        company = _get_company_for_tax()
        company_db = getattr(request, 'company_db', None) or request.session.get('company_db') or 'default'
        inv_items = SalesInvoiceItem.objects.filter(sales_inv=sales_inv)
        for item in inv_items:
            tax_override = None
            if item.prd_taxgroup:
                tax_override = TaxGroup.objects.filter(group_name=item.prd_taxgroup).prefetch_related('taxes').first()
            tax_result = _calculate_tax_for_invoice_item(
                item,
                sales_inv.customer,
                company,
                tax_override=tax_override
            )
            if not item.prd_taxgroup and tax_result.get('total_rate'):
                item.prd_tax = tax_result['total_rate']
                item.prd_taxgroup = tax_result.get('tax_name') or item.prd_taxgroup
            # Only save fields that actually exist in the model
            item.save(update_fields=['prd_tax', 'prd_taxgroup'])

            taxable_total += tax_result.get('base_amount', Decimal('0.00'))
            for comp in tax_result.get('breakdown', []):
                InvoiceTax.objects.create(
                    invoice=sales_inv,
                    tax_name=comp.get('name') or '',
                    tax_rate=comp.get('rate') or Decimal('0.00'),
                    tax_amount=comp.get('amount') or Decimal('0.00'),
                )
                comp_key = (comp.get('name') or '').upper()
                if comp_key:
                    tax_totals[comp_key] = tax_totals.get(comp_key, Decimal(0)) + (comp.get('amount') or Decimal('0.00'))

        # generate next JV number
        last = JournalEntry.objects.order_by('-id').first()
        if last and last.entry_number and last.entry_number.startswith('JV-'):
            try:
                last_num = int(last.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        next_num = last_num + 1
        entry_number = f"JV-{str(next_num).zfill(5)}"

        journal = JournalEntry.objects.create(
            entry_number=entry_number,
            date=sales_inv.date,
            reference=sales_inv.inv_number,
            narration=f"Sales Invoice {sales_inv.inv_number}",
            created_by=request.user,
            updated_by=request.user,
            status='posted'
        )

        # Debit: Debtors (Accounts Receivable)
        debt_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Debtors').first()
        if not debt_acct:
            debt_acct = ChartOfAccounts.objects.using(company_db).filter(code='1020101').first()
        # Adjust receivable for TDS/TCS so the JV balances correctly.
        tds_base = _scaled_tds_tcs_amount(sales_inv)
        debit_amt = Decimal(sales_inv.total_amount_base or 0)
        if str(getattr(sales_inv, 'tds_tcs_type', '') or '').strip().lower() == 'tds':
            debit_amt = (debit_amt - tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif str(getattr(sales_inv, 'tds_tcs_type', '') or '').strip().lower() == 'tcs':
            debit_amt = (debit_amt + tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        JournalLine.objects.create(
            journal=journal,
            account=debt_acct,
            description=f"Invoice {sales_inv.inv_number} - Receivable",
            debit=debit_amt,
            credit=Decimal(0),
            sequence=10
        )
        logger.info(
            "Posting order->inv sales JV %s: inv=%s total_amount_base=%s tds_base=%s debit_amt=%s",
            entry_number,
            getattr(sales_inv, 'inv_number', None),
            getattr(sales_inv, 'total_amount_base', None),
            tds_base,
            debit_amt,
        )
        logger.info(
            "Posting sales JV %s: inv=%s total_amount_base=%s tds_base=%s debit_amt=%s",
            entry_number,
            getattr(sales_inv, 'inv_number', None),
            getattr(sales_inv, 'total_amount_base', None),
            tds_base,
            debit_amt,
        )

        # byadarshDebit: Individual Customer Account
        # if sales_inv.customer:
        #     customer_name = f"{sales_inv.customer.first_name} {sales_inv.customer.last_name}".strip()
        #     customer_acct = ChartOfAccounts.objects.filter(name__iexact=customer_name).first()
        #     
        #     # If customer account doesn't exist, create it
        #     if not customer_acct:
        #         # Find the parent Debtors account
        #         debtors_parent = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
        #         if not debtors_parent:
        #             debtors_parent = ChartOfAccounts.objects.filter(code='1020101').first()
        #         
        #         # Create customer account as a sub-account of Debtors
        #         if debtors_parent:
        #             customer_acct = ChartOfAccounts.objects.create(
        #                 code=f"CUST-{sales_inv.customer.id}",
        #                 name=customer_name,
        #                 type=debtors_parent.type,
        #                 parent=debtors_parent,
        #                 description=f"Customer Account - {sales_inv.customer.email or ''}",
        #                 is_header=False,
        #                 active=True,
        #                 created_by=request.user,
        #                 updated_by=request.user,
        #                 status=True
        #             )
        #     
        #     # Create journal line for customer account
        #     if customer_acct:
        #         JournalLine.objects.create(
        #             journal=journal,
        #             account=customer_acct,
        #             description=f"Invoice {sales_inv.inv_number} - {customer_name}",
        #             debit=Decimal(sales_inv.total_amount or 0),
        #             credit=Decimal(0),
        #             sequence=15
        #         )

        # Credit: Sales
        sales_acct = None
        if inv_items:
            prod_sales_acc = getattr(inv_items[0].product, 'sales_account', None)
            if prod_sales_acc:
                sales_acct = ChartOfAccounts.objects.using(company_db).filter(code=str(prod_sales_acc)).first() or ChartOfAccounts.objects.using(company_db).filter(name__iexact=str(prod_sales_acc)).first()
        if not sales_acct:
            sales_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Sales').first()
        taxable_total = taxable_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        JournalLine.objects.create(
            journal=journal,
            account=sales_acct,
            description=f"Invoice {sales_inv.inv_number} - Sales",
            debit=Decimal(0),
            credit=scale_amount_for_journal(taxable_total, sales_inv),
            sequence=20
        )

        # Credit: Output Tax accounts
        tax_account_map = {
            'CGST': 'Output Tax CGST',
            'SGST': 'Output Tax SGST',
            'IGST': 'Output Tax IGST',
        }
        seq = 30
        for ttype, amount in tax_totals.items():
            amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amount == Decimal('0.00'):
                continue
            
            # Check if this is a compound tax (e.g., GST5 should be split into CGST + SGST)
            # If ttype is not a standard key, try to split it
            if ttype not in tax_account_map:
                # Try to split compound taxes like GST5 into CGST and SGST (50-50)
                ttype_lower = ttype.lower()
                if 'gst' in ttype_lower or 'tax' in ttype_lower:
                    # Split into CGST and SGST
                    cgst_amount = (amount / Decimal('2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    sgst_amount = (amount - cgst_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    # Create CGST entry
                    cgst_acct = ChartOfAccounts.objects.using(company_db).filter(name__exact='Output Tax CGST').first()
                    if not cgst_acct:
                        cgst_acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact='Output Tax CGST').first()
                    if cgst_acct and cgst_amount > 0:
                        JournalLine.objects.create(
                            journal=journal,
                            account=cgst_acct,
                            description=f"Invoice {sales_inv.inv_number} - CGST",
                            debit=Decimal(0),
                            credit=scale_amount_for_journal(cgst_amount, sales_inv),
                            sequence=seq
                        )
                        seq += 10
                    
                    # Create SGST entry
                    sgst_acct = ChartOfAccounts.objects.using(company_db).filter(name__exact='Output Tax SGST').first()
                    if not sgst_acct:
                        sgst_acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact='Output Tax SGST').first()
                    if sgst_acct and sgst_amount > 0:
                        JournalLine.objects.create(
                            journal=journal,
                            account=sgst_acct,
                            description=f"Invoice {sales_inv.inv_number} - SGST",
                            debit=Decimal(0),
                            credit=scale_amount_for_journal(sgst_amount, sales_inv),
                            sequence=seq
                        )
                        seq += 10
                    continue
            
            # Handle standard tax types (CGST, SGST, IGST)
            acct_name = tax_account_map.get(ttype, None)
            acct = None
            if acct_name:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__exact=acct_name).first()
            if not acct and acct_name:
                acct = ChartOfAccounts.objects.using(company_db).filter(name__iexact=acct_name).first()
            if not acct:
                acct = ChartOfAccounts.objects.using(company_db).exclude(name__icontains='Refund').filter(name__icontains=f'Output Tax {ttype}').first()
            if acct:
                JournalLine.objects.create(
                    journal=journal,
                    account=acct,
                    description=f"Invoice {sales_inv.inv_number} - {ttype}",
                    debit=Decimal(0),
                    credit=scale_amount_for_journal(amount, sales_inv),
                    sequence=seq
                )
                seq += 10

        seq = _post_tds_tcs_journal_line(journal, sales_inv, company_db, seq, posting_role='sales')
        journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
        journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())

        diff = (Decimal(journal.total_debit) - Decimal(journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if diff != Decimal('0.00'):
            round_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Rounded Off').first()
            if not round_acct:
                round_acct = ChartOfAccounts.objects.using(company_db).filter(code='40217').first()
            if round_acct:
                if diff > 0:
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Invoice {sales_inv.inv_number} - Rounded Off",
                        debit=Decimal(0),
                        credit=diff,
                        sequence=seq
                    )
                else:
                    amt = abs(diff)
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Invoice {sales_inv.inv_number} - Rounded Off",
                        debit=amt,
                        credit=Decimal(0),
                        sequence=seq
                    )
                journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
                journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        journal.save()
         
        # Create COGS Transfer Entry (moved the cost from Stock to Expense)
        # This ensures P&L shows true profit (Revenue - COGS - Expenses)
        try:
            cogs_journal = create_cogs_transfer(sales_inv, user=request.user)
            if cogs_journal:
                logger.info(f"COGS transfer created for invoice {sales_inv.inv_number}: {cogs_journal.entry_number}")
            else:
                logger.warning(f"COGS transfer not created for invoice {sales_inv.inv_number}")
        except Exception as e:
            logger.exception(f"Failed to create COGS transfer for invoice {sales_inv.inv_number}: {str(e)}")
    except Exception:
        try:
            logger.exception('Failed to create journal entry for invoice %s', sales_inv.pk)
        except Exception:
            pass
    #change
    # Mark the source quotation as invoiced and save
    try:
        quotation.status = 'Invoiced'
        quotation.save()
    except Exception:
        logger.exception('Failed to mark quotation %s as Invoiced', quotation_id)

    # Redirect to sales order detail or list (replace 'sales_order_detail' accordingly)
    return redirect_with_company('sales_inv_list')#change

@transaction.atomic
def convert_order_to_inv(request, order_id):
    order = get_object_or_404(SalesOrder, pk=order_id)

    # Status rule: order must be Accepted before converting to invoice.
    if order.status != 'Accepted':
        messages.error(request, 'Only Accepted orders can be converted to Invoice.')
        return redirect_with_company('order_detail', pk=order.pk)
    credit_limit_error = build_customer_credit_limit_message(order.customer, order.total_amount)
    if credit_limit_error:
        return credit_limit_block_response(request, credit_limit_error, 'order_detail', pk=order.pk)
    # Generate a unique invoice number (include fiscal year based on conversion date)
    inv_number = generate_inv_number(timezone.now().date())

    # Create SalesOrder from SalesQuotation
    sales_inv = SalesInvoice.objects.create(
        customer=order.customer,
        inv_number=inv_number,
        date=timezone.now(),
        sales_person=order.sales_person,
        notes=order.notes,
        payment_term=order.payment_term,
        discount_value=order.discount_value,
        discount_type=order.discount_type,
        status='Open',
        total_amount=order.total_amount,
        document_currency=order.document_currency,
        fx_rate_to_base=order.fx_rate_to_base or Decimal('1.000000'),
        fx_rate_date=order.fx_rate_date,
        total_amount_base=order.total_amount_base,
        place_of_supply=order.place_of_supply,
        shipping_attention=order.shipping_attention,
        shipping_email=order.shipping_email,
        shipping_phone=order.shipping_phone,
        shipping_country=order.shipping_country,
        shipping_address1=order.shipping_address1,
        shipping_address2=order.shipping_address2,
        shipping_city=order.shipping_city,
        shipping_state=order.shipping_state,
        shipping_postal_code=order.shipping_postal_code,
        tds_tcs_type=order.tds_tcs_type,
        tds_tcs_definition_id=order.tds_tcs_definition_id,
        tds_tcs_rate=order.tds_tcs_rate,
        tds_tcs_amount=order.tds_tcs_amount,
    )

    # Copy SalesQuotationItems to SalesOrderItems
    order_items = SalesOrderItem.objects.filter(sales_order=order)
    for item in order_items:
        SalesInvoiceItem.objects.create(
            sales_inv=sales_inv,
            product=item.product,
            prd_brcd=item.prd_brcd,
            hsn_code=item.hsn_code,
            prd_tax=item.prd_tax,
            prd_taxgroup=item.prd_taxgroup,
            prd_disvalue=item.prd_disvalue,
            prd_distype=item.prd_distype,
            quantity=item.quantity,
            price=item.price,
        )
    # Mark source order as invoiced after successful conversion.
    if order.status != 'Invoiced':
        order.status = 'Invoiced'
        order.save(update_fields=['status'])

    # If this order came from a quotation, mark that quotation as Invoiced.
    # if order.origin_quote and order.origin_quote.status != 'Invoiced':
    if hasattr(order, 'origin_quote') and order.origin_quote and order.origin_quote.status != 'Invoiced':  #edited by sisira 28/2
        order.origin_quote.status = 'Invoiced'
        order.origin_quote.save(update_fields=['status'])
    # (Optional) Update quotation status or notify user here
    # Create Journal Entry for this invoice similar to `save_salesinvoice`
    try:
        taxable_total = Decimal(0)
        tax_totals = {}
        company = _get_company_for_tax()
        InvoiceTax.objects.filter(invoice=sales_inv).delete()
        inv_items = SalesInvoiceItem.objects.filter(sales_inv=sales_inv)
        for item in inv_items:
            tax_override = None
            if item.prd_taxgroup:
                tax_override = TaxGroup.objects.filter(group_name=item.prd_taxgroup).prefetch_related('taxes').first()
            tax_result = _calculate_tax_for_invoice_item(
                item,
                sales_inv.customer,
                company,
                tax_override=tax_override
            )
            if not item.prd_taxgroup and tax_result.get('total_rate'):
                item.prd_tax = tax_result['total_rate']
                item.prd_taxgroup = tax_result.get('tax_name') or item.prd_taxgroup
            # Only save fields that actually exist in the model
            item.save(update_fields=['prd_tax', 'prd_taxgroup'])
            taxable_total += tax_result.get('base_amount', Decimal('0.00'))
            for comp in tax_result.get('breakdown', []):
                InvoiceTax.objects.create(
                    invoice=sales_inv,
                    tax_name=comp.get('name') or '',
                    tax_rate=comp.get('rate') or Decimal('0.00'),
                    tax_amount=comp.get('amount') or Decimal('0.00'),
                )
                comp_key = (comp.get('name') or '').upper()
                if comp_key:
                    tax_totals[comp_key] = tax_totals.get(comp_key, Decimal(0)) + (comp.get('amount') or Decimal('0.00'))


        # generate next JV number
        last = JournalEntry.objects.order_by('-id').first()
        if last and last.entry_number and last.entry_number.startswith('JV-'):
            try:
                last_num = int(last.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        next_num = last_num + 1
        entry_number = f"JV-{str(next_num).zfill(5)}"

        journal = JournalEntry.objects.create(
            entry_number=entry_number,
            date=sales_inv.date,
            reference=sales_inf.inv_number if False else sales_inv.inv_number,
            narration=f"Sales Invoice {sales_inv.inv_number}",
            created_by=request.user,
            updated_by=request.user,
            status='posted'
        )

        # Debit: Debtors (Accounts Receivable)
        debt_acct = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
        if not debt_acct:
            debt_acct = ChartOfAccounts.objects.filter(code='1020101').first()
        tds_base = _scaled_tds_tcs_amount(sales_inv)
        debit_amt = Decimal(sales_inv.total_amount_base or 0)
        if str(getattr(sales_inv, 'tds_tcs_type', '') or '').strip().lower() == 'tds':
            debit_amt = (debit_amt - tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        elif str(getattr(sales_inv, 'tds_tcs_type', '') or '').strip().lower() == 'tcs':
            debit_amt = (debit_amt + tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        JournalLine.objects.create(
            journal=journal,
            account=debt_acct,
            description=f"Invoice {sales_inv.inv_number} - Receivable",
            debit=debit_amt,
            credit=Decimal(0),
            sequence=10
        )

        # byadarshDebit: Individual Customer Account
        # if sales_inv.customer:
        #     customer_name = f"{sales_inv.customer.first_name} {sales_inv.customer.last_name}".strip()
        #     customer_acct = ChartOfAccounts.objects.filter(name__iexact=customer_name).first()
        #     
        #     # If customer account doesn't exist, create it
        #     if not customer_acct:
        #         # Find the parent Debtors account
        #         debtors_parent = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
        #         if not debtors_parent:
        #             debtors_parent = ChartOfAccounts.objects.filter(code='1020101').first()
        #         
        #         # Create customer account as a sub-account of Debtors
        #         if debtors_parent:
        #             customer_acct = ChartOfAccounts.objects.create(
        #                 code=f"CUST-{sales_inv.customer.id}",
        #                 name=customer_name,
        #                 type=debtors_parent.type,
        #                 parent=debtors_parent,
        #                 description=f"Customer Account - {sales_inv.customer.email or ''}",
        #                 is_header=False,
        #                 active=True,
        #                 created_by=request.user,
        #                 updated_by=request.user,
        #                 status=True
        #             )
        #     
        #     # Create journal line for customer account
        #     if customer_acct:
        #         JournalLine.objects.create(
        #             journal=journal,
        #             account=customer_acct,
        #             description=f"Invoice {sales_inv.inv_number} - {customer_name}",
        #             debit=Decimal(sales_inv.total_amount or 0),
        #             credit=Decimal(0),
        #             sequence=15
        #         )

        # Credit: Sales
        sales_acct = None
        if inv_items:
            prod_sales_acc = getattr(inv_items[0].product, 'sales_account', None)
            if prod_sales_acc:
                sales_acct = ChartOfAccounts.objects.filter(code=str(prod_sales_acc)).first() or ChartOfAccounts.objects.filter(name__iexact=str(prod_sales_acc)).first()
        if not sales_acct:
            sales_acct = ChartOfAccounts.objects.filter(name__icontains='Sales').first()
        taxable_total = taxable_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        JournalLine.objects.create(
            journal=journal,
            account=sales_acct,
            description=f"Invoice {sales_inv.inv_number} - Sales",
            debit=Decimal(0),
            credit=scale_amount_for_journal(taxable_total, sales_inv),
            sequence=20
        )

        # Credit: Output Tax accounts
        tax_account_map = {
            'CGST': 'Output Tax CGST',
            'SGST': 'Output Tax SGST',
            'IGST': 'Output Tax IGST',
        }
        seq = 30
        for ttype, amount in tax_totals.items():
            ttype = normalize_tax_code(ttype)
            amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amount == Decimal('0.00'):
                continue
            acct = resolve_tax_account(ttype, direction='output', using=company_db)
            JournalLine.objects.create(
                journal=journal,
                account=acct,
                description=f"Invoice {sales_inv.inv_number} - {ttype}",
                debit=Decimal(0),
                credit=scale_amount_for_journal(amount, sales_inv),
                sequence=seq
            )
            # acct_name = tax_account_map.get(ttype, None)
            # acct = None
            # if acct_name:
            #     acct = ChartOfAccounts.objects.filter(name__exact=acct_name).first()
            # if not acct and acct_name:
            #     acct = ChartOfAccounts.objects.filter(name__iexact=acct_name).first()
            # if not acct:
            #     acct = ChartOfAccounts.objects.exclude(name__icontains='Refund').filter(name__icontains=f'Output Tax {ttype}').first()
            # JournalLine.objects.create(
            #     journal=journal,
            #     account=acct,
            #     description=f"Invoice {sales_inv.inv_number} - {ttype}",
            #     debit=Decimal(0),
            #     credit=amount,
            #     sequence=seq
            # )
            seq += 10

        seq = _post_tds_tcs_journal_line(journal, sales_inv, db, seq, posting_role='sales')
        journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
        journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())

        diff = (Decimal(journal.total_debit) - Decimal(journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if diff != Decimal('0.00'):
            round_acct = ChartOfAccounts.objects.filter(name__icontains='Rounded Off').first()
            if not round_acct:
                round_acct = ChartOfAccounts.objects.filter(code='40217').first()
            if round_acct:
                if diff > 0:
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Invoice {sales_inv.inv_number} - Rounded Off",
                        debit=Decimal(0),
                        credit=diff,
                        sequence=seq
                    )
                else:
                    amt = abs(diff)
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Invoice {sales_inv.inv_number} - Rounded Off",
                        debit=amt,
                        credit=Decimal(0),
                        sequence=seq
                    )
                journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
                journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        journal.save()

        # Create COGS Transfer Entry (moved the cost from Stock to Expense)
        # This ensures P&L shows true profit (Revenue - COGS - Expenses)
        try:
            cogs_journal = create_cogs_transfer(sales_inv, user=request.user)
            if cogs_journal:
                logger.info(f"COGS transfer created for invoice {sales_inv.inv_number}: {cogs_journal.entry_number}")
            else:
                logger.warning(f"COGS transfer not created for invoice {sales_inv.inv_number}")
        except Exception as e:
            logger.exception(f"Failed to create COGS transfer for invoice {sales_inv.inv_number}: {str(e)}")
    except Exception:
        try:
            logger.exception('Failed to create journal entry for invoice %s', sales_inv.pk)
        except Exception:
            pass
    #changes
    # Mark the source order as invoiced and save commented by neha on 23-2-26 as order is already marked invoiced at line 316 
    # try:
    #     order.status = 'Invoiced'
    #     order.save()
    # except Exception:
    #     logger.exception('Failed to mark order %s as Invoiced', order_id)

    # Redirect to invoice list
    return redirect_with_company('sales_inv_list')#changes
    
def check_quotation_invoiced(request, quotation_id):
    """AJAX endpoint: return whether a quotation already has an invoice linked.

    Response: { exists: true|false, invoice_id: <id>, inv_number: <inv_number> }
    """
    quotation = get_object_or_404(SalesQuotation, pk=quotation_id)
    inv = SalesInvoice.objects.filter(origin_quote=quotation).first()
    if inv:
        return JsonResponse({'exists': True, 'invoice_id': inv.pk, 'inv_number': inv.inv_number})
    return JsonResponse({'exists': False})


def performa_inv_list(request):
    # Permission: require View on Sales Performa Invoice
    if not (getattr(request.user, 'is_superuser', False) or can_view_performa_invoice(request.user)):
        messages.error(request, 'You do not have permission to view Sales Performa Invoice.')
        return redirect_with_company('sales_index')
    
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    performa_qs = PerformaInvoice.objects.all().order_by('-id')
    if search_query:
        performa_qs = performa_qs.filter(
            Q(sales_person__name__icontains=search_query) |
            Q(customer__first_name__icontains=search_query) |
            Q(customer__company_name__icontains=search_query) |
            Q(inv_number__icontains=search_query) |
            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )

    if status_filter:
        performa_qs = performa_qs.filter(status=status_filter)

    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_docs = defaultdict(list)
    for doc in performa_qs:
        match = pattern.match(doc.inv_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_docs[base_number].append((revision_num, doc))

    latest_docs = []
    revisions_dict = {}
    for base_number, rev_list in grouped_docs.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_docs.append(latest)

    paginator = Paginator(latest_docs, page_size)
    page_number = request.GET.get('page')
    performa_inv = paginator.get_page(page_number)

    context = {
        'performa_inv': performa_inv,
        'sales_inv': performa_inv,
        'search_query': search_query,
        'status_filter': status_filter,
        'total_count': len(latest_docs),
        'page_size': page_size,
        'revisions_dict': revisions_dict,
    }
    return render(request, 'sales/performa_inv_list.html', context)



def sales_inv_list(request):
    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    payment_status_filter = request.GET.get('payment_status', '').strip()
    page_size = int(request.GET.get('page_size', 10))

    sales_inv_qs = SalesInvoice.objects.all().order_by('-id')

    if search_query:
        sales_inv_qs = sales_inv_qs.filter(
            Q(sales_person__name__icontains=search_query) |
            Q(customer__first_name__icontains=search_query) |
            Q(inv_number__icontains=search_query) |
            Q(status__icontains=search_query) |
            Q(total_amount__icontains=search_query)
        )
            
    if status_filter:
        sales_inv_qs = sales_inv_qs.filter(status=status_filter)
    
    if payment_status_filter == 'outstanding':
        # Home dashboard "Open Invoices": everything not fully paid, drafts excluded
        sales_inv_qs = sales_inv_qs.exclude(status='Draft').exclude(payment_status__name__iexact='Paid')
    elif payment_status_filter:
        sales_inv_qs = sales_inv_qs.filter(payment_status__name=payment_status_filter)
        

    # Grouping logic
    # Assume quotation_number like "QT123-R1", "QT123-R2", "QT456"
    pattern = re.compile(r'^(?P<base>.+?)(?:-R(?P<rev>\d+))?$')
    grouped_invs = defaultdict(list)

    for inv in sales_inv_qs:
        match = pattern.match(inv.inv_number)
        if match:
            base_number = match.group('base')
            revision_num = int(match.group('rev') or 0)
            grouped_invs[base_number].append((revision_num, inv))

    # Select latest revision per base_number
    latest_invs = []
    revisions_dict = {}

    for base_number, rev_list in grouped_invs.items():
        rev_list.sort(key=lambda x: x[0], reverse=True)
        latest = rev_list[0][1]
        older_revisions = [r[1] for r in rev_list[1:]]
        setattr(latest, 'older_revisions', older_revisions)
        latest_invs.append(latest)

    # Compute return status flag for each invoice (fully returned?)
    def invoice_is_fully_returned(inv_obj):
        try:
            items_qs = SalesInvoiceItem.objects.filter(sales_inv=inv_obj)
            # If there are no items on the invoice, it should NOT be considered "fully returned".
            if not items_qs.exists():
                setattr(inv_obj, 'is_fully_returned', False)
                return

            for itm in items_qs:
                returned = SalesReturnItem.objects.filter(invoice_item=itm, sales_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
                if int(returned) < int(itm.quantity):
                    setattr(inv_obj, 'is_fully_returned', False)
                    break
            else:
                setattr(inv_obj, 'is_fully_returned', True)
        except Exception:
            setattr(inv_obj, 'is_fully_returned', False)

    for inv in latest_invs:
        invoice_is_fully_returned(inv)

    # Paginate latest quotations
    paginator = Paginator(latest_invs, page_size)
    page_number = request.GET.get('page')
    sales_inv = paginator.get_page(page_number)

    total_count = len(latest_invs)

    context = {
        'sales_inv': sales_inv,
        'search_query': search_query,
        'status_filter': status_filter,
        'payment_status_filter': payment_status_filter,
        'total_count': total_count,
        'page_size': page_size,
        'revisions_dict': revisions_dict,  # Pass older revisions mapped by latest quote id
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return render(request, 'sales/sales_inv_list.html', context)

    return render(request, 'sales/sales_inv_list.html', context)


def invoice_detail(request, pk):
    """Render a readonly detail page for a sales invoice."""
    inv = get_object_or_404(SalesInvoice, pk=pk)
    company = _get_company_for_request(request)
    fx_rate = Decimal(inv.fx_rate_to_base or Decimal('1'))
    tds_tcs_type = (inv.tds_tcs_type or '').strip().lower()
    print("tds_tcs_type:", tds_tcs_type)
    tds_tcs_amount = Decimal(str(inv.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate
    # Use a simple formset to iterate items if needed in template
    SalesInvoiceItemFormSet = modelformset_factory(SalesInvoiceItem, form=SalesInvoiceItemForm, extra=0)
    existing_items_qs = SalesInvoiceItem.objects.filter(sales_inv=inv)
    sales_formset = SalesInvoiceItemFormSet(queryset=existing_items_qs)

    items_info = []
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_item_discount = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        base = qty * price

        discount_val = Decimal(item.prd_disvalue or 0)

        # — NEW: Apply discount per unit
        if item.prd_distype == 'percent':
            discount_amount = (base * discount_val) / Decimal('100')
        else:
            # Flat discount is total for the line
            discount_amount = discount_val
        
        if discount_amount > base:
            discount_amount = base

        # Calculate discounted amount
        discounted = base - discount_amount
        if discounted < Decimal('0.00'):
            discounted = Decimal('0.00')

        # Calculate discount amount for display
        discount_amount = base - discounted

        tax_rate = Decimal(item.prd_tax or 0)
        tax_amount = (discounted * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        # — NEW: line_total is ONLY the discounted amount (no tax)
        line_total = discounted

        # line_total = discounted + tax_amount

        # — NEW: Subtotal accumulates discounted amounts (before tax)
        subtotal_calc += discounted
        # subtotal_calc += line_total
        total_tax += tax_amount
        total_item_discount += discount_amount

         # Calculate returned quantity for this item
        returned_qty = SalesReturnItem.objects.filter(invoice_item=item, sales_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'o_price': getattr(item, 'o_price', None),
            'base': base,
            'discount_peritem':discount_val,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'line_total': line_total,
            'hsn': getattr(item, 'hsn_code', '') or '',
            'returned_qty': returned_qty,
           
        })

    # Split tax equally into CGST/SGST for display (simple assumption)
    total_cgst = total_tax / Decimal('2')
    total_sgst = total_tax / Decimal('2')

    # — NEW: Total before grand discount = subtotal + tax
    total_before_discount = subtotal_calc + total_tax

    # Grand discount handling - applies to (subtotal + tax)
    grand_discount_value = Decimal(inv.discount_value or 0)
    grand_discount_type = inv.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (total_before_discount  * grand_discount_value) / Decimal('100')

        # grand_discount = subtotal_calc * grand_discount_value / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > total_before_discount:
        grand_discount = total_before_discount
    # if grand_discount > subtotal_calc:
    #     grand_discount = subtotal_calc

    total_discount_combined = total_item_discount + grand_discount
    
    final_total = inv.total_amount
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        # pre-turnover total includes taxes (subtotal + tax) minus grand discount
        pre_turnover_total = total_before_discount - grand_discount
        stored_final_total = Decimal(inv.total_amount or pre_turnover_total)
        turnover_tax_amount = stored_final_total - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')
        final_total = stored_final_total

        # Base currency handling is done after base conversions below
    total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')
    remaining_amount = inv.total_amount - total_paid
    is_fully_paid = remaining_amount <= Decimal('0.00')
     
    # Check if invoice is fully returned
    def invoice_is_fully_returned(inv_obj):
        try:
            for itm in SalesInvoiceItem.objects.filter(sales_inv=inv_obj):
                returned = SalesReturnItem.objects.filter(invoice_item=itm, sales_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
                if int(returned) < int(itm.quantity):
                    setattr(inv_obj, 'is_fully_returned', False)
                    break
            else:
                setattr(inv_obj, 'is_fully_returned', True)
        except Exception:
            setattr(inv_obj, 'is_fully_returned', False)
    
    invoice_is_fully_returned(inv)
    is_fully_returned = getattr(inv, 'is_fully_returned', False)


    # Related activity for detail view sections
    payment_allocations = (
        InvPaymentAllocation.objects.filter(inv=inv)
        .select_related('payment', 'payment__payment_mode')
        .order_by('-payment__payment_date', '-payment__created_at')
    )
    sales_returns = (
        SalesReturn.objects.filter(sales_invoice=inv)
        .select_related('warehouse')
        .order_by('-date', '-created_at')
    )
    delivery_notes = (
        SalesDeliveryNote.objects.filter(sales_invoice=inv)
        .select_related('warehouse')
        .order_by('-delivery_date', '-created_at')
    )
    
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    # Currency handling: provide document currency and company base currency symbols
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    # Document / invoice currency
    doc_currency = inv.document_currency
    doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip() if doc_currency else ''
    doc_currency_code = doc_currency.code if doc_currency else ''
    show_base_currency_only = (
        not doc_currency_code
        or (
            bool(base_currency_code)
            and doc_currency_code.strip().upper() == base_currency_code.strip().upper()
        )
    )

    # FX rate from document currency to base (invoice.fx_rate_to_base)
    subtotal_calc_base = Decimal('0.00')
    total_tax_base = Decimal('0.00')
    total_item_discount_base = Decimal('0.00')
    for itm in items_info:
        price = Decimal(itm.get('price') or 0)
        qty = Decimal(itm.get('quantity') or 0)
        price_base = Decimal(itm.get('o_price') or 0) or (price * fx_rate)
        discount_value = Decimal(itm.get('discount_peritem') or 0)
        discount_type = itm.get('discount_type') or 'flat'
        line_base = price_base * qty
        if discount_type == 'percent':
            discount_amount_base = (line_base * discount_value) / Decimal('100')
        else:
            discount_amount_base = discount_value * fx_rate
        if discount_amount_base > line_base:
            discount_amount_base = line_base
        line_total_base = line_base - discount_amount_base
        tax_rate = Decimal(itm.get('tax_rate') or 0)
        tax_amount_base = (line_total_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')

        itm['price_base'] = price_base
        itm['line_total_base'] = line_total_base
        itm['discount_amount_base'] = discount_amount_base
        itm['tax_amount_base'] = tax_amount_base

        subtotal_calc_base += line_total_base
        total_tax_base += tax_amount_base
        total_item_discount_base += discount_amount_base

    total_cgst_base = total_cgst * fx_rate
    total_sgst_base = total_sgst * fx_rate
    final_total_base = inv.total_amount_base if getattr(inv, 'total_amount_base', None) is not None else ((final_total * fx_rate) if final_total is not None else Decimal('0.00'))
    grand_discount_base = grand_discount * fx_rate
    total_discount_combined_base = total_item_discount_base + grand_discount_base
    total_paid_base = (total_paid * fx_rate) if total_paid is not None else Decimal('0.00')
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = total_before_discount - grand_discount
        turnover_tax_amount = Decimal(inv.total_amount or 0) - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')

        # Determine stored final total in base currency.
        # Prefer explicit stored value only if it appears to include turnover tax (i.e. greater than pre-turnover base).
        stored_final_total_base_raw = getattr(inv, 'total_amount_base', None)
        pre_turnover_total_base_est = (pre_turnover_total * fx_rate)
        if stored_final_total_base_raw is not None:
            try:
                stored_final_total_base = Decimal(stored_final_total_base_raw)
            except Exception:
                stored_final_total_base = Decimal('0.00')
            # If stored base total doesn't exceed pre-turnover base, derive from document total instead
            if stored_final_total_base <= pre_turnover_total_base_est:
                stored_final_total_base = Decimal(inv.total_amount or final_total or 0) * fx_rate
        else:
            stored_final_total_base = Decimal(inv.total_amount or final_total or 0) * fx_rate

        turnover_tax_amount_base = stored_final_total_base - pre_turnover_total_base_est
        if turnover_tax_amount_base < Decimal('0.00'):
            turnover_tax_amount_base = Decimal('0.00')
        # ensure final_total_base reflects the stored/derived base total
        final_total_base = stored_final_total_base

    # Add base-converted amounts to payment allocations and sales returns for template
    for alloc in payment_allocations:
        try:
            alloc.amount_base = (Decimal(alloc.amount or 0) * fx_rate)
        except Exception:
            alloc.amount_base = Decimal('0.00')

    for r in sales_returns:
        try:
            r.refund_amount_base = (Decimal(getattr(r, 'refund_amount', 0)) * fx_rate)
        except Exception:
            r.refund_amount_base = Decimal('0.00')

    context = {
        'invoice': inv,
        'total_paid': total_paid,
        'total_paid_base': total_paid_base,
        'q_no': inv.inv_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax': total_tax,
        'total_tax_base': total_tax_base,
        'total_cgst': total_cgst,
        'total_cgst_base': total_cgst_base,
        'total_sgst': total_sgst,
        'total_sgst_base': total_sgst_base,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'total_discount_combined': total_discount_combined,
        'total_discount_combined_base': total_discount_combined_base,
        'final_total': final_total,
        'final_total_base': final_total_base,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'is_fully_paid': is_fully_paid,
        'is_fully_returned': is_fully_returned,
        'payment_allocations': payment_allocations,
        'payment_allocations_count': payment_allocations.count(),
        'sales_returns': sales_returns,
        'sales_returns_count': sales_returns.count(),
        'delivery_notes': delivery_notes,
        'delivery_notes_count': delivery_notes.count(),
        'company_country': company_country,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'show_base_currency_only': show_base_currency_only,
        'fx_rate': fx_rate,
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }

    return render(request, 'sales/invoice_detail.html', context)


def invoice_print_view(request, pk):
    """Render a printable HTML page for the invoice (for printing in browser)."""
    context = build_invoice_context(pk, request)
    return render(request, 'sales/invoice_print.html', context)


def invoice_pdf_view(request, pk):
    """Generate a PDF for the invoice using ReportLab with proper rupee symbol support."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    
    inv = get_object_or_404(SalesInvoice, pk=pk)
    context = build_invoice_context(pk, request)
    company = Company.objects.filter(status=True).first() or Company.objects.first()
    
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
                          title=f'Invoice {pk}',
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
    
    invoice_badge_style = ParagraphStyle(
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
    #added by neha on 20-1-26
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
        [header_left_cell, Paragraph("INVOICE", invoice_badge_style)],
    ]
    header_table = Table(header_data, colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5*mm))
    
    # edited by neha on 20-1-26
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
    sales_person_obj = getattr(inv, 'sales_person', None)
    sales_person_name = (getattr(sales_person_obj, 'name', None) or '').strip()
    sales_person_phone = (getattr(sales_person_obj, 'phone', None) or '').strip()
    if sales_person_name and sales_person_phone:
        sales_person_display = f"{sales_person_name} ({sales_person_phone})"
    elif sales_person_name:
        sales_person_display = sales_person_name
    else:
        sales_person_display = '-'
    meta_data = [
        [Paragraph("<b>inv No</b>", meta_label_style), Paragraph(str(context.get('q_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(inv.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(inv.place_of_supply or '-'), meta_value_style),
         Paragraph("<b>Sales Person</b>", meta_label_style), Paragraph(str(sales_person_display), meta_value_style)],
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
    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if inv.customer:
        bill_lines = []
        customer_type = (getattr(inv.customer, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(inv.customer, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(inv.customer, 'first_name', '') or '').strip()
            last_name = (getattr(inv.customer, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name

        if display_name:
            bill_lines.append(display_name)

        # Use shipping address for billing if it exists, otherwise use customer address
        if inv.shipping_address1 or inv.shipping_city:
            if getattr(inv, 'shipping_address1', None):
                bill_lines.append(inv.shipping_address1)
            if getattr(inv, 'shipping_address2', None):
                bill_lines.append(inv.shipping_address2)
            if getattr(inv, 'shipping_city', None):
                bill_lines.append(inv.shipping_city)
            if getattr(inv, 'shipping_postal_code', None):
                bill_lines.append(inv.shipping_postal_code)
            if getattr(inv, 'shipping_state', None):
                bill_lines.append(inv.shipping_state)
            if getattr(inv, 'shipping_country', None):
                bill_lines.append(str(inv.shipping_country))
        else:
            if getattr(inv.customer, 'address_line_1', None):
                bill_lines.append(inv.customer.address_line_1)
            if getattr(inv.customer, 'state', None):
                bill_lines.append(inv.customer.state)
            if getattr(inv.customer, 'country', None):
                bill_lines.append(str(inv.customer.country))

        gst = getattr(inv.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")

        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>"
            bill_to += "<br/>".join(bill_lines)
            bill_to += "</font>"
    else:
        bill_to += "-"
    
    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if inv.shipping_address1 or inv.shipping_city:
        ship_lines = []

        if getattr(inv, 'shipping_attention', None):
            ship_lines.append(inv.shipping_attention)

        if getattr(inv, 'shipping_address1', None):
            ship_lines.append(inv.shipping_address1)

        if getattr(inv, 'shipping_address2', None):
            ship_lines.append(inv.shipping_address2)

        if getattr(inv, 'shipping_city', None):
            ship_lines.append(inv.shipping_city)

        if getattr(inv, 'shipping_postal_code', None):
            ship_lines.append(inv.shipping_postal_code)

        if getattr(inv, 'shipping_state', None):
            ship_lines.append(inv.shipping_state)

        if getattr(inv, 'shipping_country', None):
            ship_lines.append(str(inv.shipping_country))

        if getattr(inv, 'shipping_email', None):
            ship_lines.append(inv.shipping_email)

        if getattr(inv, 'shipping_phone', None):
            ship_lines.append(inv.shipping_phone)

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
    # Fetch currency symbols from context
    currency = context.get('document_currency_symbol') or ("₹" if font_registered else "Rs.")
    base_currency = context.get('company_base_currency_symbol') or ("₹" if font_registered else "Rs.")
    show_base_currency_only = context.get('show_base_currency_only', False)
    if show_base_currency_only:
        currency = base_currency
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', 'GST')
    # Conditional table headers based on country
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
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
        display_price = Decimal(str(item.get('price_base' if show_base_currency_only else 'price', 0)))
        display_tax_amount = Decimal(str(item.get('tax_amount_base' if show_base_currency_only else 'tax_amount', 0)))
        
        base_price_str = ""
        tax_base_str = ""

        if company_tax_type == 'GST':
            # Split tax equally for India (CGST/SGST)
            cgst_rate = tax_rate / 2
            sgst_rate = tax_rate / 2
            cgst_amount = display_tax_amount / 2
            sgst_amount = display_tax_amount / 2
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{display_price:.2f}", amount_style),
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
                Paragraph(f"{currency}\u00A0{display_price:.2f}", amount_style),
                Paragraph(f"{tax_rate:.2f}%", amount_style),
               
            ])
        elif company_tax_type in ('TURNOVER', 'NONE'):
            items_data.append([
                Paragraph(str(idx), value_style),
                Paragraph(f"{item.get('product_name', '')}<br/><font size=6><i>{item.get('description', '')}</i></font>", value_style),
                Paragraph(item.get('hsn', '-'), value_style),
                Paragraph(str(item.get('quantity', '')), ParagraphStyle('Right', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)),
                Paragraph(f"{currency}\u00A0{display_price:.2f}", amount_style),
                
               
            ])
    
    items_table = Table(items_data, colWidths=[25, 135, 59, 30, 90, 90, 90])
    items_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#ffffff')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
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
    # line_total already excludes tax, so use the context subtotal directly.
    pdf_subtotal = Decimal(str(
        context.get('subtotal_calc_base' if show_base_currency_only else 'subtotal_calc', 0) or 0
    ))    
    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    # TOTALS - Professional styling with conditional VAT/CGST-SGST
       
    subtotal_base = context.get('subtotal_calc_base', 0)
    total_cgst_base = context.get('total_cgst_base', 0)
    total_sgst_base = context.get('total_sgst_base', 0)
    total_tax_base = context.get('total_tax_base', 0)
    final_total_base = context.get('final_total_base', 0)
    display_total_cgst = context.get('total_cgst_base' if show_base_currency_only else 'total_cgst', 0)
    display_total_sgst = context.get('total_sgst_base' if show_base_currency_only else 'total_sgst', 0)
    display_total_tax = context.get('total_tax_base' if show_base_currency_only else 'total_tax', 0)
    display_final_total = context.get('final_total_base' if show_base_currency_only else 'final_total', 0)

    sub_base_str = ""
    cgst_base_str = ""
    sgst_base_str = ""
    tax_base_str = ""
    final_base_str = ""

    totals_label_style = ParagraphStyle(
        'TotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT
    )
    
    if company_tax_type == 'GST':
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}{sub_base_str}", amount_style)],
            [Paragraph("CGST", totals_label_style), Paragraph(f"{currency}\u00A0{display_total_cgst:.2f}{cgst_base_str}", amount_style)],
            [Paragraph("SGST", totals_label_style), Paragraph(f"{currency}\u00A0{display_total_sgst:.2f}{sgst_base_str}", amount_style)],
        ]
    elif company_tax_type in ('VAT', 'SALES','TURNOVER'):
        totals_data = [
            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}\u00A0{float(pdf_subtotal):.2f}{sub_base_str}", amount_style)],
            [Paragraph("TAX", totals_label_style), Paragraph(f"{currency}\u00A0{display_total_tax:.2f}{tax_base_str}", amount_style)],
        ]
    elif company_tax_type == 'NONE':
        totals_data = [
            
        ]
    inv_obj = context.get('invoice')
    round_off_value = getattr(inv_obj, 'round_off', None) if inv_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}\u00A0{float(round_off_value):.2f}", amount_style)])

    company_tax_type = context.get('company_tax_type', '')
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}\u00A0{Decimal(str(turnover_amt)):.2f}", amount_style)])
    
    grand_total_style = ParagraphStyle(
        'GrandTotal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2c3e50'),
        fontName='DejaVuSans',
        alignment=TA_RIGHT
    )
    
    totals_data.append([Paragraph("<b>Grand Total</b>", ParagraphStyle('GrandTotalLabel', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#000000'))), 
                       Paragraph(f"<b>{currency}\u00A0{display_final_total:.2f}</b>{final_base_str}", grand_total_style)])
    
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
    inv_obj = context.get('invoice')
    notes_text = (getattr(inv_obj, 'notes', None) or '').strip() or '-'
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
    bottom_table = Table(bottom_data, colWidths=[330, 190])
    bottom_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
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
    
    inv = get_object_or_404(SalesInvoice, pk=pk)
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="invoice_{inv.inv_number}.pdf"'
    return response



def build_invoice_context(pk, request=None):
    inv = get_object_or_404(SalesInvoice, pk=pk)
    #added by neha on 20-1-26
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    
    # Currency and FX setup
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    doc_currency = inv.document_currency
    doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip() if doc_currency else ''
    doc_currency_code = doc_currency.code if doc_currency else ''
    show_base_currency_only = (
        not doc_currency_code
        or (
            bool(base_currency_code)
            and doc_currency_code.strip().upper() == base_currency_code.strip().upper()
        )
    )

    fx_rate = Decimal(inv.fx_rate_to_base or Decimal('1'))

    existing_items_qs = SalesInvoiceItem.objects.filter(sales_inv=inv)
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
            'price_base': price * fx_rate,
            'base': base,
            'discount_amount': discount_amount,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'tax_amount_base': tax_amount * fx_rate,
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'cgst_amount_base': cgst_amount * fx_rate,
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'sgst_amount_base': sgst_amount * fx_rate,
            'taxable_value': discounted,
            'line_total': line_total,
            'line_total_base': line_total * fx_rate,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')

    grand_discount_value = Decimal(inv.discount_value or 0)
    grand_discount_type = inv.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > subtotal_calc:
        grand_discount = subtotal_calc

    final_total = inv.total_amount

    # — Add company_is_india for conditional tax display
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = subtotal_calc - grand_discount
        turnover_tax_amount = Decimal(inv.total_amount or 0) - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')
        turnover_tax_amount_base = turnover_tax_amount * fx_rate

    # Get company country code from the company object
    company_country_code = ''
    if company and hasattr(company, 'country'):
        if hasattr(company.country, 'code'):
            company_country_code = company.country.code
        elif isinstance(company.country, str):
            company_country_code = company.country
    
    company_is_india = _is_indian_company_country(company_country_code)

    context = {
        'invoice': inv,
        'q_no': inv.inv_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'total_tax': total_tax,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'total_discount_combined': total_item_discount + grand_discount,
        'subtotal_calc_base': subtotal_calc * fx_rate,
        'total_tax_base': total_tax * fx_rate,
        'total_cgst_base': total_cgst * fx_rate,
        'total_sgst_base': total_sgst * fx_rate,
        'final_total_base': final_total * fx_rate if final_total is not None else Decimal('0.00'),
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'show_base_currency_only': show_base_currency_only,
        'fx_rate': fx_rate,
        #added by neha on 20-1-26
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }
    print("items_info",items_info)
    return context

@require_POST
def delete_sales_inv(request, inv_id):
    # Permission: require Delete on Invoices
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_invoices(request.user)):
            error_msg = 'You do not have permission to delete Sales Invoices.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect_with_company('sales_inv_list')
    except Exception:
        error_msg = 'You do not have permission to delete Sales Invoices.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, error_msg)
        return redirect_with_company('sales_inv_list')

    inv = get_object_or_404(SalesInvoice, pk=inv_id)
    
    # —by adarshlock CHECK PERIOD LOCK BEFORE DELETING INVOICE
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    try:
        PeriodLockEnforcer.check_can_edit(inv.date, request.user, db=db, transaction_type='invoice')
    except PermissionDenied as e:
        # Return the actual period lock error message with all details
        error_msg = str(e)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, error_msg)
        return redirect_with_company('sales_inv_list')
    # —by adarshlock
    
    # Delete ALL journal entries associated with this invoice
    # 1. Invoice entries (reference = invoice number)
    # 2. COGS entries (reference = "COGS-" + invoice number)  
    # 3. Any reversals of the above
    
    invoice_entries = JournalEntry.objects.filter(reference=inv.inv_number)
    cogs_entries = JournalEntry.objects.filter(reference=f"COGS-{inv.inv_number}")
    
    deleted_count = 0
    
    # Delete invoice journal entries
    for entry in invoice_entries:
        logger.info(f"Deleting invoice journal entry {entry.entry_number} for invoice {inv.inv_number}")
        entry.delete()
        deleted_count += 1
    
    # Delete COGS journal entries
    for entry in cogs_entries:
        logger.info(f"Deleted COGS journal entry {entry.entry_number} for invoice {inv.inv_number}")
        entry.delete()
        deleted_count += 1
    
    logger.info(f"Deleted {deleted_count} journal entries for invoice {inv.inv_number}")

    inv.delete()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': f"Sales Invoice '{inv.inv_number}' deleted successfully."})
    
    messages.success(request, f"Sales Invoice '{inv.inv_number}' deleted successfully.")
    return redirect_with_company('sales_inv_list')


def inv_add(request):
    # Permission: require Create on Invoices
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_invoices(request.user)):
            messages.error(request, 'You do not have permission to create Sales Invoices.')
            return redirect_with_company('sales_inv_list')
    except Exception:
        messages.error(request, 'You do not have permission to create Sales Invoices.')
        return redirect_with_company('sales_inv_list')

    # Create form instance for SalesInvoice
    try:
        db_alias = getattr(request, 'company_db', 'default')
        resolved_company = _get_company_for_request(request)
        auto_load = bool(getattr(resolved_company, 'auto_load_cash_customer', False)) if resolved_company else False
        cash_customer = None
        if auto_load:
            cash_customer = Customer.objects.using(db_alias).filter(customer_code__iexact='CASH').first()
        sp = _get_current_salesperson(request)
        initial = {'customer': cash_customer.pk if cash_customer else None}
        if sp:
            initial['sales_person'] = sp.pk
        invoice_form = SalesInvoiceForm(initial=initial)
    except Exception:
        sp = _get_current_salesperson(request)
        initial = {}
        if sp:
            initial['sales_person'] = sp.pk
        invoice_form = SalesInvoiceForm(initial=initial if initial else None)
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    # Create a formset for SalesQuotationItem if you plan multiple items
    SalesInvoiceItemFormSet = formset_factory(SalesInvoiceItemForm, extra=1)
    sales_formset = SalesInvoiceItemFormSet()
    all_items = Item.objects.all()
    company = _get_company_for_request(request)
    company_tax_type = _get_request_company_tax_type(request, company)
    from currencies.models import Currency
    company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    if not base_currency_symbol:
        base_currency_symbol = base_currency.code if base_currency and base_currency.code else '₹'
    base_currency_code = base_currency.code if base_currency else ''
    company_country = _get_current_company_country(request)
    
    # — Fetch TDS and TCS for invoice_add template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    
    # Pass both to the template
    return render(request, 'sales/invoice_add.html', {
        'invoice_form': invoice_form,
        'item_formset': item_formset,
        'sales_formset': sales_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': f"SQ-{SalesInvoice.objects.count() + 1:05d}",
        'company_country': company_country,
        'company_is_india': _is_indian_company_country(company_country),
        'company_tax_type': company_tax_type,
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'base_currency': base_currency,
        'tds_tax_master_items': tds_tax_master_items,
        'tcs_tax_master_items': tcs_tax_master_items,
        'show_base_transaction_summary': bool(getattr(company, 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    })



def save_salesinvoice(request):
    if request.method == "POST":
        # Permission: require Create on Invoices
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_create_invoices(request.user)):
                messages.error(request, 'You do not have permission to create Sales Invoices.')
                return redirect_with_company('sales_inv_list')
        except Exception:
            messages.error(request, 'You do not have permission to create Sales Invoices.')
            return redirect_with_company('sales_inv_list')
        post_data = request.POST.copy()  # make mutable copy
        prd_brcd_map = {}
        # — CHECK PERIOD LOCK BEFORE CREATING INVOICE
        from datetime import datetime
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        post_data = request.POST.copy()  # make mutable copy
        date_str = post_data.get('date')
        if date_str:
            try:
                invoice_date = datetime.strptime(date_str, '%Y-%m-%d').date() if isinstance(date_str, str) else date_str
            except:
                invoice_date = None
            
            if invoice_date:
                try:
                    PeriodLockEnforcer.check_can_edit(invoice_date, request.user, db=db, transaction_type='invoice')
                except PermissionDenied as e:
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return JsonResponse({'success': False, 'error': f"₹ Cannot create invoice: {str(e)}"}, status=403)
                    # Render form page with error modal instead of redirecting
                    invoice_form = SalesInvoiceForm()
                    SalesInvoiceItemFormSet = modelformset_factory(SalesInvoiceItem, form=SalesInvoiceItemForm, extra=3, can_delete=True)
                    item_formset = SalesInvoiceItemFormSet(queryset=SalesInvoiceItem.objects.none())
                    sales_formset = SalesInvoiceItemFormSet(queryset=SalesInvoiceItem.objects.none())
                    all_items = Item.objects.all()
                    company_country = _get_current_company_country(request)
                    company = _get_company_for_request(request)
                    company_tax_type = _get_request_company_tax_type(request, company)
                    return render(request, 'sales/invoice_add.html', {
                        'invoice_form': invoice_form,
                        'item_formset': item_formset,
                        'sales_formset': sales_formset,
                        'all_items': all_items,
                        'today': localdate().isoformat(),
                        'q_no': f"SQ-{SalesInvoice.objects.count() + 1:05d}",
                        'company_country': company_country,
                        'company_is_india': _is_indian_company_country(company_country),
                        'company_tax_type': company_tax_type,
                        'error_message': str(e),
                        'show_error_modal': True,
                    })
        

        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
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


        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
        post_data = _normalize_item_tax_tokens(post_data, company_is_india)

        raw_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
        try:
            total_amount = (
                Decimal(str(raw_total).strip())
                if raw_total not in (None, '')
                else Decimal('0')
            )
        except Exception:
            total_amount = Decimal('0')
        rounding_company = _get_company_for_request(request)
        total_amount, rounding_adjustment = apply_sales_rounding(total_amount, rounding_company)
        customer_id = request.POST.get('customer')
        date = request.POST.get('date')
        sales_person_id = request.POST.get('sales_person')
        notes = request.POST.get('notes', '')
        inv_number = generate_inv_number(date)
        # #print("quote_number:",quote_number)
        try:
            discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
            discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_value = Decimal('0.00')
        discount_type = request.POST.get('discount_type', 'percent')

        if not customer_id or not date:
            #added by neha on 16-2-26
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': 'Customer and Invoice Date are required.'
                }, status=400)
            messages.error(request, "Customer and Invoice Date are required.")
            return redirect_with_company('sales_inv_list')

        try:
            customer = Customer.objects.get(pk=customer_id)
            sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None
            credit_limit_error = build_customer_credit_limit_message(customer, total_amount)
            if credit_limit_error:
                return credit_limit_block_response(request, credit_limit_error, 'sales_inv_list')

            place_of_supply = request.POST.get('place_of_supply', '')
            company_db = getattr(request, 'company_db', 'default')
            company_tax_type = (get_company_tax_type(using=company_db) or '').strip().upper()
            turnover_tax_obj = None
            if company_tax_type == 'TURNOVER':
                turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                if turnover_tax_id:
                    turnover_tax_obj = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first()
            tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
            tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
            tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
            tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
            
            invoice = SalesInvoice.objects.create(
                customer=customer,
                date=date,
                sales_person=sales_person,
                inv_number=inv_number,
                # New invoice should start as Open.
                status='Open',
                total_amount=total_amount,
                round_off=rounding_adjustment,
                fx_rate_to_base=Decimal('1.000000'),
                notes=notes,
                discount_value=discount_value,
                discount_type=discount_type,
                place_of_supply=place_of_supply,
                shipping_attention=request.POST.get('shipping_attention', ''),
                shipping_email=request.POST.get('shipping_email', ''),
                shipping_phone=request.POST.get('shipping_phone', ''),
                shipping_country=request.POST.get('shipping_country', ''),
                shipping_address1=request.POST.get('shipping_address1', ''),
                shipping_address2=request.POST.get('shipping_address2', ''),
                shipping_city=request.POST.get('shipping_city', ''),
                shipping_state=request.POST.get('shipping_state', ''),
                shipping_postal_code=request.POST.get('shipping_postal_code', ''),
                tds_tcs_type=tds_tcs_type,
                tds_tcs_definition_id=tds_tcs_definition_id,
                tds_tcs_rate=tds_tcs_rate,
                tds_tcs_amount=tds_tcs_amount,
                turnover_tax=turnover_tax_obj,
            )

            # Apply document currency and FX values based on customer/company
            try:
                company = _get_company_for_request(request)
                from currencies.services import apply_sales_invoice_fx, parse_currency_fx_from_post

                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
                apply_sales_invoice_fx(
                    invoice,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )
            except Exception:
                logger.exception('Failed to apply FX to invoice %s', getattr(invoice, 'pk', None))

            # attach user/request so activity log picks it up even though the model lacks
            invoice._current_user = request.user
            invoice._current_request = request
            invoice.save()
            # Attach payment term if provided
            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    pt = PayTerms.objects.get(pk=pay_term_id)
                    invoice.payment_term = pt
                    invoice.save()
                except PayTerms.DoesNotExist:
                    pass
        except Customer.DoesNotExist:
            error_msg = 'The selected customer was not found. Refresh the page and try again.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect_with_company('sales_inv_list')
        except SalesPerson.DoesNotExist:
            error_msg = 'The selected sales person was not found. Clear the field or choose another.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=400)
            messages.error(request, error_msg)
            return redirect_with_company('sales_inv_list')
        #updated by neha on 16-2-26 
        except IntegrityError as e:
            err_lower = str(e).lower()
            detail = str(e).strip()
            if 'unique constraint' in err_lower or 'duplicate entry' in err_lower or 'unique key' in err_lower:
                error_msg = f"Invoice Number '{inv_number}' already exists. Please choose a different one."
            elif (
                'foreign key' in err_lower
                or 'violates foreign key' in err_lower
                or 'child row' in err_lower
            ):
                error_msg = (
                    "Cannot save invoice: a related master record is missing "
                    "(often Payment Status). Add Payment Status rows in Purchase settings or run migrations, then try again. "
                    f"Details: {detail}"
                )
            elif 'not null' in err_lower or 'cannot be null' in err_lower or 'null value' in err_lower:
                error_msg = (
                    "Cannot save invoice: a required field is empty at the database level. "
                    f"Details: {detail}"
                )
            elif 'check constraint' in err_lower:
                error_msg = f"Cannot save invoice: data failed a database check. Details: {detail}"
            else:
                # Use module logger by qualified call so this never hits UnboundLocalError
                # if a future edit adds `logger = ...` anywhere in this function.
                logging.getLogger(__name__).warning(
                    'save_salesinvoice IntegrityError (unclassified): %s', e, exc_info=True
                )
                error_msg = f"Database error while saving the invoice: {detail}"
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'error': error_msg
                }, status=400)
            
            messages.error(request, error_msg)
            return redirect_with_company('sales_inv_list')

        item_rows = _extract_line_items(post_data, prd_brcd_map)

        if not item_rows:
            messages.error(request, "At least one item is required for a sales invoice.")
            return redirect_with_company('sales_inv_list')

        # initialize accumulators for journal posting (invoice)
        taxable_total = Decimal(0)
        tax_totals = {}
        company = _get_company_for_tax()
        InvoiceTax.objects.filter(invoice=invoice).delete()
        items = []

        for row in item_rows:
            quantity = Decimal(str(row['quantity'])) if row['quantity'] else Decimal('0')
            price = Decimal(str(row['price'])) if row['price'] else Decimal('0')

            item = SalesInvoiceItem.objects.create(
                sales_inv=invoice,
                product_id=row['product_id'],
                prd_brcd=row.get('prd_brcd', ''),
                hsn_code=row.get('hsn_code', ''),
                prd_disvalue=row.get('discount', '0'),
                prd_distype=row.get('discount_type', 'flat'),
                quantity=quantity,
                price=price,
                o_price=Decimal(str(row.get('o_price') or '0')),
            )

            selected_tax = _normalize_tax_selection_token(row.get('tax_token', ''), company_is_india)
            item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
            tax_override = None
            if selected_tax.startswith('group:'):
                tax_override = TaxGroup.objects.filter(
                    id=selected_tax.split(':', 1)[1]
                ).prefetch_related('taxes').first()
            tax_result = _calculate_tax_for_invoice_item(
                item,
                invoice.customer,
                company,
                tax_override=tax_override
            )
            if not selected_tax and tax_result.get('total_rate'):
                item.prd_tax = tax_result['total_rate']
                item.prd_taxgroup = tax_result.get('tax_name') or item.prd_taxgroup
            if hasattr(item, 'tax_amount'):
                item.tax_amount = tax_result.get('total_tax', Decimal('0.00'))
            save_fields = ['prd_tax', 'prd_taxgroup']
            if hasattr(item, 'tax_amount'):
                save_fields.append('tax_amount')
            item.save(update_fields=save_fields)
            items.append(item)

            base_amount = tax_result.get('base_amount', Decimal('0.00'))
            taxable_total += base_amount

            breakdown = tax_result.get('breakdown', [])
            # Keep create behavior aligned with edit: if tax engine returns no
            # breakdown but we have a rate, derive one deterministic component.
            if not breakdown and item.prd_tax:
                tax_amount = (
                    base_amount * Decimal(item.prd_tax) / Decimal('100')
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                tax_name = item.prd_taxgroup or f'Tax-{item.prd_tax}%'
                if tax_amount > 0:
                    breakdown = [{
                        'name': tax_name,
                        'rate': item.prd_tax,
                        'amount': tax_amount,
                    }]
            for comp in breakdown:
                InvoiceTax.objects.create(
                    invoice=invoice,
                    tax_name=comp.get('name') or '',
                    tax_rate=comp.get('rate') or Decimal('0.00'),
                    tax_amount=comp.get('amount') or Decimal('0.00'),
                )
                comp_key = (comp.get('name') or '').upper()
                comp_amount = comp.get('amount') or Decimal('0.00')
                if comp_key and comp_amount > 0:
                    tax_totals[comp_key] = tax_totals.get(comp_key, Decimal(0)) + comp_amount

        invoice.total_amount_base = _calculate_document_base_total(
            invoice.items.all(),
            invoice.fx_rate_to_base,
            invoice.discount_value,
            invoice.discount_type,
        )
        invoice.total_amount_base = (
            Decimal(invoice.total_amount_base or 0)
            + (rounding_adjustment * Decimal(invoice.fx_rate_to_base or 1))
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        invoice.round_off = rounding_adjustment
        SalesInvoice.objects.using(company_db).filter(pk=invoice.pk).update(
            total_amount_base=invoice.total_amount_base,
            round_off=rounding_adjustment,
        )
        invoice.refresh_from_db(using=company_db)

        # Create Journal Entry for this invoice.
        # Posting pattern:
        #   Debit  : Debtors (Accounts Receivable) = invoice.total_amount
        #   Credit : Sales = sum of taxable amounts
        #   Credit : Output Tax (CGST/SGST/IGST) = respective tax totals
        # If rounding causes a small imbalance, a 'Rounded Off' line is added to balance the JV.
        try:
            # generate next JV number
            last = JournalEntry.objects.order_by('-id').first()
            if last and last.entry_number and last.entry_number.startswith('JV-'):
                try:
                    last_num = int(last.entry_number.split('-')[1])
                except Exception:
                    last_num = 0
            else:
                last_num = 0
            next_num = last_num + 1
            entry_number = f"JV-{str(next_num).zfill(5)}"

            journal = JournalEntry.objects.create(
                entry_number=entry_number,
                date=invoice.date,
                reference=invoice.inv_number,
                narration=f"Sales Invoice {invoice.inv_number}",
                created_by=request.user,
                updated_by=request.user,
                status='posted'
            )

            # Debit: Debtors (Accounts Receivable)
            debt_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Debtors').first()
            if not debt_acct:
                debt_acct = ChartOfAccounts.objects.using(db).filter(code='1020101').first()
            # Adjust receivable for TDS/TCS so the JV balances correctly.
            tds_base = _scaled_tds_tcs_amount(invoice)
            debit_amt = Decimal(invoice.total_amount_base or 0)
            if str(getattr(invoice, 'tds_tcs_type', '') or '').strip().lower() == 'tds':
                debit_amt = (debit_amt - tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            elif str(getattr(invoice, 'tds_tcs_type', '') or '').strip().lower() == 'tcs':
                debit_amt = (debit_amt + tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            JournalLine.objects.create(
                journal=journal,
                account=debt_acct,
                description=f"Invoice {invoice.inv_number} - Receivable",
                debit=debit_amt,
                credit=Decimal(0),
                sequence=10
            )#byadarsh
            logger.info(
                "Posting save_salesinvoice JV %s: inv=%s total_amount_base=%s tds_base=%s debit_amt=%s",
                entry_number,
                getattr(invoice, 'inv_number', None),
                getattr(invoice, 'total_amount_base', None),
                tds_base,
                debit_amt,
            )

            # byadarshDebit: Individual Customer Account
            # if invoice.customer:
            #     customer_name = f"{invoice.customer.first_name} {invoice.customer.last_name}".strip()
            #     customer_acct = ChartOfAccounts.objects.filter(name__iexact=customer_name).first()
            #     
            #     # If customer account doesn't exist, create it
            #     if not customer_acct:
            #         # Find the parent Debtors account
            #         debtors_parent = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
            #         if not debtors_parent:
            #             debtors_parent = ChartOfAccounts.objects.filter(code='1020101').first()
            #         
            #         # Create customer account as a sub-account of Debtors
            #         if debtors_parent:
            #             customer_acct = ChartOfAccounts.objects.create(
            #                 code=f"CUST-{invoice.customer.id}",
            #                 name=customer_name,
            #                 type=debtors_parent.type,
            #                 parent=debtors_parent,
            #                 description=f"Customer Account - {invoice.customer.email or ''}",
            #                 is_header=False,
            #                 active=True,
            #                 created_by=request.user,
            #                 updated_by=request.user,
            #                 status=True
            #             )
            #     
            #     # Create journal line for customer account
            #     if customer_acct:
            #         JournalLine.objects.create(
            #             journal=journal,
            #             account=customer_acct,
            #             description=f"Invoice {invoice.inv_number} - {customer_name}",
            #             debit=Decimal(invoice.total_amount or 0),
            #             credit=Decimal(0),
            #             sequence=15
            #         )#byadarsh

            # Credit: Sales (sum of taxable_total minus any discount)
            sales_acct = None
            # try to find a sales account from first item product
            if items:
                prod_sales_acc = items[0].product.sales_account if getattr(items[0], 'product', None) else None
                if prod_sales_acc:
                    sales_acct = ChartOfAccounts.objects.using(db).filter(code=str(prod_sales_acc)).first() or ChartOfAccounts.objects.using(db).filter(name__iexact=str(prod_sales_acc)).first()
            if not sales_acct:
                sales_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Sales').first()
            # Post directly from base-currency computed values.
            sales_credit_amount, base_tax_totals = _build_base_tax_posting_amounts(invoice, tax_totals)
            JournalLine.objects.create(
                journal=journal,
                account=sales_acct,
                description=f"Invoice {invoice.inv_number} - Sales",
                debit=Decimal(0),
                credit=sales_credit_amount,
                sequence=20
            )

            # Credit: Output Tax accounts for each tax type
            # We map tax type codes (CGST/SGST/IGST) to exact COA account names and
            # post the quantized per-type amounts as credit lines.
            # Note: we explicitly search for the base accounts (not Refund variants)
            tax_account_map = {
                'CGST': 'Output Tax CGST',
                'SGST': 'Output Tax SGST',
                'IGST': 'Output Tax IGST',
            }
            seq = 30
            # Convert tax totals to base once, then post. For CGST+SGST we split
            # from combined base amount to avoid FX cent-rounding skew (e.g. 4.17/3.33).
            # quantize tax totals and post lines
            for ttype, amount in base_tax_totals.items():
                amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if amount == Decimal('0.00'):
                    continue
                
                # Check if we have a specific account for this tax type
                acct = resolve_tax_account(ttype, direction='output', using=db)
                
                # If we get a fallback account (like "Duties and Taxes") for a compound tax, split it
                if acct and acct.name and 'Duties and Taxes' in acct.name:
                    # This is likely a compound tax like "GST5", split it into CGST and SGST
                    cgst_amount = (amount / Decimal('2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    sgst_amount = (amount - cgst_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    
                    # Create CGST entry
                    cgst_acct = resolve_tax_account('CGST', direction='output', using=db)
                    if cgst_acct and cgst_amount > 0:
                        JournalLine.objects.create(
                            journal=journal,
                            account=cgst_acct,
                            description=f"Invoice {invoice.inv_number} - CGST",
                            debit=Decimal(0),
                            credit=cgst_amount,
                            sequence=seq
                        )
                        seq += 10
                    
                    # Create SGST entry
                    sgst_acct = resolve_tax_account('SGST', direction='output', using=db)
                    if sgst_acct and sgst_amount > 0:
                        JournalLine.objects.create(
                            journal=journal,
                            account=sgst_acct,
                            description=f"Invoice {invoice.inv_number} - SGST",
                            debit=Decimal(0),
                            credit=sgst_amount,
                            sequence=seq
                        )
                        seq += 10
                elif acct:
                    JournalLine.objects.create(
                        journal=journal,
                        account=acct,
                        description=f"Invoice {invoice.inv_number} - {ttype}",
                        debit=Decimal(0),
                        credit=amount,
                        sequence=seq
                    )
                    seq += 10
            # update journal totals
            seq = _post_tds_tcs_journal_line(journal, invoice, db, seq, posting_role='sales')
            journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
            journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())

            # check for small rounding difference and post a 'Rounded Off' balancing line
            # compute difference (debit - credit) after summing created journal lines
            diff = (Decimal(journal.total_debit) - Decimal(journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if diff != Decimal('0.00'):
                # find Rounded Off account (fall back to code '40217')
                round_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Rounded Off').first()
                if not round_acct:
                    round_acct = ChartOfAccounts.objects.using(db).filter(code='40217').first()
                if round_acct:
                    if diff > 0:
                        # debit > credit -> add credit rounded off to balance
                        JournalLine.objects.create(
                            journal=journal,
                            account=round_acct,
                            description=f"Invoice {invoice.inv_number} - Rounded Off",
                            debit=Decimal(0),
                            credit=diff,
                            sequence=seq
                        )
                    else:
                        # credit > debit -> add debit rounded off to balance
                        amt = abs(diff)
                        JournalLine.objects.create(
                            journal=journal,
                            account=round_acct,
                            description=f"Invoice {invoice.inv_number} - Rounded Off",
                            debit=amt,
                            credit=Decimal(0),
                            sequence=seq
                        )
                    # recalc totals after rounding line
                    journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
                    journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
            journal.save()
            
            # Create COGS Transfer Entry (moved the cost from Stock to Expense)
            # This ensures P&L shows true profit (Revenue - COGS - Expenses)
            try:
                cogs_journal = create_cogs_transfer(invoice, user=request.user)
                _log = logging.getLogger(__name__)
                if cogs_journal:
                    _log.info(
                        "COGS transfer created for invoice %s: %s",
                        invoice.inv_number,
                        cogs_journal.entry_number,
                    )
                else:
                    _log.warning("COGS transfer not created for invoice %s", invoice.inv_number)
            except Exception as e:
                logging.getLogger(__name__).exception(
                    "Failed to create COGS transfer for invoice %s: %s",
                    invoice.inv_number,
                    str(e),
                )
        except Exception:
            # Do not block invoice save on accounting post failures; log if logger available
            # Note: do not assign `logger = ...` here ₹ it makes `logger` local for the whole
            # function and breaks earlier uses with UnboundLocalError.
            try:
                logging.getLogger(__name__).exception(
                    'Failed to create journal entry for invoice %s', invoice.pk
                )
            except Exception:
                pass

        messages.success(request, "Sales Invoice created successfully!")
        #added by neha on 16-2-26
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'invoice_id': invoice.id,
                'invoice_number': invoice.inv_number,
                'customer_name': (
                    invoice.customer.company_name
                    if invoice.customer and invoice.customer.customer_type == 'company'
                    else (
                        f"{invoice.customer.first_name} "
                        f"{invoice.customer.last_name or ''}"
                    ).strip()
                    if invoice.customer
                    else ''
                ),
                'total_amount': str(invoice.total_amount),
                'currency_symbol': invoice.document_currency.symbol if invoice.document_currency else "₹",
                'message': 'Sales Invoice created successfully!'
            })
        return redirect_with_company('sales_inv_list')
    else:
        return redirect_with_company('sales_inv_list')



#correctly prefilled
def invoice_edit(request, pk):
    #for viewing only
    readonly = request.GET.get('readonly', 'false').lower() == 'true'
    # If user doesn't have edit permission, force readonly
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_invoices(request.user)):
            readonly = True
    except Exception:
        readonly = True
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    SalesInvoiceItemFormSet = modelformset_factory(SalesInvoiceItem, form=SalesInvoiceItemForm, extra=0,can_delete=True)

    if request.method == "POST" and not readonly:
        # Permission: require Edit on Invoices
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_invoices(request.user)):
                messages.error(request, 'You do not have permission to edit Sales Invoices.')
                return redirect_with_company('sales_inv_list')
        except Exception:
            messages.error(request, 'You do not have permission to edit Sales Invoices.')
            return redirect_with_company('sales_inv_list')
        
        # — CHECK PERIOD LOCK BEFORE EDITING INVOICE
        from datetime import datetime
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(invoice.date, request.user, db=db, transaction_type='invoice')
        except PermissionDenied as e:
            # — For AJAX requests, return JSON
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': str(e)}, status=403)
            
            messages.error(request, str(e))
            return redirect_with_company('sales_inv_list')
        
        # Extract data from POST (same way as save_salesinvoice)
        post_data = request.POST.copy()
        prd_brcd_map = {}
        # If customer was changed in the edit form/modal, assign it now so
        # subsequent checks (credit limit, taxes) use the updated customer.
        cust_id = post_data.get('customer') or post_data.get('customer_select')
        if cust_id:
            try:
                from customer.models import Customer
                new_cust = Customer.objects.filter(pk=cust_id).first()
                if new_cust:
                    invoice.customer = new_cust
            except Exception:
                logger.exception('Failed to assign customer during invoice edit')
        
        # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
        for key in post_data:
            if key.startswith("form-") and key.endswith("-product"):
                value = post_data[key]
                if value:
                    parts = value.split("_", 1)
                    post_data[key] = parts[0]
                    if len(parts) > 1:
                        prefix = key.rsplit("-", 1)[0]
                        prd_brcd_map[prefix] = parts[1]
        
        total_amount = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
        try:
            total_amount = Decimal(str(total_amount).strip()) if total_amount not in (None, '') else Decimal('0.00')
        except Exception:
            total_amount = Decimal('0.00')
        rounding_company = _get_company_for_request(request)
        total_amount, rounding_adjustment = apply_sales_rounding(total_amount, rounding_company)
        date = request.POST.get('date')
        notes = request.POST.get('notes', '')
        try:
            discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
            discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            discount_value = Decimal('0.00')
        discount_type = request.POST.get('discount_type', 'percent')
        
        if not date:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Invoice Date is required.'}, status=400)
            messages.error(request, "Invoice Date is required.")
            return redirect_with_company('sales_inv_list')
        
        try:
            from django.db import transaction
            credit_limit_error = build_customer_credit_limit_message(invoice.customer, total_amount, exclude_invoice=invoice)
            if credit_limit_error:
                return credit_limit_block_response(request, credit_limit_error, 'sales_inv_list')
            
            with transaction.atomic(using=db):
                existing_items_before_save = list(
                    SalesInvoiceItem.objects.filter(sales_inv=invoice).order_by('id')
                )

                # Update invoice header
                invoice.date = date
                invoice.total_amount = total_amount
                invoice.round_off = rounding_adjustment
                invoice.notes = notes
                invoice.discount_value = discount_value
                invoice.discount_type = discount_type
                company_tax_type = (get_company_tax_type(using=db) or '').strip().upper()
                if company_tax_type == 'TURNOVER':
                    turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
                    invoice.turnover_tax = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first() if turnover_tax_id else None
                else:
                    invoice.turnover_tax = None
                invoice.shipping_attention = request.POST.get('shipping_attention', '')
                invoice.shipping_email = request.POST.get('shipping_email', '')
                invoice.shipping_phone = request.POST.get('shipping_phone', '')
                invoice.shipping_country = request.POST.get('shipping_country', '')
                invoice.shipping_address1 = request.POST.get('shipping_address1', '')
                invoice.shipping_address2 = request.POST.get('shipping_address2', '')
                invoice.shipping_city = request.POST.get('shipping_city', '')
                invoice.shipping_state = request.POST.get('shipping_state', '')
                invoice.shipping_postal_code = request.POST.get('shipping_postal_code', '')
                invoice.tds_tcs_type = request.POST.get('tds_tcs_type', invoice.tds_tcs_type or 'tds')
                invoice.tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
                invoice.tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
                invoice.tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))

                # Update payment term if provided (was previously missing, causing
                # the selected payment term to be silently dropped on invoice edit)
                pay_term_id = (request.POST.get('payment_term') or '').strip()
                if pay_term_id:
                    try:
                        invoice.payment_term = PayTerms.objects.get(pk=pay_term_id)
                    except PayTerms.DoesNotExist:
                        pass
                else:
                    invoice.payment_term = None

                invoice._current_user = request.user

                invoice._current_request = request

                from currencies.services import apply_sales_invoice_fx, parse_currency_fx_from_post
                company = getattr(request, 'company', None) or _get_company_for_tax()
                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
                apply_sales_invoice_fx(
                    invoice,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )

                invoice.save()
                
                # Delete old items and taxes - MUST delete taxes first!
                InvoiceTax.objects.filter(invoice=invoice).delete()
                SalesInvoiceItem.objects.filter(sales_inv=invoice).delete()
                
                # Parse items from formset POST data
                items = []
                taxable_total = Decimal(0)
                tax_totals = {}
                company = _get_company_for_tax()
                
                logger.info(f'Invoice edit starting: company={company}, invoice={invoice.inv_number}')
                
                index = 0
                while True:
                    product_key = f'form-{index}-product'
                    if product_key not in post_data:
                        break
                    
                    product_id = post_data.get(product_key, '').strip()
                    if not product_id:
                        index += 1
                        continue
                    
                    try:
                        product = Item.objects.get(pk=product_id)
                        quantity = int(post_data.get(f'form-{index}-quantity', 0) or 0)
                        price = Decimal(post_data.get(f'form-{index}-price', 0) or 0)
                        
                        if quantity > 0:
                            discount_raw = post_data.get(f'form-{index}-prd_disvalue', '0')
                            discount_raw = str(discount_raw).strip()
                            if not discount_raw:
                                discount_raw = '0'
                            try:
                                item_item_discount_value = Decimal(str(discount_raw))
                            except (InvalidOperation, ValueError, TypeError):
                                item_item_discount_value = Decimal('0.00')
                            discount_type_selected = post_data.get(f'form-{index}-prd_distype', 'flat') or 'flat'
                            posted_o_price = Decimal(str(post_data.get(f'form-{index}-o_price', '0') or '0'))
                            existing_item = existing_items_before_save[index] if index < len(existing_items_before_save) else None
                            if posted_o_price > Decimal('0.00'):
                                o_price_value = posted_o_price
                            elif (
                                existing_item
                                and existing_item.product_id == product.id
                                and getattr(existing_item, 'o_price', None) not in (None, Decimal('0.00'))
                            ):
                                o_price_value = Decimal(str(existing_item.o_price))
                            else:
                                o_price_value = (
                                    price * Decimal(str(invoice.fx_rate_to_base or 1))
                                ).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)

                            # Create item
                            item = SalesInvoiceItem.objects.create(
                                sales_inv=invoice,
                                product=product,
                                quantity=quantity,
                                price=price,
                                o_price=o_price_value,
                                description=post_data.get(f'form-{index}-description', ''),
                                hsn_code=post_data.get(f'form-{index}-hsn_code', ''),
                                prd_disvalue=item_item_discount_value,
                                prd_distype=discount_type_selected,
                            )
                            
                            # Handle barcode
                            prefix = f'form-{index}'
                            if prefix in prd_brcd_map:
                                item.prd_brcd = prd_brcd_map[prefix]
                            
                            # Handle tax
                            selected_tax = post_data.get(f'form-{index}-prd_tax', '').strip()
                            tg = None
                            item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)
                            if selected_tax.startswith('group:'):
                                tg = TaxGroup.objects.filter(
                                    id=selected_tax.split(':', 1)[1]
                                ).prefetch_related('taxes').first()
                            item.save()
                            
                            items.append(item)
                            logger.info(f'Invoice edit: created item {item.id}, product={product.id}, quantity={quantity}, price={price}')
                            
                            # Calculate taxable for journal
                            tax_result = _calculate_tax_for_invoice_item(
                                item,
                                invoice.customer,
                                company,
                                tax_override=tg
                            )
                            if not tg and tax_result.get('total_rate'):
                                item.prd_tax = tax_result['total_rate']
                                item.prd_taxgroup = tax_result.get('tax_name') or item.prd_taxgroup
                            # Only save fields that actually exist in the model
                            item.save(update_fields=['prd_tax', 'prd_taxgroup'])

                            base_amount = tax_result.get('base_amount', Decimal('0.00'))
                            taxable_total += base_amount
                            
                            # Debug log for invoice edit
                            logger.info(f'Invoice edit item {index}: tax_result={tax_result}, base_amount={base_amount}')
                            
                            breakdown = tax_result.get('breakdown', [])
                            
                            # If breakdown is empty but we have a tax rate, calculate it manually
                            if not breakdown and item.prd_tax:
                                tax_amount = (base_amount * Decimal(item.prd_tax) / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                                tax_name = item.prd_taxgroup or f'Tax-{item.prd_tax}%'
                                if tax_amount > 0:
                                    logger.warning(f'Invoice edit: using fallback tax calculation for item {index}: tax_name={tax_name}, rate={item.prd_tax}%, amount={tax_amount}')
                                    breakdown = [{
                                        'name': tax_name,
                                        'rate': item.prd_tax,
                                        'amount': tax_amount,
                                    }]
                            
                            for comp in breakdown:
                                InvoiceTax.objects.create(
                                    invoice=invoice,
                                    tax_name=comp.get('name') or '',
                                    tax_rate=comp.get('rate') or Decimal('0.00'),
                                    tax_amount=comp.get('amount') or Decimal('0.00'),
                                )
                                comp_key = (comp.get('name') or '').upper()
                                comp_amount = comp.get('amount') or Decimal('0.00')
                                if comp_key and comp_amount > 0:
                                    tax_totals[comp_key] = tax_totals.get(comp_key, Decimal(0)) + comp_amount
                                    logger.info(f'Invoice edit: added tax {comp_key}={comp_amount}, tax_totals now={tax_totals}')
                    except Item.DoesNotExist:
                        # Product doesn't exist, skip this item
                        pass
                    except Exception as e:
                        # Log the error for debugging
                        logger.exception(f'Error processing item at index {index} during invoice edit: {str(e)}')
                        pass
                    
                    index += 1
                
                # Log final status before journal creation
                logger.info(f'Invoice edit items_loop complete: items_count={len(items)}, taxable_total={taxable_total}, tax_totals={tax_totals}')
                logger.info(f'InvoiceTax records created: {InvoiceTax.objects.filter(invoice=invoice).count()}')
                invoice.total_amount_base = _calculate_document_base_total(
                    invoice.items.all(),
                    invoice.fx_rate_to_base,
                    invoice.discount_value,
                    invoice.discount_type,
                )
                invoice.total_amount_base = (
                    Decimal(invoice.total_amount_base or 0)
                    + (rounding_adjustment * Decimal(invoice.fx_rate_to_base or 1))
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if (get_company_tax_type(using=db) or '').strip().upper() == 'TURNOVER':
                    invoice.total_amount_base = (Decimal(invoice.total_amount or 0) * Decimal(invoice.fx_rate_to_base or 1)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                SalesInvoice.objects.using(db).filter(pk=invoice.pk).update(
                    total_amount_base=invoice.total_amount_base,
                    round_off=rounding_adjustment,
                )
                invoice.refresh_from_db(using=db)
                
                # === STEP 1: REVERSE OLD JOURNAL ENTRIES ===
                from journal.models import JournalEntry, JournalLine
                
                # Only reverse the MOST RECENT active (non-reversal) entry for each reference
                old_invoice_entry = JournalEntry.objects.using(db).filter(
                    reference=invoice.inv_number
                ).exclude(
                    narration__startswith='Reversal of'
                ).prefetch_related('lines').order_by('-id').first()
                
                old_cogs_entry = JournalEntry.objects.using(db).filter(
                    reference=f"COGS-{invoice.inv_number}"
                ).exclude(
                    narration__startswith='Reversal of'
                ).prefetch_related('lines').order_by('-id').first()
                
                old_entries = []
                if old_invoice_entry:
                    old_entries.append(old_invoice_entry)
                if old_cogs_entry:
                    old_entries.append(old_cogs_entry)
                
                reversal_count = 0
                for old_entry in old_entries:
                    last = JournalEntry.objects.using(db).order_by('-id').first()
                    if last and last.entry_number and last.entry_number.startswith('JV-'):
                        try:
                            last_num = int(last.entry_number.split('-')[1])
                        except Exception:
                            last_num = 0
                    else:
                        last_num = 0
                    next_num = last_num + 1
                    reversal_entry_number = f"JV-{str(next_num).zfill(5)}"
                    
                    reversal_entry = JournalEntry.objects.using(db).create(
                        entry_number=reversal_entry_number,
                        date=invoice.date,
                        reference=old_entry.reference,
                        narration=f"Reversal of {old_entry.entry_number}",
                        created_by=request.user,
                        updated_by=request.user,
                        status='posted'
                    )
                    
                    for line in old_entry.lines.all():
                        JournalLine.objects.using(db).create(
                            journal=reversal_entry,
                            account=line.account,
                            description=f"Reversal: {line.description}",
                            debit=line.credit,
                            credit=line.debit,
                            sequence=line.sequence
                        )
                    # Recalculate and persist totals for the reversal entry
                    try:
                        reversal_entry.total_debit = sum(l.debit or 0 for l in reversal_entry.lines.all())
                        reversal_entry.total_credit = sum(l.credit or 0 for l in reversal_entry.lines.all())
                        reversal_entry.save(update_fields=['total_debit', 'total_credit'])
                    except Exception:
                        logger.exception('Failed to update totals for reversal entry %s', getattr(reversal_entry, 'id', None))
                    reversal_count += 1
                
                
                # === STEP 2: POST NEW INVOICE JOURNAL ===
                
                last = JournalEntry.objects.using(db).order_by('-id').first()
                if last and last.entry_number and last.entry_number.startswith('JV-'):
                    try:
                        last_num = int(last.entry_number.split('-')[1])
                    except Exception:
                        last_num = 0
                else:
                    last_num = 0
                next_num = last_num + 1
                while JournalEntry.objects.using(db).filter(entry_number=f"JV-{str(next_num).zfill(5)}").exists():
                    next_num += 1
                new_entry_number = f"JV-{str(next_num).zfill(5)}"
                
                new_journal = JournalEntry.objects.using(db).create(
                    entry_number=new_entry_number,
                    date=invoice.date,
                    reference=invoice.inv_number,
                    narration=f"Sales Invoice {invoice.inv_number}",
                    created_by=request.user,
                    updated_by=request.user,
                    status='posted'
                )
                
                # Debit: Debtors
                debt_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Debtors').first()
                if not debt_acct:
                    debt_acct = ChartOfAccounts.objects.using(db).filter(code='1020101').first()
                tds_base = _scaled_tds_tcs_amount(invoice)
                debit_amt = Decimal(invoice.total_amount_base or 0)
                if str(getattr(invoice, 'tds_tcs_type', '') or '').strip().lower() == 'tds':
                    debit_amt = (debit_amt - tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                elif str(getattr(invoice, 'tds_tcs_type', '') or '').strip().lower() == 'tcs':
                    debit_amt = (debit_amt + tds_base).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                JournalLine.objects.using(db).create(
                    journal=new_journal,
                    account=debt_acct,
                    description=f"Invoice {invoice.inv_number} - Receivable",
                    debit=debit_amt,
                    credit=Decimal(0),
                    sequence=10
                )
                
                # Credit: Sales
                sales_acct = None
                if items and len(items) > 0:
                    logger.info(f'Invoice edit: found {len(items)} items, using first item for sales account')
                    prod_sales_acc = items[0].product.sales_account if getattr(items[0], 'product', None) else None
                    if prod_sales_acc:
                        sales_acct = ChartOfAccounts.objects.using(db).filter(code=str(prod_sales_acc)).first() or ChartOfAccounts.objects.using(db).filter(name__iexact=str(prod_sales_acc)).first()
                else:
                    logger.warning(f'Invoice edit: NO ITEMS FOUND IN LIST! items={items}')
                    
                if not sales_acct:
                    sales_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Sales').first()
                
                # Post directly from base-currency computed values.
                sales_credit_amount, base_tax_totals = _build_base_tax_posting_amounts(invoice, tax_totals)
                
                logger.info(f'Invoice edit: creating sales line with amount {sales_credit_amount}')
                JournalLine.objects.using(db).create(
                    journal=new_journal,
                    account=sales_acct,
                    description=f"Invoice {invoice.inv_number} - Sales",
                    debit=Decimal(0),
                    credit=sales_credit_amount,
                    sequence=20
                )
                
                # Credit: Output Tax
                tax_account_map = {
                    'CGST': 'Output Tax CGST',
                    'SGST': 'Output Tax SGST',
                    'IGST': 'Output Tax IGST',
                }
                seq = 30
                # Convert tax totals to base once, then post. For CGST+SGST we split
                # from combined base amount to avoid FX cent-rounding skew (e.g. 4.17/3.33).
                for ttype, amount in base_tax_totals.items():
                    amount = Decimal(amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                    if amount == Decimal('0.00'):
                        continue
                    
                    # Check if we have a specific account for this tax type
                    acct = resolve_tax_account(ttype, direction='output', using=db)
                    
                    # If we get a fallback account (like "Duties and Taxes") for a compound tax, split it
                    if acct and acct.name and 'Duties and Taxes' in acct.name:
                        # This is likely a compound tax like "GST5", split it into CGST and SGST
                        cgst_amount = (amount / Decimal('2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        sgst_amount = (amount - cgst_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        
                        # Create CGST entry
                        cgst_acct = resolve_tax_account('CGST', direction='output', using=db)
                        if cgst_acct and cgst_amount > 0:
                            JournalLine.objects.using(db).create(
                                journal=new_journal,
                                account=cgst_acct,
                                description=f"Invoice {invoice.inv_number} - CGST",
                                debit=Decimal(0),
                                credit=cgst_amount,
                                sequence=seq
                            )
                            seq += 10
                        
                        # Create SGST entry
                        sgst_acct = resolve_tax_account('SGST', direction='output', using=db)
                        if sgst_acct and sgst_amount > 0:
                            JournalLine.objects.using(db).create(
                                journal=new_journal,
                                account=sgst_acct,
                                description=f"Invoice {invoice.inv_number} - SGST",
                                debit=Decimal(0),
                                credit=sgst_amount,
                                sequence=seq
                            )
                            seq += 10
                    elif acct:
                        JournalLine.objects.using(db).create(
                            journal=new_journal,
                            account=acct,
                            description=f"Invoice {invoice.inv_number} - {ttype}",
                            debit=Decimal(0),
                            credit=amount,
                            sequence=seq
                        )
                        seq += 10
                
                # Check for rounding
                seq = _post_tds_tcs_journal_line(new_journal, invoice, db, seq, posting_role='sales')
                new_journal.total_debit = sum(line.debit or 0 for line in new_journal.lines.all())
                new_journal.total_credit = sum(line.credit or 0 for line in new_journal.lines.all())
                
                diff = (Decimal(new_journal.total_debit) - Decimal(new_journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                if diff != Decimal('0.00'):
                    round_acct = ChartOfAccounts.objects.using(db).filter(name__icontains='Rounded Off').first()
                    if not round_acct:
                        round_acct = ChartOfAccounts.objects.using(db).filter(code='40217').first()
                    if round_acct:
                        if diff > 0:
                            JournalLine.objects.using(db).create(
                                journal=new_journal,
                                account=round_acct,
                                description=f"Invoice {invoice.inv_number} - Rounded Off",
                                debit=Decimal(0),
                                credit=diff,
                                sequence=seq
                            )
                        else:
                            JournalLine.objects.using(db).create(
                                journal=new_journal,
                                account=round_acct,
                                description=f"Invoice {invoice.inv_number} - Rounded Off",
                                debit=abs(diff),
                                credit=Decimal(0),
                                sequence=seq
                            )
                        new_journal.total_debit = sum(line.debit or 0 for line in new_journal.lines.all())
                        new_journal.total_credit = sum(line.credit or 0 for line in new_journal.lines.all())
                
                new_journal.save()
                
                # === STEP 3: POST NEW COGS ===
                from sales.cogs_utils import create_cogs_transfer
                try:
                    cogs_journal = create_cogs_transfer(invoice, user=request.user)
                except Exception as e:
                    pass
                
                # Re-evaluate status after edit because total/line items may change payment completion.updt by naha on 23-2-26
                update_invoice_payment_status(invoice)
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': str(e)}, status=400)
            messages.error(request, f"Error updating invoice: {str(e)}")
            return redirect_with_company('sales_inv_list')
        
        # Success
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': 'Sales Invoice updated successfully!',
                'invoice_id': invoice.id,
                'invoice_number': invoice.inv_number,
                'customer_name': f"{invoice.customer.company_name if invoice.customer.customer_type == 'company' else invoice.customer.first_name}",
                'total_amount': str(invoice.total_amount),
                'currency_symbol': invoice.document_currency.symbol if invoice.document_currency else "₹"
            })
        
        messages.success(request, "Sales Invoice updated successfully!")
        return redirect_with_company('sales_inv_list')
    else:
        existing_items_qs = SalesInvoiceItem.objects.filter(sales_inv=invoice)
        sales_formset = SalesInvoiceItemFormSet(queryset=existing_items_qs)
        sp = _get_current_salesperson(request)
        if not getattr(invoice, 'sales_person', None) and sp:
            invoice_form = SalesInvoiceForm(instance=invoice, initial={'sales_person': sp.pk})
        else:
            invoice_form = SalesInvoiceForm(instance=invoice)
        company_country = _get_current_company_country(request)
        company_is_india = _is_indian_company_country(company_country)
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



        # — Fetch tax group names in bulk (to avoid N+1 queries)
        product_ids = existing_items_qs.values_list('product_id', flat=True)
        items_with_tax = Item.objects.filter(id__in=product_ids).select_related('intra_tax')

        tax_map = {}
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

        for form, item in zip(sales_formset.forms, existing_items_qs):
            key = f"{item.product_id}_{item.prd_brcd}"
            uom_name = uoms_by_barcode.get(key, '')
            # #print(f"Checking key {key} ₹ matched UOM: {uom_name}")

            # — Force the dropdown to display the correct text for this specific instance
            if uom_name:
                display_label = f"{item.product.name} ({uom_name})"
            else:
                display_label = item.product.name

            # — Replace the field choices with the selected item label
            form.fields['product'].choices = [
                (item.product.id, display_label)
            ]
            form.initial['product'] = item.product.id
            # #print(f"₹ Updated dropdown for {display_label}")


        # for item in existing_items_qs:
            # #print("prd_brcd value:", item.prd_brcd, "type:", type(item.prd_brcd))

        # — Combine product + UOM + Tax Group for display
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
    # return render(request, 'sales/quotation_duplicate.html', {
    #     'quotation_form': quotation_form,
    #     'sales_formset': sales_formset,
    #     'all_items': all_items,
    #     'today': localdate().isoformat(),
    #     'q_no': quote.quote_number,
    #     # 'item_display_list': item_display_list,
    #     'quote': quote, 
    #     'readonly': readonly,
        
    # })
    # Use origin_quote if available, otherwise use invoice for shipping info
    shipping_source = invoice.origin_quote or invoice
    
    # Company currencies for document currency select and base currency display
    try:
        company = _get_company_for_request(request)
        from currencies.models import Currency
        company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
        base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
        company_base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
        company_base_currency_code = (base_currency.code or '').strip() if base_currency else ''
    except Exception:
        company_currencies = []
        company_base_currency_symbol = '₹'
        company_base_currency_code = ''
        base_currency = None

    context = {
        'invoice_form': invoice_form,
        'sales_formset': sales_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': invoice.inv_number,
        'invoice': invoice,
        'readonly': readonly,
        # Shipping #by adarshaddress fields for prefilling
        'shipping_attention': shipping_source.shipping_attention or '',
        'shipping_email': shipping_source.shipping_email or '',
        'shipping_phone': shipping_source.shipping_phone or '',
        'shipping_country': shipping_source.shipping_country or '',
        'shipping_address1': shipping_source.shipping_address1 or '',
        'shipping_address2': shipping_source.shipping_address2 or '',
        'shipping_city': shipping_source.shipping_city or '',
        'shipping_state': shipping_source.shipping_state or '',
        'shipping_postal_code': shipping_source.shipping_postal_code or '',#by adarsh
        'company_country': company_country,
        'company_is_india': company_is_india,
        'company_tax_type': _get_request_company_tax_type(request, _get_company_for_request(request)),
        'company_currencies': company_currencies,
        'company_base_currency_symbol': company_base_currency_symbol,
        'company_base_currency_code': company_base_currency_code,
        'base_currency': base_currency,
        'selected_currency_id': invoice.document_currency_id or '',
        'fx_rate_to_base': invoice.fx_rate_to_base,
        'fx_rate_date': invoice.fx_rate_date,
        'tds_tcs_type': invoice.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': invoice.tds_tcs_definition_id or '',
        'tds_tcs_rate': invoice.tds_tcs_rate or 0,
        'tds_tcs_amount': invoice.tds_tcs_amount or 0,
        # — Fetch TDS and TCS for invoice_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=_get_company_for_request(request), is_active=True),
        'sales_rounding_method': getattr(_get_company_for_request(request), 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(_get_company_for_request(request), 'sales_rounding_increment', 0),
    }
    if readonly:
        for form in sales_formset.forms:
            for field_name, field in form.fields.items():
                form.fields['product'].widget.attrs['disabled'] = True
                form.fields['prd_tax'].widget.attrs['disabled'] = True

                widget = field.widget
                if widget.__class__.__name__ in ['Select', 'SelectMultiple', 'CheckboxInput', 'RadioSelect']:
                    widget.attrs['disabled'] = True  # disable selects and similar widgets
                else:
                    widget.attrs['readonly'] = True
        # Render a dedicated readonly template without edit actions/buttons
        return render(request, 'sales/quotation_view.html', context)
    else:
        # Render the editable template
        return render(request, 'sales/invoice_duplicate.html', context)


# def invoice_duplicate(request, pk):
#     original_invoice = get_object_or_404(SalesInvoice, pk=pk)
#     original_invoice_number = original_invoice.inv_number
#     # #print("original_quote_number:", original_quote_number)
#     if request.method == "POST":
#         post_data = request.POST.copy()  # make mutable copy
#         prd_brcd_map = {}

#         # Fix product IDs: if form-0-product contains 'id_barcode', keep only id part
#         for key in post_data:
#             # Identify product field keys
#             if key.startswith("form-") and key.endswith("-product"):
#                 value = post_data[key]
#                 # #print(f"Key matched: {key} with value: '{value}'")
#                 if value:
#                     parts = value.split("_", 1)
#                     post_data[key] = parts[0]  # Save only item id for product field
#                     # #print("Updated post_data[key]:", post_data[key])
#                     if len(parts) > 1:
#                         # Map barcode corresponding to this form prefix
#                         prefix = key.rsplit("-", 1)[0]  # e.g. 'form-0'
#                         prd_brcd_map[prefix] = parts[1]
#                         # #print("prd_brcd_map[prefix]:",prd_brcd_map[prefix])


#         total_amount = request.POST.get('grandTotal')
#         customer_id = request.POST.get('customer')
#         date = request.POST.get('date')
#         sales_person_id = request.POST.get('sales_person')
#         notes = request.POST.get('notes', '')
#         # quote_number = generate_quote_number()
#         inv_number = generate_revised_invoice_number(original_invoice_number)
#         # #print("quote_number:",quote_number)
    #         try:
    #             discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
    #             discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    #         except (InvalidOperation, ValueError, TypeError):
    #             discount_value = Decimal('0.00')
    #         discount_type = request.POST.get('discount_type', 'percent')

#         if not customer_id or not date:
#             messages.error(request, "Customer and Invoice Date are required.")
#             return redirect('sales_inv_list')

#         try:
#             customer = Customer.objects.get(pk=customer_id)
#             sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None

#             invoice = SalesInvoice.objects.create(
                # customer=customer,
                # date=date,
                # sales_person=sales_person,
                # inv_number=inv_number,
                # total_amount=total_amount,
                # notes=notes,
                # discount_value=discount_value,
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
#             )
#         except IntegrityError as e:
#             if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower():
#                 messages.error(request, f"Invoice Number '{inv_number}' already exists. Please choose a different one.")
#             else:
#                 messages.error(request, "An error occurred while saving the invoice.")
#             return redirect('sales_inv_list')

#         SalesInvoiceItemFormSet = modelformset_factory(
#             SalesInvoiceItem, form=SalesInvoiceItemForm, extra=0, can_delete=True
#         )

#         formset = SalesInvoiceItemFormSet(post_data, queryset=SalesInvoiceItem.objects.none())

#         if formset.is_valid():
#             items = formset.save(commit=False)
#             for index, item in enumerate(items):
#                 #added for edit save
#                 item.pk = None
                
#                 prefix = f"form-{index}"              # formset form key pattern
#                 if prefix in prd_brcd_map:            # check if barcode was extracted
#                     item.prd_brcd = prd_brcd_map[prefix]
                
#                 tax_group_id = post_data.get(f'form-{index}-prd_tax', '').strip()
#                 if tax_group_id:
#                     tax_group = TaxGroup.objects.filter(id=tax_group_id).prefetch_related('taxes').first()
#                     if tax_group:
#                         total_rate = Decimal(sum(t.rate for t in tax_group.taxes.all()))
#                         item.prd_tax = total_rate
#                         item.prd_taxgroup = tax_group.group_name
#                     else:
#                         item.prd_tax = Decimal(0)
#                         item.prd_taxgroup = None
#                 else:
#                     item.prd_tax = Decimal(0)
#                     item.prd_taxgroup = None
#                 item.sales_inv = invoice          # set foreign key
#                 item.save()                
#             for deleted_item in formset.deleted_objects:
#                 deleted_item.delete()

#             messages.success(request, "Sales Invoice created successfully!")
#             return redirect('sales_inv_list')
#         else:
#             # #print("Formset errors:", formset.errors)
#             # for form in formset:
#             #     #print("Individual form errors:", form.errors)
#             messages.error(request, "There are errors with the items in the invoice.")
#             return redirect('sales_inv_list')
#     else:
#         return redirect('sales_inv_list')


@transaction.atomic
def invoice_duplicate(request, pk):
    original_invoice = get_object_or_404(SalesInvoice, pk=pk)
    original_invoice_number = original_invoice.inv_number
    original_payment_status = original_invoice.payment_status
    original_allocations = list(
        InvPaymentAllocation.objects.filter(inv=original_invoice)
        .values('payment_id', 'amount', 'payment_made_on')
    )

    if request.method != "POST":
        return redirect_with_company('sales_inv_list')

    # — CHECK PERIOD LOCK BEFORE DUPLICATING/EDITING INVOICE
    from datetime import datetime
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer
    
    db = getattr(request, 'company_db', 'default')
    invoice_date = request.POST.get('date')
    if invoice_date:
        try:
            period_date = datetime.strptime(invoice_date, '%Y-%m-%d').date() if isinstance(invoice_date, str) else invoice_date
            PeriodLockEnforcer.check_can_edit(period_date, request.user, db=db, transaction_type='invoice')
        except PermissionDenied as e:
            error_msg = f"₹ Cannot save invoice: {str(e)}"
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect_with_company('sales_inv_list')
    # —

    # ---------- PERMISSION ----------
    if not (request.user.is_superuser or can_create_orders(request.user)):
        #Added by neha on 16-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'You do not have permission to create Orders.'
            }, status=403)
        messages.error(request, "You do not have permission to create Orders.")
        return redirect_with_company('sales_inv_list')

    # ---------- DELETE OLD invoice (FREE UNIQUE NUMBER) ----------
    try:
        SalesInvoiceItem.objects.filter(sales_inv=original_invoice).delete()
        original_invoice.delete()
    except Exception as e:
        logger.exception("Error deleting old invoice: %s", e)
        #Added by neha on 16-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Unable to replace the existing invoice.'
            }, status=400)
        messages.error(request, "Unable to replace the existing invoice.")
        return redirect_with_company('sales_inv_list')

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
    company_country = _get_current_company_country(request)
    company_is_india = _is_indian_company_country(company_country)
    post_data = _normalize_item_tax_tokens(post_data, company_is_india)

    # ---------- HEADER ----------
    customer_id = post_data.get('customer')
    date = post_data.get('date')

    if not customer_id or not date:
        #Added by neha on 16-2-26 for ajax request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Customer and Date are required.'
            }, status=400)
        messages.error(request, "Customer and Date are required.")
        return redirect_with_company('sales_inv_list')

    customer = get_object_or_404(Customer, pk=customer_id)

    try:
        discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
        discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        discount_value = Decimal('0.00')
    discount_type = request.POST.get('discount_type', 'percent')
    sales_person_id = post_data.get('sales_person')
    sales_person = None
    if sales_person_id:
        sales_person = SalesPerson.objects.filter(pk=sales_person_id).first()


    # ---------- CREATE NEW invoice ----------
    invoice = SalesInvoice.objects.create(
                customer=customer,
                date=date,
                sales_person=sales_person,
                inv_number = original_invoice_number,
                payment_status=original_payment_status,
                notes=post_data.get('notes', ''),
                total_amount=0,
                fx_rate_to_base=Decimal('1.000000'),
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
                tds_tcs_type=request.POST.get('tds_tcs_type', original_invoice.tds_tcs_type or 'tds'),
                tds_tcs_definition_id=_parse_positive_int(request.POST.get('tds_tcs_definition_id')),
                tds_tcs_rate=_parse_decimal(request.POST.get('tds_tcs_rate', '0')),
                tds_tcs_amount=_parse_decimal(request.POST.get('tds_tcs_amount', '0.00')),
    )

    try:
        company = _get_company_for_request(request)
        from currencies.services import apply_sales_invoice_fx, parse_currency_fx_from_post

        doc_cur, rate_override, fx_date = parse_currency_fx_from_post(post_data, company)
        apply_sales_invoice_fx(
            invoice,
            company,
            document_currency=doc_cur,
            fx_rate_to_base_override=rate_override,
            fx_rate_date_override=fx_date,
        )
    except Exception:
        logger.exception('Failed to apply FX to invoice %s', getattr(invoice, 'pk', None))

    pay_term_id = post_data.get('payment_term')
    if pay_term_id:
        invoice.payment_term = PayTerms.objects.filter(pk=pay_term_id).first()
        invoice.save()


    # ---------- ITEMS ----------
    SalesInvoiceItemFormSet = modelformset_factory(
        SalesInvoiceItem,
        form=SalesInvoiceItemForm,
        extra=0,
        can_delete=True
    )

    formset = SalesInvoiceItemFormSet(
        post_data,
        queryset=SalesInvoiceItem.objects.none()
    )

    if not formset.is_valid():
        logger.error("Formset errors: %s", formset.errors)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'error': 'Item validation failed.',
                'form_errors': formset.errors
            }, status=400)
        messages.error(request, "Item validation failed.")
        raise Exception("Invalid formset")  # rollback atomic

    calculated_total = Decimal('0.00')
    company = _get_company_for_tax()
    InvoiceTax.objects.filter(invoice=invoice).delete()

    for index, item in enumerate(formset.save(commit=False)):
        prefix = f"form-{index}"
        item.pk = None
        item.sales_inv = invoice
        item.prd_brcd = prd_brcd_map.get(prefix)
        item.hsn_code = post_data.get(f'{prefix}-hsn_code', '')

        selected_tax = post_data.get(f'{prefix}-prd_tax', '').strip()
        item.prd_tax, item.prd_taxgroup = _resolve_selected_tax(selected_tax)

        item.save()

        # tax_group_id = post_data.get(f'{prefix}-prd_tax', '').strip()
        # if tax_group_id:
        #     tax_group = TaxGroup.objects.filter(id=tax_group_id).prefetch_related('taxes').first()
        #     if tax_group:
        #         item.prd_tax = sum(t.rate for t in tax_group.taxes.all())
        #         item.prd_taxgroup = tax_group.group_name
        #     else:
        #         item.prd_tax = 0
        #         item.prd_taxgroup = None
        # else:
        #     item.prd_tax = 0
        #     item.prd_taxgroup = None

        # item.save()

        qty = Decimal(item.quantity)
        price = Decimal(item.price)
        base = qty * price

        disc = Decimal(item.prd_disvalue or 0)
        discounted = (
            base - (base * disc / 100)
            if item.prd_distype == 'percent'
            else base - disc
        )

        discounted = max(discounted, Decimal('0'))
        per_unit = (discounted / qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if qty else Decimal('0.00')
        # tax_amt = discounted * Decimal(item.prd_tax or 0) / 100

        tax_override = None
        if selected_tax.startswith('group:'):
            tax_override = TaxGroup.objects.filter(
                id=selected_tax.split(':', 1)[1]
            ).prefetch_related('taxes').first()
        elif item.prd_taxgroup:
            tax_override = TaxGroup.objects.filter(group_name=item.prd_taxgroup).prefetch_related('taxes').first()
        from taxation.services.tax_engine import calculate_tax
        tax_result = calculate_tax(
            item.product,
            invoice.customer,
            company,
            quantity=int(item.quantity or 0),
            unit_price=per_unit,
            tax_override=tax_override,
            tax_inclusive=getattr(item.product, 'taxincld_slprice', False),
            raise_on_missing=False,
        )

        if not item.prd_taxgroup and tax_result.get('total_rate'):
            item.prd_tax = tax_result['total_rate']
            item.prd_taxgroup = tax_result.get('tax_name') or item.prd_taxgroup
        # Only save fields that actually exist in the model
        item.save(update_fields=['prd_tax', 'prd_taxgroup'])

        for comp in tax_result.get('breakdown', []):
            InvoiceTax.objects.create(
                invoice=invoice,
                tax_name=comp.get('name') or '',
                tax_rate=comp.get('rate') or Decimal('0.00'),
                tax_amount=comp.get('amount') or Decimal('0.00'),
            )

        tax_amt = tax_result.get('total_tax', Decimal('0.00'))
        calculated_total += discounted + tax_amt

    # ---------- GRAND DISCOUNT ----------
    gd_val = Decimal(discount_value)
    grand_discount = (
        calculated_total * gd_val / 100
        if discount_type == 'percent'
        else gd_val
    )

    invoice.total_amount = calculated_total - min(grand_discount, calculated_total)
    from currencies.services import refresh_document_total_base
    refresh_document_total_base(invoice)
    invoice.save()
    
    # Restore invoice allocations that were deleted with the old invoice.
    for allocation in original_allocations:
        payment_id = allocation.get('payment_id')
        if not payment_id:
            continue
        if InvPayment.objects.filter(pk=payment_id).exists():
            InvPaymentAllocation.objects.create(
                payment_id=payment_id,
                inv=invoice,
                amount=allocation.get('amount') or Decimal('0.00'),
                payment_made_on=allocation.get('payment_made_on')
            )
    update_invoice_payment_status(invoice)
    messages.success(request, "Invoice edited successfully.")
    #Added by neha on 16-2-26 for ajax request
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'invoice_id': invoice.id,
            'invoice_number': invoice.inv_number,
            'customer_name': (
                invoice.customer.company_name
                if invoice.customer and invoice.customer.customer_type == 'company'
                else (
                    f"{invoice.customer.first_name} "
                    f"{invoice.customer.last_name or ''}"
                ).strip()
                if invoice.customer
                else ''
            ),
            'total_amount': str(invoice.total_amount),
            'message': 'Invoice edited successfully!'
        })

    return redirect_with_company('sales_inv_list')

def generate_revised_invoice_number(original_invoice_number):
    # Extract base quote number without revision suffix (e.g. "SQN001" from "SQN001-R2")
    base_invoice = re.sub(r'-R\d+$', '', original_invoice_number)

    # Find all quotes with this base plus revision suffix, e.g. "SQN001-R1", "SQN001-R2"
    existing_revisions = SalesInvoice.objects.filter(
        inv_number__startswith=base_invoice + '-R'
    ).order_by('-id')

    # Determine new revision number
    if existing_revisions.exists():
        last_invoice_number = existing_revisions.first().inv_number
        match = re.search(r'-R(\d+)$', last_invoice_number)
        last_rev_num = int(match.group(1)) if match else 0
        new_rev_num = last_rev_num + 1
    else:
        new_rev_num = 1

    # Create new quote number with revision suffix
    new_invoice_number = f"{base_invoice}-R{new_rev_num}"
    return new_invoice_number

@transaction.atomic
def duplicate_order(request, pk):
    logger.info("duplicate_order view HIT for pk=%s method=%s", pk, request.method)

    if request.method == 'POST':
        logger.info("POST data: %s", request.POST)
        
        # Get order_id from POST data
        order_id = request.POST.get('order_id')
        logger.info("Order ID from POST: %s", order_id)
        
        if not order_id:
            messages.error(request, "Order ID not provided.")
            return redirect_with_company('sales_order_list')
        
        # — FIX 1: Get original INSIDE POST block
        original = get_object_or_404(SalesOrder, pk=order_id)
        
        try:
            # Clone header
            new_order = original
            new_order.pk = None
            new_order.id = None
            new_order.order_number = generate_order_number()
            new_order.status = 'Draft'
            new_order.save()
            
            logger.info("New order created with PK: %s", new_order.pk)
            
            # Clone items - ₹ FIX 2: Use new_item.save()
            items = SalesOrderItem.objects.filter(sales_order=order_id)
            for item in items:
                new_item = item
                new_item.pk = None
                new_item.id = None
                new_item.sales_order = new_order
                new_item.save()  # — WAS: item.save()
            
            logger.info("Duplicated %d items", items.count())
            messages.success(request, f"Sales order duplicated! New order: {new_order.order_number}")
            # return JsonResponse({
            #     'success': True,
            #     'message': f"Sales order duplicated! New order: '{new_order.order_number}'.",
            # })
            
            # — FIX 3: Redirect to NEW order
            return redirect_with_company('order_detail', pk=new_order.pk)
            
        except Exception as e:
            logger.exception("Error duplicating order %s: %s", order_id, e)
            messages.error(request, "Failed to duplicate order.")
            return redirect_with_company('order_detail', pk=order_id)
    
    # GET request
    messages.warning(request, "Use Duplicate button from order detail.")
    return redirect_with_company('order_list')

@transaction.atomic
def duplicate_invoice(request, pk):
    logger.info("duplicate_invoice view HIT for pk=%s method=%s", pk, request.method)

    if request.method == 'POST':
        logger.info("POST data: %s", request.POST)
        
        # Get invoice_id from POST data
        invoice_id = request.POST.get('order_id')
        logger.info("Order ID from POST: %s", invoice_id)
        
        if not invoice_id:
            messages.error(request, "Invoice ID not provided.")
            return redirect_with_company('invoice_order_list')
        
        # — FIX 1: Get original INSIDE POST block
        original = get_object_or_404(SalesInvoice, pk=invoice_id)
        
        try:
            # Clone header
            new_invoice = original
            new_invoice.pk = None
            new_invoice.id = None
            new_invoice.inv_number = generate_inv_number()
            new_invoice.status = 'Draft'
            new_invoice.save()
            
            logger.info("New invoice created with PK: %s", new_invoice.pk)
            
            # Clone items - ₹ FIX 2: Use new_item.save()
            items = SalesInvoiceItem.objects.filter(sales_inv=invoice_id)
            for item in items:
                new_item = item
                new_item.pk = None
                new_item.id = None
                new_item.sales_inv = new_invoice
                new_item.save()  # — WAS: item.save()
            
            logger.info("Duplicated %d items", items.count())
            messages.success(request, f"Invoice duplicated! New invoice: {new_invoice.inv_number}")
            # return JsonResponse({
            #     'success': True,
            #     'message': f"Sales order duplicated! New order: '{new_order.order_number}'.",
            # })
            
            # — FIX 3: Redirect to NEW order
            return redirect_with_company('invoice_detail', pk=new_invoice.pk)
            
        except Exception as e:
            logger.exception("Error duplicating order %s: %s", invoice_id, e)
            messages.error(request, "Failed to duplicate order.")
            return redirect_with_company('order_detail', pk=invoice_id)
    
    # GET request
    messages.warning(request, "Use Duplicate button from order detail.")
    return redirect_with_company('order_list')


def invoice_journal(request, pk):
    """Display journal entries for a sales invoice"""
    invoice = get_object_or_404(SalesInvoice, pk=pk)
    
    # Fetch the actual journal entry for this invoice
    # The journal entry should have been created when the invoice was saved
    journal_entries = JournalEntry.objects.filter(
        reference__icontains=invoice.inv_number
    ).first()
    
    display_rows = []
    total_debit = Decimal('0.00')
    total_credit = Decimal('0.00')
    
    if journal_entries:
        # Get all lines for this journal entry
        lines = journal_entries.lines.all()
        
        for line in lines:
            debit = Decimal(str(line.debit or 0))
            credit = Decimal(str(line.credit or 0))
            
            total_debit += debit
            total_credit += credit
            
            display_rows.append({
                'account_name': line.account.name if line.account else 'Unknown',
                'debit': debit,
                'credit': credit
            })
    
    from currencies.services import get_base_currency
    company = _get_company_for_request(request)
    try:
        base_currency = get_base_currency(company)
    except Exception:
        base_currency = None

    return render(request, 'sales/invoice_journal.html', {
        'invoice': invoice,
        'display_rows': display_rows,
        'display_total': total_debit,
        'base_currency': base_currency,
    })


#updation by neha on 17-1-25
def payment_recieved_view(request, pk):
    """
    Invoice record-payment entrypoint.
    Reuse the same add_payment_received flow/template used elsewhere so
    send-email behavior and toasts are consistent.
    """
    return add_payment_received(request, pk)


@login_required
@transaction.atomic
def receive_payment(request, pk):
    """
    Record payment for a specific bill with automatic advance management.
    This function delegates to save_payment which handles all the advance logic.
    """
    if request.method == 'POST':
        # Delegate to save_payment which handles advance automatically
        return save_payment_received(request, pk)
    
    # GET request - show the form (handled by record_payment_view)
    return payment_recieved_view(request, pk)

# @login_required
# @transaction.atomic
# def receive_payment(request, pk):
#     """Record payment for a inv"""
#     inv = get_object_or_404(SalesInvoice, pk=pk)
    
#     # Calculate final total (your existing logic)
#     subtotal_calc = Decimal('0.00')
#     total_tax = Decimal('0.00')
    
#     for item in SalesInvoiceItem.objects.filter(sales_inv=inv):
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
#     grand_discount_value = Decimal(str(inv.discount_value or 0))
#     grand_discount_type = inv.discount_type or 'percent'
#     if grand_discount_type == 'percent':
#         grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
#     else:
#         grand_discount = grand_discount_value
#     if grand_discount > subtotal_calc:
#         grand_discount = subtotal_calc
    
#     final_total = subtotal_calc - grand_discount
    
#     # Calculate remaining amount to pay
#     total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
#         total=Sum('amount')
#     )['total'] or Decimal('0.00')
#     remaining_amount = inv.total_amount - total_paid
    
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
    
#     # payment_modes = PaymentMode.objects.all().order_by('name')
    
#     # Generate next payment number
#     next_payment_number = InvPayment.generate_payment_number()
    
#     # Handle POST request - Save payment
#     if request.method == 'POST':
#         print(f"DEBUG FILES: {request.FILES}")
#         print(f"DEBUG FILES.getlist('attachments'): {request.FILES.getlist('attachments')}")
#         try:
#             with transaction.atomic():
#                 # Extract form data
#                 amount = request.POST.get('amount')
#                 # payment_mode_id = request.POST.get('payment_mode')
#                 payment_date = request.POST.get('payment_date')
#                 payment_number = request.POST.get('payment_number')
#                 payment_made_on = request.POST.get('payment_made_on')
#                 paid_through_id = request.POST.get('paid_through')
#                 reference = request.POST.get('reference', '')
#                 notes = request.POST.get('notes', '')
#                 send_email = request.POST.get('send_email') == '1'
#                 # Get customer_id from form or automatically from inv
#                 customer_id = request.POST.get('customer_id') or inv.customer_id
                
#                 print(f"DEBUG receive_payment: amount={amount}, payment_number={payment_number}, inv={inv.id}")
                
#                 # Validate required fields
#                 if not all([amount, payment_date, payment_number, paid_through_id]):
#                     messages.error(request, 'Please fill in all required fields.')
#                     return render(request, 'sales/receive_payment.html', {
#                         'inv': inv,
#                         'final_total': final_total,
#                         'remaining_amount': remaining_amount,
#                         'payment_accounts': payment_accounts,
#                         # 'payment_modes': payment_modes,
#                         'next_payment_number': next_payment_number,
#                     })
                
#                 # Get foreign key objects
#                 customer = get_object_or_404(Customer, pk=customer_id)
#                 # payment_mode = get_object_or_404(PaymentMode, pk=payment_mode_id)
#                 paid_through = get_object_or_404(ChartOfAccounts, pk=paid_through_id, active=1)
                
#                 # Validate amount
#                 amount_decimal = Decimal(amount)
#                 if amount_decimal <= 0:
#                     messages.error(request, 'Payment amount must be greater than zero.')
#                     return render(request, 'sales/receive_payment.html', {
#                         'inv': inv,
#                         'final_total': final_total,
#                         'remaining_amount': remaining_amount,
#                         'payment_accounts': payment_accounts,
#                         # 'payment_modes': payment_modes,
#                         'next_payment_number': next_payment_number,
#                     })
                
#                 # Allow overpayment (will be treated as advance)
#                 # No need to check if payment exceeds inv amount
                
#                 print(f"DEBUG: Creating InvPayment record...")
                
#                 # Create payment record (NO inv field!)
#                 payment = InvPayment.objects.create(
#                     customer=customer,
#                     # payment_mode=payment_mode,
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
                
#                 # Create allocation linking payment to inv
#                 allocation_amount = min(amount_decimal, remaining_amount)
                
#                 print(f"DEBUG: Creating allocation for ₹{allocation_amount}")
                
#                 allocation = InvPaymentAllocation.objects.create(
#                     payment=payment,
#                     inv=inv,
#                     amount=allocation_amount,
#                     payment_made_on=payment_made_on if payment_made_on else None
#                 )
                
#                 print(f"DEBUG: Allocation created with ID={allocation.id}")
                
#                 # Update inv status
#                 new_total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
#                     total=Sum('amount')
#                 )['total'] or Decimal('0.00')
                
#                 if new_total_paid >= inv.total_amount:
#                     inv.status = 'Paid'
#                 elif new_total_paid > 0:
#                     inv.status = 'Partial'
#                 else:
#                     inv.status = 'Open'
#                 inv.save()
                
#                 print(f"DEBUG: inv status updated to {inv.status}")
                
#                 # If payment exceeds inv amount, note it as advance
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
#                         InvPaymentAttachment.objects.create(
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
#                         f'inv Payment: ₹{allocation_amount:.2f}, Advance: ₹{advance_amount:.2f}'
#                     )
#                 else:
#                     messages.success(
#                         request, 
#                         f'Payment #{payment.payment_number} of ₹{payment.amount} recorded successfully.'
#                     )
                
#                 print(f"SUCCESS: Transaction committed")
#                 return redirect('invoice_detail', pk=inv.id)  # or 'payment_recieved_list'
            
#         except Exception as e:
#             print(f"ERROR in receive_payment: {str(e)}")
#             import traceback
#             traceback.print_exc()
#             messages.error(request, f'Error recording payment: {str(e)}')
#             return render(request, 'sales/receive_payment.html', {
#                 'inv': inv,
#                 'final_total': final_total,
#                 'remaining_amount': remaining_amount,
#                 'payment_accounts': payment_accounts,
#                 # 'payment_modes': payment_modes,
#                 'next_payment_number': next_payment_number,
#             })
    
#     # GET request - Display form
#     context = {
#         'inv_number': inv.inv_number,
#         'inv': inv,
#         'final_total': final_total,
#         'remaining_amount': remaining_amount,
#         'payment_accounts': payment_accounts,
#         # 'payment_modes': payment_modes,
#         'next_payment_number': next_payment_number,
#     }
#     return render(request, 'sales/receive_payment.html', context)


def payment_received_list(request):
    # Permission: require View on Payments Received
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_payments_received(request.user)):
            messages.error(request, 'You do not have permission to view Payments Received.')
            return redirect_with_company('/')
    except Exception:
        messages.error(request, 'You do not have permission to view Payments Received.')
        return redirect_with_company('/')
    
    search_query = request.GET.get('q', '')
    payment_status_filter = request.GET.get('payment_status', '')
    payments = InvPayment.objects.all()

    if search_query:
        payments = payments.filter(
            Q(amount__icontains=search_query) |
            Q(customer__first_name__icontains=search_query) |
            Q(customer__company_name__icontains=search_query) |
            Q(payment_number__icontains=search_query) |
            Q(reference__icontains=search_query)
        )
    
    # Filter by payment status (partial or full)
    if payment_status_filter:
        all_payments = list(payments.order_by('-payment_date', '-id'))
        
        if payment_status_filter == 'full':
            # Get payments where all allocated invoices are FULLY PAID
            all_payments = [p for p in all_payments 
                           if p.inv_allocations.exists() and 
                           all(alloc.inv.payment_status and alloc.inv.payment_status.name == 'Paid' for alloc in p.inv_allocations.all())]
        elif payment_status_filter == 'partial':
            # Get payments where at least one allocated invoice is NOT fully paid
            all_payments = [p for p in all_payments 
                           if p.inv_allocations.exists() and 
                           any(not alloc.inv.payment_status or alloc.inv.payment_status.name != 'Paid' for alloc in p.inv_allocations.all())]
        
        payments = all_payments
    else:
        payments = payments.order_by('-payment_date', '-id')

    paginator = Paginator(payments, 10)
    page_number = request.GET.get('page')
    payment_page = paginator.get_page(page_number)

    return render(request, "sales/payment_received_list.html", {
        "payments": payment_page,
        "search_query": search_query,
        "payment_status_filter": payment_status_filter,
    })


def add_payment_received(request, pk=None):
    """
    Render payment recording page with advance balance information.
    """
    if request.method == 'POST':
        return save_payment_received(request, pk)
    
    # Load payment accounts and modes updt  by neha on 18-2-26
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

    # Load payment modes dynamically
    # payment_modes = PaymentMode.objects.all().order_by('name')
    # Get children grouped by parent header
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
    next_payment_number = InvPayment.generate_payment_number()
    
    # Case 1: New payment - show customer selection
    if pk is None:
        customers = Customer.objects.filter(is_active=1, is_draft=False).order_by('id')
        
        context = {
            'customers': customers,
            'payment_accounts': payment_accounts,
            'payment_account_header_ids': payment_account_header_ids,
            'payment_accounts_grouped': payment_accounts_grouped,
            # 'payment_modes': payment_modes,
            'next_payment_number': next_payment_number,
            'is_new_payment': True,
        }
        return render(request, 'sales/add_payment_received.html', context)
    
    # Case 2: Payment for specific invoice
    inv = get_object_or_404(SalesInvoice, pk=pk)
    
    # Calculate remaining amount
    total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')
    final_total = inv.total_amount - total_paid
    
    # Get customer's advance balance
    advance_balance = get_customer_advance_balance(inv.customer.id)
    
    # Calculate net amount to pay
    advance_to_use = min(advance_balance, final_total)
    net_amount = max(final_total - advance_balance, Decimal('0.00'))
    remaining_advance = advance_balance - advance_to_use
    
    context = {
        'inv_number': inv.inv_number,
        'final_total': final_total,
        'inv': inv,
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
        'currency_symbol': inv.get_currency_symbol(),
    }
    return render(request, 'sales/add_payment_received.html', context)

#by adarsh payment journal entry updation on 17-1-25
def _get_payment_journal_base_amount(payment):
    """
    Convert the cash receipt to company base currency using the invoice FX
    values already saved on the allocated invoices.
    """
    payment_amount = Decimal(str(payment.amount or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    allocations = list(payment.inv_allocations.select_related('inv').all())
    if not allocations:
        return payment_amount

    total_allocated = sum(Decimal(str(allocation.amount or 0)) for allocation in allocations)
    if total_allocated <= 0:
        return payment_amount

    advance_added = sum(
        Decimal(str(txn.amount or 0))
        for txn in payment.advance_transactions.filter(amount__gt=0)
    )
    cash_applied_to_invoices = payment_amount - advance_added
    if cash_applied_to_invoices <= 0:
        cash_applied_to_invoices = min(payment_amount, total_allocated)

    remaining_cash = cash_applied_to_invoices
    journal_amount = Decimal('0.00')

    for index, allocation in enumerate(allocations, start=1):
        allocation_amount = Decimal(str(allocation.amount or 0))

        if index == len(allocations):
            proportional_cash = remaining_cash
        else:
            proportional_cash = (
                cash_applied_to_invoices * allocation_amount / total_allocated
            ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            remaining_cash -= proportional_cash

        if proportional_cash <= 0:
            continue

        invoice = allocation.inv
        if getattr(invoice, 'total_amount_base', None):
            base_amount = scale_amount_for_journal(proportional_cash, invoice)
        else:
            base_amount = proportional_cash.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        journal_amount += base_amount

    if journal_amount <= 0:
        return payment_amount
    return journal_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def post_payment_journal_entry(payment, request_user):
    """
    Post journal entry for payment received.
    
    When a payment is received from a customer:
    - Debit: Bank/Cash Account (from paid_through)
    - Credit: Debtors/Accounts Receivable
    
    This reverses the original invoice entry that debited Debtors/AR.
    """
    try:
        print(f"\n{'='*80}")
        print(f"POSTING JOURNAL ENTRY FOR PAYMENT #{payment.payment_number}")
        print(f"{'='*80}")
        
        # Get customer name based on customer type
        if payment.customer.customer_type == 'company':
            customer_name = payment.customer.company_name or "Company"
        else:
            customer_name = f"{payment.customer.first_name or ''} {payment.customer.last_name or ''}".strip() or "Customer"
        
        # Generate next JV number
        last = JournalEntry.objects.order_by('-id').first()
        if last and last.entry_number and last.entry_number.startswith('JV-'):
            try:
                last_num = int(last.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        next_num = last_num + 1
        entry_number = f"JV-{str(next_num).zfill(5)}"
        
        # Create journal entry header
        journal = JournalEntry.objects.create(
            entry_number=entry_number,
            date=payment.payment_date,
            reference=f"Payment #{payment.payment_number}",
            narration=f"Payment received from {customer_name} - Payment #{payment.payment_number}",
            created_by=request_user,
            updated_by=request_user,
            status='posted'
        )
        
        journal_amount = _get_payment_journal_base_amount(payment)

        print(f"Created Journal Entry: {entry_number}")
        print(f"Customer: {customer_name}")
        print(f"Document Amount: {payment.amount}")
        print(f"Journal/Base Amount: {journal_amount}")
        print(f"Paid Through: {payment.paid_through.name}")
        
        # DEBIT: Bank/Cash/Payment Account (paid_through)
        debit_acct = payment.paid_through
        debit_amount = journal_amount
        
        JournalLine.objects.create(
            journal=journal,
            account=debit_acct,
            description=f"Payment #{payment.payment_number} - {customer_name}",
            debit=debit_amount,
            credit=Decimal('0.00'),
            sequence=10
        )
        
        print(f"\n₹ DEBIT: {debit_acct.name} - ₹{debit_amount:.2f}")
        
        # CREDIT: Debtors/Accounts Receivable
        debt_acct = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
        if not debt_acct:
            debt_acct = ChartOfAccounts.objects.filter(code='1020101').first()
        
        credit_amount = journal_amount
        
        JournalLine.objects.create(
            journal=journal,
            account=debt_acct,
            description=f"Payment #{payment.payment_number} - {customer_name}",
            debit=Decimal('0.00'),
            credit=credit_amount,
            sequence=20
        )
        
        print(f"₹ CREDIT: {debt_acct.name} - ₹{credit_amount:.2f}")
        
        # Calculate and update totals
        journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
        journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        
        # Check for imbalance and handle rounding
        diff = (Decimal(journal.total_debit) - Decimal(journal.total_credit)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        print(f"\nTotal Debit: ₹{journal.total_debit:.2f}")
        print(f"Total Credit: ₹{journal.total_credit:.2f}")
        print(f"Difference: ₹{abs(diff):.2f}")
        
        if diff != Decimal('0.00'):
            round_acct = ChartOfAccounts.objects.filter(name__icontains='Rounded Off').first()
            if not round_acct:
                round_acct = ChartOfAccounts.objects.filter(code='40217').first()
            
            if round_acct:
                if diff > 0:
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Payment #{payment.payment_number} - Rounded Off",
                        debit=Decimal('0.00'),
                        credit=diff,
                        sequence=30
                    )
                    print(f"₹ ROUNDING: {round_acct.name} (Credit) - ₹{diff:.2f}")
                else:
                    abs_diff = abs(diff)
                    JournalLine.objects.create(
                        journal=journal,
                        account=round_acct,
                        description=f"Payment #{payment.payment_number} - Rounded Off",
                        debit=abs_diff,
                        credit=Decimal('0.00'),
                        sequence=30
                    )
                    print(f"₹ ROUNDING: {round_acct.name} (Debit) - ₹{abs_diff:.2f}")
                
                # Recalculate totals
                journal.total_debit = sum(line.debit or 0 for line in journal.lines.all())
                journal.total_credit = sum(line.credit or 0 for line in journal.lines.all())
        
        journal.save()
        
        print(f"\n₹ JOURNAL ENTRY POSTED SUCCESSFULLY")
        print(f"Entry Number: {entry_number}")
        print(f"Final Total Debit: ₹{journal.total_debit:.2f}")
        print(f"Final Total Credit: ₹{journal.total_credit:.2f}")
        print(f"{'='*80}\n")
        
        return journal
        
    except Exception as e:
        print(f"\n₹ ERROR POSTING JOURNAL ENTRY: {str(e)}")
        import traceback
        traceback.print_exc()
        logger.exception(f"Failed to create journal entry for payment #{payment.payment_number}")
        # Don't raise - allow payment to be saved even if journaling fails
        return None
        #by adarsh payment

def send_payment_received_email(payment):
    """
    Send a payment received notification email to the customer.
    Returns (success: bool, message: str).
    """
    try:
        customer = payment.customer
        recipient = (getattr(customer, 'email', '') or '').strip()
        if not recipient:
            # Fallback for invoice-linked payments where shipping email may exist on invoice.
            first_alloc = payment.inv_allocations.select_related('inv').first()
            if first_alloc and first_alloc.inv:
                recipient = (getattr(first_alloc.inv, 'shipping_email', '') or '').strip()
        if not recipient:
            return False, "Customer email not found."

        # Build placeholder context for replacement in templates
        try:
            from company.models import Company
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = get_company_logo_base64(company_obj)
        except Exception:
            company_name = "Company"
            company_logo = ""

        customer_name = (
            getattr(customer, 'company_name', '') or
            f"{getattr(customer, 'first_name', '')} {getattr(customer, 'last_name', '')}".strip() or
            "Customer"
        )

        placeholder_ctx = {
            'customer': customer_name,
            'payment_number': getattr(payment, 'payment_number', ''),
            'date': getattr(payment, 'payment_date', '') if getattr(payment, 'payment_date', None) else '',
            'amount': str(getattr(payment, 'amount', '')),
            'company': company_name,
            'logo': company_logo,
        }

        # Try to use EmailTemplateStyle if available (reuses the invoice pattern)
        try:
            from email_templates.models import EmailTemplateStyle
            template_style = None
            try_names = ["Payment Received", "Invoice", "Bill", "Quotation"]
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
                subject = replace_placeholders(template_style.subject or "Payment Received", placeholder_ctx)
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
                # Attach invoice PDFs for all allocated invoices
                try:
                    for alloc in payment.inv_allocations.select_related('inv').all():
                        inv = alloc.inv
                        try:
                            pdf_bytes = generate_invoice_pdf_bytes(inv.pk)
                            if pdf_bytes:
                                inv_no = getattr(inv, 'inv_number', inv.pk)
                                msg.attach(f'invoice_{inv_no}.pdf', pdf_bytes, 'application/pdf')
                        except Exception:
                            logger.exception('Failed to generate invoice PDF for attachment (inv %s)', getattr(inv, 'pk', None))
                except Exception:
                    pass
                msg.send(fail_silently=False)
                return True, f"Email sent to {recipient} successfully."

        except Exception:
            # If email_templates isn't available or fails, fall through to a simple plain/html send below
            pass

        # Try hardcoded default templates from email_templates if DB template not found
        try:
            from email_templates.views import get_default_email_template
            default_template = get_default_email_template("Payment Received")
            if default_template:
                subject = replace_placeholders(default_template.get("subject", "Payment Received"), placeholder_ctx)
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

                # Determine email configuration: prefer one matching 'Payment Received'
                email_config = EmailConfiguration.objects.filter(usage_types__icontains="Payment Received", status=True).first()
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
                    # Attach invoice PDFs for all allocated invoices (if any)
                    try:
                        for alloc in payment.inv_allocations.select_related('inv').all():
                            inv = alloc.inv
                            try:
                                pdf_bytes = generate_invoice_pdf_bytes(inv.pk)
                                if pdf_bytes:
                                    inv_no = getattr(inv, 'inv_number', inv.pk)
                                    msg.attach(f'invoice_{inv_no}.pdf', pdf_bytes, 'application/pdf')
                            except Exception:
                                logger.exception('Failed to generate invoice PDF for attachment (inv %s)', getattr(inv, 'pk', None))
                    except Exception:
                        pass
                    msg.send(fail_silently=False)
                    return True, f"Email sent to {recipient} successfully."
                except Exception as e:
                    traceback.print_exc()
                    return False, f"Email failed: {str(e)}"
        except Exception:
            pass

        # Fallback: simple built-in message if no template applied
        subject = "Payment Received"
        plain_body = (
            f"Dear {customer_name},\n\n"
            f"We have received your payment.\n"
            f"Payment Date: {payment.payment_date}\n"
            f"Amount: {payment.amount}\n\n"
            f"Thank you."
        )
        html_body = (
            f"<p>Dear {customer_name},</p>"
            f"<p>We have received your payment.</p>"
            f"<p><strong>Payment Date:</strong> {payment.payment_date}<br>"
            f"<strong>Amount:</strong> {payment.amount}</p>"
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
        # Attach invoice PDFs for allocated invoices
        try:
            for alloc in payment.inv_allocations.select_related('inv').all():
                inv = alloc.inv
                try:
                    pdf_bytes = generate_invoice_pdf_bytes(inv.pk)
                    if pdf_bytes:
                        inv_no = getattr(inv, 'inv_number', inv.pk)
                        msg.attach(f'invoice_{inv_no}.pdf', pdf_bytes, 'application/pdf')
                except Exception:
                    logger.exception('Failed to generate invoice PDF for attachment (inv %s)', getattr(inv, 'pk', None))
        except Exception:
            pass
        msg.send(fail_silently=False)
        return True, f"Email sent to {recipient} successfully."
    except Exception as e:
        logger.exception("Failed to send payment received email for payment #%s", getattr(payment, 'payment_number', ''))
        return False, f"Email failed: {str(e)}"

def save_payment_received(request, pk=None):
    """
    Handle payment submission with automatic advance management for customers.
    
    Scenarios:
    1. No invoices selected ₹ Pure advance payment
    2. No advance + Invoices selected ₹ Direct invoice payment (full or partial)
    3. Advance available + Invoices selected ₹ Use advance first, then cash
    4. Advance insufficient ₹ Use full advance + cash (full or partial payment)
    """
    try:
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        def _json_response(success, message, redirect_url=None, status=200, **extra):
            payload = {'success': success, 'message': message}
            if redirect_url:
                payload['redirect_url'] = redirect_url
            payload.update(extra)
            return JsonResponse(payload, status=status)

        # — CHECK PERIOD LOCK BEFORE CREATING PAYMENT RECEIVED
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
                    # Handle both: generic payment (pk=None) and invoice-specific payment (pk=invoice_id)
                    
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
                    next_payment_number = InvPayment.generate_payment_number()
                    
                    # Case 1: Generic payment (no specific invoice)
                    if pk is None:
                        customers = Customer.objects.filter(is_active=1, is_draft=False).order_by('id')
                        context = {
                            'customers': customers,
                            'payment_accounts': payment_accounts,
                            'payment_account_header_ids': payment_account_header_ids,
                            'payment_accounts_grouped': payment_accounts_grouped,
                            'next_payment_number': next_payment_number,
                            'is_new_payment': True,
                            'error_message': str(e),
                            'show_error_modal': True,
                        }
                        return render(request, 'sales/add_payment_received.html', context)
                    
                    # Case 2: Invoice-specific payment
                    inv = get_object_or_404(SalesInvoice, pk=pk)
                    subtotal_calc = Decimal('0.00')
                    total_tax = Decimal('0.00')
                    
                    for item in SalesInvoiceItem.objects.filter(sales_inv=inv):
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
                    
                    total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
                        total=Sum('amount')
                    )['total'] or Decimal('0.00')
                    
                    final_total = inv.total_amount - total_paid
                    
                    context = {
                        'inv_number': inv.inv_number,
                        'final_total': final_total,
                        'inv': inv,
                        'payment_accounts': payment_accounts,
                        'payment_account_header_ids': payment_account_header_ids,
                        'payment_accounts_grouped': payment_accounts_grouped,
                        'next_payment_number': next_payment_number,
                        'title': 'Receive Payment',
                        'error_message': str(e),
                        'show_error_modal': True,
                    }
                    return render(request, 'sales/receive_payment.html', context)
        with transaction.atomic():
            print("\n" + "="*80)
            print("CUSTOMER PAYMENT - FORM DATA RECEIVED:")
            
            # Get form data
            customer_id = request.POST.get('customer_id')
            payment_amount = Decimal(request.POST.get('amount', '0'))
            payment_date = request.POST.get('payment_date')
            payment_number = int(request.POST.get('payment_number'))
            paid_through_id = request.POST.get('paid_through')
            reference = request.POST.get('reference', '')
            notes = request.POST.get('notes', '')
            send_email = request.POST.get('send_email') == '1'
            
            print(f"  customer_id: {customer_id}")
            print(f"  amount: {payment_amount}")
            print(f"  payment_date: {payment_date}")
            print(f"  payment_number: {payment_number}")
            print(f"  paid_through_id: {paid_through_id}")
            
            # Get invoice IDs
            if pk:
                print(f"DEBUG: Single invoice payment mode - pk={pk}")
                inv_ids = [int(pk)]
            else:
                print("DEBUG: Multiple invoices payment mode")
                inv_ids_str = request.POST.get('inv_ids', '')
                print(f"DEBUG: inv_ids_str from POST: '{inv_ids_str}'")
                
                if inv_ids_str:
                    inv_ids = [int(iid) for iid in inv_ids_str.split(',') if iid.strip()]
                else:
                    inv_ids = []
            
            # Print all invoice-related POST data
            for key, value in request.POST.items():
                if key.startswith('inv_'):
                    print(f"  {key}: {value}")
            print("="*80 + "\n")
            
            print(f"DEBUG: Payment Amount={payment_amount}, Invoice IDs={inv_ids}")
            
            # Validate required fields
            if customer_id is None or not payment_date or payment_number is None or paid_through_id is None:
                msg = 'Please fill all required fields!.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                print("customer_id:",customer_id,"payment_date",payment_date,"payment_number",payment_number,"paid_through_id",paid_through_id)
                return redirect_with_company('add_payment_received') if not pk else redirect_with_company('receive_payment', pk=pk)
            
            # Validate payment amount
            if payment_amount < 0:
                msg = 'Payment amount cannot be negative.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                return redirect_with_company('add_payment_received') if not pk else redirect_with_company('receive_payment', pk=pk)
            
            # Get objects
            customer = get_object_or_404(Customer, id=customer_id)
            paid_through = get_object_or_404(ChartOfAccounts, id=paid_through_id)
            
            # Get current advance balance
            current_advance = get_customer_advance_balance(customer_id)
            print(f"DEBUG: Current Advance Balance = ₹{current_advance}")
            
            # Create payment record
            payment = InvPayment.objects.create(
                customer=customer,
                amount=payment_amount,
                payment_date=payment_date,
                payment_number=payment_number,
                paid_through=paid_through,
                reference=reference,
                notes=notes,
                send_email=send_email,
                created_by=request.user if request.user.is_authenticated else None,
            )
            
            print(f"DEBUG: Payment #{payment_number} created with ID={payment.id}")
            
            # Handle attachments
            attachments = request.FILES.getlist('attachments')
            for attachment in attachments:
                InvPaymentAttachment.objects.create(
                    payment=payment,
                    file=attachment
                )
                print(f"DEBUG: Attachment saved: {attachment.name}")
            
            # ========================================================================
            # POST #by adarsh paymentJOURNAL ENTRY FOR PAYMENT
            # ========================================================================
            # When payment is received:
            # - Debit: Bank/Cash Account (paid_through)
            # - Credit: Debtors/Accounts Receivable
            #by adarsh payment
            # ========================================================================
            # SCENARIO 1: No invoices selected ₹ Pure advance payment
            # ========================================================================
            if not inv_ids:
                print("\n" + "="*60)
                print("SCENARIO 1: No invoices selected - Pure advance payment")
                print("="*60)
                
                if payment_amount <= 0:
                    msg = 'Payment amount must be greater than 0 for advance payments.'
                    if is_ajax:
                        return _json_response(False, msg, status=400)
                    messages.error(request, msg)
                    payment.delete()
                    return redirect_with_company('add_payment_received')
                
                CustomerAdvancePayment.add_advance(customer, payment_amount, payment)
                print(f"DEBUG: Added advance payment of ₹{payment_amount}")

                print("\n" + "="*60)
                print("POSTING JOURNAL ENTRY FOR PAYMENT")
                print("="*60)
                journal_entry = post_payment_journal_entry(payment, request.user)
                if journal_entry:
                    print(f"₹ Journal Entry Posted: {journal_entry.entry_number}")
                else:
                    print("₹  Payment saved but journal entry posting encountered an error")
                
                # Use the payment's currency symbol when available (falls back to '₹')
                try:
                    symbol = payment.get_currency_symbol() if hasattr(payment, 'get_currency_symbol') else '₹'
                    if not symbol:
                        symbol = '₹'
                except Exception:
                    symbol = '₹'

                success_msg = f'Advance payment of {symbol}{payment_amount:.2f} recorded successfully! Payment #{payment_number}'
                email_msg = ''
                if send_email:
                    email_ok, email_msg = send_payment_received_email(payment)
                    if not email_ok:
                        raise Exception(email_msg)

                if is_ajax:
                    return _json_response(
                        True,
                        success_msg,
                        redirect_url=get_company_redirect_url(request, 'payment_received_list'),
                        email_sent=bool(send_email),
                        email_message=email_msg
                    )

                messages.success(request, success_msg)
                return redirect_with_company('payment_received_list')
            
            # ========================================================================
            # SCENARIO 2, 3, 4: Invoices selected - Process with or without advance
            # ========================================================================
            
            # Prepare invoices data
            invs_data = []
            total_invs_due = Decimal('0.00')
            
            for inv_id in inv_ids:
                inv = get_object_or_404(SalesInvoice, id=inv_id)
                
                # Calculate remaining amount for this invoice
                total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
                    total=Sum('amount')
                )['total'] or Decimal('0.00')
                inv_remaining = inv.total_amount - total_paid
                
                print(f"DEBUG: Invoice #{inv.inv_number} - Total: ₹{inv.total_amount}, Paid: ₹{total_paid}, Remaining: ₹{inv_remaining}")
                
                # Skip fully paid invoices
                if inv_remaining <= 0:
                    print(f"DEBUG: Skipping Invoice #{inv.inv_number} - Already fully paid")
                    continue
                
                invs_data.append({
                    'inv': inv,
                    'inv_id': inv_id,
                    'amount_to_pay': inv_remaining,
                    'inv_remaining': inv_remaining
                })
                total_invs_due += inv_remaining
            
            if not invs_data:
                msg = 'No valid invoices to process. All selected invoices are already paid.'
                if is_ajax:
                    return _json_response(False, msg, status=400)
                messages.error(request, msg)
                payment.delete()
                return redirect_with_company('add_payment_received') if not pk else redirect_with_company('receive_payment', pk=pk)
            
            print(f"\nDEBUG: Total Invoices Due = ₹{total_invs_due} (across {len(invs_data)} invoice(s))")
            
            # STEP 1: Calculate advance distribution for each invoice
            remaining_advance = current_advance
            total_advance_used = Decimal('0.00')
            
            for inv_data in invs_data:
                if remaining_advance <= 0:
                    inv_data['advance_used'] = Decimal('0.00')
                    inv_data['cash_needed'] = inv_data['amount_to_pay']
                else:
                    # Use advance for this invoice (up to invoice amount)
                    advance_for_this_inv = min(remaining_advance, inv_data['amount_to_pay'])
                    inv_data['advance_used'] = advance_for_this_inv
                    inv_data['cash_needed'] = inv_data['amount_to_pay'] - advance_for_this_inv
                    
                    remaining_advance -= advance_for_this_inv
                    total_advance_used += advance_for_this_inv
                
                print(f"DEBUG: Invoice #{inv_data['inv'].inv_number}")
                print(f"  - Amount to Pay: ₹{inv_data['amount_to_pay']}")
                print(f"  - Advance Used: ₹{inv_data['advance_used']}")
                print(f"  - Cash Needed: ₹{inv_data['cash_needed']}")
            
            # STEP 2: Determine allocation based on payment sufficiency
            total_cash_needed = sum(id['cash_needed'] for id in invs_data)
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
                    
                    for inv_data in invs_data:
                        inv_data['cash_allocated'] = inv_data['cash_needed'] * proportion
                        inv_data['total_allocated'] = inv_data['advance_used'] + inv_data['cash_allocated']
                        
                        print(f"\nInvoice #{inv_data['inv'].inv_number}:")
                        print(f"  - Advance Used: ₹{inv_data['advance_used']:.2f}")
                        print(f"  - Cash Allocated: ₹{inv_data['cash_allocated']:.2f}")
                        print(f"  - Total Allocated: ₹{inv_data['total_allocated']:.2f}")
                else:
                    # SCENARIO 2: No advance + insufficient cash
                    # Distribute cash proportionally across all invoices
                    print("Strategy: No advance - proportional cash distribution across invoices")
                    
                    proportion = payment_amount / total_invs_due if total_invs_due > 0 else Decimal('0')
                    print(f"Cash proportion: {proportion:.4f}")
                    
                    for inv_data in invs_data:
                        inv_data['cash_allocated'] = inv_data['amount_to_pay'] * proportion
                        inv_data['total_allocated'] = inv_data['cash_allocated']
                        inv_data['advance_used'] = Decimal('0.00')
                        
                        print(f"\nInvoice #{inv_data['inv'].inv_number}:")
                        print(f"  - Cash Allocated: ₹{inv_data['cash_allocated']:.2f}")
                        print(f"  - Total Allocated: ₹{inv_data['total_allocated']:.2f}")
                    
                    total_advance_used = Decimal('0.00')
                
                excess_cash = Decimal('0.00')
                
            else:
                # ================================================================
                # SUFFICIENT PAYMENT - Full payment scenario
                # ================================================================
                print("\n" + "="*60)
                print("SCENARIO: SUFFICIENT PAYMENT - Full Invoice Payment")
                print(f"Has Advance: {has_advance}")
                print("="*60)
                
                if has_advance:
                    # SCENARIO 3: With advance + sufficient cash
                    print("Strategy: Full advance + full cash for invoices + excess as new advance")
                else:
                    # SCENARIO 2: No advance + sufficient cash
                    print("Strategy: Full cash for invoices + excess as new advance")
                
                for inv_data in invs_data:
                    inv_data['cash_allocated'] = inv_data['cash_needed']
                    inv_data['total_allocated'] = inv_data['amount_to_pay']
                    
                    print(f"\nInvoice #{inv_data['inv'].inv_number}:")
                    if has_advance:
                        print(f"  - Advance Used: ₹{inv_data['advance_used']:.2f}")
                    print(f"  - Cash Allocated: ₹{inv_data['cash_allocated']:.2f}")
                    print(f"  - Total Allocated: ₹{inv_data['total_allocated']:.2f}")
                
                excess_cash = payment_amount - total_cash_needed
                print(f"\nExcess cash for new advance: ₹{excess_cash:.2f}")
            
            # STEP 3: Create invoice payment allocations
            print("\n" + "="*60)
            print("CREATING INVOICE PAYMENT ALLOCATIONS")
            print("="*60)
            
            allocations_created = 0
            for inv_data in invs_data:
                inv = inv_data['inv']
                total_allocation = inv_data['total_allocated']
                
                # Get payment made on date
                inv_payment_made_on = request.POST.get(f'inv_payment_made_on_{inv_data["inv_id"]}')
                
                print(f"\nInvoice #{inv.inv_number}:")
                print(f"  - Allocating: ₹{total_allocation:.2f}")
                print(f"  - Payment made on: {inv_payment_made_on}")
                
                # Create allocation
                allocation = InvPaymentAllocation.objects.create(
                    payment=payment,
                    inv=inv,
                    amount=total_allocation,
                    payment_made_on=inv_payment_made_on if inv_payment_made_on else None
                )
                allocations_created += 1
                print(f"  Allocation created: ID={allocation.id}")

                # Update invoice status from centralized payment-status logic
                old_status = inv.status
                update_invoice_payment_status(inv)
                new_total_paid = InvPaymentAllocation.objects.filter(inv=inv).aggregate(
                    total=Sum('amount')
                )['total'] or Decimal('0.00')

                print(f"  Status: {old_status} -> {inv.status} (Paid: {new_total_paid:.2f}/{inv.total_amount:.2f})")
                # If stock management is configured to happen on payment instead
                # of on delivery, update stock when the invoice is fully paid.
                try:
                    if not is_stock_management_on_delivery(request=request) and inv.payment_status_id == 3:
                        update_stock_from_paid_invoice(inv, request.user, request)
                except Exception:
                    logger.exception('Failed to update stock on payment for invoice %s', getattr(inv, 'inv_number', None))

                print(f"\nCreated {allocations_created} allocation(s)")
            
            # STEP 4: Record advance transactions
            print("\n" + "="*60)
            print("RECORDING ADVANCE TRANSACTIONS")
            print("="*60)
            
            if total_advance_used > 0:
                # Debit advance (used for invoices)
                advance_debit = CustomerAdvancePayment.use_advance(customer, total_advance_used, payment)
                if advance_debit:
                    print(f"₹ Used advance: -₹{total_advance_used:.2f}")
                else:
                    print(f"₹ Failed to use advance (insufficient balance)")
                    messages.error(request, 'Insufficient advance balance.')
                    raise Exception("Failed to use advance")
            
            # STEP 5: Handle excess cash (new advance)
            if excess_cash > 0:
                CustomerAdvancePayment.add_advance(customer, excess_cash, payment)
                print(f"₹ Added new advance: +₹{excess_cash:.2f}")

            print("\n" + "="*60)
            print("POSTING JOURNAL ENTRY FOR PAYMENT")
            print("="*60)
            journal_entry = post_payment_journal_entry(payment, request.user)
            if journal_entry:
                print(f"₹ Journal Entry Posted: {journal_entry.entry_number}")
            else:
                print("₹  Payment saved but journal entry posting encountered an error")
            
            # Get final balance
            final_balance = get_customer_advance_balance(customer_id)
            
            print("\n" + "="*80)
            print("CUSTOMER PAYMENT SUMMARY")
            print("="*80)
            print(f"Initial Advance Balance:  ₹{current_advance:.2f}")
            print(f"Advance Used for Invoices: -₹{total_advance_used:.2f}")
            print(f"Cash Payment Received:    ₹{payment_amount:.2f}")
            print(f"New Advance Added:        +₹{excess_cash:.2f}")
            print(f"Final Advance Balance:    ₹{final_balance:.2f}")
            print(f"Invoice Allocations:      {allocations_created}")
            print("="*80 + "\n")
            
            # Build success message
            # Use the payment's currency symbol when available (falls back to '₹')
            try:
                symbol = payment.get_currency_symbol() if hasattr(payment, 'get_currency_symbol') else '₹'
                if not symbol:
                    symbol = '₹'
            except Exception:
                symbol = '₹'

            msg_parts = [f'Payment #{payment_number} recorded successfully!']
            
            if total_advance_used > 0 and payment_amount > 0:
                msg_parts.append(f'Advance Used: {symbol}{total_advance_used:.2f} + Cash Received: {symbol}{payment_amount:.2f}')
            elif total_advance_used > 0:
                msg_parts.append(f'Paid using Advance: {symbol}{total_advance_used:.2f}')
            elif payment_amount > 0:
                msg_parts.append(f'Cash Received: {symbol}{payment_amount:.2f}')
            
            if excess_cash > 0:
                msg_parts.append(f'New Advance: {symbol}{excess_cash:.2f}')

            msg_parts.append(f'Current Advance Balance: {symbol}{final_balance:.2f}')
            
            email_msg = ''
            if send_email:
                email_ok, email_msg = send_payment_received_email(payment)
                if not email_ok:
                    raise Exception(email_msg)

            final_msg = ' | '.join(msg_parts)
            if is_ajax:
                redirect_url = (
                    get_company_redirect_url(request, 'invoice_detail', pk=pk)
                    if pk else
                    get_company_redirect_url(request, 'payment_received_list')
                )
                return _json_response(
                    True,
                    final_msg,
                    redirect_url=redirect_url,
                    email_sent=bool(send_email),
                    email_message=email_msg
                )

            messages.success(request, final_msg)
            return redirect_with_company('invoice_detail', pk=pk) if pk else redirect_with_company('payment_received_list')
                
    except Exception as e:
        import traceback
        traceback.print_exc()
        error_msg = f'Error recording payment: {str(e)}'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'message': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect_with_company('add_payment_received') if not pk else redirect_with_company('receive_payment', pk=pk)


def get_customer_advance_balance(customer_id):
    """Get customer's current advance balance."""
    balance = CustomerAdvancePayment.get_customer_advance_balance(customer_id)
    return balance


def get_customer_unpaid_invoices(request):
    """
    AJAX endpoint to get unpaid invoices for a customer with advance calculations.
    """
    customer_id = request.GET.get('customer_id')
    
    if not customer_id:
        return JsonResponse({'error': 'Customer ID is required'}, status=400)
    
    try:
        customer = Customer.objects.get(pk=customer_id, is_active=1)
        
        # Get unpaid invoices
        invs = SalesInvoice.objects.filter(
            customer=customer,
            payment_status_id__in=[1, 2]
        ).select_related('customer').order_by('-date')
        
        invs_data = []
        total_due = Decimal('0.00')
        
        for inv in invs:
            total_paid = InvPaymentAllocation.objects.filter(
                inv=inv
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            
            remaining = inv.total_amount - total_paid
            
            if remaining > 0:
                invs_data.append({
                    'id': inv.id,
                    'inv_number': inv.inv_number,
                    'date': inv.date.strftime('%Y-%m-%d'),
                    'date_formatted': inv.date.strftime('%d %b %Y'),
                    'total_amount': str(inv.total_amount),
                    'total_amount_float': float(inv.total_amount),
                    'paid_amount': str(total_paid),
                    'remaining_amount': str(remaining),
                    'remaining_amount_float': float(remaining),
                    'currency_symbol': inv.get_currency_symbol(),
                })
                
                total_due += remaining
        
        # Get customer's advance balance
        advance_balance = get_customer_advance_balance(customer_id)
        
        # Calculate how advance will be distributed across invoices
        remaining_advance = advance_balance
        invs_with_advance = []
        total_net_amount = Decimal('0.00')
        
        for inv in invs_data:
            inv_due = Decimal(str(inv['remaining_amount_float']))
            
            if remaining_advance > 0:
                advance_used_for_inv = min(remaining_advance, inv_due)
                net_amount_for_inv = inv_due - advance_used_for_inv
                remaining_advance -= advance_used_for_inv
            else:
                advance_used_for_inv = Decimal('0.00')
                net_amount_for_inv = inv_due
            
            invs_with_advance.append({
                **inv,
                'advance_used': str(advance_used_for_inv),
                'advance_used_float': float(advance_used_for_inv),
                'net_amount': str(net_amount_for_inv),
                'net_amount_float': float(net_amount_for_inv),
            })
            
            total_net_amount += net_amount_for_inv
        
        total_advance_will_use = advance_balance - remaining_advance
        
        return JsonResponse({
            'success': True,
            'invs': invs_with_advance,
            'total_invs': len(invs_with_advance),
            'total_due': str(total_due),
            'total_due_float': float(total_due),
            'advance_balance': str(advance_balance),
            'advance_balance_float': float(advance_balance),
            'has_advance': advance_balance > 0,
            'total_advance_will_use': str(total_advance_will_use),
            'total_advance_will_use_float': float(total_advance_will_use),
            'net_amount_to_pay': str(total_net_amount),
            'net_amount_to_pay_float': float(total_net_amount),
            'remaining_advance': str(remaining_advance),
            'remaining_advance_float': float(remaining_advance),
            'customer_currency_symbol': customer.get_currency_symbol(),
        })
    
    except Customer.DoesNotExist:
        return JsonResponse({'error': 'Customer not found or inactive'}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': f'An error occurred: {str(e)}'}, status=500)


def edit_payment_received(request, payment_id):
    """
    Edit an existing payment received.
    Pre-fills all payment details and allows modification.
    """
    payment = get_object_or_404(InvPayment, pk=payment_id)
    print("payment is:", payment)
    
    if request.method == 'POST':
        return update_payment_received(request, payment_id)
    
    # Load payment accounts and modes updt  by neha on 18-2-26
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

    # Load payment modes dynamically
    # payment_modes = PaymentMode.objects.all().order_by('name')
    # Get children grouped by parent header
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
    
    # Get all customers for the dropdown
    customers = Customer.objects.filter(is_active=True, is_draft=False).order_by('customer_code')
    
    # Get customer's current advance balance
    advance_balance = get_customer_advance_balance(payment.customer.id)
    
    # Get allocated invoices for this payment
    allocations = InvPaymentAllocation.objects.filter(payment=payment)
    
    # Debug: Print to console
    print(f"Payment ID from URL: {payment_id}")
    print(f"Payment object ID: {payment.id}")
    print(f"Payment number: {payment.payment_number}")
    print(f"Number of allocations found: {allocations.count()}")
    
    # Get allocated invoices for this payment
    allocated_invoices = []
    for allocation in allocations:
        inv = allocation.inv
        
        print(f"Processing invoice: {inv.inv_number}, Allocation amount: {allocation.amount}")
        
        # Calculate total paid for this invoice (excluding this payment to get the remaining before this payment)
        other_payments_total = InvPaymentAllocation.objects.filter(
            inv=inv
        ).exclude(
            payment=payment
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        
        # Calculate remaining amount (before this payment)
        remaining_before_payment = inv.total_amount - other_payments_total
        
        allocated_invoices.append({
            'id': inv.id,
            'inv_number': inv.inv_number,
            'date': inv.date.strftime('%Y-%m-%d'),
            'date_formatted': inv.date.strftime('%d %b %Y'),
            'total_amount': float(inv.total_amount),
            'remaining_amount': float(remaining_before_payment),
            'allocated_amount': float(allocation.amount),
            'payment_amount': float(allocation.amount),
            'payment_made_on': allocation.payment_made_on.strftime('%Y-%m-%d') if allocation.payment_made_on else '',
            'currency_symbol': inv.get_currency_symbol(),
        })
    
    print(f"Total allocated invoices to send to template: {len(allocated_invoices)}")
    print(f"Allocated invoices data: {allocated_invoices}")
    
    context = {
        'payment': payment,
        'payment_id': payment_id,
        'customers': customers,
        'payment_accounts': payment_accounts,
        'payment_account_header_ids': payment_account_header_ids,
        'payment_accounts_grouped': payment_accounts_grouped,
        'is_new_payment': True,  # Use new payment form style
        'is_edit_mode': True,  # Flag to indicate edit mode
        'advance_balance': advance_balance,
        'has_advance': advance_balance > 0,
        'allocated_invoices': allocated_invoices,
    }
    
    return render(request, 'sales/add_payment_received.html', context)


@transaction.atomic
def update_payment_received(request, payment_id):
    """
    Update an existing payment received with new details.
    Handles advance balance adjustments and invoice allocations.
    """
    payment = get_object_or_404(InvPayment, pk=payment_id)
    
    # — CHECK PERIOD LOCK BEFORE UPDATING PAYMENT RECEIVED
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
                # Build full context like edit_payment_received does
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
                customers = Customer.objects.filter(is_active=True, is_draft=False).order_by('customer_code')
                advance_balance = get_customer_advance_balance(payment.customer.id)
                
                allocations = InvPaymentAllocation.objects.filter(payment=payment)
                allocated_invoices = []
                for allocation in allocations:
                    inv = allocation.inv
                    other_payments_total = InvPaymentAllocation.objects.filter(
                        inv=inv
                    ).exclude(
                        payment=payment
                    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
                    
                    remaining_before_payment = inv.total_amount - other_payments_total
                    
                    allocated_invoices.append({
                        'id': inv.id,
                        'inv_number': inv.inv_number,
                        'date': inv.date.strftime('%Y-%m-%d'),
                        'date_formatted': inv.date.strftime('%d %b %Y'),
                        'total_amount': float(inv.total_amount),
                        'remaining_amount': float(remaining_before_payment),
                        'allocated_amount': float(allocation.amount),
                        'payment_amount': float(allocation.amount),
                        'payment_made_on': allocation.payment_made_on.strftime('%Y-%m-%d') if allocation.payment_made_on else '',
                    })
                
                context = {
                    'payment': payment,
                    'payment_id': payment_id,
                    'customers': customers,
                    'payment_accounts': payment_accounts,
                    'payment_account_header_ids': payment_account_header_ids,
                    'payment_accounts_grouped': payment_accounts_grouped,
                    'is_new_payment': True,
                    'is_edit_mode': True,
                    'advance_balance': advance_balance,
                    'has_advance': advance_balance > 0,
                    'allocated_invoices': allocated_invoices,
                    'error_message': str(e),
                    'show_error_modal': True,
                }
                return render(request, 'sales/add_payment_received.html', context)
    
    try:
        # Get form data
        customer_id = request.POST.get('customer_id')
        amount = Decimal(request.POST.get('amount', 0))
        payment_date = request.POST.get('payment_date')
        paid_through_id = request.POST.get('paid_through')
        reference = request.POST.get('reference', '').strip()
        notes = request.POST.get('notes', '').strip()
        if amount <= 0:
            messages.error(request, 'Payment amount must be greater than zero.')
            return redirect_with_company('edit_payment_received', payment_id=payment_id)
        
        # Get customer and paid through account
        customer = get_object_or_404(Customer, pk=customer_id, is_active=True)
        paid_through = get_object_or_404(ChartOfAccounts, pk=paid_through_id)
        send_email = request.POST.get('send_email') == '1'
        inv_ids = request.POST.get('inv_ids', '').strip()
        
        # Validate required fields
        if not customer_id or not payment_date or not paid_through_id:
            messages.error(request, 'Please fill all required fields.')
            return redirect_with_company('edit_payment_received', payment_id=payment_id)
        
        if amount <= 0:
            messages.error(request, 'Payment amount must be greater than zero.')
            return redirect_with_company('edit_payment_received', payment_id=payment_id)
        
        # Get customer and paid through account
        customer = get_object_or_404(Customer, pk=customer_id, is_active=True)
        paid_through = get_object_or_404(ChartOfAccounts, pk=paid_through_id)
        
        # Store old allocated invoices before deletion
        old_allocated_invoices = list(payment.inv_allocations.values_list('inv_id', flat=True))

        # Create reversal journal entries for the existing payment (before modifications)
        try:
            from django.utils import timezone
            # find the original journal entry for this payment
            orig_journal = JournalEntry.objects.filter(reference=f"Payment #{payment.payment_number}").order_by('-id').first()
            if orig_journal:
                # generate next JV
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
                    date=timezone.now().date() or orig_journal.date,
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
        old_advance_transactions = CustomerAdvancePayment.objects.filter(payment=payment)
        for txn in old_advance_transactions:
            # Create reverse entry
            CustomerAdvancePayment.objects.create(
                customer=customer,
                amount=-txn.amount,  # Reverse the transaction
                payment=None  # Not linked to any payment (adjustment)
            )
        # Delete old advance transactions
        old_advance_transactions.delete()
        
        # Delete old invoice allocations
        payment.inv_allocations.all().delete()
        
        # Update payment details
        payment.customer = customer
        payment.amount = amount
        payment.payment_date = payment_date
        payment.paid_through = paid_through
        payment.reference = reference
        payment.notes = notes
        payment.send_email = send_email
        payment.updated_by = request.user
        payment.save()
        
        # Get new selected invoice IDs
        new_selected_inv_ids = []
        if inv_ids:
            new_selected_inv_ids = [int(id.strip()) for id in inv_ids.split(',') if id.strip()]
        
        # Process invoice allocations
        if new_selected_inv_ids:
            # Get current advance balance
            current_advance = get_customer_advance_balance(customer.id)
            remaining_advance = current_advance
            
            # Get invoices and calculate distribution
            invoices = SalesInvoice.objects.filter(id__in=new_selected_inv_ids, customer=customer)
            
            total_invoice_amount = Decimal('0.00')
            invoice_allocations = []
            
            for inv in invoices:
                # Get custom amount from form
                inv_amount_key = f'inv_amount_{inv.id}'
                inv_payment_date_key = f'inv_payment_made_on_{inv.id}'
                
                inv_amount = Decimal(request.POST.get(inv_amount_key, 0))
                inv_payment_date = request.POST.get(inv_payment_date_key)
                
                if inv_amount > 0:
                    # Calculate advance usage for this invoice
                    advance_for_inv = min(remaining_advance, inv_amount)
                    remaining_advance -= advance_for_inv
                    
                    invoice_allocations.append({
                        'inv': inv,
                        'amount': inv_amount,
                        'payment_made_on': inv_payment_date,
                        'advance_used': advance_for_inv
                    })
                    total_invoice_amount += inv_amount
            
            # Validate payment amount covers invoices
            if amount < total_invoice_amount - current_advance:
                messages.error(
                    request,
                    f'Payment amount ₹{amount} is insufficient. Need at least ₹{total_invoice_amount - current_advance} after advance.'
                )
                raise ValueError('Insufficient payment amount')
            
            # Create allocations and use advance
            total_advance_used = Decimal('0.00')
            for allocation_data in invoice_allocations:
                InvPaymentAllocation.objects.create(
                    payment=payment,
                    inv=allocation_data['inv'],
                    amount=allocation_data['amount'],
                    payment_made_on=allocation_data['payment_made_on'] or payment_date
                )
                
                # Track advance usage
                if allocation_data['advance_used'] > 0:
                    total_advance_used += allocation_data['advance_used']
            
            # Record advance usage if any
            if total_advance_used > 0:
                CustomerAdvancePayment.use_advance(customer, total_advance_used, payment)
            
            # Calculate excess amount (if payment > invoices - advance used)
            allocated_cash = total_invoice_amount - total_advance_used
            excess = amount - allocated_cash
            
            if excess > 0:
                CustomerAdvancePayment.add_advance(customer, excess, payment)
            
            # Update invoice statuses for newly allocated invoices
            for allocation_data in invoice_allocations:
                update_invoice_payment_status(allocation_data['inv'])
        
        else:
            # Pure advance payment (no invoices selected)
            CustomerAdvancePayment.add_advance(customer, amount, payment)
        
        # Update status for invoices that were removed from this payment
        removed_inv_ids = set(old_allocated_invoices) - set(new_selected_inv_ids)
        if removed_inv_ids:
            removed_invoices = SalesInvoice.objects.filter(id__in=removed_inv_ids)
            for inv in removed_invoices:
                update_invoice_payment_status(inv)
        
        # Handle file attachments
        deleted_attachments = request.POST.get('deleted_attachments', '').strip()
        if deleted_attachments:
            deleted_ids = [int(id) for id in deleted_attachments.split(',') if id]
            InvPaymentAttachment.objects.filter(
                id__in=deleted_ids,
                payment=payment
            ).delete()
        
        # Add new attachments
        files = request.FILES.getlist('attachments')
        for file in files:
            InvPaymentAttachment.objects.create(
                payment=payment,
                file=file,
                uploaded_by=request.user
            )
        
        # Post updated journal entry for the modified payment
        try:
            journal_entry = post_payment_journal_entry(payment, request.user)
            if journal_entry:
                logger.info('Posted updated journal %s for payment %s', journal_entry.entry_number, payment.payment_number)
            else:
                logger.warning('Posting updated journal failed for payment %s', payment.payment_number)
        except Exception:
            logger.exception('Failed to post updated journal for payment %s', payment.payment_number)

        messages.success(request, f'Payment #{payment.payment_number} updated successfully!')
        return redirect_with_company('payment_received_list')
    
    except Exception as e:
        messages.error(request, f'Error updating payment: {str(e)}')
        return redirect_with_company('edit_payment_received', payment_id=payment_id)


def update_invoice_payment_status(inv):
    """
    Update invoice status based on payment allocations.
    FIXED: Changed parameter name and filter field from 'invoice' to 'inv'
    """
    total_paid = InvPaymentAllocation.objects.filter(
        inv=inv
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    total_amount = Decimal(inv.total_amount or 0)
    #added by neha on 4-3-26
    is_fully_paid = total_paid >= total_amount and total_amount > Decimal('0.00')
    if is_fully_paid:
        inv.payment_status_id = 3  # Paid
    elif total_paid > Decimal('0.00'):
        # Partial/underpaid invoice must remain open.
        inv.payment_status_id = 2  # Partial
    else:
        # No payment allocated.
        inv.payment_status_id = 1
    
    # Delivery completion: remaining quantity must be zero for all items.
    is_fully_delivered = True
    for invoice_item in inv.items.all():
        delivered = SalesDeliveryNoteItem.objects.filter(
            invoice_item=invoice_item,
            delivery_note__stock_updated=True
        ).aggregate(total=Sum('quantity_delivered'))['total'] or 0

        remaining_quantity = invoice_item.quantity - delivered
        print(f"DEBUG: Invoice #{inv.inv_number} - Item {invoice_item.id} - Ordered: {invoice_item.quantity}, Delivered: {delivered}, Remaining: {remaining_quantity}")
        if remaining_quantity > 0:
            is_fully_delivered = False
            break

    inv.status = 'Closed' if (is_fully_paid and is_fully_delivered) else 'Open'
    inv.save(update_fields=['status', 'payment_status'])


def payment_received_detail(request, payment_id):
    """
    Display detailed information about a specific payment.
    """
    payment = get_object_or_404(InvPayment, pk=payment_id)
    
    # Get all allocations for this payment
    allocations = InvPaymentAllocation.objects.filter(
        payment=payment
    ).select_related('inv').order_by('inv__inv_number')
    
    # Calculate totals
    total_allocated = allocations.aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')
    
    # Get advance transactions related to this payment
    advance_transactions = CustomerAdvancePayment.objects.filter(
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
    allocated_invs = []
    for allocation in allocations:
        inv = allocation.inv
        allocated_invs.append({
            'inv': inv,
            'allocated_amount': allocation.amount,
            'payment_made_on': allocation.payment_made_on,
        })
    
    context = {
        'payment': payment,
        'allocated_invs': allocated_invs,
        'total_allocated': total_allocated,
        'advance_used': advance_used,
        'advance_added': advance_added,
        'attachments': attachments,
        'has_allocations': allocations.exists(),
    }
    
    return render(request, 'sales/payment_received_detail.html', context)


# Get advance transactions related to this payment
    advance_transactions = CustomerAdvancePayment.objects.filter(
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

    # Prepare allocated invoices data
    allocated_invs = []
    for allocation in allocations:
        inv = allocation.inv
        allocated_invs.append({
            'inv': inv,
            'allocated_amount': allocation.amount,
            'payment_made_on': allocation.payment_made_on,
        })

    context = {
        'payment': payment,
        'allocated_invs': allocated_invs,
        'total_allocated': total_allocated,
        'advance_used': advance_used,
        'advance_added': advance_added,
        'attachments': attachments,
        'has_allocations': allocations.exists(),
        # company context used by print template
        'company': Company.objects.first(),
        'show_logo_in_print': True,
    }

    return render(request, 'sales/payment_received_print.html', context)


def payment_received_print(request, payment_id):
    """Render a printable HTML page for a payment received."""
    payment = get_object_or_404(InvPayment, pk=payment_id)

    # Get all allocations for this payment
    allocations = InvPaymentAllocation.objects.filter(
        payment=payment
    ).select_related('inv').order_by('inv__inv_number')

    # Calculate totals
    total_allocated = allocations.aggregate(
        total=Sum('amount')
    )['total'] or Decimal('0.00')

    # Get advance transactions related to this payment
    advance_transactions = CustomerAdvancePayment.objects.filter(
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

    # Prepare allocated invoices data
    allocated_invs = []
    for allocation in allocations:
        inv = allocation.inv
        allocated_invs.append({
            'inv': inv,
            'allocated_amount': allocation.amount,
            'payment_made_on': allocation.payment_made_on,
        })

    context = {
        'payment': payment,
        'allocated_invs': allocated_invs,
        'total_allocated': total_allocated,
        'advance_used': advance_used,
        'advance_added': advance_added,
        'attachments': attachments,
        'has_allocations': allocations.exists(),
        # company context used by print template
        'company': Company.objects.first(),
        'show_logo_in_print': True,
    }

    return render(request, 'sales/payment_received_print.html', context)


@transaction.atomic
def delete_payment_received(request, payment_id):
    """
    Delete a payment and reverse all associated transactions.
    
    This will:
    1. Reverse advance transactions (debit ₹ credit, credit ₹ debit)
    2. Delete bill allocations
    3. Update bill statuses
    4. Delete payment attachments
    5. Delete the payment record
    """
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect_with_company('payment_received_list')
    
    payment = get_object_or_404(InvPayment, pk=payment_id)
    
    # — CHECK PERIOD LOCK BEFORE DELETING PAYMENT RECEIVED
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
            return render(request, 'sales/receive_payment.html', {
                'title': 'Delete Payment',
                'error_message': str(e),
                'show_error_modal': True,
            })
    
    
    payment = get_object_or_404(InvPayment, pk=payment_id)
    
    try:
        # Store payment info for success message
        payment_number = payment.payment_number
        customer = payment.customer
        amount = payment.amount
        
        # Get all bills that were allocated from this payment
        allocated_invs = list(
            InvPaymentAllocation.objects.filter(payment=payment)
            .values_list('inv_id', flat=True)
        )
        
        # Step 1: Reverse advance transactions
        advance_transactions = CustomerAdvancePayment.objects.filter(payment=payment)
        
        for txn in advance_transactions:
            # Create reverse entry
            CustomerAdvancePayment.objects.create(
                customer=customer,
                amount=-txn.amount,  # Reverse the transaction
                payment=None  # Not linked to any payment (adjustment entry)
            )
        
        # Delete the original advance transactions
        advance_transactions.delete()
        
        # Step 2: Delete bill allocations
        InvPaymentAllocation.objects.filter(payment=payment).delete()
        
        # Step 3: Update bill statuses for affected bills
        if allocated_invs:
            invs = SalesInvoice.objects.filter(id__in=allocated_invs)
            for inv in invs:
                update_invoice_payment_status(inv)
        
        # Step 4: Delete attachments
        # Get all attachment file paths before deleting
        attachments = InvPaymentAttachment.objects.filter(payment=payment)
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
            f'Payment #{payment_number} for customer {customer} (₹{amount:.2f}) has been deleted successfully. '
            f'All advance transactions and bill allocations have been reversed.'
        )
        
        # Return JSON for AJAX requests, redirect for regular requests
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': success_msg
            })
        
        messages.success(request, success_msg)

        return redirect_with_company('payment_received_list')
        
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
        return redirect_with_company('payment_detail', payment_id=payment_id)

class SalesDeliveryNoteListView(ListView):
    model = SalesDeliveryNote
    template_name = 'sales/delivery_notes/sales_delivery_note_list.html'
    context_object_name = 'delivery_notes'
    paginate_by = 20
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require View on Sales Delivery
        if not (getattr(request.user, 'is_superuser', False) or can_view_delivery(request.user)):
            messages.error(request, 'You do not have permission to view Sales Delivery.')
            return redirect_with_company('sales_quote_list')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        queryset = SalesDeliveryNote.objects.select_related(
            'sales_invoice', 'sales_invoice__customer', 'warehouse'
        ).prefetch_related('items')
        
        # Search filter
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(delivery_note_number__icontains=search)
        
        # Invoice filter
        invoice = self.request.GET.get('invoice')
        if invoice:
            queryset = queryset.filter(sales_invoice__inv_number__icontains=invoice)
        
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
        all_deliveries = SalesDeliveryNote.objects.all()
        context['total_count'] = all_deliveries.count()
        context['pending_count'] = all_deliveries.filter(status='pending').count()
        context['delivered_count'] = all_deliveries.filter(status='delivered').count()
        context['cancelled_count'] = all_deliveries.filter(status='cancelled').count()
        
        return context


class SalesDeliveryNoteDetailView(DetailView):
    model = SalesDeliveryNote
    template_name = 'sales/delivery_notes/sales_delivery_note_detail.html'
    context_object_name = 'delivery_note'
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require View on Sales Delivery
        if not (getattr(request.user, 'is_superuser', False) or can_view_delivery(request.user)):
            messages.error(request, 'You do not have permission to view Sales Delivery.')
            return redirect_with_company('sales_quote_list')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        return SalesDeliveryNote.objects.select_related(
            'sales_invoice', 'sales_invoice__customer', 'warehouse'
        ).prefetch_related(
            'items__invoice_item__product',
            'stock_movements__stock__item',
            'stock_movements__stock__warehouse'
        )


class SalesDeliveryNoteCreateView(CreateView):
    model = SalesDeliveryNote
    form_class = SalesDeliveryNoteForm
    template_name = 'sales/delivery_notes/sales_delivery_note_form.html'
    success_url = 'sales_delivery_note_list'
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require Create on Sales Delivery
        if not (getattr(request.user, 'is_superuser', False) or can_create_delivery(request.user)):
            messages.error(request, 'You do not have permission to create Sales Delivery.')
            return redirect_with_company('sales_delivery_note_list')
        return super().dispatch(request, *args, **kwargs)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['formset'] = None
        
        # Check if period is locked for Sales Delivery (for current date pre-fill)
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
            
            if fiscal_year and fiscal_year.lock_sales_delivery:
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
            PeriodLockEnforcer.check_can_edit(delivery_date, self.request.user, db=db, transaction_type='sales_delivery')
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
                        'transaction_type': 'sales_delivery',
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
            
            print(f"Sales Delivery Note created: {self.object.delivery_note_number}, Status: {self.object.status}")
            
            # Process items from POST data
            items_created = self.save_delivery_items()
            
            if items_created == 0:
                messages.error(self.request, 'Please select at least one item to deliver.')
                self.object.delete()
                return self.form_invalid(form)
            
            print(f"Items created: {items_created}")
            
            # Update stock if status is delivered
            try:
                if self.object.status == 'delivered' and is_stock_management_on_delivery(request=self.request):
                    print(f"Calling update_stock() for {self.object.delivery_note_number}")
                    self.object.update_stock()
                    print(f"Stock updated: {self.object.stock_updated}")
                update_invoice_payment_status(self.object.sales_invoice)

                messages.success(
                    self.request,
                    f'Sales Delivery Note {self.object.delivery_note_number} created successfully with {items_created} items!'
                )
                
                if self.object.stock_updated:
                    messages.success(self.request, 'Stock has been reduced in the warehouse.')
                    
            except ValidationError as e:
                messages.error(self.request, str(e))
                self.object.delete()
                return self.form_invalid(form)
            
            return redirect_with_company(self.success_url)
    
    def save_delivery_items(self):
        """Process the custom item data from POST"""
        items_created = 0
        
        # Get all item indices from POST data
        item_indices = set()
        for key in self.request.POST.keys():
            if key.startswith('items[') and '][invoice_item]' in key:
                index = key.split('[')[1].split(']')[0]
                item_indices.add(index)
        
        print(f"Found item indices: {item_indices}")
        
        # Process each item
        for index in item_indices:
            selected_key = f'items[{index}][selected]'
            invoice_item_key = f'items[{index}][invoice_item]'
            quantity_key = f'items[{index}][quantity_delivered]'
            
            # Check if item is selected
            if selected_key in self.request.POST:
                invoice_item_id = self.request.POST.get(invoice_item_key)
                quantity = self.request.POST.get(quantity_key)
                
                print(f"Processing item {index}: invoice_item_id={invoice_item_id}, quantity={quantity}")
                
                if invoice_item_id and quantity:
                    try:
                        invoice_item = SalesInvoiceItem.objects.get(id=invoice_item_id)
                        delivery_item = SalesDeliveryNoteItem.objects.create(
                            delivery_note=self.object,
                            invoice_item=invoice_item,
                            quantity_delivered=int(quantity)
                        )
                        print(f"Created SalesDeliveryNoteItem: {delivery_item}")
                        items_created += 1
                    except (SalesInvoiceItem.DoesNotExist, ValueError) as e:
                        print(f"Error creating item: {str(e)}")
                        messages.warning(self.request, f'Error adding item: {str(e)}')
        
        return items_created


class SalesDeliveryNoteUpdateView(UpdateView):
    model = SalesDeliveryNote
    form_class = SalesDeliveryNoteForm
    template_name = 'sales/delivery_notes/sales_delivery_note_form.html'
    success_url = 'sales_delivery_note_list'
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require Edit on Sales Delivery
        if not (getattr(request.user, 'is_superuser', False) or can_edit_delivery(request.user)):
            messages.error(request, 'You do not have permission to edit Sales Delivery.')
            return redirect_with_company('sales_delivery_note_list')
        return super().dispatch(request, *args, **kwargs)
    template_name = 'sales/delivery_notes/sales_delivery_note_form.html'
    success_url = 'sales_delivery_note_list'
    
    def get_queryset(self):
        # Only allow editing if stock hasn't been updated
        return SalesDeliveryNote.objects.filter(stock_updated=False)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['formset'] = None
        context['existing_items'] = self.object.items.all()
        
        # Check if period is locked for Sales Delivery (for current object's date)
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
            
            if fiscal_year and fiscal_year.lock_sales_delivery:
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
            PeriodLockEnforcer.check_can_edit(delivery_date, self.request.user, db=db, transaction_type='sales_delivery')
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
                        'transaction_type': 'sales_delivery',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception: pass

            if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'message': str(e), 'period_lock_error': period_lock_error}, status=400)
            
            messages.error(self.request, str(e))
            return self.form_invalid(form)

        old_status = SalesDeliveryNote.objects.get(pk=self.object.pk).status
        
        with transaction.atomic():
            self.object = form.save()
            
            # Delete existing items and create new ones
            self.object.items.all().delete()
            items_created = self.save_delivery_items()
            
            if items_created == 0:
                messages.error(self.request, 'Please select at least one item to deliver.')
                return self.form_invalid(form)
            
            # Update stock if status changed to delivered and delivery-based stock management is enabled
            try:
                if self.object.status == 'delivered' and old_status != 'delivered' and is_stock_management_on_delivery(request=self.request):
                    self.object.update_stock()
                update_invoice_payment_status(self.object.sales_invoice)
                messages.success(
                    self.request,
                    f'Sales Delivery Note {self.object.delivery_note_number} updated successfully!'
                )
                
            except ValidationError as e:
                messages.error(self.request, str(e))
                return self.form_invalid(form)
            
            # return super().form_valid(form)
            return redirect_with_company(self.success_url)

    
    def save_delivery_items(self):
        """Process the custom item data from POST"""
        items_created = 0
        
        item_indices = set()
        for key in self.request.POST.keys():
            if key.startswith('items[') and '][invoice_item]' in key:
                index = key.split('[')[1].split(']')[0]
                item_indices.add(index)
        
        for index in item_indices:
            selected_key = f'items[{index}][selected]'
            invoice_item_key = f'items[{index}][invoice_item]'
            quantity_key = f'items[{index}][quantity_delivered]'
            
            if selected_key in self.request.POST:
                invoice_item_id = self.request.POST.get(invoice_item_key)
                quantity = self.request.POST.get(quantity_key)
                
                if invoice_item_id and quantity:
                    try:
                        invoice_item = SalesInvoiceItem.objects.get(id=invoice_item_id)
                        SalesDeliveryNoteItem.objects.create(
                            delivery_note=self.object,
                            invoice_item=invoice_item,
                            quantity_delivered=int(quantity)
                        )
                        items_created += 1
                    except (SalesInvoiceItem.DoesNotExist, ValueError) as e:
                        messages.warning(self.request, f'Error adding item: {str(e)}')
        
        return items_created


def sales_delivery_note_mark_delivered(request, pk):
    """Mark sales delivery note as delivered"""
    # Permission: require Edit on Sales Delivery
    if not (getattr(request.user, 'is_superuser', False) or can_edit_delivery(request.user)):
        messages.error(request, 'You do not have permission to mark Sales Delivery as delivered.')
        return redirect_with_company('sales_delivery_note_detail', pk=pk)
    
    delivery_note = get_object_or_404(SalesDeliveryNote, pk=pk)
    
    if request.method == 'POST':
        # Check period lock before marking delivered
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(delivery_note.delivery_date, request.user, db=db, transaction_type='sales_delivery')
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
                        'transaction_type': 'sales_delivery',
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
            return redirect_with_company('sales_delivery_note_detail', pk=pk)

        if delivery_note.status == 'delivered':
            messages.warning(request, 'This delivery is already marked as delivered.')
        else:
            try:
                with transaction.atomic():
                    delivery_note.status = 'delivered'
                    delivery_note.save()
                    if is_stock_management_on_delivery(request=request):
                        delivery_note.update_stock()
                    update_invoice_payment_status(delivery_note.sales_invoice)
                messages.success(
                    request,
                    f'Sales Delivery Note {delivery_note.delivery_note_number} marked as delivered and stock updated!'
                )
            except ValidationError as e:
                messages.error(request, f'Error: {str(e)}')
            except Exception as e:
                messages.error(request, f'Error updating delivery: {str(e)}')
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    
    return redirect_with_company('sales_delivery_note_detail', pk=pk)


def sales_delivery_note_cancel(request, pk):
    """Cancel sales delivery note"""
    # Permission: require Delete on Sales Delivery
    if not (getattr(request.user, 'is_superuser', False) or can_delete_delivery(request.user)):
        messages.error(request, 'You do not have permission to cancel Sales Delivery.')
        return redirect_with_company('sales_delivery_note_detail', pk=pk)
    
    delivery_note = get_object_or_404(SalesDeliveryNote, pk=pk)
    
    if request.method == 'POST':
        # Check period lock before canceling
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(delivery_note.delivery_date, request.user, db=db, transaction_type='sales_delivery')
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
                        'transaction_type': 'sales_delivery',
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
            return redirect_with_company('sales_delivery_note_detail', pk=pk)

        if delivery_note.status == 'cancelled':
            messages.warning(request, 'This delivery is already cancelled.')
        else:
            try:
                with transaction.atomic():
                    delivery_note.status = 'cancelled'
                    delivery_note.save()
                    
                    # Reverse stock if it was updated
                    if delivery_note.stock_updated:
                        delivery_note.reverse_stock()
                    update_invoice_payment_status(delivery_note.sales_invoice)
                
                messages.success(
                    request,
                    f'Sales Delivery Note {delivery_note.delivery_note_number} cancelled successfully!'
                )
            except Exception as e:
                messages.error(request, f'Error cancelling delivery: {str(e)}')
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    
    return redirect_with_company('sales_delivery_note_detail', pk=pk)


def sales_delivery_note_print(request, pk):
    """Print sales delivery note in professional format"""
    delivery_note = get_object_or_404(SalesDeliveryNote, pk=pk)
    
    # Fetch company settings
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    
    # Calculate totals
    total_line_items = delivery_note.items.count()
    total_units_delivered = sum(item.quantity_delivered for item in delivery_note.items.all())
    
    return render(request, 'sales/delivery_notes/sales_delivery_note_print.html', {
        'delivery_note': delivery_note,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
        'total_line_items': total_line_items,
        'total_units_delivered': total_units_delivered,
    })

# API views for AJAX calls
def get_invoice_items(request, invoice_id):
    """Get items for a specific invoice with delivery status"""
    invoice = get_object_or_404(SalesInvoice, pk=invoice_id)
    items = []
    
    for invoice_item in invoice.items.all():
        # Calculate already delivered quantity
        delivered = SalesDeliveryNoteItem.objects.filter(
            invoice_item=invoice_item,
            delivery_note__stock_updated=True
        ).aggregate(total=Sum('quantity_delivered'))['total'] or 0
        
        items.append({
            'id': invoice_item.id,
            'product_name': invoice_item.product.name,
            'hsn_code': invoice_item.hsn_code,
            'quantity': invoice_item.quantity,
            'delivered_quantity': delivered,
            'remaining_quantity': invoice_item.quantity - delivered,
            'price': str(invoice_item.price)
        })
    
    return JsonResponse(items, safe=False)


def get_invoice_details(request, invoice_id):
    """Get invoice details"""
    invoice = get_object_or_404(SalesInvoice, pk=invoice_id)
    
    data = {
        'customer_name': str(invoice.customer) if invoice.customer else 'N/A',
        'inv_number': invoice.inv_number,
        'sales_person': str(invoice.sales_person) if invoice.sales_person else 'N/A',
        'shipping_address': f"{invoice.shipping_address1 or ''} {invoice.shipping_address2 or ''} {invoice.shipping_city or ''} {invoice.shipping_state or ''} {invoice.shipping_postal_code or ''}".strip() or 'N/A'
    }
    
    return JsonResponse(data)


def add_unit_from_sales(request):
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        name = request.POST.get('name', '').strip()
        
        if not name:
            return JsonResponse({'success': False, 'error': 'Unit name is required'})
        
        # Check if unit already exists
        if Unit.objects.filter(unit_name__iexact=name).exists():
            return JsonResponse({'success': False, 'error': 'Unit already exists'})
        
        # Create new unit
        unit = Unit.objects.create(unit_name=name)
        
        return JsonResponse({
            'success': True,
            'id': unit.id,
            'name': unit.unit_name
        })
    
    return JsonResponse({'success': False, 'error': 'Invalid request'})



def _get_sales_return_fixed_warehouse(invoice, request):
    """Return the warehouse that should receive stock for an invoice return."""
    from stock.models import Stock

    db = getattr(request, 'company_db', 'default')
    delivery_note = (
        SalesDeliveryNote.objects.using(db)
        .filter(sales_invoice_id=invoice.id)
        .select_related('warehouse')
        .order_by('-delivery_date', '-id')
        .first()
    )
    fixed_warehouse = delivery_note.warehouse if delivery_note and delivery_note.warehouse_id else None
    if fixed_warehouse is not None:
        return fixed_warehouse

    invoice_item_product_ids = list(
        SalesInvoiceItem.objects.using(db)
        .filter(sales_inv_id=invoice.id)
        .values_list('product_id', flat=True)
    )
    if not invoice_item_product_ids:
        return None

    stock_row = (
        Stock.objects.using(db)
        .filter(item_id__in=invoice_item_product_ids, warehouse__isnull=False)
        .select_related('warehouse')
        .order_by('-id')
        .first()
    )
    return stock_row.warehouse if stock_row and stock_row.warehouse_id else None


def _get_sales_invoice_paid_total(invoice):
    try:
        return invoice.payment_allocations.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    except Exception:
        return Decimal('0.00')


def _get_returnable_invoice_items(invoice):
    items = []
    for invoice_item in SalesInvoiceItem.objects.filter(sales_inv=invoice).select_related('product'):
        returned_total = SalesReturnItem.objects.filter(
            invoice_item=invoice_item,
            sales_return__status__in=['pending', 'received']
        ).aggregate(total=Sum('quantity_returned'))['total'] or 0

        try:
            ordered_qty = int(invoice_item.quantity or 0)
            returned_qty = int(returned_total or 0)
        except Exception:
            ordered_qty = 0
            returned_qty = 0

        remaining_qty = max(ordered_qty - returned_qty, 0)
        if remaining_qty <= 0:
            continue

        items.append({
            'id': invoice_item.id,
            'product_name': invoice_item.product.name if invoice_item.product else '',
            'hsn_code': invoice_item.hsn_code or '',
            'quantity': ordered_qty,
            'already_returned': returned_qty,
            'remaining_quantity': remaining_qty,
            'price': str(invoice_item.price or 0),
            'tax_rate': str(getattr(invoice_item, 'prd_tax', 0) or 0),
        })
    return items


@login_required
def sales_return_create(request):
    if not (getattr(request.user, 'is_superuser', False) or can_create_returns(request.user)):
        messages.error(request, 'You do not have permission to create Sales Returns.')
        return redirect_with_company('sales_return_list')

    invoices = SalesInvoice.objects.select_related('customer').order_by('-date', '-id')
    return render(request, 'sales/return_create_form.html', {
        'invoices': invoices,
    })


@login_required
def sales_return_invoice_items(request, invoice_id):
    if not (getattr(request.user, 'is_superuser', False) or can_create_returns(request.user)):
        return JsonResponse({'success': False, 'message': 'Permission denied'}, status=403)

    invoice = get_object_or_404(SalesInvoice.objects.select_related('customer'), pk=invoice_id)
    total_paid = _get_sales_invoice_paid_total(invoice)
    fixed_warehouse = _get_sales_return_fixed_warehouse(invoice, request)
    items = _get_returnable_invoice_items(invoice)

    return JsonResponse({
        'success': True,
        'invoice': {
            'id': invoice.id,
            'inv_number': invoice.inv_number,
            'customer_name': get_customer_display_name(invoice.customer) or str(invoice.customer or ''),
            'status': invoice.status,
            'paid': str(total_paid),
            'can_return': total_paid > Decimal('0.00'),
            'warehouse_name': fixed_warehouse.warehouse_name if fixed_warehouse else '',
        },
        'items': items,
    })


@login_required
def sales_return_view(request, pk):
    """Create a SalesReturn for the given invoice. Handles GET (form) and POST (process).

    POST expects:
    - quantities for items as items[<invoice_item_id>]=<qty>
    - warehouse (optional)
    - refund_amount (optional)
    - refund_type in ['refund','credit']
    """
    from .models import SalesReturn, SalesReturnItem
    from stock.models import Stock
    from chart_of_accounts.models import ChartOfAccounts

    inv = get_object_or_404(SalesInvoice, pk=pk)

    total_paid = _get_sales_invoice_paid_total(inv)
    fixed_warehouse = _get_sales_return_fixed_warehouse(inv, request)

    if request.method == 'GET':
        if total_paid <= Decimal('0.00'):
            messages.error(request, 'Returns are allowed only after a payment has been received for this invoice.')
            return redirect_with_company(request, 'invoice_detail', pk=inv.pk)

        # Check if period is locked for Sales Return
        from system_settings.models import FiscalYear
        db = getattr(request, 'company_db', 'default')
        period_locked = False
        fiscal_year_name = ''
        
        try:
            fiscal_year = FiscalYear.objects.using(db).filter(
                start_date__lte=timezone.now().date(),
                end_date__gte=timezone.now().date(),
                status='active'
            ).first()
            
            if fiscal_year and fiscal_year.lock_sales_return:
                period_locked = True
                fiscal_year_name = fiscal_year.name
        except Exception:
            pass

        # Exclude invoice items that are already fully returned
        items = []
        for ii in SalesInvoiceItem.objects.filter(sales_inv=inv):
            returned_total = SalesReturnItem.objects.filter(invoice_item=ii, sales_return__status__in=['pending','received']).aggregate(total=Sum('quantity_returned'))['total'] or 0
            try:
                remaining = int(ii.quantity or 0) - int(returned_total or 0)
            except Exception:
                remaining = 0
            if remaining > 0:
                # set the displayed quantity to remaining so template shows correct prefilled qty
                ii.quantity = remaining
                items.append(ii)
        return render(request, 'sales/return_form.html', {
            'invoice': inv, 
            'items': items, 
            'fixed_warehouse': fixed_warehouse,
            'total_paid': total_paid,
            'period_locked': period_locked,
            'fiscal_year_name': fiscal_year_name
        })

    # POST
    try:
        # Check period lock before processing the return
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        return_date = request.POST.get('date') or timezone.now().date()
        try:
            return_date = datetime.strptime(return_date, '%Y-%m-%d').date() if isinstance(return_date, str) else return_date
        except Exception:
            return_date = timezone.now().date()
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(return_date, request.user, db=db, transaction_type='sales_return')
        except PermissionDenied as e:
            error_msg = str(e)
            # Provide structured period lock info so front-end can show modal (consistent with other endpoints)
            try:
                _, fiscal_year, _ = PeriodLockEnforcer.is_period_locked(return_date, request.user, db=db, transaction_type='sales_return')
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
            return JsonResponse({'success': False, 'message': 'Returns are allowed only after a payment has been received for this invoice.'}, status=400)

        if not quantities:
            return JsonResponse({'success': False, 'message': 'No items selected for return.'}, status=400)

        for inv_item_id, qty in quantities.items():
            try:
                inv_item = SalesInvoiceItem.objects.get(pk=inv_item_id, sales_inv=inv)
            except SalesInvoiceItem.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Invalid invoice item selected.'}, status=400)

            returned_total = SalesReturnItem.objects.filter(
                invoice_item=inv_item,
                sales_return__status__in=['pending', 'received']
            ).aggregate(total=Sum('quantity_returned'))['total'] or 0
            remaining_qty = int(inv_item.quantity or 0) - int(returned_total or 0)
            if qty > remaining_qty:
                item_name = inv_item.product.name if inv_item.product else 'Selected item'
                return JsonResponse({
                    'success': False,
                    'message': f'{item_name} has only {remaining_qty} quantity remaining for return.'
                }, status=400)

        warehouse_id = fixed_warehouse.pk if fixed_warehouse else None
        status = request.POST.get('status') or 'pending'
        shipped_by = request.POST.get('shipped_by') or ''
        courier_name = request.POST.get('courier_name') or ''
        tracking_number = request.POST.get('tracking_number') or ''

        with transaction.atomic():
            sr = SalesReturn.objects.create(
                sales_invoice=inv,
                customer=inv.customer,
                warehouse_id=warehouse_id if warehouse_id else None,
                refund_amount=Decimal('0.00'),
                notes=request.POST.get('notes',''),
                status=status,
                shipped_by=shipped_by,
                courier_name=courier_name,
                tracking_number=tracking_number,
            )

            # create return items and update stock
            for inv_item_id, qty in quantities.items():
                try:
                    inv_item = SalesInvoiceItem.objects.get(pk=inv_item_id, sales_inv=inv)
                except SalesInvoiceItem.DoesNotExist:
                    raise

                SalesReturnItem.objects.create(
                    sales_return=sr,
                    invoice_item=inv_item,
                    quantity_returned=qty
                )

                # increase stock (if Stock entry exists for same warehouse)
                try:
                    if warehouse_id and status == 'received':
                        stock = Stock.objects.get(item=inv_item.product, warehouse_id=warehouse_id)
                        stock.quantity = (stock.quantity or 0) + qty
                        stock.save()

                        # create SalesStockMovement record (stock in)
                        try:
                            SalesStockMovement = None
                            from .models import SalesStockMovement
                            SalesStockMovement.objects.create(
                                stock=stock,
                                movement_type='in',
                                quantity=qty,
                                reference_type='sales_return',
                                reference_id=sr.id,
                                sales_return=sr,
                                notes=f"Stock in from Sales Return {sr.return_number}",
                                created_by=request.user
                            )
                        except Exception:
                            logger.exception('Failed to create stock movement for return')
                except Exception:
                    # ignore stock errors but log
                    logger.exception('Failed to update stock for return')

            # If refund requested, create an outgoing payment record (InvPayment)
            # Compute refund amount automatically from returned items (price * qty)
            try:
                computed_refund = Decimal('0.00')
                for ritem in SalesReturnItem.objects.filter(sales_return=sr).select_related('invoice_item'):
                    try:
                        price = Decimal(ritem.invoice_item.price or 0)
                        qty = Decimal(ritem.quantity_returned or 0)
                        tax_rate = Decimal(getattr(ritem.invoice_item, 'prd_tax', 0) or 0)
                        # include tax in refund: price * qty * (1 + tax_rate/100)
                        line_amount = (price * qty) * (Decimal('1.00') + (tax_rate / Decimal('100.00')))
                        computed_refund += line_amount
                    except Exception:
                        continue
                # persist computed refund on SalesReturn
                sr.refund_amount = computed_refund.quantize(Decimal('0.01'))
                sr.save(update_fields=['refund_amount'])
            except Exception:
                logger.exception('Failed to compute/save refund amount for SalesReturn %s', getattr(sr, 'id', None))

            # --- Reverse related journal entries: invoice, COGS and payment allocations ---
            try:
                # helper to get next JV number
                def _next_jv():
                    last = JournalEntry.objects.order_by('-id').first()
                    if last and last.entry_number and last.entry_number.startswith('JV-'):
                        try:
                            last_num = int(last.entry_number.split('-')[1])
                        except Exception:
                            last_num = 0
                    else:
                        last_num = 0
                    return f"JV-{str(last_num + 1).zfill(5)}"

                # Use the sales return date when available
                rev_date = getattr(sr, 'date', None) or timezone.now().date()

                # Reverse invoice journal (scale proportionally for partial returns)
                old_invoice_entry = JournalEntry.objects.filter(reference=inv.inv_number).exclude(narration__startswith='Reversal of').prefetch_related('lines').order_by('-id').first()
                if old_invoice_entry:
                    # compute taxable totals on invoice and returned items to derive proportion
                    try:
                        invoice_items = SalesInvoiceItem.objects.filter(sales_inv=inv)
                        # include tax in totals so proportion reflects full line value
                        invoice_taxable_total = Decimal('0.00')
                        for it in invoice_items:
                            p = Decimal(it.price or 0)
                            q = Decimal(it.quantity or 0)
                            tr = Decimal(getattr(it, 'prd_tax', 0) or 0)
                            invoice_taxable_total += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))

                        returned_items = SalesReturnItem.objects.filter(sales_return=sr).select_related('invoice_item')
                        returned_taxable = Decimal('0.00')
                        for ri in returned_items:
                            p = Decimal(ri.invoice_item.price or 0)
                            q = Decimal(ri.quantity_returned or 0)
                            tr = Decimal(getattr(ri.invoice_item, 'prd_tax', 0) or 0)
                            returned_taxable += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))
                    except Exception:
                        invoice_taxable_total = Decimal('0.00')
                        returned_taxable = Decimal('0.00')

                    proportion = Decimal('1.00')
                    if invoice_taxable_total and returned_taxable and invoice_taxable_total > 0:
                        proportion = (returned_taxable / invoice_taxable_total).quantize(Decimal('0.0001'))
                        if proportion > 1:
                            proportion = Decimal('1.00')

                    entry_number = _next_jv()
                    rev = JournalEntry.objects.create(
                        entry_number=entry_number,
                        date=rev_date,
                        reference=old_invoice_entry.reference,
                        narration=f"Reversal of {old_invoice_entry.entry_number}",
                        created_by=request.user,
                        updated_by=request.user,
                        status='posted'
                    )
                    for line in old_invoice_entry.lines.all():
                        # scale amounts by proportion for partial return
                        try:
                            rev_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                            rev_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
                        except Exception:
                            rev_debit = line.credit
                            rev_credit = line.debit

                        JournalLine.objects.create(
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
                        logger.exception('Failed to update totals for invoice reversal %s', getattr(rev, 'id', None))

                # Reverse COGS journal (if any)
                old_cogs_entry = JournalEntry.objects.filter(reference=f"COGS-{inv.inv_number}").exclude(narration__startswith='Reversal of').prefetch_related('lines').order_by('-id').first()
                if old_cogs_entry:
                    # For COGS, scale by same proportion as taxable sales
                    entry_number = _next_jv()
                    revc = JournalEntry.objects.create(
                        entry_number=entry_number,
                        date=rev_date,
                        reference=old_cogs_entry.reference,
                        narration=f"Reversal of {old_cogs_entry.entry_number}",
                        created_by=request.user,
                        updated_by=request.user,
                        status='posted'
                    )
                    for line in old_cogs_entry.lines.all():
                        try:
                            revc_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                            revc_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
                        except Exception:
                            revc_debit = line.credit
                            revc_credit = line.debit

                        JournalLine.objects.create(
                            journal=revc,
                            account=line.account,
                            description=f"Reversal: {line.description}",
                            debit=revc_debit,
                            credit=revc_credit,
                            sequence=line.sequence
                        )
                    try:
                        revc.total_debit = sum(l.debit or 0 for l in revc.lines.all())
                        revc.total_credit = sum(l.credit or 0 for l in revc.lines.all())
                        revc.save(update_fields=['total_debit', 'total_credit'])
                    except Exception:
                        logger.exception('Failed to update totals for COGS reversal %s', getattr(revc, 'id', None))

                # Reverse payment allocations for this invoice by creating a reversing
                # journal for the allocated amount (debit Debtors, credit Bank/Cash account)
                allocations = InvPaymentAllocation.objects.filter(inv=inv).select_related('payment')
                # If refund_amount provided, distribute it across allocations (so partial refund doesn't reverse full allocations)
                try:
                    remaining_refund = Decimal(sr.refund_amount) if (getattr(sr, 'refund_amount', None) is not None and getattr(sr, 'refund_amount', 0) != 0) else None
                except Exception:
                    remaining_refund = None

                for alloc in allocations:
                    try:
                        amt = alloc.amount or Decimal('0.00')
                        if amt <= 0:
                            continue
                        payment_obj = getattr(alloc, 'payment', None)
                        paid_through_acct = getattr(payment_obj, 'paid_through', None) if payment_obj else None
                        debt_acct = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
                        if not debt_acct:
                            debt_acct = ChartOfAccounts.objects.filter(code='1020101').first()

                        if paid_through_acct and debt_acct:
                            # Determine amount to reverse:
                            # - If a refund amount was provided, allocate it across allocations (consume remaining_refund)
                            # - Otherwise, use the same proportion as the invoice/COGS reversal
                            try:
                                if remaining_refund is not None:
                                    if remaining_refund <= 0:
                                        alloc_reverse_amt = Decimal('0.00')
                                    else:
                                        alloc_reverse_amt = min(amt, remaining_refund)
                                        remaining_refund = (remaining_refund - alloc_reverse_amt).quantize(Decimal('0.01'))
                                else:
                                    alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))
                            except Exception:
                                alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))

                            if alloc_reverse_amt and alloc_reverse_amt > 0:
                                alloc_reverse_base = scale_amount_for_journal(alloc_reverse_amt, inv)
                                entry_number = _next_jv()
                                pay_rev = JournalEntry.objects.create(
                                    entry_number=entry_number,
                                    date=rev_date,
                                    reference=f"Reversal-PAY-{getattr(payment_obj, 'payment_number', '')}-INV-{inv.inv_number}",
                                    narration=f"Reversal of payment allocation for Invoice {inv.inv_number}",
                                    created_by=request.user,
                                    updated_by=request.user,
                                    status='posted'
                                )
                                # Debit Debtors (increase receivable)
                                JournalLine.objects.create(
                                    journal=pay_rev,
                                    account=debt_acct,
                                    description=f"Reversal alloc: Invoice {inv.inv_number}",
                                    debit=alloc_reverse_base,
                                    credit=Decimal('0.00'),
                                    sequence=10
                                )
                                # Credit Bank/Cash (reduce cash)
                                JournalLine.objects.create(
                                    journal=pay_rev,
                                    account=paid_through_acct,
                                    description=f"Reversal alloc: Payment #{getattr(payment_obj, 'payment_number', '')}",
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
            except Exception:
                logger.exception('Failed to create reversal journal entries for sales return %s', getattr(sr, 'id', None))

            # If return status indicates stock received, mark stock_updated True
            if status == 'received':
                try:
                    sr.stock_updated = True
                    sr.save(update_fields=['stock_updated'])
                except Exception:
                    logger.exception('Failed to mark sales return stock_updated')

        return JsonResponse({'success': True, 'message': 'Sales return created', 'return_id': sr.pk})

    except Exception as e:
        logger.exception('Error processing sales return')
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
def sales_return_list(request):
    """List all sales returns with pagination and simple search."""
    # Permission: require View on Sales Return
    if not can_view_returns(request.user):
        return redirect_with_company(request, 'license_restricted')
    
    qs = SalesReturn.objects.select_related('sales_invoice', 'customer', 'warehouse').order_by('-date')

    # Basic search and filters
    q = request.GET.get('q', '').strip()
    invoice_q = request.GET.get('invoice', '').strip()
    status = request.GET.get('status', '').strip()
    stock_updated = request.GET.get('stock_updated', '').strip()

    if q:
        qs = qs.filter(
            Q(return_number__icontains=q) |
            Q(customer__company_name__icontains=q) |
            Q(customer__first_name__icontains=q) |
            Q(customer__last_name__icontains=q)
        )

    if invoice_q:
        qs = qs.filter(sales_invoice__inv_number__icontains=invoice_q)

    # status filter
    if status:
        if status == 'returned':
            qs = qs.filter(status='received')
        elif status == 'pending':
            qs = qs.filter(status='pending')
        elif status == 'cancelled':
            qs = qs.filter(status='cancelled')

    if stock_updated == 'true':
        qs = qs.filter(stock_updated=True)
    elif stock_updated == 'false':
        qs = qs.filter(stock_updated=False)

    # Stats for cards
    total_count = qs.count()
    pending_count = qs.filter(status='pending').count()
    processed_count = qs.filter(status='received').count()
    cancelled_count = qs.filter(status='cancelled').count()

    paginator = Paginator(qs, 25)
    page = request.GET.get('page')
    returns = paginator.get_page(page)

    context = {
        'returns': returns,
        'search_query': q,
        'total_count': total_count,
        'pending_count': pending_count,
        'delivered_count': processed_count,
        'cancelled_count': cancelled_count,
        'request': request,
        'is_paginated': returns.has_other_pages(),
        'page_obj': returns,
        'can_create_returns': getattr(request.user, 'is_superuser', False) or can_create_returns(request.user),
    }

    return render(request, 'sales/return_list.html', context)


@login_required
def sales_return_detail(request, pk):
    sr = get_object_or_404(SalesReturn, pk=pk)
    items = SalesReturnItem.objects.filter(sales_return=sr).select_related('invoice_item__product')
    # Stock movements created for this return (if any)
    movements = SalesStockMovement.objects.filter(sales_return=sr).select_related('stock__item').order_by('-created_at')
    return render(request, 'sales/return_detail.html', {'return_obj': sr, 'items': items, 'movements': movements})


@login_required
def sales_return_print(request, pk):
    """Render a printable page for the sales return (for printing in browser)."""
    sales_return = get_object_or_404(SalesReturn, pk=pk)
    items = SalesReturnItem.objects.filter(sales_return=sales_return).select_related('invoice_item__product')
    # Fetch company settings
    company = Company.objects.filter(status=1).first() or Company.objects.first()
    return render(request, 'sales/return_print.html', {
        'sales_return': sales_return,
        'items': items,
        'company': company,
        'show_logo_in_print': bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False,
    })


def sales_return_cancel(request, pk):
    """Cancel a sales return: set status and reverse stock if updated."""
    # Permission: require Delete on Sales Delivery (reuse delivery permission)
    if not (getattr(request.user, 'is_superuser', False) or can_delete_delivery(request.user)):
        messages.error(request, 'You do not have permission to cancel Sales Returns.')
        return redirect_with_company('sales_return_detail', pk=pk)

    sr = get_object_or_404(SalesReturn, pk=pk)

    if request.method == 'POST':
        # Check period lock before canceling
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(sr.date, request.user, db=db, transaction_type='sales_return')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=sr.date,
                    end_date__gte=sr.date,
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
                        'transaction_type': 'sales_return',
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
            return redirect_with_company('sales_return_detail', pk=pk)

        if sr.status == 'cancelled':
            messages.warning(request, 'This return is already cancelled.')
        else:
            try:
                from stock.models import Stock
                from journal.models import JournalEntry, JournalLine
                
                with transaction.atomic():
                    sr.status = 'cancelled'
                    sr.refund_amount = 0
                    sr.save()

                    # Reverse journal entries created for this return (reverse the reversals)
                    if sr.sales_invoice:
                        inv = sr.sales_invoice
                        company_db = getattr(request, 'company_db', 'default')
                        try:
                            from journal.models import JournalEntry, JournalLine
                            
                            # Helper to get next journal entry number
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
                            
                            # Find the most recent reversal entries for invoice, COGS, and payment
                            # All reversals created when return was created will mention the invoice
                            
                            logger.info(f'=== CANCEL: Looking for reversals for invoice {inv.inv_number} return {sr.return_number} in DB={company_db} ===')
                            
                            # Check if reversals were created when marked as received (they will have "Sales Return" in narration)
                            # If so, cancel those. Otherwise, cancel the original reversals from creation.
                            mark_received_reversals = JournalEntry.objects.using(company_db).filter(
                                Q(reference=inv.inv_number) | Q(reference__icontains=f"COGS-{inv.inv_number}") |
                                (Q(reference__icontains='Reversal-PAY') & Q(reference__icontains=f"-INV-{inv.inv_number}"))
                            ).filter(
                                narration__icontains=f'Sales Return {sr.return_number}'
                            ).exclude(
                                narration__icontains='Reversal of Reversal'
                            ).exclude(
                                narration__icontains='Cancelled Return'
                            ).order_by('-id')
                            
                            # If we found mark_received reversals, use those. Otherwise, use original reversals.
                            if mark_received_reversals.exists():
                                logger.info(f'Found mark_received reversals, will cancel those')
                                reversal_entries = mark_received_reversals
                            else:
                                logger.info(f'No mark_received reversals found, looking for original reversals')
                                # Get original reversals that haven't been cancelled yet
                                # (i.e., no "Reversal of Reversal" cancel entry exists for them)
                                reversal_entries = JournalEntry.objects.using(company_db).filter(
                                    Q(reference=inv.inv_number) |  
                                    Q(reference__icontains=f"COGS-{inv.inv_number}") |  
                                    (Q(reference__icontains='Reversal-PAY') & Q(reference__icontains=f"-INV-{inv.inv_number}"))
                                ).filter(
                                    narration__icontains='Reversal'
                                ).exclude(
                                    narration__icontains='Sales Return'  # Exclude mark_received reversals
                                ).exclude(
                                    narration__icontains='Reversal of Reversal'  # Exclude already-cancelled reversals
                                ).exclude(
                                    narration__icontains='Cancelled Return'  # Exclude cancel entries
                                ).order_by('-id')
                            
                            logger.info(f'=== Found {reversal_entries.count()} reversal entries to cancel: {[(e.entry_number, e.reference, e.narration[:60]) for e in reversal_entries]} ===')
                            
                            if reversal_entries:
                                # Filter out reversals that already have cancel entries.
                                # Then keep only the latest reversal per logical reference so
                                # older qty/version reversals are not cancelled again.
                                pending_cancel = []
                                for rev_entry in reversal_entries:
                                    cancel_exists = JournalEntry.objects.using(company_db).filter(
                                        narration__icontains="Reversal of Reversal"
                                    ).filter(
                                        narration__icontains=rev_entry.entry_number
                                    ).exists()

                                    if not cancel_exists:
                                        pending_cancel.append(rev_entry)
                                        logger.info(f'Will consider cancel reversal {rev_entry.entry_number}')
                                    else:
                                        logger.info(f'Cancel entry already exists for {rev_entry.entry_number}, skipping')

                                latest_by_key = {}
                                for rev_entry in pending_cancel:
                                    ref = (rev_entry.reference or '').strip()
                                    if ref == inv.inv_number:
                                        key = f"INV:{inv.inv_number}"
                                    elif ref.startswith(f"COGS-{inv.inv_number}") or f"COGS-{inv.inv_number}" in ref:
                                        key = f"COGS:{inv.inv_number}"
                                    elif "Reversal-PAY-" in ref and f"-INV-{inv.inv_number}" in ref:
                                        # keep latest per payment reversal reference
                                        key = f"PAY:{ref}"
                                    else:
                                        key = f"OTHER:{ref}"

                                    current = latest_by_key.get(key)
                                    if current is None or rev_entry.id > current.id:
                                        latest_by_key[key] = rev_entry

                                entries_to_cancel = sorted(latest_by_key.values(), key=lambda e: e.id, reverse=True)
                                logger.info(f'=== After latest-only dedup: will cancel {len(entries_to_cancel)} entries ===')
                                
                                for rev_entry in entries_to_cancel:
                                    try:
                                        logger.info(f'Creating reversal for journal {rev_entry.entry_number} (ref: {rev_entry.reference})')
                                        # Create a reversal entry for this reversal entry (reversal of reversal)
                                        entry_number = _next_jv()
                                        
                                        cancel_entry = JournalEntry.objects.using(company_db).create(
                                            entry_number=entry_number,
                                            date=sr.date,
                                            reference=f"Cancel-{sr.return_number}-{inv.inv_number}",
                                            narration=f"Reversal of Reversal (Cancelled Return {sr.return_number}) - {rev_entry.entry_number}",
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
                                logger.warning(f'No reversal entries found for return {sr.return_number} with narration containing "Sales Return {sr.return_number}"')
                        except Exception as e:
                            logger.exception(f'Failed to reverse journal entries for return cancellation: {str(e)}')

                    # Reverse stock if it was updated (remove the qty added by return)
                    if sr.stock_updated:
                        for ritem in sr.items.all():
                            try:
                                stock = Stock.objects.get(item=ritem.invoice_item.product, warehouse=sr.warehouse)
                                # reduce stock
                                try:
                                    qty = Decimal(str(ritem.quantity_returned or 0))
                                except Exception:
                                    qty = Decimal('0')
                                try:
                                    before_qty = Decimal(str(stock.quantity or 0))
                                except Exception:
                                    before_qty = Decimal('0')
                                stock.quantity = before_qty - qty
                                stock.save()

                                # record stock movement (out)
                                try:
                                    SalesStockMovement.objects.create(
                                        stock=stock,
                                        movement_type='out',
                                        quantity=ritem.quantity_returned,
                                        reference_type='sales_return_reversal',
                                        reference_id=sr.id,
                                        sales_return=sr,
                                        notes=f"Outward for cancelled Sales Return {sr.return_number}",
                                        created_by=request.user
                                    )
                                except Exception:
                                    logger.exception('Failed to create stock movement for return cancellation')
                            except Exception:
                                logger.exception('Failed to reverse stock for return item')

                        sr.stock_updated = False
                        sr.save(update_fields=['stock_updated'])

                messages.success(request, f'Sales Return {sr.return_number} cancelled successfully!')
                
                # Check if AJAX request - redirect back to the same return detail page
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    from django.urls import reverse
                    try:
                        if hasattr(request, 'company_code') and request.company_code:
                            detail_url = reverse('sales_return_detail', args=[request.company_code, pk])
                        else:
                            detail_url = reverse('sales_return_detail', args=[pk])
                    except Exception:
                        detail_url = f'/sales/return/{pk}/'

                    return JsonResponse({
                        'success': True,
                        'message': f'Sales Return {sr.return_number} cancelled successfully!',
                        'redirect_url': detail_url
                    })
            except Exception as e:
                error_msg = f'Error cancelling return: {str(e)}'
                messages.error(request, error_msg)
                
                # Check if AJAX request
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False,
                        'message': error_msg
                    }, status=400)

    return redirect_with_company('sales_return_detail', pk=pk)


def sales_return_mark_received(request, pk):
    """Mark a sales return as received (increase warehouse stock)."""
    # Permission: require Edit on Sales Delivery (reuse delivery permission)
    if not (getattr(request.user, 'is_superuser', False) or can_edit_delivery(request.user)):
        messages.error(request, 'You do not have permission to mark Sales Return as received.')
        return redirect_with_company('sales_return_detail', pk=pk)

    sr = get_object_or_404(SalesReturn, pk=pk)

    if request.method == 'POST':
        # Check period lock before marking received
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(sr.date, request.user, db=db, transaction_type='sales_return')
        except PermissionDenied as e:
            # Extract lock details for modal display
            period_lock_error = None
            try:
                fiscal_year = FiscalYear.objects.using(db).filter(
                    start_date__lte=sr.date,
                    end_date__gte=sr.date,
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
                        'transaction_type': 'sales_return',
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
            return redirect_with_company('sales_return_detail', pk=pk)

        if sr.status == 'received':
            messages.warning(request, 'This return is already marked as received.')
        else:
            try:
                from stock.models import Stock
                with transaction.atomic():
                    sr.status = 'received'
                    
                    # Calculate refund amount from returned items
                    refund_total = Decimal('0')
                    for ritem in sr.items.all():
                        try:
                            qty = Decimal(str(ritem.quantity_returned or 0))
                            price = Decimal(str(ritem.invoice_item.price or 0))
                            tax_rate = Decimal(str(ritem.invoice_item.prd_tax or 0))
                            # amount = quantity * price * (1 + tax/100)
                            item_amount = qty * price * (1 + (tax_rate / 100))
                            refund_total += item_amount
                        except Exception:
                            pass
                    
                    sr.refund_amount = refund_total
                    sr.save()

                    # Increase stock for each returned item
                    success_count = 0
                    fail_count = 0
                    for ritem in sr.items.all():
                        try:
                            if not sr.warehouse:
                                logger.warning('No warehouse for SalesReturn %s, skipping stock update', sr.id)
                                fail_count += 1
                                continue

                            stock, created = Stock.objects.get_or_create(
                                item=ritem.invoice_item.product,
                                warehouse=sr.warehouse,
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

                            stock.quantity = before_qty + qty
                            stock.save()
                            logger.info('Updated stock id=%s item=%s warehouse=%s before=%s after=%s', getattr(stock, 'id', None), getattr(stock.item, 'id', None), getattr(stock.warehouse, 'id', None), before_qty, stock.quantity)

                            # record stock movement (in)
                            try:
                                SalesStockMovement.objects.create(
                                    stock=stock,
                                    movement_type='in',
                                    quantity=ritem.quantity_returned,
                                    reference_type='sales_return',
                                    reference_id=sr.id,
                                    sales_return=sr,
                                    notes=f"Stock in from Sales Return {sr.return_number}",
                                    created_by=request.user
                                )
                                success_count += 1
                            except Exception:
                                fail_count += 1
                                logger.exception('Failed to create stock movement for return receive for return id=%s item=%s', sr.id, getattr(ritem.invoice_item.product, 'id', None))
                        except Exception:
                            fail_count += 1
                            logger.exception('Failed to update stock for return item for return id=%s', sr.id)

                    sr.stock_updated = True
                    sr.save(update_fields=['stock_updated'])

                    # Create reversal journal entries when marked as received
                    if sr.sales_invoice:
                        try:
                            from journal.models import JournalEntry, JournalLine
                            
                            def _next_jv():
                                """Generate the next journal entry number."""
                                company_db = getattr(request, 'company_db', 'default')
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
                            
                            inv = sr.sales_invoice
                            company_db = getattr(request, 'company_db', 'default')
                            
                            # Calculate proportion of return vs total invoice first (used for all reversals)
                            proportion = Decimal('1.00')
                            try:
                                invoice_items = SalesInvoiceItem.objects.filter(sales_inv=inv)
                                invoice_taxable_total = Decimal('0.00')
                                for it in invoice_items:
                                    p = Decimal(it.price or 0)
                                    q = Decimal(it.quantity or 0)
                                    tr = Decimal(getattr(it, 'prd_tax', 0) or 0)
                                    invoice_taxable_total += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))

                                returned_items = sr.items.all()
                                returned_taxable = Decimal('0.00')
                                for ri in returned_items:
                                    p = Decimal(ri.invoice_item.price or 0)
                                    q = Decimal(ri.quantity_returned or 0)
                                    tr = Decimal(getattr(ri.invoice_item, 'prd_tax', 0) or 0)
                                    returned_taxable += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))
                                
                                if invoice_taxable_total and returned_taxable and invoice_taxable_total > 0:
                                    proportion = (returned_taxable / invoice_taxable_total).quantize(Decimal('0.0001'))
                                    if proportion > 1:
                                        proportion = Decimal('1.00')
                            except Exception:
                                proportion = Decimal('1.00')
                            
                            # Reverse invoice journal
                            old_invoice_entry = JournalEntry.objects.using(company_db).filter(
                                reference=inv.inv_number
                            ).exclude(
                                narration__icontains='Reversal'
                            ).prefetch_related('lines').order_by('-id').first()
                            
                            if old_invoice_entry:
                                entry_number = _next_jv()
                                rev = JournalEntry.objects.using(company_db).create(
                                    entry_number=entry_number,
                                    date=sr.date,
                                    reference=old_invoice_entry.reference,
                                    narration=f"Reversal: {old_invoice_entry.entry_number} (Sales Return {sr.return_number})",
                                    created_by=request.user,
                                    updated_by=request.user,
                                    status='posted'
                                )
                                
                                for line in old_invoice_entry.lines.all():
                                    try:
                                        rev_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                                        rev_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
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
                                    rev.total_debit = sum(Decimal(str(l.debit or 0)) for l in rev.lines.all())
                                    rev.total_credit = sum(Decimal(str(l.credit or 0)) for l in rev.lines.all())
                                    rev.save(update_fields=['total_debit', 'total_credit'])
                                    logger.info(f'Created reversal journal {rev.entry_number} for sales return {sr.return_number}')
                                except Exception as e:
                                    logger.exception(f'Failed to update totals for reversal journal: {str(e)}')
                            
                            # Reverse COGS journal (if any)
                            old_cogs_entry = JournalEntry.objects.using(company_db).filter(
                                reference=f"COGS-{inv.inv_number}"
                            ).exclude(
                                narration__icontains='Reversal'
                            ).prefetch_related('lines').order_by('-id').first()
                            
                            if old_cogs_entry:
                                entry_number = _next_jv()
                                revc = JournalEntry.objects.using(company_db).create(
                                    entry_number=entry_number,
                                    date=sr.date,
                                    reference=old_cogs_entry.reference,
                                    narration=f"Reversal: {old_cogs_entry.entry_number} (Sales Return {sr.return_number})",
                                    created_by=request.user,
                                    updated_by=request.user,
                                    status='posted'
                                )
                                for line in old_cogs_entry.lines.all():
                                    try:
                                        rev_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                                        rev_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
                                    except Exception:
                                        rev_debit = line.credit
                                        rev_credit = line.debit

                                    JournalLine.objects.using(company_db).create(
                                        journal=revc,
                                        account=line.account,
                                        description=f"Reversal: {line.description}",
                                        debit=rev_debit,
                                        credit=rev_credit,
                                        sequence=line.sequence
                                    )
                                try:
                                    revc.total_debit = sum(Decimal(str(l.debit or 0)) for l in revc.lines.all())
                                    revc.total_credit = sum(Decimal(str(l.credit or 0)) for l in revc.lines.all())
                                    revc.save(update_fields=['total_debit', 'total_credit'])
                                    logger.info(f'Created COGS reversal journal {revc.entry_number} for sales return {sr.return_number}')
                                except Exception as e:
                                    logger.exception(f'Failed to update totals for COGS reversal journal: {str(e)}')
                            
                            # Reverse payment allocations
                            alloc_entries = InvPaymentAllocation.objects.using(company_db).filter(
                                inv=inv
                            )
                            
                            remaining_refund = sr.refund_amount
                            for alloc in alloc_entries:
                                try:
                                    amt = alloc.amount or Decimal('0.00')
                                    if amt <= 0:
                                        continue
                                    payment_obj = getattr(alloc, 'payment', None)
                                    paid_through_acct = getattr(payment_obj, 'paid_through', None) if payment_obj else None
                                    debt_acct = ChartOfAccounts.objects.using(company_db).filter(name__icontains='Debtors').first()
                                    if not debt_acct:
                                        debt_acct = ChartOfAccounts.objects.using(company_db).filter(code='1020101').first()

                                    if paid_through_acct and debt_acct:
                                        # Use refund amount to allocate
                                        try:
                                            if remaining_refund is not None:
                                                if remaining_refund <= 0:
                                                    alloc_reverse_amt = Decimal('0.00')
                                                else:
                                                    alloc_reverse_amt = min(amt, remaining_refund)
                                                    remaining_refund = (remaining_refund - alloc_reverse_amt).quantize(Decimal('0.01'))
                                            else:
                                                alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))
                                        except Exception:
                                            alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))

                                        if alloc_reverse_amt and alloc_reverse_amt > 0:
                                            alloc_reverse_base = scale_amount_for_journal(alloc_reverse_amt, inv)
                                            entry_number = _next_jv()
                                            pay_rev = JournalEntry.objects.using(company_db).create(
                                                entry_number=entry_number,
                                                date=sr.date,
                                                reference=f"Reversal-PAY-{getattr(payment_obj, 'payment_number', '')}-INV-{inv.inv_number}",
                                                narration=f"Reversal of payment allocation for Invoice {inv.inv_number} (Sales Return {sr.return_number})",
                                                created_by=request.user,
                                                updated_by=request.user,
                                                status='posted'
                                            )
                                            # Debit Debtors (increase receivable)
                                            JournalLine.objects.using(company_db).create(
                                                journal=pay_rev,
                                                account=debt_acct,
                                                description=f"Reversal alloc: Invoice {inv.inv_number}",
                                                debit=alloc_reverse_base,
                                                credit=Decimal('0.00'),
                                                sequence=10
                                            )
                                            # Credit Bank/Cash (reduce cash)
                                            JournalLine.objects.using(company_db).create(
                                                journal=pay_rev,
                                                account=paid_through_acct,
                                                description=f"Reversal alloc: Payment #{getattr(payment_obj, 'payment_number', '')}",
                                                debit=Decimal('0.00'),
                                                credit=alloc_reverse_base,
                                                sequence=20
                                            )
                                            try:
                                                pay_rev.total_debit = sum(Decimal(str(l.debit or 0)) for l in pay_rev.lines.all())
                                                pay_rev.total_credit = sum(Decimal(str(l.credit or 0)) for l in pay_rev.lines.all())
                                                pay_rev.save(update_fields=['total_debit', 'total_credit'])
                                                logger.info(f'Created payment reversal journal {pay_rev.entry_number} for sales return {sr.return_number}')
                                            except Exception as e:
                                                logger.exception(f'Failed to update totals for payment reversal: {str(e)}')
                                except Exception as e:
                                    logger.exception(f'Failed to create reversal for allocation: {str(e)}')
                        except Exception as e:
                            logger.exception(f'Failed to create reversal journal entries for received return: {str(e)}')

                if success_count > 0:
                    messages.success(request, f'Sales Return {sr.return_number} marked as received and {success_count} stock movement(s) created.')
                else:
                    messages.warning(request, f'Sales Return {sr.return_number} marked as received but no stock movements were created.')

                if fail_count > 0:
                    messages.error(request, f'{fail_count} stock update/movement operation(s) failed ₹ check server logs.')
                
                # Check if AJAX request
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    from django.urls import reverse
                    try:
                        if hasattr(request, 'company_code') and request.company_code:
                            detail_url = reverse('sales_return_detail', args=[request.company_code, pk])
                        else:
                            detail_url = reverse('sales_return_detail', args=[pk])
                    except:
                        detail_url = f'/sales/return/{pk}/'
                    
                    return JsonResponse({
                        'success': True,
                        'message': f'Sales Return {sr.return_number} marked as received successfully!',
                        'redirect_url': detail_url
                    })
            except Exception as e:
                error_msg = f'Error updating return: {str(e)}'
                messages.error(request, error_msg)
                
                # Check if AJAX request
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': False,
                        'message': error_msg
                    }, status=400)

    return redirect_with_company('sales_return_detail', pk=pk)





class SalesReturnUpdateView(UpdateView):
    model = SalesReturn
    form_class = SalesReturnForm
    template_name = 'sales/sales_return_form.html'
    success_url = 'sales_return_list'
    
    def dispatch(self, request, *args, **kwargs):
        # Permission: require Edit on Sales Delivery
        if not (getattr(request.user, 'is_superuser', False) or can_edit_delivery(request.user)):
            messages.error(request, 'You do not have permission to edit Sales Returns.')
            return redirect_with_company('sales_return_list')
        return super().dispatch(request, *args, **kwargs)
    
    def get_queryset(self):
        # Only allow editing if stock hasn't been updated
        return SalesReturn.objects.filter(stock_updated=False)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Warehouse is fixed for the return; do not allow changing on edit.
        if 'warehouse' in form.fields:
            form.fields['warehouse'].disabled = True
            form.fields['warehouse'].required = False
        return form
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Check if period is locked for Sales Return
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
                
                if fiscal_year and fiscal_year.lock_sales_return:
                    context['period_locked'] = True
                    context['fiscal_year_name'] = fiscal_year.name
                else:
                    context['period_locked'] = False
            except Exception:
                context['period_locked'] = False
        
        # Get all invoice items for this return's invoice
        invoice_items = SalesInvoiceItem.objects.filter(sales_inv=self.object.sales_invoice)
        
        # Create a mapping of invoice_item_id to return quantity for quick lookup
        return_items_map = {}
        for ri in self.object.items.all():
            return_items_map[ri.invoice_item.id] = ri
        
        # Calculate already returned quantities from OTHER returns (not this one)
        already_returned_map = {}
        for ii in invoice_items:
            # Sum all returned quantities from other sales returns (excluding this one)
            other_returns = SalesReturnItem.objects.filter(
                invoice_item=ii,
                sales_return__status__in=['pending', 'received']
            ).exclude(sales_return=self.object).aggregate(total=Sum('quantity_returned'))['total'] or 0
            already_returned_map[ii.id] = other_returns
        
        # For each invoice item, check if it has a return record in THIS return
        # If it does, use that SalesReturnItem; otherwise create a placeholder
        items_for_template = []
        for ii in invoice_items:
            if ii.id in return_items_map:
                # Use the existing SalesReturnItem
                item_obj = return_items_map[ii.id]
            else:
                # Create a temporary SalesReturnItem-like object with 0 quantity for display
                item_obj = SalesReturnItem(
                    sales_return=self.object,
                    invoice_item=ii,
                    quantity_returned=0
                )
            # Attach the already_returned info (without underscore for template compatibility)
            item_obj.already_returned_qty = already_returned_map[ii.id]
            items_for_template.append(item_obj)
        
        context['existing_items'] = items_for_template
        return context
    
    def form_valid(self, form):
        from stock.models import Stock
        import json
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        from system_settings.models import FiscalYear
        
        persisted_sr = SalesReturn.objects.get(pk=self.object.pk)
        old_status = persisted_sr.status
        old_stock_updated = persisted_sr.stock_updated
        
        # Check period lock before updating
        db = getattr(self.request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(self.object.date, self.request.user, db=db, transaction_type='sales_return')
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
                        'transaction_type': 'sales_return',
                        'locked_by': fiscal_year.locked_by.username if fiscal_year.locked_by else 'Admin',
                        'lock_date': fiscal_year.lock_date.strftime('%d %b %Y, %I:%M %p') if fiscal_year.lock_date else 'N/A',
                        'locked_areas': ', '.join(sorted(set(locked_fields))),
                    }
            except Exception:
                pass
            
            return self.form_invalid(form)
        
        with transaction.atomic():
            # Enforce fixed warehouse from persisted return record.
            self.object.warehouse = persisted_sr.warehouse
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
            
            # Get invoice items to check against selected items
            invoice = self.object.sales_invoice
            all_invoice_items = SalesInvoiceItem.objects.filter(sales_inv=invoice)
            
            # Create a map of existing return items by invoice_item_id
            existing_return_items = {}
            for ri in self.object.items.all():
                existing_return_items[ri.invoice_item.id] = ri
            
            # Process selected items - create or update SalesReturnItem records
            for invoice_item in all_invoice_items:
                if str(invoice_item.id) in [str(x) for x in selected_items]:
                    # This item was selected for return
                    new_qty = int(return_quantities.get(str(invoice_item.id), 0))
                    
                    if invoice_item.id in existing_return_items:
                        # Update existing return item
                        return_item = existing_return_items[invoice_item.id]
                        return_item.quantity_returned = new_qty
                        return_item.save()
                    else:
                        # Create new return item
                        SalesReturnItem.objects.create(
                            sales_return=self.object,
                            invoice_item=invoice_item,
                            quantity_returned=new_qty
                        )
                else:
                    # This item was NOT selected - delete or set to 0
                    if invoice_item.id in existing_return_items:
                        # Delete the return item if it existed
                        existing_return_items[invoice_item.id].delete()
            
            # If status changed from cancelled to received/pending, or from pending to received
            # We need to handle stock movements and journal entries
            try:
                # If transitioning to received status
                if self.object.status == 'received' and old_status != 'received':
                    # Calculate refund amount from returned items
                    refund_total = Decimal('0')
                    for ritem in self.object.items.all():
                        try:
                            qty = Decimal(str(ritem.quantity_returned or 0))
                            price = Decimal(str(ritem.invoice_item.price or 0))
                            tax_rate = Decimal(str(ritem.invoice_item.prd_tax or 0))
                            # amount = quantity * price * (1 + tax/100)
                            item_amount = qty * price * (1 + (tax_rate / 100))
                            refund_total += item_amount
                        except Exception:
                            pass
                    
                    self.object.refund_amount = refund_total
                    self.object.save(update_fields=['refund_amount'])
                    
                    # Mark as stock updated and create stock movements for SELECTED items only
                    success_count = 0
                    fail_count = 0
                    
                    for ritem in self.object.items.all():
                        # Skip items with no quantity_returned (not selected)
                        if not ritem.quantity_returned or ritem.quantity_returned == 0:
                            continue
                        try:
                            if not self.object.warehouse:
                                logger.warning('No warehouse for SalesReturn %s, skipping stock update', self.object.id)
                                fail_count += 1
                                continue
                            
                            stock, created = Stock.objects.get_or_create(
                                item=ritem.invoice_item.product,
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
                            
                            stock.quantity = before_qty + qty
                            stock.save()
                            
                            # Create stock movement record
                            try:
                                SalesStockMovement.objects.create(
                                    stock=stock,
                                    movement_type='in',
                                    quantity=ritem.quantity_returned,
                                    reference_type='sales_return',
                                    reference_id=self.object.id,
                                    sales_return=self.object,
                                    notes=f"Stock in from Sales Return {self.object.return_number}",
                                    created_by=self.request.user
                                )
                                success_count += 1
                            except Exception:
                                fail_count += 1
                                logger.exception('Failed to create stock movement for return id=%s', self.object.id)
                        except Exception:
                            fail_count += 1
                            logger.exception('Failed to update stock for return item for return id=%s', self.object.id)
                    
                    self.object.stock_updated = True
                    self.object.save(update_fields=['stock_updated'])
                    
                    # --- Create journal entries when transitioning to received ---
                    try:
                        self._create_return_journal_entries()
                    except Exception:
                        logger.exception('Failed to create journal entries for sales return %s', self.object.id)
                        messages.warning(self.request, f'Sales return created but journal entries failed.')
                    
                    if success_count > 0:
                        messages.success(self.request, f'Sales Return {self.object.return_number} updated and {success_count} stock movement(s) created.')
                    if fail_count > 0:
                        messages.warning(self.request, f'{fail_count} stock update/movement operation(s) had issues.')
                
                # If status changed back to pending/cancelled from received
                elif self.object.status != 'received' and old_status == 'received' and old_stock_updated:
                    # Reverse stock movements only for items that have them
                    for ritem in self.object.items.all():
                        # Skip items with no quantity_returned (they weren't part of the return)
                        if not ritem.quantity_returned or ritem.quantity_returned == 0:
                            continue
                        try:
                            stock = Stock.objects.get(
                                item=ritem.invoice_item.product,
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
                            
                            stock.quantity = before_qty - qty
                            stock.save()
                            
                            # Create reversal stock movement record
                            try:
                                SalesStockMovement.objects.create(
                                    stock=stock,
                                    movement_type='out',
                                    quantity=ritem.quantity_returned,
                                    reference_type='sales_return_reversal',
                                    reference_id=self.object.id,
                                    sales_return=self.object,
                                    notes=f"Stock reversal for cancelled/pending Sales Return {self.object.return_number}",
                                    created_by=self.request.user
                                )
                            except Exception:
                                logger.exception('Failed to create reversal stock movement for return id=%s', self.object.id)
                        except Exception:
                            logger.exception('Failed to reverse stock for return item for return id=%s', self.object.id)
                    
                    self.object.stock_updated = False
                    self.object.save(update_fields=['stock_updated'])
                    
                    # --- Reverse journal entries when transitioning back from received ---
                    try:
                        self._reverse_return_journal_entries()
                    except Exception:
                        logger.exception('Failed to reverse journal entries for sales return %s', self.object.id)
                        messages.warning(self.request, f'Sales return status changed but journal entry reversal failed.')
                    
                    messages.success(self.request, f'Sales Return {self.object.return_number} updated and stock movements reversed.')
                else:
                    messages.success(self.request, f'Sales Return {self.object.return_number} updated successfully!')
                
                # Check if AJAX request
                if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    # Get the redirect URL
                    from django.urls import reverse
                    try:
                        if hasattr(self.request, 'company_code') and self.request.company_code:
                            list_url = reverse('sales_return_list', args=[self.request.company_code])
                        else:
                            list_url = reverse('sales_return_list')
                    except:
                        list_url = '/sales/returns/'
                    
                    return JsonResponse({
                        'success': True,
                        'message': f'Sales Return {self.object.return_number} updated successfully!',
                        'redirect_url': list_url
                    })
                
            except Exception as e:
                messages.error(self.request, f'Error updating return: {str(e)}')
                logger.exception('Error in SalesReturnUpdateView form_valid')
                return self.form_invalid(form)
            
            return redirect_with_company(self.success_url)
    
    def form_invalid(self, form):
        """Handle invalid form submission with AJAX support."""
        # Check if there's a period lock error
        if hasattr(self.request, 'period_lock_error') and self.request.period_lock_error:
            error_msg = (
                f"₹ Period Locked: The fiscal year is locked for editing (sales_return).<br>"
                f"<i class='bi bi-lock-fill'></i> Locked by: {self.request.period_lock_error.get('locked_by', 'Admin')}<br>"
                f"<i class='bi bi-calendar'></i> Lock Date: {self.request.period_lock_error.get('lock_date', 'N/A')}<br>"
                f"<i class='bi bi-info-circle'></i> Locked areas: {self.request.period_lock_error.get('locked_areas', 'N/A')}<br><br>"
                f"Contact your administrator to unlock this period if needed."
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

    def _create_return_journal_entries(self):
        """Create journal entries for sales return (reverse invoice, COGS, and payment allocations)"""
        sr = self.object
        inv = sr.sales_invoice
        
        def _next_jv():
            last = JournalEntry.objects.order_by('-id').first()
            if last and last.entry_number and last.entry_number.startswith('JV-'):
                try:
                    last_num = int(last.entry_number.split('-')[1])
                except Exception:
                    last_num = 0
            else:
                last_num = 0
            return f"JV-{str(last_num + 1).zfill(5)}"
        
        rev_date = getattr(sr, 'date', None) or timezone.now().date()
        
        # Reverse invoice journal (scale proportionally for partial returns)
        old_invoice_entry = JournalEntry.objects.filter(reference=inv.inv_number).exclude(narration__startswith='Reversal of').prefetch_related('lines').order_by('-id').first()
        if old_invoice_entry:
            try:
                invoice_items = SalesInvoiceItem.objects.filter(sales_inv=inv)
                invoice_taxable_total = Decimal('0.00')
                for it in invoice_items:
                    p = Decimal(it.price or 0)
                    q = Decimal(it.quantity or 0)
                    tr = Decimal(getattr(it, 'prd_tax', 0) or 0)
                    invoice_taxable_total += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))
                
                returned_items = SalesReturnItem.objects.filter(sales_return=sr).select_related('invoice_item')
                returned_taxable = Decimal('0.00')
                for ri in returned_items:
                    p = Decimal(ri.invoice_item.price or 0)
                    q = Decimal(ri.quantity_returned or 0)
                    tr = Decimal(getattr(ri.invoice_item, 'prd_tax', 0) or 0)
                    returned_taxable += (p * q) * (Decimal('1.00') + (tr / Decimal('100.00')))
            except Exception:
                invoice_taxable_total = Decimal('0.00')
                returned_taxable = Decimal('0.00')
            
            proportion = Decimal('1.00')
            if invoice_taxable_total and returned_taxable and invoice_taxable_total > 0:
                proportion = (returned_taxable / invoice_taxable_total).quantize(Decimal('0.0001'))
                if proportion > 1:
                    proportion = Decimal('1.00')
            
            entry_number = _next_jv()
            rev = JournalEntry.objects.create(
                entry_number=entry_number,
                date=rev_date,
                reference=old_invoice_entry.reference,
                narration=f"Reversal of {old_invoice_entry.entry_number} - Sales Return {sr.return_number}",
                created_by=self.request.user,
                updated_by=self.request.user,
                status='posted'
            )
            for line in old_invoice_entry.lines.all():
                try:
                    rev_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                    rev_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
                except Exception:
                    rev_debit = line.credit
                    rev_credit = line.debit
                
                JournalLine.objects.create(
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
                logger.exception('Failed to update totals for invoice reversal %s', getattr(rev, 'id', None))
        
        # Reverse COGS journal (if any)
        old_cogs_entry = JournalEntry.objects.filter(reference=f"COGS-{inv.inv_number}").exclude(narration__startswith='Reversal of').prefetch_related('lines').order_by('-id').first()
        if old_cogs_entry:
            entry_number = _next_jv()
            revc = JournalEntry.objects.create(
                entry_number=entry_number,
                date=rev_date,
                reference=old_cogs_entry.reference,
                narration=f"Reversal of {old_cogs_entry.entry_number} - Sales Return {sr.return_number}",
                created_by=self.request.user,
                updated_by=self.request.user,
                status='posted'
            )
            for line in old_cogs_entry.lines.all():
                try:
                    revc_debit = (Decimal(line.credit or 0) * proportion).quantize(Decimal('0.01'))
                    revc_credit = (Decimal(line.debit or 0) * proportion).quantize(Decimal('0.01'))
                except Exception:
                    revc_debit = line.credit
                    revc_credit = line.debit
                
                JournalLine.objects.create(
                    journal=revc,
                    account=line.account,
                    description=f"Reversal: {line.description}",
                    debit=revc_debit,
                    credit=revc_credit,
                    sequence=line.sequence
                )
            try:
                revc.total_debit = sum(l.debit or 0 for l in revc.lines.all())
                revc.total_credit = sum(l.credit or 0 for l in revc.lines.all())
                revc.save(update_fields=['total_debit', 'total_credit'])
            except Exception:
                logger.exception('Failed to update totals for COGS reversal %s', getattr(revc, 'id', None))
        
        # Reverse payment allocations
        allocations = InvPaymentAllocation.objects.filter(inv=inv).select_related('payment')
        try:
            remaining_refund = Decimal(sr.refund_amount) if (getattr(sr, 'refund_amount', None) is not None and getattr(sr, 'refund_amount', 0) != 0) else None
        except Exception:
            remaining_refund = None
        
        for alloc in allocations:
            try:
                amt = alloc.amount or Decimal('0.00')
                if amt <= 0:
                    continue
                payment_obj = getattr(alloc, 'payment', None)
                paid_through_acct = getattr(payment_obj, 'paid_through', None) if payment_obj else None
                debt_acct = ChartOfAccounts.objects.filter(name__icontains='Debtors').first()
                if not debt_acct:
                    debt_acct = ChartOfAccounts.objects.filter(code='1020101').first()
                
                if paid_through_acct and debt_acct:
                    try:
                        if remaining_refund is not None:
                            if remaining_refund <= 0:
                                alloc_reverse_amt = Decimal('0.00')
                            else:
                                alloc_reverse_amt = min(amt, remaining_refund)
                                remaining_refund = (remaining_refund - alloc_reverse_amt).quantize(Decimal('0.01'))
                        else:
                            alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))
                    except Exception:
                        alloc_reverse_amt = (Decimal(amt) * proportion).quantize(Decimal('0.01'))
                    
                    if alloc_reverse_amt and alloc_reverse_amt > 0:
                        alloc_reverse_base = scale_amount_for_journal(alloc_reverse_amt, inv)
                        entry_number = _next_jv()
                        pay_rev = JournalEntry.objects.create(
                            entry_number=entry_number,
                            date=rev_date,
                            reference=f"Reversal-PAY-{getattr(payment_obj, 'payment_number', '')}-INV-{inv.inv_number}",
                            narration=f"Reversal of payment allocation for Invoice {inv.inv_number} - Sales Return {sr.return_number}",
                            created_by=self.request.user,
                            updated_by=self.request.user,
                            status='posted'
                        )
                        JournalLine.objects.create(
                            journal=pay_rev,
                            account=debt_acct,
                            description=f"Reversal alloc: Invoice {inv.inv_number}",
                            debit=alloc_reverse_base,
                            credit=Decimal('0.00'),
                            sequence=10
                        )
                        JournalLine.objects.create(
                            journal=pay_rev,
                            account=paid_through_acct,
                            description=f"Reversal alloc: Payment #{getattr(payment_obj, 'payment_number', '')}",
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
    
    def _reverse_return_journal_entries(self):
        """Reverse journal entries when sales return status changes back from received"""
        sr = self.object
        inv = sr.sales_invoice
        
        # Find and delete all journal entries that were created to reverse this invoice
        # These entries have:
        # 1. Narration starting with "Reversal of" (indicating they are reversal entries)
        # 2. Reference containing the invoice number
        # 3. Created after this sales return was created
        try:
            # Find all reversal entries for this invoice that were created after this return
            entries_to_delete = JournalEntry.objects.filter(
                narration__startswith='Reversal of',
                reference__icontains=inv.inv_number,
                created_at__gte=sr.created_at if hasattr(sr, 'created_at') else timezone.now() - timezone.timedelta(hours=1)
            ).order_by('-created_at')
            
            deleted_count = 0
            for entry in entries_to_delete:
                try:
                    # Delete all journal lines for this entry
                    entry.lines.all().delete()
                    # Delete the entry itself
                    entry.delete()
                    deleted_count += 1
                except Exception as e:
                    logger.exception('Error deleting journal entry %s: %s', entry.id, str(e))
            
            if deleted_count > 0:
                logger.info('Deleted %d journal reversal entries for sales return %s', deleted_count, sr.id)
        except Exception:
            logger.exception('Failed to reverse journal entries for sales return %s', sr.id)


def performa_invoice_detail(request, pk):
    # Permission: require View on Sales Performa Invoice
    if not (getattr(request.user, 'is_superuser', False) or can_view_performa_invoice(request.user)):
        messages.error(request, 'You do not have permission to view Sales Performa Invoice.')
        return redirect_with_company('sales_index')
    
    context = build_performa_context(pk, request=request)
    return render(request, 'sales/performa_invoice_detail.html', context)


def performa_invoice_print_view(request, pk):
    # Permission: require View on Sales Performa Invoice
    if not (getattr(request.user, 'is_superuser', False) or can_view_performa_invoice(request.user)):
        messages.error(request, 'You do not have permission to view Sales Performa Invoice.')
        return redirect_with_company('sales_index')
    
    context = build_performa_context(pk, request=request)
    return render(request, 'sales/performa_invoice_print.html', context)


def performa_invoice_pdf_view(request, pk):
    """Generate PDF bytes for a Performa Invoice and return as HTTP response."""
    # Permission: require View on Sales Performa Invoice
    if not (getattr(request.user, 'is_superuser', False) or can_view_performa_invoice(request.user)):
        messages.error(request, 'You do not have permission to view Sales Performa Invoice.')
        return redirect_with_company('sales_index')

    try:
        pdf_bytes = generate_performa_invoice_pdf_bytes(pk)
    except Exception:
        # Fallback: render printable HTML if PDF generation fails
        context = build_performa_context(pk, request=request)
        return render(request, 'sales/performa_invoice_print.html', context)

    from django.http import HttpResponse
    inv = get_object_or_404(PerformaInvoice, pk=pk)
    resp = HttpResponse(pdf_bytes, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="performa_invoice_{inv.inv_number}.pdf"'
    return resp


def _get_company_for_request(request):
    """
    Resolve the active Company record for the current request/tenant.
    Falls back to the default DB if tenant DB has no row.
    """
    try:
        if request is None:
            return Company.objects.filter(status=True).first() or Company.objects.first()

        company_db = getattr(request, 'company_db', None) or request.session.get('company_db') if hasattr(request, 'session') else None
        company_code = getattr(request, 'company_code', None) or (request.session.get('company_code') if hasattr(request, 'session') else None)

        # Prefer tenant DB when available
        if company_db and company_db != 'default':
            qs = Company.objects.using(company_db).filter(status=True)
            if company_code:
                obj = qs.filter(company_code=company_code).first()
                if obj:
                    return obj
            obj = qs.first()
            if obj:
                return obj

        # Fallback to master/default DB
        qs = Company.objects.using('default').filter(status=True)
        if company_code:
            obj = qs.filter(company_code=company_code).first()
            if obj:
                return obj
        return qs.first() or Company.objects.using('default').first()
    except Exception:
        return Company.objects.filter(status=True).first() or Company.objects.first()


def _get_request_company_tax_type(request=None, company=None):
    """Resolve tax type from the active tenant context, with safe fallbacks."""
    candidates = []
    try:
        db_name = getattr(request, 'company_db', None)
        if db_name:
            candidates.append(db_name)
    except Exception:
        pass
    try:
        session_db = request.session.get('company_db') if request is not None and hasattr(request, 'session') else None
        if session_db:
            candidates.append(session_db)
    except Exception:
        pass

    for db_name in candidates:
        try:
            tax_type = (get_company_tax_type(using=db_name) or '').strip().upper()
            if tax_type:
                return tax_type
        except Exception:
            pass

    tax_type = (str(getattr(company, 'tax_type', '') or '').strip().upper() if company else '')
    if tax_type:
        return tax_type

    try:
        company_code = getattr(request, 'company_code', None) or request.session.get('company_code')
        if company_code:
            company_obj = Company.objects.using('default').filter(company_code=company_code).first()
            tax_type = str(getattr(company_obj, 'tax_type', '') or '').strip().upper() if company_obj else ''
            if tax_type:
                return tax_type
    except Exception:
        pass

    try:
        return (get_company_tax_type() or '').strip().upper()
    except Exception:
        return ''


def build_performa_context(pk, request=None):
    inv = get_object_or_404(PerformaInvoice, pk=pk)
    company = _get_company_for_request(request)
    
    from currencies.models import Currency
    base_currency = Currency.objects.filter(company=company, is_base=True).first() or Currency.objects.filter(company=company).first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    doc_currency = inv.document_currency
    doc_currency_symbol = (doc_currency.symbol or doc_currency.code or '').strip() if doc_currency else ''
    doc_currency_code = doc_currency.code if doc_currency else ''

    fx_rate = Decimal(inv.fx_rate_to_base or Decimal('1'))
    tds_tcs_type = (inv.tds_tcs_type or '').strip().lower()
    tds_tcs_amount = Decimal(str(inv.tds_tcs_amount or 0))
    tds_tcs_amount_base = tds_tcs_amount * fx_rate
    
    existing_items_qs = PerformaInvoiceItem.objects.filter(performa_inv=inv)
    converted_order = SalesOrder.objects.filter(origin_performa=inv).order_by('-id').first()
    items_info = []
    subtotal_calc = Decimal('0.00')
    total_tax = Decimal('0.00')
    total_item_discount = Decimal('0.00')

    for item in existing_items_qs:
        qty = Decimal(item.quantity or 0)
        price = Decimal(item.price or 0)
        o_price = Decimal(getattr(item, 'o_price', 0) or 0)
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

        cgst_rate = tax_rate / 2
        sgst_rate = tax_rate / 2
        cgst_amount = tax_amount / 2
        sgst_amount = tax_amount / 2

        # — NEW: Base currency calculations anchored to o_price
        price_base = o_price if o_price else (price * fx_rate)
        base_base = qty * price_base
        
        if item.prd_distype == 'percent':
            discount_amount_base = (base_base * discount_val) / Decimal('100')
        else:
            discount_amount_base = discount_val * fx_rate
        if discount_amount_base > base_base:
            discount_amount_base = base_base
            
        discounted_base = base_base - discount_amount_base
        if discounted_base < 0:
            discounted_base = Decimal('0.00')
            
        tax_amount_base = (discounted_base * tax_rate) / Decimal('100') if tax_rate else Decimal('0.00')
        line_total_base = discounted_base + tax_amount_base

        items_info.append({
            'product_name': getattr(item.product, 'name', ''),
            'description': getattr(item, 'description', '') or getattr(item.product, 'sales_desc', ''),
            'quantity': int(qty),
            'price': price,
            'price_base': price_base,
            'base': base,
            'discount_amount': discount_amount,
            'discount_amount_base': discount_amount_base,
            'discount_type': item.prd_distype,
            'tax_rate': tax_rate,
            'tax_amount': tax_amount,
            'tax_amount_base': tax_amount_base,
            'cgst_rate': cgst_rate,
            'cgst_amount': cgst_amount,
            'cgst_amount_base': tax_amount_base / 2 if tax_amount_base else Decimal('0.00'),
            'sgst_rate': sgst_rate,
            'sgst_amount': sgst_amount,
            'sgst_amount_base': tax_amount_base / 2 if tax_amount_base else Decimal('0.00'),
            'taxable_value': discounted,
            'line_total': line_total,
            'line_total_base': line_total_base,
            'hsn': getattr(item, 'hsn_code', '') or '',
        })

    total_cgst = (total_tax / 2) if total_tax else Decimal('0.00')
    total_sgst = (total_tax / 2) if total_tax else Decimal('0.00')

    grand_discount_value = Decimal(inv.discount_value or 0)
    grand_discount_type = inv.discount_type or 'percent'
    if grand_discount_type == 'percent':
        grand_discount = (subtotal_calc * grand_discount_value) / Decimal('100')
    else:
        grand_discount = grand_discount_value
    if grand_discount > subtotal_calc:
        grand_discount = subtotal_calc

    final_total = inv.total_amount

    # — NEW: Calculate base totals manually from items to eliminate rounding drift
    subtotal_calc_base = sum((itm['line_total_base'] for itm in items_info), Decimal('0.00'))
    total_tax_base = sum((itm['tax_amount_base'] for itm in items_info), Decimal('0.00'))
    total_item_discount_base = sum((itm.get('discount_amount_base', Decimal('0.00')) for itm in items_info), Decimal('0.00'))

    total_cgst_base = total_tax_base / Decimal('2')
    total_sgst_base = total_tax_base / Decimal('2')

    if grand_discount_type == 'percent':
        grand_discount_base = (subtotal_calc_base * grand_discount_value) / Decimal('100')
    else:
        grand_discount_base = grand_discount_value * fx_rate
    if grand_discount_base > subtotal_calc_base:
        grand_discount_base = subtotal_calc_base

    total_discount_combined_base = total_item_discount_base + grand_discount_base
    final_total_base = subtotal_calc_base - grand_discount_base
    company_tax_type = _get_request_company_tax_type(request, company)
    turnover_tax_amount = Decimal('0.00')
    turnover_tax_amount_base = Decimal('0.00')
    if company_tax_type == 'TURNOVER':
        pre_turnover_total = subtotal_calc - grand_discount
        stored_final_total = Decimal(inv.total_amount or pre_turnover_total)
        turnover_tax_amount = stored_final_total - pre_turnover_total
        if turnover_tax_amount < Decimal('0.00'):
            turnover_tax_amount = Decimal('0.00')
        final_total = stored_final_total

        pre_turnover_total_base = final_total_base
        stored_final_total_base = Decimal(getattr(inv, 'total_amount_base', None) or (stored_final_total * fx_rate))
        turnover_tax_amount_base = stored_final_total_base - pre_turnover_total_base
        if turnover_tax_amount_base < Decimal('0.00'):
            turnover_tax_amount_base = Decimal('0.00')
        final_total_base = stored_final_total_base

    # — Add company_is_india for conditional tax display
    company_is_india = _is_indian_company_country(_get_current_company_country(request))

    return {
        'invoice': inv,
        'performa_invoice': inv,
        'q_no': inv.inv_number,
        'items_info': items_info,
        'subtotal_calc': subtotal_calc,
        'total_tax': total_tax,
        'total_cgst': total_cgst,
        'total_sgst': total_sgst,
        'total_item_discount': total_item_discount,
        'grand_discount': grand_discount,
        'grand_discount_value': grand_discount_value,
        'grand_discount_type': grand_discount_type,
        'final_total': final_total,
        'total_discount_combined': total_item_discount + grand_discount,
        'total_discount_combined_base': total_discount_combined_base,
        'company': company,
        'converted_order': converted_order,
        'order_conversion_count': 1 if converted_order else 0,
        'company_is_india': company_is_india,
        'company_tax_type': company_tax_type,
        'turnover_tax_amount': turnover_tax_amount,
        'turnover_tax_amount_base': turnover_tax_amount_base,
        'subtotal_calc_base': subtotal_calc_base,
        'total_tax_base': total_tax_base,
        'total_cgst_base': total_cgst_base,
        'total_sgst_base': total_sgst_base,
        'final_total_base': final_total_base,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'document_currency_symbol': doc_currency_symbol,
        'document_currency_code': doc_currency_code,
        'document_currency': doc_currency,
        'base_currency': base_currency,
        'fx_rate': fx_rate,
        'tds_tcs_type': tds_tcs_type,
        'tds_tcs_amount': tds_tcs_amount,
        'tds_tcs_amount_base': tds_tcs_amount_base,
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    }





def generate_performa_invoice_pdf_bytes(pk):
    """Generate proforma invoice PDF bytes using the same ReportLab style as other sales PDFs."""
    import os
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
    from reportlab.lib.enums import TA_RIGHT, TA_LEFT

    inv = get_object_or_404(PerformaInvoice, pk=pk)
    context = build_performa_context(pk, request=None)

    try:
        from django.conf import settings
        static_font_path = os.path.join(settings.BASE_DIR, 'static', 'fonts', 'DejaVuSans.ttf')
        font_registered = False
        if os.path.exists(static_font_path):
            pdfmetrics.registerFont(TTFont('DejaVuSans', static_font_path))
            font_registered = True
        else:
            for font_path in [
                'C:/Windows/Fonts/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/System/Library/Fonts/Supplemental/DejaVuSans.ttf',
            ]:
                if os.path.exists(font_path):
                    pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))
                    font_registered = True
                    break
    except Exception:
        font_registered = False

    font_name = 'DejaVuSans' if font_registered else 'Helvetica'
    currency = context.get('document_currency_symbol') or ("\u20b9" if font_registered else "Rs.")
    company_is_india = context.get('company_is_india', True)
    company_tax_type = context.get('company_tax_type', 'GST')
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f'Performa Invoice {inv.inv_number}',
        author='LyraERP',
    )

    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'PerformaTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#1a1a1a'),
        spaceAfter=0,
        fontName='Helvetica-Bold',
        alignment=TA_LEFT,
    )
    badge_style = ParagraphStyle(
        'PerformaBadge',
        parent=styles['Normal'],
        fontSize=16,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT,
    )
    company_header_style = ParagraphStyle(
        'PerformaCompanyHeader',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#555555'),
        fontName=font_name,
        leading=10,
        leftIndent=10,
    )
    meta_label_style = ParagraphStyle(
        'PerformaMetaLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#7f8c8d'),
        fontName='Helvetica-Bold',
    )
    meta_value_style = ParagraphStyle(
        'PerformaMetaValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
    )
    label_style = ParagraphStyle(
        'PerformaTableLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#ffffff'),
        fontName='Helvetica-Bold',
    )
    value_style = ParagraphStyle(
        'PerformaValue',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
    )
    amount_style = ParagraphStyle(
        'PerformaAmount',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT,
    )

    company = context.get('company') or Company.objects.filter(status=True).first() or Company.objects.first()
    legal_name = (getattr(company, 'legal_name', '') or '').strip() if company else ''
    company_name_value = legal_name or ((getattr(company, 'name', '') or '').strip() if company else '')
    company_name = safe(company_name_value)

    header_left_cell = Paragraph(f"<b>{company_name}</b>", title_style)
    try:
        show_logo = bool(getattr(company, 'show_logo_in_print_pdf', False)) if company else False
        if show_logo and company and getattr(company, 'logo', None) and company.logo.path and os.path.exists(company.logo.path):
            header_left_cell = Image(company.logo.path, width=45 * mm, height=14 * mm)
    except Exception:
        pass

    header_table = Table([[header_left_cell, Paragraph("PERFORMA INVOICE", badge_style)]], colWidths=[310, 210])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BORDER', (0, 0), (-1, -1), 1, colors.HexColor('#e0e0e0')),
        ('LINEWIDTH', (0, 0), (-1, -1), 1.5),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 5 * mm))

    if company:
        company_info = f"""
        <font color='#2c3e50'><b>{company_name}</b></font><br/>
        <font size='6' color='#555555'>
        {safe(getattr(company, 'address_line1', ''))}<br/>
        {safe(getattr(company, 'address_line2', ''))}<br/>
        {safe(getattr(company, 'city', ''))}, {safe(getattr(company, 'state', ''))}, {safe(getattr(company, 'country', ''))}, {safe(getattr(company, 'postal_code', ''))}<br/>
        GSTIN: {safe(getattr(company, 'tax_id', ''))}<br/>
        {safe(getattr(company, 'email', ''))}
        </font>
        """
    else:
        company_info = ""
    elements.append(Paragraph(company_info, company_header_style))
    elements.append(Spacer(1, 6 * mm))

    sales_person_obj = getattr(inv, 'sales_person', None)
    sales_person_name = (getattr(sales_person_obj, 'name', None) or '').strip()
    sales_person_phone = (getattr(sales_person_obj, 'phone', None) or '').strip()
    if sales_person_name and sales_person_phone:
        sales_person_display = f"{sales_person_name} ({sales_person_phone})"
    elif sales_person_name:
        sales_person_display = sales_person_name
    else:
        sales_person_display = '-'

    meta_data = [
        [Paragraph("<b>Performa Inv No</b>", meta_label_style), Paragraph(str(context.get('q_no', '')), meta_value_style),
         Paragraph("<b>Date</b>", meta_label_style), Paragraph(inv.date.strftime("%d/%m/%y"), meta_value_style)],
        [Paragraph("<b>Place of Supply</b>", meta_label_style), Paragraph(str(inv.place_of_supply or '-'), meta_value_style),
         Paragraph("<b>Sales Person</b>", meta_label_style), Paragraph(str(sales_person_display), meta_value_style)],
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
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f8f9fa')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#f8f9fa')),
    ]))
    elements.append(meta_table)
    elements.append(Spacer(1, 5 * mm))

    bill_to = "<b style='color: #2c3e50'>Bill To</b><br/>"
    if inv.customer:
        bill_lines = []
        customer_type = (getattr(inv.customer, 'customer_type', '') or '').strip().lower()
        if customer_type == 'company':
            display_name = (getattr(inv.customer, 'company_name', '') or '').strip()
        else:
            first_name = (getattr(inv.customer, 'first_name', '') or '').strip()
            last_name = (getattr(inv.customer, 'last_name', '') or '').strip()
            display_name = f"{first_name} {last_name}".strip() if last_name else first_name
        if display_name:
            bill_lines.append(display_name)
        if inv.shipping_address1 or inv.shipping_city:
            for field in ['shipping_address1', 'shipping_address2', 'shipping_city', 'shipping_postal_code', 'shipping_state', 'shipping_country']:
                value = getattr(inv, field, None)
                if value:
                    bill_lines.append(str(value))
        else:
            for field in ['address_line_1', 'state', 'country']:
                value = getattr(inv.customer, field, None)
                if value:
                    bill_lines.append(str(value))
        gst = getattr(inv.customer, 'gst_number', None)
        if gst:
            bill_lines.append(f"GSTIN: {gst}")
        if bill_lines:
            bill_to += "<font size='7' color='#2c3e50'>" + "<br/>".join(safe(line) for line in bill_lines) + "</font>"
    else:
        bill_to += "-"

    ship_to = "<b style='color: #2c3e50'>Ship To</b><br/>"
    if inv.shipping_address1 or inv.shipping_city:
        ship_lines = []
        for field in ['shipping_attention', 'shipping_address1', 'shipping_address2', 'shipping_city', 'shipping_postal_code', 'shipping_state', 'shipping_country', 'shipping_email', 'shipping_phone']:
            value = getattr(inv, field, None)
            if value:
                ship_lines.append(str(value))
        ship_to += "<font size='7' color='#2c3e50'>" + "<br/>".join(safe(line) for line in ship_lines) + "</font>" if ship_lines else "-"
    else:
        ship_to += "-"

    addr_table = Table([[Paragraph(bill_to, value_style), Paragraph(ship_to, value_style)]], colWidths=[260, 260])
    addr_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e0e0e0')),
    ]))
    elements.append(addr_table)
    elements.append(Spacer(1, 6 * mm))

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
    qty_style = ParagraphStyle('PerformaQtyRight', parent=styles['Normal'], fontSize=8, fontName=font_name, alignment=TA_RIGHT)
    for idx, item in enumerate(context.get('items_info', []), 1):
        tax_rate = Decimal(str(item.get('tax_rate', 0)))
        tax_amount = Decimal(str(item.get('tax_amount', 0)))
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
        ('ALIGN', (3, 1), (6, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ffffff'), colors.HexColor('#f8f9fa')]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 5 * mm))

    pdf_subtotal = Decimal('0.00')
    for item in context.get('items_info', []):
        pdf_subtotal += Decimal(str(item.get('line_total', 0))) - Decimal(str(item.get('tax_amount', 0)))

    totals_label_style = ParagraphStyle(
        'PerformaTotalLabel',
        parent=styles['Normal'],
        fontSize=7,
        textColor=colors.HexColor('#2c3e50'),
        fontName='Helvetica',
        alignment=TA_RIGHT,
    )
    if company_tax_type in ('GST', 'VAT', 'SALES', 'TURNOVER'):
        totals_data = [

            [Paragraph("Sub Total", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('subtotal_calc', 0):.2f}", totals_value_style)],
        ]
    elif company_tax_type == 'NONE':
        totals_data = []
    
    if company_tax_type == 'GST':
        totals_data.append([Paragraph("CGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_cgst', 0):.2f}", totals_value_style)])
        totals_data.append([Paragraph("SGST", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_sgst', 0):.2f}", totals_value_style)])
    elif company_tax_type in ('VAT', 'SALES'):
        totals_data.append([Paragraph("Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('total_tax', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'TURNOVER':
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{context.get('turnover_tax_amount', 0):.2f}", totals_value_style)])
    elif company_tax_type == 'NONE':
        pass  # No tax rows for NONE type

    # Add Round Off and Turnover Tax rows when applicable for proforma
    inv_obj = context.get('invoice')
    # Performa may store round_off on inv object as well
    round_off_value = getattr(inv_obj, 'round_off', None) if inv_obj else None
    if round_off_value:
        totals_data.append([Paragraph("Round Off", totals_label_style), Paragraph(f"{currency}&nbsp;{Decimal(str(round_off_value)):.2f}", amount_style)])
    company_tax_type = context.get('company_tax_type', '')
    turnover_amt = context.get('turnover_tax_amount', Decimal('0.00'))
    if (company_tax_type or '').upper() == 'TURNOVER' and turnover_amt and Decimal(str(turnover_amt)) != Decimal('0.00'):
        totals_data.append([Paragraph("Turnover Tax", totals_label_style), Paragraph(f"{currency}&nbsp;{Decimal(str(turnover_amt)):.2f}", amount_style)])

    grand_total_style = ParagraphStyle(
        'PerformaGrandTotal',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.HexColor('#2c3e50'),
        fontName=font_name,
        alignment=TA_RIGHT,
    )
    totals_data.append([
        Paragraph("<b>Grand Total</b>", ParagraphStyle('PerformaGrandTotalLabel', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.HexColor('#000000'))),
        Paragraph(f"<b>{currency}&nbsp;{Decimal(str(context.get('final_total', 0))):.2f}</b>", grand_total_style),
    ])

    totals_table = Table(totals_data, colWidths=[371, 150])
    totals_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-2, -1), 7),
        ('FONTSIZE', (-2, -1), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('BACKGROUND', (0, 0), (-1, -2), colors.HexColor('#ffffff')),
        ('BACKGROUND', (-2, -1), (-1, -1), colors.HexColor('#f8f9fa')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 5 * mm))

    notes_text = (getattr(inv, 'notes', None) or '').strip() or '-'
    notes_text = safe(notes_text).replace('\n', '<br/>')
    terms_text = (getattr(company, 'terms_and_conditions', None) or '').strip() if company else ''
    terms_text = safe(terms_text or '-').replace('\n', '<br/>')
    bottom_table = Table([[
        Paragraph(
            f"<b>Notes</b><br/><font size=6>{notes_text}</font><br/><br/>"
            f"<b>Terms & Conditions</b><br/><font size=6>{terms_text}</font>",
            value_style,
        ),
        Paragraph(f"<b>Authorized Signatory</b><br/><br/>For {company_name}", value_style),
    ]], colWidths=[330, 190])
    bottom_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    elements.append(bottom_table)

    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(font_name, 6)
        canvas.setFillColor(colors.grey)
        canvas.drawString(30, 20, "POWERED BY LyraERP")
        page_num = canvas.getPageNumber()
        canvas.drawRightString(570, 20, f"Page {page_num}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
    pdf = buffer.getvalue()
    buffer.close()
    return pdf


@require_POST
def send_performa_invoice_message(request, pk):
    """Send Email for a Performa Invoice."""
    # Permission: require Create on Sales Performa Invoice (to send/communicate)
    if not (getattr(request.user, 'is_superuser', False) or can_create_performa_invoice(request.user)):
        return JsonResponse({"success": False, "message": "You do not have permission to send Sales Performa Invoice."}, status=403)
    
    try:
        method_param = request.GET.get('method')
        if not method_param:
            return JsonResponse({"success": False, "message": "Method query required."}, status=400)

        methods = set(m.strip().lower() for m in method_param.split(','))
        if any(m not in ("email",) for m in methods):
            return JsonResponse({"success": False, "message": "Invalid method"}, status=400)

        invoice = PerformaInvoice.objects.select_related('customer').filter(pk=pk).first()
        if not invoice:
            return JsonResponse({"success": False, "message": "Performa Invoice not found"}, status=404)

        context = build_performa_context(pk, request=request)

        try:
            company_obj = Company.objects.first()
            company_name = company_obj.name if company_obj else "Company"
            company_logo = company_obj.logo_base64 if company_obj and getattr(company_obj, 'logo_base64', None) else ""
        except Exception:
            company_name = "Company"
            company_logo = ""

        customer_name = get_customer_display_name(invoice.customer)
        placeholder_ctx = {
            'customer': customer_name,
            'invoice_number': invoice.inv_number,
            'date': invoice.date.strftime("%d %B %Y") if getattr(invoice, 'date', None) else '',
            'total': str(invoice.total_amount or ''),
            'company': company_name,
            'logo': company_logo,
            'document_type': 'Performa Invoice',
        }
        placeholder_ctx['employee'] = placeholder_ctx['customer']
        placeholder_ctx['amount'] = placeholder_ctx['total']
        placeholder_ctx['code'] = placeholder_ctx['invoice_number']

        def get_active_email_config(*usage_names):
            for usage_name in usage_names:
                if not usage_name:
                    continue
                cfg = EmailConfiguration.objects.filter(
                    usage_types__icontains=usage_name,
                    status=True,
                ).first()
                if cfg:
                    return cfg
            return (
                EmailConfiguration.objects.filter(status=True, is_default=True).first()
                or EmailConfiguration.objects.filter(status=True).first()
            )

        recipient = invoice.shipping_email or (
            getattr(invoice.customer, 'email', None) if invoice.customer else None
        )
        if not recipient:
            return JsonResponse({"success": False, "message": "Customer email not found."}, status=400)

        messages_sent = []

        if 'email' in methods:
            template_style = None
            subject = None
            message_html = None
            try_names = [
                "Performa Invoice Details",
                "Performa Invoice Email",
                "Performa Invoice",
                "Proforma Invoice Details",
                "Proforma Invoice Email",
                "Proforma Invoice",
            ]
            from email_templates.models import EmailTemplateStyle

            for template_name in try_names:
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=template_name,
                    is_default=True,
                    status=True,
                ).first()
                if template_style:
                    break
                template_style = EmailTemplateStyle.objects.filter(
                    template_name=template_name,
                    status=True,
                ).first()
                if template_style:
                    break

            if template_style:
                subject = replace_placeholders(
                    template_style.subject or f"Performa Invoice {invoice.inv_number}",
                    placeholder_ctx,
                )
                message_html = replace_placeholders(template_style.body or "", placeholder_ctx)

                try:
                    message_html = re.sub(r'style="[^"]*(?:background(?:-color)?|color)[^"]*"', '', message_html, flags=re.I)
                    message_html = re.sub(r'<font[^>]*>', '', message_html, flags=re.I)
                    message_html = re.sub(r'</font>', '', message_html, flags=re.I)
                except Exception:
                    pass

                email_config = get_active_email_config(
                    template_style.template_name,
                    "Performa Invoice",
                    "Proforma Invoice",
                    "Invoice",
                )
                if not email_config:
                    return JsonResponse(
                        {"success": False, "message": "No email configuration found for Performa Invoice. Go to settings and set up email configuration."},
                        status=400,
                    )

                try:
                    connection = get_connection(
                        host=email_config.host,
                        port=email_config.port,
                        username=email_config.host_user,
                        password=email_config.host_password,
                        use_tls=email_config.use_tls,
                        fail_silently=False,
                    )

                    pdf_bytes = generate_performa_invoice_pdf_bytes(pk)
                    from_email = email_config.default_from_email or email_config.host_user
                    msg = EmailMultiAlternatives(
                        subject,
                        '',
                        from_email,
                        [recipient],
                        connection=connection,
                    )
                    try:
                        msg.attach_alternative(message_html, 'text/html')
                    except Exception:
                        pass

                    if pdf_bytes:
                        msg.attach(f'performa_invoice_{invoice.pk}.pdf', pdf_bytes, 'application/pdf')

                    msg.send(fail_silently=False)
                    if invoice.status == 'Draft':
                        invoice.status = 'Sent'
                        invoice.save(update_fields=['status'])
                    messages_sent.append(f"Email sent to {recipient} successfully!")
                except Exception as e:
                    traceback.print_exc()
                    return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
            else:
                from email_templates.views import get_default_email_template

                default_template = (
                    get_default_email_template("Performa Invoice")
                    or get_default_email_template("Proforma Invoice")
                )

                if default_template:
                    subject = replace_placeholders(
                        default_template.get("subject", f"Performa Invoice {invoice.inv_number}"),
                        placeholder_ctx,
                    )
                    message_html = replace_placeholders(default_template.get("body", ""), placeholder_ctx)

                    try:
                        attach_pdf_flag = default_template.get("attach_pdf", True)
                        marker_match = re.search(r"\[\[attach_pdf:(true|false)\]\]", message_html, flags=re.I)
                        if marker_match:
                            attach_pdf_flag = marker_match.group(1).lower() == 'true'
                            message_html = re.sub(r"\[\[attach_pdf:(?:true|false)\]\]", '', message_html, flags=re.I)
                        message_html = re.sub(r'<div\s+class\s*=\s*"attach-option"[\s\S]*?<\/div>', '', message_html, flags=re.I)
                    except Exception:
                        attach_pdf_flag = True

                    email_config = get_active_email_config("Performa Invoice", "Proforma Invoice", "Invoice")
                    if not email_config:
                        return JsonResponse(
                            {"success": False, "message": "No email configuration found for Performa Invoice. Go to settings and set up email configuration."},
                            status=400,
                        )

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False,
                        )

                        pdf_bytes = generate_performa_invoice_pdf_bytes(pk) if attach_pdf_flag else None
                        from_email = email_config.default_from_email or email_config.host_user
                        plain_body = f"Please find attached Performa Invoice {invoice.inv_number}."
                        msg = EmailMultiAlternatives(
                            subject or f"Performa Invoice {invoice.inv_number}",
                            plain_body,
                            from_email,
                            [recipient],
                            connection=connection,
                        )

                        try:
                            msg.attach_alternative(message_html, 'text/html')
                        except Exception:
                            pass

                        if pdf_bytes:
                            msg.attach(f'performa_invoice_{invoice.pk}.pdf', pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)
                        if invoice.status == 'Draft':
                            invoice.status = 'Sent'
                            invoice.save(update_fields=['status'])
                        messages_sent.append(f"Email sent to {recipient} successfully!")
                    except Exception as e:
                        traceback.print_exc()
                        return JsonResponse({"success": False, "message": f"Email failed: {str(e)}"}, status=500)
                else:
                    email_config = get_active_email_config("Performa Invoice", "Proforma Invoice", "Invoice")
                    if not email_config:
                        return JsonResponse({"success": False, "message": "No email configuration found."}, status=400)

                    try:
                        connection = get_connection(
                            host=email_config.host,
                            port=email_config.port,
                            username=email_config.host_user,
                            password=email_config.host_password,
                            use_tls=email_config.use_tls,
                            fail_silently=False,
                        )

                        pdf_bytes = generate_performa_invoice_pdf_bytes(pk)
                        from_email = email_config.default_from_email or email_config.host_user
                        msg = EmailMultiAlternatives(
                            f"Performa Invoice {invoice.inv_number}",
                            f"Please find attached Performa Invoice {invoice.inv_number}.",
                            from_email,
                            [recipient],
                            connection=connection,
                        )
                        fallback_html = render_to_string(
                            'sales/performa_invoice_print.html',
                            {**context, 'email_mode': True},
                        )
                        try:
                            msg.attach_alternative(fallback_html, 'text/html')
                        except Exception:
                            pass

                        if pdf_bytes:
                            msg.attach(f'performa_invoice_{invoice.pk}.pdf', pdf_bytes, 'application/pdf')

                        msg.send(fail_silently=False)
                        if invoice.status == 'Draft':
                            invoice.status = 'Sent'
                            invoice.save(update_fields=['status'])
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


def update_performa_status(request, invoice_id):
    """Update Performa Invoice status via AJAX."""
    if request.method == 'POST':
        # Permission: require Edit on Sales Performa Invoice
        try:
            if not (getattr(request.user, 'is_superuser', False) or can_edit_performa_invoice(request.user)):
                return JsonResponse({
                    'success': False,
                    'error': 'You do not have permission to edit Performa Invoices.'
                }, status=403)
        except Exception:
            return JsonResponse({
                'success': False,
                'error': 'You do not have permission to edit Performa Invoices.'
            }, status=403)

        try:
            invoice = get_object_or_404(PerformaInvoice, pk=invoice_id)
            new_status = request.POST.get('status', '').strip()
            current_status = (invoice.status or '').strip()

            valid_statuses = [choice[0] for choice in PerformaInvoice.STATUS_CHOICES]
            if new_status not in valid_statuses:
                return JsonResponse({
                    'success': False,
                    'error': f'Invalid status. Valid options: {", ".join(valid_statuses)}'
                }, status=400)

            allowed_transitions = {
                'Draft': {'Sent', 'Cancelled'},
                'Sent': {'Accepted', 'Cancelled'},
                'Accepted': set(),
                'Cancelled': set(),
            }

            if new_status == current_status:
                return JsonResponse({
                    'success': True,
                    'message': f"Performa Invoice status is already '{current_status}'.",
                    'status': invoice.status,
                })

            allowed_next = allowed_transitions.get(current_status, set())
            if new_status not in allowed_next:
                return JsonResponse({
                    'success': False,
                    'error': f"Invalid status transition: {current_status} -> {new_status}."
                }, status=400)

            invoice.status = new_status
            invoice.save(update_fields=['status'])
            return JsonResponse({
                'success': True,
                'message': f"Performa Invoice status updated to '{new_status}'.",
                'status': invoice.status,
            })
        except Exception as e:
            logger.exception("Error updating performa invoice status: %s", e)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)


@transaction.atomic
def convert_performa_to_order(request, performa_id):
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_orders(request.user)):
            messages.error(request, 'You do not have permission to create Sales Orders.')
            return redirect_with_company('performa_invoice_detail', pk=performa_id)
    except Exception:
        messages.error(request, 'You do not have permission to create Sales Orders.')
        return redirect_with_company('performa_invoice_detail', pk=performa_id)

    performa = get_object_or_404(PerformaInvoice, pk=performa_id)

    if performa.status != 'Accepted':
        messages.error(request, 'Only Accepted Performa Invoices can be converted to Sales Order.')
        return redirect_with_company('performa_invoice_detail', pk=performa.pk)

    existing_order = SalesOrder.objects.filter(origin_performa=performa).order_by('-id').first()
    if existing_order:
        messages.info(request, f"Sales Order '{existing_order.order_number}' already exists for this Performa Invoice.")
        return redirect_with_company('order_detail', pk=existing_order.pk)

    order_number = generate_order_number()

    sales_order = SalesOrder.objects.create(
        customer=performa.customer,
        order_number=order_number,
        date=timezone.now(),
        sales_person=performa.sales_person,
        notes=performa.notes,
        payment_term=performa.payment_term,
        discount_value=performa.discount_value,
        discount_type=performa.discount_type,
        status='Draft',
        total_amount=performa.total_amount,
        place_of_supply=performa.place_of_supply,
        shipping_attention=performa.shipping_attention,
        shipping_email=performa.shipping_email,
        shipping_phone=performa.shipping_phone,
        shipping_country=performa.shipping_country,
        shipping_address1=performa.shipping_address1,
        shipping_address2=performa.shipping_address2,
        shipping_city=performa.shipping_city,
        shipping_state=performa.shipping_state,
        shipping_postal_code=performa.shipping_postal_code,
        tds_tcs_type=performa.tds_tcs_type,
        tds_tcs_definition_id=performa.tds_tcs_definition_id,
        tds_tcs_rate=performa.tds_tcs_rate,
        tds_tcs_amount=performa.tds_tcs_amount,
        origin_performa=performa,
    )

    performa_items = PerformaInvoiceItem.objects.filter(performa_inv=performa)
    for item in performa_items:
        SalesOrderItem.objects.create(
            sales_order=sales_order,
            product=item.product,
            prd_brcd=item.prd_brcd,
            hsn_code=item.hsn_code,
            prd_tax=item.prd_tax,
            prd_taxgroup=item.prd_taxgroup,
            prd_disvalue=item.prd_disvalue,
            prd_distype=item.prd_distype,
            quantity=item.quantity,
            price=item.price,
            description=item.description,
        )

    messages.success(request, f"Sales Order '{sales_order.order_number}' created from Performa Invoice '{performa.inv_number}'.")
    return redirect_with_company('order_detail', pk=sales_order.pk)


def delete_performa_inv(request, inv_id):
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_performa_invoice(request.user)):
            error_msg = 'You do not have permission to delete Performa Invoices.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': error_msg}, status=403)
            messages.error(request, error_msg)
            return redirect_with_company('performa_inv_list')
    except Exception:
        error_msg = 'You do not have permission to delete Performa Invoices.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, error_msg)
        return redirect_with_company('performa_inv_list')

    inv = get_object_or_404(PerformaInvoice, pk=inv_id)

    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer

    db = getattr(request, 'company_db', 'default')
    try:
        PeriodLockEnforcer.check_can_edit(inv.date, request.user, db=db)
    except PermissionDenied as e:
        error_msg = str(e)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=403)
        messages.error(request, error_msg)
        return redirect_with_company('performa_inv_list')

    inv.delete()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'message': f"Performa Invoice '{inv.inv_number}' deleted successfully."})

    messages.success(request, f"Performa Invoice '{inv.inv_number}' deleted successfully.")
    return redirect_with_company('performa_inv_list')

def performa_inv_add(request):
    # Permission: require Create on Sales Performa Invoice
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_performa_invoice(request.user)):
            messages.error(request, 'You do not have permission to create Performa Invoices.')
            return redirect_with_company('performa_inv_list')
    except Exception:
        messages.error(request, 'You do not have permission to create Performa Invoices.')
        return redirect_with_company('performa_inv_list')

    try:
        db_alias = getattr(request, 'company_db', 'default')
        resolved_company = _get_company_for_request(request)
        auto_load = bool(getattr(resolved_company, 'auto_load_cash_customer', False)) if resolved_company else False
        cash_customer = None
        if auto_load:
            cash_customer = Customer.objects.using(db_alias).filter(customer_code__iexact='CASH').first()
        sp = _get_current_salesperson(request)
        initial = {'customer': cash_customer.pk if cash_customer else None}
        if sp:
            initial['sales_person'] = sp.pk
        invoice_form = PerformaInvoiceForm(initial=initial)
    except Exception:
        sp = _get_current_salesperson(request)
        initial = {}
        if sp:
            initial['sales_person'] = sp.pk
        invoice_form = PerformaInvoiceForm(initial=initial if initial else None)
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    PerformaInvoiceItemFormSet = formset_factory(PerformaInvoiceItemForm, extra=1)
    sales_formset = PerformaInvoiceItemFormSet()
    all_items = Item.objects.all()
    
    company = _get_company_for_request(request)
    from currencies.models import Currency
    company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    if not base_currency_symbol:
        base_currency_symbol = base_currency.code if base_currency and base_currency.code else '₹'
    base_currency_code = base_currency.code if base_currency else ''
    
    # — Fetch TDS and TCS for performa_invoice_add template
    tds_tax_master_items = TdsMaster.objects.filter(company=company, is_active=True) if company else TdsMaster.objects.none()
    tcs_tax_master_items = TcsMaster.objects.filter(company=company, is_active=True) if company else TcsMaster.objects.none()
    
    return render(request, 'sales/performa_invoice_add.html', {
        'invoice_form': invoice_form,
        'item_formset': item_formset,
        'sales_formset': sales_formset,
        'all_items': all_items,
        'today': localdate().isoformat(),
        'q_no': generate_performa_number(localdate()),
        'is_edit': False,
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': _get_request_company_tax_type(request, company),
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'base_currency': base_currency,
        'selected_currency_id': None,
        'fx_rate_to_base': None,
        'fx_rate_date': None,
        'tds_tax_master_items': tds_tax_master_items,
        'tcs_tax_master_items': tcs_tax_master_items,
        'show_base_transaction_summary': bool(getattr(company, 'show_base_transaction_summary', True)),
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    })


def performa_invoice_edit(request, pk):
    # Permission: require Edit on Sales Performa Invoice
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_edit_performa_invoice(request.user)):
            messages.error(request, 'You do not have permission to edit Performa Invoices.')
            return redirect_with_company('performa_inv_list')
    except Exception:
        messages.error(request, 'You do not have permission to edit Performa Invoices.')
        return redirect_with_company('performa_inv_list')

    invoice = get_object_or_404(PerformaInvoice, pk=pk)
    sp = _get_current_salesperson(request)
    if not getattr(invoice, 'sales_person', None) and sp:
        invoice_form = PerformaInvoiceForm(instance=invoice, initial={'sales_person': sp.pk})
    else:
        invoice_form = PerformaInvoiceForm(instance=invoice)
    ItemFormSet = modelformset_factory(Item, form=ItemForm, extra=0)
    item_formset = ItemFormSet(queryset=Item.objects.none())
    PerformaInvoiceItemFormSet = modelformset_factory(
        PerformaInvoiceItem,
        form=PerformaInvoiceItemForm,
        extra=0,
        can_delete=True,
    )
    existing_items_qs = PerformaInvoiceItem.objects.filter(performa_inv=invoice)
    sales_formset = PerformaInvoiceItemFormSet(queryset=existing_items_qs)

    for form, item in zip(sales_formset.forms, existing_items_qs):
        display_label = item.product.name if item.product else ''
        form.fields['product'].choices = [(item.product.id, display_label)]
        form.initial['product'] = item.product.id

    company = _get_company_for_request(request)
    from currencies.models import Currency
    company_currencies = Currency.objects.filter(company=company, is_active=True).order_by('code')
    base_currency = company_currencies.filter(is_base=True).first() or company_currencies.first()
    base_currency_symbol = (base_currency.symbol or base_currency.code or '').strip() if base_currency else '₹'
    if not base_currency_symbol:
        base_currency_symbol = base_currency.code if base_currency and base_currency.code else '₹'
    base_currency_code = base_currency.code if base_currency else ''

    return render(request, 'sales/performa_invoice_add.html', {
        'invoice_form': invoice_form,
        'item_formset': item_formset,
        'sales_formset': sales_formset,
        'all_items': Item.objects.all(),
        'today': localdate().isoformat(),
        'q_no': invoice.inv_number,
        'invoice': invoice,
        'is_edit': True,
        'shipping_attention': invoice.shipping_attention or '',
        'shipping_email': invoice.shipping_email or '',
        'shipping_phone': invoice.shipping_phone or '',
        'shipping_country': invoice.shipping_country or '',
        'shipping_address1': invoice.shipping_address1 or '',
        'shipping_address2': invoice.shipping_address2 or '',
        'shipping_city': invoice.shipping_city or '',
        'shipping_state': invoice.shipping_state or '',
        'shipping_postal_code': invoice.shipping_postal_code or '',
        'company_is_india': _is_indian_company_country(_get_current_company_country(request)),
        'company_tax_type': _get_request_company_tax_type(request, company),
        'company_currencies': company_currencies,
        'company_base_currency_symbol': base_currency_symbol,
        'company_base_currency_code': base_currency_code,
        'base_currency': base_currency,
        'selected_currency_id': invoice.document_currency_id,
        'fx_rate_to_base': invoice.fx_rate_to_base,
        'fx_rate_date': invoice.fx_rate_date,
        'tds_tcs_type': invoice.tds_tcs_type or 'tds',
        'tds_tcs_definition_id': invoice.tds_tcs_definition_id or '',
        'tds_tcs_rate': invoice.tds_tcs_rate or 0,
        'tds_tcs_amount': invoice.tds_tcs_amount or 0,
        # — Fetch TDS and TCS for performa_invoice_edit template
        'tds_tax_master_items': TdsMaster.objects.filter(company=company, is_active=True),
        'tcs_tax_master_items': TcsMaster.objects.filter(company=company, is_active=True),
        'sales_rounding_method': getattr(company, 'sales_rounding_method', 'none'),
        'sales_rounding_increment': getattr(company, 'sales_rounding_increment', 0),
    })



def _save_performa_items_from_post(performa_invoice, post_data, prd_brcd_map):
    existing_items_before_save = list(
        PerformaInvoiceItem.objects.filter(performa_inv=performa_invoice).order_by('id')
    )
    PerformaInvoiceItem.objects.filter(performa_inv=performa_invoice).delete()
    items_created = 0
    index = 0

    while True:
        product_key = f'form-{index}-product'
        if product_key not in post_data:
            break

        product_id = (post_data.get(product_key) or '').strip()
        if not product_id:
            index += 1
            continue

        try:
            product = Item.objects.get(pk=product_id)
            quantity = int(post_data.get(f'form-{index}-quantity', 0) or 0)
            price = Decimal(post_data.get(f'form-{index}-price', 0) or 0)
        except (Item.DoesNotExist, ValueError, TypeError, ArithmeticError):
            index += 1
            continue

        if quantity <= 0:
            index += 1
            continue

        try:
            item_discount_value = Decimal(str(post_data.get(f'form-{index}-prd_disvalue', '0') or '0'))
        except (InvalidOperation, ValueError, TypeError):
            item_discount_value = Decimal('0.00')

        try:
            posted_o_price = Decimal(str(post_data.get(f'form-{index}-o_price', '0') or '0'))
        except (InvalidOperation, ValueError, TypeError):
            posted_o_price = Decimal('0.00')

        existing_item = existing_items_before_save[index] if index < len(existing_items_before_save) else None
        if (
            existing_item
            and existing_item.product_id == product.id
            and getattr(existing_item, 'o_price', None) not in (None, Decimal('0.00'))
        ):
            o_price = Decimal(str(existing_item.o_price))
        else:
            o_price = posted_o_price

        item = PerformaInvoiceItem.objects.create(
            performa_inv=performa_invoice,
            product=product,
            quantity=quantity,
            price=price,
            o_price=o_price,
            description=post_data.get(f'form-{index}-description', ''),
            hsn_code=post_data.get(f'form-{index}-hsn_code', ''),
            prd_disvalue=item_discount_value,
            prd_distype=post_data.get(f'form-{index}-prd_distype', 'flat'),
            prd_brcd=prd_brcd_map.get(f'form-{index}', ''),
        )

        # The UI can submit `prd_tax` as either:
        # - `group:<id>` for TaxGroup selections
        # - `<id>` (legacy / non-prefixed)
        # - `tax:<id>` (if a single Tax was selected)
        tax_choice = (post_data.get(f'form-{index}-prd_tax', '') or '').strip()
        if tax_choice:
            try:
                if tax_choice.startswith('group:'):
                    choice_id = tax_choice.split(':', 1)[1].strip()
                    tax_group = (
                        TaxGroup.objects.filter(id=choice_id)
                        .prefetch_related('taxes')
                        .first()
                    )
                    if tax_group:
                        item.prd_tax = Decimal(sum(t.rate for t in tax_group.taxes.all()))
                        item.prd_taxgroup = tax_group.group_name
                    else:
                        item.prd_tax = Decimal(0)
                        item.prd_taxgroup = None
                elif tax_choice.startswith('tax:'):
                    choice_id = tax_choice.split(':', 1)[1].strip()
                    tax_obj = Tax.objects.filter(id=choice_id).first()
                    if tax_obj:
                        item.prd_tax = Decimal(tax_obj.rate or 0)
                        item.prd_taxgroup = None
                    else:
                        item.prd_tax = Decimal(0)
                        item.prd_taxgroup = None
                else:
                    # Assume already a TaxGroup numeric id
                    tax_group = (
                        TaxGroup.objects.filter(id=tax_choice)
                        .prefetch_related('taxes')
                        .first()
                    )
                    if tax_group:
                        item.prd_tax = Decimal(sum(t.rate for t in tax_group.taxes.all()))
                        item.prd_taxgroup = tax_group.group_name
                    else:
                        item.prd_tax = Decimal(0)
                        item.prd_taxgroup = None
            except (ValueError, TypeError):
                item.prd_tax = Decimal(0)
                item.prd_taxgroup = None
        else:
            item.prd_tax = Decimal(0)
            item.prd_taxgroup = None

        item.save()
        items_created += 1
        index += 1

    return items_created

def save_performa_invoice(request):
    if request.method != "POST":
        return redirect_with_company('performa_inv_list')

    post_data = request.POST.copy()
    prd_brcd_map = {}
    performa_invoice = None
    is_edit = bool(post_data.get('invoice_id'))
    success_message = 'Performa Invoice updated successfully!' if is_edit else 'Performa Invoice created successfully!'

    from datetime import datetime
    from django.core.exceptions import PermissionDenied
    from system_settings.validators import PeriodLockEnforcer

    try:
        # Permission: require Edit/Create on Sales Performa Invoice
        has_permission = (
            getattr(request.user, 'is_superuser', False)
            or (can_edit_performa_invoice(request.user) if is_edit else can_create_performa_invoice(request.user))
        )
        if not has_permission:
            messages.error(
                request,
                'You do not have permission to edit Performa Invoices.' if is_edit else 'You do not have permission to create Performa Invoices.'
            )
            return redirect_with_company('performa_inv_list')
    except Exception:
        messages.error(
            request,
            'You do not have permission to edit Performa Invoices.' if is_edit else 'You do not have permission to create Performa Invoices.'
        )
        return redirect_with_company('performa_inv_list')

    db = getattr(request, 'company_db', 'default')
    date_str = post_data.get('date')
    invoice_date = None
    if date_str:
        try:
            invoice_date = datetime.strptime(date_str, '%Y-%m-%d').date() if isinstance(date_str, str) else date_str
        except Exception:
            invoice_date = None

        if invoice_date:
            try:
                PeriodLockEnforcer.check_can_edit(invoice_date, request.user, db=db)
            except PermissionDenied as e:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'error': f"Cannot {'update' if is_edit else 'create'} performa invoice: {str(e)}"}, status=403)
                invoice_instance = None
                if is_edit and post_data.get('invoice_id'):
                    invoice_instance = PerformaInvoice.objects.filter(pk=post_data.get('invoice_id')).first()
                invoice_form = PerformaInvoiceForm(instance=invoice_instance)
                PerformaInvoiceItemFormSet = modelformset_factory(PerformaInvoiceItem, form=PerformaInvoiceItemForm, extra=3, can_delete=True)
                existing_items = PerformaInvoiceItem.objects.filter(performa_inv=invoice_instance) if invoice_instance else PerformaInvoiceItem.objects.none()
                item_formset = PerformaInvoiceItemFormSet(queryset=existing_items)
                sales_formset = PerformaInvoiceItemFormSet(queryset=existing_items)
                all_items = Item.objects.all()
                return render(request, 'sales/performa_invoice_add.html', {
                    'invoice_form': invoice_form,
                    'item_formset': item_formset,
                    'sales_formset': sales_formset,
                    'all_items': all_items,
                    'today': invoice_instance.date.isoformat() if invoice_instance else localdate().isoformat(),
                    'q_no': invoice_instance.inv_number if invoice_instance else generate_performa_number(localdate()),
                    'invoice': invoice_instance,
                    'is_edit': is_edit,
                    'error_message': str(e),
                    'show_error_modal': True,
                })

    for key in post_data:
        if key.startswith("form-") and key.endswith("-product"):
            value = post_data[key]
            if value:
                parts = value.split("_", 1)
                post_data[key] = parts[0]
                if len(parts) > 1:
                    prefix = key.rsplit("-", 1)[0]
                    prd_brcd_map[prefix] = parts[1]

    total_amount = request.POST.get('grandTotal')
    raw_total = request.POST.get('unroundedGrandTotal') or request.POST.get('grandTotal')
    try:
        raw_total_amount = (
            Decimal(str(raw_total).strip())
            if raw_total not in (None, '')
            else Decimal('0')
        )
    except Exception:
        raw_total_amount = Decimal('0')
    rounding_company = _get_company_for_request(request)
    total_amount, rounding_adjustment = apply_sales_rounding(raw_total_amount, rounding_company)
    customer_id = request.POST.get('customer')
    date = request.POST.get('date')
    sales_person_id = request.POST.get('sales_person')
    notes = request.POST.get('notes', '')
    invoice_id = post_data.get('invoice_id')
    inv_number = generate_performa_number(date)
    try:
        discount_raw = request.POST.get('grand-discount-value', '0').strip() or '0'
        discount_value = Decimal(discount_raw).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        discount_value = Decimal('0.00')
    discount_type = request.POST.get('discount_type', 'percent')

    if not customer_id or not date:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': 'Customer and Performa Invoice Date are required.'}, status=400)
        messages.error(request, "Customer and Performa Invoice Date are required.")
        return redirect_with_company('performa_inv_list')

    try:
        customer = Customer.objects.get(pk=customer_id)
        sales_person = SalesPerson.objects.get(pk=sales_person_id) if sales_person_id else None
        company = _get_company_for_request(request)
        company_tax_type = _get_request_company_tax_type(request, company)
        turnover_tax_obj = None
        if company_tax_type == 'TURNOVER':
            turnover_tax_id = (request.POST.get('turnover_tax') or '').strip()
            if turnover_tax_id:
                turnover_tax_obj = Tax.objects.filter(id=turnover_tax_id, tax_type__iexact='TURNOVER').first()
        tds_tcs_type = request.POST.get('tds_tcs_type', 'tds')
        tds_tcs_definition_id = _parse_positive_int(request.POST.get('tds_tcs_definition_id'))
        tds_tcs_rate = _parse_decimal(request.POST.get('tds_tcs_rate', '0'))
        tds_tcs_amount = _parse_decimal(request.POST.get('tds_tcs_amount', '0.00'))
        with transaction.atomic():
            if is_edit:
                performa_invoice = get_object_or_404(PerformaInvoice, pk=invoice_id)
                performa_invoice.customer = customer
                performa_invoice.date = date
                performa_invoice.sales_person = sales_person
                performa_invoice.total_amount = total_amount
                performa_invoice.round_off = rounding_adjustment
                performa_invoice.notes = notes
                performa_invoice.discount_value = discount_value
                performa_invoice.discount_type = discount_type
                performa_invoice.place_of_supply = request.POST.get('place_of_supply', '')
                performa_invoice.shipping_attention = request.POST.get('shipping_attention', '')
                performa_invoice.shipping_email = request.POST.get('shipping_email', '')
                performa_invoice.shipping_phone = request.POST.get('shipping_phone', '')
                performa_invoice.shipping_country = request.POST.get('shipping_country', '')
                performa_invoice.shipping_address1 = request.POST.get('shipping_address1', '')
                performa_invoice.shipping_address2 = request.POST.get('shipping_address2', '')
                performa_invoice.shipping_city = request.POST.get('shipping_city', '')
                performa_invoice.shipping_state = request.POST.get('shipping_state', '')
                performa_invoice.shipping_postal_code = request.POST.get('shipping_postal_code', '')
                performa_invoice.tds_tcs_type = tds_tcs_type
                performa_invoice.tds_tcs_definition_id = tds_tcs_definition_id
                performa_invoice.tds_tcs_rate = tds_tcs_rate
                performa_invoice.tds_tcs_amount = tds_tcs_amount
                performa_invoice.turnover_tax = turnover_tax_obj if company_tax_type == 'TURNOVER' else None
                performa_invoice.save()
            else:
                performa_invoice = PerformaInvoice.objects.create(
                    customer=customer,
                    date=date,
                    sales_person=sales_person,
                    inv_number=inv_number,
                    status='Draft',
                    total_amount=total_amount,
                    round_off=rounding_adjustment,
                    notes=notes,
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
                    tds_tcs_type=tds_tcs_type,
                    tds_tcs_definition_id=tds_tcs_definition_id,
                    tds_tcs_rate=tds_tcs_rate,
                    tds_tcs_amount=tds_tcs_amount,
                    turnover_tax=turnover_tax_obj,
                )

            pay_term_id = request.POST.get('payment_term')
            if pay_term_id:
                try:
                    performa_invoice.payment_term = PayTerms.objects.get(pk=pay_term_id)
                except PayTerms.DoesNotExist:
                    performa_invoice.payment_term = None
            else:
                performa_invoice.payment_term = None
            performa_invoice.save()

            # Apply document currency and FX values
            try:
                from currencies.services import apply_performa_invoice_fx, parse_currency_fx_from_post

                doc_cur, rate_override, fx_date = parse_currency_fx_from_post(request.POST, company)
                apply_performa_invoice_fx(
                    performa_invoice,
                    company,
                    document_currency=doc_cur,
                    fx_rate_to_base_override=rate_override,
                    fx_rate_date_override=fx_date,
                )
            except Exception as ex:
                import logging
                logging.getLogger(__name__).exception('Failed to apply FX to performa invoice %s: %s', getattr(performa_invoice, 'pk', None), str(ex))

            items_created = _save_performa_items_from_post(performa_invoice, post_data, prd_brcd_map)
            if items_created == 0:
                raise ValueError('Please add at least one item to the Performa Invoice.')
            if company_tax_type == 'TURNOVER':
                performa_invoice.total_amount_base = (
                    Decimal(performa_invoice.total_amount or 0) * Decimal(performa_invoice.fx_rate_to_base or 1)
                ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                performa_invoice.save(update_fields=['total_amount_base'])
    except IntegrityError as e:
        error_msg = f"Performa Invoice Number '{inv_number}' already exists. Please choose a different one." if 'unique constraint' in str(e).lower() or 'duplicate entry' in str(e).lower() else "An error occurred while saving the performa invoice."
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect_with_company('performa_inv_list')
    except ValueError as e:
        if performa_invoice and not is_edit:
            performa_invoice.delete()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': str(e)}, status=400)
        messages.error(request, str(e))
        return redirect_with_company('performa_inv_list')

    messages.success(request, success_message)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'invoice_id': performa_invoice.id,
            'invoice_number': performa_invoice.inv_number,
            'customer_name': get_customer_display_name(performa_invoice.customer),
            'total_amount': str(performa_invoice.total_amount),
            'message': success_message
        })
    return redirect_with_company('performa_inv_list')


# ==============================================================================
# E-WAY BILL VIEWS
# ==============================================================================

EWAY_THRESHOLD = 50000  # —50,000 ₹ GST mandate threshold


def _is_indian_company_country(country_str):
    """Return whether the active company should use GST-style tax UI/posting."""
    try:
        country_value = str(country_str or "").strip()
        if country_value.upper() in {"GST", "VAT", "SALES", "TURNOVER", "NONE", "LOCAL"}:
            return country_value.upper() == "GST"

        company = Company.objects.filter(country__iexact=country_value).order_by("id").first()
        company_tax_type = str(getattr(company, "tax_type", "") or "").strip().upper() if company else ""
        if company_tax_type:
            return company_tax_type == "GST"
    except Exception:
        pass

    if not country_str:
        return get_company_tax_type() == "GST"
    country_str = str(country_str).lower().strip()
    return country_str in ['in', 'india', 'ind']


def _get_current_company_country(request):
    """Get current company's country code."""
    try:
        company = _get_company_for_request(request)
        if company:
            return str(getattr(company, 'country', '') or '')
    except Exception:
        pass
    return 'IN'


@login_required
def eway_bill_generate(request, invoice_id):
    """
    Create or edit the E-Way Bill draft for a specific invoice.
    Only available for Indian companies and invoices ₹ ₹50,000.
    """
    # Permission: require Create or Edit on E-Way Bill
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_eway_bill(request.user) or can_edit_eway_bill(request.user)):
            messages.error(request, 'You do not have permission to generate E-Way Bills.')
            return redirect_with_company('sales_inv_list')
    except Exception:
        messages.error(request, 'You do not have permission to generate E-Way Bills.')
        return redirect_with_company('sales_inv_list')

    invoice = get_object_or_404(SalesInvoice, pk=invoice_id)

    # India-only guard
    if not _is_indian_company_country(_get_current_company_country(request)):
        from django.contrib import messages
        messages.error(request, 'E-Way Bill is only applicable for Indian companies.')
        return redirect_with_company('invoice_detail', pk=invoice_id)

    eway = getattr(invoice, 'eway_bill', None)

    if request.method == 'POST':
        form = EWayBillForm(request.POST, instance=eway)
        if form.is_valid():
            bill = form.save(commit=False)
            bill.invoice     = invoice
            bill.total_value = invoice.total_amount
            bill.status      = 'generated'
            bill.generated_at = timezone.now()

            # Auto-fill supplier side from company record
            try:
                company = Company.objects.filter(status=True).first() or Company.objects.first()
            except Exception:
                company = None

            if not bill.gstin_from and company:
                bill.gstin_from = getattr(company, 'tax_id', '') or ''
            if not bill.place_from and company:
                bill.place_from = getattr(company, 'city', '') or ''
            if not bill.pincode_from and company:
                bill.pincode_from = getattr(company, 'postal_code', '') or ''
            if not bill.state_from and company:
                bill.state_from = str(getattr(company, 'state', '') or '')

            # Auto-fill recipient side defaults if blank
            if not bill.gstin_to:
                bill.gstin_to = getattr(invoice.customer, 'gst_number', '') or ''
            if not bill.state_to:
                bill.state_to = invoice.shipping_state or invoice.place_of_supply or ''
            if not bill.place_to:
                bill.place_to = (
                    invoice.shipping_city
                    or getattr(invoice.customer, 'city', '')
                    or ''
                )
            if not bill.pincode_to:
                bill.pincode_to = (
                    invoice.shipping_postal_code
                    or getattr(invoice.customer, 'postal_code', '')
                    or ''
                )

            # Primary HSN from first invoice item
            if not bill.hsn_code:
                first_item = SalesInvoiceItem.objects.filter(sales_inv=invoice).order_by('id').first()
                bill.hsn_code = getattr(first_item, 'hsn_code', '') or ''

            bill.save()
            from django.contrib import messages
            messages.success(
                request,
                f'E-Way Bill generated successfully for Invoice {invoice.inv_number}.'
            )
            return redirect_with_company('eway_bill_detail', pk=bill.pk)
    else:
        # Pre-fill form fields from invoice when creating new
        initial = {}
        if not eway:
            initial = {
                'gstin_to'   : getattr(invoice.customer, 'gst_number', '') or '',
                'place_to'   : invoice.shipping_city or getattr(invoice.customer, 'city', '') or '',
                'pincode_to' : invoice.shipping_postal_code or getattr(invoice.customer, 'postal_code', '') or '',
                'state_to'   : invoice.shipping_state or invoice.place_of_supply or '',
            }
        form = EWayBillForm(instance=eway, initial=initial)

    return render(request, 'sales/eway_bill_generate.html', {
        'form'   : form,
        'invoice': invoice,
        'eway'   : eway,
        'company_is_india': True,
    })


@login_required
def eway_bill_detail(request, pk):
    """Read-only detail view for a generated E-Way Bill."""
    # Permission: require View on E-Way Bill
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_eway_bill(request.user)):
            messages.error(request, 'You do not have permission to view E-Way Bill details.')
            return redirect_with_company('eway_bill_list')
    except Exception:
        messages.error(request, 'You do not have permission to view E-Way Bill details.')
        return redirect_with_company('eway_bill_list')

    bill = get_object_or_404(EWayBill, pk=pk)
    return render(request, 'sales/eway_bill_detail.html', {'bill': bill})


@login_required
def eway_bill_list(request):
    """
    List all E-Way Bills with search + status filter.
    Pattern mirrors existing invoice/order list views.
    Also shows eligible invoices for E-Way Bill generation.
    """
    # Permission: require View on E-Way Bill
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_view_eway_bill(request.user)):
            messages.error(request, 'You do not have permission to access E-Way Bills.')
            return redirect_with_company('sales_dashboard')
    except Exception:
        messages.error(request, 'You do not have permission to access E-Way Bills.')
        print("sreevidya")
        return redirect_with_company('sales_dashboard')

    bills = EWayBill.objects.select_related(
        'invoice', 'invoice__customer'
    ).all()

    status    = request.GET.get('status', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to   = request.GET.get('date_to', '').strip()
    search    = request.GET.get('q', '').strip()

    if status:
        bills = bills.filter(status=status)
    if date_from:
        bills = bills.filter(created_at__date__gte=date_from)
    if date_to:
        bills = bills.filter(created_at__date__lte=date_to)
    if search:
        bills = bills.filter(
            Q(ewb_number__icontains=search)
            | Q(invoice__inv_number__icontains=search)
            | Q(invoice__customer__company_name__icontains=search)
            | Q(invoice__customer__first_name__icontains=search)
        )

    paginator   = Paginator(bills.order_by('-created_at'), 5)
    page_number = request.GET.get('page')
    page_obj    = paginator.get_page(page_number)

    # Get eligible invoices for E-Way Bill generation
    # Criteria: total_amount >= 50000 AND no existing E-Way Bill
    company = Company.objects.filter(status=True).first() or Company.objects.first()
    company_is_india = _is_indian_company_country(_get_current_company_country(request))
    
    eligible_invoices_qs = SalesInvoice.objects.none()
    eligible_page_obj = None
    
    if company_is_india:
        eligible_invoices_qs = SalesInvoice.objects.filter(
            total_amount__gte=EWAY_THRESHOLD
        ).exclude(
            eway_bill__isnull=False
        ).select_related(
            'customer'
        ).order_by('-date')
        
        # Paginate eligible invoices with 5 per page
        eligible_paginator = Paginator(eligible_invoices_qs, 5)
        eligible_page_number = request.GET.get('eligible_page')
        eligible_page_obj = eligible_paginator.get_page(eligible_page_number)

    return render(request, 'sales/eway_bill_list.html', {
        'bills'    : page_obj,
        'status'   : status,
        'search'   : search,
        'date_from': date_from,
        'date_to'  : date_to,
        'total_count': paginator.count,
        'eligible_invoices': eligible_page_obj,
        'eligible_count': eligible_invoices_qs.count(),
        'company_is_india': company_is_india,
    })


@login_required
@require_POST
def eway_bill_cancel(request, pk):
    """Cancel (void) an E-Way Bill that has not yet been submitted to GST portal."""
    # Permission: require Delete or Edit on E-Way Bill
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_eway_bill(request.user) or can_edit_eway_bill(request.user)):
            messages.error(request, 'You do not have permission to cancel E-Way Bills.')
            return redirect_with_company('eway_bill_detail', pk=pk)
    except Exception:
        messages.error(request, 'You do not have permission to cancel E-Way Bills.')
        return redirect_with_company('eway_bill_detail', pk=pk)

    bill = get_object_or_404(EWayBill, pk=pk)
    from django.contrib import messages
    if bill.status == 'cancelled':
        messages.warning(request, 'E-Way Bill is already cancelled.')
    else:
        bill.status = 'cancelled'
        bill.save(update_fields=['status'])
        messages.success(
            request,
            f'E-Way Bill {bill.ewb_number or "(draft)"} cancelled successfully.'
        )
    return redirect_with_company('eway_bill_detail', pk=pk)
