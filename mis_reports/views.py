import csv
import logging
import traceback
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.http import HttpResponse
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from urllib.parse import urlencode
from Lyraerp.utils.redirect_utils import redirect_with_company

from Lyraerp.utils.thread_locals import get_current_db
from .permissions import (
    can_export,
    can_view_finance,
    can_view_inventory,
    can_view_purchase,
    can_view_sales,
    can_view_crm,
    can_view_hr,
    can_view_expense,
)
from .services import build_export_query_string, financial_year_bounds, parse_date_range_from_request, safe_date_filter


logger = logging.getLogger(__name__)

@login_required
def account_ledgers(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_finance(request.user)):
        messages.error(request, 'You do not have permission to view MIS account ledgers.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    from chart_of_accounts.models import ChartOfAccounts
    from journal.models import JournalLine

    start_date, end_date, period = parse_date_range_from_request(request)
    company_db = getattr(request, 'company_db', 'default')
    account_value = request.GET.get('account') or ''
    account_filter = int(account_value) if account_value.isdigit() else None

    lines = JournalLine.objects.using(company_db).filter(
        journal__status='posted', status=True,
        journal__date__gte=start_date, journal__date__lte=end_date,
    )
    if account_filter:
        lines = lines.filter(account_id=account_filter)

    ledger_entries = []
    running_balance = Decimal('0.00')
    for line in lines.select_related('journal', 'account').order_by('journal__date', 'id'):
        debit = line.debit or Decimal('0.00')
        credit = line.credit or Decimal('0.00')
        if account_filter:
            running_balance += debit - credit
        query = urlencode({'date_from': start_date.isoformat(), 'date_to': end_date.isoformat()})
        detail_url = reverse('mis_account_detail', kwargs={
            'company_code': company_code, 'pk': line.account_id,
        })
        ledger_entries.append({
            'date': line.journal.date,
            'account_code': line.account.code,
            'account_name': line.account.name,
            'reference': line.journal.reference or line.journal.entry_number,
            'journal_pk': line.journal_id,
            'debit': debit,
            'credit': credit,
            'running_balance': running_balance,
            'detail_url': f'{detail_url}?{query}',
        })

    account_options = ChartOfAccounts.objects.using(company_db).filter(
        active=True, status=True,
        journal_lines__journal__status='posted',
        journal_lines__status=True,
        journal_lines__journal__date__gte=start_date,
        journal_lines__journal__date__lte=end_date,
    ).distinct().order_by('code', 'name')

    return render(request, 'mis_reports/account_ledgers.html', {
        'company_code': company_code,
        'ledger_entries': ledger_entries,
        'account_options': account_options,
        'account_filter': account_filter,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
    })


@login_required
def account_ledgers_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    from chart_of_accounts.models import ChartOfAccounts
    from journal.models import JournalLine

    start_date, end_date, period = parse_date_range_from_request(request)
    company_db = getattr(request, 'company_db', 'default')
    account_value = request.GET.get('account') or ''
    account_filter = int(account_value) if account_value.isdigit() else None
    selected_account = (
        ChartOfAccounts.objects.using(company_db).filter(pk=account_filter).first()
        if account_filter else None
    )
    lines = JournalLine.objects.using(company_db).filter(
        journal__status='posted', status=True,
        journal__date__gte=start_date, journal__date__lte=end_date,
    )
    if account_filter:
        lines = lines.filter(account_id=account_filter)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="ledger_report.csv"'
    writer = csv.writer(response)
    writer.writerow(['Ledger Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d')])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d')])
    writer.writerow([
        'Account Filter',
        f'{selected_account.code} - {selected_account.name}' if selected_account else 'All accounts',
    ])
    writer.writerow([])
    writer.writerow(['Date', 'Account', 'Reference', 'Debit', 'Credit'])
    for line in lines.select_related('journal', 'account').order_by('journal__date', 'id'):
        writer.writerow([
            line.journal.date.strftime('%d-%m-%Y'),
            f'{line.account.code} - {line.account.name}',
            line.journal.reference or line.journal.entry_number,
            f'{line.debit or Decimal("0.00"):.2f}',
            f'{line.credit or Decimal("0.00"):.2f}',
        ])
    return response

@login_required
def account_detail(request, pk, company_code=None):
    """Render an account ledger inside MIS using the shared ledger implementation."""
    if not (getattr(request.user, 'is_superuser', False) or can_view_finance(request.user)):
        messages.error(request, 'You do not have permission to view MIS account details.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    from chart_of_accounts.views import account_detail as chart_account_detail

    return chart_account_detail(
        request,
        pk,
        template_name='mis_reports/account_detail.html',
        permission_override=True,
    )


def _to_decimal(value):
    try:
        if value in (None, ''):
            return Decimal('0.00')
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0.00')


def _format_amount(value):
    return f"₹{_to_decimal(value):,.2f}"


def _format_count(value):
    return f"{int(_to_decimal(value)):,}"


def _customer_display_name(customer):
    if not customer:
        return 'Unknown'
    if getattr(customer, 'customer_type', '') == 'company':
        return getattr(customer, 'company_name', '') or str(customer)
    return f"{getattr(customer, 'first_name', '') or ''} {getattr(customer, 'last_name', '') or ''}".strip() or str(customer)


def _customer_display_name_from_row(row):
    if row.get('customer__company_name'):
        return row['customer__company_name']
    first_name = row.get('customer__first_name') or ''
    last_name = row.get('customer__last_name') or ''
    display_name = f"{first_name} {last_name}".strip()
    if display_name:
        return display_name
    return row.get('customer__customer_code') or 'Unknown'


def _vendor_display_name_from_row(row):
    if row.get('vendor__company_name'):
        return row['vendor__company_name']
    first_name = row.get('vendor__first_name') or ''
    last_name = row.get('vendor__last_name') or ''
    display_name = f"{first_name} {last_name}".strip()
    if display_name:
        return display_name
    return row.get('vendor__vendor_code') or 'Unknown'


def _build_summary(start_date=None, end_date=None):
    summary = {
        'total_sales': Decimal('0.00'),
        'total_purchases': Decimal('0.00'),
        'gross_profit': Decimal('0.00'),
        'outstanding_receivables': Decimal('0.00'),
        'outstanding_payables': Decimal('0.00'),
        'stock_value': Decimal('0.00'),
        'low_stock_count': 0,
        'lead_opportunity_count': 0,
        'employee_count': 0,
        'monthly_expenses': Decimal('0.00'),
    }

    try:
        from sales.models import SalesInvoice

        sales_queryset = safe_date_filter(SalesInvoice.objects.all(), 'date', start_date, end_date)
        summary['total_sales'] = sales_queryset.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')
    except Exception:
        summary['total_sales'] = Decimal('0.00')

    try:
        from Purchase.models import Bill

        purchase_queryset = safe_date_filter(Bill.objects.all(), 'date', start_date, end_date)
        summary['total_purchases'] = purchase_queryset.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')
    except Exception:
        summary['total_purchases'] = Decimal('0.00')

    summary['gross_profit'] = summary['total_sales'] - summary['total_purchases']

    try:
        from sales.models import SalesInvoice, InvPaymentAllocation

        invoices = safe_date_filter(SalesInvoice.objects.all(), 'date', start_date, end_date)
        invoice_total = invoices.aggregate(total=Coalesce(Sum('total_amount'), Decimal('0.00')))['total'] or Decimal('0.00')
        allocations = InvPaymentAllocation.objects.filter(inv__in=invoices)
        if start_date and end_date:
            allocations = allocations.filter(payment__payment_date__gte=start_date, payment__payment_date__lte=end_date)
        received_total = allocations.aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total'] or Decimal('0.00')
        summary['outstanding_receivables'] = max(invoice_total - received_total, Decimal('0.00'))
    except Exception:
        summary['outstanding_receivables'] = Decimal('0.00')

    try:
        from Purchase.models import Bill, BillPaymentAllocation

        bills = safe_date_filter(Bill.objects.all(), 'date', start_date, end_date)
        bill_total = bills.aggregate(total=Coalesce(Sum('total_amount'), Decimal('0.00')))['total'] or Decimal('0.00')
        allocations = BillPaymentAllocation.objects.filter(bill__in=bills)
        if start_date and end_date:
            allocations = allocations.filter(payment__payment_date__gte=start_date, payment__payment_date__lte=end_date)
        paid_total = allocations.aggregate(total=Coalesce(Sum('amount'), Decimal('0.00')))['total'] or Decimal('0.00')
        summary['outstanding_payables'] = max(bill_total - paid_total, Decimal('0.00'))
    except Exception:
        summary['outstanding_payables'] = Decimal('0.00')

    try:
        from stock.models import Stock

        stock_items = Stock.objects.select_related('item').all()
        stock_value = Decimal('0.00')
        low_stock_count = 0
        for stock in stock_items:
            quantity = _to_decimal(getattr(stock, 'quantity', 0))
            rate = _to_decimal(getattr(getattr(stock, 'item', None), 'cost_price', 0))
            stock_value += quantity * rate
            min_stock = getattr(getattr(stock, 'item', None), 'min_stock', None)
            if min_stock is not None and quantity <= _to_decimal(min_stock):
                low_stock_count += 1
        summary['stock_value'] = stock_value
        summary['low_stock_count'] = low_stock_count
    except Exception:
        summary['stock_value'] = Decimal('0.00')
        summary['low_stock_count'] = 0

    try:
        from crm.models import Lead, Opportunity

        lead_count = safe_date_filter(Lead.objects.all(), 'created_at', start_date, end_date).count()
        opportunity_count = safe_date_filter(Opportunity.objects.all(), 'created_at', start_date, end_date).count()
        summary['lead_opportunity_count'] = lead_count + opportunity_count
    except Exception:
        summary['lead_opportunity_count'] = 0

    try:
        from Employee.models import Employee

        summary['employee_count'] = Employee.objects.count()
    except Exception:
        summary['employee_count'] = 0

    try:
        from expenses.models import Expense

        expense_queryset = safe_date_filter(Expense.objects.all(), 'date', start_date, end_date)
        expense_total = Decimal('0.00')
        for expense in expense_queryset:
            expense_total += _to_decimal(getattr(expense, 'total_amount', 0))
        summary['monthly_expenses'] = expense_total
    except Exception:
        summary['monthly_expenses'] = Decimal('0.00')

    return summary


def dashboard(request, company_code=None):
    start_date, end_date, period = parse_date_range_from_request(request)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    summary = _build_summary(start_date, end_date)
    summary_cards = [
        {'title': 'Total Sales', 'value': _format_amount(summary['total_sales']), 'description': 'Sales invoices in the selected period.'},
        {'title': 'Total Purchases', 'value': _format_amount(summary['total_purchases']), 'description': 'Purchase bills in the selected period.'},
        {'title': 'Gross Profit', 'value': _format_amount(summary['gross_profit']), 'description': 'Estimated sales minus purchases.'},
        {'title': 'Outstanding Receivables', 'value': _format_amount(summary['outstanding_receivables']), 'description': 'Open customer receivables for matching invoices.'},
        {'title': 'Outstanding Payables', 'value': _format_amount(summary['outstanding_payables']), 'description': 'Open vendor payables for matching bills.'},
        {'title': 'Stock Value', 'value': _format_amount(summary['stock_value']), 'description': 'Current inventory value based on stock records.'},
        {'title': 'Low Stock Items', 'value': _format_count(summary['low_stock_count']), 'description': 'Items at or below minimum stock levels.'},
        {'title': 'Leads & Opportunities', 'value': _format_count(summary['lead_opportunity_count']), 'description': 'New leads and opportunities in the period.'},
        {'title': 'Employees', 'value': _format_count(summary['employee_count']), 'description': 'Registered employees in the system.'},
        {'title': 'Monthly Expenses', 'value': _format_amount(summary['monthly_expenses']), 'description': 'Expenses recorded in the selected period.'},
    ]

    return render(request, 'mis_reports/dashboard.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'export_query_string': export_query_string,
        'summary': summary,
        'summary_cards': summary_cards,
    })


