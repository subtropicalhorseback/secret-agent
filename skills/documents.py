"""
skills/documents.py — read, produce and track PDF/DOCX documents
=================================================================
Added 19 Nov 2026 (the admin: "read, edit, produce pdf and docx files, send them to
me as attachments, recorded locally then indexed").

Produced files land in data/documents/, go out over Signal as attachments, and
are ingested into the vault (domain-tagged) so they're searchable later. The
"edit" loop is read_document → model rewrites → produce_* writes a new
timestamped version — originals are never overwritten.

Writers are deliberately plain: fpdf2 and python-docx, both pure-python and
already on the Pi. Content is treated as paragraphs of prose with light
heading support ("# Heading" lines), matching Chip's plain-text house style.
"""
import os
import re
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
DOC_DIR   = f"{WORKSPACE}/data/documents"

# Where read_document is allowed to look, in search order.
READ_DIRS = [
    DOC_DIR,
    f"{WORKSPACE}/data/archive/reports",
    f"{WORKSPACE}/syncthing",
]


def _slug(title):
    s = re.sub(r"[^\w\s-]", "", (title or "document")).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:60] or "document"


def _outpath(title, ext):
    os.makedirs(DOC_DIR, exist_ok=True)
    return f"{DOC_DIR}/{datetime.now():%Y%m%d_%H%M%S}_{_slug(title)}.{ext}"


def _paragraphs(content):
    """Split model-produced content into (kind, text) blocks.
    kind: 'h1' for '# ' lines, 'h2' for '## ', else 'p'."""
    blocks = []
    for raw in (content or "").split("\n\n"):
        text = raw.strip()
        if not text:
            continue
        if text.startswith("## "):
            blocks.append(("h2", text[3:].strip()))
        elif text.startswith("# "):
            blocks.append(("h1", text[2:].strip()))
        else:
            blocks.append(("p", text))
    return blocks


