x"""
explain.py  -  show, stage by stage, how pipeline.py turns one page into tables.

    python explain.py report.pdf <pdf_page_number> out.png

Panel A  raw input:   every word box PyMuPDF returns + every line the PDF draws
Panel B  XY-cut:      each cut the recursion made, numbered in order, with the reason
Panel C  regions:     final blocks after titles / headers / totals are attached
Panel D  columns:     for the biggest region, the 'ink coverage' profile -> gutters -> dashed lines

Everything is plotted in PDF coordinates (points), on top of a rendering of the page.
"""
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pymupdf

import pipeline as P


def page_image(page, dpi=110):
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
    return img


def show_page(ax, img, page, title):
    W, H = page.rect.width, page.rect.height
    ax.imshow(img, extent=[0, W, H, 0], alpha=0.35)   # image stretched onto PDF point coords
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xticks(range(0, int(W), 100))
    ax.set_yticks(range(0, int(H), 100))
    ax.tick_params(labelsize=7)


def box(ax, r, **kw):
    x0, y0, x1, y1 = r
    ax.add_patch(patches.Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, **kw))


def main(pdf, pno, out_png):
    doc = pymupdf.open(pdf)
    chrome = P.find_chrome(doc)
    page = doc[pno - 1]
    img = page_image(page)
    D = P.load_page(page, chrome)

    fig, axs = plt.subplots(2, 2, figsize=(22, 15.5))
    (a, b), (c, d) = axs

    # ---------------- A. raw input -------------------------------------------------------
    show_page(a, img, page, "A. Input: word boxes (blue), drawn lines (orange/purple), "
                            "chrome removed (grey), section title (green)")
    for w in D["words"]:
        box(a, (w["x0"], w["y0"], w["x1"], w["y1"]), lw=0.5, ec="tab:blue")
    for w in D["removed"]:
        a.add_patch(patches.Rectangle((w["x0"], w["y0"]), w["x1"] - w["x0"], w["y1"] - w["y0"],
                                      fc="grey", alpha=0.5))
    for r in D["H"]:
        a.plot([r.x0, r.x1], [r.y0, r.y0], color="tab:orange", lw=1.2)
    for r in D["V"]:
        a.plot([r.x0, r.x0], [r.y0, r.y1], color="tab:purple", lw=1.2)
    a.text(10, 20, f"page title = '{D['title']}'", color="green", fontsize=9)

    # ---------------- B. XY-cut steps ----------------------------------------------------
    log = []
    blocks = P.xy_cut(D["words"], D["H"], D["V"], log=log)
    show_page(b, img, page, f"B. Recursive XY-cut: {len(log)} cuts (red = horizontal band, "
                            f"blue = vertical gutter), numbered in order")
    for n, cut in enumerate(log, 1):
        x0, y0, x1, y1 = cut["bbox"]
        box(b, cut["bbox"], lw=0.4, ec="black", ls=":")
        if cut["kind"] == "h":
            b.plot([x0, x1], [cut["pos"]] * 2, color="red", lw=1.6)
            b.text(x1 + 2, cut["pos"], f"#{n} {cut['why']}", color="red", fontsize=6.5, va="center")
        else:
            b.plot([cut["pos"]] * 2, [y0, y1], color="blue", lw=1.6)
            b.text(cut["pos"] + 2, y0 + 8 + 9 * (n % 4), f"#{n} {cut['why']}", color="blue", fontsize=6.5)

    # ---------------- C. regions ---------------------------------------------------------
    blocks = P.attach_headers(blocks)
    regions = []
    for blk in sorted(blocks, key=lambda q: (round(P.bbox(q)[1] / 20), P.bbox(q)[0])):
        r = P.Region(page=pno, words=blk, bbox=P.bbox(blk), title=D["title"])
        P.build_table(r)
        r.kind = P.classify(r, D["fills"], D["boxes"])
        regions.append(r)
    show_page(c, img, page, "C. Final regions (after attaching titles / header lines / total lines)")
    for i, r in enumerate(regions):
        col = P.COLORS[i % len(P.COLORS)]
        x0, y0, x1, y1 = r.bbox
        box(c, (x0 - 3, y0 - 3, x1 + 3, y1 + 3), lw=2, ec=col)
        c.text(x0, y0 - 6, f"R{i} {r.kind} {r.df.shape[0]}x{r.df.shape[1]}", color=col, fontsize=8)

    # ---------------- D. columns of the biggest table region -----------------------------
    tables = [r for r in regions if r.kind in ("table", "text-table")] or regions
    r = max(tables, key=lambda r: len(r.words))
    rows = P.group_rows(r.words)
    x0, y0, x1, y1 = r.bbox
    lh = P.line_height(r.words)
    cover = P.ink_coverage(rows, x0, x1)
    xs = x0 + np.arange(len(cover)) * 0.5
    tol = 0.12 if len(rows) >= 8 else 0.0
    d.set_title(f"D. Columns of R{regions.index(r)}: count, for every x, how many of the {len(rows)} "
                f"rows have ink there; valleys = gutters", fontsize=11, loc="left")
    # top half: the words of the region; bottom half: the coverage profile (shared x axis)
    for w in r.words:
        box(d, (w["x0"], w["y0"], w["x1"], w["y1"]), lw=0.5, ec="tab:blue")
        d.text(w["x0"], w["y1"] - 1, w["t"], fontsize=4.5)
    base = y1 + 25                       # draw the bar profile below the region
    scale = 110 / max(len(rows), 1)       # rows -> points of bar height
    d.fill_between(xs, base + 110, base + 110 - np.array(cover) * scale, step="mid",
                   color="tab:blue", alpha=0.35, lw=0)
    d.axhline(base + 110, color="black", lw=0.6)
    d.axhline(base + 110 - tol * len(rows) * scale, color="red", lw=0.8, ls="--")
    d.text(x1 + 4, base + 110 - tol * len(rows) * scale, f"threshold\n{tol:.0%} of rows",
           color="red", fontsize=7, va="center")
    d.text(x0, base + 125, "ink coverage per x (bar height = number of rows with a word there)",
           fontsize=8)
    for cut in r.col_cuts[1:-1]:
        d.plot([cut, cut], [y0 - 4, base + 110], color="green", lw=1, ls="--")
    d.set_xlim(x0 - 15, x1 + 60)
    d.set_ylim(base + 135, y0 - 15)
    d.tick_params(labelsize=7)
    d.text(x0, y0 - 8, f"green dashed = gutter middles = column separators "
                       f"(gap must be > {0.55 * lh:.1f}pt = 0.55 x line height)",
           color="green", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_png, dpi=90)
    print("saved", out_png)
    print(r.df.head(8).to_string())


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
