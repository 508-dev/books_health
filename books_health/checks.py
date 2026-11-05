"""Health checks for the books.

Each check returns findings: a severity, the document to open, the party and amount involved, and a
plain-language explanation of what looks wrong and what to do about it. The checks only read data.

Most of them exist because of a real incident; the docstrings say which.
"""

import frappe
from frappe.utils import add_days, date_diff, flt, getdate, today

ACTION = "Action needed"  # money is probably misstated
REVIEW = "Review"  # worth a look; may be fine
INFO = "FYI"

SEVERITY_ORDER = {ACTION: 0, REVIEW: 1, INFO: 2}

HOURLY_SALES_ITEM = "Contract Engineering (Sales)"
MARGIN_TARGET_PCT = 5
MARGIN_TOLERANCE_PCT = 0.5  # projects priced at exactly 5% land a little either side
RESIDUAL = 2.0  # unapplied amounts below this are rounding residue to write off


def money(amount) -> str:
    amount = flt(amount, 2)
    return f"-${abs(amount):,.2f}" if amount < 0 else f"${amount:,.2f}"


def finding(severity, check, doctype, name, detail, party=None, amount=None):
    return frappe._dict(
        severity=severity, check=check, doctype=doctype, name=name, party=party, amount=amount, detail=detail
    )


def unapplied_payments(min_age_days=7):
    """Payments sitting unapplied to any invoice.

    A client's payments once sat unapplied for ten months because their invoices were never created.
    The credit made later invoices look prepaid, which led to a discount for an overpayment that never happened.
    """
    rows = frappe.db.sql(
        """
        select name, posting_date, payment_type, party_type, party, unallocated_amount
        from `tabPayment Entry`
        where docstatus = 1 and party_type in ('Customer', 'Supplier')
          and unallocated_amount > 0.009 and posting_date <= %s
        order by posting_date
        """,
        add_days(today(), -min_age_days),
        as_dict=True,
    )
    open_items = _open_invoice_totals()
    findings = []
    for pe in rows:
        verb = "received from" if pe.payment_type == "Receive" else "paid to"
        base = f"{money(pe.unallocated_amount)} {verb} {pe.party} on {pe.posting_date} isn't applied to any invoice."
        if flt(pe.unallocated_amount) < RESIDUAL:
            findings.append(
                finding(INFO, "Unapplied payments", "Payment Entry", pe.name, f"{base} Rounding residue: write it off.",
                        pe.party, pe.unallocated_amount)
            )
            continue
        open_total, open_count = open_items.get((pe.party_type, pe.party), (0, 0))
        if open_total:
            detail = (
                f"{base} {pe.party} has {money(open_total)} open across {open_count} invoice(s): apply it in "
                "Payment Reconciliation, or find out why it doesn't belong to them."
            )
            severity = ACTION
        else:
            detail = (
                f"{base} Nothing of theirs is open to apply it to, so an invoice may be missing, or it should "
                "be recorded as an advance or refund."
            )
            severity = REVIEW
        findings.append(finding(severity, "Unapplied payments", "Payment Entry", pe.name, detail, pe.party,
                                pe.unallocated_amount))
    return findings


def _open_invoice_totals():
    totals = {}
    for doctype, party_field, party_type in (
        ("Sales Invoice", "customer", "Customer"),
        ("Purchase Invoice", "supplier", "Supplier"),
    ):
        for row in frappe.db.sql(
            f"""
            select {party_field} party, sum(outstanding_amount) total, count(*) n
            from `tab{doctype}` where docstatus = 1 and outstanding_amount > 0.009
            group by {party_field}
            """,
            as_dict=True,
        ):
            totals[(party_type, row.party)] = (flt(row.total), row.n)
    return totals


