"""Excel export of one visible week: a grid sheet and a plain event list."""

from __future__ import annotations

import io
import re
from datetime import datetime, time, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..config import AppConfig
from .periods import MonthView, fmt_hours
from .week import WeekView, fmt_day, fmt_time

# Light tints that work as cell fills; index matches the CSS palette c0..c7.
PALETTE = [
    "DCE8FB",
    "DDF3E4",
    "FCE7D6",
    "EDE3FA",
    "FBE0E6",
    "D9F2F2",
    "F6EFCF",
    "E6E9EE",
    "E6F2D5",
    "D7EFFB",
    "F0E0D2",
    "F5DDF7",
]
BUSY = "E3E5E8"
SLOT_MIN = 30

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _fill_for(cls: str, custom: str | None) -> str:
    if custom and (m := _HEX.match(custom)):
        # lighten a custom colour toward white so text stays readable
        r, g, b = (int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
        mix = lambda c: int(c + (255 - c) * 0.75)  # noqa: E731
        return f"{mix(r):02X}{mix(g):02X}{mix(b):02X}"
    if cls.startswith("c") and cls[1:].isdigit():
        return PALETTE[int(cls[1:]) % len(PALETTE)]
    return BUSY


def week_to_xlsx(cfg: AppConfig, week: WeekView) -> bytes:
    tz = week.tz or cfg.tz
    wb = Workbook()
    ws = wb.active
    ws.title = "Week"
    thin = Side(style="thin", color="D0D4DA")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    bold = Font(bold=True)

    ws["A1"] = f"{week.title} — {week.range_label}"
    ws["A1"].font = Font(bold=True, size=13)
    header_row, allday_row, first_slot_row = 3, 4, 5

    ws.cell(header_row, 1, "Time").font = bold
    ws.cell(allday_row, 1, "All day").font = bold
    ws.column_dimensions["A"].width = 10
    for i, dv in enumerate(week.days):
        col = i + 2
        c = ws.cell(header_row, col, dv.label)
        c.font = bold
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = 30
        if dv.all_day:
            cell = ws.cell(allday_row, col, "\n".join(b.ev.title for b in dv.all_day))
            cell.alignment = wrap
            first = dv.all_day[0]
            cell.fill = PatternFill("solid", fgColor=_fill_for(first.color_class, first.color))
        ws.cell(allday_row, col).border = grid

    n_slots = week.n_hours * 60 // SLOT_MIN
    for s in range(n_slots):
        mins = week.hour_start * 60 + s * SLOT_MIN
        row = first_slot_row + s
        ws.cell(row, 1, fmt_time(time(mins // 60, mins % 60)) if s % 2 == 0 else "")
        ws.row_dimensions[row].height = 26

    for i, dv in enumerate(week.days):
        col = i + 2
        day0 = datetime.combine(dv.day, time(week.hour_start), tzinfo=tz)
        keys: list[tuple[str, ...]] = []
        for s in range(n_slots):
            a = day0 + timedelta(minutes=s * SLOT_MIN)
            b = a + timedelta(minutes=SLOT_MIN)
            keys.append(tuple(blk.ev.key for blk in dv.timed if blk.start < b and blk.end > a))
        by_key = {blk.ev.key: blk for blk in dv.timed}
        s = 0
        while s < n_slots:
            e = s
            while e + 1 < n_slots and keys[e + 1] == keys[s]:
                e += 1
            r0, r1 = first_slot_row + s, first_slot_row + e
            if keys[s]:
                blks = [by_key[k] for k in keys[s]]
                seg_start = day0 + timedelta(minutes=s * SLOT_MIN)
                seg_end = day0 + timedelta(minutes=(e + 1) * SLOT_MIN)
                # Name an event only in the segment where it starts (or the first visible one).
                starting = [
                    b for b in blks if seg_start <= b.start < seg_end or (s == 0 and b.start < seg_start)
                ]
                text = "\n".join(f"{b.ev.title} ({b.time_label})" for b in starting)
                blks = starting or blks  # colour by the newest event
                if r1 > r0:
                    ws.merge_cells(start_row=r0, start_column=col, end_row=r1, end_column=col)
                cell = ws.cell(r0, col, text or None)
                cell.alignment = wrap
                fill = PatternFill("solid", fgColor=_fill_for(blks[0].color_class, blks[0].color))
                for r in range(r0, r1 + 1):
                    ws.cell(r, col).fill = fill
            for r in range(r0, r1 + 1):
                ws.cell(r, col).border = grid
            s = e + 1
    ws.freeze_panes = ws.cell(first_slot_row, 2)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    # Plain list, easy to sort/filter.
    lst = wb.create_sheet("Events")
    headers = ["Date", "Start", "End", "Title", "Calendar", "Location", "Notes"]
    for c, h in enumerate(headers, 1):
        lst.cell(1, c, h).font = bold
    row = 2
    for dv in week.days:
        for blk in dv.all_day + sorted(dv.timed, key=lambda b: b.start):
            ev = blk.ev
            lst.cell(row, 1, dv.label)
            lst.cell(row, 2, "all day" if ev.all_day else fmt_time(blk.start))
            lst.cell(row, 3, "" if ev.all_day else fmt_time(blk.end))
            lst.cell(row, 4, ev.title)
            lst.cell(row, 5, ev.calendar_name or "")
            lst.cell(row, 6, ev.location)
            lst.cell(row, 7, ev.notes)
            row += 1
    for c, w in zip(range(1, 8), [14, 10, 10, 34, 16, 24, 40], strict=True):
        lst.column_dimensions[get_column_letter(c)].width = w
    lst.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def month_to_xlsx(cfg: AppConfig, month: MonthView) -> bytes:
    """A month grid (one cell per day, every item listed) plus a plain event list."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Month"
    thin = Side(style="thin", color="D0D4DA")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    bold = Font(bold=True)
    muted = Font(color="8A93A3")

    ws["A1"] = f"{month.title} — {month.label}"
    ws["A1"].font = Font(bold=True, size=13)
    for i, name in enumerate(month.dows):
        c = ws.cell(3, i + 1, name)
        c.font = bold
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(i + 1)].width = 26

    row = 4
    for week in month.weeks:
        lines_max = 1
        for i, cell in enumerate(week):
            lines = [str(cell.day.day)]
            for chip in cell.chips:
                lines.append(f"{chip.when + ' ' if chip.when else ''}{chip.ev.title}")
            if not cell.in_range and cell.in_month:
                lines.append("(not shared)")
            c = ws.cell(row, i + 1, "\n".join(lines))
            c.alignment = wrap
            c.border = grid
            if not cell.in_month:
                c.font = muted
            elif cell.chips:
                first = cell.chips[0]
                c.fill = PatternFill("solid", fgColor=_fill_for(first.color_class, first.color))
            lines_max = max(lines_max, len(lines))
        ws.row_dimensions[row].height = max(48, 15 * lines_max)
        row += 1
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    lst = wb.create_sheet("Events")
    headers = ["Date", "Start", "End", "Title", "Calendar", "Location", "Notes"]
    for c, h in enumerate(headers, 1):
        lst.cell(1, c, h).font = bold
    r = 2
    for week in month.weeks:
        for cell in week:
            if not cell.in_month:
                continue
            for chip in cell.chips:
                ev = chip.ev
                lst.cell(r, 1, fmt_day(cell.day))
                lst.cell(r, 2, "all day" if ev.all_day else fmt_time(ev.start.astimezone(month.tz)))
                lst.cell(r, 3, "" if ev.all_day else fmt_time(ev.end.astimezone(month.tz)))
                lst.cell(r, 4, ev.title)
                lst.cell(r, 5, ev.calendar_name or "")
                lst.cell(r, 6, ev.location)
                lst.cell(r, 7, ev.notes)
                r += 1
    for c, w in zip(range(1, 8), [14, 10, 10, 34, 16, 24, 40], strict=True):
        lst.column_dimensions[get_column_letter(c)].width = w
    lst.freeze_panes = "A2"

    summary = wb.create_sheet("Busy hours")
    summary.cell(1, 1, "Date").font = bold
    summary.cell(1, 2, "Busy").font = bold
    r = 2
    for week in month.weeks:
        for cell in week:
            if cell.in_month and cell.in_range:
                summary.cell(r, 1, fmt_day(cell.day))
                summary.cell(r, 2, fmt_hours(cell.minutes))
                r += 1
    summary.column_dimensions["A"].width = 14
    summary.column_dimensions["B"].width = 18

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
