app_name = "books_health"
app_title = "Books Health"
app_publisher = "508.dev"
app_description = "Weekly health check of the books: finds the bookkeeping mistakes that are easy to make and slow to notice."
app_email = "admin@508.dev"
app_license = "mit"

# Monday 07:00 in the site's time zone.
scheduler_events = {
    "cron": {
        "0 7 * * 1": ["books_health.digest.send_weekly_digest"],
    }
}