def negative_cash_and_bank():
    """A cash or bank account below zero means something was booked to the wrong account.

    Bank payouts booked to Cash once left it deeply negative.
    """
    rows = frappe.db.sql(
        """
        select gle.account, sum(gle.debit - gle.credit) balance
        from `tabGL Entry` gle join `tabAccount` acc on acc.name = gle.account
        where gle.is_cancelled = 0 and acc.account_type in ('Cash', 'Bank')
        group by gle.account having balance < -0.005
        """,
        as_dict=True,
    )
    return [
        finding(
            ACTION, "Cash or bank below zero", "Account", row.account,
            f"{row.account} has a balance of {money(row.balance)}. A cash or bank account below zero means "
            "something was booked to the wrong account; check its most recent entries in the General Ledger.",
            amount=row.balance,
        )
        for row in rows
    ]


def _timesheet_billed_projects():
    """Projects whose client invoices are built from timesheets (fixed retainers are not)."""
    return frappe.db.sql_list(
        """
        select distinct si.project from `tabSales Invoice Timesheet` sit
        join `tabSales Invoice` si on si.name = sit.parent
        where si.docstatus = 1 and ifnull(si.project, '') != ''
        """
    )


def unbilled_timesheets(min_age_days=35, billing_window_days=60):
    """Billable hours that were never invoiced to the client.

    Hours can be paid to the engineer and never reach a client invoice. They can also be covered some
    other way (a prepayment, say); mark those rows non-billable with a comment. Some projects invoice
    hours without linking the timesheets; when invoices from the same period carry enough unlinked hours to explain the gap,
    the finding is a Review rather than an Action.
    """
    projects = _timesheet_billed_projects()
    if not projects:
        return []
    rows = frappe.db.sql(
        """
        select ts.name, ts.employee_name, td.project, sum(td.billing_hours) hours, sum(td.billing_amount) amount,
               min(td.from_time) first, max(td.to_time) last, p.customer
        from `tabTimesheet Detail` td
        join `tabTimesheet` ts on ts.name = td.parent
        left join `tabProject` p on p.name = td.project
        where ts.docstatus = 1 and td.is_billable = 1 and ifnull(td.sales_invoice, '') = ''
          and td.project in %(projects)s and td.to_time < %(cutoff)s
        group by ts.name, td.project
        having hours > 0
        order by first
        """,
        {"projects": projects, "cutoff": add_days(today(), -min_age_days)},
        as_dict=True,
    )
    findings = []
    for row in rows:
        what = (
            f"{flt(row.hours, 2):g}h ({money(row.amount)}) logged by {row.employee_name} on {row.project} between "
            f"{getdate(row.first)} and {getdate(row.last)} aren't linked to any client invoice."
        )
        unlinked = _invoices_with_unlinked_hours(
            row.project, getdate(row.first), add_days(getdate(row.last), billing_window_days)
        )
        if sum(flt(si.unlinked_hours) for si in unlinked) + 0.5 >= flt(row.hours):
            names = ", ".join(f"{si.name} ({flt(si.unlinked_hours, 2):g}h)" for si in unlinked)
            findings.append(finding(
                REVIEW, "Unbilled hours", "Timesheet", row.name,
                f"{what} They were probably billed without linking the timesheet on {names}: link them, or "
                "confirm they were billed.",
                row.customer, row.amount,
            ))
        else:
            findings.append(finding(
                ACTION, "Unbilled hours", "Timesheet", row.name,
                f"{what} No invoice from that period bills unlinked hours that could cover them, so they look "
                "unbilled. Invoice them, or mark the rows non-billable if they weren't meant to be charged.",
                row.customer, row.amount,
            ))
    return findings


def _invoices_with_unlinked_hours(project, since, until):
    """Invoices on the project whose hourly lines carry more hours than their linked timesheets."""
    return frappe.db.sql(
        """
        select si.name,
               (select sum(i.qty) from `tabSales Invoice Item` i
                 where i.parent = si.name and i.item_code = %(item)s)
             - (select coalesce(sum(t.billing_hours), 0) from `tabSales Invoice Timesheet` t
                 where t.parent = si.name) unlinked_hours
        from `tabSales Invoice` si
        where si.docstatus = 1 and si.project = %(project)s and si.posting_date between %(since)s and %(until)s
        having unlinked_hours >= 0.5
        order by si.posting_date
        """,
        {"item": HOURLY_SALES_ITEM, "project": project, "since": since, "until": until},
        as_dict=True,
    )


