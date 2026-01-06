
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


def parse_addresses(header_val: Optional[str]) -> str:
    if not header_val:
        return ""
    pairs = getaddresses([header_val])
    formatted = []
    for name, addr in pairs:
        if name and addr:
            formatted.append(f"{name} <{addr}>")
        elif addr:
            formatted.append(addr)
        elif name:
            formatted.append(name)
    return ", ".join(formatted)


def extract_bodies(msg: EmailMessage) -> Tuple[Optional[str], Optional[str]]:
    text_candidates = []
    html_candidates = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = part.get_content_disposition()
            if disp == "attachment":
                continue
            if ctype == "text/plain":
                try:
                    text_candidates.append(part.get_content())
                except Exception:
                    pass
            elif ctype == "text/html":
                try:
                    html_candidates.append(part.get_content())
                except Exception:
                    pass
    else:
        ctype = msg.get_content_type()
        if ctype == "text/plain":
            try:
                text_candidates.append(msg.get_content())
            except Exception:
                pass
        elif ctype == "text/html":
            try:
                html_candidates.append(msg.get_content())
            except Exception:
                pass

    best_text = max(text_candidates, key=lambda s: len(s), default=None)
    best_html = max(html_candidates, key=lambda s: len(s), default=None)
    return best_text, best_html


def extract_attachments(msg: EmailMessage) -> List[Dict[str, Any]]:
    results = []
    for part in msg.walk():
        disp = part.get_content_disposition()
        if disp not in ("attachment", "inline"):
            continue
        data = None
        try:
            content = part.get_content()
            if isinstance(content, bytes):
                data = content
            else:
                data = content.encode("utf-8")
        except Exception:
            try:
                data = part.get_payload(decode=True)
            except Exception:
                data = None
        if data is None:
            continue

        sha256_hex = hashlib.sha256(data).hexdigest()
        filename = part.get_filename() or "attachment.bin"
        content_type = part.get_content_type()
        is_inline = (disp == "inline")
        content_id = part.get("Content-ID")
        results.append({
            "filename": filename,
            "content_type": content_type,
            "is_inline": is_inline,
            "content_id": content_id,
            "data": data,
            "size_bytes": len(data),
            "sha256_hex": sha256_hex
        })
    return results


def extract_headers_only(msg: EmailMessage) -> str:
    lines = []
    for (k, v) in msg.items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


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
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            if dt is not None:
                if dt.tzinfo is None:
                    sent_at_utc = dt  # naive: keep as-is
                else:
                    sent_at_utc = dt.astimezone()  # normalize; psycopg2 keeps tz-aware
        except Exception:
            sent_at_utc = None

    text_body, html_body = extract_bodies(msg)
    raw_headers = extract_headers_only(msg)

    email_row = {
        "message_id": message_id,
        "subject": subject,
        "from_addr": from_addr,
        "to_addrs": to_addrs,
        "cc_addrs": cc_addrs,
        "bcc_addrs": bcc_addrs,
        "reply_to": reply_to,
        "in_reply_to": in_reply_to,
        "references_hdr": references_hdr,
        "sent_at_utc": sent_at_utc,
        "text_body": text_body,
        "html_body": html_body,
        "raw_headers": raw_headers
    }

    attachments = extract_attachments(msg)
    return email_row, attachments