def _build_sales_report(start_date=None, end_date=None, db_alias=None, filters=None):
    logger.info('Building sales MIS report: start_date=%s, end_date=%s', start_date, end_date)
    db_alias = db_alias or get_current_db() or 'default'
    filters = filters or {}
    report = {
        'total_sales': Decimal('0.00'),
        'invoice_count': 0,
        'quotation_count': 0,
        'sales_invoices': [],
        'top_customers': [],
        'top_items': [],
        'monthly_trend': [],
        'outstanding_sales': Decimal('0.00'),
    }

    try:
        from sales.models import InvPaymentAllocation, SalesInvoice, SalesInvoiceItem, SalesQuotation

        invoices = safe_date_filter(
            SalesInvoice.objects.using(db_alias), 'date', start_date, end_date
        )
        if filters.get('customer'):
            invoices = invoices.filter(customer_id=filters['customer'])
        if filters.get('sales_person'):
            invoices = invoices.filter(sales_person_id=filters['sales_person'])
        if filters.get('payment_status'):
            invoices = invoices.filter(payment_status_id=filters['payment_status'])
        item_filters = {
            'product_id': filters.get('item'),
            'product__category_id': filters.get('category'),
            'product__subcategory_id': filters.get('subcategory'),
            'product__item_type_id': filters.get('item_type'),
            'product__brand_id': filters.get('brand'),
        }
        item_filters = {key: value for key, value in item_filters.items() if value}
        if item_filters:
            matching_invoice_ids = SalesInvoiceItem.objects.using(db_alias).filter(
                **item_filters
            ).values('sales_inv_id')
            invoices = invoices.filter(pk__in=matching_invoice_ids)
        report['invoice_count'] = invoices.count()
        report['total_sales'] = invoices.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')

        for invoice in invoices.select_related('customer', 'payment_status', 'sales_person').order_by('-date', '-pk'):
            report['sales_invoices'].append({
                'invoice_number': invoice.inv_number,
                'customer_name': _customer_display_name(invoice.customer),
                'sales_person': invoice.sales_person.name if invoice.sales_person else 'Unassigned',
                'date': invoice.date,
                'amount': invoice.total_amount or Decimal('0.00'),
                'payment_status': str(invoice.payment_status) if invoice.payment_status else 'Not Paid',
            })

        report['quotation_count'] = safe_date_filter(
            SalesQuotation.objects.using(db_alias), 'date', start_date, end_date
        ).count()

        customer_rows = invoices.filter(customer__isnull=False).values(
            'customer',
            'customer__company_name',
            'customer__first_name',
            'customer__last_name',
            'customer__customer_code',
        ).annotate(
            total_sales=Coalesce(Sum('total_amount'), Decimal('0.00')),
            invoice_count=Count('pk'),
        ).order_by('-total_sales')[:10]

        for row in customer_rows:
            report['top_customers'].append({
                'customer_name': _customer_display_name_from_row(row),
                'total_sales': row['total_sales'] or Decimal('0.00'),
                'invoice_count': row['invoice_count'],
            })

        item_rows = SalesInvoiceItem.objects.using(db_alias).filter(
            sales_inv__in=invoices
        ).values(
            'product__name',
        ).annotate(
            qty=Sum('quantity'),
        ).order_by('-qty')[:6]

        for row in item_rows:
            item_name = row.get('product__name') or 'Unknown'
            report['top_items'].append({
                'item_name': item_name,
                'quantity_sold': row.get('qty') or Decimal('0.00'),
            })
        trend_rows = invoices.annotate(month=TruncMonth('date')).values('month').annotate(
            total_sales=Coalesce(Sum('total_amount'), Decimal('0.00'))
        ).order_by('month')

        for row in trend_rows:
            month = row.get('month')
            report['monthly_trend'].append({
                'month': month.strftime('%Y-%m') if month else 'Unknown',
                'total_sales': row['total_sales'] or Decimal('0.00'),
            })

        paid_amount = InvPaymentAllocation.objects.using(db_alias).filter(
            inv__in=invoices
        ).aggregate(
            total=Coalesce(Sum('amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')
        report['outstanding_sales'] = max(report['total_sales'] - paid_amount, Decimal('0.00'))
    except Exception:
        print('Failed to build sales MIS report:', flush=True)
        traceback.print_exc()
        logger.exception('Failed to build sales MIS report')
        pass

    return report


def _sales_filter_options(db_alias=None):
    from Items.models import Item
    from Purchase.models import PaymentStatus
    from brand.models import Brand
    from category.models import Category, Subcategory
    from customer.models import Customer
    from sales.models import SalesPerson
    from type.models import Type

    db_alias = db_alias or get_current_db() or 'default'
    return {
        'items': Item.objects.using(db_alias).filter(status=True).order_by('name'),
        'categories': Category.objects.using(db_alias).filter(status=True).order_by('category_name'),
        'subcategories': Subcategory.objects.using(db_alias).filter(status=True).order_by('subcategory_name'),
        'item_types': Type.objects.using(db_alias).filter(status=True).order_by('type_name'),
        'brands': Brand.objects.using(db_alias).filter(status=True).order_by('brand_name'),
        'customers': Customer.objects.using(db_alias).order_by('company_name', 'first_name', 'last_name'),
        'sales_persons': SalesPerson.objects.using(db_alias).order_by('name'),
        'payment_statuses': PaymentStatus.objects.using(db_alias).order_by('name'),
    }


def _sales_filter_values(request):
    values = {}
    for name in ('customer', 'sales_person', 'payment_status', 'item', 'category', 'subcategory', 'item_type', 'brand'):
        value = (request.GET.get(name) or '').strip()
        if value.isdigit():
            values[name] = int(value)
    return values


def _build_sales_dimension_report(start_date=None, end_date=None, db_alias=None, dimension='customer', filters=None):
    db_alias = db_alias or get_current_db() or 'default'
    filters = filters or {}
    report = {'invoice_count': 0, 'total_sales': Decimal('0.00'), 'sales_invoices': []}

    try:
        from sales.models import SalesInvoice, SalesInvoiceItem

        invoices = safe_date_filter(
            SalesInvoice.objects.using(db_alias), 'date', start_date, end_date
        )
        if dimension == 'customer' and filters.get('customer'):
            invoices = invoices.filter(customer_id=filters['customer'])
        if dimension == 'salesperson' and filters.get('sales_person'):
            invoices = invoices.filter(sales_person_id=filters['sales_person'])

        item_filters = {
            'product_id': filters.get('item'),
            'product__category_id': filters.get('category'),
            'product__subcategory_id': filters.get('subcategory'),
            'product__item_type_id': filters.get('item_type'),
            'product__brand_id': filters.get('brand'),
        }
        item_filters = {key: value for key, value in item_filters.items() if value}
        if dimension == 'item' and item_filters:
            matching_invoice_ids = SalesInvoiceItem.objects.using(db_alias).filter(
                **item_filters
            ).values('sales_inv_id')
            invoices = invoices.filter(pk__in=matching_invoice_ids)

        report['invoice_count'] = invoices.count()
        report['total_sales'] = invoices.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')

        if dimension == 'item':
            invoice_items = SalesInvoiceItem.objects.using(db_alias).filter(
                sales_inv__in=invoices
            ).select_related(
                'sales_inv', 'sales_inv__payment_status', 'product',
                'product__category', 'product__subcategory',
                'product__item_type', 'product__brand',
            ).order_by('-sales_inv__date', '-sales_inv_id', 'pk')
            if item_filters:
                invoice_items = invoice_items.filter(**item_filters)
            for invoice_item in invoice_items:
                invoice = invoice_item.sales_inv
                product = invoice_item.product
                report['sales_invoices'].append({
                    'invoice_number': invoice.inv_number,
                    'date': invoice.date,
                    'amount': invoice.total_amount or Decimal('0.00'),
                    'payment_status': str(invoice.payment_status) if invoice.payment_status else 'Not Paid',
                    'item_name': product.name if product else 'Unknown',
                    'category_name': product.category.category_name if product and product.category_id else 'Uncategorized',
                    'subcategory_name': product.subcategory.subcategory_name if product and product.subcategory_id else 'Uncategorized',
                    'type_name': product.item_type.type_name if product and product.item_type_id else 'Unspecified',
                    'brand_name': product.brand.brand_name if product and product.brand_id else 'Unbranded',
                })
        else:
            for invoice in invoices.select_related('payment_status').order_by('-date', '-pk'):
                row = {
                    'invoice_number': invoice.inv_number,
                    'date': invoice.date,
                    'amount': invoice.total_amount or Decimal('0.00'),
                    'payment_status': str(invoice.payment_status) if invoice.payment_status else 'Not Paid',
                }
                if dimension == 'customer':
                    row['customer_name'] = _customer_display_name(invoice.customer)
                else:
                    row['sales_person'] = invoice.sales_person.name if invoice.sales_person else 'Unassigned'
                report['sales_invoices'].append(row)
    except Exception:
        logger.exception('Failed to build sales %s report', dimension)

    return report


def _build_purchase_report(start_date=None, end_date=None, db_alias=None):
    db_alias = db_alias or get_current_db() or 'default'
    report = {
        'total_purchases': Decimal('0.00'),
        'purchase_count': 0,
        'bills': [],
        'top_vendors': [],
        'top_items': [],
        'monthly_trend': [],
        'outstanding_purchases': Decimal('0.00'),
    }

    try:
        from Purchase.models import Bill, BillItem, BillPaymentAllocation

        bills = safe_date_filter(Bill.objects.using(db_alias), 'date', start_date, end_date)
        report['purchase_count'] = bills.count()
        report['total_purchases'] = bills.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')

        for bill in bills.select_related('vendor', 'payment_status').order_by('-date', '-pk'):
            report['bills'].append({
                'bill_number': bill.bill_number,
                'vendor_name': str(bill.vendor) if bill.vendor else 'Unknown',
                'date': bill.date,
                'amount': bill.total_amount or Decimal('0.00'),
                'payment_status': str(bill.payment_status) if bill.payment_status else 'Not Paid',
            })

        vendor_rows = bills.filter(vendor__isnull=False).values(
            'vendor',
            'vendor__company_name',
            'vendor__first_name',
            'vendor__last_name',
            'vendor__vendor_code',
        ).annotate(
            total_purchases=Coalesce(Sum('total_amount'), Decimal('0.00')),
            bill_count=Count('pk'),
        ).order_by('-total_purchases')[:10]

        for row in vendor_rows:
            report['top_vendors'].append({
                'vendor_name': _vendor_display_name_from_row(row),
                'total_purchases': row['total_purchases'] or Decimal('0.00'),
                'bill_count': row['bill_count'],
            })

        item_rows = BillItem.objects.using(db_alias).filter(
            bill__in=bills
        ).values(
            'product__name',
        ).annotate(
            qty=Sum('quantity'),
        ).order_by('-qty')[:6]

        for row in item_rows:
            report['top_items'].append({
                'item_name': row.get('product__name') or 'Unknown',
                'quantity_purchased': row.get('qty') or Decimal('0.00'),
            })

        trend_rows = bills.annotate(month=TruncMonth('date')).values('month').annotate(
            total_purchases=Coalesce(Sum('total_amount'), Decimal('0.00'))
        ).order_by('month')

        for row in trend_rows:
            month = row.get('month')
            report['monthly_trend'].append({
                'month': month.strftime('%Y-%m') if month else 'Unknown',
                'total_purchases': row['total_purchases'] or Decimal('0.00'),
            })

        paid_amount = BillPaymentAllocation.objects.using(db_alias).filter(bill__in=bills).aggregate(
            total=Coalesce(Sum('amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')
        report['outstanding_purchases'] = max(report['total_purchases'] - paid_amount, Decimal('0.00'))
    except Exception:
        pass

    return report


def _purchase_filter_options(db_alias=None):
    from Items.models import Item
    from Purchase.models import Vendor
    from brand.models import Brand
    from category.models import Category, Subcategory
    from type.models import Type

    db_alias = db_alias or get_current_db() or 'default'
    return {
        'vendors': Vendor.objects.using(db_alias).order_by('company_name', 'first_name', 'last_name'),
        'items': Item.objects.using(db_alias).filter(status=True).order_by('name'),
        'categories': Category.objects.using(db_alias).filter(status=True).order_by('category_name'),
        'subcategories': Subcategory.objects.using(db_alias).filter(status=True).order_by('subcategory_name'),
        'item_types': Type.objects.using(db_alias).filter(status=True).order_by('type_name'),
        'brands': Brand.objects.using(db_alias).filter(status=True).order_by('brand_name'),
    }


def _purchase_filter_values(request):
    values = {}
    for name in ('vendor', 'item', 'category', 'subcategory', 'item_type', 'brand'):
        value = (request.GET.get(name) or '').strip()
        if value.isdigit():
            values[name] = int(value)
    return values


def _build_purchase_dimension_report(start_date=None, end_date=None, db_alias=None, dimension='vendor', filters=None):
    db_alias = db_alias or get_current_db() or 'default'
    filters = filters or {}
    report = {'bill_count': 0, 'total_purchases': Decimal('0.00'), 'bills': []}

    try:
        from Purchase.models import Bill, BillItem

        bills = safe_date_filter(Bill.objects.using(db_alias), 'date', start_date, end_date)
        if dimension == 'vendor' and filters.get('vendor'):
            bills = bills.filter(vendor_id=filters['vendor'])

        item_filters = {
            'product_id': filters.get('item'),
            'product__category_id': filters.get('category'),
            'product__subcategory_id': filters.get('subcategory'),
            'product__item_type_id': filters.get('item_type'),
            'product__brand_id': filters.get('brand'),
        }
        item_filters = {key: value for key, value in item_filters.items() if value}
        if dimension == 'item' and item_filters:
            matching_bill_ids = BillItem.objects.using(db_alias).filter(
                **item_filters
            ).values('bill_id')
            bills = bills.filter(pk__in=matching_bill_ids)

        report['bill_count'] = bills.count()
        report['total_purchases'] = bills.aggregate(
            total=Coalesce(Sum('total_amount'), Decimal('0.00'))
        )['total'] or Decimal('0.00')

        if dimension == 'item':
            bill_items = BillItem.objects.using(db_alias).filter(
                bill__in=bills
            ).select_related(
                'bill', 'bill__payment_status', 'bill__vendor', 'product',
                'product__category', 'product__subcategory',
                'product__item_type', 'product__brand',
            ).order_by('-bill__date', '-bill_id', 'pk')
            if item_filters:
                bill_items = bill_items.filter(**item_filters)
            for bill_item in bill_items:
                bill = bill_item.bill
                product = bill_item.product
                report['bills'].append({
                    'bill_number': bill.bill_number,
                    'date': bill.date,
                    'amount': bill.total_amount or Decimal('0.00'),
                    'payment_status': str(bill.payment_status) if bill.payment_status else 'Not Paid',
                    'item_name': product.name if product else 'Unknown',
                    'category_name': product.category.category_name if product and product.category_id else 'Uncategorized',
                    'subcategory_name': product.subcategory.subcategory_name if product and product.subcategory_id else 'Uncategorized',
                    'type_name': product.item_type.type_name if product and product.item_type_id else 'Unspecified',
                    'brand_name': product.brand.brand_name if product and product.brand_id else 'Unbranded',
                })
        else:
            for bill in bills.select_related('vendor', 'payment_status').order_by('-date', '-pk'):
                report['bills'].append({
                    'bill_number': bill.bill_number,
                    'vendor_name': str(bill.vendor) if bill.vendor else 'Unknown',
                    'date': bill.date,
                    'amount': bill.total_amount or Decimal('0.00'),
                    'payment_status': str(bill.payment_status) if bill.payment_status else 'Not Paid',
                })
    except Exception:
        logger.exception('Failed to build purchase %s report', dimension)

    return report


def _build_inventory_report(start_date=None, end_date=None, filters=None):
    report = {
        'stock_value': Decimal('0.00'),
        'item_stock_details': [],
        'low_stock_items': [],
        'out_of_stock_items': [],
        'fast_moving_items': [],
        'slow_moving_items': [],
        'warehouse_summary': [],
        'category_summary': [],
    }

    try:
        from stock.models import Stock, StockMovement
        from sales.models import SalesInvoiceItem

        stocks = Stock.objects.filter(status=True, item__status=True).select_related(
            'item',
            'warehouse',
            'item__category',
            'item__subcategory',
            'item__item_type',
            'item__brand',
        )
        filters = filters or {}
        if end_date:
            stocks = stocks.filter(created_at__date__lte=end_date)
        if filters.get('item'):
            stocks = stocks.filter(item_id=filters['item'])
        if filters.get('category'):
            stocks = stocks.filter(item__category_id=filters['category'])
        if filters.get('subcategory'):
            stocks = stocks.filter(item__subcategory_id=filters['subcategory'])
        if filters.get('item_type'):
            stocks = stocks.filter(item__item_type_id=filters['item_type'])
        if filters.get('brand'):
            stocks = stocks.filter(item__brand_id=filters['brand'])
        if filters.get('warehouse'):
            stocks = stocks.filter(warehouse_id=filters['warehouse'])
        if not stocks.exists():
            return report

        stock_ids = list(stocks.values_list('pk', flat=True))
        total_movement_delta_by_stock = {}
        period_movement_delta_by_stock = {}

        def add_movement_rows(model, in_types):
            all_rows = model.objects.filter(stock_id__in=stock_ids).values(
                'stock_id', 'movement_type'
            ).annotate(quantity=Sum('quantity'))

            for row in all_rows:
                quantity = _to_decimal(row.get('quantity'))
                movement_type = row.get('movement_type')
                signed_quantity = quantity if movement_type in in_types else -quantity
                stock_id = row.get('stock_id')
                total_movement_delta_by_stock[stock_id] = (
                    total_movement_delta_by_stock.get(stock_id, Decimal('0.00')) + signed_quantity
                )

            if end_date:
                period_rows = model.objects.filter(
                    stock_id__in=stock_ids,
                    created_at__date__lte=end_date,
                ).values('stock_id', 'movement_type').annotate(quantity=Sum('quantity'))

                for row in period_rows:
                    quantity = _to_decimal(row.get('quantity'))
                    movement_type = row.get('movement_type')
                    signed_quantity = quantity if movement_type in in_types else -quantity
                    stock_id = row.get('stock_id')
                    period_movement_delta_by_stock[stock_id] = (
                        period_movement_delta_by_stock.get(stock_id, Decimal('0.00')) + signed_quantity
                    )

        add_movement_rows(StockMovement, {'adjustment_in', 'transfer_in'})

        try:
            from Purchase.models import StockMovement as PurchaseStockMovement
            add_movement_rows(PurchaseStockMovement, {'in', 'adjustment', 'transfer'})
        except Exception:
            pass

        try:
            from sales.models import SalesStockMovement
            add_movement_rows(SalesStockMovement, {'in'})
        except Exception:
            pass

        # When a period is selected, derive stock from opening balance plus
        # movements up to the period end. Without an end date, keep live stock.
        if end_date:
            stock_quantities = {}
            for stock in stocks:
                current_quantity = _to_decimal(stock.quantity)
                opening_quantity = getattr(stock, 'opening_stock', None)
                if opening_quantity is None:
                    opening_quantity = current_quantity - total_movement_delta_by_stock.get(stock.pk, Decimal('0.00'))
                else:
                    opening_quantity = _to_decimal(opening_quantity)
                stock_quantities[stock.pk] = (
                    opening_quantity + period_movement_delta_by_stock.get(stock.pk, Decimal('0.00'))
                )
        else:
            stock_quantities = {stock.pk: _to_decimal(stock.quantity) for stock in stocks}

        product_stats = {}
        warehouse_stats = {}
        category_stats = {}

        for stock in stocks:
            quantity = stock_quantities.get(stock.pk, Decimal('0.00'))
            item = stock.item
            cost_price = _to_decimal(item.cost_price or getattr(item, 'op_rate', 0))
            stock_value = quantity * cost_price
            report['stock_value'] += stock_value

            product_id = item.id
            category_name = item.category.category_name  if item.category_id else 'Uncategorized'
            warehouse_name = stock.warehouse.warehouse_name if stock.warehouse_id else 'Unknown'
            report['item_stock_details'].append({
                'item_name': item.name,
                'quantity': quantity,
                'warehouse_name': warehouse_name,
                'category_name': category_name,
                'subcategory_name': item.subcategory.subcategory_name if item.subcategory_id else 'Uncategorized',
                'type_name': item.item_type.type_name if item.item_type_id else 'Unspecified',
                'brand_name': item.brand.brand_name if item.brand_id else 'Unbranded',
                'stock_value': stock_value,
            })
            if product_id not in product_stats:
                product_stats[product_id] = {
                    'item_name': item.name,
                    'total_quantity': Decimal('0.00'),
                    'stock_value': Decimal('0.00'),
                    'min_stock': _to_decimal(item.min_stock or 0),
                    'quantity_sold': Decimal('0.00'),
                    'category': category_name,
                }

            product_stats[product_id]['total_quantity'] += quantity
            product_stats[product_id]['stock_value'] += stock_value

            warehouse_stats.setdefault(warehouse_name, {
                'warehouse_name': warehouse_name,
                'total_quantity': Decimal('0.00'),
                'stock_value': Decimal('0.00'),
            })
            warehouse_stats[warehouse_name]['total_quantity'] += quantity
            warehouse_stats[warehouse_name]['stock_value'] += stock_value

            category_stats.setdefault(category_name, {
                'category_name': category_name,
                'total_quantity': Decimal('0.00'),
                'stock_value': Decimal('0.00'),
            })
            category_stats[category_name]['total_quantity'] += quantity
            category_stats[category_name]['stock_value'] += stock_value

        sales_qs = SalesInvoiceItem.objects.filter(product__status=True)
        if start_date:
            sales_qs = sales_qs.filter(sales_inv__date__gte=start_date)
        if end_date:
            sales_qs = sales_qs.filter(sales_inv__date__lte=end_date)

        for row in sales_qs.values('product_id').annotate(quantity_sold=Sum('quantity')):
            product_id = row['product_id']
            if product_id in product_stats:
                product_stats[product_id]['quantity_sold'] = _to_decimal(row['quantity_sold'] or 0)

        report['fast_moving_items'] = sorted(
            product_stats.values(),
            key=lambda x: x['quantity_sold'],
            reverse=True,
        )[:10]

        report['slow_moving_items'] = sorted(
            [item for item in product_stats.values() if item['total_quantity'] > 0],
            key=lambda x: x['quantity_sold'],
        )[:10]

        report['low_stock_items'] = [
            item for item in product_stats.values()
            if item['min_stock'] > 0 and item['total_quantity'] <= item['min_stock'] and item['total_quantity'] > 0
        ][:10]

        report['out_of_stock_items'] = [
            item for item in product_stats.values()
            if item['total_quantity'] <= 0
        ][:10]

        report['warehouse_summary'] = sorted(warehouse_stats.values(), key=lambda x: x['warehouse_name'])
        report['category_summary'] = sorted(category_stats.values(), key=lambda x: x['category_name'])
        report['item_stock_details'].sort(key=lambda x: (x['item_name'].lower(), x['warehouse_name'].lower()))
    except Exception as e:
        import traceback
        print(f"[inventory_report] ERROR: {e}", flush=True)
        traceback.print_exc()

    return report


def _inventory_filter_options():
    from Items.models import Item
    from brand.models import Brand
    from category.models import Category, Subcategory
    from type.models import Type
    from warehouse.models import Warehouse

    return {
        'items': Item.objects.filter(status=True).order_by('name'),
        'categories': Category.objects.filter(status=True).order_by('category_name'),
        'subcategories': Subcategory.objects.filter(status=True).order_by('subcategory_name'),
        'item_types': Type.objects.filter(status=True).order_by('type_name'),
        'brands': Brand.objects.filter(status=True).order_by('brand_name'),
        'warehouses': Warehouse.objects.filter(status=True).order_by('warehouse_name'),
    }


def _item_cascade_data(db_alias=None):
    """Data for the Category -> Subcategory -> Type -> Brand dependent filters.

    Used by the Inventory by Items, Sales by Item and Purchase by Item reports.
    Brand has no direct link to category/subcategory/type, so brand_links holds
    the distinct combinations found on items; the page narrows brands from that.
    """
    from Items.models import Item
    from brand.models import Brand
    from category.models import Subcategory
    from type.models import Type

    def objects(model):
        return model.objects.using(db_alias) if db_alias else model.objects

    return {
        'subcategories': list(
            objects(Subcategory).filter(status=True)
            .order_by('subcategory_name')
            .values('id', 'subcategory_name', 'category_id')
        ),
        'types': list(
            objects(Type).filter(status=True)
            .order_by('type_name')
            .values('id', 'type_name', 'subcategory_id')
        ),
        'brands': list(
            objects(Brand).filter(status=True)
            .order_by('brand_name')
            .values('id', 'brand_name')
        ),
        'brand_links': list(
            objects(Item).filter(brand__isnull=False)
            .values('brand_id', 'category_id', 'subcategory_id', 'item_type_id')
            .distinct()
        ),
    }


def _inventory_filter_values(request):
    values = {}
    for name in ('item', 'category', 'subcategory', 'item_type', 'brand', 'warehouse'):
        value = (request.GET.get(name) or '').strip()
        if value.isdigit():
            values[name] = int(value)
    return values


def _build_finance_report(start_date=None, end_date=None):
    report = {
        'total_income': Decimal('0.00'),
        'total_expenses': Decimal('0.00'),
        'net_profit_loss': Decimal('0.00'),
        'opening_stock': Decimal('0.00'),
        'closing_stock': Decimal('0.00'),
        'stock_adjustments': Decimal('0.00'),
        'cash_summary': {
            'opening_cash': Decimal('0.00'),
            'closing_cash': Decimal('0.00'),
            'net_change': Decimal('0.00'),
        },
        'receivables_balance': Decimal('0.00'),
        'payables_balance': Decimal('0.00'),
        'income_groups': [],
        'expense_groups': [],
        'monthly_profit_trend': [],
    }

    try:
        from chart_of_accounts.balance_sheet import (
            generate_profit_loss,
            generate_cash_flow_statement,
            get_account_balance,
        )
        from chart_of_accounts.models import ChartOfAccounts
        from datetime import date, timedelta
        from sales.models import SalesInvoice
        from Purchase.models import Bill

        fy_start_date, _ = financial_year_bounds(end_date or date.today())
        pl_data = generate_profit_loss(start_date, end_date, fy_start_date)
        cash_data = generate_cash_flow_statement(start_date, end_date, fy_start_date=fy_start_date)

        report['total_income'] = Decimal(str(pl_data.get('sales', 0))) + Decimal(str(pl_data.get('non_operating_income', 0)))
        report['total_expenses'] = Decimal(str(pl_data.get('cogs', 0))) + Decimal(str(pl_data.get('operating_expenses_total', 0)))
        report['net_profit_loss'] = Decimal(str(pl_data.get('net_profit_loss', 0)))
        report['opening_stock'] = Decimal(str(pl_data.get('opening_stock', 0)))
        report['closing_stock'] = Decimal(str(pl_data.get('closing_stock', 0)))
        report['stock_adjustments'] = Decimal(str(pl_data.get('stock_adjustments', 0)))

        report['cash_summary'] = {
            'opening_cash': Decimal(str(cash_data.get('cash_movement', {}).get('opening_cash_balance', 0))),
            'closing_cash': Decimal(str(cash_data.get('cash_movement', {}).get('closing_cash_balance', 0))),
            'net_change': Decimal(str(cash_data.get('cash_movement', {}).get('net_change_in_cash', 0))),
        }

        report['income_groups'] = pl_data.get('income', [])
        report['expense_groups'] = pl_data.get('expenses', [])

        receivables_balance = Decimal('0.00')
        payables_balance = Decimal('0.00')
        for account in ChartOfAccounts.objects.filter(status=True):
            name = (account.name or '').lower()
            balance = Decimal(str(get_account_balance(account, as_of_date=end_date)))
            if 'receivable' in name or 'debtor' in name or 'accounts receivable' in name:
                receivables_balance += balance
            if 'payable' in name or 'creditor' in name or 'accounts payable' in name or 'supplier' in name:
                payables_balance += balance

        report['receivables_balance'] = receivables_balance
        report['payables_balance'] = payables_balance

        if start_date and end_date:
            current = start_date.replace(day=1)
            until = end_date.replace(day=1)
            while current <= until:
                next_month = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
                month_end = next_month - timedelta(days=1)
                sales = SalesInvoice.objects.filter(date__gte=current, date__lte=month_end).aggregate(
                    total=Coalesce(Sum('total_amount'), Decimal('0.00'))
                )['total'] or Decimal('0.00')
                purchases = Bill.objects.filter(date__gte=current, date__lte=month_end).aggregate(
                    total=Coalesce(Sum('total_amount'), Decimal('0.00'))
                )['total'] or Decimal('0.00')
                report['monthly_profit_trend'].append({
                    'month': current.strftime('%Y-%m'),
                    'sales': sales,
                    'purchases': purchases,
                    'profit': sales - purchases,
                })
                current = next_month
    except Exception:
        pass

    return report


def _build_crm_report(start_date=None, end_date=None):
    report = {
        'lead_count': 0,
        'opportunity_count': 0,
        'won_deals': 0,
        'lost_deals': 0,
        'conversion_rate': 0.0,
        'followups': {
            'pending': 0,
            'completed': 0,
        },
        'pipeline_value': Decimal('0.00'),
        'source_wise': [],
        'leads': [],
        'opportunities': [],
        'followup_list': [],
        'presales': [],
    }

    try:
        from crm.models import Lead, Opportunity, FollowUp, PreSalesInteraction
        from django.db.models import Count

        leads = safe_date_filter(Lead.objects.all(), 'created_at__date', start_date, end_date)
        opps = safe_date_filter(Opportunity.objects.all(), 'created_at__date', start_date, end_date)

        report['lead_count'] = leads.count()
        report['opportunity_count'] = opps.count()

        report['won_deals'] = opps.filter(status__iexact='Closed Won').count()
        report['lost_deals'] = opps.filter(status__iexact='Closed Lost').count()

        if report['lead_count']:
            report['conversion_rate'] = float(report['won_deals']) / float(report['lead_count']) * 100.0
        else:
            report['conversion_rate'] = 0.0

        for lead in leads.select_related('assigned_to', 'assigned_employee').order_by('-created_at', '-pk'):
            assigned_to = getattr(lead, 'assigned_employee', None) or getattr(lead, 'assigned_to', None)
            report['leads'].append({
                'lead_number': getattr(lead, 'lead_number', '') or 'N/A',
                'customer_name': getattr(lead, 'customer_name', '') or 'N/A',
                'phone': getattr(lead, 'phone', '') or 'N/A',
                'email': getattr(lead, 'email', '') or 'N/A',
                'product_interested': getattr(lead, 'product_interested', '') or 'N/A',
                'source': getattr(lead, 'source', '') or 'N/A',
                'priority': getattr(lead, 'priority', '') or 'N/A',
                'status': getattr(lead, 'status', '') or 'N/A',
                'assigned_to': str(assigned_to) if assigned_to else 'N/A',
                'created_at': getattr(lead, 'created_at', None),
            })

        for opportunity in opps.select_related('lead', 'assigned_to', 'assigned_employee').order_by('-created_at', '-pk'):
            assigned_to = getattr(opportunity, 'assigned_employee', None) or getattr(opportunity, 'assigned_to', None)
            lead = getattr(opportunity, 'lead', None)
            report['opportunities'].append({
                'opportunity_number': getattr(opportunity, 'opportunity_number', '') or 'N/A',
                'lead_number': getattr(lead, 'lead_number', '') if lead else 'N/A',
                'customer_name': getattr(lead, 'customer_name', '') if lead else 'N/A',
                'estimated_deal_value': getattr(opportunity, 'estimated_deal_value', Decimal('0.00')),
                'expected_closing_date': getattr(opportunity, 'expected_closing_date', None),
                'status': getattr(opportunity, 'status', '') or 'N/A',
                'priority': getattr(opportunity, 'priority', '') or 'N/A',
                'assigned_to': str(assigned_to) if assigned_to else 'N/A',
                'created_at': getattr(opportunity, 'created_at', None),
            })

        followups = FollowUp.objects.all()
        if start_date:
            followups = followups.filter(followup_date__date__gte=start_date)
        if end_date:
            followups = followups.filter(followup_date__date__lte=end_date)

        report['followups']['pending'] = followups.filter(status__iexact='Pending').count()
        report['followups']['completed'] = followups.filter(status__iexact='Completed').count()

        for followup in followups.select_related(
            'lead', 'opportunity', 'opportunity__lead', 'assigned_to', 'assigned_employee'
        ).order_by('followup_date', '-pk'):
            assigned_to = getattr(followup, 'assigned_employee', None) or getattr(followup, 'assigned_to', None)
            lead = getattr(followup, 'lead', None) or getattr(getattr(followup, 'opportunity', None), 'lead', None)
            opportunity = getattr(followup, 'opportunity', None)
            report['followup_list'].append({
                'date': getattr(followup, 'followup_date', None),
                'customer_name': getattr(lead, 'customer_name', '') if lead else 'N/A',
                'lead_number': getattr(lead, 'lead_number', '') if lead else 'N/A',
                'opportunity_number': getattr(opportunity, 'opportunity_number', '') if opportunity else 'N/A',
                'description': getattr(followup, 'description', '') or 'N/A',
                'reminder_interval': getattr(followup, 'reminder_interval', '') or 'N/A',
                'assigned_to': str(assigned_to) if assigned_to else 'N/A',
                'status': getattr(followup, 'status', '') or 'N/A',
            })

        presales_qs = safe_date_filter(
            PreSalesInteraction.objects.all(),
            'created_at__date',
            start_date,
            end_date,
        )
        for presale in presales_qs.select_related(
            'lead', 'opportunity', 'opportunity__lead', 'assigned_employee'
        ).order_by('-created_at', '-pk'):
            lead = getattr(presale, 'lead', None) or getattr(getattr(presale, 'opportunity', None), 'lead', None)
            opportunity = getattr(presale, 'opportunity', None)
            report['presales'].append({
                'created_at': getattr(presale, 'created_at', None),
                'customer_name': getattr(presale, 'customer_name', '') or getattr(lead, 'customer_name', '') or 'N/A',
                'product': getattr(presale, 'product', '') or 'N/A',
                'contact': getattr(presale, 'contact', '') or 'N/A',
                'type': getattr(presale, 'type', '') or 'N/A',
                'lead_number': getattr(lead, 'lead_number', '') if lead else 'N/A',
                'opportunity_number': getattr(opportunity, 'opportunity_number', '') if opportunity else 'N/A',
                'assigned_to': str(getattr(presale, 'assigned_employee', None)) if getattr(presale, 'assigned_employee', None) else 'N/A',
                'description': getattr(presale, 'description', '') or 'N/A',
            })

        # Pipeline value: sum of estimated_deal_value for open opportunities
        open_statuses = ['Open', 'Discussion', 'Quotation Created']
        pipeline_qs = safe_date_filter(
            Opportunity.objects.filter(status__in=open_statuses),
            'created_at__date',
            start_date,
            end_date,
        )
        pipeline_sum = pipeline_qs.aggregate(total=Coalesce(Sum('estimated_deal_value'), Decimal('0.00')))['total'] or Decimal('0.00')
        report['pipeline_value'] = pipeline_sum

        # Source-wise leads
        source_rows = leads.values('source').annotate(count=Count('pk')).order_by('-count')
        for row in source_rows:
            report['source_wise'].append({'source': row.get('source') or 'Unknown', 'count': row.get('count', 0)})
    except Exception:
        pass

    return report


def sales_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_sales(request.user)):
        messages.error(request, 'You do not have permission to view Sales MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    sales_filters = _sales_filter_values(request)
    report_data = _build_sales_report(
        start_date, end_date, getattr(request, 'company_db', None), sales_filters
    )
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **sales_filters,
    })

    return render(request, 'mis_reports/sales_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'sales_filters': sales_filters,
        'sales_filter_options': _sales_filter_options(getattr(request, 'company_db', None)),
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def sales_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    sales_filters = _sales_filter_values(request)
    report_data = _build_sales_report(
        start_date, end_date, getattr(request, 'company_db', None), sales_filters
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_sales_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['Sales MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Total Sales', f'{report_data["total_sales"]:.2f}'])
    writer.writerow(['Invoice Count', report_data['invoice_count']])
    writer.writerow(['Quotation Count', report_data['quotation_count']])
    writer.writerow(['Outstanding Sales', f'{report_data["outstanding_sales"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Top Customers'])
    writer.writerow(['Customer', 'Invoice Count', 'Total Sales'])
    for customer in report_data['top_customers']:
        writer.writerow([
            customer['customer_name'],
            customer['invoice_count'],
            f'{customer["total_sales"]:.2f}',
        ])
    writer.writerow([])

    writer.writerow(['Top Selling Items'])
    writer.writerow(['Item', 'Quantity Sold'])
    for item in report_data['top_items']:
        writer.writerow([item['item_name'], f'{item["quantity_sold"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Monthly Sales Trend'])
    writer.writerow(['Month', 'Total Sales'])
    for trend in report_data['monthly_trend']:
        writer.writerow([trend['month'], f'{trend["total_sales"]:.2f}'])

    return response


def _sales_dimension_report(request, company_code, dimension, template_name):
    if not (getattr(request.user, 'is_superuser', False) or can_view_sales(request.user)):
        messages.error(request, 'You do not have permission to view Sales MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    sales_filters = _sales_filter_values(request)
    report_data = _build_sales_dimension_report(
        start_date, end_date, getattr(request, 'company_db', None), dimension, sales_filters
    )
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **sales_filters,
    })

    return render(request, template_name, {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'sales_dimension': dimension,
        'sales_filters': sales_filters,
        'sales_filter_options': _sales_filter_options(getattr(request, 'company_db', None)),
        'item_cascade_data': _item_cascade_data(getattr(request, 'company_db', None)) if dimension == 'item' else None,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def sales_by_customer_report(request, company_code=None):
    return _sales_dimension_report(request, company_code, 'customer', 'mis_reports/sales_by_customer_report.html')


def sales_by_salesperson_report(request, company_code=None):
    return _sales_dimension_report(request, company_code, 'salesperson', 'mis_reports/sales_by_salesperson_report.html')


def sales_by_item_report(request, company_code=None):
    return _sales_dimension_report(request, company_code, 'item', 'mis_reports/sales_by_item_report.html')

def _resolve_filter_label(dimension, sales_filters, company_db):
    """Return a human-readable 'Filter applied' string for the CSV header."""
    if dimension == 'customer':
        customer_id = sales_filters.get('customer')
        if not customer_id:
            return 'Customer Filter Applied', 'All customers'
        from customer.models import Customer
        customer = Customer.objects.using(company_db).filter(pk=customer_id).first()
        return 'Customer Filter Applied', (_customer_display_name(customer) if customer else 'All customers')

    if dimension == 'salesperson':
        sp_id = sales_filters.get('sales_person')
        if not sp_id:
            return 'Sales Person Filter Applied', 'All sales persons'
        from sales.models import SalesPerson  # adjust import to actual app
        sp = SalesPerson.objects.using(company_db).filter(pk=sp_id).first()
        return 'Sales Person Filter Applied', (sp.name if sp else 'All sales persons')
    if dimension == 'item':
        parts = []
        item_id = sales_filters.get('item')
        category_id = sales_filters.get('category')
        subcategory_id = sales_filters.get('subcategory')
        type_id = sales_filters.get('item_type')
        brand_id = sales_filters.get('brand')

        options = _sales_filter_options(company_db)

        if item_id:
            match = next((i for i in options['items'] if str(i.pk) == str(item_id)), None)
            if match:
                parts.append(f'Item: {match.name}')
        if category_id:
            match = next((c for c in options['categories'] if str(c.pk) == str(category_id)), None)
            if match:
                parts.append(f'Category: {match.category_name}')
        if subcategory_id:
            match = next((s for s in options['subcategories'] if str(s.pk) == str(subcategory_id)), None)
            if match:
                parts.append(f'Subcategory: {match.subcategory_name}')
        if type_id:
            match = next((t for t in options['item_types'] if str(t.pk) == str(type_id)), None)
            if match:
                parts.append(f'Type: {match.type_name}')
        if brand_id:
            match = next((b for b in options['brands'] if str(b.pk) == str(brand_id)), None)
            if match:
                parts.append(f'Brand: {match.brand_name}')

        return 'Item Filter Applied', (', '.join(parts) if parts else 'All items')

    return 'Filter Applied', 'N/A'

def _resolve_filter_labels(dimension, sales_filters, company_db):
    """Return a list of (row_label, value) tuples for the CSV header's filter rows."""
    if dimension == 'customer':
        customer_id = sales_filters.get('customer')
        if not customer_id:
            return [('Customer Filter Applied', 'All customers')]
        from customer.models import Customer
        customer = Customer.objects.using(company_db).filter(pk=customer_id).first()
        return [('Customer Filter Applied', _customer_display_name(customer) if customer else 'All customers')]

    if dimension == 'salesperson':
        sp_id = sales_filters.get('sales_person')
        if not sp_id:
            return [('Sales Person Filter Applied', 'All sales persons')]
        options = _sales_filter_options(company_db)
        match = next((sp for sp in options['sales_persons'] if str(sp.pk) == str(sp_id)), None)
        return [('Sales Person Filter Applied', match.name if match else 'All sales persons')]

    if dimension == 'item':
        options = _sales_filter_options(company_db)
        rows = []

        item_id = sales_filters.get('item')
        match = next((i for i in options['items'] if str(i.pk) == str(item_id)), None) if item_id else None
        rows.append(('Item Filter Applied', match.name if match else 'All items'))

        category_id = sales_filters.get('category')
        match = next((c for c in options['categories'] if str(c.pk) == str(category_id)), None) if category_id else None
        rows.append(('Category Filter Applied', match.category_name if match else 'All categories'))

        subcategory_id = sales_filters.get('subcategory')
        match = next((s for s in options['subcategories'] if str(s.pk) == str(subcategory_id)), None) if subcategory_id else None
        rows.append(('Subcategory Filter Applied', match.subcategory_name if match else 'All subcategories'))

        type_id = sales_filters.get('item_type')
        match = next((t for t in options['item_types'] if str(t.pk) == str(type_id)), None) if type_id else None
        rows.append(('Type Filter Applied', match.type_name if match else 'All types'))

        brand_id = sales_filters.get('brand')
        match = next((b for b in options['brands'] if str(b.pk) == str(brand_id)), None) if brand_id else None
        rows.append(('Brand Filter Applied', match.brand_name if match else 'All brands'))

        return rows

    return [('Filter Applied', 'N/A')]

def _sales_dimension_report_export_csv(request, company_code, dimension, filename, title, columns):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    sales_filters = _sales_filter_values(request)
    company_db = getattr(request, 'company_db', None)
    report_data = _build_sales_dimension_report(
        start_date, end_date, company_db, dimension, sales_filters
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([title])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])
    for label, value in _resolve_filter_labels(dimension, sales_filters, company_db):   # <-- loop now
        writer.writerow([label, value])

    writer.writerow([])
    writer.writerow(columns)

    for invoice in report_data['sales_invoices']:
        row = [invoice['invoice_number'], invoice['date'].strftime('%Y-%m-%d'), f'{invoice["amount"]:.2f}', invoice['payment_status']]
        if dimension == 'customer':
            row.insert(2, invoice['customer_name'])
        elif dimension == 'salesperson':
            row.insert(2, invoice['sales_person'])
        else:
            row.extend([
                invoice['item_name'],
                invoice['category_name'],
                invoice['subcategory_name'],
                invoice['type_name'],
                invoice['brand_name'],
            ])
        writer.writerow(row)

    return response


def sales_by_customer_report_export_csv(request, company_code=None):
    return _sales_dimension_report_export_csv(
        request, company_code, 'customer', 'mis_sales_by_customer.csv',
        'Sales by Customer', ['Invoice', 'Date', 'Customer', 'Amount', 'Payment Status']
    )


def sales_by_salesperson_report_export_csv(request, company_code=None):
    return _sales_dimension_report_export_csv(
        request, company_code, 'salesperson', 'mis_sales_by_salesperson.csv',
        'Sales by Salesperson', ['Invoice', 'Date', 'Sales Person', 'Amount', 'Payment Status']
    )


def sales_by_item_report_export_csv(request, company_code=None):
    return _sales_dimension_report_export_csv(
        request, company_code, 'item', 'mis_sales_by_item.csv',
        'Sales by Item', ['Invoice', 'Date', 'Amount', 'Payment Status', 'Item', 'Category', 'Subcategory', 'Type', 'Brand']
    )


def purchase_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_purchase(request.user)):
        messages.error(request, 'You do not have permission to view Purchase MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_purchase_report(
        start_date, end_date, getattr(request, 'company_db', None)
    )
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    return render(request, 'mis_reports/purchase_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def _purchase_dimension_report(request, company_code, dimension, template_name):
    if not (getattr(request.user, 'is_superuser', False) or can_view_purchase(request.user)):
        messages.error(request, 'You do not have permission to view Purchase MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    purchase_filters = _purchase_filter_values(request)
    report_data = _build_purchase_dimension_report(
        start_date, end_date, getattr(request, 'company_db', None), dimension, purchase_filters
    )
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **purchase_filters,
    })

    return render(request, template_name, {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'purchase_dimension': dimension,
        'purchase_filters': purchase_filters,
        'purchase_filter_options': _purchase_filter_options(getattr(request, 'company_db', None)),
        'item_cascade_data': _item_cascade_data(getattr(request, 'company_db', None)) if dimension == 'item' else None,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def purchase_by_vendor_report(request, company_code=None):
    return _purchase_dimension_report(
        request, company_code, 'vendor', 'mis_reports/purchase_by_vendor_report.html'
    )


def purchase_by_item_report(request, company_code=None):
    return _purchase_dimension_report(
        request, company_code, 'item', 'mis_reports/purchase_by_item_report.html'
    )


def _purchase_dimension_report_export_csv(request, company_code, dimension, filename, title, columns):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    purchase_filters = _purchase_filter_values(request)
    company_db = getattr(request, 'company_db', None)
    report_data = _build_purchase_dimension_report(
        start_date, end_date, company_db, dimension, purchase_filters
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow([title])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])

    for label, value in _resolve_purchase_filter_labels(dimension, purchase_filters, company_db):   # <-- added
        writer.writerow([label, value])

    writer.writerow([])
    writer.writerow(columns)

    for bill in report_data['bills']:
        row = [bill['bill_number'], bill['date'].strftime('%Y-%m-%d'), f'{bill["amount"]:.2f}', bill['payment_status']]
        if dimension == 'vendor':
            row.insert(2, bill['vendor_name'])
        else:
            row.extend([
                bill['item_name'],
                bill['category_name'],
                bill['subcategory_name'],
                bill['type_name'],
                bill['brand_name'],
            ])
        writer.writerow(row)

    return response


def purchase_by_vendor_report_export_csv(request, company_code=None):
    return _purchase_dimension_report_export_csv(
        request, company_code, 'vendor', 'mis_purchase_by_vendor.csv',
        'Purchase by Vendor', ['Bill', 'Date', 'Vendor', 'Amount', 'Payment Status']
    )


def purchase_by_item_report_export_csv(request, company_code=None):
    return _purchase_dimension_report_export_csv(
        request, company_code, 'item', 'mis_purchase_by_item.csv',
        'Purchase by Item', ['Bill', 'Date', 'Amount', 'Payment Status', 'Item', 'Category', 'Subcategory', 'Type', 'Brand']
    )


def purchase_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_purchase_report(
        start_date, end_date, getattr(request, 'company_db', None)
    )

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_purchase_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['Purchase MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Total Purchases', f'{report_data["total_purchases"]:.2f}'])
    writer.writerow(['Purchase Count', report_data['purchase_count']])
    writer.writerow(['Outstanding Purchases', f'{report_data["outstanding_purchases"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Top Vendors'])
    writer.writerow(['Vendor', 'Bill Count', 'Total Purchases'])
    for vendor in report_data['top_vendors']:
        writer.writerow([
            vendor['vendor_name'],
            vendor['bill_count'],
            f'{vendor["total_purchases"]:.2f}',
        ])
    writer.writerow([])

    writer.writerow(['Top Purchased Items'])
    writer.writerow(['Item', 'Quantity Purchased'])
    for item in report_data['top_items']:
        writer.writerow([item['item_name'], f'{item["quantity_purchased"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Monthly Purchase Trend'])
    writer.writerow(['Month', 'Total Purchases'])
    for trend in report_data['monthly_trend']:
        writer.writerow([trend['month'], f'{trend["total_purchases"]:.2f}'])

    return response

def _resolve_purchase_filter_labels(dimension, purchase_filters, company_db):
    """Return a list of (row_label, value) tuples for the CSV header's filter rows."""
    options = _purchase_filter_options(company_db)

    if dimension == 'vendor':
        vendor_id = purchase_filters.get('vendor')
        if not vendor_id:
            return [('Vendor Filter Applied', 'All vendors')]
        match = next((v for v in options['vendors'] if str(v.pk) == str(vendor_id)), None)
        return [('Vendor Filter Applied', str(match) if match else 'All vendors')]

    if dimension == 'item':
        rows = []

        item_id = purchase_filters.get('item')
        match = next((i for i in options['items'] if str(i.pk) == str(item_id)), None) if item_id else None
        rows.append(('Item Filter Applied', match.name if match else 'All items'))

        category_id = purchase_filters.get('category')
        match = next((c for c in options['categories'] if str(c.pk) == str(category_id)), None) if category_id else None
        rows.append(('Category Filter Applied', match.category_name if match else 'All categories'))

        subcategory_id = purchase_filters.get('subcategory')
        match = next((s for s in options['subcategories'] if str(s.pk) == str(subcategory_id)), None) if subcategory_id else None
        rows.append(('Subcategory Filter Applied', match.subcategory_name if match else 'All subcategories'))

        type_id = purchase_filters.get('item_type')
        match = next((t for t in options['item_types'] if str(t.pk) == str(type_id)), None) if type_id else None
        rows.append(('Type Filter Applied', match.type_name if match else 'All types'))

        brand_id = purchase_filters.get('brand')
        match = next((b for b in options['brands'] if str(b.pk) == str(brand_id)), None) if brand_id else None
        rows.append(('Brand Filter Applied', match.brand_name if match else 'All brands'))

        return rows

    return [('Filter Applied', 'N/A')]

def inventory_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_inventory(request.user)):
        messages.error(request, 'You do not have permission to view Inventory MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    inventory_filters = _inventory_filter_values(request)
    report_data = _build_inventory_report(start_date, end_date, inventory_filters)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **inventory_filters,
    })

    return render(request, 'mis_reports/inventory_items_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'inventory_filters': inventory_filters,
        'inventory_filter_options': _inventory_filter_options(),
        'inventory_cascade_data': _item_cascade_data(),
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })

