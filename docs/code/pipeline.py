"""
pipeline.py  -  geometry-first table extraction for digital (born-PDF) trustee reports.

    python pipeline.py report.pdf  out/  [first_page last_page]     (1-based PDF page numbers)

NO coordinates are configured anywhere. Every box and line you see in the overlay is
*computed* from two things PyMuPDF gives us for free on a digital PDF:

    page.get_text("words")  ->  [(x0, y0, x1, y1, "text", ...), ...]   one box per word
    page.get_drawings()     ->  every line / rectangle the PDF itself paints

Coordinates are PDF points (1/72 inch), origin top-left, y grows downward.
A landscape A4 page is 841 x 595 points.

STAGES (each is one function below, in this order):
  S1  find_chrome()      text repeated at the same spot on most pages (logo, title bar, footer)
  S2  load_page()        words for this page minus chrome, + drawn rules + filled shapes
  S3  xy_cut()           recursively split the page along whitespace -> regions (blocks)
  S4  attach_headers()   glue small blocks (titles / header lines) onto the block below
  S5  build_table()      inside one region: rows (y clustering) + columns (x gutters) -> DataFrame
  S6  classify()         table / key-value / chart labels / empty-table / text
  S7  stitch()           same title + same header on consecutive pages -> one logical table
  S8  validate()         Total / Subtotal rows must equal the sum of the rows they close
  S9  draw_overlay()     draw the computed boxes + column lines back onto the PDF (debug)
  S10 export()           Excel workbook (one sheet per logical table) + JSON report
"""
from __future__ import annotations

import json
import re
import statistics as st
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pymupdf

# --------------------------------------------------------------------------------------
# Small data structures
# --------------------------------------------------------------------------------------
Word = dict  # {"x0","y0","x1","y1","t"}


@dataclass
class Region:
    page: int                     # 1-based PDF page number
    words: list[Word]
    bbox: tuple = ()
    col_cuts: list[float] = field(default_factory=list)   # x positions of column separators
    row_ys: list[float] = field(default_factory=list)     # y centre of every text row
    df: pd.DataFrame | None = None
    kind: str = ""
    title: str = ""               # page section title, e.g. "Asset Information I"
    n_header_rows: int = 0


# --------------------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------------------
def bbox(ws):
    return (min(w["x0"] for w in ws), min(w["y0"] for w in ws),
            max(w["x1"] for w in ws), max(w["y1"] for w in ws))


def cy(w):  # vertical centre of a word
    return (w["y0"] + w["y1"]) / 2


def cx(w):  # horizontal centre of a word
    return (w["x0"] + w["x1"]) / 2


def line_height(ws):
    return st.median(w["y1"] - w["y0"] for w in ws)


def empty_runs(intervals, lo, hi):
    """Given [(start,end),...] on one axis, return the EMPTY stretches strictly inside (lo,hi).
    Projecting every word onto the y-axis and asking for empty runs = horizontal white bands.
    Projecting onto the x-axis = vertical white gutters."""
    out, cur = [], lo
    for a, b in sorted(intervals):
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    return [(a, b) for a, b in out if a > lo and b < hi]


def group_rows(ws):
    """Cluster words into text rows: words whose vertical centres are within half a line."""
    if not ws:
        return []
    tol = line_height(ws) * 0.5
    rows = []
    for w in sorted(ws, key=cy):
        if rows and abs(cy(w) - rows[-1]["c"]) <= tol:
            rows[-1]["ws"].append(w)
            rows[-1]["c"] = st.mean(cy(v) for v in rows[-1]["ws"])
        else:
            rows.append({"c": cy(w), "ws": [w]})
    for r in rows:
        r["ws"].sort(key=lambda w: w["x0"])
    return rows


def row_alignment(left, right, tol):
    """Fraction of rows on the smaller side that have a row at the same height on the other side.
    ~1.0 -> the two sides are columns of ONE table.   low -> two different blocks side by side."""
    a = [r["c"] for r in group_rows(left)]
    b = [r["c"] for r in group_rows(right)]
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return sum(any(abs(x - y) <= tol for y in big) for x in small) / len(small)


# --------------------------------------------------------------------------------------
# S1  page chrome = what repeats at the same place on most pages
# --------------------------------------------------------------------------------------
def word_key(x0, y0, t):
    return (t, round(x0 / 4), round(y0 / 4))   # 4pt buckets tolerate tiny jitter


