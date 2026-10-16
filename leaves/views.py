from django.shortcuts import render, redirect
from Lyraerp.utils.redirect_utils import redirect_with_company, get_company_redirect_url
from django.urls import NoReverseMatch
from decimal import Decimal, InvalidOperation
from .models import Leaves


def _parse_days_allowed(raw):
    """Parse the annual quota from the form; blank or invalid input means 0 (no limit)."""
    try:
        value = Decimal((raw or '0').strip() or '0')
    except InvalidOperation:
        return Decimal('0')
    return value if value >= 0 else Decimal('0')


def leaves_list(request):
    items = Leaves.objects.filter(status=True).order_by('leaves_name')
    return render(request, 'leaves/list.html', {'items': items})


def leaves_create(request):
    next_target = request.GET.get('next') or request.POST.get('next')
    # require master Create permission
    from HR.permissions import check_master_access
    if not check_master_access(request.user, 'Create'):
        return redirect_with_company(request, '/')
    if request.method == 'POST':
        name = request.POST.get('leaves_name') or request.POST.get('leave_name')
        status = True if request.POST.get('status') == 'on' else False
        if name:
            Leaves.objects.create(
                leaves_name=name,
                days_allowed=_parse_days_allowed(request.POST.get('days_allowed')),
                status=status,
            )
            if next_target:
                if not next_target.startswith('#'):
                    next_target = '#' + next_target
                url = get_company_redirect_url(request, 'admin_dashboard') + next_target
                return redirect(url)
            return redirect_with_company(request, 'admin_dashboard')
    return render(request, 'leaves/create.html', {'next': next_target})


def leaves_deactivate(request, pk):
    try:
        obj = Leaves.objects.get(pk=pk)
        obj.status = False
        obj.save()
    except Leaves.DoesNotExist:
        pass
    return redirect_with_company(request, 'admin_dashboard')


def leaves_edit(request, pk):
    next_target = request.GET.get('next') or request.POST.get('next')
    try:
        obj = Leaves.objects.get(pk=pk)
    except Leaves.DoesNotExist:
        return redirect_with_company(request, 'admin_dashboard')

    if request.method == 'POST':
        name = request.POST.get('leaves_name') or request.POST.get('leave_name')
        status = True if request.POST.get('status') == 'on' else False
        if name:
            obj.leaves_name = name
            obj.days_allowed = _parse_days_allowed(request.POST.get('days_allowed'))
            obj.status = status
            obj.save()
            if next_target:
                if not next_target.startswith('#'):
                    next_target = '#' + next_target
                url = get_company_redirect_url(request, 'admin_dashboard') + next_target
                return redirect(url)
            return redirect_with_company(request, 'admin_dashboard')

    return render(request, 'leaves/create.html', {'obj': obj, 'next': next_target})
