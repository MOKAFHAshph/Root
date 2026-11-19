"""Professional Arabic PDF reports powered by ReportLab.

This module intentionally avoids WeasyPrint, GTK, Cairo, Pango, and other
system-level rendering dependencies. Arabic text is reshaped with
``arabic-reshaper`` and reordered with ``python-bidi`` when those packages are
installed. The project requirements install both packages by default.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

try:
    import arabic_reshaper
except ImportError:  # The module still loads; PDF generation logs a fallback warning.
    arabic_reshaper = None

try:
    from bidi.algorithm import get_display as bidi_get_display
except ImportError:  # The module still loads; PDF generation logs a fallback warning.
    bidi_get_display = None

try:
    from reportlab.graphics.shapes import Circle as RLCircle, Drawing as RLDrawing, String as RLString
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.colors import HexColor as RLHexColor
    from reportlab.lib.enums import TA_CENTER as RL_TA_CENTER, TA_RIGHT as RL_TA_RIGHT
    from reportlab.lib.pagesizes import A4 as RL_A4
    from reportlab.lib.styles import ParagraphStyle as RLParagraphStyle, getSampleStyleSheet as rl_get_sample_style_sheet
    from reportlab.lib.units import mm as RL_mm
    from reportlab.pdfbase import pdfmetrics as rl_pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
    from reportlab.pdfgen.canvas import Canvas as RLCanvas
    from reportlab.platypus import (
        HRFlowable as RLHRFlowable,
        Image as RLImage,
        LongTable as RLLongTable,
        Paragraph as RLParagraph,
        SimpleDocTemplate as RLSimpleDocTemplate,
        Spacer as RLSpacer,
        Table as RLTable,
        TableStyle as RLTableStyle,
    )
except ImportError as exc:
    _REPORTLAB_IMPORT_ERROR: Exception | None = exc
else:
    _REPORTLAB_IMPORT_ERROR = None

from bot.runtime import seed_globals as _seed_globals, export_globals as _export_globals
_seed_globals(globals())

ARABIC_FONT_PATH = env("ARABIC_FONT_PATH", "")
ARABIC_BOLD_FONT_PATH = env("ARABIC_BOLD_FONT_PATH", "")
BOT_LOGO_PATH = env("BOT_LOGO_PATH", "assets/logo.png")
REPORT_COMPANY_NAME = env("REPORT_COMPANY_NAME", APP_NAME)
REPORT_FOOTER_TEXT = env("REPORT_FOOTER_TEXT", "تم إنشاء هذا التقرير بواسطة البوت")

_ARABIC_RE = re.compile(
    "[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]"
)


# ---------------------------------------------------------------------------
# Text and amount helpers
# ---------------------------------------------------------------------------

def _plain(value: Any) -> str:
    return str(value if value not in (None, "") else "—")


def _display_amount(value: Any, *, places: int | None = None) -> str:
    """تنسيق العرض وفق إعداد الخانات العشرية دون تغيير دقة التخزين."""
    return format_display_amount(value, places)


def _status_ar(status: str) -> str:
    labels = {
        "COMPLETED": "مكتملة",
        "VERIFIED": "تم التحقق",
        "WAITING_REFERENCE": "بانتظار رقم العملية",
        "WAITING_PAYMENT": "بانتظار الدفع",
        "PAYMENT_REVIEW": "مراجعة الدفع",
        "PROCESSING": "قيد التنفيذ",
        "SUBMITTED": "تم الإرسال",
        "STATUS_UNKNOWN": "غير مؤكدة",
        "MANUAL_REVIEW": "مراجعة يدوية",
        "FAILED": "فاشلة",
        "FAILED_RELEASED": "فشلت وأعيد الرصيد",
        "EXPIRED_RELEASED": "انتهت وأعيد الرصيد",
        "REJECTED": "مرفوضة",
        "CANCELLED": "ملغاة",
        "RELEASED": "تمت إعادة الرصيد",
        "PENDING": "معلقة",
        "ACTIVE": "نشط",
        "SUSPENDED": "موقوف",
    }
    return labels.get(str(status or "").upper(), str(status or "—"))


def _rtl_text(value: Any) -> str:
    """Return visual-order Arabic text while preserving ordinary LTR strings."""
    text = _plain(value)
    if not _ARABIC_RE.search(text):
        return text
    if arabic_reshaper is not None and bidi_get_display is not None:
        return bidi_get_display(arabic_reshaper.reshape(text))
    return text


def _paragraph(value: Any, style: "RLParagraphStyle", *, rtl: bool = True) -> "RLParagraph":
    text = _rtl_text(value) if rtl else _plain(value)
    return RLParagraph(xml_escape(text), style)


# ---------------------------------------------------------------------------
# Portable Arabic font discovery
# ---------------------------------------------------------------------------

def _configured_font_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def _pdf_report_font_candidates() -> tuple[list[Path], list[Path]]:
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    regular = [
        _configured_font_path(ARABIC_FONT_PATH) if ARABIC_FONT_PATH else Path(),
        windows_fonts / "tahoma.ttf",
        windows_fonts / "arial.ttf",
        windows_fonts / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    bold = [
        _configured_font_path(ARABIC_BOLD_FONT_PATH) if ARABIC_BOLD_FONT_PATH else Path(),
        windows_fonts / "tahomabd.ttf",
        windows_fonts / "arialbd.ttf",
        windows_fonts / "segoeuib.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ]
    return regular, bold


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        # Path() means the current directory and must never be treated as a font.
        if str(path) not in ("", ".") and path.is_file():
            return path.resolve()
    return None


def _register_arabic_fonts() -> tuple[str, str, Path]:
    regular_candidates, bold_candidates = _pdf_report_font_candidates()
    regular_path = _first_existing(regular_candidates)
    if regular_path is None:
        raise WalletUserError(
            "لم يتم العثور على خط عربي صالح لإنشاء التقرير. "
            "ضع مسار ملف TTF في ARABIC_FONT_PATH داخل ملف .env."
        )
    bold_path = _first_existing(bold_candidates) or regular_path

    regular_name = "BNArabicRegular"
    bold_name = "BNArabicBold"
    registered = set(rl_pdfmetrics.getRegisteredFontNames())
    if regular_name not in registered:
        rl_pdfmetrics.registerFont(RLTTFont(regular_name, str(regular_path)))
    if bold_name not in registered:
        rl_pdfmetrics.registerFont(RLTTFont(bold_name, str(bold_path)))

    return regular_name, bold_name, regular_path


def _ensure_pdf_engine() -> None:
    if _REPORTLAB_IMPORT_ERROR is not None:
        raise WalletUserError(
            "مكتبة ReportLab غير مثبتة. نفّذ الأمر: "
            "py -3.13 -m pip install -r requirements.txt"
        ) from _REPORTLAB_IMPORT_ERROR
    if arabic_reshaper is None or bidi_get_display is None:
        log.warning(
            "arabic-reshaper أو python-bidi غير مثبتة؛ سيتم إنشاء PDF لكن قد لا يظهر "
            "اتصال الحروف العربية بصورة صحيحة. ثبّت requirements.txt."
        )


# ---------------------------------------------------------------------------
# Page canvas, styles, and reusable report components
# ---------------------------------------------------------------------------
if _REPORTLAB_IMPORT_ERROR is None:

    class _NumberedCanvas(RLCanvas):
        """Canvas that adds an Arabic ``page X of Y`` marker after pagination."""

        def __init__(self, *args, footer_font_name: str, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states: list[dict[str, Any]] = []
            self._footer_font_name = footer_font_name

        def showPage(self) -> None:  # noqa: N802 - ReportLab API name
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            page_count = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_page_count(page_count)
                super().showPage()
            super().save()

        def _draw_page_count(self, page_count: int) -> None:
            self.saveState()
            self.setFillColor(RLHexColor("#697771"))
            self.setFont(self._footer_font_name, 8)
            self.drawString(
                14 * RL_mm,
                10 * RL_mm,
                _rtl_text(f"الصفحة {self._pageNumber} من {page_count}"),
            )
            self.restoreState()


def _styles(regular_font: str, bold_font: str) -> dict[str, "RLParagraphStyle"]:
    base = rl_get_sample_style_sheet()
    return {
        "title": RLParagraphStyle(
            "BNTitle",
            parent=base["Title"],
            fontName=bold_font,
            fontSize=22,
            leading=31,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#CE1230"),
            spaceAfter=2 * RL_mm,
        ),
        "subtitle": RLParagraphStyle(
            "BNSubtitle",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=10,
            leading=15,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#697771"),
        ),
        "section": RLParagraphStyle(
            "BNSection",
            parent=base["Heading2"],
            fontName=bold_font,
            fontSize=13,
            leading=19,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#CE1230"),
            spaceBefore=4 * RL_mm,
            spaceAfter=2 * RL_mm,
        ),
        "label": RLParagraphStyle(
            "BNLabel",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=8.5,
            leading=13,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#697771"),
        ),
        "value": RLParagraphStyle(
            "BNValue",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=9.5,
            leading=14,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#171C1A"),
        ),
        "value_bold": RLParagraphStyle(
            "BNValueBold",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=10,
            leading=15,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#171C1A"),
        ),
        "metric": RLParagraphStyle(
            "BNMetric",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=8,
            leading=12,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#697771"),
        ),
        "metric_value": RLParagraphStyle(
            "BNMetricValue",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=11,
            leading=17,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#171C1A"),
        ),
        "table_header": RLParagraphStyle(
            "BNTableHeader",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=8,
            leading=12,
            alignment=RL_TA_CENTER,
            textColor=rl_colors.white,
        ),
        "table_cell": RLParagraphStyle(
            "BNTableCell",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.7,
            leading=11,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#171C1A"),
        ),
        "table_cell_center": RLParagraphStyle(
            "BNTableCellCenter",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=7.7,
            leading=11,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#171C1A"),
        ),
        "status": RLParagraphStyle(
            "BNStatus",
            parent=base["Normal"],
            fontName=bold_font,
            fontSize=7.5,
            leading=11,
            alignment=RL_TA_CENTER,
            textColor=RLHexColor("#158F70"),
        ),
        "note": RLParagraphStyle(
            "BNNote",
            parent=base["Normal"],
            fontName=regular_font,
            fontSize=8,
            leading=13,
            alignment=RL_TA_RIGHT,
            textColor=RLHexColor("#697771"),
        ),
    }


def _section_title(title: str, styles: dict[str, "RLParagraphStyle"]) -> "RLTable":
    table = RLTable(
        [[_paragraph(title, styles["section"])], [""]],
        colWidths=[182 * RL_mm],
        rowHeights=[None, 0.7 * RL_mm],
    )
    table.setStyle(
        RLTableStyle(
            [
                ("BACKGROUND", (0, 1), (0, 1), RLHexColor("#26A17B")),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _logo_flowable(logo_path: Path) -> Any:
    if logo_path.is_file():
        return RLImage(str(logo_path), width=24 * RL_mm, height=24 * RL_mm, kind="proportional")

    badge = RLDrawing(70, 70)
    badge.add(RLCircle(35, 35, 30, fillColor=RLHexColor("#CE1230"), strokeColor=RLHexColor("#26A17B"), strokeWidth=5))
    badge.add(RLString(35, 31, "USDT", textAnchor="middle", fontName="Helvetica-Bold", fontSize=12, fillColor=rl_colors.white))
    return badge


def _page_frame(canvas: "RLCanvas", doc: "RLSimpleDocTemplate", regular_font: str, bold_font: str) -> None:
    page_width, page_height = RL_A4
    canvas.saveState()

    canvas.setFont(bold_font, 8.5)
    canvas.setFillColor(RLHexColor("#CE1230"))
    canvas.drawRightString(
        page_width - 14 * RL_mm,
        page_height - 12 * RL_mm,
        _rtl_text("تقرير العمليات المالية"),
    )

    canvas.setFont(regular_font, 8)
    canvas.setFillColor(RLHexColor("#697771"))
    canvas.drawString(14 * RL_mm, page_height - 12 * RL_mm, _rtl_text(APP_NAME))

    canvas.setStrokeColor(RLHexColor("#DCE5E1"))
    canvas.setLineWidth(0.6)
    canvas.line(14 * RL_mm, page_height - 15 * RL_mm, page_width - 14 * RL_mm, page_height - 15 * RL_mm)
    canvas.line(14 * RL_mm, 15 * RL_mm, page_width - 14 * RL_mm, 15 * RL_mm)

    canvas.setFont(regular_font, 8)
    canvas.setFillColor(RLHexColor("#697771"))
    canvas.drawRightString(
        page_width - 14 * RL_mm,
        10 * RL_mm,
        _rtl_text(REPORT_FOOTER_TEXT),
    )
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Main public report function
# ---------------------------------------------------------------------------

def generate_user_pdf_report(telegram_user_id: int) -> Path:
    _ensure_pdf_engine()
    regular_font, bold_font, font_path = _register_arabic_fonts()

    user_id = int(telegram_user_id)
    customer = DBX.get_customer_by_telegram(user_id)
    if not customer:
        raise WalletUserError("لا توجد بيانات عميل مرتبطة بهذا الحساب.")

    stats = wallet_user_stats(user_id)
    operations = wallet_report_operations(user_id)
    timestamp = datetime.now()
    filename = f"financial_report_{user_id}_{timestamp.strftime('%Y%m%d_%H%M%S')}.pdf"
    output = EXPORT_DIR / filename
    output.parent.mkdir(parents=True, exist_ok=True)

    styles = _styles(regular_font, bold_font)
    document = RLSimpleDocTemplate(
        str(output),
        pagesize=RL_A4,
        rightMargin=14 * RL_mm,
        leftMargin=14 * RL_mm,
        topMargin=23 * RL_mm,
        bottomMargin=21 * RL_mm,
        title="Financial Operations Report",
        author=REPORT_COMPANY_NAME,
        subject=f"User report for Telegram ID {user_id}",
    )

    story: list[Any] = []
    logo = _logo_flowable(BASE_DIR / BOT_LOGO_PATH)
    logo_table = RLTable([[logo]], colWidths=[182 * RL_mm])
    logo_table.setStyle(RLTableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.extend(
        [
            logo_table,
            RLSpacer(1, 2 * RL_mm),
            _paragraph("تقرير العمليات المالية", styles["title"]),
            _paragraph(REPORT_COMPANY_NAME, styles["subtitle"]),
            _paragraph(REPORT_FOOTER_TEXT, styles["subtitle"]),
            RLSpacer(1, 3 * RL_mm),
            RLHRFlowable(width="100%", thickness=1.4, color=RLHexColor("#CE1230"), spaceBefore=1 * RL_mm, spaceAfter=3 * RL_mm),
        ]
    )

    # Report metadata: the last table cell is visually the rightmost cell.
    story.append(_section_title("بيانات التقرير", styles))
    report_info = [
        [
            _paragraph(timestamp.strftime("%Y-%m-%d %H:%M:%S"), styles["value"], rtl=False),
            _paragraph("تاريخ ووقت التصدير", styles["label"]),
            _paragraph(timestamp.strftime("%Y-%m-%d"), styles["value"], rtl=False),
            _paragraph("تاريخ إنشاء التقرير", styles["label"]),
        ],
        [
            _paragraph("تقرير مستخدم مالي", styles["value"]),
            _paragraph("نوع التقرير", styles["label"]),
            _paragraph(APP_NAME, styles["value"]),
            _paragraph("اسم البوت", styles["label"]),
        ],
    ]
    info_table = RLTable(report_info, colWidths=[48 * RL_mm, 33 * RL_mm, 48 * RL_mm, 33 * RL_mm])
    info_table.setStyle(
        RLTableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.55, RLHexColor("#DCE5E1")),
                ("BACKGROUND", (1, 0), (1, -1), RLHexColor("#FDECEF")),
                ("BACKGROUND", (3, 0), (3, -1), RLHexColor("#FDECEF")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([info_table, RLSpacer(1, 2 * RL_mm)])

    story.append(_section_title("بيانات العميل", styles))
    username = f"@{customer['telegram_username']}" if customer["telegram_username"] else "—"
    customer_info = [
        [
            _paragraph(_status_ar(customer["status"]), styles["value_bold"]),
            _paragraph("حالة الحساب", styles["label"]),
            _paragraph(customer["full_name"], styles["value_bold"]),
            _paragraph("اسم المستخدم", styles["label"]),
        ],
        [
            _paragraph(username, styles["value"], rtl=False),
            _paragraph("اسم مستخدم تيليجرام", styles["label"]),
            _paragraph(user_id, styles["value"], rtl=False),
            _paragraph("Telegram ID", styles["label"], rtl=False),
        ],
        [
            _paragraph(customer["phone_alt"] or "—", styles["value"], rtl=False),
            _paragraph("الرقم البديل", styles["label"]),
            _paragraph(customer["phone_primary"] or "—", styles["value"], rtl=False),
            _paragraph("رقم الهاتف", styles["label"]),
        ],
        [
            _paragraph(stats["last_at"] or "—", styles["value"], rtl=False),
            _paragraph("تاريخ آخر عملية", styles["label"]),
            _paragraph(stats["first_used_at"] or "—", styles["value"], rtl=False),
            _paragraph("تاريخ أول استخدام", styles["label"]),
        ],
    ]
    customer_table = RLTable(customer_info, colWidths=[48 * RL_mm, 33 * RL_mm, 48 * RL_mm, 33 * RL_mm])
    customer_table.setStyle(
        RLTableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.55, RLHexColor("#DCE5E1")),
                ("BACKGROUND", (1, 0), (1, -1), RLHexColor("#F8FAF9")),
                ("BACKGROUND", (3, 0), (3, -1), RLHexColor("#F8FAF9")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([customer_table, RLSpacer(1, 2 * RL_mm)])

    story.append(_section_title("الملخص المالي", styles))
    metrics = [
        ("عدد عمليات الشراء", stats["purchase_count"], False),
        ("إجمالي المشتريات", f"{_display_amount(stats['purchase_usdt'])} USDT", False),
        ("عدد عمليات السحب", stats["withdraw_count"], False),
        ("إجمالي السحوبات", f"{_display_amount(stats['withdraw_usdt'])} USDT", False),
        ("إجمالي المدفوع بالريال", q2(stats["purchase_yer"]), True),
        ("الرصيد المتاح", f"{_display_amount(stats['available'])} USDT", True),
        ("عدد الإحالات", stats["referral_count"], True),
        ("رصيد الإحالات", f"{_display_amount(stats['referral_balance'])} USDT", True),
        ("إجمالي عدد العمليات", stats["operation_count"], False),
        ("العمليات الناجحة", stats["successful_operations"], False),
        ("نسبة النجاح", f"{stats['success_rate']:.1f}%", False),
        ("الرصيد المحجوز", f"{_display_amount(stats['locked'])} USDT", False),
    ]
    metric_rows: list[list[Any]] = []
    metric_background_rows: list[bool] = []
    for index in range(0, len(metrics), 4):
        # Reverse each logical group so the first metric appears on the right.
        group = list(reversed(metrics[index:index + 4]))
        row: list[Any] = []
        for label, value, gold in group:
            cell = [
                _paragraph(label, styles["metric"]),
                RLSpacer(1, 1.1 * RL_mm),
                _paragraph(value, styles["metric_value"], rtl=False),
            ]
            row.append(cell)
        metric_rows.append(row)
        metric_background_rows.append(any(item[2] for item in group))

    summary_table = RLTable(metric_rows, colWidths=[40.5 * RL_mm] * 4, rowHeights=[23 * RL_mm] * 3)
    summary_commands: list[tuple[Any, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.65, RLHexColor("#DCE5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 0), (-1, 0), RLHexColor("#FDECEF")),
        ("BACKGROUND", (0, 1), (-1, 1), RLHexColor("#E9F7F2")),
        ("BACKGROUND", (0, 2), (-1, 2), RLHexColor("#FDECEF")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    summary_table.setStyle(RLTableStyle(summary_commands))
    story.extend([summary_table, RLSpacer(1, 2 * RL_mm)])

    story.append(_section_title("جدول جميع العمليات", styles))
    # ReportLab lays columns from left to right. The list is therefore reversed so
    # readers see date -> type -> amount -> status -> reference from right to left.
    operation_data: list[list[Any]] = [
        [
            _paragraph("المرجع", styles["table_header"]),
            _paragraph("الحالة", styles["table_header"]),
            _paragraph("المبلغ", styles["table_header"]),
            _paragraph("نوع العملية", styles["table_header"]),
            _paragraph("التاريخ", styles["table_header"]),
        ]
    ]
    for operation in operations:
        operation_data.append(
            [
                _paragraph(operation.get("reference") or "—", styles["table_cell_center"], rtl=False),
                _paragraph(_status_ar(operation["status"]), styles["status"]),
                _paragraph(
                    f"{_display_amount(operation['amount'])} {operation.get('currency') or 'USDT'}",
                    styles["table_cell_center"],
                    rtl=False,
                ),
                _paragraph(operation["type_label"], styles["table_cell"]),
                _paragraph(operation["created_at"], styles["table_cell_center"], rtl=False),
            ]
        )
    if not operations:
        operation_data.append(
            [
                _paragraph("لا توجد عمليات مسجلة حتى الآن.", styles["table_cell"]),
                "",
                "",
                "",
                "",
            ]
        )

    operations_table = RLLongTable(
        operation_data,
        repeatRows=1,
        colWidths=[38 * RL_mm, 29 * RL_mm, 31 * RL_mm, 30 * RL_mm, 34 * RL_mm],
        splitByRow=1,
    )
    operation_style: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), RLHexColor("#CE1230")),
        ("GRID", (0, 0), (-1, -1), 0.45, RLHexColor("#DCE5E1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if not operations:
        operation_style.append(("SPAN", (0, 1), (-1, 1)))
        operation_style.append(("ALIGN", (0, 1), (-1, 1), "CENTER"))
    else:
        for row_index in range(1, len(operation_data)):
            background = "#F8FAF9" if row_index % 2 == 0 else "#FFFFFF"
            operation_style.append(("BACKGROUND", (0, row_index), (-1, row_index), RLHexColor(background)))
    operations_table.setStyle(RLTableStyle(operation_style))
    story.extend([operations_table, RLSpacer(1, 4 * RL_mm)])

    note_table = RLTable(
        [[_paragraph(
            "هذا التقرير ملخص آلي للعمليات المسجلة في النظام وقت التصدير، "
            "ويُرجع إلى سجلات النظام الأصلية عند الحاجة إلى المراجعة.",
            styles["note"],
        )]],
        colWidths=[162 * RL_mm],
    )
    note_table.setStyle(
        RLTableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), RLHexColor("#F8FAF9")),
                ("BOX", (0, 0), (-1, -1), 0.6, RLHexColor("#DCE5E1")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(note_table)

    on_page = lambda canvas, doc: _page_frame(canvas, doc, regular_font, bold_font)
    canvasmaker = lambda *args, **kwargs: _NumberedCanvas(
        *args,
        footer_font_name=regular_font,
        **kwargs,
    )
    try:
        document.build(
            story,
            onFirstPage=on_page,
            onLaterPages=on_page,
            canvasmaker=canvasmaker,
        )
    except Exception:
        output.unlink(missing_ok=True)
        raise

    log.info(
        "Created Arabic PDF report with ReportLab: user=%s file=%s font=%s",
        user_id,
        output.name,
        font_path,
    )
    return output


_export_globals(globals())
