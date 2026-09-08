"""The tray's usage flyout — a frameless dark card painted directly (no QSS).

Two things it does for TintaView's multi-agent world:

  - it renders one SECTION per agent (a small header line, then that agent's rows,
    or a one-line reason when the agent errored) instead of a single global usage
    block;
  - `set_results(dict[str, UsageResult])` is keyed by agent rather than taking one
    global usage payload, since there is no longer one "the" payload.

The remaining details are deliberate, not incidental: dismiss-on-focus-out,
remembering `hidden_at` so the click that just closed the flyout doesn't immediately
reopen it, and the rounded track/fill bars with severity colours.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QRectF, Qt

from tintaview.core.config import MAX_CLOCKS, ClockConfig, Config
from tintaview.i18n import t
from tintaview.stats import format as fmt
from tintaview.ui import icons

if TYPE_CHECKING:  # pragma: no cover - types only, no runtime import of the stats layer
    from tintaview.stats.model import UsageResult, UsageRow

# --------------------------------------------------------------------------- palette

CARD_BG = QtGui.QColor("#242427")
TEXT = QtGui.QColor("#e7e7ea")
SUBTLE = QtGui.QColor("#9b9ba3")
#: Secondary header ink, for the running-tool name beside an agent's title — it has to
#: read as an annotation on the agent, not as a second agent.
FAINT = QtGui.QColor("#75757e")
TRACK = QtGui.QColor("#3b3b42")
FILL = QtGui.QColor("#6c8cff")
WARN = QtGui.QColor("#e0a63a")
CRIT = QtGui.QColor("#e0574f")
BORDER = QtGui.QColor(255, 255, 255, 22)

# --------------------------------------------------------------------------- geometry

PAD = 18
CARD_W = 380
SECTION_GAP = 14  # extra vertical space between one agent's block and the next
HEADER_H = 24  # height of the badge+title header line, per section
CHEVRON_W = 16  # right-edge width reserved for the collapse affordance
REASON_LINE_H = 20.0  # one line of an errored section's failure sentence
#: Gap above the local-estimate block, separating it from the official rows (or from
#: the error line) it sits under. The same 8px `_row_layout` puts before a credits row,
#: for the same reason: it is a different *kind* of claim, not the next item in a list.
ESTIMATE_GAP = 8.0
#: A failure sentence wraps onto as many lines as it needs (`_wrap_reason`); this bounds
#: the one input that can run away — the exception repr in an `…error.unavailable`
#: message. Every catalogue reason is well under it, so every reason a user is expected
#: to act on renders complete whatever the system font.
#:
#: A cap on *lines* was tried first and is the wrong knob: three fitted the English
#: "Claude Code login expired — current usage can't be shown. Run `claude` to sign in
#: again." in the font this was measured with, and clipped it to "Run `claude` to si…"
#: on a CI runner whose font fits half as many characters per line. What a too-low cap
#: drops is always the tail, and the tail is the half telling the user how to fix it.
REASON_MAX_CHARS = 200

#: Tokens that must never begin a line, so they are glued to the word before them. On a
#: narrow card with a wide system font the alternative is a line opening on a dangling
#: "—" (observed on a Windows CI runner).
_REASON_NO_LEAD = ("—", "–", "-", "·")

# --------------------------------------------------------------------------- title bar

TOP_BAR_H = 40  # logo + "TintaView" + settings/close row, above the separator
TOP_BAR_BTN = 20.0  # settings/close hit-box side (also the icon's own bounding box)
TOP_BAR_BTN_GAP = 8.0  # gap between the settings and close hit-boxes
#: y where the first agent section begins when there is no clocks band. With one, the
#: band's measured height is added — `Flyout._content_top`, which every consumer of this
#: goes through, because the band is optional and its height depends on the system font.
CONTENT_TOP = TOP_BAR_H + SECTION_GAP

# --------------------------------------------------------------------------- clocks band

#: The two type sizes in the clocks band. The time is deliberately the largest text on
#: the card — it is what the band exists to be read at a glance — and the zone id under
#: it only has to be legible enough to confirm *which* place a time belongs to.
CLOCK_TIME_PT = 15
#: Floor `_fit_time_font` may shrink the time to. Four 12-hour columns ("11:33 AM") on a
#: 380px card do not fit at the preferred size — they overlapped their neighbours and the
#: fourth was clipped mid-word — and a locale whose am/pm text is longer than English's,
#: or a wider system font, moves that boundary again. So the size is fitted to what is
#: actually being drawn rather than assumed, and this is how small that may go before
#: legibility matters more than the last column's width.
CLOCK_TIME_PT_MIN = 10
CLOCK_ZONE_PT = 8
#: Floor for the zone label, fitted the same way and for the same reason as the time —
#: "Poland/Warsaw" is 83px in this repo's CI font and a four-column card gives it 76.
CLOCK_ZONE_PT_MIN = 6
CLOCK_LINE_GAP = 0.0  # between a time and the zone id under it (both rects are tight)
CLOCK_BAND_PAD = 9.0  # above the times and below the zone ids
CLOCK_LABEL_INSET = 5.0  # kept clear either side of a zone id, so it can't touch a divider
CLOCK_TIME_INSET = 4.0  # ditto for a time, which is `_fit_time_font`'s width budget

#: The vertical divider between two clocks. Fainter than `BORDER`: it separates two
#: readings of the same kind, so it should be the last thing on the card you notice.
CLOCK_SEP = QtGui.QColor(255, 255, 255, 26)


def _clock_fonts(base: QtGui.QFont) -> tuple[QtGui.QFont, QtGui.QFont]:
    """(time font, zone-id font) derived from the widget's own font."""
    time_font = QtGui.QFont(base)
    time_font.setPointSize(CLOCK_TIME_PT)
    zone_font = QtGui.QFont(base)
    zone_font.setPointSize(CLOCK_ZONE_PT)
    return time_font, zone_font


def _clock_band_height(base: QtGui.QFont) -> float:
    """Measured from the fonts rather than fixed as a constant.

    Same reasoning as `_wrap_reason`'s: the card is laid out in points against whatever
    the system font happens to be, and a hardcoded band height that fits on one machine
    clips a descender or leaves a gap on another.

    Always the *preferred* time size, even when `_fit_time_font` ends up drawing smaller.
    The band's height then depends only on the fonts — not on how wide the current times
    happen to be — so the card doesn't resize under the cursor when 9:59 becomes 10:00.
    """
    time_font, zone_font = _clock_fonts(base)
    return (CLOCK_BAND_PAD + QtGui.QFontMetricsF(time_font).height() + CLOCK_LINE_GAP
            + QtGui.QFontMetricsF(zone_font).height() + CLOCK_BAND_PAD)


