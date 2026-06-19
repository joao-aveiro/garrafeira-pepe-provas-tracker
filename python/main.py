#!/usr/bin/env python3
"""Polls Garrafeira Pepe's Amelia booking API for new tasting events.

If a Telegram send fails, the event is NOT marked seen, so the next run
retries it. Defensive parsing: missing/renamed fields degrade gracefully
instead of crashing the run.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

AMELIA_URL = (
    "https://garrafeirapepe.pt/wp-admin/admin-ajax.php"
    "?action=wpamelia_api&call=/events&bookings=false"
)
PROVAS_URL = "https://garrafeirapepe.pt/provas/"
DEFAULT_UA = "GarrafeiraPepeMonitor/1.0 (+personal use)"
USER_AGENT = os.environ.get("MONITOR_USER_AGENT", DEFAULT_UA)
STATE_PATH = Path(__file__).parent / "state.json"
LISBON = ZoneInfo("Europe/Lisbon")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HTTP_TIMEOUT = 30
MAX_PAGES = 100
TELEGRAM_MAX_RETRIES = 3


def http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:200].decode(errors="replace")
        raise RuntimeError(f"Non-JSON response from {url}: {snippet!r}") from exc


def fetch_amelia_events() -> list[dict]:
    events: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload = http_get_json(f"{AMELIA_URL}&page={page}")
        data = payload.get("data") or {}
        page_events = data.get("events") or []
        if not page_events:
            break
        events.extend(page_events)
    return events


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"seen_ids": [], "notified_ids": []}
    try:
        state = json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"seen_ids": [], "notified_ids": []}
    # Back-compat: pre-split state has no notified_ids. Treat everything
    # already seen as already notified so the first run doesn't re-blast the
    # backlog; an explicit edit frees any id that was never really sent.
    if "notified_ids" not in state:
        state["notified_ids"] = list(state.get("seen_ids", []))
    return state


def save_state(state: dict) -> None:
    state["seen_ids"] = sorted(set(state.get("seen_ids", [])))
    state["notified_ids"] = sorted(set(state.get("notified_ids", [])))
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON)
    except (ValueError, TypeError):
        return None


def first_period(event: dict) -> tuple[datetime | None, datetime | None]:
    periods = event.get("periods") or []
    if not periods:
        return (None, None)
    p = periods[0]
    return (parse_dt(p.get("periodStart")), parse_dt(p.get("periodEnd")))


def is_future(event: dict, now: datetime) -> bool:
    for p in event.get("periods") or []:
        start = parse_dt(p.get("periodStart"))
        if start and start >= now:
            return True
    return False


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).strip()
    return re.sub(r"\s+", " ", text)


def extract_wines(description: str) -> list[str]:
    items = re.findall(
        r"<li[^>]*>(.*?)</li>",
        description or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = []
    for raw in items:
        # Cut at first internal paragraph break so trailing prose nested in the
        # last <li> doesn't leak into the wine name.
        cut = re.split(
            r"(?:<br\s*/?>\s*){2,}|</p>\s*<p",
            raw,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        text = _strip_html(cut)
        if text:
            cleaned.append(text)
    return cleaned


def extract_intro(description: str, max_chars: int = 500) -> str:
    paragraphs = re.findall(
        r"<p[^>]*>(.*?)</p>",
        description or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    pieces: list[str] = []
    for raw in paragraphs:
        text = _strip_html(raw)
        if not text:
            continue
        # Skip the logistics block (Local: ..., Data: ..., Vagas: ...) since
        # we already render date and price from API fields.
        if re.search(
            r"\b(Local|Data|Hor[áa]rio|Valor|Vagas|Inscri[çc][ãa]o)\b\s*:",
            text,
            flags=re.IGNORECASE,
        ):
            continue
        pieces.append(text)
        if sum(len(p) for p in pieces) >= 200:
            break
    text = " ".join(pieces)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    return text


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_when(start: datetime | None, end: datetime | None) -> str:
    if not start:
        return "?"
    base = (
        f"{WEEKDAYS[start.weekday()]}, "
        f"{start.strftime('%d/%m/%Y')} {start.strftime('%H:%M')}"
    )
    if end:
        base += f"-{end.strftime('%H:%M')}"
    return base


def format_message(event: dict) -> str:
    name = html_escape(str(event.get("name") or "(untitled)"))
    price = event.get("price")
    start, end = first_period(event)

    lines = [f"🍷 <b>New tasting: {name}</b>", f"📅 {format_when(start, end)}"]
    if price is not None:
        lines.append(f"💶 {price}€")

    description = event.get("description") or ""
    intro = extract_intro(description)
    if intro:
        lines.append("")
        lines.append(html_escape(intro))

    wines = extract_wines(description)
    if wines:
        lines.append("")
        lines.append("<b>Wines:</b>")
        for w in wines:
            lines.append(f"• {html_escape(w)}")

    lines.append("")
    lines.append(f'<a href="{PROVAS_URL}">Book →</a>')
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """Send a Telegram message, honoring HTTP 429 `retry_after` up to TELEGRAM_MAX_RETRIES."""
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    for attempt in range(TELEGRAM_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                body = json.loads(resp.read())
            if not body.get("ok"):
                raise RuntimeError(f"Telegram API rejected: {body}")
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= TELEGRAM_MAX_RETRIES:
                raise
            try:
                err_body = json.loads(exc.read())
                retry_after = float(
                    err_body.get("parameters", {}).get("retry_after", 1)
                )
            except (json.JSONDecodeError, ValueError, TypeError):
                retry_after = 2.0**attempt
            print(
                f"Telegram 429; sleeping {retry_after}s "
                f"before retry {attempt + 1}/{TELEGRAM_MAX_RETRIES}",
                file=sys.stderr,
            )
            time.sleep(retry_after)


def write_summary(
    events: list[dict],
    notified_before: set[int],
    notified: set[int],
    failed: set[int],
    fetch_error: str | None = None,
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    now_str = datetime.now(LISBON).strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"# Run {now_str}", ""]

    if fetch_error:
        lines.append(f"## ❌ Fetch failed\n\n```\n{fetch_error}\n```\n")
        Path(path).write_text("\n".join(lines) + "\n")
        return

    lines.append(f"- Events fetched from API: **{len(events)}**")
    lines.append(
        f"- Already notified: **{len(notified_before & {e.get('id') for e in events})}**"
    )
    lines.append(f"- Notifications sent: **{len(notified)}**")
    if failed:
        lines.append(
            f"- ⚠️ Notifications failed (will retry next run): **{len(failed)}** - ids {sorted(failed)}"
        )
    lines.append("")
    lines.append("| id | date | € | name | status |")
    lines.append("|---:|---|---:|---|---|")

    def sort_key(e):
        start, _ = first_period(e)
        return start or datetime.max.replace(tzinfo=LISBON)

    for e in sorted(events, key=sort_key):
        eid = e.get("id")
        start, _ = first_period(e)
        when = start.strftime("%Y-%m-%d %H:%M") if start else "?"
        if eid in notified:
            tag = "🆕 notified"
        elif eid in failed:
            tag = "⚠️ failed - retry"
        else:
            tag = ""
        name = (e.get("name") or "")[:60].replace("|", "\\|")
        lines.append(f"| {eid} | {when} | {e.get('price', '?')} | {name} | {tag} |")
    Path(path).write_text("\n".join(lines) + "\n")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    try:
        events = fetch_amelia_events()
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        write_summary([], set(), set(), set(), set(), fetch_error=str(exc))
        return 1

    state = load_state()
    seen_before: set[int] = set(state.get("seen_ids", []))
    notified_before: set[int] = set(state.get("notified_ids", []))
    now = datetime.now(LISBON)

    api_ids: set[int] = {e["id"] for e in events if e.get("id") is not None}
    newly_appeared = api_ids - seen_before

    notified: set[int] = set()
    failed: set[int] = set()

    # Notify on what's future-and-not-yet-delivered, re-evaluated every run.
    # We never suppress on mere observation, so a dateless-on-publish or a
    # deleted-and-re-added event fires once it presents a future period.
    for event in events:
        eid = event.get("id")
        if eid in notified_before:
            continue
        if not is_future(event, now):
            continue

        message = format_message(event)
        print(f"--- notify id={eid} ---\n{message}\n")

        if not (token and chat_id):
            print(
                "(no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; dry-run, not marked notified)"
            )
            continue

        try:
            send_telegram(token, chat_id, message)
            notified.add(eid)
        except Exception as exc:
            print(f"Notification failed for id={eid}: {exc!r}", file=sys.stderr)
            failed.add(eid)

    # seen_ids records everything observed (diagnostics only); notified_ids is
    # the suppression set and grows only on a successful send, so failed sends
    # retry on the next run.
    state["seen_ids"] = sorted(seen_before | api_ids)
    state["notified_ids"] = sorted(notified_before | notified)
    state["last_check"] = now.isoformat()
    save_state(state)

    write_summary(events, notified_before, notified, failed)

    print(
        f"Fetched {len(events)} events; "
        f"new={len(newly_appeared)} notified={len(notified)} "
        f"failed={len(failed)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
