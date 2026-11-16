"""Read what the gateway needs out of a raw email: the +tag it was addressed to, who it claims to
be from, what Gmail's own servers concluded about SPF, DKIM and DMARC, and the attachments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
# Gmail keeps the full +tag address in To/Cc. Delivered-To is checked too so a Bcc still works,
# but Gmail SEARCH (deliveredto:) only indexes the base address, so the tag is always read from
# the headers themselves.
TAG_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To")
# A result is "method=result" at the very start of one ;-separated piece of the header.
_METHOD = re.compile(r"\s*(dkim|spf|dmarc|arc)\s*=\s*([a-z]+)", re.I)


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedMail:
    tags: list[str]
    from_addr: str
    auth: dict[str, str]
    attachments: list[Attachment] = field(default_factory=list)
    subject: str = ""
    date_ts: float | None = None
    message_id: str = ""


def canonical_local(local: str, domain: str) -> str:
    """Gmail ignores dots and case in the part before the @."""
    local = local.lower()
    if domain in GMAIL_DOMAINS:
        local = local.replace(".", "")
    return local


def _same_domain(a: str, b: str) -> bool:
    return a == b or (a in GMAIL_DOMAINS and b in GMAIL_DOMAINS)


def _addresses(msg: EmailMessage, header: str) -> list[tuple[str, str]]:
    """(name, address) pairs of one header, or none at all when the header cannot be parsed. The
    email package raises on some malformed address headers (a bare "To: <" is enough), and a
    sender must not be able to stop the mailbox check with one."""
    try:
        return getaddresses([str(v) for v in msg.get_all(header, [])])
    except Exception:
        return []


def find_tags(msg: EmailMessage, base_local: str, base_domain: str) -> list[str]:
    base_domain = base_domain.lower()
    base = canonical_local(base_local, base_domain)
    pairs: list[tuple[str, str]] = []
    for header in TAG_HEADERS:
        pairs += _addresses(msg, header)
    found: list[str] = []
    for _name, addr in pairs:
        local, at, domain = addr.strip().lower().rpartition("@")
        if not at or not _same_domain(domain, base_domain) or "+" not in local:
            continue
        head, _, tag = local.partition("+")
        if canonical_local(head, domain) == base and tag and tag not in found:
            found.append(tag)
    return found


def provider_auth_results(msg: EmailMessage, authserv_id: str = "mx.google.com") -> dict[str, str]:
    """SPF/DKIM/DMARC results, trusted only from the TOPMOST Authentication-Results header and only
    if our own provider wrote it. Gmail adds its header on top of the message. Anything below that
    could have been typed in by the sender (RFC 8601 section 5), so it is ignored."""
    headers = msg.get_all("Authentication-Results", [])
    if not headers:
        return {}
    parts = _resinfo_parts(str(headers[0]))
    if parts[0].strip().lower() != authserv_id:
        return {}
    results: dict[str, str] = {}
    # Only the start of each piece counts. Gmail copies the sender's envelope address into the
    # header (in the spf comment and in smtp.mailfrom), and "=" is legal in an address, so a sender
    # can make the text "dmarc=pass" appear before Gmail's own dmarc result.
    for part in parts[1:]:
        match = _METHOD.match(part)
        if match:
            results.setdefault(match.group(1).lower(), match.group(2).lower())
    return results


def _resinfo_parts(text: str) -> list[str]:
    """Split an Authentication-Results value at the semicolons that separate results, dropping
    (comments) and not splitting inside "quoted strings" (RFC 8601 section 2.2)."""
    parts: list[str] = []
    current: list[str] = []
    depth, quoted, escaped = 0, False, False
    for ch in text:
        if escaped:
            escaped = False
            if not depth:
                current.append(ch)
        elif ch == "\\" and (quoted or depth):
            escaped = True
            if not depth:
                current.append(ch)
        elif depth:
            depth += {"(": 1, ")": -1}.get(ch, 0)
        elif quoted:
            current.append(ch)
            quoted = ch != '"'
        elif ch == "(":
            depth = 1
            current.append(" ")
        elif ch == '"':
            quoted = True
            current.append(ch)
        elif ch == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def attachments(msg: EmailMessage) -> list[Attachment]:
    out = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        out.append(Attachment(part.get_filename() or "attachment", part.get_content_type(), data))
    return out


def parse(raw: bytes, base_local: str, base_domain: str) -> ParsedMail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    senders = _addresses(msg, "From")
    from_addr = senders[0][1].strip().lower() if senders else ""
    date_ts = None
    if msg.get("Date"):
        try:
            date_ts = parsedate_to_datetime(str(msg["Date"])).timestamp()
        except (TypeError, ValueError, IndexError):
            date_ts = None
    return ParsedMail(
        tags=find_tags(msg, base_local, base_domain),
        from_addr=from_addr,
        auth=provider_auth_results(msg),
        attachments=attachments(msg),
        subject=str(msg.get("Subject", "")),
        date_ts=date_ts,
        message_id=str(msg.get("Message-ID", "")),
    )


def parse_headers(raw_headers: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw_headers, headersonly=True)