def hours_without_timesheets(lookback_days=120):
    """Invoiced hours that no timesheet backs up, on projects that bill from timesheets.

    Hours have been billed a second time on a separate invoice with no timesheet behind it.
    """
    projects = _timesheet_billed_projects()
    if not projects:
        return []
    rows = frappe.db.sql(
        """
        select si.name, si.customer, si.project, si.posting_date,
               (select sum(i.qty) from `tabSales Invoice Item` i
                 where i.parent = si.name and i.item_code = %(item)s) item_hours,
               (select coalesce(sum(t.billing_hours), 0) from `tabSales Invoice Timesheet` t
                 where t.parent = si.name) timesheet_hours,
               (select max(i.rate) from `tabSales Invoice Item` i
                 where i.parent = si.name and i.item_code = %(item)s) rate
        from `tabSales Invoice` si
        where si.docstatus = 1 and si.project in %(projects)s and si.posting_date >= %(since)s
        having item_hours > 1 and abs(item_hours - timesheet_hours) >= 0.5
        order by si.posting_date
        """,
        {"item": HOURLY_SALES_ITEM, "projects": projects, "since": add_days(today(), -lookback_days)},
        as_dict=True,
    )
    return [
        finding(
            REVIEW, "Hours without timesheets", "Sales Invoice", row.name,
            f"Bills {flt(row.item_hours, 2):g}h but only {flt(row.timesheet_hours, 2):g}h are backed by timesheets. "
            "Check the difference isn't billed twice or missing from a timesheet.",
            row.customer, (flt(row.item_hours) - flt(row.timesheet_hours)) * flt(row.rate),
        )
        for row in rows
    ]


def referral_fees(lookback_days=60):
    """Every invoice on a project with referrers should have one referral bill per referrer.

    A referral bill that was deleted by hand (rather than by cancelling the invoice) is listed as waived,
    so deliberate decisions stay visible without being flagged as errors.
    """
    invoices = frappe.db.sql(
        """
        select si.name, si.project, si.grand_total, si.posting_date
        from `tabSales Invoice` si
        where si.docstatus = 1 and si.posting_date >= %s
          and si.project in (select parent from `tabProject Referrer`)
        order by si.posting_date
        """,
        add_days(today(), -lookback_days),
        as_dict=True,
    )
    findings = []
    for si in invoices:
        referrers = frappe.get_all(
            "Project Referrer", filters={"parent": si.project, "parenttype": "Project"},
            fields=["supplier", "percentage"],
        )
        actual = {
            row.supplier: row
            for row in frappe.get_all(
                "Purchase Invoice",
                filters={"referral_source_si": si.name, "docstatus": ["!=", 2]},
                fields=["name", "supplier", "grand_total", "docstatus"],
            )
        }
        waived = _deleted_referral_bills(si.name)
        for ref in referrers:
            if not ref.supplier or not flt(ref.percentage):
                continue
            expected = flt(flt(si.grand_total) * flt(ref.percentage) / 100, 2)
            bill = actual.get(ref.supplier)
            if bill and abs(flt(bill.grand_total) - expected) > 0.05:
                findings.append(finding(
                    REVIEW, "Referral fees", "Purchase Invoice", bill.name,
                    f"Referral bill for {si.name} is {money(bill.grand_total)}, but {flt(ref.percentage):g}% of "
                    f"{money(si.grand_total)} is {money(expected)}.",
                    ref.supplier, flt(bill.grand_total) - expected,
                ))
            elif not bill and ref.supplier in waived:
                deleted = waived[ref.supplier]
                findings.append(finding(
                    INFO, "Referral fees", "Sales Invoice", si.name,
                    f"{ref.supplier}'s referral bill ({money(expected)}) was deleted by {deleted.owner} on "
                    f"{getdate(deleted.creation)}; treated as waived.",
                    ref.supplier, expected,
                ))
            elif not bill:
                findings.append(finding(
                    ACTION, "Referral fees", "Sales Invoice", si.name,
                    f"No referral bill for {ref.supplier}: {flt(ref.percentage):g}% of {money(si.grand_total)} "
                    f"= {money(expected)}. Create it, or delete the draft deliberately if the fee is waived.",
                    ref.supplier, expected,
                ))
    return findings


