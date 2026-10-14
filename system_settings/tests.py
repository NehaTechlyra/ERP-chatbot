from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from types import SimpleNamespace
from unittest.mock import Mock

from company.models import Company
from system_settings.views import _has_trial_module_access


class PrinterSettingsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='testuser', password='pass123')
        self.client = Client()
        self.client.login(username='testuser', password='pass123')
        self.company = Company.objects.create(name="Test Company", company_code="TEST-1", status=True)

    def test_default_print_paper_size_is_a4(self):
        self.assertEqual(self.company.print_paper_size, "A4")

    def test_company_can_store_print_paper_size(self):
        self.company.print_paper_size = "POS"
        self.company.save()
        self.company.refresh_from_db()

        self.assertEqual(self.company.print_paper_size, "POS")

    def test_printer_paper_size_saved_from_settings_page(self):
        url = reverse('settings_page', kwargs={'company_code': self.company.company_code})
        response = self.client.post(
            url,
            {
                'printer_paper_size': 'POS',
                'active_tab': 'other_settings_tab',
            },
        )
        self.company.refresh_from_db()

        self.assertEqual(self.company.print_paper_size, 'POS')
        self.assertEqual(response.status_code, 302)


class TrialModuleAccessTests(TestCase):
    def test_active_trial_without_valid_license_has_all_module_access(self):
        company = SimpleNamespace(trial_active=True, is_trial_expired=0)

        self.assertTrue(_has_trial_module_access(company, None))

    def test_expired_trial_does_not_have_trial_module_access(self):
        company = SimpleNamespace(trial_active=True, is_trial_expired=1)

        self.assertFalse(_has_trial_module_access(company, None))

    def test_valid_license_takes_precedence_over_trial_access(self):
        company = SimpleNamespace(trial_active=True, is_trial_expired=0)
        license_obj = Mock(is_active=True)
        license_obj.is_expired.return_value = False

        self.assertFalse(_has_trial_module_access(company, license_obj))