@lru_cache(maxsize=64)
def _zone_valid(zone_id: str) -> bool:
    """Does Qt know this IANA id? Cached — the answer cannot change while we run, and
    the question is asked from `_layout`, which runs on every paint and mouse-move.

    Qt, not `zoneinfo`, on purpose: Windows ships no tz database, so `zoneinfo` there
    needs the `tzdata` wheel — see `ClocksConfig`. The trade-off is that Qt's Windows
    backend only knows the zones Windows knows, which is why an id is checked at all
    rather than assumed good.
    """
    return QtCore.QTimeZone(zone_id.encode()).isValid()


def _clock_time(zone_id: str, fmt: str, now: QtCore.QDateTime | None = None) -> str:
    """The current time in `zone_id`, or "" for a zone Qt doesn't know.

    Converted from the live zone on every call, never from a stored offset: that is what
    makes a DST change (and any future tz database update) show up on its own.
    """
    tz = QtCore.QTimeZone(zone_id.encode())
    if not tz.isValid():
        return ""
    utc = QtCore.QDateTime.currentDateTimeUtc() if now is None else now
    # "AP" renders the locale's own am/pm text, which is what a 12-hour clock is for;
    # 24-hour stays zero-padded so four columns line up.
    return utc.toTimeZone(tz).toString("h:mm AP" if fmt == "12h" else "HH:mm")


def _fit_time_font(base: QtGui.QFont, texts: list[str], width: float) -> QtGui.QFont:
    """The largest time size, `CLOCK_TIME_PT` down to `CLOCK_TIME_PT_MIN`, at which
    every one of `texts` fits `width`.

    Fitted to the strings actually being drawn because the inputs vary more than a fixed
    size can absorb: 24-hour "20:33" against 12-hour "11:33 AM", one column against
    four, English am/pm against a locale with longer words, and whatever the system font
    is. Measured, not guessed — an unfitted size drew four 12-hour columns straight
    through each other.
    """
    font = QtGui.QFont(base)
    for points in range(CLOCK_TIME_PT, CLOCK_TIME_PT_MIN - 1, -1):
        font.setPointSize(points)
        metrics = QtGui.QFontMetricsF(font)
        if all(metrics.horizontalAdvance(text) <= width for text in texts):
            break
    return font


@lru_cache(maxsize=64)
def _zone_place(zone_id: str) -> tuple[str, str, str]:
    """(country, ISO code, city) for an IANA id — ("Poland", "PL", "Warsaw").

    The country is Qt's, from the zone's own territory, so it needs no table here and
    stays in step with whatever tz database the OS has. Cached because it cannot change
    while we run and the band is laid out on every repaint.
    """
    territory = QtCore.QTimeZone(zone_id.encode()).territory()
    city = zone_id.rsplit("/", 1)[-1].replace("_", " ")
    if territory in (QtCore.QLocale.Country.AnyCountry, QtCore.QLocale.Country.World):
        # Qt's catch-all territories, named "Default" and "world" — `UTC` and the
        # deprecated aliases land here. Neither name says anything about where the time
        # is, so those zones are labelled by their own name alone.
        return "", "", city
    return QtCore.QLocale.territoryToString(territory), QtCore.QLocale.territoryToCode(territory), city


def _zone_label(zone_id: str, *, code: bool, city: bool) -> str:
    """"Poland/Warsaw", "PL/Warsaw" (`code`), or "Poland" (no `city`).

    Country/city rather than the raw IANA id ("Europe/Warsaw"): the region prefix is an
    artefact of how the tz database is organised, and the clock is configured by country
    in the first place, so that is the vocabulary the card answers in.

    A zone with no country falls back to its own name whatever the arguments say — Qt
    files `UTC` and the deprecated aliases under a territory called "Default", and
    "Default" is not a place.
    """
    country, iso, town = _zone_place(zone_id)
    prefix = iso if code else country
    if not prefix:
        return town
    return f"{prefix}/{town}" if city else prefix


#: How `_clock_labels` gives way when the full wording doesn't fit, as
#: (abbreviate the labels *with* a city, abbreviate the labels without one). Ordered by
#: what each step costs the reader: the city labels are the long ones, so they abbreviate
#: first, and a label that is only a country name shortens last because it was never the
#: problem. Applied to whole kinds of label, never to individual columns.
_FALLBACKS: tuple[tuple[bool, bool], ...] = ((False, False), (True, False), (True, True))


def _clock_labels(clocks: list[ClockConfig], base: QtGui.QFont,
                  width: float) -> tuple[list[str], QtGui.QFont]:
    """The band's labels and the font they fit in — one form per *kind* of label.

    Whether a clock shows its city is the user's, per clock (`ClockConfig.show_city`):
    it is a judgement about the place, since two American zones need a city to be told
    apart and the one in India does not. Whether a country is spelled out or abbreviated
    is not that — it is a question about the space available, which is decided here.

    Four columns leave ~76px each, which fits "India" and "PL/Warsaw" but neither
    "Poland/Warsaw" (83px) nor "United States/New York" (128px), so something has to
    give. `_FALLBACKS` gives way in order of how much each step costs the reader, and
    every label of the same kind always takes the same step:

        (full, full)  Poland/Warsaw · India · United States/New York · Japan
        (code, full)  PL/Warsaw · India · US/New York · Japan
        (code, code)  PL/Warsaw · IN · US/New York · JP

    Each is tried at `CLOCK_ZONE_PT` and shrunk towards `CLOCK_ZONE_PT_MIN` before the
    next one is considered, so the fuller wording survives wherever it can.

    The middle step is the point of having three. Abbreviating the *city* labels is what
    buys the room, and dragging "India" down to "IN" alongside them buys nothing — it
    was already short. What must not happen is two labels of the same kind disagreeing
    ("Poland/Warsaw" next to "IN/Kolkata"), because which of them fits depends on the
    system font, so the same config would look inconsistent in a different way on every
    machine.
    """
    font = QtGui.QFont(base)
    labels: list[str] = []
    for city_code, country_code in _FALLBACKS:
        labels = [
            _zone_label(c.zone, code=city_code if c.show_city else country_code,
                        city=c.show_city)
            for c in clocks
        ]
        for points in range(CLOCK_ZONE_PT, CLOCK_ZONE_PT_MIN - 1, -1):
            font.setPointSize(points)
            metrics = QtGui.QFontMetricsF(font)
            if all(metrics.horizontalAdvance(label) <= width for label in labels):
                return labels, font
    # Nothing fits even compact and small: elide, still uniformly. Reachable with a very
    # wide system font, or a city name long enough to beat the column on its own.
    font.setPointSize(CLOCK_ZONE_PT_MIN)
    metrics = QtGui.QFontMetricsF(font)
    return [metrics.elidedText(label, Qt.ElideRight, width) for label in labels], font


