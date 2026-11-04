from django import forms

from leaves.models import Leaves

from .models import Employee, LeaveRequest, Shift


class LeaveRequestForm(forms.ModelForm):
    """Apply for leave. Pass ``fixed_employee`` to lock the form to one employee
    (self-service); otherwise any active employee can be chosen (HR)."""

    class Meta:
        model = LeaveRequest
        fields = ['employee', 'leave_type', 'from_date', 'to_date', 'is_half_day', 'reason']
        widgets = {
            'from_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'to_date': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'reason': forms.Textarea(attrs={'rows': 3}),
        }
        labels = {'is_half_day': 'Half day'}

    def __init__(self, *args, fixed_employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['employee'].queryset = Employee.objects.filter(status=True).order_by('first_name', 'last_name')
        self.fields['employee'].label_from_instance = lambda e: f"{e.emp_code or ''} - {e.first_name} {e.last_name}".strip(' -')
        self.fields['leave_type'].queryset = Leaves.objects.filter(status=True).order_by('leaves_name')
        self.fields['leave_type'].empty_label = 'Select leave type'
        self.fields['reason'].required = False
        if fixed_employee is not None:
            self.fields['employee'].initial = fixed_employee.pk
            self.fields['employee'].disabled = True
        for name, field in self.fields.items():
            css = 'form-check-input' if isinstance(field.widget, forms.CheckboxInput) else 'form-control'
            if isinstance(field.widget, forms.Select):
                css = 'form-select'
            field.widget.attrs.setdefault('class', css)


class ShiftForm(forms.ModelForm):
    class Meta:
        model = Shift
        fields = ['name', 'start_time', 'end_time']
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'end_time': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        # Case-insensitive; includes removed shifts because the name is unique in the database.
        if Shift.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('A shift with this name already exists.')
        return name
