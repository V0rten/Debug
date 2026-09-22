#!/usr/bin/env python3
"""kalibrasi7.py

Calibrate and repair Spaceman timestamps from a raw markdown table.

Usage:
    python kalibrasi7.py target.md

The only generated file is ``target_kalibrasi.md``.  The first 50 rows are
copied without timestamp changes.  Existing Epoch values (tag_ts) are never
changed; missing Epoch values are reconstructed only inside an anchored gap.
Non-Epoch timestamps are calibrated before missing values are filled.
"""
from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCKED_ROWS = 50
LOCAL_TZ = timezone(timedelta(hours=8))

# Robust windows used only to classify observations.  Repairs use the centre,
# never the nearest threshold edge.
TS_TO_TAG_TOL_MS = 300.0
TS_TO_DEAL_TOL_MS = 300.0
GR_TO_NEXT_EPOCH_TOL_MS = 250.0
GR_TO_NEXT_DEAL_TOL_MS = 300.0

DEFAULT_TAG_TO_TS_MS = 0.0
DEFAULT_TS_TO_DEAL_MS = 8_300.0
DEFAULT_GR_TO_NEXT_EPOCH_MS = 2_400.0
DEFAULT_GR_TO_NEXT_DEAL_MS = 9_900.0
K_FLIGHT = 0.0781
K_GAP = 0.0830
BASE_GAP_MS = 10_000.0

OUT_COLUMNS = ["tag_ts", "ts", "startdealing_ts", "gr_ts", "gr_result", "game_id", "status"]
ALIASES = {
    "tag_ts": ["tag_ts", "Ts Epoch Starttime", "epoch", "Epoch"],
    "ts": ["ts", "Ts Starttime (Bukan Epoch)", "starttime", "Starttime"],
    "startdealing_ts": ["startdealing_ts", "Ts Start Dealing", "startdealing"],
    "gr_ts": ["gr_ts", "Ts Gr", "gr"],
    "gr_result": ["gr_result", "Result Gr", "result", "mult"],
    "game_id": ["game_id", "Id Game", "id_game"],
    "status": ["status", "Status (Live/History)", "Status"],
}


def clean(value):
    value = "" if value is None else str(value).strip()
    return "" if value in {"", "-", "nan", "NaN", "None"} else value


def parse_epoch(value):
    value = clean(value)
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_number(value):
    value = clean(value).replace(",", ".")
    if not value:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def parse_clock(value, reference_ms=None):
    """Return absolute milliseconds, accepting date-time and HH:MM:SS[.sss]."""
    text = clean(value)
    if not text:
        return None
    try:
        if "T" in text or ("-" in text and " " in text):
            text = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LOCAL_TZ)
            return dt.timestamp() * 1000.0
        if " " in text:
            date_part, clock_part = text.split(None, 1)
            dt = datetime.fromisoformat(f"{date_part}T{clock_part}")
            return dt.replace(tzinfo=LOCAL_TZ).timestamp() * 1000.0
        parts = text.split(".", 1)
        h, m, sec = map(int, parts[0].split(":"))
        fraction = int(((parts[1] if len(parts) == 2 else "") + "000")[:3])
        day_ms = (h * 3600 + m * 60 + sec) * 1000 + fraction
        if reference_ms is None:
            return day_ms
        ref = datetime.fromtimestamp(reference_ms / 1000.0, tz=LOCAL_TZ)
        candidate = datetime.combine(ref.date(), datetime.min.time(), tzinfo=LOCAL_TZ).timestamp() * 1000 + day_ms
        candidates = [candidate + delta * 86_400_000 for delta in (-1, 0, 1)]
        return min(candidates, key=lambda x: abs(x - reference_ms))
    except (ValueError, TypeError, OverflowError):
        return None