def _msecs_to_next_minute() -> int:
    """Delay until the top of the next minute, plus a little slack.

    Re-armed every tick instead of a 1-second repeating timer: with HH:MM there is
    nothing to redraw in between, and a boundary-aligned single shot re-syncs itself
    after a suspend/resume rather than drifting. The slack stops a tick that fires a
    hair early from repainting the minute it was supposed to replace.
    """
    since_minute = QtCore.QTime.currentTime().msecsSinceStartOfDay() % 60_000
    return max(250, 60_000 - since_minute + 50)

#: Effective session status (see `core.events`/`core.state.StateStore`) -> the
#: `ColorsConfig` attribute driving that status's colour. `"none"` (no session open
#: for that agent) deliberately has no entry: `_status_dot_color` returns None for it,
#: which `paintEvent` reads as "draw no dot at all" rather than some default colour.
_STATUS_DOT_KEYS = {"idle": "idle", "working": "working", "confirm": "confirm"}


def _severity_color(sev: str) -> QtGui.QColor:
    return {"warning": WARN, "critical": CRIT}.get(sev, FILL)


def _draw_chevron(p: QtGui.QPainter, header_rect: QRectF, collapsed: bool) -> None:
    """Small ▸ / ▾ mark at a header's right edge — the only visual cue that a
    section can be clicked to collapse/expand (the hover cursor is the other)."""
    cx = header_rect.right() - CHEVRON_W / 2
    cy = header_rect.center().y()
    s = 4.0
    if collapsed:
        pts = [QtCore.QPointF(cx - s * 0.6, cy - s), QtCore.QPointF(cx - s * 0.6, cy + s),
               QtCore.QPointF(cx + s * 0.7, cy)]
    else:
        pts = [QtCore.QPointF(cx - s, cy - s * 0.6), QtCore.QPointF(cx + s, cy - s * 0.6),
               QtCore.QPointF(cx, cy + s * 0.7)]
    p.setPen(Qt.NoPen)
    p.setBrush(SUBTLE)
    p.drawPolygon(QtGui.QPolygonF(pts))


def _draw_gear_icon(p: QtGui.QPainter, rect: QRectF) -> None:
    """A generic 8-tooth cog (star silhouette, punched with a background-colour
    hole) for the settings button — no trademarked icon set involved."""
    cx, cy = rect.center().x(), rect.center().y()
    r_out, r_in = rect.width() * 0.5, rect.width() * 0.32
    teeth = 8
    pts = [
        QtCore.QPointF(
            cx + (r_out if i % 2 == 0 else r_in) * math.cos(math.radians(i * (360 / (teeth * 2)))),
            cy + (r_out if i % 2 == 0 else r_in) * math.sin(math.radians(i * (360 / (teeth * 2)))),
        )
        for i in range(teeth * 2)
    ]
    p.setPen(Qt.NoPen)
    p.setBrush(TEXT)
    p.drawPolygon(QtGui.QPolygonF(pts))
    p.setBrush(CARD_BG)
    p.drawEllipse(QtCore.QPointF(cx, cy), rect.width() * 0.19, rect.width() * 0.19)


def _draw_close_icon(p: QtGui.QPainter, rect: QRectF) -> None:
    """A plain X for the close button."""
    inset = rect.adjusted(rect.width() * 0.24, rect.height() * 0.24,
                           -rect.width() * 0.24, -rect.height() * 0.24)
    pen = QtGui.QPen(TEXT, max(1.4, rect.width() * 0.09))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(inset.topLeft(), inset.bottomRight())
    p.drawLine(inset.topRight(), inset.bottomLeft())


def _reason_font(base: QtGui.QFont) -> QtGui.QFont:
    """The font an errored section's reason is drawn in — the section header's size,
    since a failure sentence is secondary text rather than a usage figure. Shared by
    `Flyout._layout` (which measures it) and `Flyout.paintEvent` (which draws it), so
    the wrap the first one computes is the wrap the second one gets."""
    font = QtGui.QFont(base)
    font.setPointSize(10)
    return font


def _truncate_reason(text: str) -> str:
    """`text` cut to `REASON_MAX_CHARS` on a word boundary, with an ellipsis.

    The only reason text with no natural bound is `…error.unavailable`, whose `{detail}`
    is an exception repr; every catalogue sentence is far shorter than this and passes
    through untouched.
    """
    if len(text) <= REASON_MAX_CHARS:
        return text
    cut = text[:REASON_MAX_CHARS].rsplit(" ", 1)[0] or text[:REASON_MAX_CHARS]
    return f"{cut}…"


def _reason_tokens(text: str) -> list[str]:
    """`text` split into the units `_wrap_reason` may break between — whitespace, except
    that a `_REASON_NO_LEAD` token is glued to the word before it."""
    tokens: list[str] = []
    for word in text.split():
        if tokens and word in _REASON_NO_LEAD:
            tokens[-1] = f"{tokens[-1]} {word}"
        else:
            tokens.append(word)
    return tokens


def _wrap_reason(text: str, metrics: QtGui.QFontMetrics, width: float) -> list[str]:
    """`text` wrapped to `width`, over as many lines as it takes.

    Deliberately not capped at a line count — see `REASON_MAX_CHARS` for why that was
    the wrong knob. An errored section has no bars competing for the space, so letting
    it be as tall as its sentence costs nothing and cannot clip the remedy.

    Qt would wrap this itself given `Qt.TextWordWrap`, but then `_layout` would have to
    predict how many lines that produced in order to size the section, and a wrong
    guess clips the last line through the middle of its glyphs. Wrapping here keeps
    sizing and drawing working from one identical list of lines — the same reason
    `_row_layout` exists for the rows above.
    """
    tokens = _reason_tokens(_truncate_reason(text))
    if not tokens:
        return [""]
    lines = [tokens[0]]
    for token in tokens[1:]:
        candidate = f"{lines[-1]} {token}"
        if metrics.horizontalAdvance(candidate) <= width:
            lines[-1] = candidate
        else:
            lines.append(token)
    # A single token wider than the card (a URL, a long Windows path inside a
    # `{detail}`) cannot be broken by word wrapping — elide those, and only those, so
    # an ordinary sentence is never touched.
    return [
        line if metrics.horizontalAdvance(line) <= width
        else metrics.elidedText(line, Qt.ElideRight, int(width))
        for line in lines
    ]