def find_chrome(doc, min_share=0.5, top=0.12, bottom=0.92):
    cnt = Counter()
    for p in doc:
        h = p.rect.height
        cnt.update({word_key(x0, y0, t)
                    for x0, y0, x1, y1, t, *_ in p.get_text("words")
                    if y1 < top * h or y0 > bottom * h})       # only page bands can be chrome
    return {k for k, c in cnt.items() if c >= min_share * len(doc)}


# --------------------------------------------------------------------------------------
# S2  load one page
# --------------------------------------------------------------------------------------
def load_page(page, chrome, top=0.12):
    raw = page.get_text("words")
    h = page.rect.height
    # footer line = the line carrying the copyright mark (drop the whole line)
    foot = [(w[1] + w[3]) / 2 for w in raw if w[4].startswith("©")]
    words, removed, title_words = [], [], []
    for x0, y0, x1, y1, t, *_ in raw:
        w = dict(x0=x0, y0=y0, x1=x1, y1=y1, t=t)
        if word_key(x0, y0, t) in chrome or any(abs(cy(w) - f) < 4 for f in foot):
            removed.append(w)
        elif y1 < top * h:
            title_words.append(w)           # non-repeating text in the title bar = section title
        else:
            words.append(w)
    title = " ".join(w["t"] for r in group_rows(title_words) for w in r["ws"])

    H, V, fills, boxes = [], [], [], []
    for d in page.get_drawings():
        if d["rect"].width > 100 and d["rect"].height > 100:
            boxes.append(d["rect"])
        if d.get("fill") and d["rect"].width > 4 and d["rect"].height > 4:
            fills.append(d["rect"])
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                r = pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))
            elif it[0] == "re":
                r = it[1]
            else:
                continue
            if r.height < 3 and r.width > 30:
                H.append(r)                                   # horizontal rule
            elif r.width < 3 and r.height > 30:
                V.append(r)                                   # vertical rule
            elif it[0] == "re" and r.width > 30 and r.height > 30 and not d.get("fill"):
                # outlined box: its four sides are separators
                H += [pymupdf.Rect(r.x0, r.y0, r.x1, r.y0), pymupdf.Rect(r.x0, r.y1, r.x1, r.y1)]
                V += [pymupdf.Rect(r.x0, r.y0, r.x0, r.y1), pymupdf.Rect(r.x1, r.y0, r.x1, r.y1)]
    return dict(words=words, removed=removed, title=title, H=H, V=V, fills=fills, boxes=boxes)


# --------------------------------------------------------------------------------------
# S3  recursive XY-cut
# --------------------------------------------------------------------------------------
def xy_cut(ws, H, V, depth=0, out=None, log=None):
    """Split `ws` in two along the best whitespace cut, then recurse into both halves.
    `log` (optional list) records every cut so explain.py can draw them in order."""
    out = [] if out is None else out
    if len(ws) < 2 or depth > 12:
        if ws:
            out.append(ws)
        return out
    x0, y0, x1, y1 = bbox(ws)
    lh = line_height(ws)
    rows = group_rows(ws)
    # "normal" whitespace between consecutive rows inside this block
    row_gaps = [min(w["y0"] for w in b["ws"]) - max(w["y1"] for w in a["ws"])
                for a, b in zip(rows, rows[1:])]
    normal_gap = max(st.median([g for g in row_gaps if g > 0] or [lh]), 1.0)

    candidates = []
    # --- horizontal cuts: a white band across the block, clearly taller than normal,
    #     or any white band that has a drawn rule spanning most of the block
    for a, b in empty_runs([(w["y0"], w["y1"]) for w in ws], y0, y1):
        ruled = any(r.y0 >= a - 1 and r.y1 <= b + 1 and r.x0 <= x0 + 0.3 * (x1 - x0)
                    and r.x1 >= x1 - 0.3 * (x1 - x0) for r in H)
        tall = (b - a) > max(2.2 * normal_gap, lh)
        if tall or (ruled and (b - a) > 0.8 * lh):
            score = (b - a) / normal_gap + (2 if ruled else 0)
            why = f"H gap {b - a:.0f}pt = {(b - a) / normal_gap:.1f}x normal" + (" + rule" if ruled else "")
            candidates.append((score, "h", (a + b) / 2, why))
    # --- vertical cuts: a white gutter top-to-bottom where rows on both sides DON'T line up
    #     (rows that line up = columns of the same table, so never cut there)
    header_band = not has_digit(ws) and len(rows) <= 4   # multi-line headers never line up
    for a, b in empty_runs([(w["x0"], w["x1"]) for w in ws], x0, x1):
        if b - a < 2 * lh or header_band:
            continue
        mid = (a + b) / 2
        L = [w for w in ws if w["x1"] <= mid]
        R = [w for w in ws if w["x0"] >= mid]
        ruled = any(a - 1 <= r.x0 <= b + 1 and min(r.y1, y1) - max(r.y0, y0) > 0.5 * (y1 - y0)
                    for r in V)
        align = row_alignment(L, R, tol=lh * 0.35)
        if ruled or align < 0.6:
            score = (b - a) / lh + (5 if ruled else 0) + (1 - align) * 5
            why = f"V gutter {b - a:.0f}pt, row-align {align:.2f}" + (" + rule" if ruled else "")
            candidates.append((score, "v", mid, why))

    if not candidates:
        out.append(ws)           # nothing left to cut: this is a final region
        return out
    _, kind, pos, why = max(candidates)
    if log is not None:
        log.append(dict(depth=depth, kind=kind, pos=pos, bbox=(x0, y0, x1, y1), why=why))
    key = cy if kind == "h" else cx
    first = [w for w in ws if key(w) < pos]
    second = [w for w in ws if key(w) >= pos]
    xy_cut(first, H, V, depth + 1, out, log)
    xy_cut(second, H, V, depth + 1, out, log)
    return out


