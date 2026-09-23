# ScienceLogic Support Monitor

Updated Salesforce/ScienceLogic support monitor with activity-aware Chatter comment detection.

## Main behavior

- Opens every ticket currently visible in the Salesforce Case list.
- Does **not** use the Case-list `Last Modified` value as the only change detector.
- Opens the Case detail page and inspects Chatter feed activity.
- Inspects **only the top-most Chatter feed message**.
- Reads only the comments belonging to that top-most message.
- Expands `More comments` when Salesforce has additional comments collapsed for that same post.
- Extracts comment author, timestamp, and the actual comment body separately.
- Selects the newest available comment from the top-most post; if there are no comments, the top-most post itself is selected.
- Stores an activity signature in `state.json` so the same activity is not repeatedly processed.
- Uses the previous ticket JSON as a migration baseline when an older state file does not yet contain an activity signature.

## Google Chat behavior

**Google Chat notifications are disabled.**

The monitor does **not** send Google Chat messages in any mode:

- `DEBUG_MODE=1`: no Chat messages.
- `FORCE_PROCESS_ALL=1`: no Chat messages.
- `HEADLESS=0`: no Chat messages.
- Normal/headless operation: no Chat messages.
- Processing failures and JSON validation failures: no Chat messages.

All results remain local through logs, JSON files, screenshots, and HTML debug artifacts.

`send_chat.py` is retained only for backward compatibility. Its functions are safe no-ops and never make an HTTP request.

## Files

- `main.py` - monitor implementation.
- `send_chat.py` - disabled compatibility helper; never posts to Google Chat.
- `requirements.txt` - Python dependencies.

`config.py` is intentionally not included because it contains deployment-specific credentials.

## Environment variables

```text
SL1_CONFIG_DIR=/tmp/ocr_extraction
PROFILE_DIR=/path/to/persistent/.auth
STATE_FILE=/path/to/state.json
OUTPUT_DIR=/path/to/output
SCREENSHOT_DIR=/tmp
DEBUG_DIR=/tmp/monitor_support_debug
DEBUG_MODE=1
FORCE_PROCESS_ALL=0
HEADLESS=1
PAGE_SETTLE_MS=5000
TICKET_SETTLE_MS=7000
NETWORK_IDLE_TIMEOUT_MS=15000
```

## Page loading behavior

Salesforce Lightning can continue rendering after `DOMContentLoaded`, so the monitor deliberately waits before extracting ticket or Chatter data.

For Salesforce pages it:

1. Waits for the normal page load event.
2. Attempts a `networkidle` wait.
3. Waits for the relevant Salesforce DOM selector to become visible.
4. Waits for common Lightning loading spinners/overlays to disappear.
5. Applies an additional settle delay before extraction.

The defaults are intentionally conservative:

- `PAGE_SETTLE_MS=5000` milliseconds for normal Salesforce pages.
- `TICKET_SETTLE_MS=7000` milliseconds for individual Case pages.
- `NETWORK_IDLE_TIMEOUT_MS=15000` milliseconds for the network-idle attempt.

A Salesforce network-idle timeout is not treated as a failure because Salesforce can keep background requests open. The DOM/readiness checks and settle delay are used instead.

## Debug artifacts

With `DEBUG_MODE=1`, each processed ticket can produce:

```text
/tmp/monitor_support_debug/<case>/
  ticket_loaded.png
  ticket_loaded.html
  article_0_before_more_comments.png
  article_0_before_more_comments.html
  article_0_after_more_comments.png
  article_0_after_more_comments.html
  article_0_after_comment_extraction.png
  article_0_after_comment_extraction.html
  final_activity_selected.png
  final_activity_selected.html
  FAILED.png
  FAILED.html
```

The monitor logs:

- Salesforce page readiness
- reported comment count
- `More comments` controls found/clicked
- rendered comment count
- extracted comment author/time/body
- final selected activity
- activity signature comparison

Relative timestamps such as `15 hours ago` are not used directly in the activity signature, preventing false changes as the displayed relative time moves forward.

## Chatter extraction behavior

The monitor intentionally inspects **only the top-most Chatter feed message** on each Salesforce case. It does not scan older feed messages and does not select a comment from another feed item.

For that top-most message it:

1. Reads the top-level post.
2. Reads only comments nested under that post.
3. Uses `More comments` when Salesforce exposes it, so additional comments for the same top-level post can be rendered.
4. Selects the newest available comment from that top-level post; if there are no comments, the top-level post itself is selected.
5. Extracts the actual comment body rather than Salesforce UI/header/action text such as `Actions for this Feed Item Comment` or `Like`.

This prevents an older comment belonging to another Chatter post from being selected as the case activity.

## Running a debug extraction

Use:

```bash
DEBUG_MODE=1 FORCE_PROCESS_ALL=1 HEADLESS=1 python3 main.py
```

This processes the visible tickets for inspection and **does not send any Google Chat message**.