def format_clock(value, reference_ms=None):
    if value is None or not math.isfinite(value):
        return ""
    dt = datetime.fromtimestamp(value / 1000.0, tz=LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if reference_ms is not None else dt.strftime("%H:%M:%S.%f")[:-3]


def median(values, fallback):
    values = [float(x) for x in values if x is not None and math.isfinite(x)]
    return statistics.median(values) if values else fallback


def header_map(header):
    result = {}
    lowered = {name.strip().lower(): i for i, name in enumerate(header)}
    for canonical, names in ALIASES.items():
        for name in names:
            if name.lower() in lowered:
                result[canonical] = lowered[name.lower()]
                break
    missing = [name for name in OUT_COLUMNS if name not in result]
    if missing:
        raise ValueError("Kolom tidak ditemukan: " + ", ".join(missing))
    return result


def read_table(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    header = None
    rows = []
    for line in lines:
        if "|" not in line:
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if header is None and any(x.lower() in {"tag_ts", "ts", "startdealing_ts"} for x in cells):
            header = cells
            continue
        if header is None or not cells or all(set(x) <= {"-", ":", " "} for x in cells):
            continue
        if len(cells) < len(header):
            continue
        rows.append(cells[: len(header)])
    if header is None:
        raise ValueError("Markdown table tidak ditemukan")
    mapping = header_map(header)
    normalized = []
    for cells in rows:
        normalized.append({key: clean(cells[index]) for key, index in mapping.items()})
    return normalized


def flight_ms(mult):
    if mult is None or mult <= 0:
        return None
    return (1000.0 / K_FLIGHT) * math.log(mult)


def gap_ms(mult):
    flight = None if mult is None else (1000.0 / K_GAP) * math.log(max(mult, 1.0))
    return BASE_GAP_MS + (flight or 0.0)


def calibrate(rows, epochs, locked):
    tag_to_ts, ts_to_deal = [], []
    gr_next_epoch, gr_next_deal = [], []
    for i in range(locked, len(rows)):
        e = epochs[i]
        ts = parse_clock(rows[i]["ts"], e)
        deal = parse_clock(rows[i]["startdealing_ts"], e)
        gr = parse_clock(rows[i]["gr_ts"], e)
        if e is not None and ts is not None:
            value = ts - e
            if -5_000 <= value <= 5_000:
                tag_to_ts.append(value)
        if ts is not None and deal is not None:
            value = deal - ts
            if 5_000 <= value <= 12_000:
                ts_to_deal.append(value)
        if gr is not None and i + 1 < len(rows) and epochs[i + 1] is not None:
            value = epochs[i + 1] - gr
            if 0 < value <= 6_000:
                gr_next_epoch.append(value)
            next_deal = parse_clock(rows[i + 1]["startdealing_ts"], epochs[i + 1])
            if next_deal is not None:
                value = next_deal - gr
                if 8_000 <= value <= 12_000:
                    gr_next_deal.append(value)
    return {
        "tag_to_ts": median(tag_to_ts, DEFAULT_TAG_TO_TS_MS),
        "ts_to_deal": median(ts_to_deal, DEFAULT_TS_TO_DEAL_MS),
        "gr_next_epoch": median(gr_next_epoch, DEFAULT_GR_TO_NEXT_EPOCH_MS),
        "gr_next_deal": median(gr_next_deal, DEFAULT_GR_TO_NEXT_DEAL_MS),
    }


def fill_internal_epochs(rows, epochs):
    valid = [i for i, value in enumerate(epochs) if value is not None]
    if not valid:
        return epochs
    first, last = min(valid), max(valid)
    for i in range(first + 1, last):
        if epochs[i] is not None:
            continue
        left = epochs[i - 1]
        right = epochs[i + 1] if i + 1 < len(epochs) else None
        left_candidate = left + gap_ms(parse_number(rows[i - 1]["gr_result"])) if left is not None else None
        right_candidate = right - gap_ms(parse_number(rows[i]["gr_result"])) if right is not None else None
        if left_candidate is not None and right_candidate is not None:
            epochs[i] = (left_candidate + right_candidate) / 2.0
        elif left_candidate is not None:
            epochs[i] = left_candidate
        elif right_candidate is not None:
            epochs[i] = right_candidate
    return epochs


def repair(rows):
    original_epochs = [parse_epoch(row["tag_ts"]) for row in rows]
    epochs = original_epochs[:]
    fill_internal_epochs(rows, epochs)  # leading/trailing remain unanchored
    cal = calibrate(rows, original_epochs, LOCKED_ROWS)
    absolute = {key: [None] * len(rows) for key in ("ts", "deal", "gr")}

    # First pass: ts. A raw ts inside the calibrated window is a local anchor.
    for i, row in enumerate(rows):
        if i < LOCKED_ROWS:
            continue
        e = epochs[i]
        raw_ts = parse_clock(row["ts"], e)
        if raw_ts is not None and e is not None and abs((raw_ts - e) - cal["tag_to_ts"]) <= TS_TO_TAG_TOL_MS:
            absolute["ts"][i] = raw_ts
        elif e is not None:
            absolute["ts"][i] = e + cal["tag_to_ts"]

    # Fill ts from calibrated neighbours when no Epoch exists in an internal gap.
    for i in range(LOCKED_ROWS, len(rows)):
        if absolute["ts"][i] is not None:
            continue
        left = absolute["ts"][i - 1] if i else None
        right = absolute["ts"][i + 1] if i + 1 < len(rows) else None
        if left is not None and right is not None:
            absolute["ts"][i] = (left + right) / 2.0

    # Start dealing: fixed relation to the calibrated local round anchor.
    for i, row in enumerate(rows):
        if i < LOCKED_ROWS or absolute["ts"][i] is None:
            continue
        raw = parse_clock(row["startdealing_ts"], epochs[i])
        expected = absolute["ts"][i] + cal["ts_to_deal"]
        if raw is not None and abs(raw - absolute["ts"][i] - cal["ts_to_deal"]) <= TS_TO_DEAL_TOL_MS:
            absolute["deal"][i] = raw
        else:
            absolute["deal"][i] = expected

    # Gr: fixed next-round constraints first; multiplier is fallback/validator.
    for i in range(LOCKED_ROWS, len(rows)):
        row = rows[i]
        next_i = i + 1 if i + 1 < len(rows) else None
        candidates = []
        if next_i is not None:
            if epochs[next_i] is not None:
                candidates.append(epochs[next_i] - cal["gr_next_epoch"])
            if absolute["deal"][next_i] is not None:
                candidates.append(absolute["deal"][next_i] - cal["gr_next_deal"])
        raw = parse_clock(row["gr_ts"], epochs[i])
        deal = absolute["deal"][i]
        mult = parse_number(row["gr_result"])
        dynamic = deal + flight_ms(mult) if deal is not None and flight_ms(mult) is not None else None
        if candidates:
            fixed = statistics.median(candidates)
            if raw is not None and deal is not None and raw > deal and abs(raw - fixed) <= max(GR_TO_NEXT_DEAL_TOL_MS, 500.0):
                absolute["gr"][i] = raw
            else:
                absolute["gr"][i] = fixed
        elif raw is not None and deal is not None and raw > deal:
            absolute["gr"][i] = raw
        elif dynamic is not None:
            absolute["gr"][i] = dynamic

    # One consistency pass: next-round anchors are stronger than a dynamic Gr.
    for i in range(LOCKED_ROWS, len(rows) - 1):
        if absolute["gr"][i] is None:
            continue
        candidates = []
        if epochs[i + 1] is not None:
            candidates.append(epochs[i + 1] - cal["gr_next_epoch"])
        if absolute["deal"][i + 1] is not None:
            candidates.append(absolute["deal"][i + 1] - cal["gr_next_deal"])
        if candidates and abs(absolute["gr"][i] - statistics.median(candidates)) > 700:
            absolute["gr"][i] = statistics.median(candidates)

    return epochs, absolute, cal, original_epochs


def write_output(rows, epochs, absolute, original_epochs, path):
    lines = ["# Hasil Kalibrasi Timestamp", "", "| " + " | ".join(OUT_COLUMNS) + " |", "| " + " | ".join(["---"] * len(OUT_COLUMNS)) + " |"]
    for i, row in enumerate(rows):
        tag = row["tag_ts"]
        if not tag and original_epochs[i] is None and epochs[i] is not None:
            tag = str(int(round(epochs[i])))
        ts = row["ts"] if i < LOCKED_ROWS else (format_clock(absolute["ts"][i], epochs[i]) if absolute["ts"][i] is not None else row["ts"])
        deal = row["startdealing_ts"] if i < LOCKED_ROWS else (format_clock(absolute["deal"][i], epochs[i]) if absolute["deal"][i] is not None else row["startdealing_ts"])
        gr = row["gr_ts"] if i < LOCKED_ROWS else (format_clock(absolute["gr"][i], epochs[i]) if absolute["gr"][i] is not None else row["gr_ts"])
        values = [tag, ts, deal, gr, row["gr_result"], row["game_id"], row["status"]]
        lines.append("| " + " | ".join(clean(x) for x in values) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python kalibrasi7.py target.md")
    source = Path(sys.argv[1])
    rows = read_table(source)
    epochs, absolute, _, original_epochs = repair(rows)
    output = source.with_name(source.stem + "_kalibrasi.md")
    write_output(rows, epochs, absolute, original_epochs, output)
    print(output)


if __name__ == "__main__":
    main()
