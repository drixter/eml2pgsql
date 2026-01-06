#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import psycopg2
from psycopg2.extras import execute_values

from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from email.utils import parsedate_to_datetime, getaddresses

# ---------------- Helpers ----------------

def parse_addresses(header_val: Optional[str]) -> str:
    if not header_val:
        return ""
    pairs = getaddresses([header_val])
    return ", ".join([f"{n} <{a}>" if n and a else a or n for n, a in pairs])

def extract_bodies(msg: EmailMessage) -> Tuple[Optional[str], Optional[str]]:
    text_candidates, html_candidates = [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try: text_candidates.append(part.get_content())
                except: pass
            elif ctype == "text/html":
                try: html_candidates.append(part.get_content())
                except: pass
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            try: text_candidates.append(msg.get_content())
            except: pass
        elif ctype == "text/html":
            try: html_candidates.append(msg.get_content())
            except: pass
    return (max(text_candidates, key=len, default=None),
            max(html_candidates, key=len, default=None))

def extract_attachments(msg: EmailMessage) -> List[Dict[str, Any]]:
    results = []
    for part in msg.walk():
        disp = part.get_content_disposition()
        if disp not in ("attachment", "inline"):
            continue
        try:
            content = part.get_content()
            data = content if isinstance(content, bytes) else content.encode("utf-8")
        except:
            data = part.get_payload(decode=True)
        if not data: continue
        results.append({
            "filename": part.get_filename() or "attachment.bin",
            "content_type": part.get_content_type(),
            "is_inline": (disp == "inline"),
            "content_id": part.get("Content-ID"),
            "data": data,
            "size_bytes": len(data),
            "sha256_hex": hashlib.sha256(data).hexdigest()
        })
    return results

def extract_headers_only(msg: EmailMessage) -> str:
    return "\n".join([f"{k}: {v}" for k, v in msg.items()])

def parse_eml_file(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    with path.open("rb") as f:
        msg: EmailMessage = BytesParser(policy=policy.default).parse(f)
    message_id = (msg.get("Message-ID") or "").strip() or None
    subject = msg.get("Subject")
    from_addr = parse_addresses(msg.get("From"))
    to_addrs = parse_addresses(msg.get("To"))
    cc_addrs = parse_addresses(msg.get("Cc"))
    bcc_addrs = parse_addresses(msg.get("Bcc"))
    reply_to = parse_addresses(msg.get("Reply-To"))
    in_reply_to = msg.get("In-Reply-To")
    references_hdr = msg.get("References")
    sent_at_utc = None
    if msg.get("Date"):
        try:
            dt = parsedate_to_datetime(msg.get("Date"))
            if dt: sent_at_utc = dt.astimezone() if dt.tzinfo else dt
        except: pass
    text_body, html_body = extract_bodies(msg)
    raw_headers = extract_headers_only(msg)
    email_row = {
        "message_id": message_id, "subject": subject,
        "from_addr": from_addr, "to_addrs": to_addrs,
        "cc_addrs": cc_addrs, "bcc_addrs": bcc_addrs,
        "reply_to": reply_to, "in_reply_to": in_reply_to,
        "references_hdr": references_hdr, "sent_at_utc": sent_at_utc,
        "text_body": text_body, "html_body": html_body,
        "raw_headers": raw_headers
    }
    return email_row, extract_attachments(msg)

# ---------------- DB Helpers ----------------

def ensure_tables(conn):
    ddl = """
    CREATE SCHEMA IF NOT EXISTS mail;
    CREATE TABLE IF NOT EXISTS mail.emails (
        id BIGSERIAL PRIMARY KEY,
        message_id TEXT UNIQUE,
        subject TEXT, from_addr TEXT, to_addrs TEXT, cc_addrs TEXT, bcc_addrs TEXT,
        reply_to TEXT, in_reply_to TEXT, references_hdr TEXT,
        sent_at_utc TIMESTAMPTZ,
        text_body TEXT, html_body TEXT, raw_headers TEXT,
        fts tsvector
    );
    CREATE TABLE IF NOT EXISTS mail.attachments (
        id BIGSERIAL PRIMARY KEY,
        email_id BIGINT REFERENCES mail.emails(id) ON DELETE CASCADE,
        filename TEXT, content_type TEXT, is_inline BOOLEAN DEFAULT FALSE,
        content_id TEXT, size_bytes BIGINT, sha256_hex TEXT, content BYTEA
    );
    CREATE INDEX IF NOT EXISTS idx_emails_fts ON mail.emails USING GIN(fts);
    """
    with conn.cursor() as cur: cur.execute(ddl)
    conn.commit()

def email_exists(conn, message_id: Optional[str]) -> Optional[int]:
    if not message_id: return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM mail.emails WHERE message_id=%s;", (message_id,))
        row = cur.fetchone()
        return int(row[0]) if row else None

def upsert_email(conn, email_row: Dict[str, Any]) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mail.emails
                (message_id, subject, from_addr, to_addrs, cc_addrs, bcc_addrs,
                 reply_to, in_reply_to, references_hdr, sent_at_utc,
                 text_body, html_body, raw_headers, fts)
            VALUES
                (%(message_id)s, %(subject)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                 %(reply_to)s, %(in_reply_to)s, %(references_hdr)s, %(sent_at_utc)s,
                 %(text_body)s, %(html_body)s, %(raw_headers)s,
                 setweight(to_tsvector('simple', COALESCE(%(subject)s,'')), 'A')
                 || setweight(to_tsvector('simple', COALESCE(%(from_addr)s,'')), 'B')
                 || setweight(to_tsvector('simple', COALESCE(%(to_addrs)s,'')), 'B')
                 || setweight(to_tsvector('simple', COALESCE(%(cc_addrs)s,'')), 'C')
                 || setweight(to_tsvector('simple', COALESCE(%(text_body)s,'')), 'B')
                 || setweight(to_tsvector('simple', regexp_replace(COALESCE(%(html_body)s,''), '<[^>]+>', ' ', 'g')), 'C')
            )
            ON CONFLICT (message_id) DO NOTHING
            RETURNING id;
        """, email_row)
        row = cur.fetchone()
        if row: return int(row[0])
        if email_row["message_id"]:
            cur.execute("SELECT id FROM mail.emails WHERE message_id=%s;", (email_row["message_id"],))
            r = cur.fetchone()
            if r: return int(r[0])
        cur.execute("""
            INSERT INTO mail.emails
                (message_id, subject, from_addr, to_addrs, cc_addrs, bcc_addrs,
                 reply_to, in_reply_to, references_hdr, sent_at_utc,
                 text_body, html_body, raw_headers, fts)
            VALUES
                (%(message_id)s, %(subject)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                 %(reply_to)s, %(in_reply_to)s, %(references_hdr)s, %(sent_at_utc)s,
                 %(text_body)s, %(html_body)s, %(raw_headers)s,
                 setweight(to_tsvector('simple', COALESCE(%(subject)s,'')), 'A')
                 || setweight(to_tsvector('simple', COALESCE(%(from_addr)s,'')), 'B')
                 || setweight(to_tsvector('simple', COALESCE(%(to_addrs)s,'')), 'B')
                 || setweight(to_tsvector('simple', COALESCE(%(cc_addrs)s,'')), 'C')
                 || setweight(to_tsvector('simple', COALESCE(%(text_body)s,'')), 'B')
                 || setweight(to_tsvector('simple', regexp_replace(COALESCE(%(html_body)s,''), '<[^>]+>', ' ', 'g')), 'C')
            )
            RETURNING id;
        """, email_row)
        return int(cur.fetchone()[0])

def insert_attachments(conn, email_id: int, attachments: List[Dict[str, Any]], dedupe: bool):
    if not attachments: return
    rows = [(email_id, a["filename"], a["content_type"], a["is_inline"], a["content_id"],
             a["size_bytes"], a["sha256_hex"], psycopg2.Binary(a["data"])) for a in attachments]
    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO mail.attachments
                (email_id, filename, content_type, is_inline, content_id, size_bytes, sha256_hex, content)
            VALUES %s
        """, rows)

# ---------------- Progress ----------------

def format_hms(sec: float) -> str:
    sec = int(max(0, sec)); h, m = sec//3600, (sec%3600)//60; s = sec%60
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

def render_progress(i, total, start, imported, skipped, failed):
    pct = (i/total)*100 if total else 100; elapsed = time.time()-start
    rate = i/elapsed if elapsed>0 else 0; eta = (total-i)/rate if rate>0 else 0
    bar = "#"*int(pct/3.33)+"-"*(30-int(pct/3.33))
    return f"[{bar}] {pct:6.2f}% {i}/{total} imported:{imported} skipped:{skipped} failed:{failed} elapsed {format_hms(elapsed)} ETA {format_hms(eta)}"

# ---------------- Main ----------------

def main():
    p = argparse.ArgumentParser(description="Import EML files into PostgreSQL with FTS.")
    p.add_argument("eml_dir", help="Directory with .eml files")
    p.add_argument("--dsn", default=os.getenv("PG_DSN"), help="PostgreSQL DSN")
    p.add_argument("--dedupe-attachments", action="store_true", help="Skip duplicate attachments")
    args = p.parse_args()

    conn = psycopg2.connect(args.dsn or "")
    ensure_tables(conn)

    files = sorted(Path(args.eml_dir).rglob("*.eml"))
    total = len(files)
    print(f"Found {total} .eml files")
    if total == 0: return

    imported = skipped = failed = 0
    start = time.time()
    is_tty = sys.stdout.isatty()

    for i, path in enumerate(files, 1):
        try:
            email_row, attachments = parse_eml_file(path)
            if email_exists(conn, email_row.get("message_id")):
                skipped += 1
            else:
                with conn:
                    eid = upsert_email(conn, email_row)
                    insert_attachments(conn, eid, attachments, args.dedupe_attachments)
                imported += 1
        except Exception as e:
            failed += 1
            print(f"\n[WARN] Failed {path}: {e}")
        line = render_progress(i, total, start, imported, skipped, failed)
        if is_tty: sys.stdout.write("\r"+line); sys.stdout.flush()
        elif i % 50 == 0 or i == total: print(line)

    if is_tty: print()
    print(f"Done. Total:{total} imported:{imported} skipped:{skipped} failed:{failed}")

if __name__ == "__main__":
    main()
