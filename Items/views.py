
from django.shortcuts import render, redirect,get_object_or_404
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_code
from .forms import ItemForm,UomForm,VendorForm
from Purchase.forms import ContactPersonFormSet
from brand.forms import BrandForm
from warehouse.forms import WarehouseForm,WarehouseFormModal
from unit.forms import UnitForm
from django.db import transaction
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError
from warehouse.models import Warehouse
from stock.models import Stock
from decimal import Decimal
from datetime import datetime
import logging
import csv
from urllib.parse import urlencode


from Purchase.models import Vendor, BillItem
from company.models import Company
from Items.models import Item
from journal.models import JournalEntry, JournalLine
from chart_of_accounts.models import ChartOfAccounts

from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.contrib import messages
from .models import HSNCode,Uom,SACCode,Barcode, Uom, Uom_name
from Tax.models import Tax, TaxGroup
from unit.models import Unit
from brand.models import Brand
from category.models import Category, Subcategory
from category.forms import CategoryForm, SubcategoryForm
from type.models import Type
from type.forms import TypeForm
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from .permissions import can_create_items, can_edit_items, can_delete_items

import json
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods

# Period Locking Integration
from system_settings.validators import PeriodLockEnforcer
from django.core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)

def _get_company_country_name(request=None):
    company = None
    companies = Company.objects.using('default')

    if request is not None:
        company_code = get_company_code(request)
        print("company_code", company_code)
        company_db = getattr(request, 'company_db', None)
        session_company_db = request.session.get('company_db') if hasattr(request, 'session') else None

        # Prefer the active tenant DB when available because company setup data
        # may be newer there than in the master DB.
        if company_db and company_db != 'default':
            tenant_companies = Company.objects.using(company_db)
            if company_code:
                company = tenant_companies.filter(company_code=company_code).only('country').first()
            if company is None:
                company = tenant_companies.only('country').order_by('id').first()

        if company is None and company_code:
            company = companies.filter(company_code=company_code).only('country').first()

        if company is None and company_db and company_db != 'default':
            company = companies.filter(db_name=company_db).only('country').first()

        if company is None and session_company_db and session_company_db != 'default':
            company = companies.filter(db_name=session_company_db).only('country').first()

    if company is None:
        company = companies.order_by('id').only('country').first()

    if not company or not getattr(company, 'country', None):
        return ''
    try:
        if getattr(company.country, 'code', None):
            return company.country.code or company.country.name or str(company.country)
        return company.country.name or str(company.country)
    except Exception:
        return str(company.country)


def _get_company_tax_type(request=None):
    company = None
    companies = Company.objects.using('default')

    if request is not None:
        company_code = get_company_code(request)
        company_db = getattr(request, 'company_db', None)
        session_company_db = request.session.get('company_db') if hasattr(request, 'session') else None

        if company_db and company_db != 'default':
            tenant_companies = Company.objects.using(company_db)
            if company_code:
                company = tenant_companies.filter(company_code=company_code).only('tax_type').first()
            if company is None:
                company = tenant_companies.only('tax_type').order_by('id').first()

        if company is None and company_code:
            company = companies.filter(company_code=company_code).only('tax_type').first()

        if company is None and company_db and company_db != 'default':
            company = companies.filter(db_name=company_db).only('tax_type').first()

        if company is None and session_company_db and session_company_db != 'default':
            company = companies.filter(db_name=session_company_db).only('tax_type').first()

    if company is None:
        company = companies.order_by('id').only('tax_type').first()

    return (getattr(company, 'tax_type', '') or '').strip().upper()


def _is_indian_company_country(company_country):
    normalized_country = (company_country or '').strip().lower()
    return normalized_country in {'india', 'in', 'ind'}


def _get_company_base_currency(request=None):
    company = None
    companies = Company.objects.using('default')

    if request is not None:
        company_code = get_company_code(request)
        company_db = getattr(request, 'company_db', None)
        session_company_db = request.session.get('company_db') if hasattr(request, 'session') else None
        company_id = request.session.get('company_id') if hasattr(request, 'session') else None

        if company_db and company_db != 'default':
            tenant_companies = Company.objects.using(company_db)
            if company_id:
                company = tenant_companies.filter(pk=company_id).only('base_currency').first()
            if company is None and company_code:
                company = tenant_companies.filter(company_code=company_code).only('base_currency').first()
            if company is None:
                company = tenant_companies.only('base_currency').order_by('id').first()

        if company is None and company_id:
            company = companies.filter(pk=company_id).only('base_currency').first()

        if company is None and company_code:
            company = companies.filter(company_code=company_code).only('base_currency').first()

        if company is None and company_db and company_db != 'default':
            company = companies.filter(db_name=company_db).only('base_currency').first()

        if company is None and session_company_db and session_company_db != 'default':
            company = companies.filter(db_name=session_company_db).only('base_currency').first()

    if company is None:
        company = companies.order_by('id').only('base_currency').first()

    return (getattr(company, 'base_currency', '') or '').strip().upper()[:10] or 'INR'


def _get_default_tax_context(item=None):
    default_tax_id = None
    default_tax_text = ''
    if item:
        default_tax_obj = getattr(item, 'tax', None)
        if default_tax_obj is None and getattr(item, 'intra_tax', None):
            default_tax_obj = item.intra_tax.taxes.first()
        if default_tax_obj:
            default_tax_id = default_tax_obj.id
            default_tax_text = getattr(default_tax_obj, 'display_name', None) or getattr(default_tax_obj, 'taxname', None) or getattr(default_tax_obj, 'name', None) or ''
    return {
        'default_tax_id': default_tax_id,
        'default_tax_text': default_tax_text,
    }


def _get_item_tax_context(item=None, post=None):
    context = {
        'default_tax_id': None,
        'default_tax_text': '',
        'intra_tax_id': None,
        'intra_tax_text': '',
        'inter_tax_id': None,
        'inter_tax_text': '',
        'sales_tax_id': None,
        'sales_tax_text': '',
        'purchase_tax_id': None,
        'purchase_tax_text': '',
    }

    if item is not None:
        context.update(_get_default_tax_context(item))
        if getattr(item, 'intra_tax_id', None):
            context['intra_tax_id'] = item.intra_tax_id
            context['intra_tax_text'] = getattr(item.intra_tax, 'group_name', '') if item.intra_tax else ''
        if getattr(item, 'inter_tax_group_id', None):
            context['inter_tax_id'] = item.inter_tax_group_id
            if item.inter_tax_group:
                context['inter_tax_text'] = getattr(item.inter_tax_group, 'taxname', '') or getattr(item.inter_tax_group, 'name', '')
        if getattr(item, 'sales_tax_id', None):
            context['sales_tax_id'] = item.sales_tax_id
            if item.sales_tax:
                context['sales_tax_text'] = getattr(item.sales_tax, 'taxname', '') or getattr(item.sales_tax, 'name', '')
        if getattr(item, 'purchase_tax_id', None):
            context['purchase_tax_id'] = item.purchase_tax_id
            if item.purchase_tax:
                context['purchase_tax_text'] = getattr(item.purchase_tax, 'taxname', '') or getattr(item.purchase_tax, 'name', '')
        return context

    if post is not None:
        default_tax_id = (post.get('tax') or '').strip()
        if default_tax_id:
            default_tax = Tax.objects.filter(id=default_tax_id).first()
            if default_tax:
                context['default_tax_id'] = default_tax.id
                context['default_tax_text'] = getattr(default_tax, 'display_name', None) or default_tax.taxname

        intra_tax_id = (post.get('intra_tax') or '').strip()
        if intra_tax_id:
            intra_tax = TaxGroup.objects.filter(id=intra_tax_id).first()
            if intra_tax:
                context['intra_tax_id'] = intra_tax.id
                context['intra_tax_text'] = intra_tax.group_name

        inter_tax_id = (post.get('inter_tax_group') or '').strip()
        if inter_tax_id:
            inter_tax = Tax.objects.filter(id=inter_tax_id).first()
            if inter_tax:
                context['inter_tax_id'] = inter_tax.id
                context['inter_tax_text'] = getattr(inter_tax, 'taxname', '') or getattr(inter_tax, 'name', '')

        sales_tax_id = (post.get('sales_tax') or '').strip()
        if sales_tax_id:
            sales_tax = Tax.objects.filter(id=sales_tax_id).first()
            if sales_tax:
                context['sales_tax_id'] = sales_tax.id
                context['sales_tax_text'] = getattr(sales_tax, 'taxname', '') or getattr(sales_tax, 'name', '')

        purchase_tax_id = (post.get('purchase_tax') or '').strip()
        if purchase_tax_id:
            purchase_tax = Tax.objects.filter(id=purchase_tax_id).first()
            if purchase_tax:
                context['purchase_tax_id'] = purchase_tax.id
                context['purchase_tax_text'] = getattr(purchase_tax, 'taxname', '') or getattr(purchase_tax, 'name', '')

    return context


def _validate_item_tax_selection(post_data, company_country, company_tax_type=''):
    company_tax_type = (company_tax_type or '').strip().upper()

    if company_tax_type in ('NONE',):
        return None

    if post_data.get('tax_pref', 'taxable') != 'taxable':
        return None

    if company_tax_type == 'GST':
        if not (post_data.get('intra_tax') or '').strip():
            return "Please select Intra State Tax Rate."
        if not (post_data.get('inter_tax_group') or '').strip():
            return "Please select Inter State Tax Rate."
        return None

    if company_tax_type == 'SALES':
        if not (post_data.get('sales_tax') or '').strip():
            return "Please select Sales Tax Rate."
        if not (post_data.get('purchase_tax') or '').strip():
            return "Please select Purchase Tax Rate."
        return None

    if not (post_data.get('tax') or '').strip():
        return "Please select Tax Rate."
    return None


def _assign_item_tax_fields(item, post_data, company_country, company_tax_type=''):
    company_tax_type = (company_tax_type or '').strip().upper()

    if company_tax_type in ('NONE',):
        item.tax_pref = 'non_taxable'
        item.intra_tax = None
        item.inter_tax_group = None
        item.sales_tax = None
        item.purchase_tax = None
        return

    if post_data.get('tax_pref', 'taxable') != 'taxable':
        item.tax_pref = 'non_taxable'
        item.intra_tax = None
        item.inter_tax_group = None
        item.sales_tax = None
        item.purchase_tax = None
        return

    item.tax_pref = 'taxable'
    if company_tax_type == 'GST':
        intra_tax_id = (post_data.get('intra_tax') or '').strip()
        inter_tax_id = (post_data.get('inter_tax_group') or '').strip()
        item.intra_tax = TaxGroup.objects.filter(id=intra_tax_id).first() if intra_tax_id else None
        item.inter_tax_group = Tax.objects.filter(id=inter_tax_id).first() if inter_tax_id else None
        item.sales_tax = None
        item.purchase_tax = None
        return

    if company_tax_type == 'SALES':
        item.intra_tax = None
        item.inter_tax_group = None
        sales_tax_id = (post_data.get('sales_tax') or '').strip()
        purchase_tax_id = (post_data.get('purchase_tax') or '').strip()
        item.sales_tax = Tax.objects.filter(id=sales_tax_id).first() if sales_tax_id else None
        item.purchase_tax = Tax.objects.filter(id=purchase_tax_id).first() if purchase_tax_id else None
        return

    item.inter_tax_group = None
    item.sales_tax = None
    item.purchase_tax = None
    default_tax_id = (post_data.get('tax') or '').strip()
    if default_tax_id:
        default_tax_obj = Tax.objects.filter(id=default_tax_id).first()
        if default_tax_obj:
            item.intra_tax = _get_or_create_tax_group_for_tax(default_tax_obj)
        else:
            item.intra_tax = None
    else:
        item.intra_tax = None


def _get_or_create_tax_group_for_tax(tax_obj):
    if not tax_obj:
        return None
    group = TaxGroup.objects.filter(taxes=tax_obj).first()
    if group:
        return group
    group_name = getattr(tax_obj, 'display_name', None) or getattr(tax_obj, 'taxname', None) or getattr(tax_obj, 'name', None) or f"Tax {tax_obj.id}"
    group = TaxGroup.objects.create(group_name=group_name)
    group.taxes.add(tax_obj)
    return group


def _get_tax_response_payload(item, prefer='purchase'):
    tax_id = None
    tax_name = ''
    tax_rate = 0

    if getattr(item, 'tax_pref', '') == 'non_taxable':
        return tax_id, tax_name, tax_rate

    tax_obj = getattr(item, 'purchase_tax', None) if prefer == 'purchase' else getattr(item, 'sales_tax', None)
    if tax_obj:
        tax_id = f"tax:{tax_obj.id}"
        tax_rate = float(getattr(tax_obj, 'rate', 0) or 0)
        tax_name = getattr(tax_obj, 'taxname', '') or getattr(tax_obj, 'name', '')
        return tax_id, tax_name, tax_rate

    if getattr(item, 'intra_tax', None):
        tax_id = f"group:{item.intra_tax.id}"
        tax_rate = float(item.intra_tax.taxes.aggregate(total=Sum("rate"))["total"] or 0)
        tax_name = getattr(item.intra_tax, 'group_name', '') or str(item.intra_tax)
        return tax_id, tax_name, tax_rate

    return tax_id, tax_name, tax_rate


def _get_posted_item_lookup_context(request):
    category_id = request.POST.get('category') or ''
    category_text = ''
    if category_id:
        category_obj = Category.objects.filter(id=category_id, status=True).first()
        if category_obj:
            category_text = category_obj.category_name

    subcategory_id = request.POST.get('subcategory') or ''
    subcategory_text = ''
    if subcategory_id:
        subcategory_obj = Subcategory.objects.filter(id=subcategory_id, status=True).select_related('category').first()
        if subcategory_obj:
            subcategory_text = subcategory_obj.subcategory_name

    item_type_id = request.POST.get('item_type') or ''
    item_type_text = ''
    if item_type_id:
        item_type_obj = Type.objects.filter(id=item_type_id, status=True).first()
        if item_type_obj:
            item_type_text = item_type_obj.type_name

    return {
        'category_id': category_id,
        'category_text': category_text,
        'subcategory_id': subcategory_id,
        'subcategory_text': subcategory_text,
        'item_type_id': item_type_id,
        'item_type_text': item_type_text,
    }


def _validate_item_lookup_selection(post_data):
    category_id = (post_data.get('category') or '').strip()
    subcategory_id = (post_data.get('subcategory') or '').strip()
    item_type_id = (post_data.get('item_type') or '').strip()

    if subcategory_id:
        subcategory = Subcategory.objects.filter(id=subcategory_id, status=True).first()
        if not subcategory:
            return "Please select a valid Subcategory."
        if category_id and str(subcategory.category_id) != str(category_id):
            return "Selected Subcategory does not belong to the selected Category."

    if item_type_id:
        item_type = Type.objects.filter(id=item_type_id, status=True).first()
        if not item_type:
            return "Please select a valid Type."
        if subcategory_id and str(item_type.subcategory_id or '') != str(subcategory_id):
            return "Selected Type does not belong to the selected Subcategory."

    return None

