import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from urllib.parse import urljoin
import hashlib
import re
from datetime import datetime, timedelta
from send_chat import send_google_chat

from playwright.async_api import async_playwright

sys.path.insert(0, "/root/ocr_extraction")
from config import TARGET_URL, USERNAME, PASSWORD

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

PROFILE_DIR = "/root/ocr_extraction/monitor_support/.auth"
STORAGE_STATE = os.path.join(PROFILE_DIR, "storage_state.json")

STATE_FILE = "state.json"

OUTPUT_DIR = "output"

SCREENSHOT_DIR = "/tmp"

JSON_DIR = os.path.join(OUTPUT_DIR, "json")

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(JSON_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)
FORCE_PROCESS_ALL = False
ENABLE_GOOGLE_CHAT = True

async def extract_latest_activity(page):

    activity = {

        "type": "post",

        "author": "",

        "display_time": "",

        "estimated_time": None,

        "text": "",

        "comments": []

    }

    #
    # First (latest) activity
    #
    article = page.locator("article").first

    #
    # Expand post if it is truncated
    #
    try:

        expand = article.locator("a.cuf-more")

        if await expand.count() > 0:

            if await expand.first.is_visible():

                logger.info("Expanding post...")

                await expand.first.click()

                await page.wait_for_timeout(500)

    except Exception:
        pass

    #
    # Author
    #
    try:

        activity["author"] = (
            await article
            .locator("a[href*='/profile/']")
            .first
            .inner_text()
        ).strip()

    except Exception:
        pass

    #
    # Display Time
    #
    try:

        activity["display_time"] = (
            await article
            .locator("time")
            .first
            .inner_text()
        ).strip()

    except Exception:
        pass

    activity["estimated_time"] = estimate_time(
        activity["display_time"]
    )

    #
    # Post body
    #
    try:

        spans = article.locator("span.uiOutputText")

        fragments = []

        for i in range(await spans.count()):

            text = (
                await spans.nth(i).inner_text()
            ).strip()

            if text:
                fragments.append(text)

        #
        # Remove blank lines and duplicate consecutive lines
        #
        cleaned = []

        for line in fragments:

            line = line.strip()

            if not line:
                continue

            if cleaned and cleaned[-1] == line:
                continue

            cleaned.append(line)

        activity["text"] = "\n".join(cleaned)

    except Exception:
        #
        # Fallback
        #
        try:

            activity["text"] = (
                await article.inner_text()
            ).strip()

        except Exception:
            pass

    #
    # TODO:
    # Extract replies/comments when a ticket
    # actually contains replies.
    #
    activity["comments"] = []

    return activity
# -------------------------------------------------------------------
# State
# -------------------------------------------------------------------
def make_activity_id(author, text):
    return hashlib.sha1(
        f"{author}|{text}".encode("utf-8")
    ).hexdigest()


def estimate_time(display_time):

    now = datetime.now()

    text = display_time.lower().strip()

    try:

        m = re.match(r"(\d+)\s+minute", text)
        if m:
            return (
                now - timedelta(minutes=int(m.group(1)))
            ).isoformat()

        m = re.match(r"(\d+)\s+hour", text)
        if m:
            return (
                now - timedelta(hours=int(m.group(1)))
            ).isoformat()

        m = re.match(r"(\d+)\s+day", text)
        if m:
            return (
                now - timedelta(days=int(m.group(1)))
            ).isoformat()

    except Exception:
        pass

    return None

def load_state():

    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r") as fp:
        return json.load(fp)


def save_state(state):

    with open(STATE_FILE, "w") as fp:
        json.dump(state, fp, indent=4)


# -------------------------------------------------------------------
# Login
# -------------------------------------------------------------------


async def login_if_required(page, context):
    """
    Handle Salesforce authentication using the existing browser session.

    If Salesforce shows the "Finish Logging In" page, click the button
    and wait for Salesforce to complete the login flow.

    Screenshots are saved to /tmp for visual debugging.
    """

    logger.info("Opening Support page...")
    await page.goto(TARGET_URL, wait_until="domcontentloaded")

    await page.wait_for_timeout(3000)

    # Save the current page so we can visually inspect it if necessary.
    await page.screenshot(
        path="/tmp/salesforce_login.png",
        full_page=True
    )

    # Salesforce sometimes presents an intermediate
    # "Can't Display Page / Finish Logging In" page.
    finish_login = page.get_by_text(
        "Finish Logging In",
        exact=True
    )

    if await finish_login.count():
        logger.info("Salesforce requires login completion.")

        await finish_login.first.click()

        await page.wait_for_timeout(5000)

        await page.screenshot(
            path="/tmp/salesforce_after_login.png",
            full_page=True
        )

    # Check whether we are still on a Salesforce login page.
    if "loginflow" in page.url.lower():
        logger.info("Salesforce login flow still active.")

        username = page.locator("input[id*=username]").first
        password = page.locator("input[id*=password]").first

        if await username.count() and await password.count():
            logger.info("Username/password login form detected.")

            await username.fill(USERNAME)
            await password.fill(PASSWORD)

            submit = page.locator(
                "input[type=submit], "
                "button[type=submit], "
                "button:has-text('Log In')"
            ).first

            if await submit.count():
                await submit.click()

                await page.wait_for_timeout(5000)

                await page.screenshot(
                    path="/tmp/salesforce_after_credentials.png",
                    full_page=True
                )

    logger.info("Salesforce URL: %s", page.url)

