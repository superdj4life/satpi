#!/usr/bin/env python3
# satpi
# Export reception data and plots from SQLite into a PDF report.

import argparse
import os
import sqlite3
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    LongTable,
)

from load_config import load_config, ConfigError


def parse_args():
    parser = argparse.ArgumentParser(description="Export reception report PDF from SQLite database")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.ini (default: ../config/config.ini relative to this script)",
    )
    parser.add_argument(
        "--pass-id",
        default=None,
        help="Export exactly one pass_id",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=None,
        help="Export latest N passes",
    )
    parser.add_argument(
        "--satellite",
        default=None,
        help="Filter by satellite name",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output PDF path",
    )
    return parser.parse_args()


def get_config_path(cli_value: str | None) -> str:
    if cli_value:
        return os.path.abspath(cli_value)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "config", "config.ini")


def open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def build_output_path(base_dir: str, args) -> str:
    reports_dir = os.path.join(base_dir, "results", "reports")
    os.makedirs(reports_dir, exist_ok=True)

    if args.output:
        return os.path.abspath(args.output)

    if args.pass_id:
        return os.path.join(reports_dir, f"{args.pass_id}-report.pdf")
    if args.satellite and args.latest:
        safe_sat = args.satellite.replace(" ", "_")
        return os.path.join(reports_dir, f"{safe_sat}-latest-{args.latest}-report.pdf")
    if args.satellite:
        safe_sat = args.satellite.replace(" ", "_")
        return os.path.join(reports_dir, f"{safe_sat}-report.pdf")
    if args.latest:
        return os.path.join(reports_dir, f"latest-{args.latest}-passes-report.pdf")
    return os.path.join(reports_dir, "reception-report.pdf")


def query_passes(conn: sqlite3.Connection, args) -> list[sqlite3.Row]:
    if args.pass_id:
        sql = """
        SELECT
            h.pass_id,
            h.source_file,
            h.satellite,
            h.pipeline,
            h.frequency_hz,
            h.bandwidth_hz,
            h.gain,
            h.source_id,
            h.bias_t,
            h.pass_start,
            h.pass_end,
            h.scheduled_start,
            h.scheduled_end,
            h.sample_count,
            h.visible_sample_count,
            h.start_azimuth_deg,
            h.mid_azimuth_deg,
            h.end_azimuth_deg,
            h.max_elevation_deg,
            h.direction,
            h.first_deframer_sync_delay_seconds,
            h.total_deframer_synced_seconds,
            h.sync_drop_count,
            h.median_snr_synced,
            h.median_ber_synced,
            h.peak_snr_db,
            h.imported_at,
            s.setup_id,
            s.antenna_type,
            s.antenna_location,
            s.antenna_orientation,
            s.lna,
            s.rf_filter,
            s.feedline,
            s.raspberry_pi,
            s.power_supply,
            s.additional_info
        FROM pass_header h
        JOIN setup s ON h.setup_id = s.setup_id
        WHERE h.pass_id = ?
        """
        return list(conn.execute(sql, (args.pass_id,)).fetchall())

    sql = """
    SELECT
        h.pass_id,
        h.source_file,
        h.satellite,
        h.pipeline,
        h.frequency_hz,
        h.bandwidth_hz,
        h.gain,
        h.source_id,
        h.bias_t,
        h.pass_start,
        h.pass_end,
        h.scheduled_start,
        h.scheduled_end,
        h.sample_count,
        h.visible_sample_count,
        h.start_azimuth_deg,
        h.mid_azimuth_deg,
        h.end_azimuth_deg,
        h.max_elevation_deg,
        h.direction,
        h.first_deframer_sync_delay_seconds,
        h.total_deframer_synced_seconds,
        h.sync_drop_count,
        h.median_snr_synced,
        h.median_ber_synced,
        h.peak_snr_db,
        h.imported_at,
        s.setup_id,
        s.antenna_type,
        s.antenna_location,
        s.antenna_orientation,
        s.lna,
        s.rf_filter,
        s.feedline,
        s.raspberry_pi,
        s.power_supply,
        s.additional_info
    FROM pass_header h
    JOIN setup s ON h.setup_id = s.setup_id
    """

    params: list[Any] = []
    where = []

    if args.satellite:
        where.append("h.satellite = ?")
        params.append(args.satellite)

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY h.pass_start DESC"

    if args.latest:
        sql += " LIMIT ?"
        params.append(args.latest)

    return list(conn.execute(sql, params).fetchall())

def query_pass_details(conn: sqlite3.Connection, pass_id: str) -> list[sqlite3.Row]:
    sql = """
    SELECT
        timestamp,
        snr_db,
        peak_snr_db,
        ber,
        viterbi_state,
        deframer_state,
        azimuth_deg,
        elevation_deg
    FROM pass_detail
    WHERE pass_id = ?
    ORDER BY timestamp
    """
    return list(conn.execute(sql, (pass_id,)).fetchall())