def _preserve_post_data_for_error_rendering(request):
    """
    Extract and preserve POST data values for form re-rendering after validation errors.
    This ensures that user-entered values are not lost when validation fails.
    """
    if request.method != "POST":
        return {}
    
    # Get warehouse ID and look up its name
    warehouse_id = request.POST.get('warehouse', '')
    warehouse_text = ''
    if warehouse_id:
        try:
            warehouse_obj = Warehouse.objects.filter(id=warehouse_id).first()
            if warehouse_obj:
                warehouse_text = warehouse_obj.warehouse_name or warehouse_obj.name or str(warehouse_obj)
        except Exception:
            pass
    
    # Get unit ID and look up its name
    unit_id = request.POST.get('unit', '')
    unit_text = ''
    if unit_id:
        try:
            unit_obj = Unit.objects.filter(id=unit_id).first()
            if unit_obj:
                unit_text = unit_obj.unit_name or str(unit_obj)
        except Exception:
            pass
    
    return {
        'warehouse_id': warehouse_id,
        'warehouse_text': warehouse_text,
        'op_stock_value': request.POST.get('op_stock', ''),
        'op_rate_value': request.POST.get('op_rate', ''),
        'min_stock_value': request.POST.get('min_stock', ''),
        'reorder_qty_value': request.POST.get('reorder_qty', ''),
        'track_inventory': request.POST.get('track_inventory', False),
        'brcd_value': request.POST.get('brcd', ''),
        'opening_stock_equity_account_id': request.POST.get('opening_stock_equity_account', ''),
        'default_unit_id': unit_id,
        'default_unit_text': unit_text,
        'barcodes_list': request.POST.getlist('barcodes[]'),
        'uom_names': request.POST.getlist('uom[]'),
        'conv_factors': request.POST.getlist('conversion_factor[]'),
        'barcode_values': request.POST.getlist('barcode[]'),
    }

def _build_add_item_context(request, form, units, vendors, hsn, warehouses, company_country, **extra):
    company_db = getattr(request, 'company_db', 'default')
    vendor_id = request.POST.get('preferred_vendor') if request.method == "POST" else ''
    vendor_text = ''
    if vendor_id:
        vendor_obj = Vendor.objects.using(company_db).filter(id=vendor_id).first()
        if vendor_obj:
            vendor_text = str(vendor_obj)

    context = {
        'units': units,
        'vendors': vendors,
        'hsn': hsn,
        'form': form,
        'warehouses': warehouses,
        'company_country': company_country,
        'company_is_india': _is_indian_company_country(company_country),
        'company_tax_type': _get_company_tax_type(request),
        'base_currency': _get_company_base_currency(request),
        'can_create': (getattr(request.user, 'is_superuser', False) or can_create_items(request.user)),
        'can_edit': (getattr(request.user, 'is_superuser', False) or can_edit_items(request.user)),
        'vendor_id': vendor_id,
        'vendor_text': vendor_text,
    }
    if request.method == "POST":
        context.update(_get_posted_item_lookup_context(request))
        context.update(_get_item_tax_context(post=request.POST))
        # ✅ NEW: Preserve POST data for error re-rendering so user doesn't lose input
        preserved_data = _preserve_post_data_for_error_rendering(request)
        context.update(preserved_data)
        # ✅ Map preserved unit and warehouse data to context variables used by template JS
        context['current_unit_id'] = preserved_data.get('default_unit_id', '')
        context['current_unit_name'] = preserved_data.get('default_unit_text', '')
        context['current_warehouse_id'] = preserved_data.get('warehouse_id', '')
        context['current_warehouse_name'] = preserved_data.get('warehouse_text', '')
    else:
        default_warehouse = Warehouse.objects.using(company_db).filter(
            status=True,
            is_default=True,
        ).first()
        if default_warehouse:
            context.update({
                'warehouse_id': default_warehouse.pk,
                'warehouse_text': default_warehouse.warehouse_name,
                'current_warehouse_id': default_warehouse.pk,
                'current_warehouse_name': default_warehouse.warehouse_name,
            })
        context.update(_get_item_tax_context())
    context.update(extra)
    return context

#byadarsh
def ensure_opening_stock_accounts_exist(request=None):
    """
    Ensure required accounts exist in Chart of Accounts.
    Creates account hierarchy: Equity -> Opening Balance Equity
    and Stock Assets -> Stock In Hand
    Returns: (inventory_account, equity_account)
    """
    try:
        # 1. Ensure Equity parent account exists
        equity_parent = ChartOfAccounts.objects.filter(
            name__iexact='Equity',
            status=True,
            is_header=True
        ).first()
        
        if not equity_parent:
            equity_parent = ChartOfAccounts.objects.create(
                name='Equity',
                code='5',
                type='5',  # Equity type
                parent=None,
                is_header=True,
                status=True
            )
        
        # 2. Create Opening Balance Equity under Equity
        opening_equity_account = ChartOfAccounts.objects.filter(
            name__iexact='Opening Balance Equity',
            status=True
        ).first()
        
        if not opening_equity_account:
            opening_equity_account = ChartOfAccounts.objects.create(
                name='Opening Balance Equity',
                code='5.01',
                type='5',  # Equity
                parent=equity_parent,
                is_header=False,
                status=True
            )
        
        # 3. Ensure Stock Assets parent account exists
        stock_assets_parent = ChartOfAccounts.objects.filter(
            name__iexact='Stock Assets',
            status=True,
            is_header=True
        ).first()
        
        if not stock_assets_parent:
            stock_assets_parent = ChartOfAccounts.objects.create(
                name='Stock Assets',
                code='1.01',
                type='1',  # Asset type
                parent=None,
                is_header=True,
                status=True
            )
        
        # 4. Create Stock In Hand under Stock Assets
        inventory_account = ChartOfAccounts.objects.filter(
            name__iexact='Stock In Hand',
            status=True
        ).first()
        
        if not inventory_account:
            inventory_account = ChartOfAccounts.objects.create(
                name='Stock In Hand',
                code='1.01.01',
                type='1',  # Asset
                parent=stock_assets_parent,
                is_header=False,
                status=True
            )
        
        return inventory_account, opening_equity_account
        
    except Exception as e:
        logger.error(f"Error ensuring accounts exist: {e}", exc_info=True)
        return None, None