def _deleted_referral_bills(sales_invoice):
    rows = frappe.db.sql(
        """
        select owner, creation, json_value(data, '$.supplier') supplier from `tabDeleted Document`
        where deleted_doctype = 'Purchase Invoice' and json_value(data, '$.referral_source_si') = %s
        """,
        sales_invoice,
        as_dict=True,
    )
    return {row.supplier: row for row in rows}


def stale_drafts(min_age_days=14):
    """Drafts nobody came back to. They don't touch the ledger, which is exactly why they get forgotten."""
    sources = (
        ("Sales Invoice", "grand_total", "customer"),
        ("Purchase Invoice", "grand_total", "supplier"),
        ("Payment Entry", "paid_amount", "party"),
        ("Journal Entry", "total_debit", "null"),
    )
    findings = []
    for doctype, amount_field, party_field in sources:
        for row in frappe.db.sql(
            f"""
            select name, owner, creation, {amount_field} amount, {party_field} party
            from `tab{doctype}` where docstatus = 0 and creation < %s order by creation
            """,
            add_days(today(), -min_age_days),
            as_dict=True,
        ):
            findings.append(finding(
                REVIEW, "Stale drafts", doctype, row.name,
                f"Draft since {getdate(row.creation)}, created by {row.owner}. Submit it or delete it.",
                row.party, row.amount,
            ))
    return findings


def bank_backlog(min_age_days=14):
    """Bank lines nobody has reconciled. Plaid imports them hourly; they should not pile up."""
    rows = frappe.db.sql(
        """
        select bank_account, count(*) n, sum(deposit) deposits, sum(withdrawal) withdrawals, min(date) oldest
        from `tabBank Transaction`
        where docstatus = 1 and unallocated_amount > 0.009 and date < %s
        group by bank_account order by oldest
        """,
        add_days(today(), -min_age_days),
        as_dict=True,
    )
    return [
        finding(
            REVIEW, "Bank backlog", "Bank Account", row.bank_account,
            f"{row.n} bank line(s) older than {min_age_days} days aren't reconciled ({money(row.deposits)} in, "
            f"{money(row.withdrawals)} out, oldest {row.oldest}). Work through them in the Bank Reconciliation Tool.",
            amount=flt(row.deposits) - flt(row.withdrawals),
        )
        for row in rows
    ]


def payments_without_bank_lines(min_age_days=14, lookback_days=180):
    """Payments booked to a bank account that no bank line ever confirmed.

    Receipts have been booked to the bank with no deposit behind them (offsets, for instance), which makes
    the bank balance in the books higher than the real one.
    """
    rows = frappe.db.sql(
        """
        select pe.name, pe.posting_date, pe.payment_type, pe.party, pe.paid_amount,
               if(pe.payment_type = 'Receive', pe.paid_to, pe.paid_from) bank_account
        from `tabPayment Entry` pe
        where pe.docstatus = 1 and pe.posting_date between %(since)s and %(until)s
          and ifnull(pe.clearance_date, '') = ''
          and if(pe.payment_type = 'Receive', pe.paid_to, pe.paid_from) in (
              select ba.account from `tabBank Account` ba
              where ba.is_company_account = 1 and ifnull(ba.integration_id, '') != '')
          and not exists (
              select 1 from `tabBank Transaction Payments` btp
              where btp.payment_document = 'Payment Entry' and btp.payment_entry = pe.name)
        order by pe.posting_date
        """,
        {"since": add_days(today(), -lookback_days), "until": add_days(today(), -min_age_days)},
        as_dict=True,
    )
    findings = []
    for row in rows:
        direction = "received from" if row.payment_type == "Receive" else "paid to"
        detail = (
            f"{money(row.paid_amount)} {direction} {row.party} through {row.bank_account} on {row.posting_date}, "
            f"but it isn't reconciled to a bank line after {date_diff(today(), row.posting_date)} days."
        )
        candidates = _unreconciled_bank_lines(row)
        if candidates:
            detail += (
                f" Bank line {', '.join(candidates)} has the same amount: reconcile it to this payment "
                "(don't create a new payment from it)."
            )
        else:
            detail += (
                " No bank line has the same amount. If the money never moved through the bank (an offset, "
                "say), it shouldn't be booked to the bank."
            )
        findings.append(finding(ACTION, "Payments with no bank line", "Payment Entry", row.name, detail,
                                row.party, row.paid_amount))
    return findings