# --------------------------------------------------------------------------------------
# S4  attach titles / header lines to the block below them
# --------------------------------------------------------------------------------------
def attach_headers(blocks):
    blocks = [b for b in blocks if b]
    changed = True
    while changed:
        changed = False
        blocks.sort(key=lambda b: bbox(b)[1])
        for i, a in enumerate(blocks):
            if len(group_rows(a)) > 5 or has_digit(a):   # titles / headers carry no numbers
                continue
            ax0, _, ax1, ay1 = bbox(a)
            lh = line_height(a)
            for j, b in enumerate(blocks):
                btxt = " ".join(w["t"] for w in b).lower()
                if j == i or not (len(group_rows(b)) > len(group_rows(a)) or has_digit(b)
                                  or btxt.startswith("there are no")):
                    continue
                bx0, by0, bx1, _ = bbox(b)
                overlap = max(0, min(ax1, bx1) - max(ax0, bx0)) / max(min(ax1 - ax0, bx1 - bx0), 1)
                # a sits above b, or beside b's first lines (a header cell split off), and over it
                if ay1 <= by0 + 4 * lh and by0 - ay1 < 5 * lh and overlap > 0.6:
                    blocks[j] = a + b
                    del blocks[i]
                    changed = True
                    break
            if changed:
                break
    # totals line(s) printed a bit below the table (often under a double rule) -> attach upward
    changed = True
    while changed:
        changed = False
        blocks.sort(key=lambda b: bbox(b)[1])
        for i, a in enumerate(blocks):
            text = " ".join(w["t"] for w in a)
            if len(group_rows(a)) > 2 or not TOTAL_RE.search(text):
                continue
            ax0, ay0, ax1, _ = bbox(a)
            lh = line_height(a)
            above = [j for j, b in enumerate(blocks) if j != i and len(group_rows(b)) > 2
                     and 0 <= ay0 - bbox(b)[3] < 4 * lh
                     and min(ax1, bbox(b)[2]) - max(ax0, bbox(b)[0]) > 0.5 * (ax1 - ax0)]
            if above:
                j = max(above, key=lambda j: bbox(blocks[j])[3])
                blocks[j] = blocks[j] + a
                del blocks[i]
                changed = True
                break
    return blocks


# --------------------------------------------------------------------------------------
# S5  region -> table
# --------------------------------------------------------------------------------------
def ink_coverage(rows, x0, x1, step=0.5):
    """For every x (in `step` pt bins) count how many rows have a word covering it.
    Columns of a table show up as tall plateaus, gutters as valleys near zero."""
    n = int((x1 - x0) / step) + 2
    cover = [0] * n
    for r in rows:
        hit = set()
        for w in r["ws"]:
            hit.update(range(int((w["x0"] - x0) / step), int((w["x1"] - x0) / step) + 1))
        for i in hit:
            if 0 <= i < n:
                cover[i] += 1
    return cover