def post_opening_stock_journal_entry(stock_record, user, request=None, equity_account=None):
    """
    Post opening stock to journal when an item is created with opening stock.
    
    Creates two-entry journal:
    - Debit: Stock In Hand (Inventory Asset)
    - Credit: Selected Equity Account (or Opening Balance Equity if not specified)
    
    Args:
        stock_record: Stock model instance with opening_stock value
        user: User creating the entry (for audit trail)
        request: HTTP request (optional, for company database context)
        equity_account: ChartOfAccounts instance to post credit (optional, defaults to Opening Balance Equity)
    
    Returns:
        tuple: (success: bool, entry: JournalEntry or None, message: str)
    """
    try:
        if not stock_record:
            return (False, None, "No stock record provided")
        
        # Convert opening_stock to Decimal first (it might be stored as string)
        open_qty = Decimal(str(stock_record.opening_stock))
        
        if open_qty <= 0:
            return (False, None, "No opening stock to post")
        
        # Ensure Stock In Hand account exists
        inventory_account, _ = ensure_opening_stock_accounts_exist(request)
        
        # Use provided equity account, or find default opening_equity_account
        if equity_account:
            opening_equity_account = equity_account
        else:
            _, opening_equity_account = ensure_opening_stock_accounts_exist(request)
        
        if not inventory_account or not opening_equity_account:
            return (False, None, "Required accounts not found")
        
        # 🔥 REQUIREMENT: Opening rate MUST be explicitly provided
        # Do NOT auto-derive from purchase bills (accounting best practice)
        actual_cost = Decimal('0.00')
        
        if stock_record.item.op_rate and Decimal(str(stock_record.item.op_rate)) > 0:
            try:
                actual_cost = Decimal(str(stock_record.item.op_rate))
                
                # Convert from GST-inclusive to GST-exclusive if needed
                if stock_record.item.taxincld_costprice and stock_record.item.intra_tax:
                    try:
                        tax_sum = stock_record.item.intra_tax.taxes.aggregate(Sum('rate'))['rate__sum']
                        if tax_sum:
                            total_tax_rate = Decimal(str(tax_sum))
                            multiplier = Decimal('1.00') + (total_tax_rate / Decimal('100.00'))
                            actual_cost = actual_cost / multiplier
                            logger.info(f"Converted GST-inclusive rate from ₹{stock_record.item.op_rate} to ₹{actual_cost}")
                    except Exception as e:
                        logger.warning(f"Failed to convert GST rate: {e}")
            except Exception as e:
                logger.error(f"Invalid opening rate for {stock_record.item.name}: {e}")
                actual_cost = Decimal('0.00')
        
        # ❌ NO FALLBACK: If user didn't provide opening rate, reject immediately
        if actual_cost <= 0:
            return (False, None, 
                f"❌ REQUIRED: Opening Stock Rate\n\n"
                f"Item: {stock_record.item.name}\n"
                f"Opening Qty: {open_qty} units\n\n"
                f"You must explicitly enter 'Opening Stock Rate per Unit' when creating item.\n"
                f"Opening stock is a historical balance and cannot be auto-estimated.\n\n"
                f"💡 Accounting Standard: All opening balances require explicit rates.")
        
        stock_value = open_qty * actual_cost
        
        # Create entry reference with item name
        entry_reference = f"OPENING_STOCK_{stock_record.item.name.upper()}"

        # by adarshCheck for an existing (non-reversal) opening stock entry.
        # If the existing entry has already been reversed (there is a reversal
        # journal whose narration starts with 'Reversal of <entry_number>'),
        # allow creation of a new opening-stock entry. This preserves history
        # while enabling updates.
        existing_entry = JournalEntry.objects.filter(reference=entry_reference).exclude(narration__startswith='Reversal of').order_by('-id').first()

        if existing_entry:
            # If a reversal exists for this existing entry, permit creating a new one.
            reversal_exists = JournalEntry.objects.filter(narration__startswith=f"Reversal of {existing_entry.entry_number}").exists()
            if not reversal_exists:
                return (False, None, "Entry already posted")
        
        # Use JV-style numbering for opening stock entries so they share the
        # same sequence namespace as other journals (prevents entry-type conflicts)
        last_jv = JournalEntry.objects.filter(entry_number__startswith='JV-').order_by('-id').first()
        if last_jv and last_jv.entry_number and last_jv.entry_number.startswith('JV-'):
            try:
                last_num = int(last_jv.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        entry_number = f'JV-{str(last_num + 1).zfill(5)}'
        
        # Create journal entry
        je = JournalEntry.objects.create(
            entry_number=entry_number,
            date=datetime.now().date(),
            reference=entry_reference,
            narration=f"Opening Stock: {stock_record.item.name} ({open_qty} units @ ₹{actual_cost}/unit)",
            total_debit=stock_value,
            total_credit=stock_value,
            status='posted',
            created_by=user,
            updated_by=user
        )
        
        # Debit: Inventory Asset
        JournalLine.objects.create(
            journal=je,
            account=inventory_account,
            description=f"Opening Stock: {stock_record.item.name}",
            debit=stock_value,
            credit=Decimal('0.00'),
            sequence=10,
            status=True
        )
        
        # Credit: Opening Balance Equity
        JournalLine.objects.create(
            journal=je,
            account=opening_equity_account,
            description=f"Opening Stock: {stock_record.item.name}",
            debit=Decimal('0.00'),
            credit=stock_value,
            sequence=20,
            status=True
        )
        
        logger.info(f"✓ Posted opening stock for {stock_record.item.name}: ₹{stock_value}")
        
        return (True, je, f"✓ Posted ₹{float(stock_value):.2f}")
        
    except Exception as e:
        logger.error(f"Error posting opening stock: {e}", exc_info=True)
        return (False, None, f"Error: {str(e)}")


def ensure_stock_adjustment_account_exists(request=None):
    """
    Get existing Stock Adjustment account from Chart of Accounts.
    Stock Adjustment is already under Expenses (type='4'), not Equity.
    
    Returns: (inventory_account, stock_adjustment_account)
    """
    try:
        # Find Stock Adjustment account (under Expenses)
        stock_adjustment_account = ChartOfAccounts.objects.filter(
            name__iexact='Stock Adjustment',
            status=True
        ).first()
        
        if not stock_adjustment_account:
            logger.error("Stock Adjustment account not found in Chart of Accounts")
            return None, None
        
        # Ensure Stock In Hand account exists
        stock_assets_parent = ChartOfAccounts.objects.filter(
            name__iexact='Stock Assets',
            status=True,
            is_header=True
        ).first()
        
        if not stock_assets_parent:
            stock_assets_parent = ChartOfAccounts.objects.create(
                name='Stock Assets',
                code='1.01',
                type='1',  # Asset type
                parent=None,
                is_header=True,
                status=True
            )
        
        inventory_account = ChartOfAccounts.objects.filter(
            name__iexact='Stock In Hand',
            status=True
        ).first()
        
        if not inventory_account:
            inventory_account = ChartOfAccounts.objects.create(
                name='Stock In Hand',
                code='1.01.01',
                type='1',  # Asset
                parent=stock_assets_parent,
                is_header=False,
                status=True
            )
        
        return inventory_account, stock_adjustment_account
        
    except Exception as e:
        logger.error(f"Error ensuring stock adjustment accounts exist: {e}", exc_info=True)
        return None, None



def post_stock_adjustment_journal_entry(stock_record, user, request=None):
    """
    Auto-post stock adjustment journal entry when item created WITHOUT equity account.
    
    This handles the case when user creates item with opening stock but does NOT
    select an opening stock equity account. In this case, we automatically post:
    
    Dr Stock In Hand (Inventory Asset)
    Cr Stock Adjustment (Expense - for opening stock entry)
    
    Args:
        stock_record: Stock model instance with opening_stock value
        user: User creating the entry (for audit trail)
        request: HTTP request (optional, for company database context)
    
    Returns:
        tuple: (success: bool, entry: JournalEntry or None, message: str)
    """

    try:
        if not stock_record:
            return (False, None, "No stock record provided")
        
        # Convert opening_stock to Decimal
        open_qty = Decimal(str(stock_record.opening_stock))
        
        if open_qty <= 0:
            return (False, None, "No opening stock to post")
        
        # Get opening rate (required)
        actual_cost = Decimal('0.00')
        if stock_record.item.op_rate and Decimal(str(stock_record.item.op_rate)) > 0:
            try:
                actual_cost = Decimal(str(stock_record.item.op_rate))
                
                # Convert from GST-inclusive to GST-exclusive if needed
                if stock_record.item.taxincld_costprice and stock_record.item.intra_tax:
                    try:
                        tax_sum = stock_record.item.intra_tax.taxes.aggregate(Sum('rate'))['rate__sum']
                        if tax_sum:
                            total_tax_rate = Decimal(str(tax_sum))
                            multiplier = Decimal('1.00') + (total_tax_rate / Decimal('100.00'))
                            actual_cost = actual_cost / multiplier
                    except Exception as e:
                        logger.warning(f"Failed to convert GST rate: {e}")
            except Exception as e:
                logger.error(f"Invalid opening rate for {stock_record.item.name}: {e}")
                actual_cost = Decimal('0.00')
        
        # Require opening rate
        if actual_cost <= 0:
            return (False, None, 
                f"❌ REQUIRED: Opening Stock Rate\n\n"
                f"Item: {stock_record.item.name}\n"
                f"Opening Qty: {open_qty} units\n\n"
                f"You must explicitly enter 'Opening Stock Rate per Unit' when creating item.")
        
        stock_value = open_qty * actual_cost
        
        # Ensure accounts exist
        inventory_account, adjustment_account = ensure_stock_adjustment_account_exists(request)
        if not inventory_account or not adjustment_account:
            return (False, None, "Required accounts not found")
        
        # Create entry reference
        entry_reference = f"OPENING_STOCK_ADJUSTMENT_{stock_record.item.name.upper()}"
        
        # Check for existing entry
        existing_entry = JournalEntry.objects.filter(reference=entry_reference).exclude(narration__startswith='Reversal of').order_by('-id').first()
        if existing_entry:
            reversal_exists = JournalEntry.objects.filter(narration__startswith=f"Reversal of {existing_entry.entry_number}").exists()
            if not reversal_exists:
                return (False, None, "Entry already posted")
        
        # Generate entry number
        last_jv = JournalEntry.objects.filter(entry_number__startswith='JV-').order_by('-id').first()
        if last_jv and last_jv.entry_number and last_jv.entry_number.startswith('JV-'):
            try:
                last_num = int(last_jv.entry_number.split('-')[1])
            except Exception:
                last_num = 0
        else:
            last_num = 0
        entry_number = f'JV-{str(last_num + 1).zfill(5)}'
        
        # Create journal entry
        je = JournalEntry.objects.create(
            entry_number=entry_number,
            date=datetime.now().date(),
            reference=entry_reference,
            narration=f"Opening Stock Adjustment: {stock_record.item.name} ({open_qty} units @ ₹{actual_cost}/unit)",
            total_debit=stock_value,
            total_credit=stock_value,
            status='posted',
            created_by=user,
            updated_by=user
        )
        
        # Debit: Stock In Hand
        JournalLine.objects.create(
            journal=je,
            account=inventory_account,
            description=f"Opening Stock Adjustment: {stock_record.item.name}",
            debit=stock_value,
            credit=Decimal('0.00'),
            sequence=10,
            status=True
        )
        
        # Credit: Stock Adjustment Account
        JournalLine.objects.create(
            journal=je,
            account=adjustment_account,
            description=f"Opening Stock Adjustment: {stock_record.item.name}",
            debit=Decimal('0.00'),
            credit=stock_value,
            sequence=20,
            status=True
        )
        
        logger.info(f"✓ Posted opening stock adjustment for {stock_record.item.name}: ₹{stock_value}")
        return (True, je, f"✓ Posted ₹{float(stock_value):.2f} to Stock Adjustment")
        
    except Exception as e:
        logger.error(f"Error posting stock adjustment journal: {e}", exc_info=True)
        return (False, None, f"Error: {str(e)}")

#byadarsh

def _bool_to_str(value):
    return "true" if value else "false"

def _export_items_csv(items_qs):
    from .import_views import IMPORT_FIELDS

    items_qs = items_qs.filter(status=True)


    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="items_export.csv"'
    writer = csv.writer(response)

    writer.writerow([label for _, label, *_ in IMPORT_FIELDS])

    items_qs = items_qs.select_related(
        "brand",
        "category",
        "item_type",
        "preferred_vendor",
        "intra_tax",
        "inter_tax_group",
        "main_barcode",
    ).prefetch_related("barcodes")

    items_list = list(items_qs)

    unit_ids = {
        int(str(it.unit).strip())
        for it in items_list
        if str(it.unit or "").strip().isdigit()
    }
    unit_map = {
        u.id: u.unit_name
        for u in Unit.objects.filter(id__in=unit_ids)
    }

    for it in items_list:
        raw_unit = str(it.unit or "").strip()
        unit_name = unit_map.get(int(raw_unit), raw_unit) if raw_unit.isdigit() else raw_unit

        barcode_val = ""
        if it.main_barcode and it.main_barcode.barcode:
            barcode_val = it.main_barcode.barcode
        else:
            first_barcode = next((b for b in it.barcodes.all() if b and b.barcode), None)
            if first_barcode:
                barcode_val = first_barcode.barcode

        row = []
        for key, label, *_ in IMPORT_FIELDS:
            if key == "name":
                val = it.name
            elif key == "type" and "goods/service" in label.lower():
                val = it.type
            elif key == "item_type":
                val = it.item_type.type_name if it.item_type else ""
            elif key == "unit":
                val = unit_name
            elif key == "brand":
                val = it.brand.brand_name if it.brand else ""
            elif key == "category":
                val = it.category.category_name if it.category else ""
            elif key == "hsn_code":
                val = it.hsn_code or ""
            elif key == "sac_code":
                val = it.sac_code or ""
            elif key == "barcode":
                val = barcode_val
            # elif key == "status":
            #     val = _bool_to_str(it.status)
            elif key == "selling_price":
                val = str(it.selling_price) if it.selling_price is not None else ""
            elif key == "sales_account":
                val = it.sales_account or ""
            elif key == "sales_desc":
                val = it.sales_desc or ""
            elif key == "taxincld_slprice":
                val = _bool_to_str(it.taxincld_slprice)
            elif key == "cost_price":
                val = str(it.cost_price) if it.cost_price is not None else ""
            elif key == "purchase_account":
                val = it.purchase_account or ""
            elif key == "purchase_desc":
                val = it.purchase_desc or ""
            elif key == "taxincld_costprice":
                val = _bool_to_str(it.taxincld_costprice)
            elif key == "preferred_vendor":
                val = str(it.preferred_vendor) if it.preferred_vendor else ""
            elif key == "tax_pref":
                val = it.tax_pref or ""
            elif key == "intra_tax":
                val = str(it.intra_tax) if it.intra_tax else ""
            elif key == "inter_tax_group":
                val = str(it.inter_tax_group) if it.inter_tax_group else ""
            elif key == "track_inventory":
                val = _bool_to_str(it.track_inventory)
            elif key == "op_stock":
                val = str(it.op_stock) if it.op_stock is not None else ""
            elif key == "op_rate":
                val = str(it.op_rate) if it.op_rate is not None else ""
            elif key == "inv_method":
                val = it.inv_method or ""
            elif key == "inv_acc":
                val = it.inv_acc or ""
            elif key == "min_stock":
                val = str(it.min_stock) if it.min_stock is not None else ""
            else:
                val = ""

            row.append(val)

        writer.writerow(row)

    return response

def items(request):
    search_query = request.GET.get('q', '')
    items = Item.objects.filter(status=True).order_by('-created_at')
    if search_query:
        items = items.filter(Q(name__icontains=search_query))

    # --- Category / Subcategory / Type filters ---
    def _int_or_none(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    selected_category = _int_or_none(request.GET.get('category'))
    selected_subcategory = _int_or_none(request.GET.get('subcategory'))
    selected_type = _int_or_none(request.GET.get('type'))
    if selected_category:
        items = items.filter(category_id=selected_category)
    if selected_subcategory:
        items = items.filter(subcategory_id=selected_subcategory)
    if selected_type:
        items = items.filter(item_type_id=selected_type)

    # Query string (without page) reused by pagination / export links
    filter_params = {}
    if search_query:
        filter_params['q'] = search_query
    if selected_category:
        filter_params['category'] = selected_category
    if selected_subcategory:
        filter_params['subcategory'] = selected_subcategory
    if selected_type:
        filter_params['type'] = selected_type
    filter_qs = urlencode(filter_params)

    if request.GET.get("export") == "csv":
        return _export_items_csv(items)

    paginator = Paginator(items, 10)
    page_number = request.GET.get('page')
    items_page = paginator.get_page(page_number)

    # Compute stock-in-hand (sum of Stock.quantity) for items on this page
    item_ids = [it.id for it in items_page.object_list]
    stock_totals_qs = Stock.objects.filter(item_id__in=item_ids).values('item_id').annotate(total=Sum('quantity'))
    stock_totals = {d['item_id']: d['total'] for d in stock_totals_qs}
    # Attach stock_in_hand attribute to each item instance for template use
    for it in items_page.object_list:
        it.stock_in_hand = stock_totals.get(it.id) or None

    context = {
        "items": items_page,
        "search_query": search_query,
        "filter_qs": filter_qs,
        "filter_categories": list(Category.objects.filter(status=True).order_by('category_name').values('id', 'category_name')),
        "filter_subcategories": list(Subcategory.objects.filter(status=True).order_by('subcategory_name').values('id', 'subcategory_name', 'category_id')),
        "filter_types": list(Type.objects.filter(status=True).order_by('type_name').values('id', 'type_name', 'subcategory_id')),
        "selected_category": selected_category,
        "selected_subcategory": selected_subcategory,
        "selected_type": selected_type,
        "can_create": (getattr(request.user, 'is_superuser', False) or can_create_items(request.user)),
        "can_edit": (getattr(request.user, 'is_superuser', False) or can_edit_items(request.user)),
        "can_delete": (getattr(request.user, 'is_superuser', False) or can_delete_items(request.user)),
        "base_currency_code": _get_company_base_currency(request),
    }
    return render(request, "items.html", context)


# def add_item(request):
#     units = ["kg", "litre", "pcs"]  # Example unit list
#     vendors = Vendor.objects.all()
#     hsn=HSNCode.objects.all()

#     if request.method == "POST":
#         form = ItemForm(request.POST)
#         if form.is_valid():
#             form.save()
#             messages.success(request, "New item created successfully!")  # ✅ Success message

#             return redirect('items')  # Redirect to a page showing all items
#     else:
#         form = ItemForm()

#     return render(request, 'add_item.html', {
#         'units': units,
#         'vendors': vendors,
#         'hsn': hsn,unit_search
#         'form': form,
#     })
@login_required
def add_item(request):
    vendors = Vendor.objects.all()
    hsn = HSNCode.objects.all()
    units = ["kg", "litre", "pcs"]  # or fetch dynamically if preferred
    warehouses = Warehouse.objects.all()
    company_country = _get_company_country_name(request)
    company_tax_type = _get_company_tax_type(request)
    
    # require create permission
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_create_items(request.user)):
            messages.error(request, 'You do not have permission to create Items.')
            return redirect_with_company('items')
    except Exception:
        messages.error(request, 'You do not have permission to create Items.')
        return redirect_with_company('items')

    if request.method == "POST":
        # 🔒 by adarshlockPERIOD LOCKING: Check if today is in a locked period
        from django.utils import timezone
        today = timezone.now().date()
        db = getattr(request, 'company_db', 'default')
        try:
            PeriodLockEnforcer.check_can_edit(today, request.user, db=db, transaction_type='item')
        except PermissionDenied as e:
            # For AJAX requests, return JSON error response
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': f"❌ Cannot create item: {str(e)}"}, status=403)
            # Render form page with error modal instead of redirecting
            context = {
                'units': units,
                'vendors': vendors,
                'hsn': hsn,
                'form': ItemForm(),
                'warehouses': warehouses,
                'company_country': company_country,
                'company_is_india': _is_indian_company_country(company_country),
                'company_tax_type': company_tax_type,
                'base_currency': _get_company_base_currency(request),
                'error_message': str(e),
                'show_error_modal': True,
            }
            return render(request, 'add_item.html', context)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': f"❌ Cannot create item: {str(e)}"}, status=403)
            # Render form page with error modal instead of redirecting
            context = _build_add_item_context(
                request,
                ItemForm(),
                units,
                vendors,
                hsn,
                warehouses,
                company_country,
                error_message=str(e),
                show_error_modal=True,
            )
            return render(request, 'add_item.html', context)
        print("POST request received")
        post_data = request.POST.copy()
        if (company_tax_type or '').strip().upper() in ('NONE',):
            post_data['tax_pref'] = 'non_taxable'

        form = ItemForm(post_data)

        if form.is_valid():
            lookup_error = _validate_item_lookup_selection(post_data)
            if lookup_error:
                messages.error(request, lookup_error)
                context = _build_add_item_context(
                    request,
                    form,
                    units,
                    vendors,
                    hsn,
                    warehouses,
                    company_country,
                )
                return render(request, 'add_item.html', context)

            tax_error = _validate_item_tax_selection(post_data, company_country, company_tax_type)
            if tax_error:
                messages.error(request, tax_error)
                context = _build_add_item_context(
                    request,
                    form,
                    units,
                    vendors,
                    hsn,
                    warehouses,
                    company_country,
                )
                return render(request, 'add_item.html', context)
            # ===== PRE-VALIDATION: Validate ALL required data BEFORE saving item =====
            # This ensures that if validation fails, nothing gets saved to the database
            
            # Validate main barcode
            brcd_value = request.POST.get('brcd', '').strip()
            print('Barcode received:', brcd_value)
            if brcd_value:
                if Barcode.objects.filter(barcode=brcd_value).exists():
                    messages.error(request, f"The barcode '{brcd_value}' already exists. Please enter a unique barcode.")
                    context = _build_add_item_context(
                        request,
                        form,
                        units,
                        vendors,
                        hsn,
                        warehouses,
                        company_country,
                    )
                    return render(request, 'add_item.html', context)
            
            # Validate additional barcodes list
            barcodes_list = request.POST.getlist('barcodes[]')
            for code in barcodes_list:
                code_stripped = code.strip()
                if code_stripped:
                    if Barcode.objects.filter(barcode=code_stripped).exists():
                        messages.error(request, f"The barcode '{code_stripped}' already exists. Please enter a unique barcode.")
                        context = _build_add_item_context(
                            request,
                            form,
                            units,
                            vendors,
                            hsn,
                            warehouses,
                            company_country,
                        )
                        return render(request, 'add_item.html', context)
            
            # Validate UOM barcodes
            uom_names = request.POST.getlist('uom[]')
            conv_factors = request.POST.getlist('conversion_factor[]')
            barcode_values = request.POST.getlist('barcode[]')  # <- user entered barcode strings
            
            for name_id, conv, brcd_val in zip(uom_names, conv_factors, barcode_values):
                if name_id and brcd_val and brcd_val.strip():
                    if Barcode.objects.filter(barcode=brcd_val.strip()).exists():
                        messages.error(request, f"The barcode '{brcd_val.strip()}' already exists. Please enter a unique barcode.")
                        context = _build_add_item_context(
                            request,
                            form,
                            units,
                            vendors,
                            hsn,
                            warehouses,
                            company_country,
                        )
                        return render(request, 'add_item.html', context)
            
            # Validate opening stock rate if opening stock is provided
            selected_warehouse_id = request.POST.get('warehouse')
            op_stock_value = request.POST.get('op_stock')
            track_inventory = request.POST.get('track_inventory')
            
            if track_inventory and op_stock_value and selected_warehouse_id:
                # We need to check op_rate before saving
                op_rate = request.POST.get('op_rate', '').strip()
                if not op_rate:
                    messages.error(
                        request,
                        f"❌ REQUIRED: Opening Stock Rate\n\n"
                        f"You entered opening quantity of {op_stock_value} units,\n"
                        f"but 'Opening Stock Rate per Unit' is empty.\n\n"
                        f"Opening stock must use explicitly provided historical cost.\n"
                        f"Please fill 'Opening Stock Rate per Unit' field and try again."
                    )
                    context = _build_add_item_context(
                        request,
                        form,
                        units,
                        vendors,
                        hsn,
                        warehouses,
                        company_country,
                    )
                    return render(request, 'add_item.html', context)
                
                try:
                    op_rate_decimal = Decimal(str(op_rate))
                    if op_rate_decimal < 0:
                        messages.error(
                            request,
                            f"❌ INVALID: Opening Stock Rate\n\n"
                            f"Opening Stock Rate must be a positive number.\n"
                            f"You entered: {op_rate}"
                        )
                        context = _build_add_item_context(
                            request,
                            form,
                            units,
                            vendors,
                            hsn,
                            warehouses,
                            company_country,
                        )
                        return render(request, 'add_item.html', context)
                except (ValueError, TypeError):
                    messages.error(
                        request,
                        f"❌ INVALID: Opening Stock Rate\n\n"
                        f"Opening Stock Rate must be a valid number.\n"
                        f"You entered: {op_rate}"
                    )
                    context = _build_add_item_context(
                        request,
                        form,
                        units,
                        vendors,
                        hsn,
                        warehouses,
                        company_country,
                    )
                    return render(request, 'add_item.html', context)
                
                try:
                    warehouse_instance = Warehouse.objects.get(pk=selected_warehouse_id)
                except Warehouse.DoesNotExist:
                    messages.error(request, "Please select a valid warehouse for stock entry.")
                    context = _build_add_item_context(
                        request,
                        form,
                        units,
                        vendors,
                        hsn,
                        warehouses,
                        company_country,
                    )
                    return render(request, 'add_item.html', context)
            
            # ===== END PRE-VALIDATION =====
            
            with transaction.atomic():
                
                # item = form.save()
                item = form.save(commit=False)
                # item.status = True
                item.created_by = request.user
                preferred_vendor_id = request.POST.get('preferred_vendor')
                if preferred_vendor_id:
                    item.preferred_vendor = Vendor.objects.using(db).filter(pk=preferred_vendor_id).first()
                else:
                    item.preferred_vendor = None
                _assign_item_tax_fields(item, post_data, company_country, company_tax_type)
                # ✅ Store request context for activity logging to find correct company_db
                item._current_request = request

                
                # intra_tax_id = request.POST.get('intra_tax')
                # inter_tax_id = request.POST.get('inter_tax_group')

                # if intra_tax_id:
                #     try:
                #         intra_tax_obj = TaxGroup.objects.get(pk=intra_tax_id)
                #         gst_rate = float(intra_tax_obj.rate)  # assuming a 'rate' field exists
                #     except TaxGroup.DoesNotExist:
                #         pass


                item.save()
                # Clear inventory fields if track_inventory is not set
                if not request.POST.get('track_inventory'):
                    item.inv_acc = None
                    item.inv_method = None
                    item.op_stock = None
                    item.op_rate = None
                item.op_stock = None
                item.save()
                main_barcode_obj = None
                
                
                if brcd_value:
                    
                    # Create or get the barcode for this item
                    main_barcode_obj, created = Barcode.objects.get_or_create(item=item, barcode=brcd_value)
                    # Assign it as main barcode to the item
                    item.main_barcode = main_barcode_obj
                    item.save()
                    
                # Save barcodes
               
                # Exclude the main barcode from deletion
                if main_barcode_obj:
                    Barcode.objects.filter(item=item).exclude(id=main_barcode_obj.id).delete()
                else:
                    Barcode.objects.filter(item=item).delete()

                for code in barcodes_list:
                    code_stripped = code.strip()
                    if code_stripped:
                            
                        Barcode.objects.create(item=item, barcode=code_stripped)


                # Save UOMs
               

                Uom.objects.filter(item=item).delete()

                for name_id, conv, brcd_val in zip(uom_names, conv_factors, barcode_values):
                    if name_id:
                        barcode_fk = None
                        if brcd_val and brcd_val.strip():
                            
                            barcode_fk = Barcode.objects.create(item=item, barcode=brcd_val.strip())
                        uom_name_instance = get_object_or_404(Uom_name, pk=name_id)
                        Uom.objects.create(
                            item=item,
                            name=uom_name_instance,
                            conversion_factor=conv or 1.0,
                            barcode=barcode_fk
                        )
                # --- Save opening stock in Stock table ---
                # Note: All pre-validation already confirmed that if we reach here:
                # - If op_stock_value exists, warehouse_instance and op_rate are valid
                if track_inventory and op_stock_value and selected_warehouse_id:
                    warehouse_instance = Warehouse.objects.get(pk=selected_warehouse_id)
                    
                    stock_record = Stock.objects.create(
                        item=item,
                        warehouse=warehouse_instance,
                        opening_stock=op_stock_value,
                        quantity=op_stock_value,
                        created_by=request.user
                    )
                    
                    # Get selected equity account from form
                    selected_equity_id = request.POST.get('opening_stock_equity_account')
                    equity_account = None
                    
                    # If user explicitly selected an equity account, post to that
                    if selected_equity_id:
                        try:
                            equity_account = ChartOfAccounts.objects.get(id=selected_equity_id, type='5', status=True)
                            # AUTO-POST opening stock to journal ONLY if account was selected
                            success, je, msg = post_opening_stock_journal_entry(stock_record, request.user, request, equity_account=equity_account)
                            if success:
                                try:
                                    stock_value = Decimal(str(stock_record.opening_stock)) * Decimal(str(stock_record.item.op_rate or 0))
                                    equity_name = equity_account.name if equity_account else "Opening Balance Equity"
                                    messages.success(request, f"✓ Item created with opening stock ₹{float(stock_value):.2f} posted to {equity_name}")
                                except:
                                    messages.success(request, f"✓ Item created and opening stock posted to ledger")
                                logger.info(f"✓ Opening stock posted: {msg}")
                            else:
                                messages.error(request, f"❌ Opening stock posting failed: {msg}")
                                logger.warning(f"Opening stock posting failed: {msg}")
                        except ChartOfAccounts.DoesNotExist:
                            messages.error(request, "Selected equity account not found.")
                            pass
                    else:
                        # NO equity account selected - auto-post to Stock Adjustment account
                        success, je, msg = post_stock_adjustment_journal_entry(stock_record, request.user, request)
                        if success:
                            try:
                                stock_value = Decimal(str(stock_record.opening_stock)) * Decimal(str(stock_record.item.op_rate or 0))
                                messages.success(request, f"✓ Item created with opening stock ₹{float(stock_value):.2f} posted automatically to Stock Adjustment")
                            except:
                                messages.success(request, f"✓ Item created and opening stock posted to Stock Adjustment")
                            logger.info(f"✓ Opening stock adjustment posted: {msg}")
                        else:
                            messages.error(request, f"❌ Opening stock posting failed: {msg}")
                            logger.warning(f"Opening stock posting failed: {msg}")
                

                # --- End opening stock save ---
            messages.success(request, "Item created successfully!")
            return redirect_with_company('items')

        else:
            messages.error(request, "Please fix the errors below.")
        # if not form.is_valid():
        #     print("Form errors:", form.errors)  # Print all errors
        #     messages.error(request, "Please fix the errors below.")
        #     context = {
        #         'units': units,
        #         'vendors': vendors,
        #         'hsn': hsn,
        #         'form': form,
        #         'warehouses': warehouses,
        #     }
        #     return render(request, 'add_item.html', context)
            # print(form.errors)
    # elif request.method == "GET":
    #     print("geT request received")
    #     form = ItemForm()
    else:
        form = ItemForm()

    category_id = ''
    category_text = ''
    item_type_id = ''
    item_type_text = ''
    if request.method == "POST":
        category_id = request.POST.get('category') or ''
        item_type_id = request.POST.get('item_type') or ''
        if category_id:
            category_obj = Category.objects.filter(id=category_id, status=True).first()
            if category_obj:
                category_text = category_obj.category_name
        if item_type_id:
            item_type_obj = Type.objects.filter(id=item_type_id, status=True).first()
            if item_type_obj:
                item_type_text = item_type_obj.type_name

    # context = {
    #     'units': units,
    #     'vendors': vendors,
    #     'hsn': hsn,
    #     'form': form,
    #     'warehouses': warehouses,
    #     'category_id': category_id,
    #     'category_text': category_text,
    #     'item_type_id': item_type_id,
    #     'item_type_text': item_type_text,
    #     'can_create': True,
    #     'can_edit': True,
    # }
    context = _build_add_item_context(
        request,
        form,
        units,
        vendors,
        hsn,
        warehouses,
        company_country,
        category_id=category_id,
        category_text=category_text,
        item_type_id=item_type_id,
        item_type_text=item_type_text,
    )
    return render(request, 'add_item.html', context)

# @login_required
# def add_item(request):
#     vendors = Vendor.objects.all()
#     hsn = HSNCode.objects.all()
#     units = ["kg", "litre", "pcs"]

#     if request.method == "POST":
#         form = ItemForm(request.POST)
#         if form.is_valid():
#             # Pre-check all barcodes in POST for duplicates in DB before saving
            
#             def barcode_check(values):
#                 for val in values:
#                     val_stripped = val.strip()
#                     if val_stripped and Barcode.objects.filter(barcode=val_stripped).exists():
#                         return val_stripped
#                 return None

#             brcd_value = request.POST.get('brcd', '').strip()
#             barcodes_list = request.POST.getlist('barcodes[]')
#             barcode_values = request.POST.getlist('barcode[]')

#             duplicate_barcode = barcode_check([brcd_value] + barcodes_list + barcode_values)

#             if duplicate_barcode:
#                 messages.error(request, f"The barcode '{duplicate_barcode}' already exists. Please enter a unique barcode.")
#                 return render(request, 'add_item.html', {
#                     'units': units,
#                     'vendors': vendors,
#                     'hsn': hsn,
#                     'form': form,
#                 })

#             # No duplicates found, now save all inside transaction
#             with transaction.atomic():
#                 item = form.save(commit=False)
#                 item.created_by = request.user

#                 if not request.POST.get('track_inventory'):
#                     item.inv_acc = None
#                     item.inv_method = None
#                     item.op_stock = None
#                     item.op_rate = None

#                 item.save()

#                 main_barcode_obj = None
#                 if brcd_value:
#                     main_barcode_obj, _ = Barcode.objects.get_or_create(item=item, barcode=brcd_value)
#                     item.main_barcode = main_barcode_obj
#                     item.save()

#                 if main_barcode_obj:
#                     Barcode.objects.filter(item=item).exclude(id=main_barcode_obj.id).delete()
#                 else:
#                     Barcode.objects.filter(item=item).delete()

#                 for code in barcodes_list:
#                     code_stripped = code.strip()
#                     if code_stripped:
#                         Barcode.objects.create(item=item, barcode=code_stripped)

#                 uom_names = request.POST.getlist('uom[]')
#                 conv_factors = request.POST.getlist('conversion_factor[]')

#                 Uom.objects.filter(item=item).delete()

#                 for name, conv, brcd_val in zip(uom_names, conv_factors, barcode_values):
#                     barcode_fk = None
#                     if name:
#                         if brcd_val and brcd_val.strip():
#                             barcode_fk, _ = Barcode.objects.get_or_create(item=item, barcode=brcd_val.strip())
#                         Uom.objects.create(item=item, name=name, conversion_factor=conv or 1.0, barcode=barcode_fk)

#             messages.success(request, "Item created successfully!")
#             return redirect('items')

#         else:
#             messages.error(request, "Please fix the errors below.")
            
#     else:
#         form = ItemForm()

#     return render(request, 'add_item.html', {
#         'units': units,
#         'vendors': vendors,
#         'hsn': hsn,
#         'form': form,
#     })


@login_required
def item_edit(request, pk):
    print("called item_edit")
    item = get_object_or_404(Item, pk=pk)
    company_country = _get_company_country_name(request)
    company_tax_type = _get_company_tax_type(request)
    stock_entries = Stock.objects.filter(item=item).select_related('warehouse')
    stock_entries = list(stock_entries)
    warehouses = [s.warehouse for s in stock_entries]
    company_db = getattr(request, 'company_db', 'default')
    default_warehouse = Warehouse.objects.using(company_db).filter(
        status=True,
        is_default=True,
    ).first()
    # Determine selected warehouse id differently for GET and POST
    # if request.method == 'POST':
    #     if selected_warehouse_id:
    #         selected_warehouse_id = int(selected_warehouse_id)
    #     else:
    #         selected_warehouse_id = None
    # else:
    #     # For GET, get the first warehouse id from stock_entries or None
    #     selected_warehouse_id = stock_entries[0].warehouse.id if stock_entries.exists() else None
    if request.method == "POST":
        selected_warehouse_id = request.POST.get('warehouse', '')
    else:
        default_stock = next(
            (
                stock for stock in stock_entries
                if default_warehouse and stock.warehouse_id == default_warehouse.pk
            ),
            None,
        )
        if default_stock:
            selected_warehouse_id = default_stock.warehouse_id
        elif stock_entries:
            selected_warehouse_id = stock_entries[0].warehouse_id
        elif default_warehouse:
            selected_warehouse_id = default_warehouse.pk
        else:
            selected_warehouse_id = ''

    print("selected_warehouse_id:",selected_warehouse_id)
    warehouse_stock_data = []
    for s in stock_entries:
        warehouse_stock_data.append({
            'id': s.warehouse.id,
            'name': s.warehouse.warehouse_name,
            'opening_stock': s.opening_stock,  # use your field name
            'selected_warehouse_id': selected_warehouse_id,
            'selected': str(s.warehouse_id) == str(selected_warehouse_id),
        })
    print("warehousedata1:",warehouse_stock_data)
    # all_warehouses = Warehouse.objects.all()
    # stock_entries = {s.warehouse_id: s for s in Stock.objects.filter(item=item)}

    # warehouse_stock_data = []
    # for w in all_warehouses:
    #     stock_entry = stock_entries.get(w.id)
    #     warehouse_stock_data.append({
    #         'id': w.id,
    #         'name': w.warehouse_name,
    #         'opening_stock': float(stock_entry.opening_stock) if stock_entry else '',
    #     })

    # context['warehouses'] = warehouse_stock_data
    if request.method == "POST":
        post_data = request.POST.copy()
        if (company_tax_type or '').strip().upper() in ('NONE',):
            post_data['tax_pref'] = 'non_taxable'

        form = ItemForm(post_data, instance=item)
        if form.is_valid():
            lookup_error = _validate_item_lookup_selection(post_data)
            if lookup_error:
                messages.error(request, lookup_error)
                return redirect_with_company(request, 'item_edit', pk=pk)

            tax_error = _validate_item_tax_selection(post_data, company_country, company_tax_type)
            if tax_error:
                messages.error(request, tax_error)
                return redirect_with_company(request, 'item_edit', pk=pk)
            # ✅ by adarsh lock CHECK PERIOD LOCK BEFORE EDITING
            from datetime import date
            from django.core.exceptions import PermissionDenied
            from system_settings.validators import PeriodLockEnforcer
            
            db = getattr(request, 'company_db', 'default')
            today = date.today()
            try:
                PeriodLockEnforcer.check_can_edit(today, request.user, db=db, transaction_type='item')
            except PermissionDenied as e:
                # For AJAX requests, return JSON error response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'error': f"❌ Cannot edit item: {str(e)}"}, status=403)
                # Render form page with error modal using add_item.html template
                # Build context similar to GET request - use same logic as below
                unit_id = item.unit
                unit_name = ''
                if unit_id:
                    unit_obj = Unit.objects.filter(id=unit_id).first()
                    if unit_obj:
                        unit_name = unit_obj.unit_name
                
                hsn_code_id = item.hsn_code  # This is the object or None
                hsn_code_text = ''
                if hsn_code_id:
                    hsn_code_text = f"{hsn_code_id.code} - {hsn_code_id.description}"
                
                sac_code_id = item.sac_code  # This is the object or None
                sac_code_text = ''
                if sac_code_id:
                    sac_code_text = f"{sac_code_id.code} - {sac_code_id.description}"
                
                brand_id = item.brand_id
                brand_text = ''
                if brand_id:
                    brand_obj = Brand.objects.filter(id=brand_id).first()
                    if brand_obj:
                        brand_text = f"{brand_obj.brand_name}"

                category_id = item.category_id
                category_text = ''
                if category_id:
                    category_obj = Category.objects.filter(id=category_id, status=True).first()
                    if category_obj:
                        category_text = f"{category_obj.category_name}"

                subcategory_id = item.subcategory_id
                subcategory_text = ''
                if item.subcategory:
                    subcategory_text = item.subcategory.subcategory_name
                item_type_id = item.item_type_id
                item_type_text = ''
                if item_type_id:
                    item_type_obj = Type.objects.filter(id=item_type_id, status=True).select_related('subcategory').first()
                    if item_type_obj:
                        item_type_text = f"{item_type_obj.type_name}"
                        if not subcategory_id and item_type_obj.subcategory:
                            subcategory_id = item_type_obj.subcategory_id
                            subcategory_text = f"{item_type_obj.subcategory.subcategory_name}"
                
                vendor_id = item.preferred_vendor_id
                vendor_text = ''
                if vendor_id:
                    vendor_obj = Vendor.objects.filter(id=vendor_id).first()
                    if vendor_obj:
                        vendor_text = str(vendor_obj)
                
                intra_tax_id = item.intra_tax_id
                intra_tax_text = ''
                if intra_tax_id:
                    intra_tax_obj = TaxGroup.objects.filter(id=intra_tax_id).first()
                    if intra_tax_obj:
                        intra_tax_text = f"{intra_tax_obj.group_name}"
                
                inter_tax_id = item.inter_tax_group_id
                inter_tax_text = ''
                if inter_tax_id:
                    inter_tax_obj = Tax.objects.filter(id=inter_tax_id).first()
                    if inter_tax_obj:
                        inter_tax_text = f"{inter_tax_obj.taxname}"
                
                main_barcode_id = item.main_barcode_id
                main_barcode_name = ''
                if main_barcode_id:
                    main_barcode_obj = Barcode.objects.filter(id=main_barcode_id).first()
                    if main_barcode_obj:
                        main_barcode_name = f"{main_barcode_obj.barcode}"
                
                context = {
                    'form': form,
                    'item': item,
                    'current_unit_id': unit_id,
                    'current_unit_name': unit_name,
                    'hsn_code_id': hsn_code_id,
                    'hsn_code_text': hsn_code_text,
                    'sac_code_id': sac_code_id,
                    'sac_code_text': sac_code_text,
                    'brand_id': brand_id,
                    'brand_text': brand_text,
                    'category_id': category_id,
                    'category_text': category_text,
                    'subcategory_id': subcategory_id,
                    'subcategory_text': subcategory_text,
                    'item_type_id': item_type_id,
                    'item_type_text': item_type_text,
                    'is_edit': True,
                    'warehouses': warehouse_stock_data,
                    'vendor_id': vendor_id,
                    'vendor_text': vendor_text,
                    'intra_tax_id': intra_tax_id,
                    'intra_tax_text': intra_tax_text,
                    'inter_tax_id': inter_tax_id,
                    'inter_tax_text': inter_tax_text,
                    'main_barcode_id': main_barcode_id,
                    'main_barcode_name': main_barcode_name,
                    'prefilled_uom_data': json.dumps([], cls=DjangoJSONEncoder),
                    'prefilled_barcode_data': json.dumps([]),
                    'can_create': (getattr(request.user, 'is_superuser', False) or can_create_items(request.user)),
                    'can_edit': (getattr(request.user, 'is_superuser', False) or can_edit_items(request.user)),
                    'error_message': str(e),
                    'show_error_modal': True,
                    'company_country': company_country,
                    'company_is_india': _is_indian_company_country(company_country),
                    'company_tax_type': company_tax_type,
                }
                context.update(_get_item_tax_context(item=item))
                return render(request, 'add_item.html', context)

            updated_item = form.save(commit=False)
            # Preserve created_by, set updated_by to current user
            # updated_item.created_by = item.created_by
            updated_item.updated_by = request.user
            preferred_vendor_id = request.POST.get('preferred_vendor')
            if preferred_vendor_id:
                updated_item.preferred_vendor = Vendor.objects.using(db).filter(pk=preferred_vendor_id).first()
            else:
                updated_item.preferred_vendor = None
            # ✅ Store request context for activity logging to find correct company_db
            updated_item._current_request = request
            _assign_item_tax_fields(updated_item, post_data, company_country, company_tax_type)
            updated_item.save()
            # Update the main barcode (from POST 'brcd') 
            brcd_value = request.POST.get('brcd', '').strip()
            if brcd_value:
                main_barcode_obj, created = Barcode.objects.get_or_create(item=updated_item, barcode=brcd_value)
                updated_item.main_barcode = main_barcode_obj
                updated_item.save()
            else:
                # If no barcode provided, clear main_barcode (optional)
                updated_item.main_barcode = None
                updated_item.save()

            # Update barcodes (alternative barcodes)
            barcodes_list = request.POST.getlist('barcodes[]')
            # Delete barcodes except main barcode
            Barcode.objects.filter(item=updated_item).exclude(id=updated_item.main_barcode.id if updated_item.main_barcode else None).delete()
            for code in barcodes_list:
                if code.strip():
                    Barcode.objects.get_or_create(item=updated_item, barcode=code.strip())

            # Update UOMs
            uom_names = request.POST.getlist('uom[]')
            conv_factors = request.POST.getlist('conversion_factor[]')
            barcode_values = request.POST.getlist('barcode[]')
            Uom.objects.filter(item=updated_item).delete()  # delete old UOMs

            for name_id, conv, brcd_val in zip(uom_names, conv_factors, barcode_values):
                if name_id:
                    barcode_fk = None
                    if brcd_val and brcd_val.strip():
                        barcode_fk, created = Barcode.objects.get_or_create(item=updated_item, barcode=brcd_val.strip())
                    uom_name_instance = get_object_or_404(Uom_name, pk=name_id)
                    Uom.objects.create(
                        item=updated_item,
                        name=uom_name_instance,
                        conversion_factor=conv,
                        barcode=barcode_fk
                    )
            # Stock handling (before return!)
            selected_warehouse_id = request.POST.get('warehouse')
            print("selected_warehouse_id",selected_warehouse_id)
            op_stock_key = f'op_stock_{selected_warehouse_id}'
            print("opkey:",op_stock_key)
            op_stock_value = request.POST.get(op_stock_key)
            print("op_stock_value:",op_stock_value)
            warehouse_instance = None
            if selected_warehouse_id:
                try:
                    warehouse_instance = Warehouse.objects.get(pk=selected_warehouse_id)
                except Warehouse.DoesNotExist:
                    warehouse_instance = None

            # Convert opening stock value to Decimal for saving to DB
            try:
                op_stock_value = Decimal(op_stock_value) if op_stock_value else Decimal('0.00')
            except:
                op_stock_value = Decimal('0.00')  # fallback default or handle with error message

            if updated_item.track_inventory and warehouse_instance:
                # 🔥 VALIDATION: Ifby adarsh opening stock provided, opening rate MUST be provided
                if op_stock_value > Decimal('0.00'):
                    if not updated_item.op_rate or Decimal(str(updated_item.op_rate)) <= 0:
                        messages.error(
                            request,
                            f"❌ REQUIRED: Opening Stock Rate\n\n"
                            f"You entered opening quantity of {op_stock_value} units,\n"
                            f"but 'Opening Stock Rate per Unit' is empty.\n\n"
                            f"Opening stock must use explicitly provided historical cost.\n"
                            f"Please fill 'Opening Stock Rate per Unit' field and try again."
                        )
                        return redirect_with_company('item_edit', pk=updated_item.id)
                #byadarhs
                # # Delete existing stock entries for the item to avoid duplicates
                # Stock.objects.filter(item=updated_item).delete()
                
                # # Delete existing journal entry for this item on edit
                # old_journal_ref = f"OPENING_STOCK_{updated_item.name.upper()}"
                # JournalEntry.objects.filter(reference=old_journal_ref).delete()

                # # Create new stock entry with updated warehouse and opening stock
                # stock_record = Stock.objects.create(
                #     item=updated_item,
                #     warehouse=warehouse_instance,
                #     opening_stock=op_stock_value,
                #     quantity=op_stock_value,
                #     created_by=request.user,
                # )
                
                # 🔄 Find existing stock record (do NOT delete - preserve transaction history)
                try:
                    stock_record = Stock.objects.get(item=updated_item, warehouse=warehouse_instance)
                    old_opening_stock = Decimal(str(stock_record.opening_stock)) if stock_record.opening_stock else Decimal('0.00')
                    old_quantity = Decimal(str(stock_record.quantity)) if stock_record.quantity else Decimal('0.00')
                    
                    # Calculate transaction-based changes (purchases, sales, deliveries, etc.)
                    transaction_adjustment = old_quantity - old_opening_stock
                    
                    # Update opening stock and recalculate quantity to preserve transactions
                    stock_record.opening_stock = op_stock_value
                    stock_record.quantity = op_stock_value + transaction_adjustment
                    stock_record.updated_by = request.user
                    stock_record.save()
                    
                    print(f"Updated opening stock from {old_opening_stock} to {op_stock_value}")
                    print(f"Transaction adjustment: {transaction_adjustment}")
                    print(f"New quantity: {stock_record.quantity}")
                    
                except Stock.DoesNotExist:
                    # Create new stock record if it doesn't exist
                    stock_record = Stock.objects.create(
                        item=updated_item,
                        warehouse=warehouse_instance,
                        opening_stock=op_stock_value,
                        quantity=op_stock_value,
                        created_by=request.user,
                    )
                    print(f"Created new stock record with opening stock: {op_stock_value}")

                
                # AUTO-POST opening stock to journal when editing
                if op_stock_value > Decimal('0.00'):
                    # Instead of deleting the original opening stock journal, create a reversal
                    # entry for the existing posted opening stock (if any), then post the updated
                    # opening stock journal. This preserves the audit trail.
                    
                    # We check for both types of opening stock references:
                    # 1. Standard (with equity account)
                    # 2. Adjustment (without equity account)
                    possible_refs = [
                        f"OPENING_STOCK_{updated_item.name.upper()}",
                        f"OPENING_STOCK_ADJUSTMENT_{updated_item.name.upper()}"
                    ]
                    
                    for old_journal_ref in possible_refs:
                        old_entry = JournalEntry.objects.filter(
                            reference=old_journal_ref
                        ).exclude(
                            narration__startswith='Reversal of'
                        ).prefetch_related('lines').order_by('-id').first()

                        if old_entry:
                            # If not already reversed, create a reversal journal
                            reversed_exists = JournalEntry.objects.filter(
                                reference=old_entry.reference, 
                                narration__startswith=f"Reversal of {old_entry.entry_number}"
                            ).exists()
                            
                            if not reversed_exists:
                                # Generate next JV- number
                                last_entry = JournalEntry.objects.order_by('-id').first()
                                if last_entry and last_entry.entry_number and last_entry.entry_number.startswith('JV-'):
                                    try:
                                        last_num = int(last_entry.entry_number.split('-')[1])
                                    except Exception:
                                        last_num = 0
                                else:
                                    last_num = 0
                                next_num = last_num + 1
                                rev_entry_number = f"JV-{str(next_num).zfill(5)}"

                                with transaction.atomic():
                                    rev_je = JournalEntry.objects.create(
                                        entry_number=rev_entry_number,
                                        date=datetime.now().date(),
                                        reference=old_entry.reference,
                                        narration=f"Reversal of {old_entry.entry_number}",
                                        total_debit=old_entry.total_credit,
                                        total_credit=old_entry.total_debit,
                                        status='posted',
                                        created_by=request.user,
                                        updated_by=request.user
                                    )

                                    # Create reversed lines (swap debit/credit)
                                    for line in old_entry.lines.all():
                                        JournalLine.objects.create(
                                            journal=rev_je,
                                            account=line.account,
                                            description=(f"Reversal: {line.description}" if line.description else f"Reversal of line {line.id}"),
                                            debit=line.credit,
                                            credit=line.debit,
                                            sequence=line.sequence,
                                            status=line.status
                                        )
                    # Get selected equity account from form
                    selected_equity_id = request.POST.get('opening_stock_equity_account')
                    equity_account = None
                    
                    # ONLY post journal if user explicitly selected an equity account
                    if selected_equity_id:
                        try:
                            equity_account = ChartOfAccounts.objects.get(id=selected_equity_id, type='5', status=True)
                            success, je, msg = post_opening_stock_journal_entry(stock_record, request.user, request, equity_account=equity_account)
                            if success:
                                logger.info(f"✓ Opening stock posted on edit: {msg}")
                                messages.success(request, f"✓ Opening stock updated: {msg}")
                            else:
                                logger.warning(f"Opening stock posting on edit failed: {msg}")
                                messages.error(request, f"❌ Opening stock posting failed: {msg}")
                        except ChartOfAccounts.DoesNotExist:
                            messages.error(request, "Selected equity account not found.")
                    else:
                        # No equity account selected - post stock adjustment journal instead
                        success, je, msg = post_stock_adjustment_journal_entry(stock_record, request.user, request)
                        if success:
                            logger.info(f"✓ Stock adjustment posted on edit: {msg}")
                            messages.success(request, f"✓ Stock adjustment updated: {msg}")
                        else:
                            # If it's a "No opening stock" message, it's fine, otherwise show error
                            if "No opening stock" not in msg:
                                logger.warning(f"Stock adjustment posting on edit failed: {msg}")
                                messages.warning(request, f"⚠️ Stock adjustment: {msg}")

            messages.success(request, "Item edited successfully!")
            return redirect_with_company('items') # by adarshRedirect to your item list or detail view
            

                # if op_stock_value and item.track_inventory:
                # if warehouse_instance and op_stock_value and item.track_inventory:
                

        

    else:
        form = ItemForm(instance=item)
    item_uoms = Uom.objects.filter(item=item)
    prefilled_uom_data = [
        {
            'uom': uom_obj.name_id if uom_obj.name_id else "",
            'uom_name': uom_obj.name.name if uom_obj.name else "",
            'conversion_factor': uom_obj.conversion_factor,
            'barcode': uom_obj.barcode.barcode if uom_obj.barcode else "",
        }
        for uom_obj in item_uoms
    ]
    print(prefilled_uom_data)
    # item_barcodes = Barcode.objects.filter(item=item)
    # prefilled_barcode_data = [b.barcode for b in item_barcodes]
    # Get IDs of barcodes linked to UOM for this item
    uom_barcode_ids = Uom.objects.filter(item=item).values_list('barcode_id', flat=True)

    # Get main barcode ID of the item
    main_barcode_id = item.main_barcode_id

    # Fetch barcodes excluding those linked to UOM or main barcode
    item_barcodes = Barcode.objects.filter(item=item).exclude(id__in=uom_barcode_ids).exclude(id=main_barcode_id)

    # Prepare list of barcode strings for prefill
    prefilled_barcode_data = [b.barcode for b in item_barcodes]

    hsn_code_id = item.hsn_code
    hsn_code_text = ''
    sac_code_id = item.sac_code
    sac_code_text = ''
    vendor_id = item.preferred_vendor_id
    vendor_text = ''
    intra_tax_id = item.intra_tax_id
    intra_tax_text = ''
    inter_tax_id = item.inter_tax_group_id
    inter_tax_text = ''
    brand_id = item.brand_id
    brand_text = ''
    category_id = item.category_id
    category_text = ''
    subcategory_id = item.subcategory_id
    subcategory_text = ''
    item_type_id = item.item_type_id
    item_type_text = ''
    main_barcode_id = item.main_barcode_id  # assuming item.unit stores the unit ID (bigint)
    main_barcode_name = ''
    unit_id = item.unit  # assuming item.unit stores the unit ID (bigint)
    unit_name = ''
    warehouse_context = [
        {'id': w.id, 'name': w.warehouse_name} for w in warehouses
    ]
    selected_warehouse = next(
        (w for w in warehouses if str(w.pk) == str(selected_warehouse_id)),
        None,
    )
    if (
        selected_warehouse is None
        and default_warehouse
        and str(default_warehouse.pk) == str(selected_warehouse_id)
    ):
        selected_warehouse = default_warehouse
    if unit_id:
        unit_obj = Unit.objects.filter(id=unit_id).first()
        if unit_obj:
            unit_name = unit_obj.unit_name
    if main_barcode_id:
        main_barcode_obj = Barcode.objects.filter(id=main_barcode_id).first()
        if main_barcode_obj:
            main_barcode_name = f"{main_barcode_obj.barcode}"
    if hsn_code_id:
        hsn_obj = HSNCode.objects.filter(id=hsn_code_id).first()
        if hsn_obj:
            hsn_code_text = f"{hsn_obj.code} - {hsn_obj.description}"

    if sac_code_id:
        sac_obj = SACCode.objects.filter(id=sac_code_id).first()
        if sac_obj:
            sac_code_text = f"{sac_obj.code} - {sac_obj.description}"
    if brand_id:
        brand_obj = Brand.objects.filter(id=brand_id).first()
        if brand_obj:
            brand_text = f"{brand_obj.brand_name} "
    if category_id:
        category_obj = Category.objects.filter(id=category_id, status=True).first()
        if category_obj:
            category_text = f"{category_obj.category_name} "
    if item.subcategory:
        subcategory_text = f"{item.subcategory.subcategory_name} "
    if item_type_id:
        item_type_obj = Type.objects.filter(id=item_type_id, status=True).select_related('subcategory').first()
        if item_type_obj:
            item_type_text = f"{item_type_obj.type_name} "
            if not subcategory_id and item_type_obj.subcategory:
                subcategory_id = item_type_obj.subcategory_id
                subcategory_text = f"{item_type_obj.subcategory.subcategory_name} "
    # if warehouse_id:
    #     warehouse_obj = Warehouse.objects.filter(id=warehouse_id).first()
    #     if warehouse_obj:
    #         warehouse_text = f"{warehouse_obj.warehouse_name} "
    if vendor_id:
        vendor_obj = Vendor.objects.filter(id=vendor_id).first()
        if vendor_obj:
            vendor_text = f"{vendor_obj} "

    if intra_tax_id:
        intra_tax_obj = TaxGroup.objects.filter(id=intra_tax_id).first()
        if intra_tax_obj:
            intra_tax_text = f"{intra_tax_obj.group_name} "
    if inter_tax_id:
        inter_tax_obj = Tax.objects.filter(id=inter_tax_id).first()
        if inter_tax_obj:
            inter_tax_text = f"{inter_tax_obj.taxname} "
    
    print("warehouse_stock_data:",warehouse_stock_data)
    context = {
        'form': form,
        'item': item,
        'current_unit_id': unit_id,
        'current_unit_name': unit_name,
        'current_warehouse_id': selected_warehouse_id,
        'current_warehouse_name': selected_warehouse.warehouse_name if selected_warehouse else '',
        'hsn_code_id': hsn_code_id,
        # 'warehouses': warehouse_context,
        'hsn_code_text': hsn_code_text,
        'sac_code_id': sac_code_id,
        'sac_code_text': sac_code_text,
        'brand_id': brand_id,
        'brand_text': brand_text,
        'category_id': category_id,
        'category_text': category_text,
        'subcategory_id': subcategory_id,
        'subcategory_text': subcategory_text,
        'item_type_id': item_type_id,
        'item_type_text': item_type_text,
        # 'warehouse_id': warehouse_id,
        # 'warehouse_text': warehouse_text,
        'is_edit': pk is not None,
        'warehouses': warehouse_stock_data,
        'vendor_id': vendor_id,
        'vendor_text': vendor_text,
        'intra_tax_id': intra_tax_id,
        'intra_tax_text': intra_tax_text,
        'inter_tax_id': inter_tax_id,
        'inter_tax_text': inter_tax_text,
        'main_barcode_id': main_barcode_id,
        'main_barcode_name': main_barcode_name,
        'prefilled_uom_data': json.dumps(prefilled_uom_data, cls=DjangoJSONEncoder),
        'prefilled_barcode_data': json.dumps(prefilled_barcode_data),
        'can_create': (getattr(request.user, 'is_superuser', False) or can_create_items(request.user)),
        'can_edit': (getattr(request.user, 'is_superuser', False) or can_edit_items(request.user)),
        'company_country': company_country,
        'company_is_india': _is_indian_company_country(company_country),
        'company_tax_type': company_tax_type,
        'base_currency': _get_company_base_currency(request),
    }
    context.update(_get_item_tax_context(item=item))
    return render(request, 'add_item.html', context)

# Delete view
@require_POST
@login_required
def delete_item(request, pk):
    # Permission: require delete on Items
    try:
        if not (getattr(request.user, 'is_superuser', False) or can_delete_items(request.user)):
            messages.error(request, 'You do not have permission to delete Items.')
            return redirect_with_company('items')
    except Exception:
        messages.error(request, 'You do not have permission to delete Items.')
        return redirect_with_company('items')

    # 🔒 by adarsh lockPERIOD LOCKING: Check if today is in a locked period
    from django.utils import timezone
    today = timezone.now().date()
    db = getattr(request, 'company_db', 'default')
    try:
        PeriodLockEnforcer.check_can_edit(today, request.user, db=db, transaction_type='item')
    except PermissionDenied as e:
        # For AJAX requests, return JSON error response
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': f"❌ Cannot delete item: {str(e)}"}, status=403)
        # Redirect to items list with error message (for non-AJAX delete)
        messages.error(request, f"❌ Cannot delete item: {str(e)}")
        return redirect_with_company('items')

    item = get_object_or_404(Item, pk=pk)
    # Before marking item inactive, remove related opening-stock journal entries to avoid orphaned journals
    try:
        with transaction.atomic():
            # Reference formats used when posting opening stock
            possible_refs = [
                f"OPENING_STOCK_{item.name.upper()}",
                f"OPENING_STOCK_ADJUSTMENT_{item.name.upper()}"
            ]
            
            for entry_reference in possible_refs:
                for journal in JournalEntry.objects.filter(reference=entry_reference):
                    JournalLine.objects.filter(journal=journal).delete()
                    journal.delete()

            # Mark item inactive
            item.status = False
            item.save(update_fields=["status"])
    except Exception as e:
        logger.error(f"Error deleting item journals for {item}: {e}", exc_info=True)
        messages.error(request, f"Error deleting related journal entries: {e}")
        return redirect_with_company('items')
    messages.success(request, "Item deleted successfully.")
    return redirect_with_company('items')



def hsn_code_search(request):
    query = request.GET.get('q', '')
    results = []
    # Return matching results when query provided, otherwise return first 50 for initial dropdown
    if query:
        hsn_codes = HSNCode.objects.filter(Q(description__icontains=query) | Q(code__icontains=query))[:50]
    else:
        hsn_codes = HSNCode.objects.all()[:5]

    results = [{"id": h.id, "code": h.code, "description": h.description} for h in hsn_codes]
    return JsonResponse(results, safe=False)


def get_item_hsn(request):
    """Return the HSN code text/value for a given item id (used by quotation JS)."""
    item_id = request.GET.get('item_id')
    if not item_id:
        return JsonResponse({'hsn_id': None, 'hsn_text': ''})

    try:
        item = Item.objects.get(pk=item_id)
    except Item.DoesNotExist:
        return JsonResponse({'hsn_id': None, 'hsn_text': ''})

    hsn_id = item.hsn_code
    hsn_code = ''
    hsn_text = ''
    if hsn_id:
        hsn_obj = HSNCode.objects.filter(id=hsn_id).first()
        if hsn_obj:
            hsn_code = hsn_obj.code
            # provide display text similar to add_item page: code - description
            hsn_text = f"{hsn_obj.code} - {hsn_obj.description}"

    return JsonResponse({'hsn_id': hsn_id, 'hsn_code': hsn_code, 'hsn_text': hsn_text})

def sac_code_search(request):
    query = request.GET.get('q', '')
    # Return matching results when a query is provided, otherwise return the
    # first 50 so the dropdown has options to show as soon as it's opened.
    if query:
        # Filter SAC codes where description contains the search term (case-insensitive)
        sac_codes = SACCode.objects.filter(Q(description__icontains=query) | Q(code__icontains=query))[:50]  # limit results to 50
    else:
        sac_codes = SACCode.objects.all()[:5]
    results = [{"id": s.id,"code":s.code,"description": s.description} for s in sac_codes]
    return JsonResponse(results, safe=False)

def unit_search(request):
    query = request.GET.get('q', '')
    results = []
    if query:
        uoms = Uom.objects.filter(name__icontains=query)[:50]  # Capital Uom here
        results = [{"id": h.id, "name": h.name} for h in uoms]
    return JsonResponse(results, safe=False)

def search_unit(request):
    query = request.GET.get('q', '')
    # Return matching results when a query is provided, otherwise return the
    # first 50 so the dropdown has options to show as soon as it's opened.
    if query:
        units = Unit.objects.filter(unit_name__icontains=query)[:50]  # Capital Uom here
    else:
        units = Unit.objects.all()[:5]
    results = [{"id": h.id, "name": h.unit_name} for h in units]
    return JsonResponse(results, safe=False)

def vendor_search(request):
    query = request.GET.get('q', '')
    results = []
    vendors = Vendor.objects.all()
    if query:
        vendors = vendors.filter(
            Q(vendor_code__icontains=query) |
            Q(company_name__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(email__icontains=query)
        )[:50]
    else:
        vendors = vendors[:5]  # limit results to 50
    
    for v in vendors:
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

def create_uom_ajax(request):
    form = UomForm(request.POST)
    context = {'uom_form': form}
    # add other context as needed
    return render(request, 'add_uom.html', context)

def create_unit_ajax(request):
    form = UnitForm(request.POST)
    context = {'unit_form': form}
    # add other context as needed
    return render(request, 'add_unit.html', context)

def create_vendor_ajax(request):
    # Provide the richer Purchase vendor partial (with contact-person formset) so modal matches full vendor page
    vendor_instance = Vendor()
    # Default currency to organisation base for the modal form
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
    if request.method == 'POST':
        form = VendorForm(request.POST, company=company)
        formset = ContactPersonFormSet(request.POST or None, instance=vendor_instance, prefix='contactperson')
        if form.is_valid() and formset.is_valid():
            try:
                vendor = form.save()
                # Ensure vendor's currency defaults to organisation base currency if not provided
                try:
                    cid = request.session.get('company_id')
                    company = None
                    if cid:
                        company = Company.objects.filter(pk=cid).first()
                    if not company:
                        company = Company.objects.order_by('id').first()
                    if company and (not getattr(vendor, 'currency', '') or str(vendor.currency).strip() == ''):
                        vendor.currency = (company.base_currency or '').strip().upper()[:10]
                        vendor.save()
                except Exception:
                    pass
            except Exception as e:
                err = str(e)
                if 'vendor_code' in err or 'purchase_vendor.vendor_code' in err:
                    form.add_error('vendor_code', 'Vendor code already exists')
                else:
                    form.add_error(None, f'Error: {err}')
                return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset}, status=400)

            # Save contact persons
            contact_persons = formset.save(commit=False)
            for cp in contact_persons:
                cp.vendor = vendor
                cp.save()
            try:
                formset.save_m2m()
            except Exception:
                pass

            # If vendor was marked as customer, create corresponding Customer record and contact persons
            if getattr(vendor, 'is_customer', False):
                try:
                    from customer.models import Customer
                    from customer.models import ContactPerson as CustomerContactPerson

                    # avoid duplicate customer by email or code
                    customer_exists = False
                    if vendor.email:
                        customer_exists = Customer.objects.filter(email=vendor.email).exists()

                    code_conflict = False
                    if vendor.vendor_code:
                        code_conflict = Customer.objects.filter(customer_code=vendor.vendor_code).exists()

                    if not code_conflict and not customer_exists:
                        cust = Customer.objects.create(
                            customer_code=vendor.vendor_code,
                            customer_type=getattr(vendor, 'vendor_type', ''),
                            first_name=getattr(vendor, 'first_name', ''),
                            last_name=getattr(vendor, 'last_name', ''),
                            company_name=getattr(vendor, 'company_name', ''),
                            email=getattr(vendor, 'email', ''),
                            phone=getattr(vendor, 'phone', ''),
                            mobile=getattr(vendor, 'mobile', ''),
                            address_line_1=getattr(vendor, 'address_line_1', ''),
                            address_line_2=getattr(vendor, 'address_line_2', ''),
                            city=getattr(vendor, 'city', ''),
                            state=getattr(vendor, 'state', ''),
                            postal_code=getattr(vendor, 'postal_code', ''),
                            country=getattr(vendor, 'country', ''),
                            gst_number=getattr(vendor, 'tax_number', ''),
                            opening_balance=getattr(vendor, 'opening_balance', 0),
                            is_active=getattr(vendor, 'is_active', True),
                            is_vendor=True,
                            created_by=getattr(request, 'user', None),
                            updated_by=getattr(request, 'user', None),
                        )
                        # create contact persons for customer
                        try:
                            for cp in vendor.contact_persons.all():
                                CustomerContactPerson.objects.create(
                                    customer=cust,
                                    name=cp.name,
                                    email=cp.email,
                                    phone=cp.phone,
                                )
                        except Exception:
                            pass
                except Exception:
                    # any issue syncing customer should not block vendor creation
                    pass

            return JsonResponse({'success': True, 'id': vendor.id, 'name': str(vendor)})
        else:
            return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset}, status=400)
    else:
        form = VendorForm(company=company)
        formset = ContactPersonFormSet(instance=vendor_instance, prefix='contactperson')
        return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset})

