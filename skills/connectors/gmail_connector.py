"""
skills/connectors/gmail_connector.py — Gmail as a Chip capability
==================================================================
Scaffolded 19 Nov 2026; dormant until the OAuth dance in connectors/README.md
is done (needs the admin's browser — cannot be completed from the Pi alone).

Gmail is a capability, not a chat transport: once authorized, these functions
back two tools (`gmail_unread`, `gmail_send`) to be registered in
tool_calling.py. gmail_send ALWAYS drafts and asks for confirmation over the
originating channel before anything leaves the house.

One-time auth:  venv/bin/python3 skills/connectors/gmail_connector.py --auth
"""
import base64
import os
import sys
from email.mime.text import MIMEText

import os as _os, sys as _sys
_cc = _os.path.dirname(_os.path.abspath(__file__))
while _cc != "/" and not _os.path.exists(_os.path.join(_cc, "chip_config.py")):
    _cc = _os.path.dirname(_cc)
if _cc not in _sys.path:
    _sys.path.insert(0, _cc)
from chip_config import WORKSPACE
CREDS_FILE = f"{WORKSPACE}/config/gmail_credentials.json"
TOKEN_FILE = f"{WORKSPACE}/config/gmail_token.json"
SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly",
              "https://www.googleapis.com/auth/gmail.send"]


def available():
    if not (os.path.exists(CREDS_FILE) and os.path.exists(TOKEN_FILE)):
        return False
    try:
        import googleapiclient  # noqa: F401
        return True
    except ImportError:
        return False


def _service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        open(TOKEN_FILE, "w").write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def unread_summary(max_results=10):
    """Return sender/subject/snippet for recent unread inbox mail."""
    svc  = _service()
    resp = svc.users().messages().list(
        userId="me", q="is:unread in:inbox", maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        hdr = {h["name"]: h["value"]
               for h in msg.get("payload", {}).get("headers", [])}
        out.append(f"From: {hdr.get('From','?')}\nSubject: {hdr.get('Subject','?')}\n"
                   f"Date: {hdr.get('Date','?')}\n{msg.get('snippet','')}")
    return "\n---\n".join(out) if out else "No unread mail."


def send_mail(to, subject, body):
    """Send a plain-text email. Callers must confirm with the user first."""
    svc = _service()
    mime = MIMEText(body)
    mime["to"], mime["subject"] = to, subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Sent to {to}: {subject}"


if __name__ == "__main__" and "--auth" in sys.argv:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow  = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
    creds = flow.run_console()
    open(TOKEN_FILE, "w").write(creds.to_json())
    print(f"✓ token saved to {TOKEN_FILE}")
