"""
GRAFOMEM PDF Report Renderer — styled compliance reports using fpdf2.

Renders regulatory reports (EU AI Act, GDPR, DORA, Full Audit) as
professional PDF documents with compliance badges, evidence tables,
and GRAFOMEM branding.  Zero system dependencies — pure Python via fpdf2.

Usage::

    from aml.cloud.pdf_renderer import render_report_pdf
    pdf_bytes = render_report_pdf(report)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("grafomem.cloud.pdf_renderer")

# Colors (RGB tuples)
_DARK_BG = (15, 15, 25)
_WHITE = (255, 255, 255)
_GRAY = (180, 180, 190)
_LIGHT_GRAY = (230, 230, 235)
_GREEN = (34, 197, 94)
_AMBER = (245, 158, 11)
_RED = (239, 68, 68)
_CYAN = (6, 182, 212)
_BRAND = (99, 102, 241)  # Indigo

_FINDING_COLORS = {
    "COMPLIANT": _GREEN,
    "PARTIAL": _AMBER,
    "INSUFFICIENT_DATA": _RED,
}


def _sanitize(text: str) -> str:
    """Replace Unicode characters unsupported by Helvetica (latin-1 only)."""
    return (
        text
        .replace("\u2014", "--")   # em-dash
        .replace("\u2013", "-")    # en-dash
        .replace("\u2192", "->")   # right arrow
        .replace("\u25cf", "*")    # black circle
        .replace("\u2022", "*")    # bullet
        .replace("\u2018", "'")    # left single quote
        .replace("\u2019", "'")    # right single quote
        .replace("\u201c", '"')    # left double quote
        .replace("\u201d", '"')    # right double quote
    )


def render_report_pdf(report) -> bytes:
    """Render a regulatory Report object to styled PDF bytes.

    Parameters
    ----------
    report : Report
        A ``RegulatoryReportService.Report`` instance with populated content.

    Returns
    -------
    bytes
        The PDF file as bytes.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        raise ImportError(
            "fpdf2 is required for PDF export. Install with: "
            "pip install fpdf2"
        )

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_left_margin(20)
    pdf.set_right_margin(20)

    # ── Title page ──────────────────────────────────────────────────
    pdf.add_page()
    _render_title_page(pdf, report)

    # ── Content pages ───────────────────────────────────────────────
    content = report.content
    if not content:
        pdf.add_page()
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "No content available.", new_x="LMARGIN", new_y="NEXT")
        return pdf.output()

    # Full audit has nested frameworks
    if "frameworks" in content:
        for fw_key, fw_data in content["frameworks"].items():
            _render_framework(pdf, fw_data)
    elif "sections" in content:
        _render_framework(pdf, content)

    # ── Footer page ─────────────────────────────────────────────────
    _render_footer_page(pdf, report)

    return pdf.output()


def _render_title_page(pdf, report) -> None:
    """Render the cover page."""
    pdf.set_font("Helvetica", "B", 28)
    pdf.ln(40)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 15, "GRAFOMEM", new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_GRAY)
    pdf.cell(0, 6, "Governed Agent Memory Platform", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(15)

    # Report title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*_DARK_BG)
    pdf.multi_cell(0, 10, _sanitize(report.title), align="C")
    pdf.ln(8)

    # Framework badge
    framework = report.content.get("framework", report.report_type.value)
    regulation = report.content.get("regulation", "")
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(*_GRAY)
    if regulation:
        pdf.cell(0, 7, _sanitize(regulation), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)

    # Period
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(*_DARK_BG)
    period_start = report.period_start.strftime("%d %B %Y")
    period_end = report.period_end.strftime("%d %B %Y")
    pdf.cell(0, 7, f"Period: {period_start} -- {period_end}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    # Overall finding badge
    overall = report.content.get("overall_finding", "UNKNOWN")
    _render_finding_badge(pdf, overall, center=True, large=True)
    pdf.ln(15)

    # Generation info
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_GRAY)
    pdf.cell(0, 5, f"Generated: {report.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
             new_x="LMARGIN", new_y="NEXT", align="C")
    if report.content_hash:
        pdf.cell(0, 5, f"Content Hash: {report.content_hash[:32]}...",
                 new_x="LMARGIN", new_y="NEXT", align="C")


def _render_framework(pdf, fw_data: dict) -> None:
    """Render one regulatory framework with its sections."""
    pdf.add_page()

    framework = fw_data.get("framework", "")
    regulation = fw_data.get("regulation", "")

    # Framework header
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_BRAND)
    pdf.cell(0, 10, framework, new_x="LMARGIN", new_y="NEXT")

    if regulation:
        pdf.set_font("Helvetica", "I", 10)
        pdf.set_text_color(*_GRAY)
        pdf.cell(0, 6, _sanitize(regulation), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Overall finding for this framework
    overall = fw_data.get("overall_finding", "")
    if overall:
        _render_finding_badge(pdf, overall, center=False, large=False)
        pdf.ln(8)

    # Divider
    _draw_line(pdf)
    pdf.ln(5)

    # Sections
    sections = fw_data.get("sections", {})
    for section_key, section_data in sections.items():
        _render_section(pdf, section_key, section_data)


def _render_section(pdf, key: str, section: dict) -> None:
    """Render a single compliance section."""
    # Check if we need a new page (if near bottom)
    if pdf.get_y() > 230:
        pdf.add_page()

    title = section.get("title", key)
    requirement = section.get("requirement", "")
    finding = section.get("finding", "UNKNOWN")
    evidence = section.get("compliance_evidence", {})

    # Section title
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_DARK_BG)
    pdf.multi_cell(0, 7, _sanitize(title))
    pdf.ln(2)

    # Finding badge
    _render_finding_badge(pdf, finding, center=False, large=False)
    pdf.ln(4)

    # Requirement text
    if requirement:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_GRAY)
        pdf.multi_cell(0, 5, _sanitize(f"Requirement: {requirement}"))
        pdf.ln(3)

    # Evidence table
    if evidence:
        _render_evidence_table(pdf, evidence)
    pdf.ln(6)

    # Section divider
    _draw_line(pdf, light=True)
    pdf.ln(4)


