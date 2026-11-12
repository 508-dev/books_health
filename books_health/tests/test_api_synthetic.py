"""Exercise the API against synthetic documents on a disposable ERPNext site.

Enable site config books_health_synthetic_tests=1 explicitly. Fixtures are
ordinary ERP documents, rolled back after each test. No customer export is used.
"""

from collections import Counter
from datetime import date, datetime, timedelta
import json
import re
import unittest
import uuid
from unittest.mock import patch

import frappe
from frappe.utils import add_days, get_datetime, getdate, today

from books_health import api, checks


def deny_synthetic_invoice(doc, ptype, **kwargs):
    """A real Frappe permission-hook target used by the disposable-site test."""
    if ptype == "read" and doc.name == frappe.flags.get("books_health_denied_invoice"):
        return False
    return None


class TestSyntheticHealthAPI(unittest.TestCase):
    def setUp(self):
        if not frappe.conf.get("books_health_synthetic_tests"):
            self.skipTest(
                "Requires an explicitly enabled disposable synthetic-data site"
            )
        self.original_user = frappe.session.user
        frappe.set_user("Administrator")
        self.savepoint = "books_health_synthetic"
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(self.cleanup_fixtures)
        self.scope = api.authorized_scope()
        self.company = frappe.get_doc("Company", self.scope["company"])
        self.marker = f"Books Health Synthetic {uuid.uuid4().hex[:10]}"
        self.old_date = getdate(add_days(today(), -60))
        self.ensure_fiscal_year(self.old_date)
        self.customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": self.marker,
                "customer_type": "Company",
                "customer_group": frappe.db.get_value(
                    "Customer Group", {"is_group": 0}, "name"
                )
                or "All Customer Groups",
                "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")
                or "All Territories",
            }
        ).insert()
        # No email address is set, so invoice hooks cannot send to a recipient.
        self.item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": self.marker,
                "item_name": "Synthetic accounting test service",
                "item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name")
                or "All Item Groups",
                "stock_uom": "Nos"
                if frappe.db.exists("UOM", "Nos")
                else frappe.db.get_value("UOM", {}, "name"),
                "is_stock_item": 0,
            }
        ).insert()
        self.overdue = self.invoice(submit=True)
        self.drafts = [self.invoice() for _ in range(2)]
        for draft in self.drafts:
            frappe.db.set_value(
                "Sales Invoice",
                draft.name,
                "creation",
                get_datetime(add_days(today(), -20)),
                update_modified=False,
            )

    def cleanup_fixtures(self):
        frappe.set_user("Administrator")
        frappe.db.rollback(save_point=self.savepoint)
        frappe.clear_cache()
        frappe.set_user(self.original_user)

    def ensure_fiscal_year(self, posting_date):
        if frappe.db.exists(
            "Fiscal Year",
            {
                "year_start_date": ["<=", posting_date],
                "year_end_date": [">=", posting_date],
                "disabled": 0,
            },
        ):
            return
        year = posting_date.year
        frappe.get_doc(
            {
                "doctype": "Fiscal Year",
                "year": f"Synthetic Health {year}",
                "year_start_date": date(year, 1, 1),
                "year_end_date": date(year, 12, 31),
                "companies": [{"company": self.company.name}],
            }
        ).insert()

    def invoice(self, submit=False):
        income = self.company.default_income_account or frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "is_group": 0,
                "root_type": "Income",
            },
            "name",
        )
        receivable = self.company.default_receivable_account or frappe.db.get_value(
            "Account",
            {
                "company": self.company.name,
                "is_group": 0,
                "account_type": "Receivable",
            },
            "name",
        )
        cost_center = self.company.cost_center or frappe.db.get_value(
            "Cost Center",
            {
                "company": self.company.name,
                "is_group": 0,
            },
            "name",
        )
        self.assertTrue(
            income and receivable and cost_center,
            "The synthetic company needs a standard chart of accounts",
        )
        doc = frappe.get_doc(
            {
                "doctype": "Sales Invoice",
                "company": self.company.name,
                "customer": self.customer.name,
                "posting_date": self.old_date,
                "set_posting_time": 1,
                "due_date": add_days(self.old_date, 30),
                "currency": "USD",
                "conversion_rate": 1,
                "debit_to": receivable,
                "items": [
                    {
                        "item_code": self.item.name,
                        "qty": 1,
                        "rate": 100,
                        "income_account": income,
                        "cost_center": cost_center,
                    }
                ],
            }
        ).insert()
        if submit:
            doc.submit()
        return doc

    def test_seeded_scan_finds_overdue_and_stale_documents(self):
        result = api.get_books_health(self.company.name, include_info=True, limit=200)
        self.assertEqual(result["status"], "complete", result["failed_checks"])
        self.assertEqual(result["currency"], "USD")
        findings = {(row["check_id"], row["name"]): row for row in result["findings"]}
        overdue = findings[("overdue_invoices", self.overdue.name)]
        self.assertEqual(overdue["amount"], 100)
        self.assertEqual(overdue["party"], self.customer.name)
        self.assertEqual(overdue["severity"], "review")
        for draft in self.drafts:
            self.assertIn(("stale_drafts", draft.name), findings)

    def test_real_findings_paginate_without_duplicates(self):
        first = api.get_books_health(self.company.name, limit=1)
        self.assertGreaterEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        pages = [
            api.get_books_health(self.company.name, limit=1, offset=offset)
            for offset in range(first["total"])
        ]
        whole = api.get_books_health(self.company.name, limit=200)
        self.assertEqual(whole["total"], len(whole["findings"]))
        rows = [page["findings"][0] for page in pages]
        self.assertEqual(
            Counter(json.dumps(row, sort_keys=True, default=str) for row in rows),
            Counter(
                json.dumps(row, sort_keys=True, default=str)
                for row in whole["findings"]
            ),
        )
        self.assertIn(
            ("overdue_invoices", self.overdue.name),
            [(row["check_id"], row["name"]) for row in rows],
        )
        self.assertFalse(pages[-1]["has_more"])

    def test_multiple_referral_findings_for_one_invoice_survive_pagination(self):
        project = frappe.get_doc(
            {
                "doctype": "Project",
                "project_name": f"{self.marker} Referrals",
                "company": self.company.name,
                "customer": self.customer.name,
            }
        ).insert()
        invoice = self.invoice()
        invoice.project = project.name
        for item in invoice.items:
            item.project = project.name
        invoice.save()
        invoice.submit()
        # Referrers were added after submission, so no automated referral bills
        # exist yet. Two genuine findings share the same check and document.
        field = next(
            df.fieldname
            for df in project.meta.fields
            if df.options == "Project Referrer"
        )
        suppliers = []
        for index in (1, 2):
            supplier = frappe.get_doc(
                {
                    "doctype": "Supplier",
                    "supplier_name": f"{self.marker} Referrer {index}",
                    "supplier_type": "Company",
                    "country": self.company.country,
                    "supplier_group": frappe.db.get_value(
                        "Supplier Group", {"is_group": 0}, "name"
                    )
                    or "All Supplier Groups",
                }
            ).insert()
            suppliers.append(supplier.name)
            project.append(field, {"supplier": supplier.name, "percentage": index})
        project.save()
        whole = api.get_books_health(self.company.name, limit=200)
        duplicate_keys = [
            row
            for row in whole["findings"]
            if row["check_id"] == "referral_fees" and row["name"] == invoice.name
        ]
        self.assertCountEqual(
            [(row["party"], row["amount"]) for row in duplicate_keys],
            [(suppliers[0], 1), (suppliers[1], 2)],
        )
        pages = [
            api.get_books_health(self.company.name, limit=1, offset=offset)["findings"][
                0
            ]
            for offset in range(whole["total"])
        ]
        self.assertEqual(
            Counter(
                json.dumps(row, sort_keys=True, default=str)
                for row in whole["findings"]
            ),
            Counter(json.dumps(row, sort_keys=True, default=str) for row in pages),
        )

    def test_successful_read_has_no_database_or_email_effects(self):
        sources = (*api.SOURCE_DOCTYPES, "Error Log", "Email Queue", "Communication")
        before = {doctype: frappe.db.count(doctype) for doctype in sources}
        invoice_before = frappe.get_doc("Sales Invoice", self.overdue.name).as_dict()
        with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as execute:
            result = api.get_books_health(self.company.name)
        writes = [
            str(call.args[0] if call.args else call.kwargs.get("query", ""))
            for call in execute.call_args_list
            if re.match(
                r"\s*(insert|update|delete|replace|alter|drop|create|truncate)\b",
                str(call.args[0] if call.args else call.kwargs.get("query", "")),
                re.I,
            )
        ]
        self.assertEqual(writes, [], "A health read must not issue SQL mutations")
        self.assertEqual(result["status"], "complete", result["failed_checks"])
        self.assertEqual(
            before, {doctype: frappe.db.count(doctype) for doctype in sources}
        )
        self.assertEqual(
            invoice_before, frappe.get_doc("Sales Invoice", self.overdue.name).as_dict()
        )

    def test_failed_read_check_is_partial_without_error_log_writes(self):
        def broken():
            raise RuntimeError("Synthetic private failure detail")

        before = {
            doctype: frappe.db.count(doctype)
            for doctype in ("Error Log", "Email Queue", "GL Entry")
        }
        with (
            patch.object(checks, "CHECKS", [broken]),
            patch.object(checks, "CHECK_IDS", {broken: "synthetic_check"}),
        ):
            result = api.get_books_health(self.company.name)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failed_checks"], ["synthetic_check"])
        self.assertEqual(result["findings"][0]["error_code"], "check_failed")
        self.assertNotIn(
            "Synthetic private failure detail", result["findings"][0]["detail"]
        )
        self.assertEqual(
            before, {doctype: frappe.db.count(doctype) for doctype in before}
        )

    def test_plain_accounts_manager_cannot_read_deleted_history(self):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": f"health-{uuid.uuid4().hex}@example.invalid",
                "first_name": "Synthetic Finance Reader",
                "send_welcome_email": 0,
                "roles": [{"role": "Accounts Manager"}],
            }
        ).insert()
        frappe.set_user(user.name)
        self.assertFalse(frappe.has_permission("Deleted Document", "read"))
        with self.assertRaises(frappe.PermissionError):
            api.get_books_health(self.company.name)
        self.assertEqual(frappe.response["error_code"], "forbidden")

    def test_record_hook_denial_is_not_hidden_by_equal_list_counts(self):
        user = self.finance_user()
        original_get_hooks = frappe.get_hooks

        def get_hooks(hook=None, *args, **kwargs):
            hooks = original_get_hooks(hook, *args, **kwargs)
            if hook == "has_permission":
                return {
                    **hooks,
                    "Sales Invoice": [
                        *hooks.get("Sales Invoice", []),
                        "books_health.tests.test_api_synthetic.deny_synthetic_invoice",
                    ],
                }
            return hooks

        frappe.flags.books_health_denied_invoice = self.overdue.name
        self.addCleanup(frappe.flags.pop, "books_health_denied_invoice", None)
        frappe.set_user(user.name)
        with patch.object(frappe, "get_hooks", side_effect=get_hooks):
            self.assertFalse(
                frappe.has_permission("Sales Invoice", "read", doc=self.overdue.name)
            )
            self.assertTrue(
                frappe.has_permission("Sales Invoice", "read", doc=self.drafts[0].name)
            )
            visible = frappe.get_list(
                "Sales Invoice",
                fields=["count(name) as total"],
                order_by="",
                limit_page_length=1,
            )
            self.assertEqual(visible[0].total, frappe.db.count("Sales Invoice"))
            with patch.object(checks, "run_all") as scan:
                with self.assertRaises(frappe.PermissionError):
                    api.get_books_health(self.company.name)
                scan.assert_not_called()
            self.assertEqual(frappe.response["error_code"], "forbidden")

    def finance_user(self):
        return frappe.get_doc(
            {
                "doctype": "User",
                "email": f"health-{uuid.uuid4().hex}@example.invalid",
                "first_name": "Synthetic Finance Reader",
                "send_welcome_email": 0,
                "roles": [{"role": "Accounts Manager"}],
            }
        ).insert()

    def test_hidden_outstanding_amount_is_denied_even_with_document_read(self):
        user = self.finance_user()
        frappe.get_doc(
            {
                "doctype": "Property Setter",
                "doctype_or_field": "DocField",
                "doc_type": "Sales Invoice",
                "field_name": "outstanding_amount",
                "property": "permlevel",
                "property_type": "Int",
                "value": "9",
            }
        ).insert()
        frappe.clear_cache(doctype="Sales Invoice")
        frappe.set_user(user.name)
        self.assertTrue(
            frappe.has_permission("Sales Invoice", "read", doc=self.overdue.name)
        )
        permitted = frappe.get_meta("Sales Invoice").get_permitted_fieldnames(
            user=user.name,
            permission_type="read",
            with_virtual_fields=False,
        )
        self.assertNotIn("outstanding_amount", permitted)
        with self.assertRaises(frappe.PermissionError):
            api.require_field_access("Sales Invoice")
        with patch.object(checks, "run_all") as scan:
            with self.assertRaises(frappe.PermissionError):
                api.get_books_health(self.company.name)
            scan.assert_not_called()
        self.assertEqual(frappe.response["error_code"], "forbidden")

    def test_generated_at_is_timezone_aware_utc(self):
        result = api.get_books_health(self.company.name)
        timestamp = datetime.fromisoformat(result["generated_at"])
        self.assertEqual(timestamp.utcoffset(), timedelta(0))

    def test_actual_foreign_currency_document_is_rejected(self):
        draft = frappe.get_doc("Sales Invoice", self.drafts[0].name)
        draft.currency = "EUR"
        draft.conversion_rate = 1.2
        draft.save()
        with self.assertRaises(frappe.ValidationError):
            api.get_books_health(self.company.name)
        self.assertEqual(frappe.response["error_code"], "unsupported_scope")
