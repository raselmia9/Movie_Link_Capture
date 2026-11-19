import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
import urllib.request
from playwright.async_api import async_playwright

SOURCE_JSON_URL = os.getenv(
    "SOURCE_JSON_URL",
    "https://raw.githubusercontent.com/raselmia9/Movie-Scraper/refs/heads/main/movies.json",
)
OUTPUT_JSON_FILE = Path(os.getenv("OUTPUT_JSON_FILE", "output_movies.json"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "10"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "0"))  # 0 মানে সব নতুন আইটেম
CHECK_STATUS_200 = os.getenv("CHECK_STATUS_200", "true").lower() == "true"

VIDEO_EXTENSIONS = (
    "mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v", "3gp", "3g2",
    "m3u8", "m3u", "mpd", "ts", "m2ts", "mts", "m4s", "ism", "isml", "f4m", "f4v",
    "mpg", "mpeg", "mpe", "mpv", "m2v", "vob", "ogv", "ogg", "rm", "rmvb",
    "asf", "amv", "divx", "xvid", "nsv", "yuv", "svi", "mxf", "roq", "drc", "gifv",
)
EXT_PATTERN = "|".join(VIDEO_EXTENSIONS)

VIDEO_EXT_RE = re.compile(rf"\.(?:{EXT_PATTERN})(?:\?.*)?$", re.IGNORECASE)
URL_EXTRACT_RE = re.compile(
    rf"""https?://[^\s"'<>\\]+\.(?:{EXT_PATTERN})(?:\?[^\s"'<>\\]*)?""",
    re.IGNORECASE,
)