def find_gutters(rows, x0, x1, min_w, tolerance, step=0.5):
    """Valleys of the coverage profile: x-ranges with ink in <= tolerance*rows rows."""
    cover = ink_coverage(rows, x0, x1, step)

    def runs(limit):
        res, start = [], None
        for i, c in enumerate(cover + [10**9]):
            if c <= limit and start is None:
                start = i
            elif c > limit and start is not None:
                res.append((x0 + start * step, x0 + i * step))
                start = None
        return res

    out = []
    for a, b in runs(tolerance * len(rows)):
        if b - a >= min_w and a > x0 and b < x1:
            out.append((a, b))
        elif (a <= x0 or b >= x1) and tolerance > 0:
            # the tolerant valley runs into the region edge: that "valley" is really a
            # sparse first/last column (e.g. 'Reason for Trade' filled in 2 of 24 rows).
            # Inside it, use strictly empty gaps instead.
            out += [(p, q) for p, q in runs(0) if p >= a and q <= b and q - p >= min_w
                    and p > x0 and q < x1]
    return sorted(out)


def has_digit(ws):
    return any(ch.isdigit() for w in ws for ch in w["t"])


def build_table(region: Region):
    msg = [r for r in group_rows(region.words)
           if " ".join(w["t"] for w in r["ws"]).lower().startswith("there are no")]
    ws = [w for w in region.words if not any(w in r["ws"] for r in msg)] or region.words
    rows = group_rows(ws)
    lh = line_height(ws)
    x0, _, x1, _ = bbox(ws)

    # 1) COLUMNS: gutters = x-ranges empty in ~all rows. 12% tolerance lets a spanning title,
    #    a section label or a wrapped line cross a gutter without destroying it.
    def single_phrase(r):   # titles, section labels, "There are no ... to report on."
        ws_ = r["ws"]
        return all(b["x0"] - a["x1"] < lh * 0.6 for a, b in zip(ws_, ws_[1:]))
    grows = [r for r in rows if not single_phrase(r)]
    if len(grows) < 2:
        grows = rows
    tol = 0.12 if len(grows) >= 8 else 0.0
    gut = find_gutters(grows, x0, x1, min_w=lh * 0.55, tolerance=tol)
    cuts = [x0 - 1] + [(a + b) / 2 for a, b in gut] + [x1 + 1]     # separator = gutter middle
    ncol = len(cuts) - 1

    def col_of(x):
        return next(k for k in range(ncol) if cuts[k] <= x < cuts[k + 1])

    def to_cells(row_ws):
        cells = [""] * ncol
        for w in row_ws:
            k = col_of(cx(w))
            cells[k] = (cells[k] + " " + w["t"]).strip()
        return cells

    # 2) HEADER: the rows above the first row that contains a digit
    n_hdr = next((i for i, r in enumerate(rows) if has_digit(r["ws"])), len(rows))
    if n_hdr == len(rows) and ncol < 3 and len(rows) > 2:
        n_hdr = 0          # digit-free block with 1-2 columns = plain text, not a header
    header = [""] * ncol
    for r in rows[:n_hdr]:
        # split the header line into phrases; a phrase that spans several columns
        # ("Fitch Rating" over Original | Current) is added to every column it covers
        phrases, cur = [], []
        for w in r["ws"]:
            if cur and w["x0"] - cur[-1]["x1"] > lh * 0.6:
                phrases.append(cur)
                cur = []
            cur.append(w)
        if cur:
            phrases.append(cur)
        for ph in phrases:
            a, b = ph[0]["x0"], ph[-1]["x1"]
            txt = " ".join(w["t"] for w in ph)
            spans = [k for k in range(ncol) if min(b, cuts[k + 1]) - max(a, cuts[k]) > 0.25 * (b - a)]
            for k in spans or [col_of((a + b) / 2)]:
                header[k] = (header[k] + " " + txt).strip()

    # 3) BODY: a line with no digits, few cells, right under the previous row = wrapped text
    body, prev_c = [], None
    for r in rows[n_hdr:]:
        cells = to_cells(r["ws"])
        filled = sum(bool(c) for c in cells)
        if (body and not has_digit(r["ws"]) and filled <= max(1, 0.3 * ncol)
                and r["c"] - prev_c < 1.6 * lh):
            body[-1] = [(p + " " + c).strip() for p, c in zip(body[-1], cells)]
            prev_c = r["c"]
            continue
        body.append(cells)
        prev_c = r["c"]

    # 4) SECTION LABELS: a last header line that only fills column 0 is really a section label
    if n_hdr > 1 and all(cx(w) < cuts[1] for w in rows[n_hdr - 1]["ws"]):
        sec = " ".join(w["t"] for w in rows[n_hdr - 1]["ws"])
        header[0] = header[0].replace(sec, "").strip()
        body.insert(0, [sec] + [""] * (ncol - 1))
    #    rows with only column 0 filled and no digits -> become a "_section" column
    table, section = [], ""
    for cells in body:
        if cells[0] and not any(cells[1:]) and not any(ch.isdigit() for ch in cells[0]):
            section = cells[0]
            continue
        table.append(cells + [section])

    cols = [h or f"col{k}" for k, h in enumerate(header)] + ["_section"]
    cols = [c if cols.count(c) == 1 else f"{c}_{k}" for k, c in enumerate(cols)]   # unique
    df = pd.DataFrame(table, columns=cols)
    if not df["_section"].any():
        df = df.drop(columns="_section")
    region.df, region.col_cuts, region.n_header_rows = df, cuts, n_hdr
    region.row_ys = [r["c"] for r in rows]
    return region


