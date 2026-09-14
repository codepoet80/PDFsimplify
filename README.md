# pdfsimplify

Reflow a PDF laid out for print into a simple single-column PDF that is comfortable on a
small screen and opens on an old reader.

```
pdfsimplify paper.pdf                       # -> "paper (simplified).pdf"
pdfsimplify book.pdf --pages 26-50 -o ch5.pdf
pdfsimplify unknown.pdf --dry-run           # show what it detected, write nothing
```

## What it does

The usual obstacle to reading a paper or report on a tablet is not the file format, it is
the layout: two narrow columns of 9pt type on an A4 page means zooming and panning on
every page. `pdfsimplify` rebuilds the document as **one column at a readable size on a
small page**, so fit-to-width and fit-to-page are the same thing.

It keeps:

- **reading order** across columns, figures and page breaks
- **figures**, re-encoded to plain RGB at a resolution matched to the output page
- **tables**, rebuilt as real tables (including merged header cells) where the grid is
  clean, rasterised where it is not
- **headings**, as headings *and* as PDF bookmarks for navigation
- **lists**, bulleted and numbered, including nesting
- **bold and italic**, including italics faked with a skewed text matrix, which leave no
  trace in the font data

And it produces a file an old reader can actually open: base-14 fonts with no embedding
wherever the text allows, plain RGB images, no ICC profiles, no transparency, no
encryption, PDF 1.4, linearised.

## Options

| | |
|---|---|
| `--pages 26-50` | page ranges: `3`, `3-9`, `30-`, `1,4,7-9` |
| `--page-size` | `tablet` (default, 432x576pt = 6x8in, 3:4), `phone`, `ereader`, `a5`, `letter`, or `WxH` in points |
| `--font-size 12` | body point size (default 11) |
| `--margin 30` | page margin in points |
| `--columns 1` | override the detected column count |
| `--section-headings` | also treat bold lines like `3.2 Method` as headings — useful for specifications |
| `--max-figure 0.52` | cap figure height at this fraction of the text area |
| `--image-ppi 200` | figure resolution relative to printed size; lower means a smaller file |
| `--no-images`, `--no-tables` | drop figures / do not try to rebuild tables |
| `--dry-run` | report the detected layout and preview the text, write nothing |
| `--text FILE` | also write the reflowed text |

## How the layout is measured

Nothing is hard-coded to a particular document. From the file itself it works out:

- **running headers and footers** — text that repeats in the same place on most pages,
  sampled across the whole document, so page numbers and running titles are dropped
  rather than spliced into the body
- **columns** — from the histogram of line starts, each candidate confirmed against an
  x-coverage profile showing a near-empty gutter to its left and a sharp step up to its
  right. Looking for empty gutters alone fails, because full-width headings cross them.
  Measured **per page**, so a book that mixes one- and two-column pages works
- **heading levels** — sizes larger than the body that recur *through* the document;
  oversized type appearing only in the first pages is a cover, not a section heading
- **justification** — justified text hyphenates to reach the measure, so `psycho-` +
  `logists` must be closed up; ragged text does not, so `semi-` + `structured` must keep
  its hyphen. Decided from how sharply line endings pile up
- **the text measure** — where wrapped lines actually end, taken from the long lines only,
  because a few wide captions would otherwise overstate it

## Paragraph reconstruction

Lines are joined into paragraphs using blank-line spacing plus one geometric test: text
only wraps when the next word will not fit, so if the next line's first word *would* have
fit on the previous line, the break was deliberate. That keeps address blocks, contributor
lists and signature blocks as separate lines instead of running them into prose.

Also handled: URLs broken mid-path rejoin without a space; a bare `1.` is a list marker
only when indented text follows it, otherwise it is the tail of something like
`(Figure 25)`; section numbers set at a tab stop rejoin with their title.

## Limits

- **Scanned PDFs with no text layer** are refused with a pointer to `ocrmypdf`.
- **Heavily designed pages** — magazine spreads, pull quotes, sidebars flowing around
  figures — will not always come out in the intended order.
- **Equations and code blocks** are treated as running text and may lose their line
  structure.
- Front matter can skew detection on a whole-book run; `--pages` to skip it, and
  `--dry-run` to check before committing.

Run `--dry-run` first on an unfamiliar document. It prints what was detected and a preview
of the reflowed text, which is usually enough to tell whether the result will be right.

## Install

Self-contained: `~/.local/share/pdfsimplify/` holds the script and its virtualenv, and
`~/.local/bin/pdfsimplify` is a symlink to the script, whose shebang points at that
virtualenv. Nothing is installed system-wide.

To recreate it elsewhere, copy `pdfsimplify.py` and `requirements.txt` and run:

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python pdfsimplify.py --version
```

That is enough to use it. To get it back on your PATH as a command, repoint the shebang at
the new virtualenv and symlink it:

```sh
sed -i '' "1s|.*|#!$PWD/venv/bin/python3|" pdfsimplify.py   # GNU sed: drop the ''
chmod +x pdfsimplify.py && ln -sf "$PWD/pdfsimplify.py" ~/.local/bin/pdfsimplify
```

Needs **Python 3.10 or newer** — that floor comes from pymupdf and pillow, not from the
script. `requirements.txt` pins the exact versions this was built and tested against.

`qpdf` is an optional non-Python extra, used to linearise the output. Without it the tool
prints a note and carries on, and the PDF is still valid — only slightly slower to open.