VIDEO_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "application/vnd.ms-sstr+xml",
)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def load_source_movies(url: str) -> list[dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    movie_list = raw.get("movies", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    items: list[dict[str, str]] = []

    for row in movie_list:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        detail_url = str(row.get("detail_url") or row.get("url") or "").strip()
        thumbnail = str(row.get("thumbnail") or row.get("img") or "").strip()
        pub_date = str(row.get("published_date") or row.get("date") or "").strip()
        if title and detail_url:
            items.append({
                "title": title,
                "detail_url": detail_url,
                "thumbnail": thumbnail,
                "published_date": pub_date,
            })

    return items


def load_existing_output(filepath: Path) -> tuple[list[dict[str, Any]], set[str]]:
    if not filepath.exists():
        return [], set()

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except Exception:
        return [], set()

    records = data if isinstance(data, list) else []
    valid_records = [r for r in records if isinstance(r, dict)]
    seen = {
        normalize_title(str(r.get("title") or ""))
        for r in valid_records
        if normalize_title(str(r.get("title") or ""))
    }
    return valid_records, seen


def is_video_response(url: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in VIDEO_CONTENT_TYPES):
        return True
    if VIDEO_EXT_RE.search(url):
        return True
    return False


def check_http_200(url: str, referer: str) -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Range": "bytes=0-1024",
    }
    for method in ("GET", "HEAD"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=12) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                if code == 200:
                    return True
        except Exception:
            continue
    return False


def format_details_text(meta: dict[str, str]) -> str:
    lines = []
    for label in ("Director", "Cast(s)", "Language", "Quality", "Resolution", "Size"):
        val = meta.get(label)
        if val:
            lines.append(f"{label} : {val}")
    return "\n".join(lines)


def format_links_field(entries: list[tuple[str, str]]) -> str:
    # entries: [(label, formatted_url_with_referer), ...]
    if not entries:
        return ""
    if len(entries) == 1 and not entries[0][0]:
        return entries[0][1]

    parts = []
    for idx, (label, url_with_ref) in enumerate(entries, start=1):
        ep_label = label or f"Link {idx}"
        parts.append(f"{ep_label},,{url_with_ref}")
    return ")".join(parts)


async def capture_item(context, item: dict[str, str]) -> dict[str, Any]:
    title = item["title"]
    detail_url = item["detail_url"]
    img_url = item["thumbnail"]
    date_val = item["published_date"]

    # url -> referer
    captured_map: dict[str, str] = {}
    labeled_entries: list[tuple[str, str]] = []

    page = await context.new_page()

    def record_url(raw_url: str, referer: str):
        if not raw_url or raw_url.startswith(("blob:", "javascript:", "#", "data:")):
            return
        full_url = urljoin(referer or detail_url, raw_url.strip())
        ref = (referer or detail_url).strip()
        if full_url not in captured_map:
            captured_map[full_url] = ref

    def on_request(request):
        req_url = request.url
        if VIDEO_EXT_RE.search(req_url):
            ref = request.headers.get("referer") or (
                request.frame.url if request.frame else detail_url
            )
            record_url(req_url, ref)

    async def on_response(response):
        resp_url = response.url
        headers = response.headers or {}
        content_type = headers.get("content-type", "")
        frame_ref = response.frame.url if response.frame else detail_url

        if is_video_response(resp_url, content_type):
            record_url(resp_url, frame_ref)

        if any(
            k in content_type.lower()
            for k in ("application/json", "text/plain", "text/html", "application/javascript")
        ):
            try:
                body = await response.text()
                for match in URL_EXTRACT_RE.findall(body):
                    record_url(match, frame_ref)
            except Exception:
                pass

    page.on("request", on_request)
    page.on("response", on_response)

    page_meta: dict[str, str] = {}
    extracted_labeled_raw: list[tuple[str, str]] = []

    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=35000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        for sel in (
            "video",
            "button[aria-label*='play' i]",
            ".vjs-big-play-button",
            ".jw-icon-playback",
            "[class*='play']",
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1500)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(3000)

        dom_data = await page.evaluate(
            """() => {
                const mediaUrls = [];
                document.querySelectorAll('video, source, iframe, a').forEach(el => {
                    for (const attr of ['src', 'currentSrc', 'data-src', 'data-video', 'href']) {
                        const val = el.getAttribute ? el.getAttribute(attr) : el[attr];
                        if (val) mediaUrls.push(val);
                    }
                });

                const labeled = [];
                document.querySelectorAll('a[href], button[data-href], [data-url]').forEach(el => {
                    const href = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url') || '';
                    const txt = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (href) labeled.push([txt, href]);
                });

                const text = document.body ? document.body.innerText : '';
                return { mediaUrls, labeled, text };
            }"""
        )

        for m_url in dom_data.get("mediaUrls", []):
            if VIDEO_EXT_RE.search(m_url):
                record_url(m_url, page.url or detail_url)

        for lbl, href in dom_data.get("labeled", []):
            if VIDEO_EXT_RE.search(href):
                full_h = urljoin(page.url or detail_url, href.strip())
                record_url(full_h, page.url or detail_url)
                extracted_labeled_raw.append((lbl, full_h))

        body_text = dom_data.get("text") or ""
        label_map = {
            "director": "Director",
            "cast": "Cast(s)",
            "casts": "Cast(s)",
            "cast(s)": "Cast(s)",
            "language": "Language",
            "quality": "Quality",
            "resolution": "Resolution",
            "size": "Size",
        }
        for line in body_text.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                norm_k = k.strip().lower()
                if norm_k in label_map and v.strip():
                    page_meta.setdefault(label_map[norm_k], v.strip())

    except Exception as e:
        print(f"[Error] {title} ({detail_url}): {e}", file=sys.stderr)
    finally:
        await page.close()

    verified_urls: dict[str, str] = {}
    for u, ref in captured_map.items():
        if CHECK_STATUS_200:
            ok = await asyncio.to_thread(check_http_200, u, ref)
            if not ok:
                continue
        verified_urls[u] = f"{u}|Referer={ref}"

    if extracted_labeled_raw:
        for lbl, raw_u in extracted_labeled_raw:
            if raw_u in verified_urls:
                labeled_entries.append((lbl, verified_urls[raw_u]))
        seen_in_labeled = {u for _, u in extracted_labeled_raw}
        for u, formatted in verified_urls.items():
            if u not in seen_in_labeled:
                labeled_entries.append(("", formatted))
    else:
        for _, formatted in verified_urls.items():
            labeled_entries.append(("", formatted))

    return {
        "title": title,
        "details": format_details_text(page_meta),
        "img": img_url,
        "date": date_val,
        "link": format_links_field(labeled_entries),
    }


async def main():
    source_items = await asyncio.to_thread(load_source_movies, SOURCE_JSON_URL)
    existing_records, seen_titles = load_existing_output(OUTPUT_JSON_FILE)

    pending: list[dict[str, str]] = []
    skipped = 0

    for item in source_items:
        norm = normalize_title(item["title"])
        if norm in seen_titles:
            skipped += 1
            continue
        seen_titles.add(norm)
        pending.append(item)

    if MAX_ITEMS > 0:
        pending = pending[:MAX_ITEMS]

    print(
        f"Source: {len(source_items)} | Skipped: {skipped} | Queued: {len(pending)}"
    )

    if not pending:
        print("No new items to process.")
        return

    semaphore = asyncio.Semaphore(CONCURRENCY)
    new_records: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )

        async def worker(index: int, item: dict[str, str]):
            async with semaphore:
                rec = await capture_item(context, item)
                new_records.append(rec)
                print(f"[{index}/{len(pending)}] Done: {rec['title']}")

        await asyncio.gather(
            *(asyncio.create_task(worker(i + 1, it)) for i, it in enumerate(pending))
        )
        await browser.close()

    final_records = existing_records + new_records
    OUTPUT_JSON_FILE.write_text(
        json.dumps(final_records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Saved {len(final_records)} items to {OUTPUT_JSON_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