# -------------------------------------------------------------------
# Ticket Discovery
# -------------------------------------------------------------------


from urllib.parse import urljoin
import re


async def discover_tickets(page):

    logger.info("Reading ticket table...")

    rows = page.locator("table tbody tr")

    count = await rows.count()

    logger.info("Found %s table rows", count)

    tickets = []

    for i in range(count):

        row = rows.nth(i)

        cells = row.locator("td")

        # Find the Case Number link anywhere in the row.
        links = row.locator("a")

        link_count = await links.count()

        case_link = None

        for j in range(link_count):

            link = links.nth(j)

            text = (await link.inner_text()).strip()

            # Salesforce case numbers are 8 digits.
            if re.fullmatch(r"\d{8}", text):
                case_link = link
                break

        if case_link is None:
            continue

        ticket_number = (await case_link.inner_text()).strip()

        href = await case_link.get_attribute("href")

        url = urljoin(TARGET_URL, href)

        # These indexes are from the existing Salesforce ticket table.
        cell_count = await cells.count()

        subject = ""
        last_modified = ""

        if cell_count > 1:
            subject = (await cells.nth(1).inner_text()).strip()

        if cell_count > 8:
            last_modified = (await cells.nth(8).inner_text()).strip()

        tickets.append(
            {
                "ticket": ticket_number,
                "subject": subject,
                "url": url,
                "last_modified": last_modified,
            }
        )

        logger.info(
            "%s | %s | %s",
            ticket_number,
            last_modified,
            subject,
        )

    logger.info("Found %s tickets", len(tickets))

    return tickets


# -------------------------------------------------------------------
# Compare with previous run
# -------------------------------------------------------------------


def changed_tickets(tickets, state):

    changed = []

    active = set()

    for ticket in tickets:

        number = ticket["ticket"]

        active.add(number)

        if number not in state:

            logger.info("NEW %s", number)

            changed.append(ticket)

            continue

        if state[number] != ticket["last_modified"]:

            logger.info("UPDATED %s", number)

            changed.append(ticket)

    #
    # remove closed tickets
    #

    for number in list(state.keys()):

        if number not in active:

            del state[number]

    return changed

# -------------------------------------------------------------------
# Process Ticket
# -------------------------------------------------------------------

