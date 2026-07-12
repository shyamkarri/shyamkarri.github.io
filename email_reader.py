"""
Read-only email sources for the router. Two backends, chosen by env:

  Gmail API (OAuth, read-only scope) — preferred, matches the spec.
    Set GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET / GMAIL_OAUTH_REFRESH_TOKEN.
    Scope is gmail.readonly ONLY — the token cannot send or modify mail.

  IMAP (app password, read-only SELECT) — zero-setup fallback that reuses the
    GMAIL_USER / GMAIL_APP_PASSWORD already configured for gmail_responder.py.
    The mailbox is opened readonly=True and we only FETCH — never STORE/APPEND/DELETE.

get_reader() returns whichever is configured (Gmail API first), or None.
Both expose fetch_recent(since_minutes, limit) -> list of normalized dicts:
  {thread_id, from_email, from_name, subject, snippet, body, date}
"""

import os
import re
import email
import logging
import imaplib
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime, parseaddr

logger = logging.getLogger("agent_logger")

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def _split_from(raw_from: str):
    name, addr = parseaddr(raw_from or "")
    return addr, (name or addr.split("@")[0] if addr else "")


def _clean_body(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"https?://\S+", " ", text)          # strip tracking URLs
    return re.sub(r"\s+", " ", text).strip()


class GmailAPIReader:
    """Gmail API with a read-only OAuth token."""

    def __init__(self, client_id, client_secret, refresh_token):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(
            None, refresh_token=refresh_token,
            client_id=client_id, client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[GMAIL_READONLY_SCOPE],
        )
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch_recent(self, since_minutes=30, limit=40):
        after = int((datetime.utcnow() - timedelta(minutes=since_minutes)).timestamp())
        resp = self.service.users().messages().list(
            userId="me", q=f"in:inbox after:{after}", maxResults=limit).execute()
        out = []
        for ref in resp.get("messages", []):
            m = self.service.users().messages().get(
                userId="me", id=ref["id"], format="full").execute()
            out.append(self._normalize(m))
        return out

    def _normalize(self, m):
        headers = {h["name"].lower(): h["value"]
                   for h in m.get("payload", {}).get("headers", [])}
        addr, name = _split_from(headers.get("from", ""))
        date = None
        if headers.get("date"):
            try:
                date = parsedate_to_datetime(headers["date"]).replace(tzinfo=None)
            except Exception:
                pass
        return {
            "thread_id": m.get("threadId"),
            "from_email": addr, "from_name": name,
            "subject": headers.get("subject", ""),
            "snippet": m.get("snippet", ""),
            "body": _clean_body(self._body(m.get("payload", {})))[:6000],
            "date": date or datetime.utcnow(),
        }

    def _body(self, payload):
        import base64
        if payload.get("body", {}).get("data"):
            try:
                return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
            except Exception:
                return ""
        for part in payload.get("parts", []) or []:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                try:
                    return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "ignore")
                except Exception:
                    continue
        for part in payload.get("parts", []) or []:   # recurse into multipart
            got = self._body(part)
            if got:
                return got
        return ""


class ImapReader:
    """Read-only IMAP fallback (Gmail). Opens the mailbox readonly and only FETCHes."""

    def __init__(self, user, app_password, host="imap.gmail.com"):
        self.user, self.password, self.host = user, app_password, host

    def fetch_recent(self, since_minutes=30, limit=40):
        out = []
        conn = imaplib.IMAP4_SSL(self.host)
        try:
            conn.login(self.user, self.password)
            conn.select("INBOX", readonly=True)   # read-only — no flag changes
            since = (datetime.utcnow() - timedelta(minutes=since_minutes)).strftime("%d-%b-%Y")
            typ, data = conn.search(None, f'(SINCE {since})')
            if typ != "OK":
                return out
            ids = data[0].split()[-limit:]
            for mid in reversed(ids):
                typ, msg_data = conn.fetch(mid, "(RFC822)")
                if typ != "OK":
                    continue
                out.append(self._normalize(email.message_from_bytes(msg_data[0][1])))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _normalize(self, msg):
        addr, name = _split_from(msg.get("From", ""))
        date = None
        if msg.get("Date"):
            try:
                date = parsedate_to_datetime(msg["Date"]).replace(tzinfo=None)
            except Exception:
                pass
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        body = part.get_payload(decode=True).decode("utf-8", "ignore")
                        break
                    except Exception:
                        continue
        else:
            try:
                body = msg.get_payload(decode=True).decode("utf-8", "ignore")
            except Exception:
                body = ""
        body = _clean_body(body)
        # IMAP has no stable thread id — group by normalized subject + sender
        subject = msg.get("Subject", "") or ""
        base_subj = re.sub(r"^(re|fwd?):\s*", "", subject.strip(), flags=re.I).lower()
        thread_id = msg.get("Message-ID") or f"imap:{addr}:{base_subj}"[:250]
        # prefer a stable per-conversation id so replies collapse together
        refs = msg.get("References") or msg.get("In-Reply-To")
        if refs:
            thread_id = f"imap:{refs.split()[0].strip()}"[:250]
        else:
            thread_id = f"imap:{addr}:{base_subj}"[:250]
        return {
            "thread_id": thread_id,
            "from_email": addr, "from_name": name,
            "subject": subject, "snippet": body[:200],
            "body": body[:6000], "date": date or datetime.utcnow(),
        }


def get_reader():
    cid = os.getenv("GMAIL_OAUTH_CLIENT_ID")
    secret = os.getenv("GMAIL_OAUTH_CLIENT_SECRET")
    refresh = os.getenv("GMAIL_OAUTH_REFRESH_TOKEN")
    if cid and secret and refresh:
        try:
            return GmailAPIReader(cid, secret, refresh)
        except Exception as e:
            logger.warning(f"[EmailReader] Gmail API init failed, trying IMAP: {e}")
    user = os.getenv("GMAIL_USER")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if user and pw:
        return ImapReader(user, pw)
    return None


def source_status() -> dict:
    if os.getenv("GMAIL_OAUTH_REFRESH_TOKEN"):
        return {"connected": True, "backend": "gmail_api", "mode": "read-only (OAuth)"}
    if os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD"):
        return {"connected": True, "backend": "imap", "mode": "read-only (app password)"}
    return {"connected": False, "backend": None, "mode": None}
