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

from playwright.async_api import async_playwright

CONFIG_DIR = os.getenv("SL1_CONFIG_DIR", "/root/ocr_extraction")
if CONFIG_DIR not in sys.path:
    sys.path.insert(0, CONFIG_DIR)

from config import TARGET_URL, USERNAME, PASSWORD

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Keep the authentication/session files outside the repository by default,
# while allowing the deployment to override the location explicitly.
DEFAULT_PROFILE_DIR = os.path.join(BASE_DIR, ".auth")
PROFILE_DIR = os.getenv("PROFILE_DIR", DEFAULT_PROFILE_DIR)#/root/ocr_extraction/monitor_support/.auth
STORAGE_STATE = os.path.join(PROFILE_DIR, "storage_state.json")

STATE_FILE = os.getenv(
    "STATE_FILE",
    os.path.join(BASE_DIR, "state.json"),
)

OUTPUT_DIR = os.getenv(
    "OUTPUT_DIR",
    os.path.join(BASE_DIR, "output"),
)

SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "/tmp")

JSON_DIR = os.path.join(OUTPUT_DIR, "json")

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(JSON_DIR, exist_ok=True)

DEBUG_MODE = os.getenv("DEBUG_MODE", "0").lower() in {"1", "true", "yes", "on"}
FORCE_PROCESS_ALL = os.getenv("FORCE_PROCESS_ALL", "0").lower() in {"1", "true", "yes", "on"}
# Google Chat notifications are intentionally disabled for this monitor.
# Debug runs must never send Chat messages, and the monitor currently does
# not send Chat messages in normal runs either.
ENABLE_GOOGLE_CHAT = False
HEADLESS = os.getenv("HEADLESS", "1").lower() not in {"0", "false", "no", "off"}