async def process_ticket(browser, context, ticket, state):

    logger.info("=" * 80)
    logger.info("Processing %s", ticket["ticket"])

    page = await context.new_page()

    case_number = ticket["ticket"]

    try:

        logger.info("Opening ticket...")

        await page.goto(
            ticket["url"],
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await page.wait_for_selector(
            "body",
            timeout=30000,
        )

        await page.wait_for_timeout(5000)

        #
        # Trigger lazy rendering
        #
        await page.evaluate("""
            window.scrollTo(
                0,
                document.body.scrollHeight * 0.02
            );
        """)

        await page.wait_for_timeout(1000)

        #
        # Extract ticket information
        #
        latest_activity = await extract_latest_activity(page)

        #
        # Screenshot
        #
        screenshot = os.path.join(
            SCREENSHOT_DIR,
            f"{case_number}.png",
        )

        await page.screenshot(
            path=screenshot,
            full_page=True,
        )

        logger.info(
            "Screenshot saved: %s",
            screenshot,
        )

        #
        # Build JSON data
        #
        data = {
            "ticket": case_number,
            "subject": ticket["subject"],
            "url": ticket["url"],
            "last_modified": ticket["last_modified"],
            "captured_at": datetime.now().isoformat(),
            "latest_activity": latest_activity,
            "screenshot": screenshot,
        }

        #
        # Validate that the Python object can be
        # serialized as JSON before writing it.
        #
        try:

            json_string = json.dumps(
                data,
                indent=4,
                ensure_ascii=False,
            )

        except (TypeError, ValueError) as ex:

            logger.exception(
                "JSON serialization failed for %s",
                case_number,
            )

            if ENABLE_GOOGLE_CHAT:
                send_google_chat_error(
                    case_number,
                    "JSON serialization failed",
                    str(ex),
                )

            return False

        #
        # Write JSON
        #
        json_file = os.path.join(
            JSON_DIR,
            f"{case_number}.json",
        )

        with open(
            json_file,
            "w",
            encoding="utf-8",
        ) as fp:

            fp.write(json_string)

        #
        # Read it back and validate the actual file.
        #
        try:

            with open(
                json_file,
                "r",
                encoding="utf-8",
            ) as fp:

                validated_data = json.load(fp)

        except (OSError, json.JSONDecodeError) as ex:

            logger.exception(
                "JSON validation failed for %s",
                case_number,
            )

            if ENABLE_GOOGLE_CHAT:
                send_google_chat_error(
                    case_number,
                    "JSON file validation failed",
                    str(ex),
                )

            return False

        #
        # Make sure the JSON is actually an object.
        #
        if not isinstance(validated_data, dict):

            error = "JSON root is not an object."

            logger.error(
                "%s Ticket: %s",
                error,
                case_number,
            )

            if ENABLE_GOOGLE_CHAT:
                send_google_chat_error(
                    case_number,
                    "Invalid JSON structure",
                    error,
                )

            return False

        logger.info(
            "Valid JSON saved: %s",
            json_file,
        )

        #
        # Send Google Chat only after JSON is confirmed valid.
        #
        if ENABLE_GOOGLE_CHAT:

            try:

                send_google_chat(validated_data)

                logger.info(
                    "Google Chat notification sent."
                )

            except Exception as ex:

                logger.exception(
                    "Google Chat notification failed."
                )

                #
                # IMPORTANT:
                # The JSON is still valid, so we don't
                # delete it. But we DO notify about
                # the notification failure.
                #
                try:

                    send_google_chat_error(
                        case_number,
                        "Google Chat notification failed",
                        str(ex),
                    )

                except Exception:

                    logger.exception(
                        "Failed to send Google Chat error notification."
                    )

                return False

        #
        # Only mark the ticket processed after
        # JSON + notification succeeded.
        #
        state[case_number] = ticket["last_modified"]

        logger.info(
            "Completed %s successfully.",
            case_number,
        )

        return True

    except Exception as ex:

        logger.exception(
            "Failed processing ticket %s",
            case_number,
        )

        #
        # Failed screenshot for debugging.
        #
        try:

            failed = os.path.join(
                SCREENSHOT_DIR,
                f"{case_number}_FAILED.png",
            )

            await page.screenshot(
                path=failed,
                full_page=True,
            )

        except Exception:

            pass

        #
        # Immediately notify.
        #
        if ENABLE_GOOGLE_CHAT:

            try:

                send_google_chat_error(
                    case_number,
                    "Ticket processing failed",
                    str(ex),
                )

            except Exception:

                logger.exception(
                    "Failed to send error notification."
                )

        return False

    finally:

        await page.close()

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

async def main():

    state = load_state()

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )

        #
        # Reuse previous login session
        #
        if os.path.exists(STORAGE_STATE):

            logger.info("Loading saved session...")

            context = await browser.new_context(
                storage_state=STORAGE_STATE
            )

        else:

            logger.info("Creating new browser session...")

            context = await browser.new_context()

        page = await context.new_page()

        logger.info("Opening Support page...")

        await page.goto(TARGET_URL)
        await page.wait_for_load_state("domcontentloaded")

        #
        # Login if needed
        #
        await login_if_required(page, context)

        #
        # Give Lightning a chance to render
        #
        await page.wait_for_timeout(5000)

        #
        # Read ticket list
        #
        tickets = await discover_tickets(page)

        logger.info("Saving debug page...")

        await page.screenshot(
            path="/tmp/monitor_support_debug.png",
            full_page=True,
        )

        with open("/tmp/monitor_support_debug.html", "w", encoding="utf-8") as fp:
            fp.write(await page.content())

        logger.info("table count: %s", await page.locator("table").count())
        logger.info("tbody count: %s", await page.locator("tbody").count())
        logger.info("tr count: %s", await page.locator("tr").count())
        logger.info("a count: %s", await page.locator("a").count())
        #
        # Compare state
        #
        links = page.locator("a")

        for i in range(await links.count()):
            text = (await links.nth(i).inner_text()).strip()

            if re.search(r"\d{8}", text):
                logger.info("POSSIBLE TICKET LINK: %s", text)
        
        if FORCE_PROCESS_ALL:
            logger.info("DEBUG MODE: Processing all tickets")
            changed = tickets
        else:
            changed = changed_tickets(
                tickets,
                state,
            )

        if not changed:

            logger.info("=" * 80)
            logger.info("No ticket updates.")
            logger.info("Exiting.")
            logger.info("=" * 80)

            save_state(state)

            await browser.close()

            return

        logger.info("=" * 80)
        logger.info(
            "%s ticket(s) require processing.",
            len(changed),
        )
        logger.info("=" * 80)

        #
        # Process changed tickets
        #
        for ticket in changed:

            try:

                await process_ticket(
                    browser,
                    context,
                    ticket,
                    state,
                )

            except Exception:

                logger.exception(
                    "Failed processing %s",
                    ticket["ticket"],
                )

        #
        # Persist state
        #
        save_state(state)

        logger.info("=" * 80)
        logger.info("Completed.")
        logger.info("=" * 80)

        await browser.close()


# -------------------------------------------------------------------
# Entry Point
# -------------------------------------------------------------------

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Interrupted.")

    except Exception:

        logger.exception("Unhandled exception.")