# Geometry-First Table Extraction for Trustee Reports — Complete Guide

This guide explains `pipeline.py` and `explain.py` in full:

- what every stage does and why it exists;
- what every threshold means;
- where the approach breaks;
- how to turn it into a production system for 250 companies across several trustees.

Read it with three files open side by side:

- `page_4.png` from `explain.py`;
- `overlay.pdf`;
- `pipeline.py`.

---

## Table of contents

1. [The core idea in one paragraph](#1-the-core-idea-in-one-paragraph)
2. [Why the previous approaches failed](#2-why-the-previous-approaches-failed)
3. [What a PDF actually contains (the raw material)](#3-what-a-pdf-actually-contains-the-raw-material)
4. [The coordinate system](#4-the-coordinate-system)
5. [Pipeline overview](#5-pipeline-overview)
6. [Stage by stage](#6-stage-by-stage)
   - S1 find_chrome
   - S2 load_page
   - S3 xy_cut
   - S4 attach_headers
   - S5 build_table
   - S6 classify
   - S7 stitch
   - S8 validate
   - S9 draw_overlay
   - S10 export
7. [How the boxes and dashed lines are drawn](#7-how-the-boxes-and-dashed-lines-are-drawn)
8. [Every threshold, what it means, when to change it](#8-every-threshold-what-it-means-when-to-change-it)
9. [Results on the BNY sample](#9-results-on-the-bny-sample)
10. [Known limitations and failure modes](#10-known-limitations-and-failure-modes)
11. [Extrapolating to your project](#11-extrapolating-to-your-project)
12. [Suggested rollout plan](#12-suggested-rollout-plan)
13. [FAQ](#13-faq)

---

## 1. The core idea in one paragraph

A born-digital PDF already tells you where every word is, as exact rectangles, and where every drawn line is. You never need to "see" the page.

A table is just words arranged so that:

- **rows** share a vertical position;
- **columns** are separated by vertical strips of empty space (gutters);
- **separate tables** are separated by larger empty space, or by drawn lines.

So the whole problem reduces to **finding empty space at the right scale**:

- big empty space → boundaries between tables (regions);
- small, consistent empty space inside a region → boundaries between columns.

Every number in the pipeline is measured relative to the page itself (line height, normal row gap). Nothing is hard-coded to a coordinate. That is why it adapts when a layout shifts, and why it can replace `table_areas` / `columns` coordinates in Camelot.

---

## 2. Why the previous approaches failed

| Approach | What it does | Why it failed on trustee reports |
|---|---|---|
| Camelot lattice | Finds tables from drawn cell borders | Trustee tables are borderless, so there is nothing to find |
| Camelot stream / pdfplumber / PyMuPDF `find_tables` | Guess table area and columns from text alignment over the *whole page* | When two tables sit side by side, or columns are tight, the guess merges them. You then patched this with coordinates per template, which break when the layout moves |
| Docling (TableFormer) | A vision model detects tables, then predicts structure | Strong on single tables. On dashboard pages it treats adjacent borderless tables as one, because detection is the weak step. It is slow because it renders and runs neural nets on every page, even though the PDF already contains the text |

**The structural flaw:** all of these tried to do **detection** (where is a table?) and **structure** (rows and columns) in one guess over the whole page.

This pipeline separates the two steps:

1. Cut the page into regions first (S3 and S4).
2. Then find columns *inside each region*, using only that region's rows (S5).

A gutter that is ambiguous at page level is unambiguous inside one region.

---

## 3. What a PDF actually contains (the raw material)

PyMuPDF exposes three things this pipeline uses.

### 3.1 Word boxes: `page.get_text("words")`

```python
[(36.2, 188.1, 61.0, 195.4, 'Class', block, line, word_no),
 (63.1, 188.1, 70.3, 195.4, 'A', ...),
 (311.8, 188.1, 352.1, 195.4, '217,000,000.00', ...), ...]
```

- Each tuple is `(x0, y0, x1, y1, text, ...)`, the tight rectangle around one word.
- A "word" is a run of characters without a space. So "Senior Secured" is **two** words, and "1,234.56" is one.
- This is exact geometry from the PDF's text drawing commands, not OCR.

### 3.2 Vector drawings: `page.get_drawings()`

These are lines and rectangles the PDF paints:

- the rule under a header;
- the double underline under a total;
- the box around the "Interest Coverage Test Numerator Detail" panel;
- coloured chart bars.

They are *hints*. Many tables have none. When present, they are strong evidence of a boundary.

### 3.3 Page size: `page.rect`

The BNY sample is 841 × 595 points (landscape A4).

> Everything the pipeline does is arithmetic on these rectangles. There is no image processing anywhere in `pipeline.py`. The page image is only rendered for debugging (`overlay.pdf`, `explain.py`).

---

## 4. The coordinate system

```
(0,0) ───────────────────────────── x grows → (841,0)
  │
  │      "Class"  x0=36.2 y0=188.1
  │              ┌───────┐
  │              │ Class │  y1=195.4
  │              └───────┘ x1=61.0
  │
  y grows ↓
(0,595)
```

- Units are **points**: 1 pt = 1/72 inch, so the page is 11.7 × 8.3 inches.
- The origin is the **top-left** corner, and y grows **downward**.

Derived quantities used everywhere:

- **Line height (`lh`)**: median height of words in the current block. It is about 7–9 pt on BNY pages. Every distance threshold is expressed as a multiple of `lh`, so the pipeline is scale-invariant: a report with bigger fonts just works.
- **Vertical centre** `cy = (y0 + y1) / 2` and **horizontal centre** `cx = (x0 + x1) / 2`.

---

## 5. Pipeline overview

```
                 whole document
                       │
        S1 find_chrome ─┤  what repeats at the same spot on ≥50% of pages
                       │
   ┌────── for each page ───────────────────────────────────────────────┐
   │   S2 load_page      words − chrome, drawn rules, fills, title       │
   │        │                                                            │
   │   S3 xy_cut         recursive split on whitespace  → blocks         │
   │        │                                                            │
   │   S4 attach_headers titles / header lines / totals → their table    │
   │        │                                                            │
   │   S5 build_table    rows (y) + columns (x gutters) → DataFrame      │
   │        │                                                            │
   │   S6 classify       table | text-table | empty-table | chart | text │
   │        │                                                            │
   │   S9 draw_overlay   draw computed boxes/lines onto the page         │
   └────────────────────────────────────────────────────────────────────┘
                       │
        S7 stitch      ─┤  join continuation pieces across pages / sections
        S8 validate    ─┤  totals must equal the rows they close
        S10 export     ─┘  tables.xlsx, overlay.pdf, report.json
```

Run it with `python pipeline.py report.pdf out/ 2 37`. The page numbers are 1-based PDF page numbers.

---

## 6. Stage by stage

### S1 `find_chrome(doc)`: remove page furniture

**What it does.**

1. For every page, take the words in the **top 12%** or **bottom 8%** of the page.
2. Build a key `(text, round(x0/4), round(y0/4))` for each. The 4-point buckets tolerate tiny jitter.
3. Any key that appears on **≥ 50% of pages** is "chrome".

On BNY, that catches:

- the logo text;
- "PROVIDUS CLO II DAC 26-Feb-2021";
- "© Copyright 2021, BNY Mellon. All Rights Reserved."

**Why.** Chrome is not table content. If left in, it creates fake rows and spoils the whitespace statistics: the "normal row gap" would include the gap between the title bar and the table.

**Why only the top and bottom bands.** An early version counted the whole page. The column header "Security" sits at the same position on 20+ pages, so it was wrongly deleted as chrome. Restricting chrome to the page bands fixed that.

**Why by repetition and not by coordinates.** It adapts per document and per trustee automatically. You never write "delete y < 90".

---

### S2 `load_page(page, chrome)`: collect inputs for one page

**What it produces.**

| Key | Meaning |
|---|---|
| `words` | Remaining words as dicts `{x0, y0, x1, y1, t}` |
| `removed` | Chrome words, plus the whole footer line containing "©" (kept for debugging) |
| `title` | Non-chrome text inside the top 12% band, e.g. **"Asset Information I"**. This becomes the table name |
| `H` | Horizontal rules: drawn lines or thin rectangles with height < 3 pt and width > 30 pt |
| `V` | Vertical rules: width < 3 pt and height > 30 pt |
| `fills` | Filled shapes larger than 4 × 4 pt (chart bars, pie slices, cell shading) |
| `boxes` | Any drawing larger than 100 × 100 pt (panel boxes, chart frames) |

An *outlined* (unfilled) rectangle larger than 30 × 30 pt contributes its four sides to `H` and `V`. That is how the two boxed panels on pages 9 and 10 become hard separators.

**Why the footer is removed by the "©" line.** The footer's right-hand label changes per section ("Compliance Tests", "Asset Information 2"), so repetition alone misses it. Anchoring on the line that carries "©" removes the whole line.

**What to generalise.** Other trustees will have a different footer anchor. Make it configurable (section 11).

---

### S3 `xy_cut(words, H, V)`: split the page into regions

This is the most important stage. It is a classic document-layout algorithm (**recursive XY-cut**), with one extra rule that makes it work for tables: the **row-alignment test**.

#### 3a. Projection: finding empty space

Take a block of words.

- Project every word onto the **y-axis**: the intervals `[y0, y1]`, merged. The gaps between merged intervals are **horizontal white bands**.
- Project onto the **x-axis**: the intervals `[x0, x1]`. The gaps are **vertical white gutters**.

```
 y-projection                      x-projection
 ██  row 1                         ████ ███ ████     ███ ███
 ██  row 2                              ↑    ↑    ↑
 ░░  ← white band (cut candidate)    gutters (cut candidates)
 ██  row 3
```

This is `empty_runs()`.

#### 3b. Horizontal cut candidates

First compute `normal_gap`, the **median white space between consecutive rows** in this block. This is measured as gap height, not centre-to-centre distance. In a dense table it is about 4–6 pt.

A white band becomes a candidate if either:

- it is **tall**: `gap > max(2.2 × normal_gap, lh)`, i.e. clearly bigger than the spacing inside a table; or
- it is **ruled and at least moderately tall**: a drawn horizontal rule lies inside the band, the rule spans from the left 30% to the right 30% of the block, and `gap > 0.8 × lh`.

Score: `gap / normal_gap`, plus 2 if ruled.

> Example from `page_4.png`, panel B. Cut #2 is "H gap 10pt = 3.2× normal". This separates the Notes table from the two summary tables below it.

#### 3c. Vertical cut candidates and the row-alignment test

A vertical white gutter exists between **every pair of columns** in a table. If we cut at every gutter, we would shred every table into single columns. That is exactly what naive XY-cut does, and why it is rarely used for tables.

**The fix: compare the row positions on each side of the gutter.**

- **Columns of the same table:** every row on the left has a row at the same height on the right. Alignment ≈ 1.0. **Do not cut.**
- **Two different tables side by side:** their rows are at different heights (different spacing, different start). Alignment is low. **Cut.**

```
  Assets Summary            │  Test Results Summary
  Senior Secured Loans ──── │ ── Test Type            rows don't line up
  Second Lien Loans    ──── │ ── Collateral Quality    → alignment 0.20 → CUT
  Senior Secured Bonds ──── │ ── Coverage Tests
```

`row_alignment(left, right, tol)` groups each side into rows. It then returns the fraction of rows on the smaller side that have a partner within `tol = 0.35 × lh` on the other side.

A gutter is a candidate if:

- it is at least `2 × lh` wide; and
- it is **ruled**: a drawn vertical line covers ≥ 50% of the block height; **or** alignment < 0.6.

Score: `gutter / lh`, plus 5 if ruled, plus `(1 − alignment) × 5`.

**Header-band exception.** A block with no digits and ≤ 4 rows is treated as a pure header band and never cut vertically. Multi-line headers ("Principal / Balance" on two lines next to "Security" on one) never line up, so the alignment test would wrongly split them.

#### 3d. Recursion

1. Pick the single best-scoring candidate, horizontal or vertical.
2. Split the words by their centre (`cy` for a horizontal cut, `cx` for a vertical cut).
3. Recurse into both halves (maximum depth 12).
4. A block with no candidates is a final region.

Every cut is recorded in `log`. `explain.py` panel B draws those cuts, numbered, with the reason text.

**Why best-first.** Cutting the most obvious boundary first means later decisions are made on cleaner sub-blocks, with cleaner statistics. In particular, `normal_gap` and `lh` are recomputed per block.

---

### S4 `attach_headers(blocks)`: put titles, headers and totals back

XY-cut over-segments in three predictable ways. S4 fixes each one.

| Problem | Example | Rule that fixes it |
|---|---|---|
| A **title or header** is cut off from its table, because a rule plus gap sits under the header | Header "Test / Formula / Numerator…" on page 9; "Assets Summary" title | A block with **no digits** and ≤ 5 rows that sits **above** another block (vertical distance < 5 × `lh`, or overlapping the first 4 lines of it) and **over** it horizontally (overlap > 60% of the narrower block) is merged into it. The lower block must have more rows, contain digits, or be a "There are no …" message |
| A **header cell** was split off sideways | "Trade Amount (EUR)" on page 36 | Same rule. The "beside the first lines" allowance covers it |
| A **total line** is printed lower, under a double rule | "Total Balance: 354,184,667.25" on pages 15 and 20 | A block of ≤ 2 rows containing the word *total*, within 4 × `lh` below a bigger block and overlapping it horizontally, is appended to it |

**Why "no digits" is the key test for headers.** Headers and titles are words. Data rows almost always contain numbers. Without this test, the key-value summary box on page 35 ("350,389,357.50 EUR … 9.63% … PASS") was glued on as the header of the table below.

---

### S5 `build_table(region)`: rows, columns, header, body

#### 5a. Rows

`group_rows()` sorts the words by `cy` and starts a new row whenever the next word's centre is more than `0.5 × lh` from the current row's mean centre.

#### 5b. Columns: the ink-coverage profile

Panel D of `explain.py` draws this.

1. Divide the region's x-range into 0.5 pt bins.
2. For each bin, count **how many rows have a word covering it**.
3. Plot that as bars:
   - columns are tall plateaus;
   - gutters are valleys at or near zero.

```
rows with ink │ ███    ████  ██ ██   ███        █ █
              │ ███    ████  ██ ██   ███        █ █
              │ ███▁▁  ████  ██ ██   ███   ▁▁  █ █
  threshold ──┼─────────────────────────────────────── 12% of rows
              └──┬───────┬───┬──┬──┬────┬────────┬──
               gutter middles = column separators (green dashed)
```

A **gutter** is a run of bins whose count is ≤ `tolerance × number_of_rows`, and whose width is at least `0.55 × lh`.

- **Tolerance = 12%** for regions with ≥ 8 rows. A spanning title, a section label ("Committed Purchases"), a wrapped line or a footnote may cross a gutter without destroying it. They are *outvoted*.
- **Tolerance = 0** for small regions (< 8 rows). There are not enough rows to outvote anything, so a gap must be truly empty.
- **Minimum width `0.55 × lh`** (about 4 pt). A normal space between two words is about 0.25–0.3 × `lh`, so "Senior Secured" stays together. The gap between "0.00%" and "Senior" (about 6 pt) is a real gutter.
  - This is what fixed your **merged-column problem**: Floor | Seniority, Fitch Original | Current.
  - An earlier version required gaps wider than `0.9 × lh`, and those columns merged.
- **Single-phrase rows are left out of the profile.** A row whose words have no internal gap > `0.6 × lh` is a single phrase: a title, a section label, or "There are no Defaulted Obligations to report on." They say nothing about columns, so they are excluded from the gutter computation when at least 2 other rows remain.
- **Sparse edge column fallback.** "Reason for Trade" on page 34 has text in only 2 of 24 rows. That is under 12%, so the tolerant profile sees it as part of a valley running to the region's edge. When a tolerant valley touches the region edge, the code falls back to *strictly* empty gaps inside it, which recovers the sparse column.

**Column separators** are the **midpoints** of the gutters. Each word goes to the column whose `[cut_k, cut_k+1)` interval contains the word's centre `cx`.

#### 5c. Header

The **header rows** are the rows above the **first row that contains a digit**. This works because headers are words and data contains numbers.

Header lines are merged into one header row as follows:

1. Each header line is split into **phrases**: words separated by gaps > `0.6 × lh` start a new phrase.
2. A phrase is added to **every column it overlaps** by more than 25% of the phrase's width.
3. So a group header like **"Fitch Rating"**, which sits over two sub-columns, becomes the prefix of both: `Fitch Rating Original`, `Fitch Rating Current`.

#### 5d. Body

- **Wrapped lines.** A row with no digits, few filled cells (≤ 30% of columns, at least 1), and whose centre is within `1.6 × lh` of the previous row is a continuation. It is glued onto the row above. This turns "Banking, Finance," + "Insurance & Real Estate" into one cell.
- **Section labels.** A row with only column 0 filled and no digits (e.g. "General", "Interest", "Collateral Quality Tests") is not data. It becomes the value of a new `_section` column for the rows that follow.
  - If the last header line itself is such a label (the "Committed Purchases" case), it is moved out of the header into the body.
- **"There are no … to report on."** rows are removed before any of this, so empty tables keep their real column headers.

The output is a pandas DataFrame with unique column names. Missing headers become `col0`, `col1`, and so on.

---

### S6 `classify(region)`: what kind of block is this?

The tests run in order:

1. **`chart-labels`.** The region's centre lies inside a box that contains ≥ 4 filled shapes, or ≥ 4 filled shapes lie within 40 pt of the region. These are axis labels and legends, not tables.
2. **`empty-table`.** The text contains "there are no".
3. **`table`.** At least 1 row and 2 columns, and more than 15% of non-empty cells are numbers or percentages.
4. **`text-table`.** Multi-column but mostly words.
5. **`text`.** Everything else.

**Why.** S7 and S8 only operate on `table`, `text-table` and `empty-table`. Classification keeps chart noise out of your data.

---

### S7 `stitch(regions)`: one logical table from many pieces

A region is joined to an existing table when **all** of these hold:

- **Same page title**, e.g. "Asset Information I".
- **Same number of data columns**, ignoring `_section`.
- **Position:**
  - it is on the **next page** (continuation); or
  - it is on the **same page and its header is "section-like"**: only the first header cell is real text, e.g. `['Coverage Tests', 'col1', 'col2']`, meaning it is the next section of the table above.
  - Two tables side by side on the same page are **never** joined. Moody's and Fitch stratification on page 37 stay separate.
- **Header similarity ≥ 0.5.** This is the Jaccard overlap of header names. A piece with only generic headers (`col0…`) matches anything, because continuation pages often have no header.

When joined:

- the piece's columns are renamed **by position** to the first piece's names;
- the `_section` value carries over the page break (Portfolio Profile Tests continues from page 6 to page 8);
- an identical block repeated on consecutive pages (the summary box on pages 35 and 36) is kept once.

"There are no …" messages are stored in the table's `note` field (see the `_index` sheet).

**Result on BNY:**

- Asset Information I: pages 11–15 → **159 rows**.
- Ratings and Recovery Detail: pages 27–32 → 159 rows.
- Purchase and Sale Activity: pages 33–34.
- Compliance Tests: pages 6–8 → 74 rows, with a `_section` column.

---

### S8 `validate(table)`: let the numbers check themselves

Trustee reports are full of arithmetic invariants. S8 uses the most universal one: **every Total or Subtotal row must equal the rows it closes.**

For each table:

1. Find **label columns**: columns containing the word *total* or *subtotal*.
2. For each numeric column, walk down the rows, keeping a running `block` sum. The parser handles `(1,234.56)` as negative and strips "EUR" and "[A]"; percentages are skipped.
3. At a total row, accept the printed value if it equals any of:
   - **rows since the last total** (a plain subtotal);
   - **previous total + rows since** (cumulative: "TOTAL" = Total CDO Par Amount + Total Cash);
   - **sum of earlier totals** (Grand Total = sum of subtotals).
4. The tolerance is 0.015, i.e. rounding to the cent.
5. If a "Total X" row fails and its label does not contain "sub", "grand" or ":", it is treated as an ordinary line item and added to the block. "Total Cash" is a balance, not a sum.

**Result on BNY: 32 of 32 checks pass.** This includes:

- the **cross-page** Total Balance of Asset Information I (5 pages, 159 rows → 354,184,667.25);
- every Purchase and Sale subtotal plus the Grand Total.

**Why this matters more than anything else.**

- A pass is *proof* that the rows, columns and numbers were extracted correctly. If a column had merged, or a row had been dropped or duplicated, the sum would not match.
- This is what lets you **auto-accept** most output and send only failures to a fallback or a human.
- A model's confidence score cannot give you this.

---

### S9 `draw_overlay(page, regions)`: see what was computed

See section 7.

---

### S10 `run()` / export

| File | Contents |
|---|---|
| `overlay.pdf` | The processed pages with every region box, column separator and header line drawn on |
| `tables.xlsx` | `_index` (one row per logical table: sheet, name, pages, rows, columns, note), `_validation` (every check with PASS/FAIL), then one sheet per table `T00…T25` |
| `report.json` | Per region: page, kind, title, shape, bbox, and the exact x of every column separator. Tables and validation are also included |

---

## 7. How the boxes and dashed lines are drawn

**Nothing is detected from pixels.** Every mark on the overlay is a number the pipeline computed from the word rectangles, drawn back onto the PDF page with PyMuPDF's drawing API.

```python
# region box: min/max of the region's word rectangles, padded 3pt
x0, y0, x1, y1 = bbox(region.words)
page.draw_rect(pymupdf.Rect(x0-3, y0-3, x1+3, y1+3), color=col, width=1.4)

# column separators: the gutter midpoints from build_table()
for c in region.col_cuts[1:-1]:
    page.draw_line((c, y0-2), (c, y1+2), color=col, width=0.5, dashes="[2] 2")

# header/body boundary: halfway between the last header row and the first body row
yb = (row_ys[n_header-1] + row_ys[n_header]) / 2
page.draw_line((x0-3, yb), (x1+3, yb), color=col, width=0.6, dashes="[1] 1")

# label
page.insert_text((x0, y0-5), f"R{i} {kind} {rows}x{cols}", fontsize=6.5, color=col)
```

**A worked example: the Floor | Seniority separator on page 11.**

1. On every row, "0.00%" ends at x = 545.9 and "Senior" starts at x = 552.0. These are real values from the BNY file.
2. In the coverage profile, every bin between 545.9 and 552.0 has 0 rows with ink. That is a valley about 6 pt wide.
3. 6 pt ≥ 0.55 × `lh` (about 4 pt), so it is a gutter.
4. The separator is at its midpoint, x ≈ 549.
5. `draw_line((549, top), (549, bottom), dashes=...)` draws the dashed line you see.

`explain.py` draws the same computed values with matplotlib on top of a faded page render:

- **Panel A:** raw word boxes and drawn rules.
- **Panel B:** XY-cut lines in order, with reasons.
- **Panel C:** final region boxes.
- **Panel D:** the coverage profile and gutter midpoints.

The trick that aligns the plot with the page is `imshow(img, extent=[0, W, H, 0])`. It stretches the rendered image onto PDF point coordinates, so every number can be plotted directly.

---

## 8. Every threshold, what it means, when to change it

All distances are relative to `lh` (median word height in the current block) or to `normal_gap` (median white space between rows in the current block).

| Where | Parameter | Value | Meaning | Raise it if… | Lower it if… |
|---|---|---|---|---|---|
| S1 | `min_share` | 0.5 | Fraction of pages a word must repeat on to be chrome | Real headers get deleted | Chrome survives (a short document with few pages) |
| S1 | top / bottom band | 12% / 8% | Where chrome may live | The title bar is taller | Tables start very high |
| S3 | tall band | `2.2 × normal_gap` | Horizontal white space that separates two tables | Tables get split at blank lines between groups | Stacked tables stay merged |
| S3 | ruled band | `0.8 × lh` + rule spanning 40% of width | A drawn line plus moderate gap also separates | Header rules split headers off (S4 usually repairs this) | — |
| S3 | min vertical gutter | `2 × lh` | Narrowest gap that may separate side-by-side tables | Columns get cut | Adjacent tables with a tight gap stay merged |
| S3 | row-alignment cut | `< 0.6` (tol `0.35 × lh`) | How badly rows must misalign to cut | — | A table with ragged rows gets split |
| S3 | header band | no digits, ≤ 4 rows | Never cut such blocks vertically | — | — |
| S4 | header attach | ≤ 5 rows, < 5 × `lh` above, overlap > 60% | Titles and headers glue to the table below | Unrelated text gets glued on | Headers stay detached |
| S4 | total attach | ≤ 2 rows with "total", < 4 × `lh` below | — | — | — |
| S5 | row grouping | `0.5 × lh` | Words within half a line belong to one row | Superscripts split rows | Two close rows merge |
| S5 | gutter min width | `0.55 × lh` | Narrowest column gap | Words of one phrase split into columns | Tight columns merge (your "merged columns" issue) |
| S5 | gutter tolerance | 12% of rows (0 if < 8 rows) | How many rows may cross a gutter | Spanning text keeps destroying columns | Sparse columns get swallowed |
| S5 | phrase gap | `0.6 × lh` | Header words this close form one phrase | — | — |
| S5 | span rule | > 25% of phrase width | A header phrase applies to every column it overlaps this much | — | — |
| S5 | wrap merge | no digit, ≤ 30% filled, < `1.6 × lh` below | Continuation lines | Real sparse rows get merged | Wraps stay as separate rows |
| S6 | chart | ≥ 4 fills | Region sits on a chart | — | — |
| S6 | table | > 15% numeric cells | table vs text-table | — | — |
| S7 | header similarity | ≥ 0.5 | Header overlap needed to stitch | Unrelated tables join | Continuations don't join |
| S8 | tolerance | 0.015 | Rounding to the cent | Rounding differs (e.g. rounded to thousands) | — |

**Tuning rule of thumb:** change one parameter at a time. Re-run on a golden set (section 11.6), and keep the change only if the validation pass rate goes up and nothing that previously passed now fails.

---

## 9. Results on the BNY sample

PDF pages 2–37 were processed (cover, charts, statistical summary and risk retention excluded).

- **72 regions → 26 logical tables.**
- **32 / 32 total checks pass.**
- Multi-page tables were stitched correctly:
  - Asset Information I: 5 pages, 159 rows;
  - Asset Information II: 5 pages;
  - Ratings and Recovery Detail: 6 pages;
  - Purchase and Sale: 2 pages;
  - Compliance Tests: 3 pages, with sections.
- Dashboard pages were split correctly:
  - page 4: Notes / Assets Summary / Test Results Summary;
  - pages 9 and 10: the test table plus two boxed panels;
  - page 37: Moody's and Fitch side by side, with chart labels tagged and excluded.
- Merged columns were resolved: Floor | Seniority, Fitch/Moody's Original | Current.
- Multi-line and grouped headers were collapsed correctly, e.g. "Moody's Recovery Rate", "Fitch Rating Original".
- Empty tables keep their real header and carry a note.
- Runtime: a few seconds for the whole document on CPU.

---

## 10. Known limitations and failure modes

| Limitation | Effect | Mitigation |
|---|---|---|
| **Scanned or image PDFs** | `get_text` returns nothing | Run OCR (Tesseract, PaddleOCR, docTR) to get word boxes, and feed those into the *same* pipeline. It only needs `(x0, y0, x1, y1, text)` |
| **Key-value boxes** (Assets Summary, Numerator Detail) | Header comes out as `col1` | Expected. Handle as key-value in the schema mapping (section 11.3) |
| **Unlabelled total rows** (Ratings Stratification) | Not validated | Add a rule: last row after a double rule, label empty → total |
| **Tables separated only by a tiny gap** with rows that happen to align | Merged into one region | A drawn rule or a title between them usually saves it. Otherwise add a trustee-specific separator keyword |
| **Columns with no gutter at all** (text that touches) | Merged column | Type-aware split in post-processing (number followed by text), or the fallback |
| **Very ragged rows** (multi-line cells in many columns) | Row alignment drops and a table may be split vertically | Raise the alignment threshold for that trustee, or rely on the stitch step |
| **Thresholds were tuned on one BNY file** | Other trustees may need tweaks | Per-trustee overrides (section 11.2) plus the golden-set tuning loop (section 11.6) |
| **Rotated pages / landscape tables inside portrait pages** | Geometry is rotated | Normalise with `page.rotation` / derotation before extraction |
| **Headers containing digits** ("2021", "Class B-1 Notes" in a header) | Header detection stops early | Use a digit-ratio threshold instead of "any digit", or a per-trustee header row count |

---

## 11. Extrapolating to your project

Your situation:

- about 250 companies × 20–30 pages;
- several trustees (US Bank, Wilmington, Citi, BNY…);
- 1.5–2 years of history;
- keyword-based page capture that already works for about 95% of cases;
- about 10,000 lines of if/try code with Camelot coordinates.

### 11.1 Target architecture

```
┌──────────────┐   ┌───────────────┐   ┌───────────────────────────┐
│ 1. Ingest     │──▶│ 2. Identify   │──▶│ 3. Page capture (KEEP)     │
│ PyMuPDF open  │   │ trustee+format│   │ your keyword logic picks   │
└──────────────┘   └───────────────┘   │ the pages you need         │
                                        └─────────────┬─────────────┘
                                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. GENERIC GEOMETRY ENGINE  (pipeline.py S1–S7)                       │
│    page → regions → DataFrames → stitched logical tables             │
│    same code for every trustee; only thresholds may be overridden    │
└─────────────┬───────────────────────────────────────────────────────┘
              ▼
┌─────────────────────────────┐    ┌──────────────────────────────────┐
│ 5. TABLE MATCHING (config)   │──▶│ 6. SCHEMA MAPPING (config)        │
│ which logical table is       │    │ header text → canonical field     │
│ "Par Value Tests", "Holdings"│    │ "Principal Balance" → par_amount  │
└─────────────────────────────┘    └──────────────┬───────────────────┘
                                                   ▼
                                  ┌──────────────────────────────────┐
                                  │ 7. VALIDATION                     │
                                  │ totals, cross-table, formulas     │
                                  └───────┬───────────────┬──────────┘
                                     PASS │               │ FAIL
                                          ▼               ▼
                                  ┌────────────┐  ┌─────────────────────┐
                                  │ 8. Store    │  │ 9. Fallback chain    │
                                  └────────────┘  │ a) retry w/ alt params│
                                                  │ b) Docling on crop    │
                                                  │ c) vision LLM + schema│
                                                  │ d) human review queue │
                                                  └─────────┬───────────┘
                                                            │ re-validate
                                                            ▼
                                                   corrections → golden set
```

The key shift is from **code per template** to **one engine plus data (config) per trustee**.

Your 10k lines of `if` statements encode three kinds of knowledge, mixed together:

1. **Geometry** (where things are): now handled generically by the engine.
2. **Semantics** (which table is which, and what each column means): this moves into config files.
3. **Business rules** (what must add up): this moves into validation rules.

### 11.2 Project layout

```
trustee_extract/
├── engine/
│   ├── geometry.py        # S1–S6 (from pipeline.py)
│   ├── stitch.py          # S7
│   ├── numbers.py         # to_number, dates, percentages, currencies
│   └── overlay.py         # S9 + explain panels
├── config/
│   ├── defaults.yaml      # all thresholds from section 8
│   ├── bny.yaml
│   ├── usbank.yaml
│   ├── wilmington.yaml
│   └── citi.yaml
├── mapping/
│   └── canonical_schema.yaml   # your target fields per table type
├── validate/
│   ├── totals.py          # S8
│   ├── cross_table.py     # e.g. holdings total == asset summary par
│   └── formulas.py        # e.g. par value test = [A]/([B]+...)
├── fallback/
│   ├── docling_crop.py
│   └── llm_crop.py
├── review/                # overlay images + correction capture
├── tests/
│   ├── golden/            # PDFs + expected outputs
│   └── test_regression.py
└── run.py
```

### 11.3 Per-trustee config: data, not code

```yaml
# config/bny.yaml
trustee: BNY Mellon
detect:                         # how to recognise this trustee/format
  any_text: ["BNY MELLON", "bnymellon.com"]
chrome:
  footer_anchor: "©"            # the footer line to drop
thresholds:                     # only what differs from defaults.yaml
  gutter_min_width_lh: 0.55
tables:
  holdings:
    title_regex: "^Asset Information I$"
    required_headers: ["Security", "Principal Balance"]
    columns:                    # header text (regex) → canonical field
      "Security": security_name
      "LXID / ISIN": security_id
      "Maturity Date": maturity_date
      "Market Price": market_price
      "Spread": spread
      "Floor": floor
      "Seniority": seniority
      "Country of Domicile": country
      "Principal Balance": par_amount
    validate:
      - total_row: "Total Balance"
  par_value_tests:
    title_regex: "^Par Value Tests$"
    required_headers: ["Numerator", "Denominator", "Actual"]
    columns:
      "Test": test_name
      "Numerator": numerator
      "Denominator": denominator
      "Actual": actual_pct
      "Target": target
      "Result": result
    validate:
      - formula: "actual_pct ≈ numerator / denominator * 100"
  asset_summary:
    title_regex: "^Notes and Asset Summary Information$"
    kind: key_value             # col0 = key, col1 = value
    keys:
      "Total CDO Par Amount": total_par
      "Total Cash": total_cash
cross_checks:
  - "holdings.par_amount.sum() == asset_summary.total_par"
  - "ratings_recovery.par_amount.sum() == holdings.par_amount.sum()"
```

When a trustee changes the layout:

- the geometry adapts by itself;
- the config keeps matching, because it matches **text** (titles and headers), not positions;
- if a header is renamed, you change one line of YAML, not code.

### 11.4 Validation is your accuracy engine; expand it

Validation rules ordered from most universal to most specific:

1. **Totals and subtotals** (S8): already generic.
2. **Cross-table equalities.** On BNY, 354,184,667.25 appears in five places: Asset Summary, Asset Information I and II, Ratings and Recovery, Par Value numerator detail. Every one of them must agree.
3. **Formula checks.** BNY prints the formula. Actual = Numerator / Denominator (Par Value: 348,132,754.78 / 248,500,000 = 140.09% ✓). Headroom = Actual − Target. Interest coverage numerator = sum of the detail panel.
4. **Domain checks.**
   - Ratings belong to a known set (Aaa…C, AAA…D).
   - ISINs pass their check digit; LoanX IDs match `LX\d{6}`.
   - Dates parse.
   - Percentages fall between 0 and 100.
   - PASS/FAIL agrees with Actual vs Target.
5. **Temporal checks.** This month's opening balance equals last month's closing balance. Holdings count changes by roughly purchases − sales.

Each passing check raises confidence. Define **auto-accept** as "every applicable check passes". Track the auto-accept rate per trustee as your main KPI.

### 11.5 Fallback chain (only for failures)

Run the fallbacks in this order. Re-validate after each step and stop at the first pass.

1. **Retry with alternative parameters.** For example, gutter min width 0.45 / 0.7, tolerance 0.08 / 0.18, alignment 0.5 / 0.7. It is cheap, and often enough.
2. **Docling on the region crop only.** Use `page.get_pixmap(clip=region.bbox)` or a cropped PDF. It is fast because it is one small image, and accurate because there is only one table.
3. **Vision LLM on the crop plus word list.** Send the image crop, the words with coordinates, and the target JSON schema from config. Ask for rows. Validate the result like any other output. Never trust it unvalidated.
4. **Human review.** Show the overlay PNG next to the extracted table and the failing check. The reviewer corrects either the table or the region boxes and column lines.

### 11.6 Golden set and regression testing (non-negotiable)

1. Pick **3–5 reports per trustee per format version**, including the hardest ones.
2. Create expected outputs. Your historical manually keyed data from the last 2 years may already be this.
3. `tests/test_regression.py` runs the engine and compares each table cell by cell. It reports precision and recall per table and per trustee.
4. **Every threshold change and every code change must keep the golden set green.** This is what replaces the fear of "if I touch this `if`, what else breaks?"
5. **Tuning loop.** Grid-search the thresholds in section 8 per trustee, and pick the setting with the highest validation pass rate plus golden-set accuracy. This is the "dynamic, learned" behaviour you asked for, without training a neural net.

### 11.7 "Can I train something?" Yes, in stages

| Stage | What you learn | Data you need |
|---|---|---|
| Now | Per-trustee **thresholds** by grid search against validation and golden data | Golden set |
| Soon | A small **classifier for region types and table identity** (gradient boosting on features: n_rows, n_cols, numeric share, header tokens, position, title) | Regions labelled by the config matcher plus corrections |
| Later | A **layout detection model** (e.g. a YOLO / DETR-style table detector fine-tuned on your pages), used only when geometry fails | Every human-corrected region box from review. The overlay workflow produces this labelled data for free |

Because review corrections are stored as boxes and column x-positions (exactly what `report.json` already contains), your labelled dataset grows automatically as a side effect of operations.

### 11.8 Performance at your scale

- About 250 × 25 = 6,250 pages per cycle.
- The geometry engine is pure Python on vector data, roughly 50–200 ms per page.
- Parallelise per PDF with `multiprocessing.Pool` (one PDF per worker; PyMuPDF documents are not shared between processes).
- **Estimate:** the whole batch takes minutes on an ordinary 8-core machine.
- The fallback only touches failures, and only crops, so Docling or the LLM drops from about 10 minutes per PDF to seconds per failed region.

### 11.9 Migrating from your current code

1. **Keep your page capture** (keyword matching). It feeds page numbers into the engine.
2. Run the engine **in shadow mode** next to the current Camelot code for one reporting cycle. Compare outputs table by table, and investigate every difference. Usually one side is clearly right, and validation tells you which.
3. Move one trustee at a time:
   1. write its YAML;
   2. build its golden set;
   3. switch it over when shadow results match or beat the old code.
4. Delete the coordinate code for that trustee only after the switch.
5. Your existing try/except branches are documentation of edge cases. Turn each one into either:
   - a golden test case;
   - a validation rule; or
   - a config entry.

### 11.10 Hardening checklist before production

- [ ] Number parsing: `(1,234.56)`, `-1,234.56`, `1.234,56` (EU format), `1,234.56 EUR`, `1.5MM`, trailing `%`, `N/A`, `***`, `-`.
- [ ] Dates: `26-Feb-2021`, `02/26/2021`, `2021-02-26`.
- [ ] Unicode: non-breaking spaces, en-dashes as minus signs, ligatures.
- [ ] Encrypted or password-protected PDFs: `doc.needs_pass`.
- [ ] Logging: per table, record the stage decisions (cut log, gutters) to `report.json` for traceability.
- [ ] Every output row carries provenance: `pdf, page, region bbox, row y`, so any number can be traced back to the page.
- [ ] Monitoring: auto-accept rate per trustee per cycle. A sudden drop means a layout change.

---

## 12. Suggested rollout plan

| Week | Goal | Done when |
|---|---|---|
| 1 | Run `pipeline.py` and `explain.py` on one report from each trustee; read the overlays | You know which thresholds each trustee needs |
| 2 | Package the engine (11.2); build `defaults.yaml` plus one trustee YAML; add schema mapping for 2–3 key tables | Canonical output for one trustee |
| 3 | Golden set for that trustee; regression test; cross-table and formula validation | Auto-accept rate measured |
| 4 | Shadow run against the current code for a full cycle | Differences explained |
| 5–6 | Remaining trustees (YAML plus golden set each); retry-with-alternative-params fallback | All trustees covered |
| 7+ | Crop-level Docling or LLM fallback; review UI using the overlays; start collecting corrections | Fallback only on failures; labelled data accumulating |

---

## 13. FAQ

**Q: Is this an ML model?**
No. It is deterministic geometry plus statistics computed per page. It is fast, explainable (every decision has a logged reason) and reproducible. ML enters later, and only where geometry fails (section 11.7).

**Q: What happens when a trustee changes the layout?**
Region and column detection re-derive everything from the new page, so moves, resizes and new columns are handled. Your config matches titles and headers by text, so it survives unless the wording changes. Validation tells you immediately if anything went wrong.

**Q: Why not just give the whole page to an LLM?**
- It costs too much at your volume.
- It can hallucinate digits.
- Long tables exceed reliable output lengths.
- It cannot prove correctness.

Use it on small crops, with a schema, and always followed by validation.

**Q: How do I debug a bad table?**
1. Open `overlay.pdf` on that page.
2. If the **box** is wrong, the problem is S3 or S4. Run `explain.py` on the page and check panel B to see which cut was or wasn't made, and why.
3. If a **dashed line** is wrong, the problem is S5. Check panel D: is the valley too narrow (min width) or partly filled (tolerance)?
4. If the box and lines are right but the **values** are wrong, the problem is the header, wrap or section rules in S5 (sections 5c and 5d).

**Q: Can I use Camelot with this?**
Yes, if you want. Pass `table_areas = region.bbox` and `columns = region.col_cuts` to Camelot stream. They are now computed per page instead of hard-coded. You will probably find you no longer need Camelot.

**Q: Where are page numbers 1-based vs 0-based?**
The command line and `Region.page` use 1-based PDF page numbers. PyMuPDF indexes from 0 (`doc[pno - 1]`). The BNY printed page numbers are one less than the PDF page numbers, because the cover is unnumbered.

---

## pipeline.py

```python
--8<-- "docs/code/pipeline.py"
```

## explain.py

```python
--8<-- "docs/code/explain.py"
```