def _unreconciled_bank_lines(payment, window_days=7):
    amount_field = "deposit" if payment.payment_type == "Receive" else "withdrawal"
    return frappe.db.sql_list(
        f"""
        select bt.name from `tabBank Transaction` bt
        join `tabBank Account` ba on ba.name = bt.bank_account
        where bt.docstatus = 1 and bt.unallocated_amount > 0.009 and ba.account = %(account)s
          and abs(bt.{amount_field} - %(amount)s) < 0.005
          and bt.date between %(since)s and %(until)s
        order by abs(datediff(bt.date, %(date)s))
        """,
        {
            "account": payment.bank_account,
            "amount": payment.paid_amount,
            "date": payment.posting_date,
            "since": add_days(payment.posting_date, -window_days),
            "until": add_days(payment.posting_date, window_days),
        },
    )


def thin_margins(active_days=90):
    """Projects invoiced recently whose all-time margin is below the co-op's 5% target."""
    rows = frappe.db.sql(
        """
        select p.name, p.project_name, rev.total revenue, coalesce(cost.total, 0) cost
        from `tabProject` p
        join (select project, sum(grand_total) total, max(posting_date) last_invoice from `tabSales Invoice`
              where docstatus = 1 group by project) rev on rev.project = p.name
        left join (select project, sum(grand_total) total from `tabPurchase Invoice`
              where docstatus = 1 group by project) cost on cost.project = p.name
        where rev.last_invoice >= %s and rev.total > 0
        """,
        add_days(today(), -active_days),
        as_dict=True,
    )
    findings = []
    for row in rows:
        pct = 100 * (flt(row.revenue) - flt(row.cost)) / flt(row.revenue)
        if pct < MARGIN_TARGET_PCT - MARGIN_TOLERANCE_PCT:
            findings.append(finding(
                REVIEW, "Thin margins", "Project", row.name,
                f"{row.project_name}: {pct:.1f}% margin all-time ({money(row.revenue)} invoiced, {money(row.cost)} "
                f"in engineer and referral bills). The target is {MARGIN_TARGET_PCT}%.",
                amount=flt(row.revenue) - flt(row.cost),
            ))
    return findings


def overdue_invoices(min_days_overdue=14):
    rows = frappe.db.sql(
        """
        select name, customer, due_date, outstanding_amount from `tabSales Invoice`
        where docstatus = 1 and outstanding_amount > 0.009 and due_date < %s order by due_date
        """,
        add_days(today(), -min_days_overdue),
        as_dict=True,
    )
    return [
        finding(
            REVIEW, "Overdue invoices", "Sales Invoice", row.name,
            f"{money(row.outstanding_amount)} overdue since {row.due_date} ({date_diff(today(), row.due_date)} days).",
            row.customer, row.outstanding_amount,
        )
        for row in rows
    ]


CHECKS = [
    unapplied_payments,
    negative_cash_and_bank,
    payments_without_bank_lines,
    unbilled_timesheets,
    hours_without_timesheets,
    referral_fees,
    stale_drafts,
    bank_backlog,
    overdue_invoices,
    thin_margins,
]


def run_all(include_info=False):
    """Run every check. A failing check is reported as a finding rather than hiding the others."""
    findings = []
    for check in CHECKS:
        try:
            findings.extend(check())
        except Exception:
            frappe.log_error(title=f"books_health: {check.__name__} failed")
            findings.append(finding(
                ACTION, "Health check error", "Error Log", None,
                f"The {check.__name__} check failed to run; see the Error Log.",
            ))
    if not include_info:
        findings = [f for f in findings if f.severity != INFO]
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.check))
    return findings
