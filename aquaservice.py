#!/usr/bin/env python3
"""Read-only Aquaservice invoices and next delivery. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://appclient.aquaservice.com:2053/api/v4_9/"
USER_AGENT = "Mozilla/5.0"
PDF_HOSTS = {"fadhuavad.aquaservice.com"}
MAX_BYTES = 50 * 1024 * 1024
ROOT = Path(__file__).resolve().parent


class ClientError(Exception):
    pass


def validate_pdf_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        valid = (p.scheme == "https" and p.hostname in PDF_HOSTS
                 and p.port in (None, 443) and not p.username and not p.password)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ClientError("PDF URL uses an unapproved host/scheme/port; inspect the API change before updating PDF_HOSTS.")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("API redirect refused; credentials were not forwarded.")


class PDFRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_pdf_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_bytes(request, *, pdf=False):
    opener = urllib.request.build_opener(PDFRedirect if pdf else NoRedirect)
    for attempt in range(3):
        try:
            with opener.open(request, timeout=60) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ClientError("Response exceeds the 50 MiB safety limit.")
                return data
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            if not retryable or attempt == 2:
                raise ClientError(f"HTTP {exc.code}; stopped. Check session credentials or upstream availability.") from None
            delay = min(int(retry_after), 30) if retry_after.isdigit() else 2 ** (attempt + 1)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise ClientError("Network/TLS request failed after three attempts.") from None
            time.sleep(2 ** (attempt + 1))
    raise ClientError("Request failed.")


def load_credentials(path):
    """Load and validate a private credential file; never prints its contents."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ClientError("Credentials must be a regular private file (chmod 600).")
        value = json.load(handle)
    required = ("application", "token", "pin", "contract", "delegation", "accountFilter")
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not value[k] for k in required):
        raise ClientError("Credentials require non-empty string fields: " + ", ".join(required))
    if value["application"] != "CLWEB":
        raise ClientError("This client supports application CLWEB only.")
    return {k: value[k] for k in required}


def api(credentials, path, extra):
    body = {k: credentials[k] for k in ("application", "token", "pin", "contract", "delegation")}
    body.update(extra)
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept-Language": "es", "User-Agent": USER_AGENT})
    try:
        result = json.loads(request_bytes(request))
    except (ValueError, UnicodeError):
        raise ClientError("API returned non-JSON content; possible upstream challenge or API change.") from None
    if not isinstance(result, dict) or result.get("error") or not isinstance(result.get("success"), dict):
        raise ClientError("API did not return success. Session may be expired; re-export credentials from the logged-in portal.")
    return result


def get_next_delivery_date(credentials):
    """Return datetime.date or None; raise ClientError for API/schema failures."""
    response = api(credentials, "delivery/next-delivery", {})
    delivery = response["success"]
    if "delivery_date" not in delivery:
        raise ClientError("API schema changed: success.delivery_date is missing.")
    value = delivery["delivery_date"]
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ClientError("API returned an invalid next delivery date; expected YYYY-MM-DD.")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ClientError("API returned an invalid next delivery date; expected YYYY-MM-DD.") from None


def invoice_key(invoice):
    return tuple(str(invoice[k]) for k in ("delegation", "accountCode", "invoiceType", "number"))


def invoice_date(invoice):
    return dt.datetime.strptime(invoice["date"], "%d/%m/%y").date()


def list_invoices(credentials):
    """Return (raw response, newest-first invoice records), containing private data."""
    response = api(credentials, "invoice/", {"accountFilter": credentials["accountFilter"]})
    records = response["success"].get("invoices")
    if not isinstance(records, list):
        raise ClientError("API schema changed: success.invoices is not an array.")
    seen = set()
    for item in records:
        required = ("number", "delegation", "accountCode", "invoiceType", "textDocument", "amount", "date")
        if not isinstance(item, dict) or any(k not in item for k in required):
            raise ClientError("API invoice schema changed; refusing a partial export.")
        try:
            invoice_date(item)
            key = invoice_key(item)
        except (TypeError, ValueError):
            raise ClientError("Invalid invoice date/identity in API response.") from None
        if key in seen:
            raise ClientError("Duplicate invoice identity returned; investigate before exporting.")
        seen.add(key)
    return response, sorted(records, key=lambda item: (invoice_date(item), invoice_key(item)), reverse=True)


def get_pdf(credentials, invoice):
    """Return validated PDF bytes for one invoice without writing a file."""
    extras = {k: invoice[k] for k in ("delegation", "accountCode", "invoiceType")}
    extras.update({"invoiceExt": "pdf", "title-document": invoice["textDocument"],
                   "pending-amount": str(invoice["amount"])})
    path = "invoice/" + urllib.parse.quote(str(invoice["number"]), safe="") + "/"
    response = api(credentials, path, extras)
    url = validate_pdf_url(response["success"].get("invoice"))
    # The file request deliberately carries no token, PIN, cookies, or API body.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    data = request_bytes(request, pdf=True)
    validate_pdf(data)
    return data