def ensure_tables(conn):
    ddl = """
    CREATE SCHEMA IF NOT EXISTS mail;
    CREATE TABLE IF NOT EXISTS mail.emails (
        id              BIGSERIAL PRIMARY KEY,
        message_id      TEXT UNIQUE,
        subject         TEXT,
        from_addr       TEXT,
        to_addrs        TEXT,
        cc_addrs        TEXT,
        bcc_addrs       TEXT,
        reply_to        TEXT,
        in_reply_to     TEXT,
        references_hdr  TEXT,
        sent_at_utc     TIMESTAMPTZ,
        text_body       TEXT,
        html_body       TEXT,
        raw_headers     TEXT
    );
    CREATE TABLE IF NOT EXISTS mail.attachments (
        id              BIGSERIAL PRIMARY KEY,
        email_id        BIGINT NOT NULL REFERENCES mail.emails(id) ON DELETE CASCADE,
        filename        TEXT,
        content_type    TEXT,
        is_inline       BOOLEAN DEFAULT FALSE,
        content_id      TEXT,
        size_bytes      BIGINT,
        sha256_hex      TEXT,
        content         BYTEA
    );
    CREATE INDEX IF NOT EXISTS idx_emails_sent_at ON mail.emails(sent_at_utc);
    CREATE INDEX IF NOT EXISTS idx_emails_message_id ON mail.emails(message_id);
    CREATE INDEX IF NOT EXISTS idx_attachments_email_id ON mail.attachments(email_id);
    CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON mail.attachments(sha256_hex);
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def email_exists(conn, message_id: Optional[str]) -> Optional[int]:
    """Return existing email id if a row with this Message-ID exists, else None."""
    if not message_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM mail.emails WHERE message_id = %s;", (message_id,))
        row = cur.fetchone()
        return int(row[0]) if row else None


def upsert_email(conn, email_row: Dict[str, Any]) -> int:
    """Insert email (if new) and return id; if conflict on message_id, return existing id."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO mail.emails
                (message_id, subject, from_addr, to_addrs, cc_addrs, bcc_addrs,
                 reply_to, in_reply_to, references_hdr, sent_at_utc,
                 text_body, html_body, raw_headers)
            VALUES
                (%(message_id)s, %(subject)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                 %(reply_to)s, %(in_reply_to)s, %(references_hdr)s, %(sent_at_utc)s,
                 %(text_body)s, %(html_body)s, %(raw_headers)s)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING id;
        """, email_row)
        row = cur.fetchone()
        if row and row[0]:
            return int(row[0])

        # If conflict or message_id is NULL, try to fetch by message_id
        if email_row["message_id"]:
            cur.execute("SELECT id FROM mail.emails WHERE message_id = %s;", (email_row["message_id"],))
            existing = cur.fetchone()
            if existing:
                return int(existing[0])

        # No message_id or not found-insert and return id
        cur.execute("""
            INSERT INTO mail.emails
                (message_id, subject, from_addr, to_addrs, cc_addrs, bcc_addrs,
                 reply_to, in_reply_to, references_hdr, sent_at_utc,
                 text_body, html_body, raw_headers)
            VALUES
                (%(message_id)s, %(subject)s, %(from_addr)s, %(to_addrs)s, %(cc_addrs)s, %(bcc_addrs)s,
                 %(reply_to)s, %(in_reply_to)s, %(references_hdr)s, %(sent_at_utc)s,
                 %(text_body)s, %(html_body)s, %(raw_headers)s)
            RETURNING id;
        """, email_row)
        row = cur.fetchone()
        return int(row[0])


def insert_attachments(conn, email_id: int, attachments: List[Dict[str, Any]], dedupe_by_sha256: bool):
    if not attachments:
        return
    rows = []
    for a in attachments:
        rows.append((
            email_id,
            a["filename"],
            a["content_type"],
            a["is_inline"],
            a["content_id"],
            a["size_bytes"],
            a["sha256_hex"],
            psycopg2.Binary(a["data"]),
        ))
    with conn.cursor() as cur:
        if dedupe_by_sha256:
            cur.execute("""
                CREATE TEMP TABLE IF NOT EXISTS tmp_existing_sha
                (sha256_hex TEXT PRIMARY KEY) ON COMMIT DROP;
            """)
            cur.execute("DELETE FROM tmp_existing_sha;")
            cur.execute("""
                INSERT INTO tmp_existing_sha(sha256_hex)
                SELECT sha256_hex FROM mail.attachments WHERE email_id = %s;
            """, (email_id,))
            filtered = [r for r in rows if not _exists_sha(cur, r[6])]
            if not filtered:
                return
            execute_values(cur, """
                INSERT INTO mail.attachments
                    (email_id, filename, content_type, is_inline, content_id, size_bytes, sha256_hex, content)
                VALUES %s
            """, filtered)
        else:
            execute_values(cur, """
                INSERT INTO mail.attachments
                    (email_id, filename, content_type, is_inline, content_id, size_bytes, sha256_hex, content)
                VALUES %s
            """, rows)


def _exists_sha(cur, sha_hex: str) -> bool:
    cur.execute("SELECT 1 FROM tmp_existing_sha WHERE sha256_hex = %s;", (sha_hex,))
    return cur.fetchone() is not None


def iter_eml_files(root: Path) -> List[Path]:
    return sorted([p for p in root.rglob("*.eml") if p.is_file()])


# ------------ Progress helpers (ASCII-only) ----------------

def format_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def render_progress(i: int, total: int, start_ts: float, imported: int, skipped: int, failed: int) -> str:
    pct = (i / total) * 100 if total else 100.0
    elapsed = time.time() - start_ts
    rate = (i / elapsed) if elapsed > 0 else 0.0
    remaining = (total - i) / rate if rate > 0 else 0.0
    bar_width = 30
    filled = int((pct / 100.0) * bar_width)
    bar = "#" * filled + "-" * (bar_width - filled)
    return (f"[{bar}] {pct:6.2f}%  {i}/{total}  "
            f"imported:{imported} skipped:{skipped} failed:{failed}  "
            f"elapsed {format_hms(elapsed)}  ETA {format_hms(remaining)}")


def main():
    parser = argparse.ArgumentParser(description="Import EML files into PostgreSQL (emails + attachments).")
    parser.add_argument("eml_dir", type=str, help="Directory to scan recursively for .eml files.")
    parser.add_argument("--dsn", type=str, default=os.getenv("PG_DSN"),
                        help="PostgreSQL DSN, e.g. 'postgresql://user:pass@host:5432/dbname'. "
                             "If not given, falls back to libpq env vars (PGHOST, PGUSER, etc.).")
    parser.add_argument("--dedupe-attachments", action="store_true",
                        help="Skip inserting duplicate attachments for the same email (by sha256).")
    parser.add_argument("--no-progress", action="store_true",
                        help="Disable progress bar (use simple line logging).")
    args = parser.parse_args()

    if args.dsn:
        conn = psycopg2.connect(args.dsn)
    else:
        conn = psycopg2.connect("")

    try:
        ensure_tables(conn)
        files = iter_eml_files(Path(args.eml_dir))
        total = len(files)
        print(f"Found {total} .eml files")
        if total == 0:
            return

        imported = 0
        skipped = 0
        failed = 0
        start_ts = time.time()
        is_tty = sys.stdout.isatty() and (not args.no_progress)

        for i, path in enumerate(files, start=1):
            try:
                email_row, attachments = parse_eml_file(path)

                # Duplicate detection by Message-ID - skip entirely if exists
                existing_id = email_exists(conn, email_row.get("message_id"))
                if existing_id:
                    skipped += 1
                else:
                    with conn:
                        email_id = upsert_email(conn, email_row)
                        insert_attachments(conn, email_id, attachments, dedupe_by_sha256=args.dedupe_attachments)
                    imported += 1

            except Exception as e:
                failed += 1
                print(f"\n[WARN] Failed to import {path}: {e}")

            # Progress output
            line = render_progress(i, total, start_ts, imported, skipped, failed)
            if is_tty:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
            else:
                if i == total or (i % 50 == 0):
                    print(line)

        # Final newline to end progress line cleanly
        if is_tty:
            sys.stdout.write("\n")
            sys.stdout.flush()

        print(f"Done. Total: {total}, imported: {imported}, skipped (duplicates): {skipped}, failed: {failed}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