def inventory_report_summary(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_inventory(request.user)):
        messages.error(request, 'You do not have permission to view Inventory MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    inventory_filters = _inventory_filter_values(request)
    report_data = _build_inventory_report(start_date, end_date, inventory_filters)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **inventory_filters,
    })

    return render(request, 'mis_reports/inventory_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'inventory_filters': inventory_filters,
        'inventory_filter_options': _inventory_filter_options(),
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })

def inventory_warehouses_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_inventory(request.user)):
        messages.error(request, 'You do not have permission to view Inventory MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    requested_filters = _inventory_filter_values(request)
    warehouse_filters = {
        'warehouse': requested_filters.get('warehouse'),
    }
    report_data = _build_inventory_report(start_date, end_date, warehouse_filters)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
        **warehouse_filters,
    })

    return render(request, 'mis_reports/inventory_warehouses_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'inventory_filters': warehouse_filters,
        'inventory_filter_options': _inventory_filter_options(),
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })

def _resolve_warehouse_filter_label(warehouse_filters):
    """Return (row_label, value) for the CSV header's filter row."""
    warehouse_id = warehouse_filters.get('warehouse')
    if not warehouse_id:
        return 'Warehouse Filter Applied', 'All warehouses'
    options = _inventory_filter_options()
    match = next((w for w in options['warehouses'] if str(w.pk) == str(warehouse_id)), None)
    return 'Warehouse Filter Applied', (match.warehouse_name if match else 'All warehouses')