def _render_evidence_table(pdf, evidence: dict) -> None:
    """Render key-value evidence as a styled table."""
    col_key_w = 70
    col_val_w = 100

    # Header
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*_LIGHT_GRAY)
    pdf.set_text_color(*_DARK_BG)
    pdf.cell(col_key_w, 7, "Evidence", border=1, new_x="RIGHT", fill=True)
    pdf.cell(col_val_w, 7, "Value", border=1, new_x="LMARGIN", new_y="NEXT", fill=True)

    # Rows
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_DARK_BG)
    for key, value in evidence.items():
        # Format key
        display_key = key.replace("_", " ").title()
        # Format value
        display_val = _format_value(value)

        # Calculate row height based on content
        pdf.cell(col_key_w, 6, _sanitize(display_key[:40]), border=1, new_x="RIGHT")
        pdf.cell(col_val_w, 6, _sanitize(display_val[:55]), border=1, new_x="LMARGIN", new_y="NEXT")


def _render_finding_badge(pdf, finding: str, center: bool = False, large: bool = False) -> None:
    """Render a colored compliance badge."""
    color = _FINDING_COLORS.get(finding, _GRAY)
    size = 14 if large else 10

    pdf.set_font("Helvetica", "B", size)
    pdf.set_text_color(*color)

    symbol = "*"
    label = f" {finding}"

    if center:
        # Calculate width for centering
        pdf.cell(0, 8, f"{symbol}{label}", new_x="LMARGIN", new_y="NEXT", align="C")
    else:
        pdf.cell(0, 7, f"{symbol}{label}", new_x="LMARGIN", new_y="NEXT")


def _render_footer_page(pdf, report) -> None:
    """Render the document footer with integrity information."""
    pdf.add_page()
    pdf.ln(20)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*_DARK_BG)
    pdf.cell(0, 10, "Document Integrity", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    _draw_line(pdf)
    pdf.ln(8)

    info = [
        ("Report ID", report.report_id),
        ("Report Type", report.report_type.value),
        ("Content Hash", report.content_hash or "N/A"),
        ("Hash Algorithm", "BLAKE2b-256"),
        ("Generated At", report.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("File Size", f"{report.file_size_bytes:,} bytes"),
        ("Period", f"{report.period_start.strftime('%Y-%m-%d')} to "
                  f"{report.period_end.strftime('%Y-%m-%d')}"),
    ]

    pdf.set_font("Helvetica", "", 10)
    for label, value in info:
        pdf.set_text_color(*_GRAY)
        pdf.cell(50, 7, label, new_x="RIGHT")
        pdf.set_text_color(*_DARK_BG)
        pdf.cell(0, 7, str(value), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(20)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(*_GRAY)
    pdf.cell(0, 6, "Produced by GRAFOMEM Cloud — grafomem.com",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 6, "This report is generated from live system data and is verifiable",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 6, "using the content hash above via the GRAFOMEM API.",
             new_x="LMARGIN", new_y="NEXT", align="C")


def _draw_line(pdf, light: bool = False) -> None:
    """Draw a horizontal divider line."""
    color = _LIGHT_GRAY if light else _GRAY
    pdf.set_draw_color(*color)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())


def _format_value(value: Any) -> str:
    """Format a value for display in evidence tables."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        if len(value) <= 3:
            return ", ".join(str(v) for v in value)
        return f"{', '.join(str(v) for v in value[:3])}... ({len(value)} total)"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            return f"{value:.4f}"
        return f"{value:,}"
    if value is None:
        return "N/A"
    return str(value)[:55]
