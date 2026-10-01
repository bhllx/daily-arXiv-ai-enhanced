"""Prepare a live batch; --max-papers 0 selects all eligible IDs."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ID_PATTERN = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")


def base_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Unexpected arXiv ID: {value!r}")
    return re.sub(r"v\d+$", "", value)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def crawl_ids(output):
    # Reuse the existing spider's /new selectors and category filtering.
    # Disable the per-item API pipeline: deduplicate and limit BEFORE metadata requests.
    sys.path.insert(0, str(ROOT / "daily_arxiv"))
    from scrapy import signals
    from scrapy.crawler import CrawlerProcess
    from scrapy.settings import Settings
    from daily_arxiv.spiders.arxiv import ArxivSpider

    settings = Settings()
    settings.setmodule("daily_arxiv.settings")
    settings.setdict({
        "ITEM_PIPELINES": {},
        "FEEDS": {},
        "CONCURRENT_REQUESTS": 1,
        "DOWNLOAD_DELAY": 3,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "RETRY_TIMES": 0,
        "DOWNLOAD_TIMEOUT": 60,
        "LOG_LEVEL": "INFO",
    }, priority="cmdline")
    process = CrawlerProcess(settings)
    crawler = process.create_crawler(ArxivSpider)
    records, errors, responses = [], [], []

    def collect(item, response, spider):
        records.append(dict(item))

    def failed(failure, response=None, spider=None):
        errors.append(failure.getErrorMessage())

    def received(response, request, spider):
        responses.append({"url": response.url, "status": response.status})

    crawler.signals.connect(collect, signal=signals.item_scraped, weak=False)
    crawler.signals.connect(failed, signal=signals.spider_error, weak=False)
    crawler.signals.connect(received, signal=signals.response_received, weak=False)
    deferred = process.crawl(crawler)
    deferred.addErrback(lambda failure: errors.append(failure.getErrorMessage()))
    process.start()
    stats = crawler.stats.get_stats() if crawler.stats else {}
    write_json(output / "crawl-diagnostics.json", {
        "stats": json.loads(json.dumps(stats, default=str)),
        "responses": responses, "errors": errors,
    })
    categories = [part.strip() for part in os.environ["CATEGORIES"].split(",")]
    expected_paths = {f"/list/{category}/new" for category in categories}
    from urllib.parse import urlsplit
    successful = {urlsplit(row["url"]).path.rstrip("/") for row in responses if row["status"] == 200}
    if errors or stats.get("log_count/ERROR", 0) or not expected_paths.issubset(successful):
        raise RuntimeError("arXiv list retrieval failed or was incomplete; see crawl-diagnostics.json")
    if stats.get("finish_reason") != "finished":
        raise RuntimeError("Crawler did not finish normally; see crawl-diagnostics.json")
    unique = sorted({base_id(item.get("id")) for item in records}, reverse=True)
    write_json(output / "candidate-ids.json", unique)
    if not unique:
        raise RuntimeError("No eligible IDs found. The list may be empty or its HTML may have changed; no AI calls were made.")
    return unique, len(records)


def fetch_metadata(selected, output):
    import arxiv
    import requests

    # Fetch only selected IDs, in batches; rate limiting applies to HTTP requests.
    batch_size = 50
    client = arxiv.Client(page_size=batch_size, delay_seconds=5, num_retries=0)
    records = []
    for offset in range(0, len(selected), batch_size):
        batch = selected[offset:offset + batch_size]
        for attempt in range(3):
            try:
                search = arxiv.Search(id_list=batch, max_results=len(batch))
                papers = list(client.results(search))
                by_id = {base_id(paper.get_short_id()): paper for paper in papers}
                if len(papers) != len(batch) or set(by_id) != set(batch):
                    raise RuntimeError("arXiv metadata response is missing requested IDs or contains unexpected IDs")
                break
            except (arxiv.HTTPError, requests.RequestException) as error:
                status = getattr(error, "status", None)
                if isinstance(error, arxiv.HTTPError) and status not in (429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise RuntimeError(f"arXiv metadata batch failed at offset {offset}; no AI calls were made") from error
                delay = (30, 60)[attempt]
                print(f"arXiv metadata request failed; retrying in {delay}s", flush=True)
                time.sleep(delay)
        for paper_id in batch:
            paper = by_id[paper_id]
            item = {
                "id": paper_id,
                "title": paper.title,
                "authors": [author.name for author in paper.authors],
                "categories": paper.categories,
                "comment": paper.comment,
                "summary": paper.summary,
                "abs": paper.entry_id,
                "pdf": paper.pdf_url,
            }
            if not item["title"].strip() or not item["summary"].strip():
                raise RuntimeError(f"Empty title or abstract for {paper_id}")
            records.append(item)
        write_json(output / "metadata-progress.json", {"completed_ids": [row["id"] for row in records]})
        print(f"Metadata ready: {len(records)}/{len(selected)}", flush=True)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-papers", type=int, default=3, help="0: all eligible IDs; positive: pre-analysis limit")
    parser.add_argument("--output", type=Path, default=Path("run-output"))
    args = parser.parse_args()
    if args.max_papers < 0:
        parser.error("--max-papers must be 0 or a positive integer")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    categories = [part.strip() for part in os.environ.get("CATEGORIES", "cs.CV").split(",") if part.strip()]
    if not categories or any(not re.fullmatch(r"[A-Za-z-]+(?:\.[A-Za-z-]+)?", part) for part in categories):
        raise ValueError("CATEGORIES must contain comma-separated arXiv categories")
    os.environ["CATEGORIES"] = ",".join(dict.fromkeys(categories))
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    info = {
        "commit": os.environ.get("GITHUB_SHA"),
        "branch": os.environ.get("GITHUB_REF_NAME"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_date_utc": run_date,
        "categories": os.environ["CATEGORIES"].split(","),
        "max_papers": args.max_papers,
        "live_arxiv_fetch": True,
        "selection": "unique arXiv IDs descending, before AI analysis",
        "cross_day_deduplication": False,
        "publishes_data": False,
        "status": "preparing",
    }
    write_json(output / "run-info.json", info)
    try:
        ids, observed_count = crawl_ids(output)
        selected = ids if args.max_papers == 0 else ids[:args.max_papers]
        info.update(list_item_count=observed_count, unique_candidate_count=len(ids), selected_ids=selected)
        write_json(output / "run-info.json", info)
        records = fetch_metadata(selected, output)
        data_dir = output / "data"
        data_dir.mkdir(exist_ok=True)
        raw_path = data_dir / f"{run_date}.jsonl"
        raw_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
        info.update(status="prepared", metadata_count=len(records))
        write_json(output / "run-info.json", info)
        if os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as handle:
                handle.write(f"RUN_DATE={run_date}\n")
        print(f"Prepared {len(records)} papers from {len(ids)} unique candidates", flush=True)
    except Exception as error:
        info.update(status="failed", error=str(error))
        write_json(output / "run-info.json", info)
        raise


if __name__ == "__main__":
    main()