PDF_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  :root {{ --ink: #1a2420; --accent: #2e5d43; --muted: #6b7a72; --rule: #d8e0da; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Liberation Sans', 'DejaVu Sans', sans-serif;
         color: var(--ink); font-size: 10.5pt; line-height: 1.55; margin: 0; }}
  .titleblock {{ border-bottom: 3px solid var(--accent); padding-bottom: 10px;
                 margin-bottom: 22px; }}
  .title {{ font-size: 21pt; font-weight: 700; letter-spacing: -0.02em;
            margin: 0 0 4px 0; color: var(--ink); }}
  .dateline {{ font-size: 9pt; color: var(--muted);
               text-transform: uppercase; letter-spacing: 0.12em; }}
  h1 {{ font-size: 13pt; font-weight: 700; color: var(--accent);
        margin: 20px 0 6px 0; padding-bottom: 3px;
        border-bottom: 1px solid var(--rule); page-break-after: avoid; }}
  h2 {{ font-size: 11pt; font-weight: 700; margin: 14px 0 4px 0;
        page-break-after: avoid; }}
  p {{ margin: 0 0 9px 0; text-align: justify; }}
  ul, ol {{ margin: 0 0 9px 0; padding-left: 20px; }}
  li {{ margin-bottom: 3px; }}
  strong {{ color: var(--ink); }}
  code {{ font-family: 'Liberation Mono', monospace; font-size: 9pt;
          background: #f0f4f1; padding: 1px 4px; border-radius: 3px; }}
  blockquote {{ border-left: 3px solid var(--rule); margin: 0 0 9px 0;
                padding: 2px 0 2px 12px; color: var(--muted); }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 10px 0;
           font-size: 9.5pt; }}
  th {{ text-align: left; border-bottom: 2px solid var(--accent);
        padding: 4px 8px 4px 0; }}
  td {{ border-bottom: 1px solid var(--rule); padding: 4px 8px 4px 0; }}
</style></head><body>
  <div class="titleblock">
    <div class="title">{title}</div>
    <div class="dateline">{date} · Chip</div>
  </div>
  {body}
</body></html>"""


def _make_pdf_chromium(title, content):
    """Primary renderer (19 Nov 2026): markdown → styled HTML → Chromium
    print-to-PDF via Playwright. Real typography, full unicode, tables and
    lists — everything the latin-1 core-font fpdf path butchered. Uses the
    same Pi-patched system Chromium as the browser service, but its own
    short-lived HEADLESS instance (page.pdf() requires headless)."""
    import markdown as _md
    from playwright.sync_api import sync_playwright
    body = _md.markdown(content, extensions=["tables", "sane_lists"])
    html = PDF_TEMPLATE.format(
        title=title.replace("<", "&lt;"),
        date=datetime.now().strftime("%B %d, %Y"),
        body=body)
    path = _outpath(title, "pdf")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        pg = b.new_page()
        pg.set_content(html, wait_until="load")
        pg.pdf(path=path, format="Letter", print_background=True,
               margin={"top": "1.8cm", "bottom": "1.8cm",
                       "left": "2cm", "right": "2cm"},
               display_header_footer=True,
               header_template="<span></span>",
               footer_template=("<div style='font-size:8px; color:#6b7a72; "
                                "width:100%; text-align:center;'>"
                                "<span class='pageNumber'></span> / "
                                "<span class='totalPages'></span></div>"))
        b.close()
    return path


def make_pdf(title, content):
    """Render title+content to a PDF. Chromium typesetting first; the old
    fpdf2 path survives only as a fallback. Returns the file path."""
    try:
        return _make_pdf_chromium(title, content)
    except Exception as e:
        print(f"⚠️ chromium pdf failed ({e}) — fpdf fallback")
        return _make_pdf_basic(title, content)


def _make_pdf_basic(title, content):
    """Fallback renderer (fpdf2, latin-1 core fonts)."""
    from fpdf import FPDF

    def _safe(t):
        """Core PDF fonts are latin-1; swap the common typography first so an
        em-dash degrades to ' - ' instead of '?', then hard-replace the rest."""
        for a, b in (("—", " - "), ("–", "-"), ("‘", "'"),
                     ("’", "'"), ("“", '"'), ("”", '"'),
                     ("•", "*"), ("…", "...")):
            t = t.replace(a, b)
        return t.encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)
    epw = pdf.w - pdf.l_margin - pdf.r_margin

    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(epw, 8, _safe(title))
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(epw, 5, _safe(datetime.now().strftime("%B %d, %Y — Chip")))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    for kind, text in _paragraphs(content):
        if kind == "h1":
            pdf.set_font("Helvetica", "B", 13); pdf.ln(2)
        elif kind == "h2":
            pdf.set_font("Helvetica", "B", 11); pdf.ln(1)
        else:
            pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(epw, 6, _safe(text))
        pdf.ln(1.5)
    path = _outpath(title, "pdf")
    pdf.output(path)
    return path


def make_docx(title, content):
    """Render title+content to a DOCX. Returns the file path."""
    import docx
    doc = docx.Document()
    doc.add_heading(title, level=0)
    p = doc.add_paragraph(datetime.now().strftime("%B %d, %Y — Chip"))
    p.runs[0].italic = True
    for kind, text in _paragraphs(content):
        if kind == "h1":
            doc.add_heading(text, level=1)
        elif kind == "h2":
            doc.add_heading(text, level=2)
        else:
            doc.add_paragraph(text)
    path = _outpath(title, "docx")
    doc.save(path)
    return path


def read_any(path, max_chars=12000):
    """Extract text from pdf/docx/txt/md. Returns (text, note)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        import pdfplumber
        out, pages = [], 0
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages[:40]:
                out.append(page.extract_text() or "")
                pages += 1
        text = "\n".join(out)
        note = f"{pages} page(s) read"
    elif ext == ".docx":
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:                      # tables matter in docx
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(p for p in parts if p.strip())
        note = f"{len(d.paragraphs)} paragraph(s)"
    elif ext in (".txt", ".md", ".csv"):
        text = open(path, encoding="utf-8", errors="replace").read()
        note = "plain text"
    else:
        return "", f"unsupported type {ext}"
    if len(text) > max_chars:
        return text[:max_chars], note + f" (truncated to {max_chars} chars)"
    return text, note


def find_readable(filename):
    """Locate `filename` under the allowed read dirs (recursive, realpath-
    contained — same traversal rules as the H1 fix). Returns path or None."""
    want = os.path.basename((filename or "").strip())
    if not want:
        return None
    exact = _find_exact(want)
    if exact:
        return exact
    # Tolerant fallback: files get date-prefixed and re-punctuated when they
    # are filed, so "1795131305686_0182.pdf" may now be
    # "20261031_1795131305686-0182.pdf". Compare on letters and digits only,
    # ignoring a leading date, and accept a unique containment match.
    import re as _re

    def _key(n):
        stem = os.path.splitext(n)[0].lower()
        stem = _re.sub(r"^\d{8}[_-]", "", stem)
        return _re.sub(r"[^a-z0-9]", "", stem)
    wk = _key(want)
    if len(wk) < 4:
        return None
    hits = []
    for root_dir in READ_DIRS:
        root = os.path.realpath(root_dir)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                k = _key(f)
                if k and (k == wk or wk in k or k in wk and len(k) >= 8):
                    cand = os.path.realpath(os.path.join(dirpath, f))
                    if cand.startswith(root + os.sep):
                        hits.append(cand)
    if len(hits) == 1:
        return hits[0]
    same_ext = [h for h in hits if h.lower().endswith(os.path.splitext(want)[1].lower())]
    return max(same_ext, key=os.path.getmtime) if same_ext else None


def _find_exact(want):
    for root_dir in READ_DIRS:
        root = os.path.realpath(root_dir)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f == want or f.lower() == want.lower():
                    cand = os.path.realpath(os.path.join(dirpath, f))
                    if cand == root or cand.startswith(root + os.sep):
                        return cand
    return None


def ingest_produced(path, title, content, domain="general"):
    """Index a produced document into the vault so it's searchable later."""
    try:
        from skills.vault_ingest import ingest_text
        ingest_text(f"[Produced document: {title}]\n{content}",
                    os.path.basename(path), domain=domain,
                    source_type="produced_doc")
        return True
    except Exception as e:
        print(f"⚠️ Produced-doc ingest failed: {e}")
        return False
