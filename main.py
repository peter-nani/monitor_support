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

    if "login" not in page.url.lower():
        return

    logger.info("Logging into Salesforce...")

    username = page.locator("input[id*=username]").first

    password = page.locator("input[id*=password]").first

    submit = page.locator("button[id*=submit],input[type=submit]").first

    await username.fill(USERNAME)

    await password.fill(PASSWORD)

    await submit.click()

    await page.wait_for_load_state("networkidle")

    await context.storage_state(path=STORAGE_STATE)

    logger.info("Session saved.")


# -------------------------------------------------------------------
# Ticket Discovery
# -------------------------------------------------------------------


from urllib.parse import urljoin
import re


async def discover_tickets(page):

    logger.info("Reading ticket table...")

    rows = page.locator("table tbody tr")

    count = await rows.count()

    logger.info("Found %s tickets", count)

    tickets = []

    for i in range(count):

        row = rows.nth(i)

        cells = row.locator("td")

        # Debug first row only
        if i == 0:
            print("\n===== FIRST ROW =====")
            for j in range(await cells.count()):
                print(
                    j,
                    (await cells.nth(j).inner_text()).replace("\n", " ")
                )

        #
        # Find the Case Number link anywhere in the row.
        #
        links = row.locator("a.forceOutputLookup")

        link_count = await links.count()

        case_link = None

        for j in range(link_count):

            link = links.nth(j)

            text = (await link.inner_text()).strip()

            #
            # Case numbers are always 8 digits.
            #
            if re.fullmatch(r"\d{8}", text):
                case_link = link
                break

        if case_link is None:
            logger.warning("No case link found in row %s", i)
            continue

        ticket_number = (await case_link.inner_text()).strip()

        href = await case_link.get_attribute("href")

        url = urljoin(TARGET_URL, href)

        #
        # These indexes came from your debug output.
        #
        subject = (await cells.nth(1).inner_text()).strip()

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

    try:

        logger.info("Opening ticket...")

        await page.goto(
            ticket["url"],
            wait_until="domcontentloaded"
        )

        await page.wait_for_selector(
            "body",
            timeout=20000,
        )

        await page.wait_for_timeout(5000)

        #
        # Small scroll to trigger lazy rendering
        #
        await page.evaluate("""
            window.scrollTo(
                0,
                document.body.scrollHeight * 0.02
            );
        """)

        await page.wait_for_timeout(1000)

        #
        # Basic ticket information
        #
        case_number = ticket["ticket"]
        subject = ticket["subject"]

        #
        # Extract newest activity
        #
        latest_activity = await extract_latest_activity(page)

        #
        # Save HTML for debugging (optional)
        #
        with open("/tmp/page.html", "w", encoding="utf-8") as fp:
            fp.write(await page.content())

        #
        # Screenshot
        #
        screenshot = os.path.join(
            SCREENSHOT_DIR,
            f"{case_number}.png"
        )

        await page.screenshot(
            path=screenshot,
            full_page=True,
        )

        logger.info("Screenshot saved.")

        #
        # JSON
        #
        data = {

            "ticket": case_number,

            "subject": subject,

            "url": ticket["url"],

            "last_modified": ticket["last_modified"],

            "captured_at": datetime.now().isoformat(),

            "latest_activity": latest_activity,

            "screenshot": screenshot,

        }

        json_file = os.path.join(
            JSON_DIR,
            f"{case_number}.json"
        )

        with open(
            json_file,
            "w",
            encoding="utf-8"
        ) as fp:

            json.dump(
                data,
                fp,
                indent=4,
                ensure_ascii=False,
            )

        logger.info("JSON saved.")

        #
        # Update state
        #
        state[case_number] = ticket["last_modified"]
        if ENABLE_GOOGLE_CHAT:

            try:

                send_google_chat(data)

                logger.info("Google Chat notification sent.")

            except Exception:

                logger.exception("Google Chat notification failed.")

        logger.info("Completed %s", case_number)

    except Exception as ex:

        logger.exception(ex)

        try:

            failed = os.path.join(
                SCREENSHOT_DIR,
                f"{ticket['ticket']}_FAILED.png"
            )

            await page.screenshot(
                path=failed,
                full_page=True,
            )

        except Exception:
            pass

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

        logger.info("Opening Salesforce...")

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

        #
        # Compare state
        #
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