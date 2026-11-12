# books_health

A weekly health check of the 508.dev books in ERPNext. It finds bookkeeping mistakes that are easy to make and slow to notice, and emails a digest every Monday morning.

The checks only read data; nothing is changed.

## What it checks

| Check | Severity | Catches | Example that prompted it |
|---|---|---|---|
| Unapplied payments | Action if the party has open invoices, otherwise Review; under $2 is FYI | Payments older than a week not applied to any invoice | A client's payments sat unapplied for ten months and made later invoices look prepaid |
| Cash or bank below zero | Action | Cash or bank GL balances below zero | Bank payouts booked to Cash |
| Payments with no bank line | Action | Payments on a bank-fed account with no bank line after 14 days; names a same-amount bank line if one exists | Offsets booked as bank receipts; Wise payouts never matched |
| Unbilled hours | Action, or Review when invoices from that period carry enough unlinked hours to explain it | Billable timesheet hours older than 35 days that aren't on any client invoice | Hours paid to an engineer that never reached a client invoice |
| Hours without timesheets | Review | Invoices that bill more hours than their linked timesheets (projects that bill from timesheets) | Hours billed twice on a second invoice |
| Referral fees | Action if missing, Review if the amount is off, FYI if deleted by hand (treated as waived) | Invoices from the last 60 days on projects with referrers, without matching referral bills | |
| Stale drafts | Review | Draft invoices, payments and journal entries older than 14 days | |
| Bank backlog | Review | Unreconciled bank lines older than 14 days, per account | |
| Overdue invoices | Review | Client invoices more than 14 days past due | |
| Thin margins | Review | Projects invoiced in the last 90 days with an all-time margin under 4.5% (5% target, half a point of tolerance) | |

Thresholds are constants at the top of each check in `books_health/checks.py`.

## Where to see it

- **Report:** *Books Health* (`/app/query-report/Books Health`), for Accounts Users and Managers. Tick *Include FYI items* to see waived referral fees and rounding residue. *Email this to me* sends the digest to whoever clicks it.
- **Email:** Mondays at 07:00 (site time zone). It goes only to the addresses in site config, and there is deliberately no default, because the digest lists every party's balances and payouts:

  ```bash
  bench --site <site> set-config -p books_health_recipients '["caleb@508.dev"]'
  ```

## Install

```bash
bench get-app https://github.com/508-dev/books_health
bench --site <site> install-app books_health
bench --site <site> migrate   # syncs the report definition
```

## Tests

```bash
bench --site <site> run-tests --app books_health
```

The tests run every check against the site's own data and validate what comes back, so run them against a copy of production (see the erp-local setup), not an empty site.

The API contract also has a standalone suite that replaces the Frappe boundary
without requiring a site:

```bash
python3 -m unittest discover -s tests -p 'test_api_unit.py' -v
```

This checks authorization, unsupported scopes, input validation, pagination and
stable result identifiers. It does not replace the Bench tests of database
queries, Frappe permissions or delivery of the digest.

## Integration API (v1)

`GET /api/method/books_health.api.get_books_health` accepts:

| Parameter | Meaning |
|---|---|
| `company` | Required exact Company name. |
| `include_info` | Optional boolean (`true`, `false`, `1`, `0`); defaults to false. |
| `limit` | Page size, 1–200; defaults to 50. |
| `offset` | Offset, 0–100000; defaults to 0. |

The result is in Frappe's normal `message` envelope:

```json
{
  "message": {
    "api_version": 1,
    "company": "Example Company",
    "currency": "USD",
    "generated_at": "2026-11-13T07:00:00",
    "status": "complete",
    "failed_checks": [],
    "findings": [
      {
        "check_id": "overdue_invoices",
        "severity": "review",
        "doctype": "Sales Invoice",
        "name": "EXAMPLE-INVOICE",
        "party": "Example Customer",
        "amount": 100,
        "currency": "USD",
        "detail": "Human-readable explanation.",
        "error_code": null
      }
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
    "has_more": false
  }
}
```

`generated_at` uses the site's time zone. The stable severity values are
`action`, `review` and `info`. Check IDs are `unapplied_payments`,
`negative_cash_and_bank`, `payments_without_bank_lines`, `unbilled_hours`,
`hours_without_timesheets`, `referral_fees`, `stale_drafts`, `bank_backlog`,
`overdue_invoices` and `thin_margins`. Display text is for people; clients should
use these identifiers for control flow.

If a check fails, the response has `status: "partial_failure"`, lists its ID in
`failed_checks`, and includes an action finding with `error_code: "check_failed"`.
This status covers the entire scan, including findings outside the current
page. Private failure details are not returned to the caller. The GET endpoint
does not write Error Logs; run the ERP report to record diagnostics when a
check fails. A partial failure must not be displayed as an all-clear result.
Every request recomputes the checks; pagination bounds the response size rather
than freezing a snapshot. Refresh the list when the books change.

### Authorization and supported scope

Use Frappe token or OAuth authentication. Authorization is always that of the
authenticated ERP user; there is no `actor`, user-email override or impersonation
parameter. Discord role claims alone do not grant ERP access.

The endpoint requires an Accounts User, Accounts Manager or System Manager role
and unrestricted read access to all source documents, including their linked
parties, projects, ledger entries and deleted-document history. The standard
Accounts Manager role alone may lack access to Deleted Document; this API
deliberately rejects that account until its source access is authorized. A primary
finding can describe other documents or party-wide totals, so checking only its
primary document would disclose data to restricted readers. The API checks
source DocType permissions and permission-filtered source counts before it
runs any checks. Users with limited document access are rejected in full;
results are never silently filtered into an incomplete health report.

The checks currently aggregate the whole site and format dollars. The API
therefore supports only a single Company with USD as its default currency,
and rejects foreign-currency source records. Multi-company and multicurrency
sites need scoped checks and currency-aware calculations before this API can
serve them. This restriction is enforced rather than relying on installation
conventions. The existing report and scheduled digest keep their current
behavior. Manual **Email this to me** requests now require POST and the same
source authorization as the API.

Validation/authorization failures use Frappe's normal HTTP error response plus
a top-level `error_code`: `invalid_argument`, `forbidden` or `unsupported_scope`.
No database changes, Error Logs or outbound email occur when reading the API.

## License

MIT, see [license.txt](license.txt).
