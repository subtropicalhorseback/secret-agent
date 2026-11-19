"""
pdf_watcher.py — Chip PDF Ingestion Pipeline
=====================================================
- Docling for layout-aware text extraction
- OCR disabled by default, auto-retry with OCR if text is empty
- nomic-embed-text via Ollama for embeddings
- FOLDER_MAP for automatic domain/sensitive tagging
- Hash-based dedup, resume capability
"""

import os, time, json, sqlite3, hashlib, requests
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
WATCH_DIR   = f"{WORKSPACE}/syncthing"
VAULT_DB    = f"{WORKSPACE}/data/memory/chip_vault.db"
OLLAMA_URL  = "http://localhost:11434"
CHUNK_WORDS = 300
MIN_CHARS   = 100

import chip_config
FOLDER_MAP = chip_config.FOLDER_MAP   # config/domains.py

_converter_no_ocr   = None
_converter_with_ocr = None

def get_converter(use_ocr=False):
    global _converter_no_ocr, _converter_with_ocr
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    if use_ocr:
        if _converter_with_ocr is None:
            opts = PdfPipelineOptions()
            opts.do_ocr = True
            opts.do_table_structure = True
            _converter_with_ocr = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
        return _converter_with_ocr
    else:
        if _converter_no_ocr is None:
            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            _converter_no_ocr = DocumentConverter(
                format_options={"pdf": PdfFormatOption(pipeline_options=opts)}
            )
        return _converter_no_ocr

def ensure_schema():
    conn = sqlite3.connect(VAULT_DB)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
    if "file_hash" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN file_hash TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_hash ON documents(file_hash)")
        conn.commit()
    if "sensitive" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN sensitive INTEGER DEFAULT 0")
        conn.commit()
    conn.close()

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()[:16]

def get_pdf_date(file_path):
    """Extract creation date from PDF metadata. Falls back to mtime, then today."""
    import re as _re
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            meta = pdf.metadata or {}
            for key in ["CreationDate", "ModDate"]:
                mval = meta.get(key, "")
                if mval:
                    m = _re.search(r'D:(\d{8})', str(mval))
                    if m:
                        return m.group(1)
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y%m%d")
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")

def make_slug(filename):
    """Convert filename to YYYYMMDD_slug.pdf format."""
    import re as _re
    from pathlib import Path as _Path
    name = _Path(filename).stem
    name = _re.sub(r'^\d{8}_', '', name)
    name = _re.sub(r'[\s_]+', '-', name.lower())
    name = _re.sub(r'[^a-z0-9\-]', '', name)
    name = _re.sub(r'-+', '-', name).strip('-')
    return name[:50]

def canonical_filename(file_path):
    """Return YYYYMMDD_slug.pdf canonical name for a PDF."""
    date_str = get_pdf_date(file_path)
    slug     = make_slug(os.path.basename(file_path))
    return f"{date_str}_{slug}.pdf"

def get_domain_and_sensitive(file_path):
    parts = os.path.relpath(file_path, WATCH_DIR).split(os.sep)
    if len(parts) > 1:
        folder = parts[0].lower()
        mapping = FOLDER_MAP.get(folder)
        if folder in FOLDER_MAP and mapping is None:
            return None, None
        if mapping:
            return mapping
    return "unclassified", 0

def chunk_text(text, chunk_size=CHUNK_WORDS):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield " ".join(words[i:i + chunk_size])

def get_embedding(text):
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text[:2000]},
            timeout=30,
        )
        emb = resp.json().get("embedding", [])
        return json.dumps(emb) if emb else None
    except Exception as e:
        print(f"⚠️ Embedding error: {e}")
        return None

