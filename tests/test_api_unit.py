"""Contract tests runnable without a Bench installation.

The Frappe boundary is replaced, while the app's real API/check registry run.
Database and framework permission behavior still require the Bench suite.
"""

import importlib
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]


class Row(dict):
    def __getattr__(self, name):
        return self.get(name)

    def __setattr__(self, name, value):
        self[name] = value


class ValidationError(Exception):
    pass


class PermissionError(Exception):
    pass


class TestHealthAPI(unittest.TestCase):
    def setUp(self):
        frappe = ModuleType("frappe")
        frappe._dict = Row
        frappe.ValidationError = ValidationError
        frappe.PermissionError = PermissionError
        frappe.response = {}
        frappe.session = SimpleNamespace(user="accountant@example.test")
        frappe.conf = Row()
        frappe.get_roles = MagicMock(return_value=["Accounts Manager"])
        frappe.has_permission = MagicMock(return_value=True)
        frappe.get_all = MagicMock(
            return_value=[Row(name="Co", default_currency="USD")]
        )
        frappe.get_list = MagicMock(return_value=[Row(total=2)])
        frappe.get_meta = MagicMock(
            return_value=SimpleNamespace(has_field=lambda field: True)
        )
        frappe.db = SimpleNamespace(
            count=MagicMock(return_value=2), exists=MagicMock(return_value=False)
        )
        frappe.log_error = MagicMock()
        frappe.sendmail = MagicMock()
        frappe.only_for = MagicMock()

        def throw(message, exc):
            raise exc(message)

        def whitelist(**kwargs):
            def decorate(fn):
                fn.allowed_methods = kwargs.get("methods")
                return fn

            return decorate

        frappe.throw = throw
        frappe.whitelist = whitelist
        utils = ModuleType("frappe.utils")
        for name in (
            "add_days",
            "date_diff",
            "getdate",
            "today",
            "get_url",
            "get_url_to_form",
        ):
            setattr(utils, name, MagicMock())
        utils.flt = lambda value, precision=None: (
            round(float(value or 0), precision) if precision else float(value or 0)
        )
        utils.now_datetime = lambda: datetime(2026, 11, 13, 7, 0)
        package = ModuleType("books_health")
        package.__path__ = [str(ROOT / "books_health")]
        self.modules = patch.dict(
            sys.modules,
            {
                "frappe": frappe,
                "frappe.utils": utils,
                "books_health": package,
            },
        )
        self.modules.start()
        for name in ("books_health.api", "books_health.checks", "books_health.digest"):
            sys.modules.pop(name, None)
        self.addCleanup(self.modules.stop)
        self.frappe = frappe
        self.api = importlib.import_module("books_health.api")
        self.checks = importlib.import_module("books_health.checks")
        self.scan_patch = patch.object(self.checks, "run_all", return_value=[])
        self.scan = self.scan_patch.start()
        self.addCleanup(self.scan_patch.stop)

    def assert_error(self, code, exception=ValidationError, **kwargs):
        with self.assertRaises(exception):
            self.api.get_books_health(**{"company": "Co", **kwargs})
        self.assertEqual(self.frappe.response["error_code"], code)
        self.scan.assert_not_called()

    def test_get_only_and_explicit_company(self):
        self.assertEqual(self.api.get_books_health.allowed_methods, ["GET"])
        self.assert_error("invalid_argument", company="")

    def test_guest_cannot_scan_or_enumerate_companies(self):
        self.frappe.session.user = "Guest"
        self.assert_error("forbidden", PermissionError)
        self.frappe.get_all.assert_not_called()

    def test_non_accounting_user_cannot_scan(self):
        self.frappe.get_roles.return_value = ["Employee"]
        self.assert_error("forbidden", PermissionError)

    def test_multiple_companies_are_rejected_before_scanning(self):
        self.frappe.get_all.return_value = [
            Row(name="Co", default_currency="USD"),
            Row(name="Other", default_currency="USD"),
        ]
        self.assert_error("unsupported_scope")

    def test_foreign_company_is_not_available(self):
        self.assert_error("forbidden", PermissionError, company="Other")

    def test_non_usd_company_is_not_mislabelled(self):
        self.frappe.get_all.return_value = [Row(name="Co", default_currency="EUR")]
        self.assert_error("unsupported_scope")

    def test_foreign_currency_records_are_rejected(self):
        self.frappe.db.exists.side_effect = lambda doctype, filters: (
            doctype == "Payment Entry"
        )
        self.assert_error("unsupported_scope")

    def test_source_doctype_permission_is_required(self):
        self.frappe.has_permission.side_effect = lambda doctype, permission: (
            doctype != "Deleted Document"
        )
        self.assert_error("forbidden", PermissionError)

    def test_restricted_secondary_source_records_are_rejected(self):
        self.frappe.get_list.side_effect = lambda doctype, **kwargs: [
            Row(total=1 if doctype == "Supplier" else 2)
        ]
        self.assert_error("forbidden", PermissionError)

    def test_scope_checks_every_source_used_by_global_checks(self):
        self.assertEqual(
            self.api.authorized_scope("Co"), {"company": "Co", "currency": "USD"}
        )
        checked = {call.args[0] for call in self.frappe.has_permission.call_args_list}
        self.assertEqual(checked, set(self.api.SOURCE_DOCTYPES))
        self.assertIn("Deleted Document", checked)
        self.assertIn("Payment Ledger Entry", checked)

    def test_invalid_page_arguments_never_scan(self):
        for kwargs in (
            {"limit": 0},
            {"limit": 201},
            {"limit": True},
            {"limit": "1.5"},
            {"offset": -1},
            {"offset": "1e2"},
            {"offset": 100001},
            {"offset": "9" * 10000},
        ):
            with self.subTest(
                kwargs={key: str(value)[:30] for key, value in kwargs.items()}
            ):
                self.assert_error("invalid_argument", **kwargs)

    def test_string_false_does_not_include_info(self):
        result = self.api.get_books_health("Co", include_info="false")
        self.scan.assert_called_once_with(include_info=False, log_errors=False)
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["generated_at"], "2026-11-13T07:00:00")

    def test_nonboolean_info_flag_rejected(self):
        self.assert_error("invalid_argument", include_info=1.0)

    def test_pages_contain_stable_fields_and_total(self):
        self.scan.return_value = [
            Row(
                severity=self.checks.REVIEW,
                severity_code="review",
                check_id="overdue_invoices",
                doctype="Sales Invoice",
                name=f"INV-{i}",
                party="Client",
                amount=i,
                detail="Review invoice.",
            )
            for i in range(5)
        ]
        result = self.api.get_books_health("Co", limit="2", offset="1")
        self.assertEqual(
            [row["name"] for row in result["findings"]], ["INV-1", "INV-2"]
        )
        self.assertEqual(result["total"], 5)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["findings"][0]["severity"], "review")
        self.assertEqual(result["findings"][0]["check_id"], "overdue_invoices")

    def test_failure_outside_page_still_sets_partial_failure(self):
        self.scan.return_value = [
            Row(
                severity=self.checks.ACTION,
                severity_code="action",
                check_id=f"check_{i}",
                doctype="Error Log",
                name=None,
                party=None,
                amount=None,
                detail="Check failed.",
                error_code="check_failed" if i == 2 else None,
            )
            for i in range(3)
        ]
        result = self.api.get_books_health("Co", limit=1)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failed_checks"], ["check_2"])

    def test_manual_digest_requires_post_and_source_authorization(self):
        digest = importlib.import_module("books_health.digest")
        self.assertEqual(digest.send_digest_now.allowed_methods, ["POST"])
        self.frappe.has_permission.return_value = False
        with self.assertRaises(PermissionError):
            digest.send_digest_now()
        self.frappe.sendmail.assert_not_called()

    def test_check_ids_and_severity_codes_do_not_depend_on_prose(self):
        self.scan_patch.stop()

        def renamed_implementation():
            return [
                self.checks.finding(
                    self.checks.REVIEW,
                    "Translated display label",
                    "Sales Invoice",
                    "INV-1",
                    "Text",
                )
            ]

        with (
            patch.object(self.checks, "CHECKS", [renamed_implementation]),
            patch.object(
                self.checks, "CHECK_IDS", {renamed_implementation: "public_stable_id"}
            ),
        ):
            rows = self.checks.run_all()
        self.assertEqual(rows[0].check_id, "public_stable_id")
        self.assertEqual(rows[0].severity_code, "review")

    def test_failing_check_has_structured_code_and_stable_id(self):
        self.scan_patch.stop()

        def broken():
            raise RuntimeError("private diagnostic")

        with (
            patch.object(self.checks, "CHECKS", [broken]),
            patch.object(self.checks, "CHECK_IDS", {broken: "stable_check"}),
        ):
            rows = self.checks.run_all()
        self.assertEqual(rows[0].check_id, "stable_check")
        self.assertEqual(rows[0].error_code, "check_failed")
        self.assertNotIn("private diagnostic", rows[0].detail)

    def test_read_only_scan_does_not_write_error_logs(self):
        self.scan_patch.stop()

        def broken():
            raise RuntimeError("private diagnostic")

        with patch.object(self.checks, "CHECKS", [broken]):
            rows = self.checks.run_all(log_errors=False)
        self.frappe.log_error.assert_not_called()
        self.assertEqual(rows[0].error_code, "check_failed")


if __name__ == "__main__":
    unittest.main()
