import json
import os
import re
import sys
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

TARGET_URL = os.getenv("TARGET_URL", "http://103.203.93.2/video-details/1744915")

# সাধারণ নন-ডিআরএম ভিডিও ও স্ট্রিমিং এক্সটেনশন/প্যাটার্ন
VIDEO_EXTENSIONS_RE = re.compile(
    r"\.(mp4|mkv|m3u8|mpd|webm|flv|avi|mov|ts)(\?.*)?$",
    re.IGNORECASE,
)

# অনেক সময় প্লেয়ার লিংকে এক্সটেনশন না থাকলেও এই কিওয়ার্ডগুলো থাকে
STREAM_HINTS_RE = re.compile(
    r"(playlist\.m3u8|master\.m3u8|\.m3u8|\.mp4|\.mkv|video|stream)",
    re.IGNORECASE,
)

def is_likely_video_url(url: str, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if any(k in ct for k in ["video/", "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"]):
        return True
    if VIDEO_EXTENSIONS_RE.search(url):
        return True
    return False

def capture_video_links(url: str) -> list[str]:
    found_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = context.new_page()

        def handle_request(route_or_req):
            req_url = route_or_req.url
            if VIDEO_EXTENSIONS_RE.search(req_url):
                found_urls.add(req_url)

        def handle_response(response):
            resp_url = response.url
            headers = response.headers or {}
            content_type = headers.get("content-type", "")

            if is_likely_video_url(resp_url, content_type):
                found_urls.add(resp_url)

            # JSON বা টেক্সট রেসপন্সের ভেতরে লুকানো লিংক খোঁজা
            if any(t in content_type.lower() for t in ["application/json", "text/plain", "text/html"]):
                try:
                    body = response.text()
                    matches = re.findall(
                        r"""https?://[^\s"'<>\\]+\.(?:mp4|mkv|m3u8|mpd|webm|flv|mov)(?:\?[^\s"'<>\\]*)?""",
                        body,
                        re.IGNORECASE,
                    )
                    for m in matches:
                        found_urls.add(m)
                except Exception:
                    pass

        page.on("request", handle_request)
        page.on("response", handle_response)

        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            print(f"[Warning] Page load timeout/issue: {e}", file=sys.stderr)

        # প্লেয়ার লোড বা প্লে বাটন ক্লিক করার চেষ্টা
        try:
            play_selectors = [
                "video",
                "button[aria-label*='play' i]",
                ".vjs-big-play-button",
                ".jw-icon-playback",
                "[class*='play']",
            ]
            for sel in play_selectors:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=2000)
                    break
        except Exception:
            pass

        # ডাইনামিক রিকোয়েস্ট আসার জন্য কিছুক্ষণ অপেক্ষা
        page.wait_for_timeout(6000)

        # DOM-এর <video> এবং <source> ট্যাগ চেক করা
        dom_sources = page.evaluate(
            """() => {
                const results = [];
                document.querySelectorAll('video, source, iframe').forEach(el => {
                    if (el.src) results.push(el.src);
                    if (el.currentSrc) results.push(el.currentSrc);
                    const dataSrc = el.getAttribute('data-src') || el.getAttribute('data-video');
                    if (dataSrc) results.push(dataSrc);
                });
                return results;
            }"""
        )

        for src in dom_sources:
            if src and not src.startswith("blob:"):
                full_url = urljoin(url, src)
                found_urls.add(full_url)

        browser.close()

    return sorted(found_urls)

def main():
    links = capture_video_links(TARGET_URL)
    output = {
        "target_url": TARGET_URL,
        "count": len(links),
        "video_links": links,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))

    with open("captured_links.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