def add_vendor(request):
    # Support contact-persons from modal via formset
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

        vendor_instance = Vendor()
        form = VendorForm(request.POST, company=company)
        formset = ContactPersonFormSet(request.POST or None, instance=vendor_instance, prefix='contactperson')
        if form.is_valid() and formset.is_valid():
            try:
                vendor = form.save()
            except Exception as e:
                err = str(e)
                if 'vendor_code' in err or 'purchase_vendor.vendor_code' in err:
                    return JsonResponse({'success': False, 'errors': {'vendor_code': ['Vendor code already exists']}}, status=400)
                else:
                    return JsonResponse({'success': False, 'errors': {'__all__': [f'Database error: {err}']}}, status=400)

            contact_persons = formset.save(commit=False)
            for cp in contact_persons:
                cp.vendor = vendor
                cp.save()
            try:
                formset.save_m2m()
            except Exception:
                pass

            # If vendor marked as customer, sync to Customer model (like Purchase.vendor_create)
            if getattr(vendor, 'is_customer', False):
                try:
                    from customer.models import Customer
                    from customer.models import ContactPerson as CustomerContactPerson

                    customer_exists = False
                    if vendor.email:
                        customer_exists = Customer.objects.filter(email=vendor.email).exists()

                    code_conflict = False
                    if vendor.vendor_code:
                        code_conflict = Customer.objects.filter(customer_code=vendor.vendor_code).exists()

                    if not code_conflict and not customer_exists:
                        cust = Customer.objects.create(
                            customer_code=vendor.vendor_code,
                            customer_type=getattr(vendor, 'vendor_type', ''),
                            first_name=getattr(vendor, 'first_name', ''),
                            last_name=getattr(vendor, 'last_name', ''),
                            company_name=getattr(vendor, 'company_name', ''),
                            email=getattr(vendor, 'email', ''),
                            phone=getattr(vendor, 'phone', ''),
                            mobile=getattr(vendor, 'mobile', ''),
                            address_line_1=getattr(vendor, 'address_line_1', ''),
                            address_line_2=getattr(vendor, 'address_line_2', ''),
                            city=getattr(vendor, 'city', ''),
                            state=getattr(vendor, 'state', ''),
                            postal_code=getattr(vendor, 'postal_code', ''),
                            country=getattr(vendor, 'country', ''),
                            gst_number=getattr(vendor, 'tax_number', ''),
                            opening_balance=getattr(vendor, 'opening_balance', 0),
                            is_active=getattr(vendor, 'is_active', True),
                            is_vendor=True,
                            created_by=getattr(request, 'user', None),
                            updated_by=getattr(request, 'user', None),
                        )
                        try:
                            for cp in vendor.contact_persons.all():
                                CustomerContactPerson.objects.create(
                                    customer=cust,
                                    name=cp.name,
                                    email=cp.email,
                                    phone=cp.phone,
                                )
                        except Exception:
                            pass
                except Exception:
                    pass

            return JsonResponse({'success': True, 'id': vendor.id, 'name': str(vendor)})
        else:
            # For AJAX/modal submissions return the rendered partial so per-field errors
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return render(request, 'Purchase/partial_vendor_form.html', {'form': form, 'formset': formset}, status=400)
            errors = form.errors.copy()
            errors.update({'contactperson': formset.errors})
            return JsonResponse({'success': False, 'errors': errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

def add_brand(request):
    if request.method == "POST":
        form = BrandForm(request.POST)
        if form.is_valid():
            brand = form.save()  # saving new vendor
            return JsonResponse({'success': True, 'id': brand.id, 'name': brand.brand_name})
        else:
            return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

@login_required
def create_subcategory_ajax(request):
    if request.method == 'GET':
        form = SubcategoryForm(initial={'category': request.GET.get('category_id') or None})
        return render(request, 'Items/subcategory_create_form.html', {'form': form})

    if request.method == 'POST':
        form = SubcategoryForm(request.POST)
        if form.is_valid():
            subcategory = form.save(commit=False)
            subcategory.created_by = request.user
            subcategory.save()
            return JsonResponse({
                'success': True,
                'id': subcategory.id,
                'name': subcategory.subcategory_name,
                'category_id': subcategory.category_id,
            })
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)

    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

def add_warehouse(request):
    if request.method == "POST":
        form = WarehouseFormModal(request.POST)
        if form.is_valid():
            warehouse = form.save()  # saving new vendor
            return JsonResponse({'success': True, 'id': warehouse.id, 'name': warehouse.warehouse_name})
        else:
            return JsonResponse({'success': False, 'errors': form.errors})
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

def add_uom(request):
    if request.method == "POST":
        form = UomForm(request.POST)
        if form.is_valid():
            uom_instance = form.save()
    
            # If AJAX request, return JSON with new uom data
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'id': uom_instance.id,
                    'name': uom_instance.name,
                })

            # Otherwise standard web form submission
            messages.success(request, "New uom created successfully!")
            return redirect_with_company('uom')  # Adjust redirect target as needed

        else:
            # If AJAX, return errors as JSON
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'errors': form.errors,
                }, status=400)

    else:
        form = UomForm()

    # If GET or standard request, render a page or partial with form
    return render(request, 'add_uom.html', {'uom_form': form})