# --------------------------------------------------------------------------------------
# S6  classify
# --------------------------------------------------------------------------------------
NUM_RE = re.compile(r"^\(?-?[\d,]*\.?\d+%?\)?$")


def to_number(v):
    """'1,234.50' -> 1234.5 ; '(5,740,986.87)' -> -5740986.87 ; '1,884.29 EUR [A]' -> 1884.29"""
    if v is None:
        return None
    s = re.sub(r"\s*(EUR|USD|GBP)\b|\[[A-Z]\]|\s", "", str(v))
    if not s or not NUM_RE.match(s) or s.endswith("%"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return None


def classify(region: Region, fills, boxes=()):
    df = region.df
    x0, y0, x1, y1 = region.bbox
    centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
    near = pymupdf.Rect(x0 - 40, y0 - 40, x1 + 40, y1 + 40)
    in_chart_box = any(b.contains(centre) and sum(b.contains(f) for f in fills) >= 4 for b in boxes)
    if in_chart_box or sum(near.contains(f) for f in fills) >= 4:
        return "chart-labels"            # sits on / inside lots of coloured shapes (bars, slices)
    text = " ".join(w["t"] for w in region.words).lower()
    if "there are no" in text:
        return "empty-table"
    vals = [v for v in df.values.ravel() if v]
    if df.shape[0] >= 1 and df.shape[1] >= 2:
        nums = sum(to_number(v) is not None or str(v).endswith("%") for v in vals)
        return "table" if nums / max(len(vals), 1) > 0.15 else "text-table"
    return "text"


# --------------------------------------------------------------------------------------
# S7  stitch tables that continue over several pages
# --------------------------------------------------------------------------------------
@dataclass
class LogicalTable:
    name: str
    pages: list[int]
    df: pd.DataFrame
    checks: list[dict] = field(default_factory=list)
    note: str = ""


def header_similarity(a, b):
    generic = lambda cols: all(re.fullmatch(r"col\d+(_\d+)?|_section", c) for c in cols)
    if generic(a) or generic(b):
        return 1.0          # a continuation page without its own header matches anything
    a, b = set(a) - {"_section"}, set(b) - {"_section"}
    return len(a & b) / max(len(a | b), 1)


def data_cols(df):
    return [c for c in df.columns if c != "_section"]


def section_like(cols):
    """Header where only the first cell is real text, e.g. ['Coverage Tests','col1','col2']:
    that 'header' is actually a section label of the table above."""
    return len(cols) > 1 and all(re.fullmatch(r"col\d+(_\d+)?", c) for c in cols[1:]) \
        and not re.fullmatch(r"col\d+", cols[0])


def stitch(regions: list[Region]):
    """Join regions that are pieces of one logical table:
       - same page title, on the same page (section below) or on the next page, and
       - same number of data columns, and
       - same-ish header, or no real header (continuation page / section block)."""
    tables: list[LogicalTable] = []
    for r in regions:
        if r.kind not in ("table", "text-table", "empty-table"):
            continue
        same_title = [t for t in tables if t.name.split(" #")[0] == r.title]
        if r.df.empty and same_title and r.kind == "empty-table":   # "There are no ..." line
            same_title[-1].note = " ".join(w["t"] for w in r.words)
            continue
        rc = data_cols(r.df)
        cands = []
        for t in same_title:
            if len(data_cols(t.df)) != len(rc):
                continue
            # same page: only a section block below; side-by-side tables are never joined
            if not (t.pages[-1] == r.page - 1 or (t.pages[-1] == r.page and section_like(rc))):
                continue
            sim = 1.0 if section_like(rc) else header_similarity(data_cols(t.df), rc)
            if sim >= 0.5:
                cands.append((sim, t))
        if not cands:
            n = len(same_title)
            note = ""
            if r.kind == "empty-table":
                txt = " ".join(w["t"] for w in r.words)
                note = txt[txt.lower().find("there are no"):]
            tables.append(LogicalTable(r.title + (f" #{n + 1}" if n else ""), [r.page], r.df.copy(), note=note))
            continue
        t = max(cands, key=lambda c: c[0])[1]
        part = r.df.copy()
        if section_like(rc):
            sec = rc[0]
            part = part.rename(columns=dict(zip(rc, data_cols(t.df))))
            part["_section"] = sec
        else:
            part = part.rename(columns=dict(zip(rc, data_cols(t.df))))
            if "_section" not in part and "_section" in t.df:
                part["_section"] = t.df["_section"].iloc[-1]   # section carries over the page break
        if "_section" in part and "_section" not in t.df:
            t.df["_section"] = ""
        # identical repeated block (e.g. a summary box printed on every page) -> keep once
        tail = t.df.tail(len(part)).reset_index(drop=True)
        if len(tail) == len(part) and tail[data_cols(t.df)].equals(part[data_cols(t.df)].reset_index(drop=True)):
            continue
        t.df = pd.concat([t.df, part[t.df.columns]], ignore_index=True)
        if r.page not in t.pages:
            t.pages.append(r.page)
    return tables


# --------------------------------------------------------------------------------------
# S8  validate: every Total / Subtotal row must equal the rows it closes
# --------------------------------------------------------------------------------------
TOTAL_RE = re.compile(r"\b(?:sub)?total\b", re.I)


def validate(t: LogicalTable):
    df = t.df
    label_cols = [c for c in df.columns if df[c].astype(str).str.contains(TOTAL_RE).any()]
    if not label_cols:
        return
    is_total = df[label_cols].astype(str).apply(lambda s: s.str.contains(TOTAL_RE)).any(axis=1)
    for col in df.columns:
        nums = df[col].map(to_number)
        if nums.notna().sum() < 2 or col in label_cols:
            continue
        block, prev_totals = 0.0, []
        for i in df.index:
            v = nums[i]
            if is_total[i]:
                if v is None or pd.isna(v):
                    continue
                label = " ".join(str(df.at[i, c]) for c in label_cols).strip()
                options = {"rows since last total": block,
                           "previous total + rows since": (prev_totals[-1] if prev_totals else 0) + block,
                           "sum of earlier totals": sum(prev_totals)}
                ok = [k for k, s in options.items() if abs(s - v) < 0.015 and (s or v == 0)]
                if not ok and not re.search(r"sub|grand|:", label, re.I):
                    block += v            # e.g. "Total Cash" is a line item, not a total
                    continue
                t.checks.append(dict(table=t.name, column=col, row=label, printed=v,
                                     computed=round(options["rows since last total"], 2),
                                     status="PASS (" + ok[0] + ")" if ok else "FAIL"))
                prev_totals.append(v)
                block = 0.0
            elif v is not None and not pd.isna(v):
                block += v


# --------------------------------------------------------------------------------------
# S9  draw what we computed back onto the PDF page
# --------------------------------------------------------------------------------------
COLORS = [(0.85, 0.1, 0.1), (0, 0.55, 0), (0, 0.2, 0.9), (0.85, 0.45, 0), (0.6, 0, 0.6), (0, 0.55, 0.6)]


def draw_overlay(page, regions: list[Region]):
    """Everything here is page.draw_*(coordinates we computed). Nothing is detected from pixels."""
    for i, r in enumerate(regions):
        col = COLORS[i % len(COLORS)]
        x0, y0, x1, y1 = r.bbox
        # region box (solid)
        page.draw_rect(pymupdf.Rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3), color=col, width=1.4)
        # column separators (dashed) = middle of every gutter found by find_gutters()
        for c in r.col_cuts[1:-1]:
            page.draw_line((c, y0 - 2), (c, y1 + 2), color=col, width=0.5, dashes="[2] 2")
        # header / body boundary (dotted, horizontal)
        if r.n_header_rows and r.n_header_rows < len(r.row_ys):
            yb = (r.row_ys[r.n_header_rows - 1] + r.row_ys[r.n_header_rows]) / 2
            page.draw_line((x0 - 3, yb), (x1 + 3, yb), color=col, width=0.6, dashes="[1] 1")
        label = f"R{i} {r.kind} {r.df.shape[0]}x{r.df.shape[1]}"
        page.insert_text((x0, y0 - 5), label, fontsize=6.5, color=col)


