"""Google Chat integration intentionally disabled for monitor_support.

The support monitor must not send Google Chat messages. This module is kept
only for backward compatibility with older deployments that may still import
it; both public functions are safe no-ops and never make an HTTP request.
"""

import logging

logger = logging.getLogger(__name__)


def send_google_chat(data):
    logger.info(
        "Google Chat disabled; notification suppressed for ticket %s.",
        (data or {}).get("ticket", "unknown"),
    )
    return None


def send_google_chat_error(ticket, error_type, error_message):
    logger.info(
        "Google Chat disabled; error notification suppressed for ticket %s.",
        ticket,
    )
    return None