# def add_unit(request):
#     if request.method == "POST":
#         form = UnitForm(request.POST)
#         print("Form class location:", UnitForm.__module__)

#         print("POST DATA:", request.POST)
#         print("Form fields:", form.fields.keys())
#         if form.is_valid():
#             unit_instance = form.save(commit=False)
#             unit_instance.created_by = request.user
#             unit_instance.updated_by = request.user
#             unit_instance.save()
            
#             # If AJAX request, return JSON with new uom data
#             if request.headers.get('x-requested-with') == 'XMLHttpRequest':
#                 return JsonResponse({
#                     'success': True,
#                     'id': unit_instance.id,
#                     'name': unit_instance.unit_name,
#                 })

#             # Otherwise standard web form submission
#             messages.success(request, "New unit created successfully!")
#             return redirect_with_company('unit')  # Adjust redirect target as needed

#         else:
#             # If AJAX, return errors as JSON
#             if request.headers.get('x-requested-with') == 'XMLHttpRequest':
#                 return JsonResponse({
#                     'success': False,
#                     'errors': form.errors,
#                 }, status=400)

#     else:
#         form = UnitForm()

#     # If GET or standard request, render a page or partial with form
#     return render(request, 'add_unit.html', {'unit_form': form})


def add_unit(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    print("=== add_unit called ===")
    print("method:", request.method)
    print("is_ajax:", is_ajax)
    print("X-Requested-With header:", request.headers.get('X-Requested-With'))
    print("POST data:", dict(request.POST))
    
    if request.method == "POST":
        form = UnitForm(request.POST)
        print("form.is_valid():", form.is_valid())
        print("form.errors:", form.errors)
        
        if form.is_valid():
            unit_instance = form.save(commit=False)
            unit_instance.created_by = request.user
            unit_instance.updated_by = request.user
            unit_instance.save()
            print("Unit saved, id:", unit_instance.id, "name:", unit_instance.unit_name)
            if is_ajax:
                print("Returning JSON success")
                return JsonResponse({
                    'success': True,
                    'id': unit_instance.id,
                    'name': unit_instance.unit_name,
                })
            messages.success(request, "New unit created successfully!")
            return redirect_with_company('unit')
        else:
            print("Form invalid, errors:", form.errors)
            if is_ajax:
                return render(request, 'unit_form_partial.html', {'unit_form': form})
            return render(request, 'add_unit.html', {'unit_form': form})

    form = UnitForm()
    if is_ajax:
        return render(request, 'unit_form_partial.html', {'unit_form': form})
    return render(request, 'add_unit.html', {'unit_form': form})

def brand_search(request):
    query = request.GET.get('q', '')
    # Return matching results when a query is provided, otherwise return the
    # first 50 so the dropdown has options to show as soon as it's opened.
    if query:
        brand = Brand.objects.filter(brand_name__icontains=query, status=True)[:50]  # limit results to 50
    else:
        brand = Brand.objects.filter(status=True)[:5]
    results = [{"id": h.id,"name":h.brand_name} for h in brand]
    return JsonResponse(results, safe=False)


def category_search(request):
    query = request.GET.get('q', '')
    categories = Category.objects.filter(status=True)
    if query:
        categories = categories.filter(category_name__icontains=query)
        limit = 50
    else:
        limit = 5
    results = [{"id": c.id, "name": c.category_name} for c in categories.order_by('category_name')[:limit]]
    return JsonResponse(results, safe=False)


def subcategory_search(request):
    query = request.GET.get('q', '')
    category_id = request.GET.get('category_id') or ''
    if not category_id:
        return JsonResponse([], safe=False)
    subcategories = Subcategory.objects.filter(status=True, category__status=True).select_related('category')
    subcategories = subcategories.filter(category_id=category_id)
    if query:
        subcategories = subcategories.filter(subcategory_name__icontains=query)
        limit = 50
    else:
        limit = 5
    results = [
        {
            "id": subcat.id,
            "name": subcat.subcategory_name,
            "category_id": subcat.category_id,
            "category_name": subcat.category.category_name if subcat.category else "",
        }
        for subcat in subcategories.order_by('subcategory_name')[:limit]
    ]
    return JsonResponse(results, safe=False)


def item_type_search(request):
    query = request.GET.get('q', '')
    subcategory_id = request.GET.get('subcategory_id') or ''
    if not subcategory_id:
        return JsonResponse([], safe=False)
    types = Type.objects.filter(status=True).select_related('subcategory')
    types = types.filter(subcategory_id=subcategory_id)
    if query:
        types = types.filter(type_name__icontains=query)
        limit = 50
    else:
        limit = 5
    results = [
        {
            "id": t.id,
            "name": t.type_name,
            "subcategory_id": t.subcategory_id,
        }
        for t in types.order_by('type_name')[:limit]
    ]
    return JsonResponse(results, safe=False)


def create_brand_ajax(request):
    form = BrandForm(request.POST)
    context = {'brand_form': form}
    # add other context as needed
    return render(request, 'add_brands.html', context)


def create_category_ajax(request):
    form = CategoryForm(request.POST)
    context = {'category_form': form}
    return render(request, 'add_categories.html', context)


def create_item_type_ajax(request):
    initial = {}
    subcategory_id = request.GET.get('subcategory_id') or request.POST.get('subcategory') or ''
    if subcategory_id:
        initial['subcategory'] = subcategory_id
    form = TypeForm(request.POST or None, initial=initial)
    context = {'item_type_form': form}
    return render(request, 'add_item_types.html', context)


def create_warehouse_ajax(request):
    form = WarehouseFormModal()  #  Create empty form for GET request updt by neha on 11-2-26
    context = {'warehouse_form': form}
    return render(request, 'add_warehouses.html', context)

def warehouse_search(request):
    query = request.GET.get('q', '')
    # Return matching results when a query is provided, otherwise return the
    # first 50 so the dropdown has options to show as soon as it's opened.
    if query:
        warehouse = Warehouse.objects.filter(warehouse_name__icontains=query, status=True)[:50]  # limit results to 50
    else:
        warehouse = Warehouse.objects.filter(status=True)[:5]
    results = [{"id": h.id,"name":h.warehouse_name} for h in warehouse]
    return JsonResponse(results, safe=False)


def add_category(request):
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save(commit=False)
            category.created_by = request.user
            category.updated_by = request.user
            category.save()
            return JsonResponse({'success': True, 'id': category.id, 'name': category.category_name})
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)


