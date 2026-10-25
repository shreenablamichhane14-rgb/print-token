"""Plain words for every rejection reason, used in Telegram replies and the report."""

REASON_TEXT = {
    "no_tag": "it was not sent to a +tag print address",
    "malformed_tag": "the +tag is not a print address",
    "unknown_tag": "that print address was never issued",
    "expired_tag": "that print address had expired",
    "replayed_tag": "that print address was already used",
    "dmarc_fail": "the sender's domain failed DMARC (possible forgery)",
    "dmarc_none": "the sender's domain has no DMARC result",
    "dmarc_missing": "Gmail added no authentication result",
    "dmarc_softfail": "the sender could not be verified",
    "dmarc_temperror": "the sender check had a temporary error",
    "dmarc_permerror": "the sender's DMARC record is broken",
    "sender_not_allowlisted": "the sender is not on the allowlist",
    "no_attachment": "the email had no attachment",
    "too_many_attachments": "the email had more than 5 attachments",
    "oversize": "the file is too big",
    "unsupported_type": "the file type is not supported",
    "over_page_cap": "it has too many pages",
    "queue_full": "the print queue was full",
    "rate_limited": "the hourly print limit was reached",
    "print_error": "the printer reported an error",
    "photo_compressed": "it was sent as a photo instead of a file",
    "bad_pin": "wrong PIN",
    "pin_locked": "too many wrong PINs",
    "mint_refused": "no new address could be made",
    "not_allowlisted": "the Telegram account is not allowed",
    "download_failed": "the file could not be downloaded from Telegram",
    "internal_error": "something went wrong inside the gateway",
}

# Email rejections worth a Telegram message to the owner. Unknown and malformed tags are only
# logged, otherwise anyone guessing addresses could flood the owner's phone.
NOTIFY_MAIL_REASONS = {"expired_tag", "replayed_tag", "sender_not_allowlisted", "no_attachment",
                       "too_many_attachments", "oversize", "rate_limited", "queue_full"}


def describe(reason: str) -> str:
    if reason.startswith("dmarc_") and reason not in REASON_TEXT:
        return f"the sender check returned {reason[6:]}"
    return REASON_TEXT.get(reason, reason.replace("_", " "))


def should_notify_mail(reason: str) -> bool:
    return reason in NOTIFY_MAIL_REASONS or reason.startswith("dmarc_")
