"""The log filter that keeps the bot token, app password and PIN hash out of the log."""
import io
import logging

from printtoken.app import Redact

TOKEN = "123456:AAH-secret-token-value"


def test_secrets_are_blanked_in_messages_and_tracebacks():
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.addFilter(Redact([TOKEN]))
    log = logging.getLogger("printtoken.test_redact")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("url https://api.telegram.org/bot%s/getMe", TOKEN)
        try:
            raise RuntimeError(f"failed for https://api.telegram.org/bot{TOKEN}/getFile")
        except RuntimeError:
            log.exception("handling message failed")
    finally:
        log.removeHandler(handler)
    text = out.getvalue()
    assert TOKEN not in text
    assert text.count("<redacted>") == 2 and "RuntimeError" in text
