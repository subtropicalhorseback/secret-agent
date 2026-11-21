"""
skills/model_check.py — does every model name Chip uses exist, on its own client?
==================================================================================
Added 20 Nov 2026 (One Voice spec, build step 1).

The 19:27 browse report on 20 Nov reached the admin as a raw dump because
handle_machine_turn passed "gemini-3.5-flash" to the Anthropic client. The API
answered 404, the caller caught it, and the fallback hid the failure. The
FIX-0006 tests passed because the client was stubbed. This module is the check
that would have caught it: every model name Chip is configured with is looked
up on the client its name says it belongs to, using the free model-lookup
endpoints (no tokens are generated, nothing is billed).

    claude-*  → Anthropic  GET /v1/models/{id}
    gemini-*  → Gemini     GET /v1beta/models/{id}
    anything else → the local Ollama server's model list

Results are one of:
    ok           the model exists on its client
    missing      the client answered and does not know the name   ← a real defect
    auth         the client refused our key                        ← a real defect
    unreachable  network down / timeout / server error             ← not a defect;
                 the box fails closed when the VPN drops, so this is expected
                 at times and must never crash startup

CLI:  venv/bin/python3 skills/model_check.py      (exit 1 on missing/auth)
"""
import ast
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from chip_config import WORKSPACE  # noqa: E402
from skills import llm  # noqa: E402  (also loads .env for cron/CLI callers)

OLLAMA_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
TIMEOUT = 10


def client_for(name):
    n = str(name or "")
    if n.startswith("claude"):
        return "anthropic"
    if n.startswith("gemini"):
        return "gemini"
    return "ollama"


def _coordinator_models():
    """Read the MODELS dict literal out of coordinator.py without importing it
    (importing the coordinator starts a Signal bot)."""
    try:
        tree = ast.parse(open(f"{WORKSPACE}/coordinator.py").read())
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and getattr(node.targets[0], "id", "") == "MODELS"):
                return dict(ast.literal_eval(node.value))
    except Exception as e:
        print(f"⚠️ model_check: could not read coordinator MODELS: {e}")
    return {}


def _heal_model():
    try:
        src = open(f"{WORKSPACE}/skills/self_heal.py").read()
        m = re.search(r'HEAL_MODEL\s*=\s*os\.getenv\("CHIP_HEAL_MODEL",\s*"([^"]+)"\)', src)
        return os.getenv("CHIP_HEAL_MODEL") or (m.group(1) if m else None)
    except Exception:
        return None


def configured_names(extra=None):
    """Every model name in use, mapped to where it is configured."""
    where = {}

    def add(name, source):
        if name:
            where.setdefault(str(name), []).append(source)

    for k, v in _coordinator_models().items():
        add(v, f"coordinator.MODELS[{k}]")
    for const in ("OPUS", "FABLE", "SONNET", "GFLASH", "GLITE",
                  "REFUSAL_FALLBACK", "BROWSE_LOOP_MODEL"):
        add(getattr(llm, const, None), f"llm.{const}")
    add(_heal_model(), "self_heal.HEAL_MODEL")
    for k, v in (extra or {}).items():
        add(v, k)
    return where


def _check_anthropic(name):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    try:
        r = requests.get(f"https://api.anthropic.com/v1/models/{name}",
                         headers={"x-api-key": key,
                                  "anthropic-version": "2023-06-01"},
                         timeout=TIMEOUT)
    except Exception as e:
        return "unreachable", type(e).__name__
    if r.status_code == 200:
        return "ok", ""
    if r.status_code == 404:
        return "missing", "404 not_found"
    if r.status_code in (401, 403):
        return "auth", str(r.status_code)
    return "unreachable", f"HTTP {r.status_code}"


def _check_gemini(name):
    key = os.getenv("GEMINI_API_KEY", "")
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models/{name}",
                         params={"key": key}, timeout=TIMEOUT)
    except Exception as e:
        return "unreachable", type(e).__name__
    if r.status_code == 200:
        return "ok", ""
    if r.status_code == 404:
        return "missing", "404 not found"
    if r.status_code in (400, 401, 403):
        # Gemini answers a bad key with 400 API_KEY_INVALID
        body = r.text[:200]
        if "API_KEY" in body or r.status_code != 400:
            return "auth", f"HTTP {r.status_code}"
        return "missing", f"HTTP 400"
    return "unreachable", f"HTTP {r.status_code}"


def _ollama_names():
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=TIMEOUT)
    r.raise_for_status()
    names = set()
    for m in r.json().get("models", []):
        n = m.get("name", "")
        names.add(n)
        names.add(n.split(":")[0])          # "phi4-mini:latest" → "phi4-mini"
    return names


def check_all(extra=None):
    """Returns a list of dicts: name, client, status, detail, sources."""
    results = []
    ollama = None
    for name, sources in sorted(configured_names(extra).items()):
        client = client_for(name)
        if client == "anthropic":
            status, detail = _check_anthropic(name)
        elif client == "gemini":
            status, detail = _check_gemini(name)
        else:
            if ollama is None:
                try:
                    ollama = _ollama_names()
                except Exception as e:
                    ollama = e
            if isinstance(ollama, Exception):
                status, detail = "unreachable", type(ollama).__name__
            else:
                status, detail = ("ok", "") if name in ollama else ("missing", "not pulled")
        results.append({"name": name, "client": client, "status": status,
                        "detail": detail, "sources": sources})
    return results


def problems(results):
    """Only the real defects: a name its client does not know, or a refused key."""
    return [r for r in results if r["status"] in ("missing", "auth")]


def summary_line(results):
    bad = problems(results)
    unreach = [r for r in results if r["status"] == "unreachable"]
    if not bad and not unreach:
        return f"model check: all {len(results)} model names resolve on their own clients"
    parts = []
    if bad:
        parts.append("; ".join(f"{r['name']} {r['status']} on {r['client']} "
                               f"(used by {', '.join(r['sources'])})" for r in bad))
    if unreach:
        parts.append(f"{len(unreach)} unreachable (not checked): "
                     + ", ".join(r["name"] for r in unreach))
    return "model check: " + " | ".join(parts)


if __name__ == "__main__":
    res = check_all()
    for r in res:
        mark = {"ok": "✓", "missing": "✗", "auth": "✗", "unreachable": "?"}[r["status"]]
        print(f"{mark} {r['name']:24s} {r['client']:9s} {r['status']:11s} "
              f"{r['detail']:16s} {', '.join(r['sources'])}")
    print(summary_line(res))
    sys.exit(1 if problems(res) else 0)