def inventory_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    inventory_filters = _inventory_filter_values(request)
    report_data = _build_inventory_report(start_date, end_date, inventory_filters)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_inventory_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['Inventory MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])

    for label, value in _resolve_inventory_filter_labels(inventory_filters):   # <-- added
        writer.writerow([label, value])

    writer.writerow([])

    writer.writerow(['Item Stock Details'])
    writer.writerow(['Item', 'Stock Quantity', 'Warehouse', 'Category', 'Subcategory', 'Type', 'Brand', 'Stock Value'])
    for item in report_data['item_stock_details']:
        writer.writerow([
            item['item_name'],
            f'{item["quantity"]:.2f}',
            item['warehouse_name'],
            item['category_name'],
            item['subcategory_name'],
            item['type_name'],
            item['brand_name'],
            f'{item["stock_value"]:.2f}',
        ])

    return response


def inventory_warehouses_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    requested_filters = _inventory_filter_values(request)
    warehouse_filters = {'warehouse': requested_filters.get('warehouse')}
    report_data = _build_inventory_report(start_date, end_date, warehouse_filters)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_inventory_warehouses_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['Inventory by Warehouses'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])

    label, value = _resolve_warehouse_filter_label(warehouse_filters)   # <-- added
    writer.writerow([label, value])

    writer.writerow([])
    writer.writerow(['Item Stock by Warehouse'])
    writer.writerow(['Item', 'Warehouse', 'Stock Quantity', 'Stock Value'])
    for item in report_data['item_stock_details']:
        writer.writerow([
            item['item_name'],
            item['warehouse_name'],
            f'{item["quantity"]:.2f}',
            f'{item["stock_value"]:.2f}',
        ])

    return response


