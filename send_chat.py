import sys
import requests

sys.path.insert(0, "/root/ocr_extraction")

from config import CHAT_WEBHOOK_URL
import logging

logger = logging.getLogger(__name__)


SCREENSHOT_BASE_URL = "http://192.168.1.148:8002/mount_system_temp/"


def send_google_chat(data):

    if not CHAT_WEBHOOK_URL:
        print("CHAT_WEBHOOK_URL not configured.")
        return

    activity = data.get("latest_activity", {})

    image_url = (
        f"{SCREENSHOT_BASE_URL}/{data['ticket']}.png"
    )

    payload = {
        "cardsV2": [
            {
                "cardId": data["ticket"],
                "card": {
                    "header": {
                        "title": f"Ticket {data['ticket']}",
                        "subtitle": data.get("subject", ""),
                    },

                    "sections": [
                        {
                            "widgets": [

                                {
                                    "decoratedText": {
                                        "topLabel": "Last Modified",
                                        "text": data.get(
                                            "last_modified",
                                            ""
                                        ),
                                    }
                                },

                                {
                                    "decoratedText": {
                                        "topLabel": "Author",
                                        "text": activity.get(
                                            "author",
                                            ""
                                        ),
                                    }
                                },

                                {
                                    "decoratedText": {
                                        "topLabel": "Activity",
                                        "text": activity.get(
                                            "display_time",
                                            ""
                                        ),
                                    }
                                },

                                {
                                    "textParagraph": {
                                        "text": activity.get(
                                            "text",
                                            ""
                                        ),
                                    }
                                },

                                {
                                    "textParagraph": {
                                        "text": (
                                            f"<b>Screenshot URL:</b> "
                                            f"{image_url}"
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
                                                        "url": data["url"]
                                                    }
                                                },
                                            },

                                            {
                                                "text": "OPEN SCREENSHOT",
                                                "onClick": {
                                                    "openLink": {
                                                        "url": image_url
                                                    }
                                                },
                                            },

                                        ]
                                    }
                                },

                            ]
                        }
                    ]
                }
            }
        ]
    }

    try:

        response = requests.post(
            CHAT_WEBHOOK_URL,
            json=payload,
            timeout=30,
        )

        print(
            "Google Chat:",
            response.status_code,
            response.text,
        )

        response.raise_for_status()

    except Exception as ex:

        print("Google Chat Error:", ex)

def send_google_chat_error(ticket, error_type, error_message):

    if not CHAT_WEBHOOK_URL:
        logger.warning(
            "CHAT_WEBHOOK_URL not configured."
        )
        return

    payload = {
        "text": (
            "🚨 monitor_support ERROR\n\n"
            f"Ticket: {ticket}\n"
            f"Error: {error_type}\n"
            f"Details: {error_message}"
        )
    }

    response = requests.post(
        CHAT_WEBHOOK_URL,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()