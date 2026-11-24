"""Generates the two seed PDFs (text only, one policy topic per page). Run once; output is committed."""

from datetime import datetime
from pathlib import Path

from fpdf import FPDF

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "assets"

POLICY_PAGES = [
    (
        "1. Purpose and Effective Date",
        "This Q3 Commercial and Discount Policy governs the commercial terms offered on every customer "
        "order booked in the third quarter of 2026. The policy is effective 1 July 2026 and stays in force "
        "until Finance publishes a replacement. It applies to all regions, all customer tiers and all four "
        "product categories: CLOUD_OPS, DATA_PLATFORM, SECURITY and AI_SUITE. Sales representatives, "
        "regional directors and the finance operations team are responsible for following it.",
    ),
    (
        "2. Discount Caps by Customer Tier",
        "The maximum discount a sales representative may grant without further approval depends on the "
        "customer tier. STARTUP customers have a discount cap of 10 percent. MID_MARKET customers have a "
        "discount cap of 15 percent. ENTERPRISE customers have a discount cap of 25 percent. The cap is "
        "measured against the order-level discount percentage, not against individual line items, and it "
        "applies equally to new business and renewals.",
    ),
    (
        "3. Approval for Discounts Above the Cap",
        "Any discount above the cap for the customer tier requires written approval from the Regional "
        "Director within 48 hours of the quote being issued. Approval requests must state the customer, "
        "the requested discount and the commercial justification. An order that exceeds the cap without "
        "approval is flagged for review by finance operations and may be reversed or re-priced.",
    ),
    (
        "4. Payment Terms",
        "ENTERPRISE customers receive Net-45 payment terms, meaning invoices are due 45 days after the "
        "invoice date. STARTUP and MID_MARKET customers receive Net-30 payment terms, meaning invoices are "
        "due 30 days after the invoice date. Any other payment arrangement needs written approval from "
        "the Regional Director and finance operations before the order is confirmed.",
    ),
    (
        "5. Refund Window",
        "Customers may request a refund within 30 days of the order date. The refund window is 30 days "
        "for every tier and every product category. Requests made after the 30 day window are declined "
        "unless the Regional Director documents a service failure that was caused by the company. "
        "Approved refunds are returned to the original payment method.",
    ),
    (
        "6. Audit and Exceptions",
        "Finance operations audits discount compliance every quarter and shares the results with each "
        "Regional Director. Repeated unapproved discounts above the cap are escalated to the VP Sales. "
        "Exceptions to this policy must be recorded in the deal desk register with the approver name, "
        "the date and the reason. This policy is reviewed annually by the commercial excellence team.",
    ),
]

HANDBOOK_PAGES = [
    (
        "1. Zone Definitions",
        "Regions are grouped into four zones. The SOUTH zone covers Karnataka and Tamil Nadu, including "
        "the Bengaluru and Chennai regions. The WEST zone covers Maharashtra, including the Mumbai and "
        "Pune regions. The NORTH zone covers Delhi and the northern states. The EAST zone covers West "
        "Bengal and the eastern states, including the Kolkata region. Zone leads report to the VP Sales.",
    ),
    (
        "2. SLA First-Response Times",
        "Support first-response targets depend on priority. A P1 incident must receive a first response "
        "within 1 hour. A P2 incident must receive a first response within 4 hours. A P3 request must "
        "receive a first response within 1 business day. The clock starts when the ticket is created and "
        "stops at the first human response from the support team.",
    ),
    (
        "3. Escalation Chain",
        "Incidents are escalated in a fixed order: the Support Engineer first, then the Team Lead, then "
        "the Regional Director, and finally the VP Customer Success. A P1 incident that is not "
        "acknowledged within its first-response window is escalated to the next level automatically. "
        "Every escalation is recorded in the incident log with a timestamp.",
    ),
    (
        "4. Revenue Targets and At-Risk Regions",
        "Each region has a Q3 revenue target set by Finance at the start of the quarter. A region is "
        "marked at risk when its Q3 revenue is below 90 percent of its Q3 target. At-risk regions must "
        "submit a recovery plan to the VP Sales within one week. Attainment is calculated as booked "
        "revenue divided by the Q3 target for the region.",
    ),
    (
        "5. Bonus Eligibility",
        "A region is eligible for the quarterly bonus when its attainment is at or above 100 percent of "
        "its Q3 target. Regions between 90 percent and 100 percent of target are not at risk but are not "
        "bonus eligible. Bonus payouts are approved by the VP Sales after Finance confirms the revenue "
        "figures at quarter close.",
    ),
    (
        "6. Reporting Calendar",
        "Regional revenue attainment is reported weekly during the quarter and finalized within five "
        "business days of quarter close. SLA performance is reviewed monthly by the Regional Director "
        "and the Team Leads. The handbook is owned by the revenue operations team and is reviewed "
        "every six months.",
    ),
]


def build(filename: str, title: str, pages: list[tuple[str, str]]) -> None:
    pdf = FPDF()
    pdf.set_title(title)
    pdf.set_creation_date(datetime(2026, 7, 1, 9, 0, 0))
    pdf.set_auto_page_break(auto=True, margin=15)
    for heading, body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 10, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font("Helvetica", "", 12)
        pdf.multi_cell(0, 7, body, new_x="LMARGIN", new_y="NEXT")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf.output(str(OUT_DIR / filename))


if __name__ == "__main__":
    build("q3_commercial_discount_policy.pdf", "Q3 Commercial & Discount Policy", POLICY_PAGES)
    build(
        "regional_sla_revenue_handbook.pdf",
        "Regional SLA & Revenue Targets Handbook",
        HANDBOOK_PAGES,
    )
    print(f"wrote seed PDFs to {OUT_DIR}")
