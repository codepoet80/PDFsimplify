#!/Users/jonwise/.local/share/pdfsimplify/venv/bin/python3
"""
pdfsimplify - reflow a PDF into a simple single-column PDF for small or old screens.

Takes a PDF laid out for print (two columns, small type, A4/Letter) and rebuilds it
as one column at a readable size on a small page, keeping figures, tables, headings,
lists and bold/italic emphasis. The output uses only base-14 fonts where possible,
plain RGB images, no transparency and no encryption, so it opens on old readers.

    pdfsimplify input.pdf                     # -> "input (simplified).pdf"
    pdfsimplify input.pdf -o out.pdf --pages 26-50
    pdfsimplify input.pdf --dry-run           # report detected layout, write nothing

The layout is measured from the file itself; every measurement can be overridden.
Run with --dry-run first on an unfamiliar document.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

__version__ = "1.0"

try:
    import pymupdf
except ImportError:                                             # pragma: no cover
    sys.exit("pdfsimplify: PyMuPDF is required.  pip install pymupdf")
try:
    from PIL import Image
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (BaseDocTemplate, Frame, Image as RLImage,
                                    KeepTogether, PageTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
except ImportError:                                             # pragma: no cover
    sys.exit("pdfsimplify: reportlab and pillow are required.  pip install reportlab pillow")


# ----------------------------------------------------------------------------- config

PAGE_SIZES = {                       # name: (width, height) in points
    "tablet": (432, 576),            # 6x8in, 3:4 - fits an iPad/4:3 tablet exactly
    "phone":  (324, 576),            # 4.5x8in, 9:16
    "ereader": (360, 480),           # 5x6.67in, 3:4 - small e-ink
    "a5":     (420, 595),
    "letter": (612, 792),
}
UNICODE_FONTS = [                    # searched in order when text needs more than WinAnsi
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", None, None, None),
    ("/Library/Fonts/Arial Unicode.ttf", None, None, None),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
]

CAPTION_RE = re.compile(
    r"^\s*(Fig(?:ure|\.)?|Table|Chart|Exhibit|Scheme|Plate|Box)\s*\.?\s*\d+", re.I)
NUMBERED_MARKER_RE = re.compile(r"^\s*\(?(\d{1,3}|[a-z]|[ivxlc]{1,6})[.)]\s*$", re.I)
BULLET_CHARS = set("•◦▪▫●○⚫·–—-‐*‣⁃►▸√")
BULLET_FONT_RE = re.compile(r"wingdings|symbol|dingbat|webdings", re.I)
BOLD_RE = re.compile(r"bold|black|heavy|semib|demib", re.I)
ITALIC_RE = re.compile(r"italic|oblique", re.I)
SECTION_HEAD_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+\S")
URLISH_RE = re.compile(r"://|www\.")


def log(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------- helpers

def parse_pages(spec, npages):
    """'3', '3-9', '3-', '-9', '1,4,7-9' -> sorted list of 0-based indices."""
    if not spec:
        return list(range(npages))
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, _, b = part.partition("-")
            a = int(a) if a.strip() else 1
            b = int(b) if b.strip() else npages
        else:
            a = b = int(part)
        for p in range(max(1, a), min(npages, b) + 1):
            out.add(p - 1)
    if not out:
        raise ValueError(f"no pages selected by {spec!r}")
    return sorted(out)


def percentile(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def span_is_bold(span):
    return bool(span.get("flags", 0) & 16) or bool(BOLD_RE.search(span.get("font", "")))


def span_is_italic(span):
    return bool(span.get("flags", 0) & 2) or bool(ITALIC_RE.search(span.get("font", "")))


def line_text(line):
    return "".join(ch["c"] for s in line["spans"] for ch in s["chars"]).replace("\u00ad", "")


# ------------------------------------------------------------------- synthetic italics

_NUM = r"[-+]?[\d.]+"
_TOK = re.compile(rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+(cm|Tm)"
                  r"|(\bq\b|\bQ\b)")


def _matmul(m, n):
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def skew_italic_boxes(page):
    """Word processors often fake italics with a skewed text matrix instead of an italic
    font, which leaves no trace in the font data.  Recover those runs by reading the
    content stream for Tm operators with a shear term, then matching each to the span
    that starts at that point."""
    try:
        cs = page.read_contents().decode("latin-1")
    except Exception:
        return []
    H = page.rect.height
    ctm, stack, anchors = (1, 0, 0, 1, 0, 0), [], []
    for m in _TOK.finditer(cs):
        if m.group(8):
            if m.group(8) == "q":
                stack.append(ctm)
            elif stack:
                ctm = stack.pop()
            continue
        v = tuple(float(m.group(i)) for i in range(1, 7))
        if m.group(7) == "cm":
            ctm = _matmul(v, ctm)
        elif v[2] != 0:
            x, y = _matmul(v, ctm)[4:]
            anchors.append((x, H - y))
    if not anchors:
        return []
    spans = [(sp["bbox"], "".join(chr(c[0]) for c in sp["chars"]))
             for sp in page.get_texttrace() if sp["type"] == 0 and sp.get("chars")]
    if not spans:
        return []
    boxes = []
    for ax, ay in anchors:
        bbox, _ = min(spans, key=lambda s: abs(s[0][0] - ax) + abs(s[0][3] - ay))
        boxes.append(bbox)
    return boxes


# ------------------------------------------------------------------- layout detection

@dataclass
class Layout:
    page_w: float = 0.0
    page_h: float = 0.0
    top_y: float = 0.0            # body area, excluding running header
    bot_y: float = 0.0            # body area, excluding running footer / page number
    cols: list = field(default_factory=list)      # [(x_left, x_right), ...] per column
    measures: list = field(default_factory=list)  # per column: where wrapped text ends
    gutter: tuple | None = None                   # (x0, x1) of the between-column gap
    body_size: float = 10.0
    head_sizes: dict = field(default_factory=dict)   # rounded size -> outline level 0..2
    indent: float = 18.0
    justified: bool = False       # justified text hyphenates; ragged text does not
    chrome: list = field(default_factory=list)       # sample of dropped header/footer text

    @property
    def ncols(self):
        return len(self.cols)

    def column_of(self, x0, x1):
        """-1 means the line spans the gutter, i.e. it is full width."""
        if self.gutter and x0 < self.gutter[0] and x1 > self.gutter[1]:
            return -1
        for i, (cx0, cx1) in enumerate(self.cols):
            if x0 < cx1 + 2:
                return i
        return len(self.cols) - 1


def _iter_lines(doc, pages, y0=None, y1=None):
    for pno in pages:
        page = doc[pno]
        for blk in page.get_text("rawdict")["blocks"]:
            if blk["type"] == 1:
                continue
            for ln in blk["lines"]:
                t = line_text(ln)
                if not t.strip():
                    continue
                b = ln["bbox"]
                if y0 is not None and b[1] < y0:
                    continue
                if y1 is not None and b[1] > y1:
                    continue
                yield pno, ln, b, t


def detect_chrome(doc, pages, page_h):
    """Running headers, footers and page numbers repeat in the same place on most pages.
    Find them so their text does not end up spliced into the body."""
    band = page_h * 0.13
    top_hits, bot_hits, samples = defaultdict(set), defaultdict(set), {}
    for pno, ln, b, t in _iter_lines(doc, pages):
        norm = re.sub(r"\d+", "#", t).strip().lower()
        norm = re.sub(r"\s+", " ", norm)
        if len(norm) > 90:
            continue
        key = (norm, round(b[1] / 6))
        if b[1] < band:
            top_hits[key].add(pno)
            samples[key] = (t.strip(), b)
        elif b[3] > page_h - band:
            bot_hits[key].add(pno)
            samples[key] = (t.strip(), b)
    need = max(2, len(pages) * 0.4)
    top_y, bot_y, chrome = 0.0, page_h, []
    for key, seen in top_hits.items():
        if len(seen) >= need:
            t, b = samples[key]
            top_y = max(top_y, b[3] + 1)
            chrome.append(t)
    for key, seen in bot_hits.items():
        if len(seen) >= need:
            t, b = samples[key]
            bot_y = min(bot_y, b[1] - 1)
            chrome.append(t)
    return top_y, bot_y, chrome[:6]


def columns_from_boxes(boxes, page_w, force=None, min_lines=20):
    """Find column left edges from the line-start histogram, then confirm each with the
    x-coverage profile: a real column edge has a near-empty gutter to its left and a sharp
    step up to its right.  Hunting for empty gutters alone is unreliable, because
    full-width headings and captions cross them."""
    n = len(boxes)
    if n < min_lines:
        return None
    cov = [0] * (int(page_w) + 2)
    x0s = [b[0] for b in boxes]
    x1s = [b[2] for b in boxes]
    for b in boxes:
        for x in range(max(0, int(b[0])), min(len(cov) - 1, int(b[2]))):
            cov[x] += 1
    left, right = percentile(x0s, 0.02), percentile(x1s, 0.98)

    def measures_for(cols):
        out = []
        for i, (e, r) in enumerate(cols):
            nxt = cols[i + 1][0] if i + 1 < len(cols) else None
            ends = [x1 for x0, x1 in zip(x0s, x1s)
                    if x0 >= e - 3 and (nxt is None or x1 <= nxt - 2)]
            if len(ends) < 8:
                out.append(r)
                continue
            med = percentile(ends, 0.5)
            long_ends = [x for x in ends if x >= med] or ends
            out.append(min(r, percentile(long_ends, 0.85)))
        return out

    single = ([(left, right)], None, [percentile(x1s, 0.9)])
    if force == 1:
        return single

    groups = {}
    for x in x0s:
        groups.setdefault(round(x / 3) * 3, []).append(x)
    cand = sorted(k for k, v in groups.items() if len(v) >= n * 0.15)
    if not cand:
        return single

    def at(x):
        return cov[int(max(0, min(len(cov) - 1, x)))]

    peak = max(cov) or 1
    # A hanging-indent list puts most line starts at the text indent rather than at the
    # column edge, so once a column is found, step back to the leftmost cluster that
    # still has a clear gutter to its left - that is where the column really begins.
    def widen(c):
        best = c
        for other in sorted(groups, reverse=True):
            if not (c - 4 * max(8.0, page_w * 0.03) <= other < c):
                continue
            if len(groups[other]) < n * 0.06:
                continue
            if at(other - 7) <= 0.45 * at(other + 7):
                best = min(best, other)
        return best

    edges = [min(cand[0], left)]
    for c in cand[1:]:
        if c - edges[-1] < page_w * 0.15:
            continue
        if at(c - 7) <= 0.45 * at(c + 7) and at(c + 7) >= peak * 0.35:
            c = widen(c)
            if c - edges[-1] < page_w * 0.15:
                continue
            edges.append(float(sum(groups[c]) / len(groups[c])))
    if len(edges) == 1:
        return single
    if force and force > 1:
        edges = edges[:force]

    cols = []
    for i, e in enumerate(edges):
        nxt = edges[i + 1] if i + 1 < len(edges) else None
        ends = [x1 for x0, x1 in zip(x0s, x1s)
                if x0 >= e - 3 and (nxt is None or x1 <= nxt - 2)]
        cols.append((e, percentile(ends, 0.995) if ends else (nxt - 12 if nxt else right)))
    cols[-1] = (cols[-1][0], max(cols[-1][1], right))
    gutter = (cols[0][1], cols[1][0]) if len(cols) == 2 else None
    return cols, gutter, measures_for(cols)


def detect_columns(doc, pages, top_y, bot_y, page_w, force=None):
    boxes = [b for _, _, b, _ in _iter_lines(doc, pages, top_y, bot_y)]
    got = columns_from_boxes(boxes, page_w, force)
    return got if got else ([], None, [])


def detect_type(doc, pages, top_y, bot_y):
    """Body size is the most-used size.  A heading size is larger, rarer, and - crucially -
    recurs through the document; oversized type that appears only in the first pages is a
    cover or a contents page, not a section heading."""
    sizes = Counter()
    where = defaultdict(set)
    for pno, ln, b, t in _iter_lines(doc, pages, top_y, bot_y):
        for s in ln["spans"]:
            r = round(s["size"] * 2) / 2
            sizes[r] += len(s["chars"])
            where[r].add(pno)
    if not sizes:
        return 10.0, {}
    body = sizes.most_common(1)[0][0]
    total = sum(sizes.values())
    cands = [s for s, c in sizes.items()
             if body * 1.15 <= s <= body * 3.4 and c < total * 0.12 and c >= 2]
    if len(pages) >= 12:
        span_need = max(1, 0.15 * (max(pages) - min(pages)))
        spread = [s for s in cands
                  if len(where[s]) >= 2 and (max(where[s]) - min(where[s])) >= span_need]
        if spread:
            cands = spread
    cands.sort(reverse=True)
    merged = []
    for s in cands:
        if not merged or merged[-1] - s > 0.6:
            merged.append(s)
    return body, {s: i for i, s in enumerate(merged[:3])}


def detect_justified(doc, pages, lay):
    """In justified text the typesetter hyphenates words to make every line reach the
    measure, so a trailing hyphen is a split word and must be closed up.  Ragged-right
    text introduces no such hyphens, and a trailing hyphen there belongs to a compound.
    Detected from how sharply line endings pile up, which works whatever the column count."""
    x1 = [b[2] for _, _, b, _ in _iter_lines(doc, pages, lay.top_y, lay.bot_y)]
    if len(x1) < 40:
        return False
    med = percentile(x1, 0.5)
    long = [x for x in x1 if x >= med]
    hist = Counter(round(x / 2) for x in long)
    need = max(3, 0.08 * len(long))
    covered = sum(c for c in hist.values() if c >= need)
    return covered / len(long) > 0.55


def detect_indent(doc, pages, lay):
    """Bullet and list continuation indents, as a step off each column's left edge."""
    offs = Counter()
    for _, ln, b, t in _iter_lines(doc, pages, lay.top_y, lay.bot_y):
        c = lay.column_of(b[0], b[2])
        if c < 0:
            continue
        d = b[0] - lay.cols[c][0]
        if 4 < d < 80:
            offs[round(d)] += 1
    if not offs:
        return 18.0
    return float(min(offs, key=lambda k: (-offs[k], k)))


