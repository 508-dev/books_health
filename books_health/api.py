"""Read-only, versioned integration contract for Books Health.

The existing checks intentionally inspect the whole ledger. Until they support
scoped aggregates, the API fails closed for restricted readers or mixed scopes.
"""

from datetime import datetime, timezone
import json
import re

import frappe

from books_health import checks

READ_ROLES = ("Accounts Manager", "Accounts User", "System Manager")
SOURCE_DOCTYPES = (
    "Company",
    "Customer",
    "Supplier",
    "Project",
    "Timesheet",
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Payment Ledger Entry",
    "Journal Entry",
    "Bank Account",
    "Bank Transaction",
    "Account",
    "GL Entry",
    "Deleted Document",
)
# These child tables contribute SQL filters, amounts or linked-document details.
# Their field permissions inherit the specified parent's permission levels.
CHILD_SOURCES = (
    ("Sales Invoice Item", "Sales Invoice"),
    ("Sales Invoice Timesheet", "Sales Invoice"),
    ("Timesheet Detail", "Timesheet"),
    ("Project Referrer", "Project"),
    ("Bank Transaction Payments", "Bank Transaction"),
)
CURRENCY_FIELDS = {
    "Sales Invoice": ("currency",),
    "Purchase Invoice": ("currency",),
    "Payment Entry": ("paid_from_account_currency", "paid_to_account_currency"),
    "Payment Ledger Entry": ("account_currency",),
    "Account": ("account_currency",),
    "GL Entry": ("account_currency",),
    "Bank Transaction": ("currency",),
    "Timesheet": ("currency",),
    "Project": ("currency",),
}


def fail(code, message, permission=False):
    """Keep the stable error code in Frappe's normal HTTP error response."""
    frappe.response["error_code"] = code
    frappe.throw(
        message, frappe.PermissionError if permission else frappe.ValidationError
    )


def require_field_access(doctype, parenttype=None):
    """Require full source-field access before SQL can bypass field filtering.

    Deliberately authorize every persisted field rather than maintaining a
    fragile partial list of the fields used by checks and secondary aggregates.
    Administrator bypasses field restrictions in Frappe itself.
    """
    if frappe.session.user == "Administrator":
        return
    meta = frappe.get_meta(doctype)
    required = set(meta.get_fieldnames_with_value(with_virtual_fields=False))
    permitted = set(
        meta.get_permitted_fieldnames(
            parenttype=parenttype,
            user=frappe.session.user,
            permission_type="read",
            with_virtual_fields=False,
        )
    )
    if required - permitted:
        fail(
            "forbidden",
            "Books Health requires unrestricted read access to source fields.",
            permission=True,
        )
    if parenttype:
        parent = frappe.get_meta(parenttype)
        levels = set(
            parent.get_permlevel_access(
                permission_type="read", user=frappe.session.user
            )
        )
        # Table fields are omitted by get_fieldnames_with_value. Hiding the
        # parent table must also hide the child amounts read directly by SQL.
        for field in parent.fields:
            if (
                field.fieldtype in ("Table", "Table MultiSelect")
                and field.options == doctype
            ):
                if (field.permlevel or 0) not in levels:
                    fail(
                        "forbidden",
                        "Books Health requires unrestricted read access to source tables.",
                        permission=True,
                    )


def require_source_access():
    hooks = frappe.get_hooks("has_permission")
    sources = set(SOURCE_DOCTYPES) | {doctype for doctype, _parent in CHILD_SOURCES}
    # get_list applies query restrictions, but it does not evaluate per-document
    # has_permission hooks. Reject these configurations rather than leaking
    # denied records through a whole-ledger scan whose counts happen to match.
    if hooks.get("*") or any(hooks.get(doctype) for doctype in sources):
        fail(
            "forbidden",
            "Books Health does not support custom source permission hooks.",
            permission=True,
        )
    for doctype in SOURCE_DOCTYPES:
        require_field_access(doctype)
    for doctype, parenttype in CHILD_SOURCES:
        require_field_access(doctype, parenttype)


