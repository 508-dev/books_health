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

## License

MIT, see [license.txt](license.txt).
