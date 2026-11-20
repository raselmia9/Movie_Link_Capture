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
CONCURRENCY = int(os.getenv("CONCURRENCY", "60"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "300"))
CHECK_STATUS_200 = os.getenv("CHECK_STATUS_200", "false").lower() == "true"

VIDEO_EXTENSIONS = (
    "mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v", "3gp", "3g2",
    "m3u8", "mpd", "ts", "m2ts", "mts", "m4s", "ism", "isml", "f4m", "f4v",
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


def load_source_movies(url: str) -> list[dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    movie_list = (
        raw.get("movies", [])
        if isinstance(raw, dict)
        else (raw if isinstance(raw, list) else [])
    )
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


def load_existing_output(filepath: Path) -> list[dict[str, Any]]:
    if not filepath.exists():
        return []

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except Exception:
        return []

    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


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
    }
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=10) as resp:
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

    captured_map: dict[str, str] = {}
    extracted_labeled_raw: list[tuple[str, str]] = []

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
            for k in (
                "application/json",
                "text/plain",
                "text/html",
                "application/javascript",
            )
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

    try:
        await page.goto(detail_url, wait_until="networkidle", timeout=45000)
    except Exception as e:
        print(f"[Warning] Page load timeout/issue ({title}): {e}", file=sys.stderr)

    try:
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
                    await loc.click(timeout=2000)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(6000)

        dom_data = await page.evaluate(
            """() => {
                const mediaUrls = [];
                document.querySelectorAll('video, source, iframe').forEach(el => {
                    if (el.src) mediaUrls.push(el.src);
                    if (el.currentSrc) mediaUrls.push(el.currentSrc);
                    const dataSrc = el.getAttribute('data-src') || el.getAttribute('data-video');
                    if (dataSrc) mediaUrls.push(dataSrc);
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
            if m_url and not m_url.startswith("blob:"):
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

    labeled_entries: list[tuple[str, str]] = []
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
    existing_records = load_existing_output(OUTPUT_JSON_FILE)

    # আউটপুট লিস্টে থাকা টাইটেল অনুযায়ী ইন্ডেক্স তৈরি (সম্পূর্ণ টাইটেল মিলিয়ে)
    existing_by_title: dict[str, int] = {}
    for idx, rec in enumerate(existing_records):
        t = str(rec.get("title") or "")
        if t and t not in existing_by_title:
            existing_by_title[t] = idx

    to_capture: list[tuple[dict[str, str], int | None]] = []
    skipped = 0

    for item in source_items:
        title = item["title"]
        if title in existing_by_title:
            rec_idx = existing_by_title[title]
            existing_link = str(existing_records[rec_idx].get("link") or "").strip()
            if existing_link == "":
                to_capture.append((item, rec_idx))
            else:
                skipped += 1
        else:
            to_capture.append((item, None))

    if MAX_ITEMS > 0:
        to_capture = to_capture[:MAX_ITEMS]

    print(
        f"Source: {len(source_items)} | Skipped (has link): {skipped} | Queued: {len(to_capture)}"
    )

    if not to_capture:
        print("No items to process.")
        return

    semaphore = asyncio.Semaphore(CONCURRENCY)
    updated_existing_indices: set[int] = set()
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

        async def worker(index: int, item: dict[str, str], rec_idx: int | None):
            async with semaphore:
                rec = await capture_item(context, item)
                if rec_idx is not None:
                    if rec["link"]:
                        existing_records[rec_idx]["link"] = rec["link"]
                        if rec["details"]:
                            existing_records[rec_idx]["details"] = rec["details"]
                        updated_existing_indices.add(rec_idx)
                else:
                    new_records.append(rec)
                print(
                    f"[{index}/{len(to_capture)}] {rec['title']} -> "
                    f"{'OK' if rec['link'] else 'EMPTY'}"
                )

        await asyncio.gather(
            *(
                asyncio.create_task(worker(i + 1, it, r_idx))
                for i, (it, r_idx) in enumerate(to_capture)
            )
        )
        await browser.close()

    # নতুন ক্যাপচার হওয়া রেকর্ডগুলো লিস্টের একদম শুরুতে রাখা হয়েছে
    final_records = new_records + existing_records
    OUTPUT_JSON_FILE.write_text(
        json.dumps(final_records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Saved {len(final_records)} items to {OUTPUT_JSON_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
