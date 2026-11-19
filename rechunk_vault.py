#!/usr/bin/env python3
"""
rechunk_vault.py — Chip Rechunk
========================================
Docling + nomic-embed-text pipeline.
- OCR disabled by default (fast, sufficient for native PDFs)
- Auto-retry with OCR if text extraction returns empty
- Domain overrides: personal/private/vet always use OCR on retry
- Page-level progress logging
- Small docs first (sorted by file size ascending)

Usage:
  screen -dmS rechunk bash -c 'cd $CHIP_WORKSPACE && source venv/bin/activate && python3 rechunk_vault.py > data/logs/rechunk.log 2>&1'
  tail -f data/logs/rechunk.log
"""

import os, json, time, sqlite3, requests
from datetime import datetime

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
VAULT_DB    = f"{WORKSPACE}/data/memory/chip_vault.db"
WATCH_DIR   = f"{WORKSPACE}/syncthing"
OLLAMA_URL  = "http://localhost:11434"
CHUNK_WORDS = 300
MIN_CHARS   = 100  # below this = treat as empty, retry with OCR

# Domains where OCR retry matters most
import chip_config
OCR_PRIORITY_DOMAINS = chip_config.OCR_PRIORITY_DOMAINS

# ── Docling converters (lazy init) ────────────────────────────────────────────

_converter_no_ocr  = None
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

# ── Helpers ────────────────────────────────────────────────────────────────────

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
        print(f"  ⚠️ embed error: {e}")
        return None

def chunk_text(text, chunk_size=CHUNK_WORDS):
    words = text.split()
    for i in range(0, len(words), chunk_size):
        yield " ".join(words[i:i + chunk_size])

def extract_text(file_path, domain):
    """
    Extract text from PDF.
    1. Try without OCR (fast).
    2. If result is empty/thin, retry with OCR.
    Returns (text, used_ocr).
    """
    # Pass 1: no OCR
    try:
        result = get_converter(use_ocr=False).convert(file_path)
        text = result.document.export_to_markdown().strip()
        if len(text) >= MIN_CHARS:
            return text, False
        print(f"  ℹ️  No-OCR pass returned thin text ({len(text)} chars), retrying with OCR...")
    except Exception as e:
        print(f"  ⚠️ Docling (no-OCR) failed: {e}")
        text = ""

    # Pass 2: with OCR
    try:
        result = get_converter(use_ocr=True).convert(file_path)
        ocr_text = result.document.export_to_markdown().strip()
        if len(ocr_text) >= MIN_CHARS:
            return ocr_text, True
        print(f"  ⚠️ OCR pass also thin ({len(ocr_text)} chars)")
        return ocr_text, True
    except Exception as e:
        print(f"  ⚠️ Docling (OCR) failed: {e}")

    # Fallback: pdfplumber
    try:
        import pdfplumber
        fb = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    fb += t + "\n"
        return fb.strip(), False
    except Exception as e:
        print(f"  ⚠️ pdfplumber fallback failed: {e}")
    return "", False

# ── PDF index ─────────────────────────────────────────────────────────────────

_pdf_cache = {}

def build_pdf_index():
    global _pdf_cache
    print("Indexing PDF files on disk...")
    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if f.lower().endswith(".pdf"):
                _pdf_cache[f] = os.path.join(root, f)
    _pdf_cache = dict(sorted(
        _pdf_cache.items(),
        key=lambda x: os.path.getsize(x[1])
    ))
    print(f"  Found {len(_pdf_cache)} PDF files (sorted small→large)")

def find_pdf(filename):
    return _pdf_cache.get(filename)

# ── Rechunk handlers ──────────────────────────────────────────────────────────

