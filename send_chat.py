import sys
import requests

sys.path.insert(0, "/root/ocr_extraction")

from config import CHAT_WEBHOOK_URL


SCREENSHOT_BASE_URL = "http://161.97.176.170:8002/mount_system_temp"


def send_google_chat(data):

    if not CHAT_WEBHOOK_URL:
        print("CHAT_WEBHOOK_URL not configured.")
        return

    activity = data["latest_activity"]

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
                        "subtitle": data["subject"],
                    },
                    "sections": [
                        {
                            "widgets": [

                                {
                                    "decoratedText": {
                                        "topLabel": "Last Modified",
                                        "text": data["last_modified"],
                                    }
                                },

                                {
                                    "decoratedText": {
                                        "topLabel": "Author",
                                        "text": activity["author"],
                                    }
                                },

                                {
                                    "decoratedText": {
                                        "topLabel": "Activity",
                                        "text": activity["display_time"],
                                    }
                                },

                                {
                                    "textParagraph": {
                                        "text": activity["text"],
                                    }
                                },

                                #
                                # Screenshot Preview
                                #
                                # {
                                #     "image": {
                                #         "imageUrl": image_url,
                                #         "altText": "Ticket Screenshot",
                                #     }
                                # },

                                #
                                # Raw Screenshot URL
                                #
                                {
                                    "decoratedText": {
                                        "topLabel": "Screenshot URL: COPY & PASTE into browser to view",
                                        "text": f'<a href="{image_url}">{image_url}</a>',
                                    }
                                },

                                #
                                # Buttons
                                #
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
                                                "textParagraph": {
                                                    "text": f'<b>Screenshot:</b><br><a href="{image_url}">Open Screenshot</a>'
                                                }
                                            },
                                            {
                                                {
                                                    "textParagraph": {
                                                        "text": f'<a href="{image_url}">{image_url}</a>'
                                                    }
                                                }
                                            }
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

        r = requests.post(
            CHAT_WEBHOOK_URL,
            json=payload,
            timeout=30,
        )

        print(
            "Google Chat:",
            r.status_code,
            r.text,
        )

        r.raise_for_status()

    except Exception as ex:

        print("Google Chat Error:", ex)