def authorized_scope(company=None):
    """Authorize every source before global checks can expose linked data/totals.

    Checking only the document named by a finding is insufficient: details can
    contain other documents, deleted records and party-wide aggregates.
    """
    if frappe.session.user == "Guest" or not set(frappe.get_roles()).intersection(
        READ_ROLES
    ):
        fail(
            "forbidden", "Books Health requires an accounting reader.", permission=True
        )
    if company is not None and (not isinstance(company, str) or not company.strip()):
        fail("invalid_argument", "company must be a nonempty Company name.")
    require_source_access()
    companies = frappe.get_all(
        "Company", fields=["name", "default_currency"], limit_page_length=2
    )
    if len(companies) != 1:
        fail(
            "unsupported_scope",
            "Books Health API currently requires a single-company site.",
        )
    sole = companies[0]
    if company is not None and company != sole.name:
        fail("forbidden", "The requested company is not available.", permission=True)
    if sole.default_currency != "USD":
        fail("unsupported_scope", "Books Health API currently supports USD books only.")

    for doctype in SOURCE_DOCTYPES:
        if not frappe.has_permission(doctype, "read"):
            fail(
                "forbidden",
                "Books Health requires unrestricted read access to its source records.",
                permission=True,
            )
        # get_list applies User Permissions, owner-only grants and document
        # shares. Count all sources, including records only used in aggregates.
        visible = frappe.get_list(
            doctype, fields=["count(name) as total"], order_by="", limit_page_length=1
        )
        if not visible or visible[0].total != frappe.db.count(doctype):
            fail(
                "forbidden",
                "Books Health requires unrestricted read access to its source records.",
                permission=True,
            )

    for doctype, fields in CURRENCY_FIELDS.items():
        meta = frappe.get_meta(doctype)
        for field in fields:
            if meta.has_field(field) and frappe.db.exists(
                doctype, {field: ["not in", ["", "USD"]]}
            ):
                fail(
                    "unsupported_scope",
                    "Books Health API cannot summarize foreign-currency records.",
                )
    return {"company": sole.name, "currency": "USD"}


def integer_argument(value, name, minimum, maximum):
    # HTTP query parameters arrive as strings; reject floats, booleans and
    # scientific notation rather than silently rounding or coercing them.
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or len(str(value)) > 6
        or not re.fullmatch(r"[0-9]+", str(value))
    ):
        fail(
            "invalid_argument",
            f"{name} must be an integer between {minimum} and {maximum}.",
        )
    value = int(value)
    if not minimum <= value <= maximum:
        fail(
            "invalid_argument",
            f"{name} must be an integer between {minimum} and {maximum}.",
        )
    return value


@frappe.whitelist(methods=["GET"])
def get_books_health(company, include_info=False, limit=50, offset=0):
    """Return bounded structured findings as the authenticated ERP user."""
    if not isinstance(company, str) or not company.strip():
        fail("invalid_argument", "company must be a nonempty Company name.")
    if not isinstance(include_info, (bool, int, str)) or include_info not in (
        True,
        False,
        0,
        1,
        "0",
        "1",
        "true",
        "false",
    ):
        fail("invalid_argument", "include_info must be a boolean.")
    include_info = include_info in (True, 1, "1", "true")
    limit = integer_argument(limit, "limit", 1, 200)
    offset = integer_argument(offset, "offset", 0, 100000)
    scope = authorized_scope(company)
    findings = checks.run_all(include_info=include_info, log_errors=False)
    # A deterministic secondary sort makes pages reproducible when severity
    # and check match; callers should refresh after underlying records change.
    failed_checks = sorted({row.check_id for row in findings if row.get("error_code")})
    rows = [
        {
            "check_id": row.check_id,
            "severity": row.severity_code,
            "doctype": row.doctype,
            "name": row.name,
            "party": row.party,
            "amount": row.amount,
            "currency": scope["currency"],
            "detail": row.detail,
            "error_code": row.get("error_code"),
        }
        for row in findings
    ]
    severity_order = {"action": 0, "review": 1, "info": 2}
    rows.sort(
        key=lambda row: (
            severity_order[row["severity"]],
            row["check_id"],
            row["doctype"] or "",
            row["name"] or "",
            json.dumps(row, sort_keys=True, default=str),
        )
    )
    return {
        "api_version": 1,
        **scope,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "partial_failure" if failed_checks else "complete",
        "failed_checks": failed_checks,
        "findings": rows[offset : offset + limit],
        "total": len(findings),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(findings),
    }