def _row_layout(rows: list[UsageRow]) -> list[tuple[UsageRow, float, float]]:
    """(row, y-offset from the section's rows-start, block height) per row.

    This is the one place row spacing lives — `_rows_height` and `Flyout.paintEvent`
    both read from it, so sizing and drawing can never drift apart the way two
    hand-duplicated formulas eventually would.
    """
    out: list[tuple[UsageRow, float, float]] = []
    y = 0.0
    prev = None
    for row in rows:
        if prev == "limit" and row.kind == "credits":
            y += 8
        prev = row.kind
        # Info rows draw no bar, so they're shorter than a row with a track+fill.
        block_h = 22 + 6 if row.kind == "info" else 22 + 6 + 16
        out.append((row, y, block_h))
        y += block_h
    return out


def _draw_rows(p: QtGui.QPainter, base: QtGui.QFont, x: float, w: float,
               top: float, rows: list[UsageRow]) -> None:
    """Paint one block of rows starting at `top`, positioned by `_row_layout`.

    Shared by the official block and the local-estimate block so the two are drawn by
    the same code — the estimate is not a lesser kind of row, it is the same row with
    `kind="info"` (no bar) and a label that says "Est.".
    """
    f = QtGui.QFont(base)
    for row, y_off, _block_h in _row_layout(rows):
        ry = top + y_off

        f.setPointSize(11)
        p.setFont(f)

        # Worded here, on every repaint, rather than baked into the row when it
        # was fetched — see `UsageRow.reset_at`. A pre-worded countdown was
        # already up to one poll interval (5 min) behind by the time it was
        # drawn, and a cached row's stayed frozen for as long as the cache
        # stood in for a failing poll.
        right = fmt.reset_row_text(row.reset_at, row.reset_style) if row.reset_at else row.right
        if row.show_pct:
            right = f"{right}   {row.pct:.0f}%" if right else f"{row.pct:.0f}%"

        # The right-hand text is measured first; the label gets what's left, and
        # is elided if it doesn't fit. Both used to be drawn into the same
        # full-width rect — one left-aligned, one right-aligned — which only
        # looks right while the two happen to be short enough not to meet. They
        # stopped being short enough as soon as the labels were translated:
        # "5-часовой лимит" and its reset time overprinted each other mid-row.
        # The right half is also drawn a point smaller (it is the secondary half,
        # like the section header): the width that buys back is what keeps a
        # translated label from being elided in the first place.
        right_font = QtGui.QFont(f)
        right_font.setPointSize(10)
        right_metrics = QtGui.QFontMetrics(right_font)
        right_w = right_metrics.horizontalAdvance(right) if right else 0.0

        metrics = QtGui.QFontMetrics(f)
        label_w = max(24.0, w - right_w - (10 if right else 0))
        p.setPen(TEXT)
        p.drawText(
            QRectF(x, ry, label_w, 18), Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(row.label, Qt.ElideRight, int(label_w)),
        )
        p.setFont(right_font)
        p.setPen(SUBTLE)
        p.drawText(QRectF(x, ry, w, 18), Qt.AlignRight | Qt.AlignVCenter, right)

        # Informational rows (Codex token totals, Claude's local estimate) have
        # no percentage to show. Drawing an empty track for them reads as "0% of
        # your limit", which is a different and wrong claim — so skip the bar.
        if row.kind == "info":
            continue

        bar_y = ry + 22 + 6
        track = QtGui.QPainterPath()
        track.addRoundedRect(QRectF(x, bar_y, w, 6), 3, 3)
        p.fillPath(track, TRACK)
        fw = max(0.0, min(1.0, row.pct / 100.0)) * w
        if fw > 0:
            fill = QtGui.QPainterPath()
            fill.addRoundedRect(QRectF(x, bar_y, fw, 6), 3, 3)
            p.fillPath(fill, _severity_color(row.severity))


def _rows_height(rows: list[UsageRow]) -> float:
    layout = _row_layout(rows)
    return layout[-1][1] + layout[-1][2] if layout else 0.0


@dataclass
class _SectionLayout:
    """One agent's on-screen geometry for this paint — shared by sizing, drawing
    and mouse hit-testing so all three agree on where a header/body actually is."""

    key: str
    result: UsageResult
    header_rect: QRectF  # the clickable/hoverable badge+title band
    rows_top: float  # y where this section's body starts (the error line, if any)
    collapsible: bool  # False for a section with no body to hide
    collapsed: bool
    height: float  # total section height, header included
    #: The failure sentence, pre-wrapped by `_wrap_reason`; empty when the provider
    #: reported no error. Carried here rather than re-wrapped in `paintEvent` because
    #: `height` above was computed from exactly these lines. A section can now have
    #: both this and rows: an expired login still shows its local estimate underneath.
    reason_lines: list[str] = field(default_factory=list)
    #: y for `result.rows` and for `result.estimate`. Computed here rather than
    #: re-derived while painting, for the same reason `_row_layout` is the single
    #: source of row spacing — two hand-rolled copies of "reason lines, then rows, then
    #: a gap, then the estimate" drift, and the one that drifts is the one that isn't
    #: also deciding the section's height. Meaningless when the matching list is empty
    #: or the section is collapsed.
    rows_at: float = 0.0
    estimate_at: float = 0.0


# --------------------------------------------------------------------------- provider badges