# --------------------------------------------------------------------------------------
# S10 run everything + export
# --------------------------------------------------------------------------------------
def run(pdf_path, out_dir, first=None, last=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    first = first or 1
    last = last or len(doc)
    chrome = find_chrome(doc)                                            # S1

    all_regions: list[Region] = []
    for pno in range(first, last + 1):
        page = doc[pno - 1]
        P = load_page(page, chrome)                                      # S2
        if not P["words"]:
            continue
        blocks = attach_headers(xy_cut(P["words"], P["H"], P["V"]))      # S3 + S4
        blocks.sort(key=lambda b: (round(bbox(b)[1] / 20), bbox(b)[0]))
        regions = []
        for b in blocks:
            r = Region(page=pno, words=b, bbox=bbox(b), title=P["title"])
            build_table(r)                                               # S5
            r.kind = classify(r, P["fills"], P["boxes"])                             # S6
            regions.append(r)
        draw_overlay(page, regions)                                      # S9
        all_regions += regions

    tables = stitch(all_regions)                                         # S7
    checks = []
    for t in tables:
        validate(t)                                                      # S8
        checks += t.checks

    # ---- exports
    doc.select(list(range(first - 1, last)))
    doc.save(out / "overlay.pdf")                                        # every page, annotated
    with pd.ExcelWriter(out / "tables.xlsx") as xw:
        index = pd.DataFrame([dict(sheet=f"T{i:02d}", table=t.name, pages=",".join(map(str, t.pages)),
                                   rows=len(t.df), cols=t.df.shape[1], note=t.note)
                              for i, t in enumerate(tables)])
        index.to_excel(xw, sheet_name="_index", index=False)
        pd.DataFrame(checks).to_excel(xw, sheet_name="_validation", index=False)
        for i, t in enumerate(tables):
            t.df.to_excel(xw, sheet_name=f"T{i:02d}", index=False)
    report = dict(
        regions=[dict(page=r.page, kind=r.kind, title=r.title, shape=list(r.df.shape),
                      bbox=[round(v, 1) for v in r.bbox],
                      column_separators_x=[round(c, 1) for c in r.col_cuts[1:-1]])
                 for r in all_regions],
        tables=[dict(name=t.name, pages=t.pages, shape=list(t.df.shape)) for t in tables],
        validation=checks)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    return all_regions, tables, checks


if __name__ == "__main__":
    pdf, outd = sys.argv[1], sys.argv[2]
    a = int(sys.argv[3]) if len(sys.argv) > 3 else None
    b = int(sys.argv[4]) if len(sys.argv) > 4 else None
    regions, tables, checks = run(pdf, outd, a, b)
    print(f"{len(regions)} regions -> {len(tables)} logical tables")
    for t in tables:
        print(f"  {t.name:45s} pages {t.pages}  {t.df.shape}")
    print(f"validation: {sum(c['status'].startswith('PASS') for c in checks)} pass / "
          f"{sum(c['status'] == 'FAIL' for c in checks)} fail")
    for c in checks:
        if c["status"] == "FAIL":
            print("  FAIL", c)