def validate_pdf(data):
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        raise ClientError("Downloaded/existing file is not a complete PDF; refusing to treat it as an invoice.")


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ClientError("Output directory must be private (mode 700), not a symlink.")


def write_new(path, data):
    """Publish a fully written mode-600 file atomically, without overwriting."""
    private_directory(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".aquaservice-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)  # Fails if destination already exists, including a symlink.
    finally:
        os.unlink(tmp)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def filename(invoice):
    key = invoice_key(invoice)
    identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]
    number = re.sub(r"[^A-Za-z0-9_-]", "_", str(invoice["number"]))[:80]
    return f"{invoice_date(invoice).isoformat()}-aquaservice-{number}-{identity}.pdf"


def check_existing(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ClientError("Existing invoice must be a private regular file.")
        data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ClientError("Existing PDF exceeds size limit.")
        validate_pdf(data)
        return data


def sync(credentials, response, records, destination):
    private_directory(destination)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    # Snapshot before downloading: preserves structured invoice data even on failure.
    write_new(destination / f"snapshot-{stamp}.private.json", json_bytes(response))
    report = {"created_utc": stamp, "api_invoice_count": len(response["success"]["invoices"]),
              "selected": len(records), "downloaded": 0, "skipped": 0, "failed": 0, "files": []}
    for invoice in records:
        path = destination / filename(invoice)
        try:
            if path.exists() or path.is_symlink():
                data = check_existing(path)
                status = "skipped"
            else:
                if invoice["invoiceType"] == "DEVOLUCION":
                    raise ClientError("The portal does not offer a PDF for DEVOLUCION records.")
                data = get_pdf(credentials, invoice)
                try:
                    write_new(path, data)
                    status = "downloaded"
                except FileExistsError:
                    data = check_existing(path)
                    status = "skipped"
            report[status] += 1
            report["files"].append({"file": path.name, "identity": invoice_key(invoice),
                "status": status, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            print(f"{status}: {path.name}", flush=True)
        except (ClientError, OSError) as exc:
            report["failed"] += 1
            # Do not log upstream response bodies or signed/private URLs.
            message = str(exc) if isinstance(exc, ClientError) else type(exc).__name__
            report["files"].append({"file": path.name, "status": "failed", "error": message})
            print(f"failed: {path.name}: {message}", file=sys.stderr, flush=True)
            # Stop on first error rather than hammering a revoked session.
            break
    report["not_attempted"] = report["selected"] - sum(report[k] for k in ("downloaded", "skipped", "failed"))
    write_new(destination / f"run-{stamp}.private.json", json_bytes(report))
    print(json.dumps({k: v for k, v in report.items() if k != "files"}))
    return 1 if report["failed"] or report["not_attempted"] else 0


def main(argv=None, *, default_directory=None):
    default_directory = Path.cwd() if default_directory is None else Path(default_directory)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=default_directory / "credentials.json")
    subs = parser.add_subparsers(dest="command", required=True)
    invoices = subs.add_parser("invoices", help="List or download invoices")
    invoice_subs = invoices.add_subparsers(dest="invoice_command", required=True)
    for name in ("list", "sync"):
        command = invoice_subs.add_parser(name)
        command.add_argument("--since", type=dt.date.fromisoformat, help="Inclusive YYYY-MM-DD filter")
        command.add_argument("--limit", type=int, help="Newest N records; omitted means all returned records")
        if name == "sync":
            command.add_argument("--output", type=Path, default=default_directory / "invoices")
    subs.add_parser("next-delivery", help="Print the next delivery date as YYYY-MM-DD")
    args = parser.parse_args(argv)
    if args.command == "next-delivery":
        credentials = load_credentials(args.credentials)
        date = get_next_delivery_date(credentials)
        print(date.isoformat() if date is not None else "No next delivery date is currently available.")
        return 0
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    credentials = load_credentials(args.credentials)
    response, records = list_invoices(credentials)
    if args.since:
        records = [x for x in records if invoice_date(x) >= args.since]
    if args.limit:
        records = records[:args.limit]
    if args.invoice_command == "list":
        for invoice in records:
            print(f"{invoice_date(invoice)}\t{invoice['number']}\t{invoice['amount']}\t{invoice['invoiceType']}")
        print(f"Selected {len(records)} of {len(response['success']['invoices'])} API records.", file=sys.stderr)
        return 0
    return sync(credentials, response, records, args.output)


def cli(argv=None, *, default_directory=None):
    """Console entry point with sanitized errors and process-compatible exit codes."""
    try:
        return main(argv, default_directory=default_directory)
    except (ClientError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ClientError) else type(exc).__name__
        print("Error: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Preserve script-relative paths for direct source execution. Installed console
    # and `python -m aquaservice` commands must not write into site-packages.
    sys.exit(cli(default_directory=ROOT if __spec__ is None else None))
