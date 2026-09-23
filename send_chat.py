"""Google Chat notification integration for monitor_support.

The monitor imports this module only when notifications are enabled. Debug
runs are suppressed by main.py; this module is responsible for constructing
and delivering Google Chat payloads and surfacing HTTP/configuration failures
to the caller.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import requests

CONFIG_DIR = os.getenv("SL1_CONFIG_DIR", "/root/ocr_extraction")
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

try:
    from config import CHAT_WEBHOOK_URL as CONFIG_CHAT_WEBHOOK_URL
except ImportError:
    CONFIG_CHAT_WEBHOOK_URL = ""

CHAT_WEBHOOK_URL = os.getenv("CHAT_WEBHOOK_URL") or CONFIG_CHAT_WEBHOOK_URL

SCREENSHOT_BASE_URL = os.getenv(
    "SCREENSHOT_BASE_URL",
    "http://192.168.1.148:8002/mount_system_temp/",
)

REQUEST_TIMEOUT_SECONDS = int(os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS", "30"))

logger = logging.getLogger(__name__)


def _webhook_url() -> str:
    """Return the configured webhook URL or raise a clear configuration error."""
    if not CHAT_WEBHOOK_URL:
        raise RuntimeError(
            "CHAT_WEBHOOK_URL is not configured. "
            "Set it in config.py or the CHAT_WEBHOOK_URL environment variable."
        )
    return CHAT_WEBHOOK_URL


def _screenshot_url(ticket: str) -> str:
    return f"{SCREENSHOT_BASE_URL.rstrip('/')}/{ticket}.png"


def _build_ticket_card(data: dict[str, Any]) -> dict[str, Any]:
    """Build the Google Chat cardsV2 payload for a ticket activity."""
    ticket = str(data.get("ticket", "unknown"))
    activity = data.get("latest_activity") or {}
    screenshot_url = _screenshot_url(ticket)

    activity_time = str(activity.get("display_time") or "")
    estimated_time = activity.get("estimated_time")

    # Prefer Salesforce's concrete timestamp when available so the card shows
    # a stable activity date instead of a changing relative value such as
    # "17 hours ago".
    if estimated_time:
        try:
            from datetime import datetime

            parsed_activity_time = datetime.fromisoformat(
                str(estimated_time).replace("Z", "+00:00")
            )
            activity_time = parsed_activity_time.strftime(
                "%B %-d, %Y at %-I:%M %p"
            )
        except (TypeError, ValueError):
            pass

    activity_widgets = [
        {
            "decoratedText": {
                "topLabel": "Last Modified",
                "text": str(data.get("last_modified") or ""),
            }
        },
        {
            "decoratedText": {
                "topLabel": "Author",
                "text": str(activity.get("author") or ""),
            }
        },
        {
            "decoratedText": {
                "topLabel": "Activity",
                "text": activity_time,
            }
        },
        {
            "textParagraph": {
                "text": str(activity.get("text") or ""),
            }
        },
    ]

    sections = [
        {
            "header": "Latest Activity",
            "widgets": activity_widgets,
        }
    ]

    # Only add a separate comment section when the selected activity is the
    # post itself and its latest comment is newer than that post.
    latest_comment = activity.get("latest_comment")
    if (
        latest_comment
        and activity.get("type") != "comment"
        and activity.get("comment_is_newer")
    ):
        comment_widgets = []

        if latest_comment.get("author"):
            comment_widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "Comment Author",
                        "text": str(latest_comment["author"]),
                    }
                }
            )

        if latest_comment.get("display_time"):
            comment_widgets.append(
                {
                    "decoratedText": {
                        "topLabel": "Comment Time",
                        "text": str(latest_comment["display_time"]),
                    }
                }
            )

        if latest_comment.get("text"):
            comment_widgets.append(
                {
                    "textParagraph": {
                        "text": str(latest_comment["text"]),
                    }
                }
            )

        if comment_widgets:
            sections.append(
                {
                    "header": "Latest Comment",
                    "widgets": comment_widgets,
                }
            )

    sections.append(
        {
            "header": "Ticket",
            "widgets": [
                {
                    "textParagraph": {
                        "text": (
                            f"<b>Screenshot URL:</b> {screenshot_url}"
                        ),
                    }
                },
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "OPEN CASE",
                                "onClick": {
                                    "openLink": {
                                        "url": str(data.get("url", "")),
                                    }
                                },
                            },
                            {
                                "text": "OPEN SCREENSHOT",
                                "onClick": {
                                    "openLink": {
                                        "url": screenshot_url,
                                    }
                                },
                            },
                        ]
                    }
                },
            ],
        }
    )

    return {
        "cardsV2": [
            {
                "cardId": ticket,
                "card": {
                    "header": {
                        "title": f"Ticket {ticket}",
                        "subtitle": str(data.get("subject") or ""),
                    },
                    "sections": sections,
                },
            }
        ]
    }


def send_google_chat(data: dict[str, Any]) -> requests.Response:
    """Send a ticket notification to Google Chat.

    Raises:
        RuntimeError: if the webhook is not configured.
        requests.RequestException: if the HTTP request fails.
    """
    webhook_url = _webhook_url()

    if not isinstance(data, dict):
        raise TypeError("Google Chat notification data must be a dictionary.")

    payload = _build_ticket_card(data)

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    logger.info(
        "Google Chat notification delivered for ticket %s (HTTP %s).",
        data.get("ticket", "unknown"),
        response.status_code,
    )
    return response


def send_google_chat_error(
    ticket: str,
    error_type: str,
    error_message: str,
) -> requests.Response:
    """Send a compact error notification to Google Chat."""
    webhook_url = _webhook_url()

    payload = {
        "text": (
            "🚨 monitor_support ERROR\n\n"
            f"Ticket: {ticket}\n"
            f"Error: {error_type}\n"
            f"Details: {error_message}"
        )
    }

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    logger.info(
        "Google Chat error notification delivered for ticket %s (HTTP %s).",
        ticket,
        response.status_code,
    )
    return response
