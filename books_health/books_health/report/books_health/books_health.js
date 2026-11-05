const SEVERITY_COLORS = { "Action needed": "red", Review: "orange", FYI: "gray" };

frappe.query_reports["Books Health"] = {
	filters: [
		{
			fieldname: "include_info",
			label: __("Include FYI items"),
			fieldtype: "Check",
			default: 0,
		},
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (column.fieldname === "severity" && data) {
			value = `<span class="indicator-pill ${SEVERITY_COLORS[data.severity] || "gray"}">${value}</span>`;
		}
		return value;
	},
	onload(report) {
		report.page.add_inner_button(__("Email this to me"), () => {
			frappe.call({
				method: "books_health.digest.send_digest_now",
				freeze: true,
				callback: () => frappe.show_alert({ message: __("Sent"), indicator: "green" }),
			});
		});
	},
};