def rechunk_pdf(conn, doc_id, filename, domain, sensitive):
    file_path = find_pdf(filename)
    if not file_path:
        print(f"  ⚠️ Not found on disk")
        return 0

    t0 = time.time()
    text, used_ocr = extract_text(file_path, domain)
    elapsed = time.time() - t0

    if not text.strip():
        print(f"  ⚠️ No text extracted after {elapsed:.1f}s")
        return 0

    print(f"  ✅ Extracted {len(text)} chars in {elapsed:.1f}s {'(OCR)' if used_ocr else '(layout)'}")

    conn.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    conn.commit()

    chunks      = list(chunk_text(text))
    embed_count = 0

    for i, chunk in enumerate(chunks):
        embedding = get_embedding(chunk)
        conn.execute(
            "INSERT INTO chunks "
            "(document_id, domain, chunk_index, chunk_text, embedding, sensitive) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, domain, i, chunk, embedding, sensitive)
        )
        if embedding:
            embed_count += 1
        if (i + 1) % 5 == 0:
            conn.commit()
        if (i + 1) % 10 == 0:
            print(f"    [{i+1}/{len(chunks)} chunks]")

    conn.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunks), doc_id))
    conn.commit()
    print(f"  ✅ {len(chunks)} chunks, {embed_count} embedded")
    return embed_count

def rechunk_text(conn, doc_id):
    chunks = conn.execute(
        "SELECT id, chunk_text FROM chunks WHERE document_id=? ORDER BY chunk_index",
        (doc_id,)
    ).fetchall()
    if not chunks:
        return 0
    embed_count = 0
    for chunk_id, chunk_body in chunks:
        if not chunk_body:
            continue
        embedding = get_embedding(chunk_body)
        conn.execute("UPDATE chunks SET embedding=? WHERE id=?", (embedding, chunk_id))
        if embedding:
            embed_count += 1
    conn.commit()
    return embed_count

# ── Health check ──────────────────────────────────────────────────────────────

def check_nomic():
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "test"},
            timeout=10,
        )
        emb = resp.json().get("embedding", [])
        if emb:
            print(f"✅ nomic-embed-text ready (dim={len(emb)})")
            return True
    except Exception:
        pass
    print("❌ nomic-embed-text not responding. Run: ollama pull nomic-embed-text")
    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Chip Rechunk — Docling + nomic-embed-text")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    if not check_nomic():
        return

    build_pdf_index()

    print("Initializing Docling (no-OCR converter)...")
    get_converter(use_ocr=False)
    print("✅ Docling ready")

    conn = sqlite3.connect(VAULT_DB)
    docs = conn.execute(
        "SELECT id, filename, domain, source_type, sensitive FROM documents ORDER BY id"
    ).fetchall()
    conn.close()

    # Sort PDFs by file size (small first), keep text docs at end
    pdf_docs  = [(i,f,d,s,sens) for i,f,d,s,sens in docs if s == "pdf"]
    text_docs = [(i,f,d,s,sens) for i,f,d,s,sens in docs if s != "pdf"]
    pdf_docs.sort(key=lambda x: os.path.getsize(find_pdf(x[1])) if find_pdf(x[1]) else 0)
    docs = pdf_docs + text_docs

    print(f"\nFound {len(docs)} documents ({len(pdf_docs)} PDFs, {len(text_docs)} text)")
    print("=" * 60)

    pdf_ok = pdf_skip = txt_ok = txt_skip = 0
    t_start = time.time()

    for idx, (doc_id, filename, domain, source_type, sensitive) in enumerate(docs, 1):
        elapsed_total = (time.time() - t_start) / 60
        print(f"\n[{idx}/{len(docs)}] {filename}")
        print(f"  domain={domain} sensitive={sensitive} type={source_type} elapsed={elapsed_total:.1f}min")

        conn = sqlite3.connect(VAULT_DB)
        try:
            if source_type == "pdf":
                n = rechunk_pdf(conn, doc_id, filename, domain, sensitive or 0)
                if n > 0:
                    pdf_ok += 1
                else:
                    pdf_skip += 1
            else:
                n = rechunk_text(conn, doc_id)
                if n > 0:
                    txt_ok += 1
                    print(f"  ✅ {n} chunks re-embedded")
                else:
                    txt_skip += 1
                    print(f"  ⚠️ No chunks")
        except Exception as e:
            print(f"  ❌ Error: {e}")
        finally:
            conn.close()

    elapsed = time.time() - t_start
    print("\n" + "=" * 60)
    print(f"✅ Rechunk complete — {elapsed/60:.1f} min total")
    print(f"   PDFs:  {pdf_ok} done, {pdf_skip} skipped")
    print(f"   Text:  {txt_ok} done, {txt_skip} skipped")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    main()