#: One accent colour per provider, used only as a colour swatch — not sampled from any
#: official brand guideline. Deliberately not the real Claude/OpenAI/Cursor/JetBrains/
#: GitHub marks: those are trademarked artwork this project has no license to bundle,
#: so each badge below is drawn from scratch (an original glyph on a flat accent tile),
#: the same "generated, not bundled" approach `icons.py` already uses for TintaView's
#: own tray mark.
_BADGE_COLORS = {
    "claude": QtGui.QColor("#D2795A"),
    "codex": QtGui.QColor("#3FBF9F"),
    "cursor": QtGui.QColor("#4FA6E8"),
    "jetbrains": QtGui.QColor("#9B59F6"),
    "copilot": QtGui.QColor("#6E5BFF"),
}
_BADGE_DEFAULT_COLOR = QtGui.QColor("#7a7a82")
_BADGE_GLYPH = QtGui.QColor("#f2f2f5")

#: Keyed by (agent key, pixel size) — badges are re-requested on every repaint (the
#: flyout has no dirty-region tracking), so this avoids re-running QPainter for a glyph
#: that never changes once drawn.
_badge_cache: dict[tuple[str, int], QtGui.QPixmap] = {}


def _draw_claude_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A simple four-point sparkle — an original shape, not Anthropic's asterisk mark.
    cx, cy = r.center().x(), r.center().y()
    long, short = r.width() * 0.42, r.width() * 0.12
    for angle in (0, 90, 180, 270):
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        diamond = QtGui.QPolygonF([
            QtCore.QPointF(0, -long), QtCore.QPointF(short, 0),
            QtCore.QPointF(0, long * 0.35), QtCore.QPointF(-short, 0),
        ])
        p.drawPolygon(diamond)
        p.restore()


def _draw_codex_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A plain hexagon outline — an original shape, not OpenAI's mark.
    cx, cy, rad = r.center().x(), r.center().y(), r.width() * 0.34
    hexagon = QtGui.QPolygonF(
        [QtCore.QPointF(cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a)))
         for a in range(-90, 271, 60)]
    )
    pen = QtGui.QPen(_BADGE_GLYPH, max(1.0, r.width() * 0.09))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPolygon(hexagon)


def _draw_cursor_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A generic mouse-pointer arrow — literally what "cursor" means, not the product's
    # actual wordmark/icon.
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    arrow = QtGui.QPolygonF([
        QtCore.QPointF(x + w * 0.30, y + h * 0.18), QtCore.QPointF(x + w * 0.30, y + h * 0.82),
        QtCore.QPointF(x + w * 0.48, y + h * 0.64), QtCore.QPointF(x + w * 0.60, y + h * 0.86),
        QtCore.QPointF(x + w * 0.70, y + h * 0.80), QtCore.QPointF(x + w * 0.58, y + h * 0.58),
        QtCore.QPointF(x + w * 0.78, y + h * 0.52),
    ])
    p.drawPolygon(arrow)


def _draw_jetbrains_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A 2x2 grid of dots — a generic "IDE suite" motif, not JetBrains' ring mark.
    cx, cy, gap, rad = r.center().x(), r.center().y(), r.width() * 0.22, r.width() * 0.11
    for dx in (-gap, gap):
        for dy in (-gap, gap):
            p.drawEllipse(QtCore.QPointF(cx + dx, cy + dy), rad, rad)


def _draw_copilot_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # Two linked circles ("goggles") — an original abstraction, not the Copilot mark.
    cx, cy = r.center().x(), r.center().y()
    rad, dx = r.width() * 0.19, r.width() * 0.22
    pen = QtGui.QPen(_BADGE_GLYPH, max(1.0, r.width() * 0.1))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QtCore.QPointF(cx - dx * 0.4, cy), QtCore.QPointF(cx + dx * 0.4, cy))
    p.drawEllipse(QtCore.QPointF(cx - dx, cy), rad, rad)
    p.drawEllipse(QtCore.QPointF(cx + dx, cy), rad, rad)


#: One glyph painter per known provider key; anything else gets no glyph (plain tile).
_BADGE_GLYPHS = {
    "claude": _draw_claude_glyph,
    "codex": _draw_codex_glyph,
    "cursor": _draw_cursor_glyph,
    "jetbrains": _draw_jetbrains_glyph,
    "copilot": _draw_copilot_glyph,
}


def _provider_badge(key: str, size: int) -> QtGui.QPixmap:
    """A small rounded-square tile in the provider's accent colour with an original
    (non-trademarked) glyph — sized to match the section header text it sits beside."""
    cached = _badge_cache.get((key, size))
    if cached is not None:
        return cached

    pix = QtGui.QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    rect = QRectF(0, 0, size, size)
    path = QtGui.QPainterPath()
    path.addRoundedRect(rect, size * 0.28, size * 0.28)
    p.fillPath(path, _BADGE_COLORS.get(key, _BADGE_DEFAULT_COLOR))

    glyph = _BADGE_GLYPHS.get(key)
    if glyph is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(_BADGE_GLYPH)
        inset = rect.adjusted(size * 0.16, size * 0.16, -size * 0.16, -size * 0.16)
        glyph(p, inset)
    p.end()

    _badge_cache[(key, size)] = pix
    return pix


def _display_name(key: str) -> str:
    """Best-effort human name for an agent key ("claude" -> "Claude Code").

    Delegates to `agents_base.display_name`, which is the single place a key becomes a
    label for every surface (tray tooltip, settings dialog, these section headers) —
    this used to carry its own adapter lookup plus a private copy of the stats-only
    provider names, which is exactly how a name drifts between two windows. Still
    wrapped in try/except: a cosmetic lookup must never crash the flyout's paint.
    """
    try:
        from tintaview.agents.base import display_name as agent_display_name

        return agent_display_name(key)
    except Exception:
        return key.replace("_", " ").title() or key


