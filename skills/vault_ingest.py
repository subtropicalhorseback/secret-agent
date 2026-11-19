"""
vault_ingest.py — Vault Ingest Utility
=======================================
Chunks, embeds, and inserts text into chip_vault.db.
Embeds through the local Ollama nomic-embed-text model.
Hash-based dedup — safe to call multiple times.
"""

import os
import re
import json
import sqlite3
import hashlib
import requests

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
VAULT_DB     = f"{WORKSPACE}/data/memory/chip_vault.db"
EMBED_URL    = "http://127.0.0.1:11434"  # Ollama
CHUNK_WORDS  = 250


def strip_markdown(text):
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_{1,3}(.*?)_{1,3}', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def chunk_text(text, chunk_size=CHUNK_WORDS):
    words = text.split()
    return [" ".join(words[i:i+chunk_size])
            for i in range(0, len(words), chunk_size)]


def get_embedding(text):
    """Embed text via Ollama nomic-embed-text."""
    try:
        resp = requests.post(
            f"{EMBED_URL}/api/embed",
            json={"model": "nomic-embed-text", "input": text[:4096]},
            timeout=60,
        )
        data = resp.json()
        # Ollama returns {"embeddings": [[...]], ...} — take first vector
        embeddings = data.get("embeddings", [])
        emb = embeddings[0] if embeddings else []
        return json.dumps(emb) if emb else None
    except Exception as e:
        print(f"Embedding error: {e}")
        return None


def ingest_text(content, filename, domain="analyze", source_type="analyze"):
    text = strip_markdown(content)
    if not text:
        return 0

    fhash = hashlib.sha256(text.encode()).hexdigest()[:16]
    conn  = sqlite3.connect(VAULT_DB)

    try:
        if conn.execute("SELECT id FROM documents WHERE file_hash=?", (fhash,)).fetchone():
            print(f"Vault: {filename} already ingested.")
            return 0

        conn.execute(
            "INSERT INTO documents (filename, domain, source_type, file_hash, chunk_count) "
            "VALUES (?, ?, ?, ?, 0)",
            (filename, domain, source_type, fhash)
        )
        doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        chunks      = chunk_text(text)
        embed_count = 0

        for i, chunk in enumerate(chunks):
            embedding = get_embedding(chunk)
            conn.execute(
                "INSERT INTO chunks (document_id, domain, chunk_index, chunk_text, embedding) "
                "VALUES (?, ?, ?, ?, ?)",
                (doc_id, domain, i, chunk, embedding)
            )
            if embedding:
                embed_count += 1
            if (i + 1) % 5 == 0:
                conn.commit()

        conn.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunks), doc_id))
        conn.commit()
        print(f"Vault: {filename} — {len(chunks)} chunks, {embed_count} embedded, domain={domain}")
        return len(chunks)

    except Exception as e:
        print(f"Vault ingest error: {e}")
        return 0
    finally:
        conn.close()


def ingest_analyze_report(full_name, run_ts, report_content):
    safe = re.sub(r'[^\w]', '_', full_name)[:40]
    return ingest_text(report_content, f"analyze_{safe}_{run_ts}.md",
                       domain="analyze", source_type="analyze")


def ingest_research_report(query, run_ts, report_content):
    safe = re.sub(r'[^\w]', '_', query)[:40]
    return ingest_text(report_content, f"research_{safe}_{run_ts}.txt",
                       domain="research", source_type="research")