# Salesforce Lightning can continue rendering after DOMContentLoaded.
# These delays are deliberately conservative so Chatter comments are not
# inspected before the page has finished rendering.
PAGE_SETTLE_MS = int(os.getenv("PAGE_SETTLE_MS", "5000"))
TICKET_SETTLE_MS = int(os.getenv("TICKET_SETTLE_MS", "7000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("NETWORK_IDLE_TIMEOUT_MS", "15000"))
DEBUG_DIR = os.getenv(
    "DEBUG_DIR",
    os.path.join(SCREENSHOT_DIR, "monitor_support_debug"),
)

os.makedirs(DEBUG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

def _clean_text(value):
    """Normalize rendered Salesforce text without changing its content."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _parse_datetime_text(value):
    """Best-effort parser for Salesforce absolute/relative timestamps."""
    if value is None:
        return None

    text = _clean_text(value)
    if not text:
        return None

    # ISO-8601 values, including the trailing Z used by HTML datetime attrs.
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        pass

    now = datetime.now()
    lower = text.lower()

    try:
        if lower in {"just now", "now"}:
            return now

        match = re.match(r"^(\d+)\s+minute", lower)
        if match:
            return now - timedelta(minutes=int(match.group(1)))

        match = re.match(r"^(\d+)\s+hour", lower)
        if match:
            return now - timedelta(hours=int(match.group(1)))

        match = re.match(r"^(\d+)\s+day", lower)
        if match:
            return now - timedelta(days=int(match.group(1)))

        match = re.match(r"^(\d+)\s+week", lower)
        if match:
            return now - timedelta(weeks=int(match.group(1)))

        match = re.match(r"^(\d+)\s+month", lower)
        if match:
            return now - timedelta(days=30 * int(match.group(1)))

        match = re.match(r"^(\d+)\s+year", lower)
        if match:
            return now - timedelta(days=365 * int(match.group(1)))
    except Exception:
        pass

    # Common Salesforce absolute date/time forms.
    for fmt in (
        "%b %d, %Y, %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y, %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%m/%d/%Y, %I:%M %p",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue

    return None


async def _get_element_datetime(element):
    """Read a timestamp from an element's datetime/title attributes."""
    for attribute in ("datetime", "data-time", "title", "aria-label"):
        try:
            value = await element.get_attribute(attribute)
            parsed = _parse_datetime_text(value)
            if parsed is not None:
                return parsed
        except Exception:
            continue

    try:
        return _parse_datetime_text(await element.inner_text())
    except Exception:
        return None


async def _wait_for_salesforce_ready(page, selectors=None, settle_ms=None):
    """Wait for Salesforce navigation, Lightning rendering and Chatter DOM."""
    selectors = selectors or []

    try:
        await page.wait_for_load_state("load", timeout=30000)
    except Exception:
        logger.debug("Salesforce load event did not complete within timeout.")

    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=NETWORK_IDLE_TIMEOUT_MS,
        )
    except Exception:
        # Salesforce may keep background requests open. DOM readiness is the
        # important condition, so a network-idle timeout is not fatal.
        logger.debug("Salesforce networkidle timeout; continuing with DOM checks.")

    try:
        await page.locator("body").wait_for(state="visible", timeout=30000)
    except Exception:
        pass

    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(
                state="visible",
                timeout=30000,
            )
            logger.debug("Salesforce ready selector visible: %s", selector)
            break
        except Exception:
            continue

    # Wait for common Lightning loading overlays/spinners to disappear when
    # they are present. If Salesforce keeps one in the DOM, do not fail.
    for selector in (
        ".slds-spinner_container:visible",
        "lightning-spinner:visible",
        "[role='status'][aria-label*='Loading']:visible",
    ):
        try:
            await page.locator(selector).wait_for(
                state="hidden",
                timeout=10000,
            )
        except Exception:
            pass

    delay = PAGE_SETTLE_MS if settle_ms is None else settle_ms
    await page.wait_for_timeout(delay)
    logger.debug("Salesforce page settle wait complete: %sms", delay)


async def _element_xpath(element):
    """Return a best-effort XPath for a DOM element for debugging."""
    try:
        return await element.evaluate("""el => {
            if (!el) return null;
            const parts = [];
            while (el && el.nodeType === 1) {
                let index = 1;
                let sibling = el.previousElementSibling;
                while (sibling) {
                    if (sibling.tagName === el.tagName) index++;
                    sibling = sibling.previousElementSibling;
                }
                parts.unshift(el.tagName.toLowerCase() + '[' + index + ']');
                el = el.parentElement;
            }
            return '/' + parts.join('/');
        }""")
    except Exception as ex:
        logger.debug("Unable to calculate XPath: %s", ex)
        return None


async def _debug_ticket_dom(page, case_number, stage):
    """Save ticket screenshot/HTML and log the Salesforce comment DOM."""
    if not DEBUG_MODE:
        return

    ticket_dir = os.path.join(DEBUG_DIR, str(case_number))
    os.makedirs(ticket_dir, exist_ok=True)

    screenshot = os.path.join(ticket_dir, f"{stage}.png")
    html = os.path.join(ticket_dir, f"{stage}.html")

    try:
        await page.screenshot(path=screenshot, full_page=True)
        logger.debug("DEBUG SCREENSHOT [%s]: %s", stage, screenshot)
    except Exception as ex:
        logger.exception("DEBUG screenshot failed [%s]: %s", stage, ex)

    try:
        with open(html, "w", encoding="utf-8") as fp:
            fp.write(await page.content())
        logger.debug("DEBUG HTML [%s]: %s", stage, html)
    except Exception as ex:
        logger.exception("DEBUG HTML dump failed [%s]: %s", stage, ex)

    try:
        comment_count_loc = page.locator("li.qe-commentCount")
        logger.debug("DEBUG [%s] qe-commentCount count=%s", stage, await comment_count_loc.count())
        for i in range(await comment_count_loc.count()):
            node = comment_count_loc.nth(i)
            logger.debug(
                "DEBUG [%s] qe-commentCount[%s] text=%r xpath=%s",
                stage, i, _clean_text(await node.inner_text()), await _element_xpath(node),
            )
    except Exception as ex:
        logger.debug("DEBUG comment count inspection failed: %s", ex)

    try:
        more = page.get_by_text(re.compile(r"^\s*more\s+comments?\s*$", re.I))
        logger.debug("DEBUG [%s] More comments text matches=%s", stage, await more.count())
        for i in range(await more.count()):
            node = more.nth(i)
            logger.debug(
                "DEBUG [%s] More comments[%s] visible=%s xpath=%s",
                stage, i, await node.is_visible(), await _element_xpath(node),
            )
    except Exception as ex:
        logger.debug("DEBUG More comments inspection failed: %s", ex)

    try:
        comments = page.locator("article.cuf-commentItem")
        logger.debug("DEBUG [%s] article.cuf-commentItem count=%s", stage, await comments.count())
        for i in range(await comments.count()):
            node = comments.nth(i)
            logger.debug(
                "DEBUG [%s] comment[%s] visible=%s xpath=%s text=%r",
                stage, i, await node.is_visible(), await _element_xpath(node),
                _clean_text(await node.inner_text())[:500],
            )
    except Exception as ex:
        logger.debug("DEBUG comment inspection failed: %s", ex)



async def _find_activity_articles(page):
    """Return only the top-most visible Salesforce Chatter feed item.

    We intentionally do NOT scan every Chatter article.  The monitor's
    requirement is to inspect the first/top-most feed post only and then read
    the comments belonging to that post.
    """
    selectors = [
        "article.cuf-feedItem",
        "article[class*='cuf-feedItem']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()

            for i in range(count):
                article = locator.nth(i)

                try:
                    if not await article.is_visible():
                        continue

                    # A nested feed item/comment is not a top-level message.
                    nested_feed = await article.locator(
                        "xpath=ancestor::article[contains(@class,'cuf-feedItem')][1]"
                    ).count()

                    if nested_feed:
                        continue

                    logger.info(
                        "Selected top-most Salesforce Chatter feed article "
                        "(DOM index=%s, selector=%s).",
                        i,
                        selector,
                    )
                    return [article]

                except Exception:
                    continue

        except Exception:
            continue

    # Last-resort fallback: use the first visible article that is not nested
    # inside another article/comment.
    locator = page.locator("article")
    for i in range(await locator.count()):
        article = locator.nth(i)

        try:
            if not await article.is_visible():
                continue

            nested_article = await article.locator(
                "xpath=ancestor::article[1]"
            ).count()

            if nested_article:
                continue

            logger.info(
                "Selected top-most Salesforce article using fallback "
                "(DOM index=%s).",
                i,
            )
            return [article]

        except Exception:
            continue

    logger.debug("DEBUG top-most Chatter feed article: none found")
    return []


async def _find_activity_article(page):
    """Backward-compatible helper returning the first top-level feed article."""
    articles = await _find_activity_articles(page)
    if not articles:
        raise RuntimeError(
            "No visible Salesforce Chatter feed article was found on the ticket page."
        )
    return articles[0]


async def _get_activity_scope(article):
    """Return the Salesforce feed-item container that owns this post.

    In the current Salesforce DOM, the top-level ``article.cuf-feedItem``
    contains the post itself, while its comment cards and the ``More comments``
    control are rendered as sibling descendants of the same feed ``li``.
    Using that ``li`` as the scope lets us see comments for the selected post
    without scanning comments from other feed items.
    """
    try:
        container = article.locator("xpath=ancestor::li[1]").first
        if await container.count() and await container.is_visible():
            return container
    except Exception:
        pass

    return article


async def _click_more_comments_if_needed(page, article, comment_count):
    """Expand comments for the selected top-most Chatter post only."""
    scope = await _get_activity_scope(article)
    if comment_count < 2:
        logger.debug(
            "DEBUG comment_count=%s; no More comments expansion required.",
            comment_count,
        )
        return False

    more_re = re.compile(r"^\s*more\s+comments?\s*$", re.I)

    for attempt in range(1, 6):
        clicked = False

        try:
            # First use actual interactive controls inside the selected article.
            controls = scope.locator(
                "button, a, [role='button']"
            )

            for i in range(await controls.count()):
                node = controls.nth(i)

                try:
                    if not await node.is_visible():
                        continue

                    label = _clean_text(await node.inner_text())

                    if not more_re.fullmatch(label):
                        continue

                    await node.scroll_into_view_if_needed(timeout=3000)
                    await node.click(timeout=5000)
                    clicked = True

                    logger.info(
                        "Clicked More comments for top-most post "
                        "(reported=%s, attempt=%s).",
                        comment_count,
                        attempt,
                    )
                    break

                except Exception as ex:
                    logger.debug(
                        "Interactive More comments control %s failed: %s",
                        i,
                        ex,
                    )

            # Fallback: locate the exact text node and click it or its
            # nearest interactive ancestor.
            if not clicked:
                text_nodes = scope.locator(
                    "xpath=.//*[normalize-space()='More comments' or "
                    "normalize-space()='More comment']"
                )

                for i in range(await text_nodes.count()):
                    node = text_nodes.nth(i)

                    try:
                        if not await node.is_visible():
                            continue

                        target = node.locator(
                            "xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]"
                        )

                        if await target.count():
                            target = target.first
                        else:
                            target = node

                        await target.scroll_into_view_if_needed(timeout=3000)
                        await target.click(timeout=5000)
                        clicked = True

                        logger.info(
                            "Clicked More comments text fallback for "
                            "top-most post (reported=%s, attempt=%s).",
                            comment_count,
                            attempt,
                        )
                        break

                    except Exception as ex:
                        logger.debug(
                            "Text More comments fallback %s failed: %s",
                            i,
                            ex,
                        )

            if not clicked:
                # Fallback 3: Playwright text locator within the same feed-item.
                try:
                    text_locator = scope.get_by_text(
                        re.compile(r"^\s*more\s+comments?\s*$", re.I)
                    )
                    for i in range(await text_locator.count()):
                        node = text_locator.nth(i)
                        if not await node.is_visible():
                            continue
                        await node.scroll_into_view_if_needed(timeout=3000)
                        try:
                            await node.click(timeout=3000)
                        except Exception:
                            await node.evaluate("el => el.click()")
                        clicked = True
                        logger.info(
                            "Clicked More comments using text/JS fallback "
                            "for top-most post (attempt=%s).", attempt
                        )
                        break
                except Exception as ex:
                    logger.debug("Text/JS More comments fallback failed: %s", ex)

            if not clicked:
                logger.debug(
                    "No clickable More comments control found on top-most post."
                )
                break

            await page.wait_for_timeout(1000)

            visible_comments = await _count_rendered_comments(scope)

            logger.debug(
                "DEBUG More comments attempt=%s rendered=%s reported=%s",
                attempt,
                visible_comments,
                comment_count,
            )

            if visible_comments >= comment_count:
                break

        except Exception as ex:
            logger.debug(
                "More comments expansion attempt %s failed: %s",
                attempt,
                ex,
            )
            break

    visible_comments = await _count_rendered_comments(scope)

    logger.info(
        "More comments processing complete: reported=%s rendered=%s",
        comment_count,
        visible_comments,
    )

    return visible_comments > 0


async def _count_rendered_comments(scope):
    """Count visible comment cards inside the supplied feed-item scope."""
    selectors = [
        "article.cuf-commentItem",
        ".cuf-commentItem",
        "[class*='cuf-commentItem']",
        "[class*='commentItem']",
    ]

    seen = set()
    count = 0

    for selector in selectors:
        try:
            locator = scope.locator(selector)
            for i in range(await locator.count()):
                node = locator.nth(i)
                try:
                    if not await node.is_visible():
                        continue

                    key = await node.get_attribute("data-id")
                    key = key or await node.get_attribute("data-comment-id")
                    key = key or await node.get_attribute("id")
                    key = key or await _element_xpath(node)

                    if key in seen:
                        continue

                    seen.add(key)
                    count += 1
                except Exception:
                    continue
        except Exception:
            continue

    return count


async def _extract_comment_body(comment):
    """Extract only the actual Salesforce comment body.

    Salesforce's rendered comment card can contain the author, relative time,
    "Actions for this Feed Item Comment", the body, and Like controls in the
    same text node.  Prefer the dedicated body selectors, then use the
    rendered comment text and remove only those UI/header portions.
    """
    selectors = [
        ".cuf-commentBody .feedBodyInner",
        ".cuf-commentBody",
        ".cuf-feedBodyText .feedBodyInner",
        ".cuf-feedBodyText",
        "[class*='commentBody']",
        ".feedBodyInner",
    ]

    for selector in selectors:
        try:
            locator = comment.locator(selector)
            count = await locator.count()

            for i in range(count):
                node = locator.nth(i)

                try:
                    if not await node.is_visible():
                        continue

                    value = _clean_text(await node.inner_text())
                    if value:
                        return value
                except Exception:
                    continue

        except Exception:
            continue

    try:
        raw = _clean_text(await comment.inner_text())
    except Exception:
        return ""

    if not raw:
        return ""

    # In the current ScienceLogic UI the comment card is rendered like:
    #   Author
    #   7 days ago
    #   Actions for this Feed Item Comment
    #   <actual comment>
    #   Like
    #
    # Keep everything after the action/header marker and remove trailing UI.
    marker = re.search(
        r"Actions for this Feed Item Comment",
        raw,
        flags=re.I,
    )
    if marker:
        raw = raw[marker.end():]

    raw = re.sub(
        r"(?im)^\s*(like|liked|comment|reply|more comments?|view more)\s*$",
        "",
        raw,
    )

    return _clean_text(raw)


async def _extract_comments_from_scope_variants(scope):
    """Try several Salesforce DOM strategies within ONE feed-item scope.

    The strategies are deliberately layered: exact comment classes first,
    then known comment containers, then direct descendant articles. We never
    fall back to page-wide comment scanning because that could associate a
    comment from another Chatter post with the selected top-most post.
    """
    strategies = [
        "article.cuf-commentItem",
        ".cuf-commentItem",
        "[class*='cuf-commentItem']",
        "[class*='commentItem']",
        "article[class*='comment']",
    ]

    seen_nodes = set()
    candidates = []

    for selector in strategies:
        try:
            locator = scope.locator(selector)
            for i in range(await locator.count()):
                node = locator.nth(i)
                if not await node.is_visible():
                    continue
                key = (
                    await node.get_attribute("data-id")
                    or await node.get_attribute("data-comment-id")
                    or await node.get_attribute("id")
                    or await _element_xpath(node)
                )
                if key in seen_nodes:
                    continue
                seen_nodes.add(key)
                candidates.append(node)
        except Exception as ex:
            logger.debug("Comment selector fallback failed (%s): %s", selector, ex)

    # Final scoped fallback: identify articles that contain the Salesforce
    # comment action marker. This is still restricted to the selected feed li.
    if not candidates:
        try:
            locator = scope.locator("article")
            for i in range(await locator.count()):
                node = locator.nth(i)
                if not await node.is_visible():
                    continue
                text = _clean_text(await node.inner_text())
                if "Actions for this Feed Item Comment" not in text:
                    continue
                key = await _element_xpath(node)
                if key not in seen_nodes:
                    seen_nodes.add(key)
                    candidates.append(node)
        except Exception as ex:
            logger.debug("Scoped comment article fallback failed: %s", ex)

    comments = []
    seen_content = set()
    for index, comment in enumerate(candidates):
        try:
            body = await _extract_comment_body(comment)
            if not body:
                continue

            author = ""
            for selector in (
                "a.cuf-entityLink[href*='/profile/']",
                "a[href*='/profile/']",
                "a[href*='/contact/']",
                "a[href*='/user/']",
            ):
                try:
                    node = comment.locator(selector).first
                    if await node.count():
                        author = _clean_text(await node.inner_text())
                        if author:
                            break
                except Exception:
                    continue

            display_time = ""
            comment_time = None
            for selector in (
                "feeds_timestamping-comment-creation time",
                "time",
                "feeds_timestamping-comment-creation",
                "[class*='commentAge']",
                "[class*='timestamp']",
            ):
                try:
                    node = comment.locator(selector).first
                    if not await node.count():
                        continue
                    display_time = _clean_text(await node.inner_text())
                    comment_time = await _get_element_datetime(node)
                    if comment_time is not None:
                        break
                except Exception:
                    continue

            key = (author, display_time, body)
            if key in seen_content:
                continue
            seen_content.add(key)
            item = {
                "author": author,
                "display_time": display_time,
                "estimated_time": comment_time.isoformat() if comment_time else None,
                "text": body,
            }
            comments.append(item)
            logger.info(
                "TOP-POST COMMENT FALLBACK[%s]: author=%r time=%r text=%r",
                index, author, display_time, body[:1000],
            )
        except Exception as ex:
            logger.debug("Scoped fallback comment %s failed: %s", index, ex)

    return comments


async def _extract_comments(article):
    """Extract comments belonging ONLY to the selected top-level post.

    Salesforce renders the comment cards outside the top-level article element
    but inside the same feed-item ``li``.  Scope extraction to that feed-item
    container so comments from later/older feed posts cannot be selected.
    """
    scope = await _get_activity_scope(article)

    selectors = [
        "article.cuf-commentItem",
        ".cuf-commentItem",
        "[class*='cuf-commentItem']",
        "[class*='commentItem']",
    ]

    candidates = []
    seen_nodes = set()

    for selector in selectors:
        try:
            locator = scope.locator(selector)
            count = await locator.count()

            for i in range(count):
                comment = locator.nth(i)

                try:
                    if not await comment.is_visible():
                        continue

                    key = await comment.get_attribute("data-id")
                    key = key or await comment.get_attribute("data-comment-id")
                    key = key or await comment.get_attribute("id")
                    key = key or await _element_xpath(comment)

                    if key in seen_nodes:
                        continue

                    seen_nodes.add(key)
                    candidates.append(comment)

                except Exception:
                    continue

        except Exception:
            continue

    comments = []
    seen_content = set()

    for index, comment in enumerate(candidates):
        try:
            body = await _extract_comment_body(comment)

            if not body:
                logger.debug(
                    "DEBUG comment[%s] skipped because no comment body was found.",
                    index,
                )
                continue

            author = ""

            for selector in (
                "a.cuf-entityLink[href*='/profile/']",
                "a[href*='/profile/']",
                "a[href*='/contact/']",
                "a[href*='/user/']",
            ):
                try:
                    author_locator = comment.locator(selector).first

                    if await author_locator.count():
                        author = _clean_text(
                            await author_locator.inner_text()
                        )

                        if author:
                            break

                except Exception:
                    continue

            display_time = ""
            comment_time = None

            for selector in (
                "feeds_timestamping-comment-creation time",
                "time",
                "feeds_timestamping-comment-creation",
                "[class*='commentAge']",
                "[class*='timestamp']",
            ):
                try:
                    time_locator = comment.locator(selector).first

                    if not await time_locator.count():
                        continue

                    display_time = _clean_text(
                        await time_locator.inner_text()
                    )

                    comment_time = await _get_element_datetime(
                        time_locator
                    )

                    if comment_time is not None:
                        break

                except Exception:
                    continue

            key = (
                author,
                display_time,
                body,
            )

            if key in seen_content:
                continue

            seen_content.add(key)

            item = {
                "author": author,
                "display_time": display_time,
                "estimated_time": (
                    comment_time.isoformat()
                    if comment_time is not None
                    else None
                ),
                "text": body,
            }

            comments.append(item)

            logger.info(
                "TOP-POST COMMENT[%s]: author=%r time=%r text=%r",
                index,
                author,
                display_time,
                body[:1000],
            )

        except Exception as ex:
            logger.debug(
                "Unable to extract Salesforce comment %s: %s",
                index,
                ex,
            )

    return comments


async def _extract_top_level_post(article):
    """Extract a top-level post without nested comment content."""
    activity = {
        "type": "post",
        "author": "",
        "display_time": "",
        "estimated_time": None,
        "text": "",
    }

    for selector in (
        "a.cuf-entityLink[href*='/profile/']",
        "a[href*='/profile/']",
        "a[href*='/contact/']",
    ):
        try:
            author_locator = article.locator(selector).first

            if await author_locator.count():
                activity["author"] = _clean_text(
                    await author_locator.inner_text()
                )

                if activity["author"]:
                    break

        except Exception:
            continue

    post_time = None

    try:
        time_nodes = article.locator("time")

        for i in range(await time_nodes.count()):
            time_node = time_nodes.nth(i)

            try:
                if not await time_node.is_visible():
                    continue

                # Exclude timestamps inside nested comments.
                inside_comment = await time_node.locator(
                    "xpath=ancestor::*[contains(@class,'cuf-commentItem') or contains(@class,'commentItem')][1]"
                ).count()

                if inside_comment:
                    continue

                display_time = _clean_text(
                    await time_node.inner_text()
                )

                parsed = await _get_element_datetime(
                    time_node
                )

                if display_time or parsed is not None:
                    activity["display_time"] = display_time
                    post_time = parsed or _parse_datetime_text(
                        display_time
                    )
                    break

            except Exception:
                continue

    except Exception:
        pass

    activity["estimated_time"] = (
        post_time.isoformat()
        if post_time is not None
        else None
    )

    for selector in (
        ".forceChatterFeedBodyText",
        ".feedBodyInner",
    ):
        try:
            nodes = article.locator(selector)

            for i in range(await nodes.count()):
                node = nodes.nth(i)

                inside_comment = await node.locator(
                    "xpath=ancestor::*[contains(@class,'cuf-commentItem') or contains(@class,'commentItem')][1]"
                ).count()

                if inside_comment:
                    continue

                text = _clean_text(
                    await node.inner_text()
                )

                if text:
                    activity["text"] = text
                    return activity

        except Exception:
            continue

    try:
        raw = _clean_text(await article.inner_text())

        # Strip only obvious action labels. The exact post body is preferred
        # above, so this fallback is intentionally conservative.
        raw = re.sub(
            r"(?im)^\s*(like|liked|comment|reply|more comments?|view more)\s*$",
            "",
            raw,
        )

        activity["text"] = _clean_text(raw)

    except Exception:
        pass

    return activity


async def _extract_article_activity(page, article, article_index=0, case_number=None):
    """Extract a post and its nested comments from one feed article."""
    activity = await _extract_top_level_post(article)

    comment_count = 0

    try:
        scope = await _get_activity_scope(article)

        count_nodes = scope.locator("li, span, a, button, div")

        for i in range(await count_nodes.count()):
            node = count_nodes.nth(i)

            try:
                if not await node.is_visible():
                    continue

                text = _clean_text(await node.inner_text())

                match = re.fullmatch(
                    r"(\d+)\s+comments?(?:\s*[·•].*)?",
                    text,
                    re.I,
                )

                if match:
                    comment_count = max(
                        comment_count,
                        int(match.group(1)),
                    )

            except Exception:
                continue

    except Exception:
        pass

    if comment_count == 0:
        try:
            article_text = _clean_text(
                await scope.inner_text()
            )

            match = re.search(
                r"\b(\d+)\s+comments?\b",
                article_text,
                flags=re.I,
            )

            if match:
                comment_count = int(match.group(1))

        except Exception:
            pass

    logger.debug(
        "DEBUG ARTICLE[%s] comment_count=%s xpath=%s",
        article_index,
        comment_count,
        await _element_xpath(article),
    )

    await _debug_ticket_dom(
        page,
        case_number or "unknown",
        f"article_{article_index}_before_more_comments",
    )

    if comment_count >= 2:
        await _click_more_comments_if_needed(
            page,
            article,
            comment_count,
        )

    await _debug_ticket_dom(
        page,
        case_number or "unknown",
        f"article_{article_index}_after_more_comments",
    )

    comments = await _extract_comments(article)

    # Fallback ladder: Salesforce DOM variants can place comment cards in
    # different descendants of the same feed-item container. Try the
    # progressively broader/alternate scopes only when the primary strategy
    # returned nothing. Never search the whole Chatter feed.
    if not comments:
        scope = await _get_activity_scope(article)
        comments = await _extract_comments_from_scope_variants(scope)

    await _debug_ticket_dom(
        page,
        case_number or "unknown",
        f"article_{article_index}_after_comment_extraction",
    )

    activity["comments"] = comments
    activity["latest_comment"] = None
    activity["comment_is_newer"] = False
    activity["comment_count_reported"] = comment_count

    timed_comments = [
        c for c in comments
        if c.get("estimated_time")
    ]

    if timed_comments:
        latest_comment = max(
            timed_comments,
            key=lambda c: c["estimated_time"],
        )
    elif comments:
        latest_comment = comments[-1]
    else:
        latest_comment = None

    activity["latest_comment"] = latest_comment

    post_time = _parse_datetime_text(
        activity.get("estimated_time")
    )

    comment_time = (
        _parse_datetime_text(
            latest_comment.get("estimated_time")
        )
        if latest_comment
        else None
    )

    if latest_comment and comment_time is not None:
        activity["comment_is_newer"] = (
            post_time is None
            or comment_time > post_time
        )

    logger.info(
        "ARTICLE %s: author=%r post_time=%r "
        "comments_reported=%s comments_extracted=%s "
        "latest_comment_time=%r comment_is_newer=%s",
        article_index,
        activity.get("author"),
        activity.get("display_time"),
        comment_count,
        len(comments),
        latest_comment.get("display_time")
        if latest_comment else None,
        activity["comment_is_newer"],
    )

    return activity


async def extract_latest_activity(page, case_number=None):
    """Inspect ONLY the top-most Chatter post and its comments."""
    articles = await _find_activity_articles(page)

    if not articles:
        raise RuntimeError(
            "No Salesforce Chatter feed articles found."
        )

    # Deliberately inspect exactly one feed item: the top-most message.
    article = articles[0]

    activity = await _extract_article_activity(
        page,
        article,
        article_index=0,
        case_number=case_number,
    )

    comments = activity.get("comments", [])

    # The requested activity is:
    #   - the top-level post if it has no comments
    #   - otherwise the newest rendered comment belonging to that post.
    #
    # We do not compare against comments from any other feed item.
    latest_comment = activity.get("latest_comment")

    if latest_comment:
        result = dict(activity)
        result["type"] = "comment"
        result["author"] = latest_comment.get("author", "")
        result["display_time"] = latest_comment.get("display_time", "")
        result["estimated_time"] = latest_comment.get("estimated_time")
        result["text"] = latest_comment.get("text", "")
        result["selected_from_article"] = 0
        result["selected_activity_is_comment"] = True
    else:
        result = dict(activity)
        result["type"] = "post"
        result["selected_from_article"] = 0
        result["selected_activity_is_comment"] = False

    await _debug_ticket_dom(
        page,
        case_number or "unknown",
        "final_activity_selected",
    )

    logger.info(
        "SELECTED TOP-MOST ACTIVITY: type=%s author=%r "
        "time=%r text=%r comments_on_top_post=%s",
        result["type"],
        result["author"],
        result["display_time"],
        result["text"][:500],
        len(comments),
    )

    return result

# -------------------------------------------------------------------
# State
# -------------------------------------------------------------------
# -------------------------------------------------------------------
# State
# -------------------------------------------------------------------
def _normalize_activity_value(value):
    """Normalize activity fields so harmless UI whitespace changes do not
    create a false new-activity detection."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def make_activity_id(author, text, activity_type="post", estimated_time=None):
    """Create a stable fingerprint for the actual selected Chatter activity.

    Relative display times such as "17 hours ago" are intentionally not
    included because they change between monitor runs. When Salesforce gives
    us a concrete estimated timestamp, include it so two otherwise identical
    messages at different times are still distinguishable.
    """
    parts = [
        _normalize_activity_value(activity_type).lower(),
        _normalize_activity_value(author),
        _normalize_activity_value(text),
    ]

    if estimated_time:
        parts.append(_normalize_activity_value(estimated_time))

    return hashlib.sha1(
        "|".join(parts).encode("utf-8")
    ).hexdigest()


def make_activity_fingerprint(activity):
    """Return the deduplication fingerprint for a selected activity."""
    if not isinstance(activity, dict):
        return ""

    return make_activity_id(
        activity.get("author"),
        activity.get("text"),
        activity.get("type", "post"),
        activity.get("estimated_time"),
    )


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
    Ensure that the Salesforce session is authenticated.

    Handles:
    - Existing valid storage_state
    - Normal Salesforce /login page
    - loginflow pages
    - Username/password login
    - Saving a refreshed storage_state
    - Verifying successful authentication
    - Re-opening TARGET_URL after authentication
    """

    logger.info("Opening Support page...")

    await page.goto(
        TARGET_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    await _wait_for_salesforce_ready(
        page,
        selectors=["body"],
        settle_ms=PAGE_SETTLE_MS,
    )

    await page.screenshot(
        path="/tmp/salesforce_login.png",
        full_page=True,
    )

    # ------------------------------------------------------------
    # Detect whether the current page is a login page
    # ------------------------------------------------------------

    async def is_login_page():

        url = page.url.lower()

        # URL-based detection
        if "/login" in url or "loginflow" in url:
            return True

        # Visible username/password fields
        username = page.locator(
            "input[id*='username']:visible, "
            "input[name*='username']:visible, "
            "input[type='email']:visible"
        ).first

        password = page.locator(
            "input[type='password']:visible"
        ).first

        return (
            await username.count() > 0
            and await password.count() > 0
        )

    # ------------------------------------------------------------
    # Handle "Finish Logging In"
    # ------------------------------------------------------------

    finish_login = page.get_by_text(
        "Finish Logging In",
        exact=True,
    )

    if await finish_login.count():

        logger.info(
            "Salesforce requires login completion."
        )

        await finish_login.first.click()

        await page.wait_for_timeout(5000)

    # ------------------------------------------------------------
    # Detect normal username/password login
    # ------------------------------------------------------------

    if await is_login_page():

        logger.info(
            "Salesforce username/password login page detected."
        )

        # --------------------------------------------------------
        # Username
        # --------------------------------------------------------

        username = page.get_by_label(
            "Username",
            exact=True,
        ).first

        if await username.count() == 0:

            username = page.locator(
                "input[id*='username']:visible, "
                "input[name*='username']:visible, "
                "input[type='email']:visible"
            ).first

        if await username.count() == 0:

            raise RuntimeError(
                "Visible Salesforce username field not found."
            )

        logger.info(
            "Visible username field found."
        )

        await username.fill(USERNAME)

        logger.info(
            "Username entered."
        )

        # --------------------------------------------------------
        # Password
        #
        # IMPORTANT:
        # Do NOT use input[name*='password'] here.
        #
        # Salesforce has a hidden field such as:
        #
        # <input type="hidden" name="passwordShown">
        #
        # which can otherwise be selected accidentally.
        # --------------------------------------------------------

        password = page.get_by_label(
            "Password",
            exact=True,
        ).first

        if await password.count() == 0:

            password = page.locator(
                "input[type='password']:visible"
            ).first

        if await password.count() == 0:

            raise RuntimeError(
                "Visible Salesforce password field not found."
            )

        logger.info(
            "Visible password field found."
        )

        await password.fill(PASSWORD)

        logger.info(
            "Password entered."
        )

        # --------------------------------------------------------
        # Login button
        # --------------------------------------------------------

        submit = page.locator(
            "button[type='submit']:visible, "
            "input[type='submit']:visible"
        ).first

        if await submit.count() == 0:

            submit = page.get_by_role(
                "button",
                name=re.compile(
                    r"log\s*in",
                    re.I,
                ),
            ).first

        if await submit.count() == 0:

            raise RuntimeError(
                "Visible Salesforce Log In button not found."
            )

        logger.info(
            "Clicking Salesforce Log In."
        )

        await submit.click()

        logger.info(
            "Salesforce Log In clicked."
        )

        # --------------------------------------------------------
        # Wait for navigation after login
        # --------------------------------------------------------

        try:

            await page.wait_for_load_state(
                "domcontentloaded",
                timeout=30000,
            )

        except Exception:

            # Some Salesforce pages update without a conventional
            # navigation event.
            logger.info(
                "No conventional page navigation detected."
            )

        # Give Salesforce/Lightning time to finish rendering.
        await _wait_for_salesforce_ready(
            page,
            selectors=["body"],
            settle_ms=PAGE_SETTLE_MS,
        )

        await page.screenshot(
            path="/tmp/salesforce_after_credentials.png",
            full_page=True,
        )

        # ------------------------------------------------------------
        # Wait for Salesforce Lightning login flow to complete
        # ------------------------------------------------------------

        logger.info(
            "Waiting for Salesforce login flow to complete..."
        )

        for attempt in range(12):

            await page.wait_for_timeout(2000)

            current_url = page.url.lower()

            logger.info(
                "Authentication check %d/12 - URL: %s",
                attempt + 1,
                page.url,
            )

            # --------------------------------------------------------
            # If we have reached the actual Salesforce application,
            # authentication is complete.
            # --------------------------------------------------------

            if (
                "/login" not in current_url
                and "loginflow" not in current_url
            ):

                logger.info(
                    "Salesforce login flow completed."
                )

                break

        else:

            await page.screenshot(
                path="/tmp/salesforce_authentication_failed.png",
                full_page=True,
            )

            raise RuntimeError(
                "Salesforce login flow did not complete. "
                f"Current URL: {page.url}"
            )

    # ------------------------------------------------------------
    # Verify that username/password fields are no longer visible
    # ------------------------------------------------------------

    visible_password = page.locator(
        "input[type='password']:visible"
    ).first

    if await visible_password.count() > 0:

        logger.warning(
            "A visible password field is still present "
            "after login."
        )

    # ------------------------------------------------------------
    # Save refreshed authenticated session
    # ------------------------------------------------------------

    await context.storage_state(
        path=STORAGE_STATE
    )

    logger.info(
        "Authenticated Salesforce session saved: %s",
        STORAGE_STATE,
    )

    # ------------------------------------------------------------
    # Re-open TARGET_URL after authentication.
    #
    # This is important because the login flow may leave us on
    # an intermediate Salesforce page.
    # ------------------------------------------------------------

    logger.info(
        "Opening Salesforce target page after authentication..."
    )

    await page.goto(
        TARGET_URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    # Salesforce Lightning needs additional time to render.
    await page.wait_for_timeout(5000)

    logger.info(
        "Final Salesforce URL: %s",
        page.url,
    )

    # ------------------------------------------------------------
    # Final authentication check
    # ------------------------------------------------------------

    final_url = page.url.lower()

    if (
        "/login" in final_url
        or "loginflow" in final_url
    ):

        await page.screenshot(
            path="/tmp/salesforce_authentication_failed_final.png",
            full_page=True,
        )

        raise RuntimeError(
            "Salesforce redirected back to the login page "
            "after authentication. "
            f"Final URL: {page.url}"
        )

    logger.info(
        "Salesforce authentication verified successfully."
    )

    logger.info(
        "Ready for ticket discovery."
    )

# -------------------------------------------------------------------
# Ticket Discovery
# -------------------------------------------------------------------


from urllib.parse import urljoin
import re


async def discover_tickets(page):

    logger.info("Reading Salesforce ticket table...")

    # ------------------------------------------------------------
    # Give Salesforce Lightning time to render.
    # ------------------------------------------------------------

    logger.info("Waiting for Salesforce ticket data...")

    ticket_selectors = [
        "table tbody tr",
        "table tr",
        "lightning-datatable",
        "[role='grid']",
        "[role='row']",
        "a[href*='/case/Case/']",
    ]

    found_selector = None

    for selector in ticket_selectors:

        try:

            await page.locator(selector).first.wait_for(
                state="visible",
                timeout=10000,
            )

            found_selector = selector

            logger.info(
                "Salesforce content detected using selector: %s",
                selector,
            )

            await _wait_for_salesforce_ready(
                page,
                selectors=[selector],
                settle_ms=PAGE_SETTLE_MS,
            )

            break

        except Exception:

            continue

    if found_selector is None:

        logger.error(
            "No Salesforce ticket content became visible."
        )

        logger.error(
            "Current URL: %s",
            page.url,
        )

        # Debug information
        logger.info(
            "table count: %s",
            await page.locator("table").count(),
        )

        logger.info(
            "tbody count: %s",
            await page.locator("tbody").count(),
        )

        logger.info(
            "tr count: %s",
            await page.locator("tr").count(),
        )

        logger.info(
            "role=row count: %s",
            await page.locator("[role='row']").count(),
        )

        logger.info(
            "lightning-datatable count: %s",
            await page.locator("lightning-datatable").count(),
        )

        logger.info(
            "case links: %s",
            await page.locator(
                "a[href*='/case/Case/']"
            ).count(),
        )

        raise RuntimeError(
            "Salesforce ticket table/data did not render."
        )

    # ------------------------------------------------------------
    # First try normal table rows.
    # ------------------------------------------------------------

    rows = page.locator("table tbody tr")

    count = await rows.count()

    logger.info(
        "Found %s standard table rows.",
        count,
    )

    tickets = []

    # ------------------------------------------------------------
    # Parse standard Salesforce table.
    # ------------------------------------------------------------

    if count > 0:

        for i in range(count):

            row = rows.nth(i)

            cells = row.locator("td")

            links = row.locator("a")

            link_count = await links.count()

            case_link = None

            for j in range(link_count):

                link = links.nth(j)

                text = (
                    await link.inner_text()
                ).strip()

                if re.fullmatch(
                    r"\d{8}",
                    text,
                ):

                    case_link = link
                    break

            if case_link is None:
                continue

            ticket_number = (
                await case_link.inner_text()
            ).strip()

            href = await case_link.get_attribute(
                "href"
            )

            if not href:
                continue

            url = urljoin(
                TARGET_URL,
                href,
            )

            cell_count = await cells.count()

            subject = ""
            last_modified = ""

            if cell_count > 1:

                subject = (
                    await cells.nth(1).inner_text()
                ).strip()

            if cell_count > 8:

                last_modified = (
                    await cells.nth(8).inner_text()
                ).strip()

            tickets.append(
                {
                    "ticket": ticket_number,
                    "subject": subject,
                    "url": url,
                    "last_modified": last_modified,
                }
            )

    # ------------------------------------------------------------
    # Fallback: search all case links on the page.
    # ------------------------------------------------------------

    if not tickets:

        logger.info(
            "No tickets found from table rows."
        )

        logger.info(
            "Trying Salesforce case links..."
        )

        links = page.locator(
            "a[href*='/case/Case/']"
        )

        link_count = await links.count()

        logger.info(
            "Found %s Salesforce case links.",
            link_count,
        )

        seen = set()

        for i in range(link_count):

            link = links.nth(i)

            text = (
                await link.inner_text()
            ).strip()

            href = await link.get_attribute(
                "href"
            )

            if not href:
                continue

            # ----------------------------------------------------
            # Extract case number.
            # ----------------------------------------------------

            match = re.search(
                r"\b(\d{8})\b",
                text,
            )

            if not match:

                match = re.search(
                    r"\b(\d{8})\b",
                    href,
                )

            if not match:
                continue

            ticket_number = match.group(1)

            if ticket_number in seen:
                continue

            seen.add(ticket_number)

            url = urljoin(
                TARGET_URL,
                href,
            )

            # ----------------------------------------------------
            # Try to determine subject from nearby text.
            # ----------------------------------------------------

            subject = ""

            try:

                parent_text = (
                    await link.locator(
                        "xpath=.."
                    ).inner_text()
                ).strip()

                subject = parent_text

            except Exception:

                pass

            tickets.append(
                {
                    "ticket": ticket_number,
                    "subject": subject,
                    "url": url,
                    "last_modified": "",
                }
            )

            logger.info(
                "CASE LINK: %s | %s",
                ticket_number,
                url,
            )

    logger.info(
        "Found %s tickets",
        len(tickets),
    )

    return tickets


# -------------------------------------------------------------------
# Compare with previous run
# -------------------------------------------------------------------


def _state_last_modified(value):
    """Read last_modified from both the current state format and the old
    string-only state format."""
    if isinstance(value, dict):
        return value.get("last_modified", "")
    return value or ""


def _state_activity_id(value):
    if isinstance(value, dict):
        return value.get("activity_id", "")
    return ""


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

        previous_last_modified = _state_last_modified(state[number])

        if previous_last_modified != ticket["last_modified"]:

            logger.info("UPDATED %s", number)

            changed.append(ticket)

    # Remove tickets that are no longer present in the active Salesforce
    # ticket list.
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

        await _wait_for_salesforce_ready(
            page,
            selectors=[
                "article.cuf-feedItem",
                "article[class*='cuf-feedItem']",
                "body",
            ],
            settle_ms=TICKET_SETTLE_MS,
        )

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

        await _debug_ticket_dom(page, case_number, "ticket_loaded")

        #
        # Extract ticket information
        #
        latest_activity = await extract_latest_activity(page, case_number)

        # Build a stable activity fingerprint from the actual selected
        # Chatter activity. This prevents a Salesforce last_modified change
        # from causing the same activity to be treated as new.
        activity_id = make_activity_fingerprint(latest_activity)
        previous_state = state.get(case_number)
        previous_activity_id = _state_activity_id(previous_state)
        activity_is_new = (
            not previous_activity_id
            or previous_activity_id != activity_id
        )

        if previous_activity_id and previous_activity_id == activity_id:
            logger.info(
                "DUPLICATE ACTIVITY: %s latest activity is unchanged; "
                "last_modified changed but activity fingerprint matches. "
                "No new activity will be recorded as a change.",
                case_number,
            )
        elif previous_activity_id:
            logger.info(
                "NEW ACTIVITY: %s activity fingerprint changed.",
                case_number,
            )
        else:
            logger.info(
                "INITIAL ACTIVITY SNAPSHOT: %s fingerprint recorded.",
                case_number,
            )

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
            "activity_id": activity_id,
            "activity_is_new": activity_is_new,
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

            return False

        logger.info(
            "Valid JSON saved: %s",
            json_file,
        )

        # Google Chat is intentionally not sent. In particular, DEBUG_MODE
        # must never produce a Chat notification. The monitor only records
        # local JSON/screenshots/logs.
        if DEBUG_MODE:
            logger.info(
                "DEBUG_MODE enabled: Google Chat notification suppressed for %s.",
                case_number,
            )

        #
        # Only mark the ticket processed after JSON validation succeeds.
        # Keep both the Salesforce last_modified value and the actual activity
        # fingerprint. The latter is what prevents duplicate processing when
        # Salesforce updates last_modified without changing the latest Chatter
        # activity.
        state[case_number] = {
            "last_modified": ticket["last_modified"],
            "activity_id": activity_id,
        }

        logger.info(
            "State updated for %s: last_modified=%r activity_id=%s activity_is_new=%s.",
            case_number,
            ticket["last_modified"],
            activity_id,
            activity_is_new,
        )

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
        # Failed screenshot + DOM dump for debugging.
        #
        try:
            await _debug_ticket_dom(page, case_number, "FAILED")

            failed = os.path.join(
                SCREENSHOT_DIR,
                f"{case_number}_FAILED.png",
            )

            await page.screenshot(
                path=failed,
                full_page=True,
            )

            logger.info("Failed-ticket screenshot saved: %s", failed)

        except Exception as debug_ex:
            logger.exception(
                "Failed to capture debugging artifacts for ticket %s: %s",
                case_number,
                debug_ex,
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
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )

        # --------------------------------------------------------
        # Browser context
        # --------------------------------------------------------

        if os.path.exists(STORAGE_STATE):

            logger.info(
                "Loading saved session..."
            )

            context = await browser.new_context(
                storage_state=STORAGE_STATE
            )

        else:

            logger.info(
                "Creating new browser session..."
            )

            context = await browser.new_context()

        page = await context.new_page()

        # --------------------------------------------------------
        # Authentication + target page
        # --------------------------------------------------------

        await login_if_required(
            page,
            context,
        )

        # --------------------------------------------------------
        # Salesforce diagnostics
        # --------------------------------------------------------

        logger.info("=" * 80)
        logger.info("SALESFORCE PAGE DIAGNOSTICS")
        logger.info(
            "URL: %s",
            page.url,
        )
        logger.info(
            "Title: %s",
            await page.title(),
        )
        logger.info(
            "Tables: %s",
            await page.locator("table").count(),
        )
        logger.info(
            "Rows: %s",
            await page.locator("tr").count(),
        )
        logger.info(
            "Grid rows: %s",
            await page.locator(
                "[role='row']"
            ).count(),
        )
        logger.info(
            "Lightning datatables: %s",
            await page.locator(
                "lightning-datatable"
            ).count(),
        )
        logger.info(
            "Case links: %s",
            await page.locator(
                "a[href*='/case/Case/']"
            ).count(),
        )
        logger.info("=" * 80)

        # --------------------------------------------------------
        # Discover tickets
        # --------------------------------------------------------

        tickets = await discover_tickets(
            page
        )

        # --------------------------------------------------------
        # Debug artifacts
        # --------------------------------------------------------

        logger.info(
            "Saving debug page..."
        )

        await page.screenshot(
            path="/tmp/monitor_support_debug.png",
            full_page=True,
        )

        with open(
            "/tmp/monitor_support_debug.html",
            "w",
            encoding="utf-8",
        ) as fp:

            fp.write(
                await page.content()
            )

        # --------------------------------------------------------
        # Compare state
        # --------------------------------------------------------

        if FORCE_PROCESS_ALL:

            logger.info(
                "DEBUG MODE: Processing all tickets"
            )

            changed = tickets

        else:

            changed = changed_tickets(
                tickets,
                state,
            )

        # --------------------------------------------------------
        # No changes
        # --------------------------------------------------------

        if not changed:

            logger.info("=" * 80)
            logger.info(
                "No ticket updates."
            )
            logger.info(
                "Exiting."
            )
            logger.info("=" * 80)

            save_state(state)

            await browser.close()

            return

        # --------------------------------------------------------
        # Process changed tickets
        # --------------------------------------------------------

        logger.info("=" * 80)
        logger.info(
            "%s ticket(s) require processing.",
            len(changed),
        )
        logger.info("=" * 80)

        for ticket in changed:

            await process_ticket(
                browser,
                context,
                ticket,
                state,
            )

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