def _resolve_inventory_filter_labels(inventory_filters):
    """Return a list of (row_label, value) tuples for the CSV header's filter rows."""
    options = _inventory_filter_options()
    rows = []

    item_id = inventory_filters.get('item')
    match = next((i for i in options['items'] if str(i.pk) == str(item_id)), None) if item_id else None
    rows.append(('Item Filter Applied', match.name if match else 'All items'))

    category_id = inventory_filters.get('category')
    match = next((c for c in options['categories'] if str(c.pk) == str(category_id)), None) if category_id else None
    rows.append(('Category Filter Applied', match.category_name if match else 'All categories'))

    subcategory_id = inventory_filters.get('subcategory')
    match = next((s for s in options['subcategories'] if str(s.pk) == str(subcategory_id)), None) if subcategory_id else None
    rows.append(('Subcategory Filter Applied', match.subcategory_name if match else 'All subcategories'))

    type_id = inventory_filters.get('item_type')
    match = next((t for t in options['item_types'] if str(t.pk) == str(type_id)), None) if type_id else None
    rows.append(('Type Filter Applied', match.type_name if match else 'All types'))

    brand_id = inventory_filters.get('brand')
    match = next((b for b in options['brands'] if str(b.pk) == str(brand_id)), None) if brand_id else None
    rows.append(('Brand Filter Applied', match.brand_name if match else 'All brands'))

    return rows

