"""
screenshots.py
==============
Choose the one screenshot that best preserves how the article looked on its host
website at capture time.

A Browsertrix capture may hold several, and it stores them as WARC ``resource``
records under ``urn:<variant>:<url>`` - the word "screenshot" appears nowhere in
the URI. Verified against real captures: on bama.hu the ``urn:fullPage`` record
is 3.3 MB and its body is a raw PNG with no HTTP envelope.

PREFERENCE ORDER, best first:

1. ``urn:fullPageFinal`` - the whole page, after the page settled
2. ``urn:fullPage``      - the whole page
3. ``urn:view``          - the viewport only
4. ``urn:thumbnail``     - a thumbnail
5. a PNG/JPEG/WEBP member sitting in the zip (some WACZ producers)
6. ``screenshot.webp`` beside the .wacz, from the 2026-08-07 Playwright backfill

1-5 are Browsertrix's own captures and are always preferred over 6. 6 exists
because the first crawl ran before ``--screenshot`` was enabled: those captures
hold no screenshot of their own, so the 2026-08-07 Playwright backfill wrote one
beside each. The split is by CAPTURE DATE, not by outlet - ripost.hu fills the
early cohort only because it was crawled first, and its later captures carry an
ordinary ``urn:fullPage`` like everyone else. Measured over 51 captures: nothing
captured up to 2026-08-05 08:28 has an in-archive screenshot, everything from
2026-08-12 11:29 on does; the switch was thrown somewhere in that gap.

A sidecar on an early capture is therefore normal. Its ABSENCE on one is a
backfill gap rather than a property of the crawl - 2 of the 3 early captures in
that sample have no sidecar, and ``choose`` returns None for them.

Exactly one screenshot is written. Nothing is re-rendered: this extractor never
starts a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .wacz import find_backfilled_screenshot

#: What the chosen screenshot is called in the output directory.
STEM = "screenshot"


@dataclass
class ScreenshotChoice:
    body: bytes
    filename: str
    #: Where it came from: a urn: variant, a zip member name, or "backfill".
    source: str

    @property
    def from_browsertrix(self) -> bool:
        return self.source != "backfill"


def choose(contents, wacz_path: Path) -> ScreenshotChoice | None:
    """The best screenshot available for this capture, or None if there is none."""
    if contents.screenshot:
        return ScreenshotChoice(
            body=contents.screenshot,
            filename=STEM + contents.screenshot_ext,
            source=contents.screenshot_source or "archive")

    sidecar = find_backfilled_screenshot(Path(wacz_path))
    if sidecar is not None:
        try:
            body = sidecar.read_bytes()
        except OSError:
            return None
        if body:
            return ScreenshotChoice(body=body, filename=STEM + sidecar.suffix.lower(),
                                    source="backfill")
    return None
