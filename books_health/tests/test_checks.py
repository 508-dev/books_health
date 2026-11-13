import unittest

import frappe

from books_health import checks, digest


class TestChecksRun(unittest.TestCase):
    """Runs every check against the site's real data and validates the shape of what comes back."""

    def test_every_check_returns_well_formed_findings(self):
        for check in checks.CHECKS:
            with self.subTest(check=check.__name__):
                for f in check():
                    self.assertIn(f.severity, checks.SEVERITY_ORDER)
                    self.assertTrue(f.check)
                    self.assertTrue(f.detail)
                    if f.name:
                        self.assertTrue(frappe.db.exists(f.doctype, f.name), f"{f.doctype} {f.name} doesn't exist")

    def test_run_all_hides_info_unless_asked(self):
        self.assertFalse([f for f in checks.run_all() if f.severity == checks.INFO])

    def test_run_all_sorts_by_severity(self):
        order = [checks.SEVERITY_ORDER[f.severity] for f in checks.run_all(include_info=True)]
        self.assertEqual(order, sorted(order))

    def test_a_failing_check_is_reported_not_raised(self):
        def broken():
            raise RuntimeError("intentional failure")

        original = checks.CHECKS
        checks.CHECKS = [broken]
        try:
            findings = checks.run_all()
        finally:
            checks.CHECKS = original
        self.assertEqual([f.check for f in findings], ["Health check error"])


class TestDigest(unittest.TestCase):
    def test_builds_subject_and_message(self):
        subject, message = digest.build_digest()
        self.assertTrue(subject.startswith("Books health"))
        self.assertIn("Books Health report", message)

    def test_no_recipients_without_config(self):
        original = frappe.conf.get("books_health_recipients")
        frappe.conf.books_health_recipients = None
        try:
            self.assertEqual(digest.recipients(), [])
        finally:
            frappe.conf.books_health_recipients = original


class TestMoney(unittest.TestCase):
    def test_formats_negative_and_positive(self):
        self.assertEqual(checks.money(-1234.5), "-$1,234.50")
        self.assertEqual(checks.money(5000), "$5,000.00")
        self.assertEqual(checks.money(None), "$0.00")