def fmt(value, digits=2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def find_plot_paths(base_dir: str, pass_id: str) -> tuple[str | None, str | None]:
    captures_dir = os.path.join(base_dir, "results", "captures", pass_id)
    skyplot = os.path.join(captures_dir, f"{pass_id}-skyplot.png")
    timeseries = os.path.join(captures_dir, f"{pass_id}-timeseries.png")

    skyplot_path = skyplot if os.path.exists(skyplot) else None
    timeseries_path = timeseries if os.path.exists(timeseries) else None
    return skyplot_path, timeseries_path


def make_info_table(row: sqlite3.Row, col_widths: list[float]):
    data = [
        ["Field", "Value"],
        ["Pass ID", row["pass_id"]],
        ["Satellite", row["satellite"]],
        ["Pipeline", row["pipeline"]],
        ["Frequency (Hz)", fmt(row["frequency_hz"], 0)],
        ["Bandwidth (Hz)", fmt(row["bandwidth_hz"], 0)],
        ["Gain", fmt(row["gain"], 1)],
        ["Source ID", fmt(row["source_id"])],
        ["Bias-T", "True" if row["bias_t"] else "False"],
        ["Pass start", fmt(row["pass_start"])],
        ["Pass end", fmt(row["pass_end"])],
        ["Scheduled start", fmt(row["scheduled_start"])],
        ["Scheduled end", fmt(row["scheduled_end"])],
        ["Sample count", fmt(row["sample_count"], 0)],
        ["Visible samples", fmt(row["visible_sample_count"], 0)],
        ["Start azimuth", fmt(row["start_azimuth_deg"], 3)],
        ["Mid azimuth", fmt(row["mid_azimuth_deg"], 3)],
        ["End azimuth", fmt(row["end_azimuth_deg"], 3)],
        ["Max elevation", fmt(row["max_elevation_deg"], 3)],
        ["Direction", fmt(row["direction"])],
        ["First deframer sync delay (s)", fmt(row["first_deframer_sync_delay_seconds"], 1)],
        ["Deframer synced total (s)", fmt(row["total_deframer_synced_seconds"], 1)],
        ["Sync drop count", fmt(row["sync_drop_count"], 0)],
        ["Median SNR synced", fmt(row["median_snr_synced"], 6)],
        ["Median BER synced", fmt(row["median_ber_synced"], 6)],
        ["Peak SNR", fmt(row["peak_snr_db"], 6)],
        ["Imported at", fmt(row["imported_at"])],
    ]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#D9E2F3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEADING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#A6A6A6")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def make_setup_table(row: sqlite3.Row, col_widths: list[float]):
    data = [
        ["Setup field", "Value"],
        ["Setup ID", fmt(row["setup_id"], 0)],
        ["Antenna type", fmt(row["antenna_type"])],
        ["Antenna location", fmt(row["antenna_location"])],
        ["Antenna orientation", fmt(row["antenna_orientation"])],
        ["LNA", fmt(row["lna"])],
        ["RF filter", fmt(row["rf_filter"])],
        ["Feedline", fmt(row["feedline"])],
        ["Raspberry Pi", fmt(row["raspberry_pi"])],
        ["Power supply", fmt(row["power_supply"])],
        ["Additional info", fmt(row["additional_info"])],
    ]

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E2F0D9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEADING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#A6A6A6")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table

def make_raw_data_table(rows: list[sqlite3.Row]):
    data = [[
        "Timestamp",
        "SNR",
        "Peak SNR",
        "BER",
        "Viterbi",
        "Deframer",
        "Azimuth",
        "Elevation",
    ]]

    for row in rows:
        data.append([
            fmt(row["timestamp"]),
            fmt(row["snr_db"], 6),
            fmt(row["peak_snr_db"], 6),
            fmt(row["ber"], 6),
            fmt(row["viterbi_state"]),
            fmt(row["deframer_state"]),
            fmt(row["azimuth_deg"], 3),
            fmt(row["elevation_deg"], 3),
        ])

    table = LongTable(
        data,
        colWidths=[38 * mm, 20 * mm, 22 * mm, 20 * mm, 25 * mm, 25 * mm, 20 * mm, 20 * mm],
        repeatRows=1,
    )

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#FCE4D6")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.5),
        ("LEADING", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#A6A6A6")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]

    # Viterbi = Spalte 4, Deframer = Spalte 5
    for idx, row in enumerate(rows, start=1):
        viterbi = str(row["viterbi_state"] or "")
        deframer = str(row["deframer_state"] or "")

        if viterbi == "SYNCED":
            v_bg = colors.HexColor("#C6E0B4")   # grün
        elif viterbi == "SYNCING":
            v_bg = colors.HexColor("#FFF2CC")   # gelb
        else:
            v_bg = colors.HexColor("#F4CCCC")   # rot

        if deframer == "SYNCED":
            d_bg = colors.HexColor("#C6E0B4")   # grün
        elif deframer == "SYNCING":
            d_bg = colors.HexColor("#FFF2CC")   # gelb
        else:
            d_bg = colors.HexColor("#F4CCCC")   # rot

        style_cmds.append(("BACKGROUND", (4, idx), (4, idx), v_bg))
        style_cmds.append(("BACKGROUND", (5, idx), (5, idx), d_bg))
        style_cmds.append(("TEXTCOLOR", (4, idx), (4, idx), colors.black))
        style_cmds.append(("TEXTCOLOR", (5, idx), (5, idx), colors.black))
        style_cmds.append(("FONTNAME", (4, idx), (4, idx), "Helvetica-Bold"))
        style_cmds.append(("FONTNAME", (5, idx), (5, idx), "Helvetica-Bold"))

    table.setStyle(TableStyle(style_cmds))
    return table

def build_story(rows: list[sqlite3.Row], base_dir: str, conn: sqlite3.Connection):
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        spaceAfter=6,
        textColor=colors.HexColor("#1F1F1F"),
    )
    subtitle_style = ParagraphStyle(
        "SubtitleCustom",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        spaceBefore=2,
        spaceAfter=5,
        textColor=colors.HexColor("#404040"),
    )
    small_style = ParagraphStyle(
        "SmallCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#404040"),
    )

    story = []

    for idx, row in enumerate(rows):
        pass_id = row["pass_id"]
        raw_rows = query_pass_details(conn, pass_id)
        skyplot_path, timeseries_path = find_plot_paths(base_dir, pass_id)

        story.append(Paragraph(f"Reception report - {pass_id}", title_style))
        story.append(Paragraph(
            f"Satellite: {row['satellite']} | Pipeline: {row['pipeline']} | Gain: {fmt(row['gain'], 1)}",
            small_style,
        ))
        story.append(Spacer(1, 4 * mm))

        left_width = 96 * mm
        right_width = 165 * mm
        top_block = Table(
            [[
                make_info_table(row, [36 * mm, 60 * mm]),
                Image(skyplot_path, width=right_width, height=right_width * 0.72)
                if skyplot_path else Paragraph("Skyplot not found", small_style),
            ]],
            colWidths=[left_width, right_width],
        )
        top_block.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(top_block)

        story.append(Spacer(1, 5 * mm))
        story.append(PageBreak())
        story.append(Paragraph("RECEPTION SETUP", subtitle_style))
        story.append(make_setup_table(row, [40 * mm, 223 * mm]))

        story.append(PageBreak())
        story.append(Paragraph("RECEPTION TIME SERIES", subtitle_style))
        if timeseries_path:
            story.append(Image(timeseries_path, width=257 * mm, height=257 * mm * 0.48))
        else:
            story.append(Paragraph("Timeseries plot not found", small_style))

        if raw_rows:
            story.append(PageBreak())
            story.append(Paragraph(f"RAW RECEPTION SAMPLES - {pass_id}", title_style))
            story.append(Paragraph(
                f"Sample count: {len(raw_rows)}",
                small_style,
            ))
            story.append(Spacer(1, 3 * mm))
            story.append(make_raw_data_table(raw_rows))

        if idx < len(rows) - 1:
            story.append(PageBreak())

    return story

def main() -> int:
    args = parse_args()
    config_path = get_config_path(args.config)

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"[export_reception_report_pdf] CONFIG ERROR: {e}")
        return 1

    if not config["reception_db"]["enabled"]:
        print("[export_reception_report_pdf] reception_db disabled in config")
        return 1

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = config["reception_db"]["db_path"]

    if not os.path.exists(db_path):
        print(f"[export_reception_report_pdf] database not found: {db_path}")
        return 1

    conn = open_db(db_path)
    try:
        rows = query_passes(conn, args)

        if not rows:
            print("[export_reception_report_pdf] no matching passes found")
            return 1

        output_path = build_output_path(base_dir, args)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=landscape(A4),
            leftMargin=6 * mm,
            rightMargin=6 * mm,
            topMargin=8 * mm,
            bottomMargin=8 * mm,
            title="SATPI Reception Report",
            author="satpi",
        )

        story = build_story(rows, base_dir, conn)
        doc.build(story)
    finally:
        conn.close()

    print(f"[export_reception_report_pdf] created: {output_path}")
    print(f"[export_reception_report_pdf] pass count: {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main()) 

