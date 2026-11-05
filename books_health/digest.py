import frappe
from frappe.utils import get_url, get_url_to_form, today

from books_health.checks import ACTION, REVIEW, money, run_all

MAX_ROWS = 40  # per severity; the report has the full list


def recipients():
    """Site config "books_health_recipients". There is deliberately no default: the digest lists every
    party's balances and payouts, so who receives it has to be chosen."""
    return frappe.conf.get("books_health_recipients") or []


def build_digest():
    findings = run_all()
    action = [f for f in findings if f.severity == ACTION]
    review = [f for f in findings if f.severity == REVIEW]
    for f in findings:
        f.url = get_url_to_form(f.doctype, f.name) if f.name else None
        f.amount_text = money(f.amount) if f.amount is not None else ""

    counts = {}
    for f in findings:
        counts.setdefault(f.check, {ACTION: 0, REVIEW: 0})[f.severity] += 1

    if findings:
        subject = f"Books health {today()}: {len(action)} to fix, {len(review)} to review"
    else:
        subject = f"Books health {today()}: all clear"
    message = frappe.render_template(
        "books_health/templates/emails/books_health_digest.html",
        {
            "action": action[:MAX_ROWS],
            "review": review[:MAX_ROWS],
            "more_action": max(0, len(action) - MAX_ROWS),
            "more_review": max(0, len(review) - MAX_ROWS),
            "counts": counts,
            "ACTION": ACTION,
            "REVIEW": REVIEW,
            "report_url": get_url("/app/query-report/Books Health"),
        },
    )
    return subject, message


def send_weekly_digest():
    to = recipients()
    if not to:
        return
    subject, message = build_digest()
    frappe.sendmail(recipients=to, subject=subject, message=message)


@frappe.whitelist()
def send_digest_now():
    frappe.only_for(("Accounts Manager", "System Manager"))
    subject, message = build_digest()
    frappe.sendmail(recipients=[frappe.session.user], subject=subject, message=message, now=True)
