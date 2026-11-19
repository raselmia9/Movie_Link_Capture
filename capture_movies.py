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
        if u in seen_links:
            continue
        if CHECK_STATUS_200:
            ok = await asyncio.to_thread(check_http_200, u, ref)
            if not ok:
                continue
        seen_links.add(u)
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
        "source_url": detail_url,
    }


async def main():
    source_items = await asyncio.to_thread(load_source_movies, SOURCE_JSON_URL)
    existing_records, seen_links = load_existing_output(OUTPUT_JSON_FILE)

    pending = source_items[:MAX_ITEMS] if MAX_ITEMS > 0 else source_items
    print(f"Source: {len(source_items)} | Queued: {len(pending)} | Known links: {len(seen_links)}")

    if not pending:
        print("No items to process.")
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
                rec = await capture_item(context, item, seen_links)
                new_records.append(rec)
                print(
                    f"[{index}/{len(pending)}] {rec['title']} -> "
                    f"{'OK' if rec['link'] else 'EMPTY'}"
                )

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