def finance_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_finance(request.user)):
        messages.error(request, 'You do not have permission to view Finance MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_finance_report(start_date, end_date)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    return render(request, 'mis_reports/finance_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def finance_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_finance_report(start_date, end_date)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_finance_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['Finance MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])
    writer.writerow(['Total Income', f'{report_data["total_income"]:.2f}'])
    writer.writerow(['Total Expenses', f'{report_data["total_expenses"]:.2f}'])
    writer.writerow(['Net Profit/Loss', f'{report_data["net_profit_loss"]:.2f}'])
    writer.writerow(['Opening Stock', f'{report_data["opening_stock"]:.2f}'])
    writer.writerow(['Closing Stock', f'{report_data["closing_stock"]:.2f}'])
    writer.writerow(['Stock Adjustments', f'{report_data["stock_adjustments"]:.2f}'])
    writer.writerow(['Opening Cash', f'{report_data["cash_summary"]["opening_cash"]:.2f}'])
    writer.writerow(['Closing Cash', f'{report_data["cash_summary"]["closing_cash"]:.2f}'])
    writer.writerow(['Receivables Balance', f'{report_data["receivables_balance"]:.2f}'])
    writer.writerow(['Payables Balance', f'{report_data["payables_balance"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Monthly Profit Trend'])
    writer.writerow(['Month', 'Sales', 'Purchases', 'Profit'])
    for row in report_data['monthly_profit_trend']:
        writer.writerow([
            row['month'],
            f'{row["sales"]:.2f}',
            f'{row["purchases"]:.2f}',
            f'{row["profit"]:.2f}',
        ])
    writer.writerow([])

    writer.writerow(['Income Group Summary'])
    writer.writerow(['Name', 'Balance'])
    for group in report_data['income_groups']:
        writer.writerow([group['name'], f'{group["balance"]:.2f}'])
    writer.writerow([])

    writer.writerow(['Expense Group Summary'])
    writer.writerow(['Name', 'Balance'])
    for group in report_data['expense_groups']:
        writer.writerow([group['name'], f'{group["balance"]:.2f}'])

    return response


def crm_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_crm(request.user)):
        messages.error(request, 'You do not have permission to view CRM MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_crm_report(start_date, end_date)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    return render(request, 'mis_reports/crm_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def crm_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_crm_report(start_date, end_date)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_crm_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['CRM MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])
    writer.writerow(['Lead Count', report_data['lead_count']])
    writer.writerow(['Opportunity Count', report_data['opportunity_count']])
    writer.writerow(['Won Deals', report_data['won_deals']])
    writer.writerow(['Lost Deals', report_data['lost_deals']])
    writer.writerow(['Conversion Rate', f"{report_data['conversion_rate']:.2f}%"])
    writer.writerow(['Pipeline Value', f"{report_data['pipeline_value']:.2f}"])
    writer.writerow([])

    writer.writerow(['Leads'])
    writer.writerow(['Lead No.', 'Customer', 'Phone', 'Email', 'Product', 'Source', 'Priority', 'Status', 'Assigned To', 'Created'])
    for lead in report_data['leads']:
        writer.writerow([
            lead['lead_number'],
            lead['customer_name'],
            lead['phone'],
            lead['email'],
            lead['product_interested'],
            lead['source'],
            lead['priority'],
            lead['status'],
            lead['assigned_to'],
            lead['created_at'].date() if lead['created_at'] else '',
        ])
    writer.writerow([])

    writer.writerow(['Opportunities'])
    writer.writerow(['Opportunity No.', 'Lead No.', 'Customer', 'Deal Value', 'Expected Close', 'Status', 'Priority', 'Assigned To', 'Created'])
    for opportunity in report_data['opportunities']:
        writer.writerow([
            opportunity['opportunity_number'],
            opportunity['lead_number'],
            opportunity['customer_name'],
            f"{_to_decimal(opportunity['estimated_deal_value']):.2f}",
            opportunity['expected_closing_date'] or '',
            opportunity['status'],
            opportunity['priority'],
            opportunity['assigned_to'],
            opportunity['created_at'].date() if opportunity['created_at'] else '',
        ])
    writer.writerow([])

    writer.writerow(['Follow-ups'])
    writer.writerow(['Pending', report_data['followups']['pending']])
    writer.writerow(['Completed', report_data['followups']['completed']])
    writer.writerow([])

    writer.writerow(['Follow-up List'])
    writer.writerow(['Follow-up Date', 'Customer', 'Lead No.', 'Opportunity No.', 'Description', 'Reminder', 'Assigned To', 'Status'])
    for followup in report_data['followup_list']:
        writer.writerow([
            followup['date'].date() if followup['date'] else '',
            followup['customer_name'],
            followup['lead_number'],
            followup['opportunity_number'],
            followup['description'],
            followup['reminder_interval'],
            followup['assigned_to'],
            followup['status'],
        ])
    writer.writerow([])

    writer.writerow(['Pre-sales List'])
    writer.writerow(['Date', 'Customer', 'Product', 'Contact', 'Type', 'Lead No.', 'Opportunity No.', 'Assigned To', 'Description'])
    for presale in report_data['presales']:
        writer.writerow([
            presale['created_at'].date() if presale['created_at'] else '',
            presale['customer_name'],
            presale['product'],
            presale['contact'],
            presale['type'],
            presale['lead_number'],
            presale['opportunity_number'],
            presale['assigned_to'],
            presale['description'],
        ])
    writer.writerow([])

    writer.writerow(['Source-wise Leads'])
    writer.writerow(['Source', 'Count'])
    for s in report_data['source_wise']:
        writer.writerow([s['source'], s['count']])

    return response


def _build_expense_report(start_date=None, end_date=None):
    report = {
        'total_expenses': Decimal('0.00'),
        'category_wise': [],
        'monthly_trend': [],
        'highest_expenses': [],
        'expenses': [],
        'approved_count': 0,
        'pending_count': 0,
    }

    try:
        from customer.models import Customer
        from expenses.models import Expense, ExpenseLine

        qs = safe_date_filter(Expense.objects.all(), 'date', start_date, end_date)
        expenses = list(
            qs.select_related('vendor', 'paid_through')
            .prefetch_related('lines__account')
            .order_by('-date', '-id')
        )
        report['total_expenses'] = sum((Decimal(str(getattr(expense, 'total_amount', Decimal('0.00')) or 0)) for expense in expenses), Decimal('0.00'))

        customer_ids = {
            int(str(expense.customer).strip())
            for expense in expenses
            if str(getattr(expense, 'customer', '') or '').strip().isdigit()
        }
        customer_names = {
            customer.pk: _customer_display_name(customer)
            for customer in Customer.objects.using(qs.db).filter(pk__in=customer_ids)
        }

        for expense in expenses:
            expense_accounts = []
            for line in expense.lines.all():
                account_name = getattr(getattr(line, 'account', None), 'name', None)
                if account_name and account_name not in expense_accounts:
                    expense_accounts.append(account_name)
            customer_value = str(getattr(expense, 'customer', '') or '').strip()
            customer_name = (
                customer_names.get(int(customer_value), customer_value)
                if customer_value.isdigit()
                else customer_value
            )

            report['expenses'].append({
                'id': getattr(expense, 'id', None),
                'date': getattr(expense, 'date', None),
                'vendor': getattr(expense, 'vendor', None) and str(expense.vendor) or 'Unknown',
                'customer': customer_name or 'N/A',
                'paid_through': getattr(getattr(expense, 'paid_through', None), 'name', None) or 'N/A',
                'expense_account': ', '.join(expense_accounts) or 'N/A',
                'invoice_number': getattr(expense, 'invoice_number', '') or 'N/A',
                'amount': getattr(expense, 'total_amount', Decimal('0.00')),
            })

        # Category-wise totals using the actual expense line totals, including tax contributions.
        line_qs = ExpenseLine.objects.filter(expense__in=qs).select_related('account')
        category_totals = {}
        for line in line_qs:
            name = getattr(line.account, 'name', None) or 'Unspecified'
            category_totals[name] = category_totals.get(name, Decimal('0.00')) + Decimal(str(line.total_amount or 0))
        for category, total in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)[:10]:
            report['category_wise'].append({
                'category': category,
                'total': total,
            })

        # Monthly trend using actual expense totals, not base-converted totals.
        monthly_totals = {}
        for expense in expenses:
            month = expense.date.strftime('%Y-%m') if expense.date else 'Unknown'
            monthly_totals[month] = monthly_totals.get(month, Decimal('0.00')) + Decimal(str(getattr(expense, 'total_amount', Decimal('0.00')) or 0))
        for month in sorted(monthly_totals):
            report['monthly_trend'].append({
                'month': month,
                'total': monthly_totals[month],
            })

        # Highest expense entries using actual totals.
        top_expenses = sorted(expenses, key=lambda e: Decimal(str(getattr(e, 'total_amount', Decimal('0.00')) or 0)), reverse=True)[:10]
        for e in top_expenses:
            customer_value = str(getattr(e, 'customer', '') or '').strip()
            customer_name = (
                customer_names.get(int(customer_value), customer_value)
                if customer_value.isdigit()
                else customer_value
            )
            report['highest_expenses'].append({
                'id': getattr(e, 'id', None),
                'date': getattr(e, 'date', None),
                'vendor': getattr(e, 'vendor', None) and str(e.vendor) or 'Unknown',
                'customer': customer_name or 'N/A',
                'amount': getattr(e, 'total_amount', Decimal('0.00')),
            })

        # Approved / pending counts if approval field exists
        try:
            if hasattr(Expense, 'approval_status'):
                report['approved_count'] = qs.filter(approval_status__iexact='APPROVED').count()
                report['pending_count'] = qs.filter(approval_status__iexact='PENDING').count()
            elif hasattr(Expense, 'status'):
                # status boolean: treat True as approved-ish
                report['approved_count'] = qs.filter(status=True).count()
                report['pending_count'] = qs.filter(status=False).count()
        except Exception:
            report['approved_count'] = 0
            report['pending_count'] = 0

    except Exception:
        pass

    return report


def expense_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_expense(request.user)):
        messages.error(request, 'You do not have permission to view Expense MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_expense_report(start_date, end_date)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    return render(request, 'mis_reports/expense_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def expense_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_expense_report(start_date, end_date)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_expense_report.csv"'
    writer = csv.writer(response)

    NUM_COLS = 7  # widest section (Expense List) — pad every row to this width

    def row(*cells):
        cells = list(cells) + [''] * (NUM_COLS - len(cells))
        writer.writerow(cells)

    def section_title(title):
        row()
        row(title.upper())
        row('-' * len(title))

    # Header
    row('EXPENSE MIS REPORT')
    row('=' * 18)
    row('Period', period)
    row('Start Date', start_date.strftime('%d-%m-%Y') if start_date else 'N/A')
    row('End Date', end_date.strftime('%d-%m-%Y') if end_date else 'N/A')
    row('Total Expenses', f'{report_data["total_expenses"]:.2f}')

    # Expense List
    section_title('Expense List')
    row('Date', 'Vendor', 'Customer', 'Paid Through', 'Expense Account', 'Invoice', 'Amount')
    for e in report_data['expenses']:
        row(
            e['date'].strftime('%d-%m-%Y') if e['date'] else '',
            e['vendor'],
            e['customer'],
            e['paid_through'],
            e['expense_account'],
            e['invoice_number'],
            f"{_to_decimal(e['amount']):.2f}",
        )

    # Category-wise
    section_title('Category-wise Expenses')
    row('Category', 'Total')
    for c in report_data['category_wise']:
        row(c['category'], f"{c['total']:.2f}")

    # Monthly Trend
    section_title('Monthly Trend')
    row('Month', 'Total')
    for m in report_data['monthly_trend']:
        row(m['month'], f"{m['total']:.2f}")

    # Top Expenses
    section_title('Top Expenses')
    row('ID', 'Date', 'Vendor', 'Amount')
    for e in report_data['highest_expenses']:
        row(
            e['id'],
            e['date'].strftime('%d-%m-%Y') if e['date'] else '',
            e['vendor'],
            f"{_to_decimal(e['amount']):.2f}",
        )

    # Approval Summary
    # section_title('Approval Summary')
    # row('Approved Count', report_data['approved_count'])
    # row('Pending Count', report_data['pending_count'])

    return response


def _build_hr_report(start_date=None, end_date=None):
    report = {
        'total_employees': 0,
        'active_employees': 0,
        'inactive_employees': 0,
        'department_counts': [],
        'designation_counts': [],
        'employees': [],
        'leave_type_count': 0,
        'employees_with_leaves': 0,
    }

    try:
        from HR.models import Employee
        # department and designation models
        # Department: department.department_name
        # Designations: designation.designation_name

        qs = safe_date_filter(Employee.objects.all(), 'created_at__date', start_date, end_date)

        report['total_employees'] = qs.count()
        report['active_employees'] = qs.filter(status=True).count()
        report['inactive_employees'] = qs.filter(status=False).count()

        for employee in qs.select_related('department', 'designation').order_by('first_name', 'last_name', 'emp_code'):
            report['employees'].append({
                'emp_code': getattr(employee, 'emp_code', '') or 'N/A',
                'name': str(employee) or 'N/A',
                'email': getattr(employee, 'email', '') or 'N/A',
                'phone': getattr(employee, 'phone', '') or 'N/A',
                'department': getattr(getattr(employee, 'department', None), 'department_name', None) or 'N/A',
                'designation': getattr(getattr(employee, 'designation', None), 'designation_name', None) or 'N/A',
                'joining_date': getattr(employee, 'joining_date', None),
                'status': 'Active' if getattr(employee, 'status', False) else 'Inactive',
            })

        # Department-wise counts
        dept_rows = qs.values('department__department_name').annotate(count=Count('pk')).order_by('-count')
        for row in dept_rows:
            report['department_counts'].append({
                'department': row.get('department__department_name') or 'Unknown',
                'count': row.get('count', 0),
            })

        # Designation-wise counts
        desig_rows = qs.values('designation__designation_name').annotate(count=Count('pk')).order_by('-count')
        for row in desig_rows:
            report['designation_counts'].append({
                'designation': row.get('designation__designation_name') or 'Unknown',
                'count': row.get('count', 0),
            })

        # Leaves: count leave types and employees linked to any leave (many-to-many)
        try:
            from leaves.models import Leaves
            report['leave_type_count'] = Leaves.objects.count()
            report['employees_with_leaves'] = qs.filter(leaves__isnull=False).distinct().count()
        except Exception:
            report['leave_type_count'] = 0
            report['employees_with_leaves'] = 0

    except Exception:
        pass

    return report


def hr_report(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_view_hr(request.user)):
        messages.error(request, 'You do not have permission to view HR MIS report.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_hr_report(start_date, end_date)
    export_query_string = build_export_query_string({
        'period': period,
        'start_date': start_date.strftime('%Y-%m-%d') if start_date else '',
        'end_date': end_date.strftime('%Y-%m-%d') if end_date else '',
    })

    return render(request, 'mis_reports/hr_report.html', {
        'company_code': company_code,
        'period': period,
        'start_date': start_date,
        'end_date': end_date,
        'report': report_data,
        'can_export': getattr(request.user, 'is_superuser', False) or can_export(request.user),
        'export_query_string': export_query_string,
    })


def hr_report_export_csv(request, company_code=None):
    if not (getattr(request.user, 'is_superuser', False) or can_export(request.user)):
        messages.error(request, 'You do not have permission to export MIS reports.')
        return redirect_with_company(request, 'mis_reports_dashboard')

    start_date, end_date, period = parse_date_range_from_request(request)
    report_data = _build_hr_report(start_date, end_date)

    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="mis_hr_report.csv"'
    writer = csv.writer(response)

    writer.writerow(['HR MIS Report'])
    writer.writerow(['Period', period])
    writer.writerow(['Start Date', start_date.strftime('%Y-%m-%d') if start_date else ''])
    writer.writerow(['End Date', end_date.strftime('%Y-%m-%d') if end_date else ''])
    writer.writerow(['Total Employees', report_data['total_employees']])
    writer.writerow(['Active Employees', report_data['active_employees']])
    writer.writerow(['Inactive Employees', report_data['inactive_employees']])
    writer.writerow([])

    writer.writerow(['Employees'])
    writer.writerow(['Employee Code', 'Name', 'Email', 'Phone', 'Department', 'Designation', 'Joining Date', 'Status'])
    for employee in report_data['employees']:
        writer.writerow([
            employee['emp_code'],
            employee['name'],
            employee['email'],
            employee['phone'],
            employee['department'],
            employee['designation'],
            employee['joining_date'] or '',
            employee['status'],
        ])
    writer.writerow([])

    writer.writerow(['Department', 'Count'])
    for d in report_data['department_counts']:
        writer.writerow([d['department'], d['count']])
    writer.writerow([])

    writer.writerow(['Designation', 'Count'])
    for d in report_data['designation_counts']:
        writer.writerow([d['designation'], d['count']])
    writer.writerow([])

    writer.writerow(['Leave Types', report_data['leave_type_count']])
    writer.writerow(['Employees With Leave Types', report_data['employees_with_leaves']])

    return response
