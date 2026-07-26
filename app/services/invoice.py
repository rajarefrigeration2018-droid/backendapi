# app/services/invoice.py
"""
Invoice PDF generation.

Built with ReportLab, uploaded to Supabase Storage, and the public URL is
saved back onto the booking. Branding (name, colours, support details)
is pulled from app_config so a rebrand needs no code change.
"""

import io
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from app.core import storage
from app.database import db
from app.services.pricing import get_config

logger = logging.getLogger(__name__)


def _cfg(keys: tuple, defaults: Dict[str, Any]) -> Dict[str, Any]:
    values = get_config(*keys)
    return {k: values.get(k, defaults.get(k)) for k in keys}


def generate(booking_id: int, upload: bool = True) -> Optional[str]:
    """Returns the public URL of the generated PDF, or None on failure."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError:
        logger.error("reportlab is not installed")
        return None

    booking = db.fetch_one(
        """
        select b.*, b.status::text as status,
               b.payment_mode::text as payment_mode,
               b.payment_status::text as payment_status,
               u.name as customer_name, u.phone as customer_phone, u.email as customer_email,
               p.name as technician_name
          from bookings b
          join users u on u.id = b.user_id
          left join partners p on p.id = b.assigned_partner_id
         where b.id = :id
        """,
        {"id": booking_id},
    )
    if not booking:
        return None

    items = db.fetch_all(
        "select service_name, option_name, qty, unit_price, line_total "
        "from booking_items where booking_id = :b order by id",
        {"b": booking_id},
    )
    extras = db.fetch_all(
        """
        select label, amount from booking_extra_charges
         where booking_id = :b and approved_by_user = true and rejected = false
         order by id
        """,
        {"b": booking_id},
    )

    cfg = _cfg(
        ("brand_name", "primary_color", "support_phone", "support_email",
         "gst_percent", "company_address", "company_gstin"),
        {
            "brand_name": "Mistrio", "primary_color": "#1B2A5B",
            "support_phone": "", "support_email": "", "gst_percent": 0,
            "company_address": "", "company_gstin": "",
        },
    )
    brand = colors.HexColor(str(cfg["primary_color"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Invoice {booking['booking_code']}",
    )

    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=22,
                        textColor=brand, spaceAfter=2)
    small = ParagraphStyle("small", parent=ss["Normal"], fontSize=8.5,
                           textColor=colors.HexColor("#666666"))
    label = ParagraphStyle("label", parent=ss["Normal"], fontSize=8,
                           textColor=colors.HexColor("#888888"))
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=9.5)

    story = []

    # ---------- header ----------
    header = Table(
        [[
            Paragraph(f"<b>{cfg['brand_name']}</b>", h1),
            Paragraph(
                f"<b>TAX INVOICE</b><br/>{booking['booking_code']}<br/>"
                f"{booking['created_at'].strftime('%d %b %Y')}",
                ParagraphStyle("r", parent=body, alignment=2),
            ),
        ]],
        colWidths=[100 * mm, 74 * mm],
    )
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header)

    company_lines = [x for x in [cfg["company_address"], cfg["support_phone"],
                                 cfg["support_email"]] if x]
    if cfg["company_gstin"]:
        company_lines.append(f"GSTIN: {cfg['company_gstin']}")
    if company_lines:
        story.append(Paragraph(" &nbsp;|&nbsp; ".join(str(x) for x in company_lines), small))

    story.append(Spacer(1, 6 * mm))
    story.append(Table([[""]], colWidths=[174 * mm], rowHeights=[1],
                       style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), brand)])))
    story.append(Spacer(1, 5 * mm))

    # ---------- parties ----------
    snap = booking["addr_snapshot"] or {}
    address = ", ".join(
        str(x) for x in [snap.get("house"), snap.get("area"), snap.get("landmark"),
                         snap.get("city"), snap.get("pincode")] if x
    )
    parties = Table(
        [[
            Paragraph("BILL TO", label),
            Paragraph("SERVICE DETAILS", label),
        ], [
            Paragraph(
                f"<b>{booking['customer_name'] or 'Customer'}</b><br/>"
                f"{booking['customer_phone']}<br/>{address}",
                body,
            ),
            Paragraph(
                f"Date: {booking['scheduled_date'].strftime('%d %b %Y')}<br/>"
                f"Slot: {booking['slot_label'] or '-'}<br/>"
                f"Technician: {booking['technician_name'] or '-'}",
                body,
            ),
        ]],
        colWidths=[87 * mm, 87 * mm],
    )
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
    ]))
    story.append(parties)
    story.append(Spacer(1, 7 * mm))

    # ---------- items ----------
    rows = [["#", "Description", "Qty", "Rate", "Amount"]]
    n = 0
    for it in items:
        n += 1
        desc = it["service_name"]
        if it["option_name"]:
            desc += f" ({it['option_name']})"
        rows.append([
            str(n), desc, str(it["qty"]),
            f"{float(it['unit_price']):,.2f}", f"{float(it['line_total']):,.2f}",
        ])
    for ex in extras:
        n += 1
        rows.append([
            str(n), f"{ex['label']} (additional)", "1",
            f"{float(ex['amount']):,.2f}", f"{float(ex['amount']):,.2f}",
        ])

    table = Table(rows, colWidths=[10 * mm, 92 * mm, 16 * mm, 26 * mm, 30 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#F7F8FA")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E3E6EC")),
    ]))
    story.append(table)
    story.append(Spacer(1, 5 * mm))

    # ---------- totals ----------
    money = [
        ("Subtotal", float(booking["subtotal"]) + float(booking["extra_charges_total"])),
    ]
    if float(booking["visit_charge"]) > 0:
        money.append(("Visit charge", float(booking["visit_charge"])))
    if float(booking["discount"]) > 0:
        lbl = f"Discount ({booking['coupon_code']})" if booking["coupon_code"] else "Discount"
        money.append((lbl, -float(booking["discount"])))
    if float(booking["tax"]) > 0:
        money.append((f"GST ({cfg['gst_percent']}%)", float(booking["tax"])))

    total_rows = [[k, f"{v:,.2f}"] for k, v in money]
    total_rows.append(["TOTAL", f"Rs. {float(booking['total']):,.2f}"])

    totals = Table(total_rows, colWidths=[44 * mm, 30 * mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, brand),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("TEXTCOLOR", (0, -1), (-1, -1), brand),
    ]))
    story.append(totals)
    story.append(Spacer(1, 6 * mm))

    paid = booking["payment_status"] == "paid"
    story.append(Paragraph(
        f"<b>Payment:</b> {booking['payment_mode'].upper()} &nbsp;·&nbsp; "
        f"<b>Status:</b> {'PAID' if paid else 'PENDING'}",
        body,
    ))

    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(
        "This is a computer-generated invoice and does not require a signature.<br/>"
        f"For any queries contact {cfg['support_phone']} or {cfg['support_email']}.",
        small,
    ))

    doc.build(story)
    pdf = buf.getvalue()
    buf.close()

    if not upload:
        return None
    if not storage.is_configured():
        logger.warning("Storage not configured — invoice generated but not uploaded")
        return None

    url = storage.upload(
        "invoices", pdf, f"{booking['booking_code']}.pdf", "application/pdf"
    )
    if url:
        db.execute(
            "update bookings set invoice_url = :u where id = :id",
            {"u": url, "id": booking_id},
        )
    return url


def get_or_create(booking_id: int) -> Optional[str]:
    existing = db.fetch_value(
        "select invoice_url from bookings where id = :id", {"id": booking_id}
    )
    if existing:
        return existing
    return generate(booking_id)