def add_item_type(request):
    if request.method == "POST":
        form = TypeForm(request.POST)
        if form.is_valid():
            item_type = form.save(commit=False)
            item_type.created_by = request.user
            item_type.updated_by = request.user
            item_type.save()
            return JsonResponse({
                'success': True,
                'id': item_type.id,
                'name': item_type.type_name,
                'subcategory_id': item_type.subcategory_id,
            })
        return JsonResponse({'success': False, 'errors': form.errors}, status=400)
    return JsonResponse({'success': False, 'errors': {'__all__': ['Invalid method']}}, status=405)

# def tax_group_search(request):
#     query = request.GET.get('q', '')
#     tax_groups = TaxGroup.objects.all()

#     if query:
#         tax_groups = tax_groups.filter(group_name__icontains=query)

#     results = [
#         {"id": t.id, "text": t.group_name} for t in tax_groups[:50]
#     ]
#     return JsonResponse({"results": results})

def tax_group_search(request):
    query = request.GET.get('q', '')
    include_individual = request.GET.get('include_individual', 'false').lower() == 'true'
    results = []
    
    # Get TaxGroups (only active groups and active taxes)
    tax_groups = TaxGroup.objects.filter(status=True).prefetch_related('taxes')
    if query:
        tax_groups = tax_groups.filter(group_name__icontains=query)
        limit = 50
    else:
        limit = 5

    for t in tax_groups[:limit]:
        # consider only active taxes inside the group for display
        active_taxes = list(t.taxes.filter(is_active=True))
        first_tax = next((tx for tx in active_taxes if tx.country), None)
        if first_tax and first_tax.country:
            results.append({"id": t.id, "text": f"{t.group_name} ({first_tax.country})"})
        else:
            results.append({"id": t.id, "text": t.group_name})
    
    # Also get individual Tax objects only if include_individual=true (for non-India companies)
    if include_individual:
        individual_taxes = Tax.objects.filter(is_active=True)
        if query:
            individual_taxes = individual_taxes.filter(
                Q(taxname__icontains=query) | Q(name__icontains=query)
            )
        
        for t in individual_taxes[:limit]:
            text = f"{t.taxname} ({t.country})" if t.country else t.taxname
            # Check if this tax is already in a result from TaxGroups
            if not any(r['text'] == text for r in results):
                results.append({"id": t.id, "text": text})
    
    return JsonResponse({"results": results})