class Flyout(QtWidgets.QWidget):
    """Frameless dark card that paints one usage section per agent."""

    def __init__(
        self,
        collapsed: Iterable[str] | None = None,
        on_toggle: Callable[[str, bool], None] | None = None,
        cfg: Config | None = None,
        on_settings: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)  # needed to get mouseMoveEvent without a button held
        self._cfg = cfg or Config()
        self._on_settings = on_settings
        self._results: dict[str, UsageResult] = {}
        self._status: dict[str, str] = {}  # agent key -> effective session status
        self._tools: dict[str, str] = {}  # agent key -> name of the tool it's running
        self._collapsed: set[str] = set(collapsed or ())
        self._on_toggle = on_toggle
        self.hidden_at = 0.0
        self._anchor: QtCore.QPoint | None = None
        # Repaints the clocks band on each minute boundary, and only while the card is
        # on screen (`_sync_clock_timer`) — the flyout is a transient popup, so a clock
        # nobody is looking at costs nothing.
        self._clock_timer = QtCore.QTimer(self)
        self._clock_timer.setSingleShot(True)
        self._clock_timer.timeout.connect(self._on_clock_tick)
        self.resize(CARD_W, 140)

    def event(self, e: QtCore.QEvent) -> bool:
        # Dismiss when the card loses focus (click elsewhere, alt-tab, …) — without
        # this the flyout stays pinned open until the next explicit toggle.
        if e.type() == QtCore.QEvent.WindowDeactivate:
            self.hide()
        return super().event(e)

    def showEvent(self, e: QtGui.QShowEvent) -> None:
        # A card shown after a long spell hidden would otherwise open on whatever minute
        # was current when it was last painted, so the times are recomputed here rather
        # than waiting for the first tick.
        self.update()
        self._sync_clock_timer()
        super().showEvent(e)

    def hideEvent(self, e: QtGui.QHideEvent) -> None:
        # Recorded so the tray can tell "this click just closed the flyout via
        # focus-out" apart from "this click should open it" — see tray.py's
        # CLICK_REOPEN_GUARD_S.
        self.hidden_at = time.monotonic()
        self._clock_timer.stop()
        super().hideEvent(e)

    # --- positioning -------------------------------------------------------------

    def _clamped_position(self, anchor: QtCore.QPoint) -> QtCore.QPoint:
        screen = (
            QtGui.QGuiApplication.screenAt(anchor) or QtGui.QGuiApplication.primaryScreen()
        )
        area = screen.availableGeometry()
        x = min(anchor.x(), area.right() - self.width() - 8)
        y = anchor.y() - self.height() - 12  # above the cursor (tray is usually bottom)
        if y < area.top():
            y = anchor.y() + 12
        x = max(area.left() + 8, x)
        return QtCore.QPoint(x, y)

    def show_near(self, anchor: QtCore.QPoint) -> None:
        """Position the card near `anchor` (the cursor, at the moment the tray icon was
        clicked) and show it. `anchor` is remembered so a later resize — collapsing a
        section, or a usage refresh that changes a section's height — can re-clamp to
        the screen immediately (see `_resize_to_content`) instead of only being correct
        the next time the flyout happens to be reopened."""
        self._anchor = anchor
        # No adjustSize() here: `_resize_to_content` pins the card with `setFixedSize`, so
        # the size hint is already the fixed size and adjustSize is a no-op that only looks
        # like it is doing something.
        self.move(self._clamped_position(anchor))
        self.show()
        self.raise_()
        self.activateWindow()

    # --- data ------------------------------------------------------------------

    def set_results(self, results: dict[str, UsageResult]) -> None:
        """`results`: dict[str, UsageResult] keyed by agent key, in display order."""
        self._results = dict(results or {})
        self._resize_to_content()
        # Also the path a settings change arrives on: `TrayApp._apply_settings` mirrors
        # `ui.clocks` into the live config (this widget holds that same object) and then
        # calls this, so turning the band on or off has to re-arm the tick here.
        self._sync_clock_timer()
        self.update()

    # --- clocks ------------------------------------------------------------------

    def _clocks(self) -> list[ClockConfig]:
        """The clocks to draw, in order — empty when the band is switched off.

        `MAX_CLOCKS` is applied here as well as by `core.config._clocks`, because a
        `Config` built in code (tests, the demo) never goes through the loader.
        """
        clocks = self._cfg.ui.clocks
        if not clocks.enabled:
            return []
        return [c for c in clocks.clocks[:MAX_CLOCKS] if _zone_valid(c.zone)]

    def _content_top(self) -> float:
        """y of the first agent section: `CONTENT_TOP`, plus the clocks band if shown."""
        if not self._clocks():
            return float(CONTENT_TOP)
        return CONTENT_TOP + _clock_band_height(self.font())

    def _sync_clock_timer(self) -> None:
        if self.isVisible() and self._clocks():
            self._clock_timer.start(_msecs_to_next_minute())
        else:
            self._clock_timer.stop()

    def _on_clock_tick(self) -> None:
        self.update()
        self._sync_clock_timer()

    def set_status(self, status: dict[str, str], tools: dict[str, str] | None = None) -> None:
        """`status`: dict[str, str] of agent key -> effective session status, from
        `StateStore.snapshot()`'s `agents[key]["effective"]` (or absent entirely for
        an agent with no open session). Drives each section's status dot — no resize
        needed, since the dot doesn't change a section's height.

        `tools`: the matching `agents[key]["tool"]`, the name of what that agent is
        running right now ("Bash", "Edit"), "" when it isn't running anything nameable.
        Drawn beside the title, so it needs no resize either.
        """
        new_status = dict(status or {})
        new_tools = dict(tools or {})
        # Early return, because the tray calls this on every 1.5 s state poll and the maps
        # are identical between polls almost all of the time. `update()` schedules a full
        # repaint of the card — every section, every row, every elided label — so calling
        # it unconditionally repainted a card that had not changed ~40 times a minute.
        if new_status == self._status and new_tools == self._tools:
            return
        self._status = new_status
        self._tools = new_tools
        self.update()

    def _status_dot_color(self, agent: str) -> QtGui.QColor | None:
        key = _STATUS_DOT_KEYS.get(self._status.get(agent, "none"))
        return QtGui.QColor(*self._cfg.colors.rgb(key)) if key else None

    # --- layout ------------------------------------------------------------------

    def _layout(self) -> tuple[list[_SectionLayout], float]:
        """Single layout pass for the current results, shared by sizing, painting
        and mouse hit-testing (see `_SectionLayout`). Cheap enough to redo on every
        paint/mouse-move: a handful of agents, no QPainter involved — only the
        `QFontMetrics` an errored section's wrapped reason has to be measured with."""
        x, w = float(PAD), float(CARD_W - 2 * PAD)
        y = self._content_top()
        reason_metrics = QtGui.QFontMetrics(_reason_font(self.font()))
        sections: list[_SectionLayout] = []
        for i, result in enumerate(self._results.values()):
            if i:
                y += SECTION_GAP
            header_rect = QRectF(x, y, w, HEADER_H)
            # The estimate counts as body: a Codex account on API-key auth has no
            # official percentages at all and is nothing *but* an estimate block, and
            # an expired Claude login now has an error line with one underneath. Both
            # are sections worth collapsing, and neither had rows to make them
            # collapsible before.
            has_body = bool(result.rows or result.estimate)
            collapsible = has_body
            collapsed = collapsible and result.agent in self._collapsed
            rows_top = y + HEADER_H
            reason_lines: list[str] = []
            if result.error:
                # `result.error` is already localised by the provider that built it (or
                # quotes an API's own text verbatim); `flyout.no_usage_data` is only the
                # nothing-at-all case — a provider that returned neither rows, nor an
                # estimate, nor a reason.
                reason_lines = _wrap_reason(result.error, reason_metrics, w)
            elif result.notice and not collapsed:
                # Not a failure — cached rows old enough that their age is part of what
                # they say. Same muted slot as a reason, because it answers the same
                # question ("why don't these look right?") before the rows are read.
                #
                # Unlike a reason it does NOT survive collapsing. A notice qualifies the
                # rows underneath it; with those hidden it qualifies nothing, and a
                # collapsed section that still trails a line of text just looks broken
                # next to its collapsed neighbours. An `error` is the opposite case —
                # there are no trustworthy rows at all and it tells the user what to do
                # about that — so it stays visible either way.
                reason_lines = _wrap_reason(result.notice, reason_metrics, w)
            elif not has_body:
                reason_lines = _wrap_reason(t("flyout.no_usage_data"), reason_metrics, w)

            # A failure reason survives collapsing; rows, the estimate and a staleness
            # notice don't. Hiding an agent's numbers is what the chevron is for, but
            # hiding "sign in again" behind it would let the one message the user has to
            # act on disappear into a section that then looks merely empty. A notice is
            # not that: it only exists to qualify the rows below it.
            body_h = len(reason_lines) * REASON_LINE_H
            rows_at = estimate_at = rows_top + body_h
            if not collapsed:
                if result.rows:
                    estimate_at += _rows_height(result.rows)
                if result.estimate:
                    if estimate_at > rows_top:  # something is above it
                        estimate_at += ESTIMATE_GAP
                    body_h = estimate_at - rows_top + _rows_height(result.estimate)
                else:
                    body_h = estimate_at - rows_top
            height = HEADER_H + body_h
            sections.append(_SectionLayout(result.agent, result, header_rect, rows_top,
                                            collapsible, collapsed, height, reason_lines,
                                            rows_at, estimate_at))
            y += height
        return sections, y

    def _resize_to_content(self) -> None:
        if not self._results:
            h = self._content_top() + 20 + 8 + PAD  # "no agents enabled" message
        else:
            _sections, y = self._layout()
            h = y + PAD
        self.setFixedSize(CARD_W, max(80, int(h)))
        if self._anchor is not None and self.isVisible():
            self.move(self._clamped_position(self._anchor))

    def _top_bar_rects(self) -> tuple[QRectF, QRectF]:
        """(settings hit-box, close hit-box) — shared by paint and mouse handling,
        same reasoning as `_SectionLayout`. Independent of `_results`/`_layout()`:
        the title bar is always in the same place regardless of section content."""
        row_y = (TOP_BAR_H - TOP_BAR_BTN) / 2
        close = QRectF(self.width() - PAD - TOP_BAR_BTN, row_y, TOP_BAR_BTN, TOP_BAR_BTN)
        gear = QRectF(close.x() - TOP_BAR_BTN - TOP_BAR_BTN_GAP, row_y, TOP_BAR_BTN, TOP_BAR_BTN)
        return gear, close

    def _toggle(self, key: str) -> None:
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._resize_to_content()
        self.update()
        if self._on_toggle is not None:
            self._on_toggle(key, key in self._collapsed)

    # --- mouse -------------------------------------------------------------------

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        pos = event.position()
        gear_rect, close_rect = self._top_bar_rects()
        hovering = gear_rect.contains(pos) or close_rect.contains(pos)
        if not hovering:
            sections, _ = self._layout()
            hovering = any(s.collapsible and s.header_rect.contains(pos) for s in sections)
        self.setCursor(Qt.PointingHandCursor if hovering else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position()
            gear_rect, close_rect = self._top_bar_rects()
            if close_rect.contains(pos):
                self.hide()
                return
            if gear_rect.contains(pos):
                # Hide first: settings either opens a modal wizard or spawns a
                # console, and leaving the card pinned open behind either looks wrong.
                self.hide()
                if self._on_settings is not None:
                    self._on_settings()
                return
            sections, _ = self._layout()
            for s in sections:
                if s.collapsible and s.header_rect.contains(pos):
                    self._toggle(s.key)
                    return
        super().mousePressEvent(event)

    # --- paint ------------------------------------------------------------------

    def _paint_clocks(self, p: QtGui.QPainter, x: float, w: float) -> None:
        """The clocks band: up to four equal columns, each a big time over its zone id.

        Drawn even when no agent is enabled — it is chrome hanging off the title bar,
        not a section, which is also why it sits at a fixed y and never collapses: a
        band that moved as sections opened and closed would be hard to read at a glance,
        and glanceability is the whole point of it.
        """
        clocks = self._clocks()
        if not clocks:
            return
        preferred_time, preferred_zone = _clock_fonts(self.font())
        band = QRectF(x, TOP_BAR_H, w, _clock_band_height(self.font()))
        col_w = band.width() / len(clocks)
        fmt = self._cfg.ui.clocks.format
        times = [_clock_time(clock.zone, fmt) for clock in clocks]
        time_font = _fit_time_font(self.font(), times, col_w - 2 * CLOCK_TIME_INSET)
        labels, zone_font = _clock_labels(clocks, self.font(), col_w - 2 * CLOCK_LABEL_INSET)
        # Both rows sit in rects sized for the *preferred* fonts — what
        # `_clock_band_height` reserved — so text fitted smaller stays centred in the
        # band instead of riding up against the title bar, and the band's height never
        # depends on how wide the current times happen to be.
        time_h = QtGui.QFontMetricsF(preferred_time).height()
        zone_h = QtGui.QFontMetricsF(preferred_zone).height()
        for i, (text, label) in enumerate(zip(times, labels, strict=True)):
            left = band.x() + i * col_w
            if i:
                # Short of the band's full height, so the divider reads as separating
                # two readings rather than as a rule boxing the band in.
                p.setPen(QtGui.QPen(CLOCK_SEP))
                p.drawLine(QtCore.QPointF(left, band.y() + CLOCK_BAND_PAD),
                           QtCore.QPointF(left, band.bottom() - CLOCK_BAND_PAD))
            time_rect = QRectF(left, band.y() + CLOCK_BAND_PAD, col_w, time_h)
            p.setFont(time_font)
            p.setPen(TEXT)
            p.drawText(time_rect, Qt.AlignHCenter | Qt.AlignVCenter, text)
            label_rect = QRectF(left, time_rect.bottom() + CLOCK_LINE_GAP, col_w, zone_h)
            p.setFont(zone_font)
            p.setPen(SUBTLE)
            p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignVCenter, label)

        # Closed off underneath with the same rule the title bar sits on, so the band
        # reads as its own strip of chrome rather than as a heading for the first agent.
        # Only when there are clocks: with the band off, the title bar's own rule is the
        # single divider above the sections and a second one would box in nothing.
        p.setPen(QtGui.QPen(BORDER))
        p.drawLine(QtCore.QPointF(band.x(), band.bottom()),
                   QtCore.QPointF(band.right(), band.bottom()))

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, CARD_BG)
        p.setPen(QtGui.QPen(BORDER))
        p.drawPath(path)

        x, w = float(PAD), float(self.width() - 2 * PAD)
        f = p.font()

        # --- title bar: logo, "TintaView", settings, close --------------------
        logo_size = 24.0
        logo_y = (TOP_BAR_H - logo_size) / 2
        # A HiDPI screen's QIcon.pixmap() comes back bigger, in physical pixels, than
        # the (logical) size requested — a hardcoded QRectF(0, 0, logo_size, logo_size)
        # source rect then only samples that pixmap's top-left corner instead of the
        # whole mark. Use its actual .rect() so any device pixel ratio still maps the
        # entire pixmap onto the target square.
        logo_pixmap = icons.brand_icon(int(logo_size)).pixmap(int(logo_size), int(logo_size))
        p.drawPixmap(QRectF(x, logo_y, logo_size, logo_size), logo_pixmap, QRectF(logo_pixmap.rect()))

        gear_rect, close_rect = self._top_bar_rects()
        title_font = QtGui.QFont(f)
        title_font.setPointSize(13)
        p.setFont(title_font)
        p.setPen(TEXT)
        title_x = x + logo_size + 8
        p.drawText(
            QRectF(title_x, 0, gear_rect.x() - 8 - title_x, TOP_BAR_H),
            Qt.AlignLeft | Qt.AlignVCenter, "TintaView",
        )
        _draw_gear_icon(p, gear_rect)
        _draw_close_icon(p, close_rect)

        p.setPen(QtGui.QPen(BORDER))
        p.drawLine(QtCore.QPointF(x, TOP_BAR_H), QtCore.QPointF(x + w, TOP_BAR_H))

        self._paint_clocks(p, x, w)

        if not self._results:
            top = self._content_top()
            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            p.drawText(
                QRectF(x, top, w, self.height() - top - PAD),
                Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                t("flyout.no_agents"),
            )
            p.end()
            return

        sections, _ = self._layout()
        for section in sections:
            result = section.result
            header = section.header_rect

            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            # Badge sized to the header text's own line height, per its ask — not a
            # fixed pixel constant — so it tracks if the header font ever changes.
            badge_size = QtGui.QFontMetrics(f).height()
            badge_gap = 6
            p.drawPixmap(
                QRectF(header.x(), header.y() + (20 - badge_size) / 2, badge_size, badge_size),
                _provider_badge(result.agent, badge_size),
                QRectF(0, 0, badge_size, badge_size),
            )
            # The section header is the agent's display name ("Claude Code"), not
            # the provider's own `header` string ("Your usage limits · Max") — with
            # several agents on screen at once, identifying *which* agent a section
            # belongs to matters more than the tier blurb the old single-agent
            # flyout led with.
            name_w = header.width() - badge_size - badge_gap - (CHEVRON_W if section.collapsible else 0)
            name = _display_name(result.agent)
            name_x = header.x() + badge_size + badge_gap
            p.drawText(QRectF(name_x, header.y(), name_w, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, name)

            # Session status dot: green/idle, yellow/working, red/confirm, or no dot
            # at all when this agent has no session open right now (see
            # `_status_dot_color`) — the same colours the tray icon and hardware
            # lighting use, read from `cfg.colors` rather than hardcoded here.
            dot_color = self._status_dot_color(result.agent)
            tool = self._tools.get(result.agent, "")
            after_name_x = name_x + QtGui.QFontMetrics(f).horizontalAdvance(name)
            if dot_color is not None:
                dot_r = 3.5
                dot_gap = 12
                p.setPen(Qt.NoPen)
                p.setBrush(dot_color)
                p.drawEllipse(QtCore.QPointF(after_name_x + dot_gap, header.center().y()), dot_r, dot_r)
                after_name_x += dot_gap + dot_r

            if tool:
                # What the agent is busy *with*, after the status dot: the dot says it
                # is working, this says on what. Elided into whatever the title and dot
                # left, so a long tool name can never reach the chevron or spill out of
                # the card — the section's height is fixed by this point.
                tool_x = after_name_x + 8
                tool_w = (header.x() + header.width()
                          - (CHEVRON_W if section.collapsible else 0) - tool_x)
                if tool_w > 16:
                    p.setPen(FAINT)
                    p.drawText(
                        QRectF(tool_x, header.y(), tool_w, 20),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        QtGui.QFontMetrics(f).elidedText(tool, Qt.ElideRight, int(tool_w)),
                    )
                    p.setPen(SUBTLE)

            if section.collapsible:
                _draw_chevron(p, header, section.collapsed)

            if section.reason_lines:
                p.setPen(SUBTLE)
                # Wrapped and elided by `_layout`, which sized this section for exactly
                # these lines. `f` is already at the reason's point size here (the
                # header above set it), i.e. what `_reason_font` measured with.
                for line_no, line in enumerate(section.reason_lines):
                    p.drawText(
                        QRectF(x, section.rows_top + line_no * REASON_LINE_H, w, REASON_LINE_H),
                        Qt.AlignLeft | Qt.AlignVCenter, line,
                    )

            if section.collapsed:
                continue

            if result.rows:
                _draw_rows(p, f, x, w, section.rows_at, result.rows)
            # Always under the official rows, never in place of them — and drawn even
            # when the block above is an error sentence, which is the case this whole
            # arrangement exists for: the endpoint is unreachable, and the local
            # token/cost numbers are still perfectly good.
            if result.estimate:
                _draw_rows(p, f, x, w, section.estimate_at, result.estimate)

        p.end()
