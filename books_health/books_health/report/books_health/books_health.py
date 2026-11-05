from books_health.checks import run_all


def execute(filters=None):
    filters = filters or {}
    columns = [
        {"fieldname": "severity", "label": "Severity", "fieldtype": "Data", "width": 120},
        {"fieldname": "check", "label": "Check", "fieldtype": "Data", "width": 190},
        {"fieldname": "doctype", "label": "Type", "fieldtype": "Data", "width": 130},
        {"fieldname": "name", "label": "Document", "fieldtype": "Dynamic Link", "options": "doctype", "width": 190},
        {"fieldname": "party", "label": "Party", "fieldtype": "Data", "width": 160},
        {"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "width": 120},
        {"fieldname": "detail", "label": "What's wrong", "fieldtype": "Data", "width": 700},
    ]
    return columns, run_all(include_info=bool(filters.get("include_info")))