def inter_state_tax_list(request):
    query = request.GET.get('q', '')
    tax_scope = (request.GET.get('scope') or '').strip().lower()
    company_tax_type = (_get_company_tax_type(request) or '').strip().upper()
    if company_tax_type == 'TURNOVER' and tax_scope in ('', 'default'):
        tax_scope = 'local'
    # Only include active taxes
    taxes = Tax.objects.filter(is_active=True)
    if tax_scope == 'inter':
        taxes = taxes.filter(taxtype__name__iexact='IGST')
    elif tax_scope == 'sales':
        taxes = taxes.filter(Q(tax_scope__iexact='SALES') | Q(tax_scope__iexact='BOTH'))
    elif tax_scope == 'purchase':
        taxes = taxes.filter(Q(tax_scope__iexact='PURCHASE') | Q(tax_scope__iexact='BOTH'))
    elif tax_scope == 'local':
        taxes = taxes.filter(tax_type__iexact='LOCAL')
    elif tax_scope == 'turnover':
        taxes = taxes.filter(tax_type__iexact='TURNOVER')
    if query:
        taxes = taxes.filter(Q(taxname__icontains=query) | Q(name__icontains=query))
        limit = 50
    else:
        limit = 5
    results = [
        {
            "id": t.id,
            "text": f"{t.taxname} ({t.country})" if t.country else t.taxname,
            "rate": float(t.rate or 0),
        }
        for t in taxes[:limit]
    ]
    return JsonResponse({"results": results})

# def check_barcode_unique(request):
#     barcode = request.GET.get('barcode', '').strip()
#     original_barcode = request.GET.get('original_barcode', '').strip()
#     uom_barcode = request.GET.get('uom_barcode', '').strip()
#     uom_original_barcode = request.GET.get('uom_oribarcode', '').strip()

#     # exists = Barcode.objects.filter((barcode=barcode).exclude(barcode=original_barcode) or (barcode=uom_barcode).exclude(barcode=original_barcode)).exists()
#     query = Q()
#     if barcode:
#         query |= Q(barcode=barcode) & ~Q(barcode=original_barcode)
#     if uom_barcode:
#         query |= Q(barcode=uom_barcode) & ~Q(barcode=uom_original_barcode)

#     exists = Barcode.objects.filter(query).exists() if query else False
#     return JsonResponse({'exists': exists})

# def check_barcode_unique(request):
#     barcode = request.GET.get('barcode', '').strip()
#     original_barcode = request.GET.get('original_barcode', '').strip()
#     exists = Barcode.objects.filter(barcode=barcode)..exclude(barcode=original_barcode).exists()
#     return JsonResponse({'exists': exists})

def check_barcode_unique(request):
    barcode = request.GET.get('barcode', '').strip()
    original_barcode = request.GET.get('original_barcode', '').strip()
    original_sub_barcode = request.GET.get('original_sub_barcode', '').strip()
    original_alt_barcode = request.GET.get('original_alt_barcode', '').strip()
    # Filter barcodes matching the input, exclude the original barcode, and only linked to active items
    exists = Barcode.objects.filter(
        barcode=barcode  # Only considering items with status=1
    ).exclude(barcode=original_barcode).exclude(barcode=original_sub_barcode).exclude(barcode=original_alt_barcode).exists()
    return JsonResponse({'exists': exists})


def uom_search(request):
    query = request.GET.get('q', '')
    # Return matching results when a query is provided, otherwise return the
    # first 50 so the dropdown has options to show as soon as it's opened.
    if query:
        uom_name = Uom_name.objects.filter(name__icontains=query)[:50]  # limit results to 50
    else:
        uom_name = Uom_name.objects.all()[:5]
    results = [{"id": h.id,"name":h.name} for h in uom_name]
    return JsonResponse(results, safe=False)

# def get_uoms(request):
#     units = Unit.objects.all().values('id', 'unit_name')  # update fields as per model
#     return JsonResponse(list(units), safe=False)