def analyse(doc, pages, force_cols=None, quiet=False):
    page = doc[pages[0]]
    lay = Layout(page_w=page.rect.width, page_h=page.rect.height)
    sample = pages if len(pages) <= 30 else pages[::max(1, len(pages) // 30)]
    # headers and footers repeat document-wide, so judge them on a document-wide sample
    wide = sorted(set(list(range(0, len(doc), max(1, len(doc) // 24)))[:24]) | set(sample))
    lay.top_y, lay.bot_y, lay.chrome = detect_chrome(doc, wide, lay.page_h)
    lay.cols, lay.gutter, lay.measures = detect_columns(
        doc, sample, lay.top_y, lay.bot_y, lay.page_w, force_cols)
    if not lay.cols:
        return None
    if force_cols == 1 and len(lay.cols) > 1:
        lay.cols, lay.gutter = [(lay.cols[0][0], lay.cols[-1][1])], None
        lay.measures = [lay.measures[-1]]
    lay.body_size, lay.head_sizes = detect_type(doc, sample, lay.top_y, lay.bot_y)
    lay.indent = detect_indent(doc, sample, lay)
    # justification is a property of the whole book, not of two sampled pages
    lay.justified = detect_justified(doc, wide, lay)
    return lay


# --------------------------------------------------------------- figures and tables

def _rules(page):
    """Thin filled rectangles and straight strokes, i.e. the lines a table is drawn with."""
    hor, ver = [], []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width > 18 and r.height <= 3.0:
            hor.append((r.y0, r.x0, r.x1))
        elif r.height > 8 and r.width <= 3.0:
            ver.append((r.x0, r.y0, r.y1))
    return hor, ver


def _cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(g) / len(g) for g in out]


def _vrule_segments(page):
    segs = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.height > 8 and r.width <= 3.0:
            segs.append((r.x0, r.y0, r.y1))
    return segs


def detect_tables(page):
    """A table is a run of horizontal rules crossed by a run of vertical ones."""
    hor, ver = _rules(page)
    if len(hor) < 2 or len(ver) < 2:
        return []
    ys = _cluster([h[0] for h in hor], 3)
    xs = _cluster([v[0] for v in ver], 3)
    if len(ys) < 2 or len(xs) < 2:
        return []
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 - x0 < 60 or y1 - y0 < 18:
        return []
    # rules must actually span the region, not just happen to share a page
    xs = [x for x in xs if x0 - 2 <= x <= x1 + 2]
    ys = [y for y in ys if y0 - 2 <= y <= y1 + 2]
    if len(xs) < 3 or len(ys) < 2:
        return []              # two verticals only = a plain box/callout, not a table
    return [dict(rect=(x0, y0, x1, y1), xs=xs, ys=ys, vsegs=_vrule_segments(page))]


def extract_table(page, tab):
    """Read cells out of the grid by position.  Generic table finders mis-order glyphs
    on this kind of layout, so assign each text line to the cell its origin falls in."""
    xs, ys = tab["xs"], tab["ys"]
    ncol, nrow = len(xs) - 1, len(ys) - 1
    if ncol < 1 or nrow < 1 or ncol > 7 or nrow > 60:
        return None
    cells = defaultdict(list)
    for blk in page.get_text("rawdict")["blocks"]:
        if blk["type"] == 1:
            continue
        for ln in blk["lines"]:
            b = ln["bbox"]
            t = line_text(ln).strip()
            if not t or not (ys[0] - 2 <= b[1] <= ys[-1] + 2):
                continue
            r = max((i for i in range(nrow) if ys[i] <= b[1] + 3), default=None)
            c = max((i for i in range(ncol) if xs[i] <= b[0] + 3), default=None)
            if r is None or c is None:
                continue
            bold = any(span_is_bold(s) for s in ln["spans"])
            t = "".join(ch["c"] for sp in ln["spans"] for ch in sp["chars"]
                        if not BULLET_FONT_RE.search(sp.get("font", ""))).strip()
            if not t:
                continue
            cells[(r, c)].append((b[1], t, bold))
    if not cells:
        return None
    grid = [["" for _ in range(ncol)] for _ in range(nrow)]
    bold = [[False] * ncol for _ in range(nrow)]
    for (r, c), v in cells.items():
        v.sort()
        grid[r][c] = " ".join(x[1] for x in v)
        bold[r][c] = any(x[2] for x in v)
    if sum(1 for row in grid for c in row if c.strip()) < 2:
        return None
    spans = []                       # (row, first_col, last_col) for merged header cells
    vsegs = tab.get("vsegs") or []
    for r in range(nrow):
        ytop, ybot = ys[r], ys[r + 1]
        c = 0
        while c < ncol - 1:
            end = c
            while end < ncol - 1:
                xb = xs[end + 1]
                ruled = any(abs(vx - xb) <= 2.5 and vy0 <= ytop + 3 and vy1 >= ybot - 3
                            for vx, vy0, vy1 in vsegs)
                if ruled or grid[r][end + 1].strip():
                    break
                end += 1
            if end > c:
                spans.append((r, c, end))
            c = end + 1
    return dict(grid=grid, bold=bold, spans=spans)


def _text_chars_in(page, rect):
    n = 0
    for blk in page.get_text("blocks"):
        x0, y0, x1, y1 = blk[:4]
        if x0 >= rect[0] - 2 and y0 >= rect[1] - 2 and x1 <= rect[2] + 2 and y1 <= rect[3] + 2:
            n += len(blk[4].strip())
    return n


def detect_vector_figures(page, table_rects, lay):
    """Charts and diagrams drawn as vector art carry no embedded image, so find dense
    clusters of drawing operations and rasterise those regions."""
    boxes = []
    for d in page.get_drawings():
        r = d["rect"]
        if r.width < 2 and r.height < 2:
            continue
        if r.width > lay.page_w * 0.95 and r.height > lay.page_h * 0.95:
            continue                                    # page border / background
        boxes.append([r.x0, r.y0, r.x1, r.y1])
    if len(boxes) < 6:
        return []
    boxes.sort(key=lambda b: (b[1], b[0]))
    groups = []
    for b in boxes:
        placed = False
        for g in groups:
            if not (b[0] > g[2] + 24 or b[2] < g[0] - 24 or
                    b[1] > g[3] + 24 or b[3] < g[1] - 24):
                g[0] = min(g[0], b[0]); g[1] = min(g[1], b[1])
                g[2] = max(g[2], b[2]); g[3] = max(g[3], b[3]); g[4] += 1
                placed = True
                break
        if not placed:
            groups.append([b[0], b[1], b[2], b[3], 1])
    out = []
    page_area = lay.page_w * lay.page_h
    for g in groups:
        w, h = g[2] - g[0], g[3] - g[1]
        if g[4] < 6 or w * h < page_area * 0.04 or w < lay.page_w * 0.18 or h < 40:
            continue
        if any(not (g[2] < t[0] or g[0] > t[2] or g[3] < t[1] or g[1] > t[3])
               for t in table_rects):
            continue
        if _text_chars_in(page, g) > 300:
            continue          # a bordered callout or sidebar: keep its text, do not draw it
        out.append((g[0], g[1], g[2], g[3]))
    return out


def raster_region(page, rect, dpi=190):
    r = pymupdf.Rect(*rect) & page.rect
    if r.is_empty or r.width < 4 or r.height < 4:
        return None
    pix = page.get_pixmap(clip=r, dpi=dpi, alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def encode_image(im, outdir, stem, jpeg_quality=88, max_px=None):
    """Flat-colour diagrams stay crisp and small as palette PNG; photographs go to JPEG.
    Always strip ICC and drop to plain RGB, which old renderers handle without fuss.
    Resolution beyond what the output page can show is pure file size, so cap it."""
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im = im.convert("RGB")
    im.info.pop("icc_profile", None)
    lo, hi = im.convert("L").getextrema()
    if hi - lo < 6:
        return None                       # blank or single-colour: nothing to show
    # Resampling smooths flat-colour diagrams into gradients, which then no longer fit a
    # 256-colour palette, so only do it when the image is far larger than it can be shown.
    if max_px and im.width > max_px * 1.5:
        im = im.resize((int(max_px), max(1, int(im.height * max_px / im.width))),
                       Image.LANCZOS)
    opts = {}
    buf = io.BytesIO(); im.save(buf, "PNG", optimize=True); opts["png"] = buf.getvalue()
    try:
        q = im.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG)
        buf = io.BytesIO(); q.save(buf, "PNG", optimize=True); opts["png256"] = buf.getvalue()
    except Exception:
        pass
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=jpeg_quality, optimize=True)
    opts["jpg"] = buf.getvalue()
    pick = min(opts, key=lambda k: len(opts[k]) * (1.0 if k != "jpg" else 1.6))
    path = os.path.join(outdir, f"{stem}.{'jpg' if pick == 'jpg' else 'png'}")
    with open(path, "wb") as fh:
        fh.write(opts[pick])
    return path, im.size


# ------------------------------------------------------------ extraction & reading order

def first_word_width(line):
    chars = [ch for s in line["spans"] for ch in s["chars"]]
    while chars and chars[0]["c"].isspace():
        chars.pop(0)
    if not chars:
        return None
    x0, last = chars[0]["bbox"][0], chars[0]
    for ch in chars:
        if ch["c"].isspace():
            break
        last = ch
    return last["bbox"][2] - x0


def rich_runs(line, italic_boxes):
    """Text split into (text, bold, italic) runs, with bullet glyph fonts dropped."""
    out = []
    for s in line["spans"]:
        if BULLET_FONT_RE.search(s.get("font", "")):
            continue
        bold = span_is_bold(s)
        ital = span_is_italic(s)
        for ch in s["chars"]:
            c = ch["c"].replace("（", " (").replace("）", ") ")
            if c == "\u00ad":
                continue
            it = ital
            if not it and italic_boxes:
                cx = (ch["bbox"][0] + ch["bbox"][2]) / 2
                cy = (ch["bbox"][1] + ch["bbox"][3]) / 2
                it = any(b[0] - 0.6 <= cx <= b[2] + 0.6 and b[1] - 3 <= cy <= b[3] + 3
                         for b in italic_boxes)
            if out and out[-1][1] == bold and out[-1][2] == it:
                out[-1][0] += c
            else:
                out.append([c, bold, it])
    return [(t, b, i) for t, b, i in out]


def coalesce_line_fragments(items):
    """Some producers emit a visual line as several text objects, one per style run.
    Rejoin neighbours that share a baseline and sit next to each other, so the paragraph
    builder sees real lines.  The gap test keeps a list marker separate from its text,
    which is set at a tab stop much further out."""
    out = []
    for it in items:
        if (out and it.get("kind") in ("body", "head", "cap")
                and out[-1].get("kind") == it.get("kind")
                and out[-1].get("col") == it.get("col")
                and not out[-1].get("bullet") and not it.get("bullet")
                and abs(it["y0"] - out[-1]["y0"]) < 2.5
                and -1.0 <= it["x0"] - out[-1]["x1"] < 8.0):
            prev = out[-1]
            gap = it["x0"] - prev["x1"]
            join = " " if (gap > 1.2 and not prev["text"].endswith(" ")
                           and not it["text"].startswith(" ")) else ""
            prev["text"] = prev["text"] + join + it["text"]
            if join and prev["rich"]:
                prev["rich"] = list(prev["rich"][:-1]) + [
                    (prev["rich"][-1][0] + join,) + tuple(prev["rich"][-1][1:])]
            prev["rich"] = list(prev["rich"]) + list(it["rich"])
            prev["x1"] = max(prev["x1"], it["x1"])
            prev["y1"] = max(prev["y1"], it["y1"])
            prev["size"] = max(prev["size"], it["size"])
            continue
        out.append(it)
    return out


def extract(doc, pages, lay, opts, imgdir):
    """Everything on the selected pages, in reading order."""
    items, nfig, ntab = [], 0, 0
    for pno in pages:
        page = doc[pno]
        colw = max(c[1] - c[0] for c in lay.cols)   # figure scale uses the document model
        text_w = opts.page_size[0] - 2 * opts.margin
        def cap(frac):
            return max(180, int(text_w * min(1.0, max(0.32, frac)) * opts.image_ppi / 72))
        tables = [] if opts.no_tables else detect_tables(page)
        trects = [t["rect"] for t in tables]
        page_items = []

        if not opts.no_images:
            seen = set()
            for info in page.get_images(full=True):
                xref = info[0]
                for r in page.get_image_rects(xref):
                    if (r.width < 34 or r.height < 34
                            or r.width * r.height < lay.page_w * lay.page_h * 0.004):
                        continue                       # decorative icon, not a figure
                    if (r.width > lay.page_w * 0.88 and r.height > lay.page_h * 0.88
                            and r.x0 < lay.page_w * 0.08 and r.y0 < lay.page_h * 0.08
                            and len(page.get_text().strip()) > 400):
                        continue                       # page scan behind a text layer
                    key = (round(r.x0), round(r.y0), xref)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        raw = doc.extract_image(xref)
                        im = Image.open(io.BytesIO(raw["image"]))
                    except Exception:
                        im = raster_region(page, tuple(r), opts.dpi)
                    if im is None:
                        continue
                    frac = min(1.0, r.width / max(1.0, colw))
                    enc = encode_image(im, imgdir, f"fig{nfig + 1:03d}",
                                       opts.jpeg_quality, cap(frac))
                    if enc is None:
                        continue
                    nfig += 1
                    path, size = enc
                    page_items.append(dict(kind="img", y0=r.y0, y1=r.y1, x0=r.x0, x1=r.x1,
                                           img=path, px=list(size), frac=frac))
            for vr in detect_vector_figures(page, trects, lay):
                if any(not (vr[2] < it["x1"] or vr[0] > it["x0"] or
                            vr[3] < it["y0"] or vr[1] > it["y1"])
                       for it in page_items if it["kind"] == "img"):
                    continue
                im = raster_region(page, vr, opts.dpi)
                if im is None:
                    continue
                frac = min(1.0, (vr[2] - vr[0]) / max(1.0, colw))
                enc = encode_image(im, imgdir, f"fig{nfig + 1:03d}",
                                   opts.jpeg_quality, cap(frac))
                if enc is None:
                    continue
                nfig += 1
                path, size = enc
                page_items.append(dict(kind="img", y0=vr[1], y1=vr[3], x0=vr[0], x1=vr[2],
                                       img=path, px=list(size), frac=frac))

        for tab in tables:
            data = extract_table(page, tab)
            ntab += 1
            if data:
                page_items.append(dict(kind="table", y0=tab["rect"][1], y1=tab["rect"][3],
                                       x0=tab["rect"][0], x1=tab["rect"][2], **data))
            else:
                im = raster_region(page, tab["rect"], opts.dpi)
                if im is not None:
                    nfig += 1
                    path, size = encode_image(im, imgdir, f"tab{ntab:03d}", opts.jpeg_quality)
                    page_items.append(dict(kind="img", y0=tab["rect"][1], y1=tab["rect"][3],
                                           x0=tab["rect"][0], x1=tab["rect"][2],
                                           img=path, px=list(size)))

        # Books mix layouts - a two-column summary next to a full-width chapter opener -
        # so re-measure each page and fall back to the document model when a page is too
        # sparse to judge on its own.
        pcols, pgut, pmeas = lay.cols, lay.gutter, lay.measures
        if opts.columns is None:
            pboxes = [ln["bbox"] for blk in page.get_text("rawdict")["blocks"]
                      if blk["type"] == 0 for ln in blk["lines"]
                      if line_text(ln).strip() and lay.top_y <= ln["bbox"][1] <= lay.bot_y]
            got = columns_from_boxes(pboxes, lay.page_w, None, min_lines=26)
            if got:
                pcols, pgut, pmeas = got
        plocal = Layout(page_w=lay.page_w, page_h=lay.page_h, top_y=lay.top_y,
                        bot_y=lay.bot_y, cols=pcols, gutter=pgut, measures=pmeas,
                        body_size=lay.body_size, head_sizes=lay.head_sizes,
                        indent=lay.indent)

        ital = [] if opts.no_italics else skew_italic_boxes(page)
        raw_lines = []
        for blk in page.get_text("rawdict")["blocks"]:
            if blk["type"] == 1:
                continue
            for ln in blk["lines"]:
                b = ln["bbox"]
                if b[1] < lay.top_y or b[1] > lay.bot_y:
                    continue
                if any(t[0] - 2 <= b[0] and b[1] >= t[1] - 2 and b[3] <= t[3] + 2
                       and b[2] <= t[2] + 2 for t in trects):
                    continue                                  # text inside a table
                raw_lines.append(ln)

        others = [ln["bbox"][1] for ln in raw_lines
                  if plocal.column_of(ln["bbox"][0], ln["bbox"][2]) >= 1]
        last_other = max(others) if others else -1e9

        for ln in raw_lines:
            b = ln["bbox"]
            txt = line_text(ln).replace("（", " (").replace("）", ") ")
            spans = ln["spans"]
            size = max(s["size"] for s in spans)
            col = plocal.column_of(b[0], b[2])
            if col >= 0 and b[1] > last_other + 40:
                col = -1                                      # past the columned part of the page
            if col < 0:
                base = plocal.cols[0][0]
                margin = plocal.measures[-1] if plocal.measures else plocal.cols[-1][1]
            else:
                base = plocal.cols[col][0]
                margin = (plocal.measures[col] if col < len(plocal.measures)
                          else plocal.cols[col][1])
            rounded = round(size * 2) / 2
            lvl = lay.head_sizes.get(rounded)
            bullet = any(BULLET_FONT_RE.search(s.get("font", "")) for s in spans)
            stripped = txt.strip()
            if not bullet and len(stripped) <= 2 and stripped and all(c in BULLET_CHARS
                                                                      for c in stripped):
                bullet = True
            if lvl is not None and stripped:
                kind, level = "head", lvl
            elif CAPTION_RE.match(txt):
                kind, level = "cap", 0
            else:
                kind, level = "body", 0
            if kind == "body" and opts.section_headings and stripped and not bullet:
                allbold = spans and all(span_is_bold(s) for s in spans)
                short = (b[2] - b[0]) < (margin - base) * 0.72
                if (allbold and len(stripped) < 90
                        and not stripped.endswith((".", ",", ";", ":"))):
                    m = SECTION_HEAD_RE.match(stripped)
                    if m:
                        kind, level = "head", min(2, m.group(1).count("."))
                    elif short:
                        kind, level = "head", 2
            page_items.append(dict(
                kind=kind, level=level, y0=b[1], y1=b[3], x0=b[0], x1=b[2],
                text=txt, rich=rich_runs(ln, ital), bullet=bullet, size=size,
                col=col, base=base, margin=margin,
                indent=max(0, round((b[0] - base) / lay.indent)) if lay.indent else 0,
                fw=first_word_width(ln),
                ralign=(col < 0 and plocal.ncols > 0 and b[2] >= margin - 2.5
                        and b[0] > base + (margin - base) * 0.55
                        and (b[2] - b[0]) < (margin - base) * 0.38)))

        page_items.sort(key=lambda i: (round(i["y0"] / 5.0), i["x0"]))
        page_items = coalesce_line_fragments(page_items)

        # reading order: a full-width element closes the columned band above it
        pend = defaultdict(list)
        def flush(_cols=plocal.cols):
            for c in range(len(_cols)):
                lines = pend[c]
                while lines and not lines[0].get("text", " ").strip():
                    lines.pop(0)
                if c < len(_cols) - 1:         # trailing blank in a non-final column is
                    tail = []                  # column fill, unless the text really ended
                    while lines and not lines[-1].get("text", " ").strip():
                        tail.append(lines.pop())
                    if lines and tail and (_cols[c][1] - lines[-1]["x1"]) >= 60:
                        lines.append(tail[-1])
                for it in lines:
                    it["page"] = pno + 1
                    items.append(it)
                pend[c] = []

        for it in page_items:
            if it["kind"] in ("head", "cap", "img", "table") or it.get("col", -1) < 0:
                flush()
                it["page"] = pno + 1
                items.append(it)
            else:
                pend[it["col"]].append(it)
        flush()
    return items


# --------------------------------------------------------------------- block assembly

SPACE_W, FIT_TOL = 3.0, 8.0


def _join(acc, piece, drop_hyphen=False):
    """Glue a wrapped line onto the paragraph.  A broken URL must not gain a space, and an
    em dash is punctuation that keeps its spacing.  A trailing hyphen is either a word the
    typesetter split (justified text - close it up) or part of a compound (ragged text -
    keep it)."""
    if not acc:
        return [list(p) for p in piece]
    prev = acc[-1][0].rstrip()
    tail = prev.split()[-1] if prev.split() else ""
    nxt_first = (piece[0][0].lstrip()[:1] if piece and piece[0][0].strip() else "")
    if (prev.endswith("-") and drop_hyphen and len(tail) > 2
            and "-" not in tail[:-1]            # 'state-of-the-' is a compound, not a split
            and not nxt_first.isupper()):       # 'Anglo-' + 'Saxon' likewise
        prev = prev[:-1]
        sep = ""
    else:
        sep = "" if (prev.endswith(("-", "–")) or URLISH_RE.search(tail)) else " "
    acc = acc[:-1] + [[prev] + list(acc[-1][1:])]
    if not piece:
        return acc
    head = list(piece[0])
    head[0] = sep + head[0].lstrip()
    return acc + [head] + [list(p) for p in piece[1:]]


BARE_NUM_RE = re.compile(r"^\(?(\d{1,3}(?:\.\d{1,3})*)\.?\)?$")


def merge_tabbed_numbers(items):
    """Specs often set a section number and its title as two text objects on one
    baseline ('1' [tab] 'Scope').  Rejoin them, but only when the following text returns
    to the column edge - if it is indented, this is a numbered list, not a heading."""
    out, i = [], 0
    while i < len(items):
        it = items[i]
        if (it["kind"] == "body" and it["text"].strip()
                and BARE_NUM_RE.match(it["text"].strip())
                and not _marker_followed_by_indent(items, i) and i + 1 < len(items)):
            nxt = items[i + 1]
            if (nxt["kind"] in ("body", "head") and nxt.get("col") == it.get("col")
                    and abs(nxt["y0"] - it["y0"]) < 3 and nxt["x0"] > it["x1"]
                    and nxt["text"].strip()):
                m = dict(nxt)
                lead = it["text"].strip() + "  "
                bold = nxt["rich"][0][1] if nxt["rich"] else False
                m["text"] = lead + nxt["text"]
                m["rich"] = [(lead, bold, False)] + [tuple(r) for r in nxt["rich"]]
                m["x0"] = it["x0"]
                out.append(m)
                i += 2
                continue
        out.append(it)
        i += 1
    return out


def assemble(items, lay, opts):
    items = merge_tabbed_numbers(items)
    dehyph = lay.justified
    blocks, cur = [], None

    def close():
        nonlocal cur
        if cur is not None:
            txt = "".join(r[0] for r in cur["rich"]).strip()
            if txt:
                cur["text"] = txt
                for k in ("_x1", "_margin", "_col", "_ind"):
                    cur.pop(k, None)
                blocks.append(cur)
        cur = None

    def stamp(it):
        cur["_x1"] = it["x1"]; cur["_margin"] = it["margin"]
        cur["_col"] = it["col"]; cur["_ind"] = it["indent"]

    def hard_break(it):
        """Text only wraps when the next word will not fit.  If it would have fit on the
        previous line, someone broke the line deliberately - an address, a list of names -
        and that break carries meaning."""
        if cur is None or not cur.get("rich") or cur.get("_col") != it["col"]:
            return False
        if cur.get("_x1") is None or it.get("fw") is None:
            return False
        return cur["_x1"] + SPACE_W + it["fw"] <= cur["_margin"] - FIT_TOL

    def new(kind, it, **kw):
        nonlocal cur
        close()
        cur = dict(type=kind, rich=[list(r) for r in it.get("rich", [])],
                   page=it["page"], **kw)
        stamp(it)

    for idx, it in enumerate(items):
        k = it["kind"]
        if k == "img":
            close()
            blocks.append(dict(type="fig", page=it["page"], img=it["img"],
                               px=it["px"], frac=it.get("frac", 1.0), text=""))
            continue
        if k == "table":
            close()
            blocks.append(dict(type="table", page=it["page"], grid=it["grid"],
                               bold=it["bold"], spans=it.get("spans") or [], text=""))
            continue
        if k in ("head", "cap"):
            t = f"h{it['level'] + 1}" if k == "head" else "cap"
            # a heading or caption wrapped onto a second line is still one heading, even
            # though the short tail no longer spans the gutter
            if cur is not None and cur["type"] == t:
                cur["rich"] = _join(cur["rich"], it["rich"], dehyph); stamp(it)
            else:
                new(t, it)
            continue
        if not it["text"].strip():
            close()
            continue
        ind, stripped = it["indent"], it["text"].strip()
        marker = it["bullet"] or (NUMBERED_MARKER_RE.match(stripped) and ind == 0
                                  and _marker_followed_by_indent(items, idx))
        if marker:
            m = NUMBERED_MARKER_RE.match(stripped)
            close()
            cur = dict(type="li", level=max(1, ind + 1), page=it["page"],
                       marker=(stripped if m else None), rich=[])
            stamp(it)
            continue
        if it.get("ralign"):
            new("right", it)
            continue
        zone_change = (cur is not None and cur.get("_col") != it["col"]
                       and -1 in (cur.get("_col"), it["col"]))
        if (cur is not None and cur["type"] == "li" and ind == 0
                and cur.get("_ind", 0) > 0):
            new("p", it)                     # back at column level: the list has ended
            continue
        if cur is not None and cur["type"] in ("p", "li") and not zone_change \
                and not hard_break(it):
            cur["rich"] = _join(cur["rich"], it["rich"], dehyph); stamp(it)
            continue
        new("p", it)
    close()
    return blocks


def _marker_followed_by_indent(items, idx):
    """A bare '1.' is a list marker only when indented text follows it; otherwise it is
    the tail of something like '(Figure 25)' broken across lines."""
    for nxt in items[idx + 1:]:
        if nxt["kind"] != "body" or not nxt["text"].strip():
            continue
        return nxt["indent"] >= 1
    return False


# ------------------------------------------------------------------------- rendering

TRANSLIT = {
    # only characters WinAnsi/cp1252 genuinely lacks - curly quotes, en/em dashes,
    # bullets and the like are all present there and must be left alone
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>", "⇐": "<=", "≤": "<=", "≥": ">=",
    "≈": "~", "≠": "!=", "∼": "~", "−": "-", "‐": "-", "‑": "-", "―": "-", "‒": "-",
    "℃": "degC", "℉": "degF", "№": "No.", "∞": "inf", "·": "-", "‖": "||",
    "　": " ", "、": ", ", "。": ". ", "･": "-", "（": " (", "）": ") ",
    "「": ' "', "」": '" ', "『": ' "', "』": '" ',
}


def _translit(s):
    out = []
    for c in s:
        if c in TRANSLIT:
            out.append(TRANSLIT[c])
        elif "\uff01" <= c <= "\uff5e":        # full-width ASCII forms
            out.append(chr(ord(c) - 0xFEE0))
        else:
            out.append(c)
    return "".join(out)


def choose_fonts(blocks, quiet=False):
    """Base-14 fonts need no embedding and are understood by every reader ever shipped,
    so keep the text inside WinAnsi wherever that costs nothing.  Ligatures, full-width
    forms and accents all decompose cleanly; only genuinely foreign scripts force an
    embedded font."""
    for b in blocks:
        if b.get("rich"):
            b["rich"] = [(_translit(t), bo, it) for t, bo, it in b["rich"]]
        if b.get("grid"):
            b["grid"] = [[_translit(c) for c in row] for row in b["grid"]]

    def scan():
        text = "".join(r[0] for b in blocks for r in b.get("rich", []))
        text += "".join(c for b in blocks for row in b.get("grid", []) for c in row)
        bad = Counter()
        for c in text:
            try:
                c.encode("cp1252")
            except Exception:
                bad[c] += 1
        return bad

    base14 = ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique")
    bad = scan()
    if not bad:
        return base14, None

    # step 1: compatibility-decompose what we can (ﬁ -> fi, ％ -> %, ź -> z)
    sub, folded = {}, 0
    for c in list(bad):
        d = "".join(ch for ch in unicodedata.normalize("NFKD", c)
                    if not unicodedata.combining(ch))
        if d and d != c:
            try:
                d.encode("cp1252")
            except Exception:
                continue
            sub[c] = d
            folded += bad[c]
    if sub:
        _apply_sub(blocks, sub)
        bad = scan()
    if not bad:
        log(f"  fonts:   base-14; folded {folded} character(s) to ASCII "
            f"({', '.join(repr(k) for k in list(sub)[:4])})", quiet)
        return base14, None

    nbad = sum(bad.values())
    if nbad <= 24 and len(bad) <= 6:
        _apply_sub(blocks, {c: "?" for c in bad})
        log(f"  fonts:   base-14; replaced {nbad} stray character(s) "
            f"({', '.join(repr(k) for k in bad)}) with '?'", quiet)
        return base14, None

    for reg, bold, ital, bi in UNICODE_FONTS:
        if not os.path.exists(reg):
            continue
        try:
            pdfmetrics.registerFont(TTFont("Body", reg))
            for name, path in (("Body-B", bold), ("Body-I", ital), ("Body-BI", bi)):
                pdfmetrics.registerFont(TTFont(name, path if path and os.path.exists(path) else reg))
            note = (f"embedded {os.path.basename(reg)} for {len(bad)} characters outside "
                    f"WinAnsi ({nbad} occurrences)")
            if not (bold and os.path.exists(bold)):
                note += "; single weight, so bold may not look different"
            log(f"  fonts:   {note}", quiet)
            return ("Body", "Body-B", "Body-I", "Body-BI"), note
        except Exception:
            continue

    worst = ", ".join(repr(c) for c, _ in bad.most_common(6))
    log(f"  fonts:   WARNING no Unicode font found; replacing {nbad} characters "
        f"({worst}) with '?'", quiet)
    _apply_sub(blocks, {c: "?" for c in bad})
    return base14, None


def _apply_sub(blocks, sub):
    tr = str.maketrans(sub)
    for b in blocks:
        if b.get("rich"):
            b["rich"] = [(t.translate(tr), bo, it) for t, bo, it in b["rich"]]
        if b.get("grid"):
            b["grid"] = [[c.translate(tr) for c in row] for row in b["grid"]]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rich_markup(runs):
    out = []
    for t, b, i in runs:
        s = esc(t)
        if b:
            s = f"<b>{s}</b>"
        if i:
            s = f"<i>{s}</i>"
        out.append(s)
    return "".join(out).strip()


def build_pdf(blocks, out_path, opts, fonts, title, subtitle):
    reg, bold, ital, boldital = fonts
    PW, PH = opts.page_size
    ML = MR = opts.margin
    MT, MB = opts.margin + 4, opts.margin + 8
    FW = PW - ML - MR
    fs = opts.font_size
    lead = fs * 1.42

    body = ParagraphStyle("body", fontName=reg, fontSize=fs, leading=lead,
                          alignment=TA_LEFT, spaceAfter=fs * 0.82,
                          textColor=colors.black, allowWidows=0, allowOrphans=0)
    heads = [ParagraphStyle(f"h{i}", parent=body, fontName=bold,
                            fontSize=fs * s, leading=fs * s * 1.24,
                            spaceBefore=fs * (1.5 - 0.25 * i), spaceAfter=fs * (1.0 - 0.15 * i),
                            keepWithNext=1, outlineLevel=i)
             for i, s in enumerate((1.55, 1.2, 1.05))]
    cap = ParagraphStyle("cap", parent=body, fontName=ital, fontSize=fs * 0.82,
                         leading=fs * 1.1, alignment=TA_CENTER, spaceBefore=fs * 0.5,
                         spaceAfter=fs * 1.25, textColor=colors.Color(.28, .28, .28))
    lis = [ParagraphStyle(f"li{n}", parent=body, leftIndent=fs * 1.45 * n,
                          bulletIndent=fs * 1.45 * (n - 1), spaceAfter=fs * 0.55,
                          leading=lead * 0.96) for n in (1, 2, 3, 4)]
    rightst = ParagraphStyle("right", parent=body, alignment=TA_RIGHT,
                             fontSize=fs * 0.88, leading=fs * 1.15, spaceAfter=2)
    titlest = ParagraphStyle("title", parent=body, fontName=bold, fontSize=fs * 1.27,
                             leading=fs * 1.6, spaceAfter=4)
    subst = ParagraphStyle("sub", parent=body, fontSize=fs * 0.86, leading=fs * 1.18,
                           textColor=colors.Color(.35, .35, .35), spaceAfter=fs * 1.8)
    cellst = ParagraphStyle("cell", fontName=reg, fontSize=max(6.4, fs * 0.69),
                            leading=max(8.0, fs * 0.87))
    cellhd = ParagraphStyle("cellhd", parent=cellst, fontName=bold)

    def make_table(b):
        grid, bmap = b["grid"], b["bold"]
        ncol = max(len(r) for r in grid)
        mins = []
        for c in range(ncol):
            w = 0
            for r, row in enumerate(grid):
                cell = row[c] if c < len(row) else ""
                for word in cell.split():
                    fname = cellhd.fontName if (r == 0 or c == 0) else cellst.fontName
                    w = max(w, pdfmetrics.stringWidth(word, fname, cellst.fontSize))
            mins.append(w + 8)
        total = sum(mins) or 1
        widths = ([m / total * FW for m in mins] if total > FW
                  else [m + (FW - total) / ncol for m in mins])
        pad = 4
        floor = 14.0
        if min(widths) < floor:            # too many columns to honour the ideal split
            pad = 1
            floor = 9.0
            widths = [max(floor, w) for w in widths]
            scale = FW / sum(widths)
            widths = [w * scale for w in widths]
            if min(widths) < 7:            # hopeless: give every column an equal share
                widths = [FW / ncol] * ncol
        data = [[Paragraph(esc(row[c] if c < len(row) else ""),
                           cellhd if (r == 0 or c == 0) else cellst)
                 for c in range(ncol)] for r, row in enumerate(grid)]
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [("SPAN", (c0, r), (c1, r)) for r, c0, c1 in b.get("spans", [])]
        t.setStyle(TableStyle(style + [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(.55, .55, .55)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(.90, .90, .90)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    story = []
    if title:
        story.append(Paragraph(esc(title), titlest))
    if subtitle:
        story.append(Paragraph(esc(subtitle), subst))

    pending = None
    maxh = (PH - MT - MB) * opts.max_figure
    for i, b in enumerate(blocks):
        t = b["type"]
        if t == "fig":
            w, h = b["px"]
            target = FW * (1.0 if b.get("frac", 1.0) > 0.72 else max(0.32, b["frac"]))
            iw, ih = target, target * h / max(1, w)
            if ih > maxh:
                ih, iw = maxh, maxh * w / max(1, h)
            pending = RLImage(b["img"], width=iw, height=ih)
            continue
        if t == "cap":
            para = Paragraph(rich_markup(b["rich"]), cap)
            nxt = blocks[i + 1]["type"] if i + 1 < len(blocks) else None
            if pending is not None:
                story.append(KeepTogether([Spacer(1, 6), pending, para])); pending = None
            elif nxt == "table":
                story.append(para)          # table captions sit above their table
            else:
                story.append(para)
            continue
        if pending is not None:
            story.append(KeepTogether([Spacer(1, 6), pending, Spacer(1, 10)])); pending = None
        if t == "table":
            story.append(KeepTogether([make_table(b), Spacer(1, 12)]))
            continue
        if t.startswith("h") and t[1:].isdigit():
            story.append(Paragraph(rich_markup(b["rich"]), heads[min(2, int(t[1:]) - 1)]))
            continue
        if t == "right":
            story.append(Paragraph(rich_markup(b["rich"]), rightst))
            continue
        if t == "li":
            style = lis[min(len(lis), b.get("level", 1)) - 1]
            story.append(Paragraph(rich_markup(b["rich"]), style,
                                   bulletText=b.get("marker") or "•"))
            continue
        story.append(Paragraph(rich_markup(b["rich"]), body))
    if pending is not None:
        story.append(pending)

    class Doc(BaseDocTemplate):
        _bm = 0
        _last = -1
        def afterFlowable(self, flow):
            lvl = getattr(getattr(flow, "style", None), "outlineLevel", None)
            if lvl is None:
                return
            lvl = min(lvl, Doc._last + 1)      # readers reject outlines that skip a level
            Doc._last = lvl
            Doc._bm += 1
            key = f"bm{Doc._bm}"
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flow.getPlainText()[:120], key, level=lvl, closed=0)

    def decorate(canvas, d):
        canvas.saveState()
        canvas.setFont(reg if reg != "Helvetica" else "Helvetica", max(6.5, fs * 0.7))
        canvas.setFillGray(0.45)
        canvas.drawCentredString(PW / 2, MB * 0.45, str(canvas.getPageNumber()))
        canvas.restoreState()

    doc = Doc(out_path, pagesize=(PW, PH), leftMargin=ML, rightMargin=MR,
              topMargin=MT, bottomMargin=MB, title=title or "", author="",
              subject="Simplified single-column edition", creator="pdfsimplify")
    doc.addPageTemplates([PageTemplate(id="p", frames=[
        Frame(ML, MB, FW, PH - MT - MB, id="main", leftPadding=0, rightPadding=0,
              topPadding=0, bottomPadding=0)], onPage=decorate)])
    doc.multiBuild(story)
    return doc.page


# ------------------------------------------------------------------------------- CLI

def finalise(path, quiet=False):
    """Linearise so an old reader can show page 1 without parsing the whole file, and
    keep object streams off for maximum reader compatibility.  Optional: qpdf may be
    absent, and the file is already valid without it."""
    qpdf = shutil.which("qpdf")
    if qpdf:
        tmp = path + ".tmp"
        r = subprocess.run([qpdf, "--linearize", "--object-streams=disable",
                            "--stream-data=compress", path, tmp],
                           capture_output=True)
        if r.returncode in (0, 3) and os.path.exists(tmp):
            os.replace(tmp, path)
            return
        if os.path.exists(tmp):
            os.remove(tmp)
    log("  note:    qpdf not available, so the output is not linearised "
        "(harmless; install qpdf for slightly faster opening)", quiet)


def parse_page_size(s):
    if s in PAGE_SIZES:
        return PAGE_SIZES[s]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"page size must be one of {', '.join(PAGE_SIZES)} or WxH in points")
    return (float(m.group(1)), float(m.group(2)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="pdfsimplify",
        description="Reflow a PDF into a simple single-column PDF for small or old screens.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  pdfsimplify paper.pdf
  pdfsimplify book.pdf --pages 26-50 -o chapter5.pdf
  pdfsimplify report.pdf --page-size phone --font-size 12
  pdfsimplify unknown.pdf --dry-run        # show what was detected, write nothing
""")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help='default: "<input> (simplified).pdf"')
    ap.add_argument("--pages", help="e.g. 26-50, 1,4,7-9, 30- (default: all)")
    ap.add_argument("--page-size", type=parse_page_size, default=PAGE_SIZES["tablet"],
                    metavar="NAME|WxH",
                    help=f"{', '.join(PAGE_SIZES)} or points (default: tablet, 432x576)")
    ap.add_argument("--font-size", type=float, default=11.0, help="body point size (default: 11)")
    ap.add_argument("--margin", type=float, default=30.0, help="page margin in points (default: 30)")
    ap.add_argument("--columns", type=int, default=None, metavar="N",
                    help="override detected column count (use 1 to force single column)")
    ap.add_argument("--max-figure", type=float, default=0.52, metavar="F",
                    help="cap figure height at F of the text area (default: 0.52)")
    ap.add_argument("--dpi", type=int, default=190, help="rasterising DPI for vector figures")
    ap.add_argument("--image-ppi", type=int, default=200, metavar="PPI",
                    help="resolution figures are stored at, relative to their printed "
                         "size (default: 200; lower means a smaller file)")
    ap.add_argument("--jpeg-quality", type=int, default=88)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-tables", action="store_true", help="do not try to rebuild tables")
    ap.add_argument("--no-italics", action="store_true",
                    help="skip content-stream scan for faked italics (faster)")
    ap.add_argument("--section-headings", action="store_true",
                    help="also treat bold numbered lines like '3.2 Method' as headings")
    ap.add_argument("--title", help="title line on page 1 (default: from PDF metadata)")
    ap.add_argument("--subtitle", help="subtitle line on page 1")
    ap.add_argument("--no-header", action="store_true", help="omit the title block entirely")
    ap.add_argument("--dry-run", action="store_true",
                    help="report detected layout and a text preview; write no PDF")
    ap.add_argument("--text", metavar="FILE", help="also write the reflowed text here")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=f"pdfsimplify {__version__}")
    opts = ap.parse_args(argv)

    if not os.path.exists(opts.input):
        sys.exit(f"pdfsimplify: no such file: {opts.input}")
    try:
        doc = pymupdf.open(opts.input)
    except Exception as e:
        sys.exit(f"pdfsimplify: cannot open {opts.input}: {e}")
    if doc.needs_pass:
        sys.exit("pdfsimplify: the PDF is password protected; open it with the password "
                 "and save a copy first")

    try:
        pages = parse_pages(opts.pages, len(doc))
    except ValueError as e:
        sys.exit(f"pdfsimplify: {e}")

    log(f"pdfsimplify: {os.path.basename(opts.input)}  "
        f"({len(doc)} pages, using {len(pages)})", opts.quiet)

    chars = sum(len(doc[p].get_text().strip()) for p in pages[:12])
    if chars < 120 * min(12, len(pages)):
        sys.exit("pdfsimplify: this PDF has little or no text layer - it looks scanned.\n"
                 "             Run OCR first (e.g. ocrmypdf in.pdf out.pdf), then retry.")

    lay = analyse(doc, pages, opts.columns, opts.quiet)
    if lay is None:
        sys.exit("pdfsimplify: could not find enough text to measure the layout")

    log(f"  layout:  {lay.ncols} column(s)"
        + (f", gutter {lay.gutter[0]:.0f}-{lay.gutter[1]:.0f}" if lay.gutter else "")
        + f", body {lay.body_size:g}pt, indent {lay.indent:g}pt"
        + (", justified (rejoining hyphenated words)" if lay.justified else ", ragged"),
        opts.quiet)
    log(f"  columns: " + ", ".join(f"{a:.0f}-{b:.0f}" for a, b in lay.cols), opts.quiet)
    log(f"  body:    y {lay.top_y:.0f}-{lay.bot_y:.0f} of {lay.page_h:.0f}"
        + (f"; dropped running text {lay.chrome[:2]}" if lay.chrome else ""), opts.quiet)
    log(f"  headings: " + (", ".join(f"{s:g}pt->h{l+1}" for s, l in
                                     sorted(lay.head_sizes.items(), reverse=True))
                           or "none found by size"), opts.quiet)

    imgdir = tempfile.mkdtemp(prefix="pdfsimplify-")
    try:
        items = extract(doc, pages, lay, opts, imgdir)
        blocks = assemble(items, lay, opts)
        return _finish(doc, pages, blocks, lay, opts)
    finally:
        shutil.rmtree(imgdir, ignore_errors=True)


def _finish(doc, pages, blocks, lay, opts):

    kinds = Counter(b["type"] for b in blocks)
    words = sum(len(b.get("text", "").split()) for b in blocks)
    log(f"  content: {words} words, " +
        ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())), opts.quiet)

    if opts.text or opts.dry_run:
        lines = []
        for b in blocks:
            t = b.get("text", "")
            if b["type"] == "fig":
                lines.append(f"[FIGURE {b['px'][0]}x{b['px'][1]}]")
            elif b["type"] == "table":
                lines.append("[TABLE " + " | ".join(b["grid"][0]) + "]")
            elif b["type"].startswith("h") and b["type"][1:].isdigit():
                lines.append("\n" + "#" * int(b["type"][1:]) + " " + t)
            elif b["type"] == "cap":
                lines.append(f"   _{t}_")
            elif b["type"] == "li":
                lines.append("  " * b.get("level", 1) + f"{b.get('marker') or '-'} {t}")
            else:
                lines.append("\n" + t)
        text = "\n".join(lines)
        if opts.text:
            with open(opts.text, "w") as fh:
                fh.write(text + "\n")
            log(f"  text:    {opts.text}", opts.quiet)
        if opts.dry_run:
            print(text[:4000])
            if len(text) > 4000:
                print(f"\n... [{len(text) - 4000} more characters; "
                      f"use --text FILE for all of it]")
            return 0

    fonts, _ = choose_fonts(blocks, opts.quiet)
    meta = doc.metadata or {}
    title = None if opts.no_header else (opts.title or meta.get("title") or
                                         os.path.splitext(os.path.basename(opts.input))[0])
    if opts.no_header:
        subtitle = None
    elif opts.subtitle:
        subtitle = opts.subtitle
    else:
        rng = (f"pp. {pages[0]+1}–{pages[-1]+1} · " if len(pages) < len(doc) else "")
        subtitle = f"{rng}reflowed single-column edition for small screens"

    out = opts.output or os.path.join(
        os.path.dirname(os.path.abspath(opts.input)),
        os.path.splitext(os.path.basename(opts.input))[0] + " (simplified).pdf")
    try:
        npages = build_pdf(blocks, out, opts, fonts, title, subtitle)
    except Exception as e:
        sys.exit(f"pdfsimplify: failed while building the PDF: {e}")
    finalise(out, opts.quiet)

    size = os.path.getsize(out)
    log(f"  wrote:   {out}  ({npages} pages, {size/1024:.0f} KB)", opts.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