def extract_text(file_path):
    try:
        result = get_converter(use_ocr=False).convert(file_path)
        text = result.document.export_to_markdown().strip()
        if len(text) >= MIN_CHARS:
            return text
        print(f"  ℹ️ Thin text ({len(text)} chars), retrying with OCR...")
    except Exception as e:
        print(f"  ⚠️ Docling failed: {e}")

    try:
        result = get_converter(use_ocr=True).convert(file_path)
        text = result.document.export_to_markdown().strip()
        if text:
            return text
    except Exception as e:
        print(f"  ⚠️ Docling OCR failed: {e}")

    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
        return text.strip()
    except Exception as e:
        print(f"  ⚠️ pdfplumber fallback failed: {e}")
    return ""

def process_pdf(file_path):
    # Rename to canonical YYYYMMDD_slug.pdf on ingest
    canonical = canonical_filename(file_path)
    if canonical != os.path.basename(file_path):
        new_path = os.path.join(os.path.dirname(file_path), canonical)
        if not os.path.exists(new_path):
            os.rename(file_path, new_path)
            file_path = new_path
    filename = os.path.basename(file_path)
    domain, sensitive = get_domain_and_sensitive(file_path)
    if domain is None:
        print(f"⏭️  Skipping {filename} (ignored folder)")
        return

    fhash = file_hash(file_path)
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn  = sqlite3.connect(VAULT_DB)
    cursor = conn.cursor()

    existing = cursor.execute(
        "SELECT id, chunk_count FROM documents WHERE file_hash=?", (fhash,)
    ).fetchone()

    if existing:
        doc_id, expected = existing
        actual = cursor.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id=?", (doc_id,)
        ).fetchone()[0]
        if actual >= expected > 0:
            print(f"⏭️  {filename} already ingested")
            conn.close()
            return
        print(f"🔄 {filename} resuming ({actual}/{expected})...")
        resume_from = actual
        text = extract_text(file_path)
    else:
        print(f"\n[{ts}] 📄 {filename} → {domain}{' 🔒' if sensitive else ''}")
        text = extract_text(file_path)
        cursor.execute(
            "INSERT OR IGNORE INTO documents "
            "(filename, domain, source_type, file_hash, chunk_count, sensitive, ingested_at) "
            "VALUES (?, ?, 'pdf', ?, 0, ?, ?)",
            (filename, domain, fhash, sensitive, ts)
        )
        doc_id = cursor.lastrowid
        if not doc_id:
            row = cursor.execute(
                "SELECT id FROM documents WHERE filename=?", (filename,)
            ).fetchone()
            doc_id = row[0] if row else None
        resume_from = 0
        conn.commit()

    if not doc_id:
        conn.close()
        return

    chunks = list(chunk_text(text)) if text.strip() else []
    embed_count = 0

    for i, chunk in enumerate(chunks):
        if i < resume_from:
            continue
        embedding = get_embedding(chunk)
        cursor.execute(
            "INSERT INTO chunks "
            "(document_id, domain, chunk_index, chunk_text, embedding, sensitive) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, domain, i, chunk, embedding, sensitive)
        )
        if embedding:
            embed_count += 1
        if (i + 1) % 5 == 0:
            conn.commit()

    cursor.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunks), doc_id))
    conn.commit()
    conn.close()
    print(f"✅ {filename}: {len(chunks)} chunks, {embed_count} embedded, domain={domain}")

class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            time.sleep(3)
            process_pdf(event.src_path)

if __name__ == "__main__":
    ensure_schema()
    print(f"👀 PDF Watchdog online — {WATCH_DIR}")
    print(f"   Extractor: Docling (no-OCR default, auto-retry with OCR if empty)")
    print(f"   Embeddings: nomic-embed-text via Ollama")
    get_converter(use_ocr=False)
    print("✅ Docling ready")
    print("\n🔍 Sweeping for existing PDFs...")
    for root, _, files in os.walk(WATCH_DIR):
        for file in sorted(files):
            if file.lower().endswith(".pdf"):
                process_pdf(os.path.join(root, file))
    print("\n👁️  Entering watch mode...")
    observer = Observer()
    observer.schedule(PDFHandler(), path=WATCH_DIR, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