@login_required
@require_http_methods(["POST"])
def create_item_ajax(request):
    """
    AJAX endpoint to create a new item from the bill form
    Simplified version - only essential fields
    """
    try:
        # Log all POST data for debugging
        print("=" * 80)
        print("AJAX Item Creation Request (Simplified)")
        print("=" * 80)
        print("POST Data:")
        for key, value in request.POST.items():
            print(f"  {key}: {value}")
        print("=" * 80)
        
        company_country = _get_company_country_name(request)
        company_tax_type = _get_company_tax_type(request)
        effective_post = request.POST.copy()
        if (company_tax_type or '').strip().upper() in ('NONE',):
            effective_post['tax_pref'] = 'non_taxable'

        # Create a minimal form data dict with only required fields.
        # This bypasses the full ItemForm validation.
        item_data = {
            'type': request.POST.get('type', 'goods'),
            'name': request.POST.get('name', '').strip(),
            'unit': request.POST.get('unit'),
            'tax_pref': effective_post.get('tax_pref', 'taxable'),
            'cost_price': request.POST.get('cost_price', 0),
            'selling_price': request.POST.get('selling_price', 0),
            'purchase_desc': request.POST.get('purchase_desc', ''),
            'taxincld_costprice': request.POST.get('taxincld_costprice') == 'on',
            'tax': request.POST.get('tax'),
            'intra_tax': request.POST.get('intra_tax'),
            'inter_tax_group': request.POST.get('inter_tax_group'),
            'sales_tax': request.POST.get('sales_tax'),
            'purchase_tax': request.POST.get('purchase_tax'),
        }
        
        # Basic validation
        if not item_data['name']:
            return JsonResponse({
                'success': False,
                'error': 'Item name is required.'
            }, status=400)
        
        if not item_data['unit']:
            return JsonResponse({
                'success': False,
                'error': 'Unit is required.'
            }, status=400)
        
        try:
            cost_price = float(item_data['cost_price']) if item_data['cost_price'] else 0
            if cost_price <= 0:
                return JsonResponse({
                    'success': False,
                    'error': 'Cost price must be greater than zero.'
                }, status=400)
        except ValueError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid cost price value.'
            }, status=400)
            
        tax_error = _validate_item_tax_selection(effective_post, company_country, company_tax_type)
        if tax_error:
            return JsonResponse({
                'success': False,
                'error': tax_error
            }, status=400)
        
        print("Validation passed! Proceeding with item creation...")
        
        
        # ✅ by adarshlockCHECK PERIOD LOCK BEFORE CREATING ITEM
        from datetime import date
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        today = date.today()
        try:
            PeriodLockEnforcer.check_can_edit(today, request.user, db=db, transaction_type='item')
        except PermissionDenied as e:
            return JsonResponse({
                'success': False,
                'error': f"❌ Cannot create item: {str(e)}"
            }, status=403)
        
        with transaction.atomic():
            # Create item manually (bypassing form)
            
            # Get unit object
            try:
                unit_obj = Unit.objects.get(pk=item_data['unit'])
            except Unit.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid unit selected.'
                }, status=400)
            
            # Create the item
            item = Item(
                type=item_data['type'],
                name=item_data['name'],
                unit=item_data['unit'],
                tax_pref=item_data['tax_pref'],
                cost_price=item_data['cost_price'] or 0,
                selling_price=item_data['selling_price'] or 0,
                purchase_desc=item_data['purchase_desc'] or '',
                taxincld_costprice=item_data['taxincld_costprice'],
                intra_tax=None,
                inter_tax_group=None,
                sales_tax=None,
                purchase_tax=None,
                created_by=request.user,
                updated_by=request.user,
                purchase_info=1,
                # Set sales info same as purchase info for simplicity
                sales_desc=item_data['purchase_desc'] or '',
                # Inventory tracking disabled by default for quick items
                track_inventory=False,
                inv_acc=None,
                inv_method=None,
                op_stock=None,
                op_rate=None,
            )
            _assign_item_tax_fields(item, effective_post, company_country, company_tax_type)
            item.save()
            
            print(f"Item created: ID={item.id}, Name={item.name}")
            
            # Handle barcode if provided
            main_barcode_obj = None
            brcd_value = request.POST.get('barcode', '').strip()
            
            if brcd_value:
                print(f"Processing barcode: {brcd_value}")
                # Check if barcode already exists
                if Barcode.objects.filter(barcode=brcd_value).exists():
                    print(f"ERROR: Barcode '{brcd_value}' already exists!")
                    # Delete the item we just created since barcode is duplicate
                    item.delete()
                    return JsonResponse({
                        'success': False,
                        'error': f"The barcode '{brcd_value}' already exists. Please enter a unique barcode."
                    }, status=400)
                else:
                    # Create the barcode for this item
                    main_barcode_obj = Barcode.objects.create(
                        item=item, 
                        barcode=brcd_value
                    )
                    # Assign it as main barcode to the item
                    item.main_barcode = main_barcode_obj
                    item.save()
                    print(f"Barcode created: {brcd_value}")
        
        print("=" * 80)
        print("ITEM CREATED SUCCESSFULLY!")
        print("=" * 80)
        
        # ===================================================================
        # Prepare response data safely
        # ===================================================================
        tax_id, tax_name, tax_rate = _get_tax_response_payload(item, prefer='purchase')
        
        # Get unit name safely
        unit_name = ''
        if hasattr(item.unit, 'name'):
            unit_name = item.unit.name
        elif hasattr(item.unit, 'unit_name'):
            unit_name = item.unit.unit_name
        else:
            unit_name = str(item.unit) if item.unit else ''
        
        # Return success response with item details
        return JsonResponse({
            'success': True,
            'item': {
                'id': item.id,
                'name': item.name,
                'type': item.type,
                'unit': unit_name,
                'hsn_code': '',
                'sac_code': '',
                'cost_price': str(item.cost_price) if item.cost_price else '0.00',
                'selling_price': str(item.selling_price) if item.selling_price else '0.00',
                'barcode': brcd_value,
                'tax_pref': item.tax_pref,
                'track_inventory': item.track_inventory,
                'tax_id': tax_id,
                'tax_name': tax_name,
                'tax_rate': tax_rate,
                'taxincld_costprice': item.taxincld_costprice,  # ✅ Make sure this is included
            },
            'message': 'Item created successfully!'
        })
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print("=" * 80)
        print("EXCEPTION OCCURRED!")
        print("=" * 80)
        print(error_trace)
        print("=" * 80)
        
        return JsonResponse({
            'success': False,
            'error': f'An error occurred: {str(e)}',
            'traceback': error_trace
        }, status=500)



@login_required
@require_http_methods(["POST"])
def create_item_ajax_sales(request):
    """
    AJAX endpoint to create a new item from the invoice form
    Simplified version - only essential fields
    """
    try:
        # Log all POST data for debugging
        print("=" * 80)
        print("AJAX Item Creation Request (Simplified)")
        print("=" * 80)
        print("POST Data:")
        for key, value in request.POST.items():
            print(f"  {key}: {value}")
        print("=" * 80)
        
        company_country = _get_company_country_name(request)
        company_tax_type = _get_company_tax_type(request)
        effective_post = request.POST.copy()
        if (company_tax_type or '').strip().upper() in ('NONE',):
            effective_post['tax_pref'] = 'non_taxable'

        # Create a minimal form data dict with only required fields
        # This bypasses the full ItemForm validation
        item_data = {
            'type': request.POST.get('type', 'goods'),
            'name': request.POST.get('name', '').strip(),
            'unit': request.POST.get('unit'),
            'tax_pref': effective_post.get('tax_pref', 'taxable'),
            'cost_price': request.POST.get('cost_price', 0),
            'selling_price': request.POST.get('selling_price', 0),
            'purchase_desc': request.POST.get('purchase_desc', ''),
            'taxincld_sellingprice': request.POST.get('taxincld_sellingprice') == 'on',
            'intra_tax': request.POST.get('intra_tax'),
            'inter_tax_group': request.POST.get('inter_tax_group'),
            'tax': request.POST.get('tax'),  # For non-Indian companies
            'sales_tax': request.POST.get('sales_tax'),
            'purchase_tax': request.POST.get('purchase_tax'),
        }
        
        # Basic validation
        if not item_data['name']:
            return JsonResponse({
                'success': False,
                'error': 'Item name is required.'
            }, status=400)
        
        if not item_data['unit']:
            return JsonResponse({
                'success': False,
                'error': 'Unit is required.'
            }, status=400)
        
        try:
            selling_price = float(item_data['selling_price']) if item_data['selling_price'] else 0
            if selling_price <= 0:
                return JsonResponse({
                    'success': False,
                    'error': 'Selling price must be greater than zero.'
                }, status=400)
        except ValueError:
            return JsonResponse({
                'success': False,
                'error': 'Invalid Selling price value.'
            }, status=400)
        
        print("Validation passed! Proceeding with item creation...")

        tax_error = _validate_item_tax_selection(effective_post, company_country, company_tax_type)
        if tax_error:
            return JsonResponse({'success': False, 'error': tax_error}, status=400)
        default_tax_obj = None
        
        # ✅ by adarsh lockCHECK PERIOD LOCK BEFORE CREATING ITEM
        from datetime import date
        from django.core.exceptions import PermissionDenied
        from system_settings.validators import PeriodLockEnforcer
        
        db = getattr(request, 'company_db', 'default')
        today = date.today()
        try:
            PeriodLockEnforcer.check_can_edit(today, request.user, db=db, transaction_type='item')
        except PermissionDenied as e:
            return JsonResponse({
                'success': False,
                'error': f"❌ Cannot create item: {str(e)}"
            }, status=403)
        
        with transaction.atomic():
            # Create item manually (bypassing form)
            
            # Get unit object
            try:
                unit_obj = Unit.objects.get(pk=item_data['unit'])
            except Unit.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid unit selected.'
                }, status=400)
            
            # Get tax objects based on company country
            intra_tax_obj = None
            # if item_data['intra_tax']:
            #     try:
            #         # from Items.models import TaxGroup
            #         intra_tax_obj = TaxGroup.objects.get(pk=item_data['intra_tax'])
            #     except TaxGroup.DoesNotExist:
            #         pass
            
            
                
            
            inter_tax_obj = None
            sales_tax_obj = None
            purchase_tax_obj = None
            # if item_data['inter_tax_group']:
            #     try:
            #         # from Items.models import InterStateTaxGroup
            #         inter_tax_obj = Tax.objects.get(pk=item_data['inter_tax_group'])
            #     except Tax.DoesNotExist:
            #         pass
            
            if (company_tax_type or '').strip().upper() == 'SALES':
                if item_data.get('sales_tax'):
                    try:
                        sales_tax_obj = Tax.objects.get(pk=item_data['sales_tax'])
                    except Tax.DoesNotExist:
                        sales_tax_obj = None
                if item_data.get('purchase_tax'):
                    try:
                        purchase_tax_obj = Tax.objects.get(pk=item_data['purchase_tax'])
                    except Tax.DoesNotExist:
                        purchase_tax_obj = None
                intra_tax_obj = None
                inter_tax_obj = None
            elif _is_indian_company_country(company_country):
                # For Indian companies: use intra_tax and inter_tax_group from form
                if item_data['intra_tax']:
                    try:
                        intra_tax_obj = TaxGroup.objects.get(pk=item_data['intra_tax'])
                    except TaxGroup.DoesNotExist:
                        pass
                
                if item_data['inter_tax_group']:
                    try:
                        inter_tax_obj = Tax.objects.get(pk=item_data['inter_tax_group'])
                    except Tax.DoesNotExist:
                        pass
            else:
                # For non-Indian companies: use 'tax' field and wrap in TaxGroup
                if item_data['tax']:
                    try:
                        tax_obj = Tax.objects.get(pk=item_data['tax'])
                        intra_tax_obj = _get_or_create_tax_group_for_tax(tax_obj)
                    except Tax.DoesNotExist:
                        pass
                
            
            # Create the item
            item = Item.objects.create(
                type=item_data['type'],
                name=item_data['name'],
                unit=item_data['unit'],
                tax_pref=effective_post.get('tax_pref', item_data['tax_pref']),
                cost_price=item_data['cost_price'] or 0,
                selling_price=item_data['selling_price'] or 0,
                purchase_desc=item_data['purchase_desc'] or '',
                taxincld_slprice=item_data['taxincld_sellingprice'],
                intra_tax=intra_tax_obj,
                inter_tax_group=inter_tax_obj,
                sales_tax=sales_tax_obj,
                purchase_tax=purchase_tax_obj,
                created_by=request.user,
                updated_by=request.user,
                sales_info=1,
                # Set sales info same as purchase info for simplicity
                # sales_desc=item_data['purchase_desc'] or '',
                # Inventory tracking disabled by default for quick items
                track_inventory=False,
                inv_acc=None,
                inv_method=None,
                op_stock=None,
                op_rate=None,
            )
            _assign_item_tax_fields(item, effective_post, company_country, company_tax_type)
            item.save(update_fields=['intra_tax', 'inter_tax_group', 'sales_tax', 'purchase_tax'])
            
            print(f"Item created: ID={item.id}, Name={item.name}")
            
            # Handle barcode if provided
            main_barcode_obj = None
            brcd_value = request.POST.get('barcode', '').strip()
            
            if brcd_value:
                print(f"Processing barcode: {brcd_value}")
                # Check if barcode already exists
                if Barcode.objects.filter(barcode=brcd_value).exists():
                    print(f"ERROR: Barcode '{brcd_value}' already exists!")
                    # Delete the item we just created since barcode is duplicate
                    item.delete()
                    return JsonResponse({
                        'success': False,
                        'error': f"The barcode '{brcd_value}' already exists. Please enter a unique barcode."
                    }, status=400)
                else:
                    # Create the barcode for this item
                    main_barcode_obj = Barcode.objects.create(
                        item=item, 
                        barcode=brcd_value
                    )
                    # Assign it as main barcode to the item
                    item.main_barcode = main_barcode_obj
                    item.save()
                    print(f"Barcode created: {brcd_value}")
        
        print("=" * 80)
        print("ITEM CREATED SUCCESSFULLY!")
        print("=" * 80)
        
        # ===================================================================
        # Prepare response data safely
        # ===================================================================
        tax_id, tax_name, tax_rate = _get_tax_response_payload(item, prefer='sales')
        
        
        # Get unit name safely
        unit_name = ''
        if hasattr(item.unit, 'name'):
            unit_name = item.unit.name
        elif hasattr(item.unit, 'unit_name'):
            unit_name = item.unit.unit_name
        else:
            unit_name = str(item.unit) if item.unit else ''
        
        # Return success response with item details
        return JsonResponse({
            'success': True,
            'item': {
                'id': item.id,
                'name': item.name,
                'type': item.type,
                'unit': unit_name,
                'hsn_code': '',
                'sac_code': '',
                # 'cost_price': str(item.cost_price) if item.cost_price else '0.00',
                'selling_price': str(item.selling_price) if item.selling_price else '0.00',
                'barcode': brcd_value,
                'tax_pref': item.tax_pref,
                'track_inventory': item.track_inventory,
                'tax_id': tax_id,
                'tax_name': tax_name,
                'tax_rate': tax_rate,
                'taxincld_sellingprice': item.taxincld_slprice,  # ✅ Make sure this is included
            },
            'message': 'Item created successfully!'
        })
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print("=" * 80)
        print("EXCEPTION OCCURRED!")
        print("=" * 80)
        print(error_trace)
        print("=" * 80)
        
        return JsonResponse({
            'success': False,
            'error': f'An error occurred: {str(e)}',
            'traceback': error_trace
        }, status=500)


def unit_createform(request):
    form = UnitForm()
    return render(request, 'unit_form_partial.html', {'unit_form': form})
