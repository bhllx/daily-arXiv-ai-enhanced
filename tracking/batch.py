from collections import Counter

from .analysis_schema import SCHEMA_VERSION, TrackingAnalysis


CATEGORY_ORDER = {
    "MAIN": 0,
    "METHOD": 1,
    "EXPLORATORY": 2,
    "PENDING": 3,
    "EXCLUDE": 4,
}
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, None: 3, "NONE": 4}
TRACKED_CATEGORIES = {"MAIN", "METHOD", "EXPLORATORY"}


def finalize_tracking_batch(processed_data):
    """汇总一次成功的 process_all_items 返回值；None 表示被原有规则过滤。"""
    papers = []
    seen_ids = set()
    filtered_count = 0

    for item in processed_data:
        if item is None:
            filtered_count += 1
            continue
        if not isinstance(item, dict):
            raise ValueError("Batch item must be a paper object or None")

        paper_id = item.get("id")
        if not isinstance(paper_id, str) or not paper_id.strip():
            raise ValueError("Paper is missing a valid id")
        if paper_id in seen_ids:
            raise ValueError(f"Duplicate paper id in batch: {paper_id}")
        seen_ids.add(paper_id)

        analysis = TrackingAnalysis.model_validate(item.get("AI"))
        paper = dict(item)
        paper["AI"] = analysis.model_dump(mode="json")
        papers.append(paper)

    papers.sort(key=lambda paper: (
        PRIORITY_ORDER[paper["AI"]["priority"]],
        CATEGORY_ORDER[paper["AI"]["category"]],
        paper["id"],
    ))

    category_counts = Counter(paper["AI"]["category"] for paper in papers)
    priority_counts = Counter(paper["AI"]["priority"] for paper in papers)
    tracked = [
        paper for paper in papers
        if paper["AI"]["category"] in TRACKED_CATEGORIES
    ]

    summary = {
        "schema_version": SCHEMA_VERSION,
        "input_count": len(papers) + filtered_count,
        "analyzed_count": len(papers),
        "filtered_count": filtered_count,
        "tracked_count": len(tracked),
        "category_counts": {
            category: category_counts[category]
            for category in CATEGORY_ORDER
        },
        "priority_counts": {
            priority: priority_counts[priority]
            for priority in ("P0", "P1", "P2")
        },
        "deep_read_ids": [paper["id"] for paper in tracked[:5]],
    }
    return papers, summary
