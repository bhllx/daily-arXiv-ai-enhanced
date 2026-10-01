"""Stage a successful LVR batch into the data checkout and build the static site.

Git commit/push and Pages deployment are performed by the workflow, not this script.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ai.lvr_support import validate_tracking_response
from tracking.batch import finalize_tracking_batch
from to_md.convert import render_report
from to_md import convert as report_renderer
from tracking.brief_batch import (
    VALIDATION_FORMAT, archive_brief_batch, validate_brief_artifact, website_batch_status,
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_rows(path, optional=False):
    if optional and not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip() for row in rows):
        raise ValueError(f"Invalid paper records in {path.name}")
    return rows


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def paper_key(row):
    return re.sub(r"v\d+$", "", row["id"])


def merge_rows(old, new):
    # Merge only within this date. Never remove papers simply absent from a small batch.
    records = {paper_key(row): row for row in old}
    records.update({paper_key(row): row for row in new})
    return list(records.values())


def validate_legacy_batch(artifact, source_run, repository):
    info = read_json(artifact / "run-info.json")
    validation = read_json(artifact / "validation.json")
    summary = read_json(artifact / "summary.json")
    run_id = str(source_run["id"])
    if not run_id.isdecimal():
        raise ValueError("Invalid source run ID")
    if source_run.get("status") != "completed" or source_run.get("conclusion") != "success":
        raise ValueError("Source workflow did not complete successfully")
    if source_run.get("path") != ".github/workflows/run.yml":
        raise ValueError("Source run must be the batch workflow")
    if source_run.get("head_repository", {}).get("full_name") != repository:
        raise ValueError("Source run is not from this repository")
    if source_run.get("head_branch") not in ("main", "lvr-tracking"):
        raise ValueError("Unexpected source branch")
    if str(info.get("run_id")) != run_id or info.get("branch") != source_run["head_branch"]:
        raise ValueError("Artifact does not match the selected workflow run")
    if info.get("commit") != source_run["head_sha"] or validation.get("commit") != source_run["head_sha"]:
        raise ValueError("Artifact commit does not match the successful run")
    if info.get("status") != "prepared" or validation.get("status") != "passed":
        raise ValueError("Batch validation did not pass")
    if info.get("live_arxiv_fetch") is not True or validation.get("live_arxiv_fetch") is not True:
        raise ValueError("Only live batch results can be published here")
    day = info["run_date_utc"]
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid batch date")
    raw = read_rows(artifact / "data" / f"{day}.jsonl")
    enhanced = read_rows(artifact / "data" / f"{day}_AI_enhanced_Chinese.jsonl")
    if not raw or not enhanced:
        raise ValueError("Batch contains no publishable analyses")
    originals = {row["id"]: row for row in raw}
    if len(originals) != len(raw) or len(raw) != info.get("metadata_count"):
        raise ValueError("Raw metadata count or IDs do not match the run record")
    if set(originals) != set(info.get("selected_ids", [])):
        raise ValueError("Raw IDs differ from the selected batch")
    for row in enhanced:
        if row["id"] not in originals or any(row.get(key) != value for key, value in originals[row["id"]].items()):
            raise ValueError("Enhanced paper metadata differs from its input")
        validate_tracking_response(row.get("AI"), row)
    ordered, expected = finalize_tracking_batch(enhanced + [None] * (len(raw) - len(enhanced)))
    if enhanced != ordered or expected != summary or validation.get("analyzed_count") != len(enhanced):
        raise ValueError("Batch order or statistics failed validation")
    return day, raw, enhanced, info, summary, validation


def is_brief_artifact(artifact):
    return ((artifact / "brief-progress").exists()
            or read_json(artifact / "validation.json").get("format") == VALIDATION_FORMAT)


def validate_batch(artifact, source_run, repository):
    if not is_brief_artifact(artifact):
        return validate_legacy_batch(artifact, source_run, repository)
    batch = validate_brief_artifact(artifact, source_run, repository)
    return (batch["day"], batch["raw"], batch["results"], batch["info"],
            batch["summary"], batch["validation"])


def render_publication(rows, batch_status=None):
    has_briefs = any(row.get("analysis_schema_version") == "lvr-brief-v1" for row in rows)
    if has_briefs or batch_status is not None:
        if not getattr(report_renderer, "SUPPORTS_BRIEF_ANALYSIS", False):
            raise RuntimeError("Brief Markdown rendering is not connected yet; complete step 4 before publishing brief batches")
        return report_renderer.render_report(rows, batch_status=batch_status)
    return render_report(rows)


def set_js_value(text, field, value):
    updated, count = re.subn(rf"(?m)^(\s*{re.escape(field)}:\s*)['\"][^'\"\r\n]*['\"]", lambda match: match[1] + json.dumps(value), text)
    if count != 1:
        raise ValueError(f"Expected exactly one JavaScript setting: {field}")
    return updated


def build_site(site, repository, password):
    site.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "login.html", "settings.html", "statistic.html"):
        shutil.copyfile(ROOT / name, site / name)
    for name in ("css", "js", "assets", "images", "buy-me-a-coffee"):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, site / name, dirs_exist_ok=True)
    owner, name = repository.split("/", 1)
    config = site / "js/data-config.js"
    text = config.read_text(encoding="utf-8")
    for field, value in (("repoOwner", owner), ("repoName", name), ("dataBranch", "data")):
        text = set_js_value(text, field, value)
    config.write_text(text, encoding="utf-8")
    auth = site / "js/auth-config.js"
    auth_text = auth.read_text(encoding="utf-8")
    if password:
        auth_text = set_js_value(auth_text, "passwordHash", hashlib.sha256(password.encode()).hexdigest())
    elif "PLACEHOLDER_PASSWORD_HASH" in auth_text:
        raise ValueError("Authentication placeholder is unresolved; configure ACCESS_PASSWORD or the existing auth config")
    auth.write_text(auth_text, encoding="utf-8")
    # Preserve configured authentication when the secret is absent.
    revision = os.environ.get("GITHUB_SHA", "local")[:12]
    if not re.fullmatch(r"[A-Za-z0-9-]+", revision):
        raise ValueError("Invalid build revision")
    index = site / "index.html"
    index.write_text(re.sub(r'js/app\.js(?:\?[^"\s]*)?', f"js/app.js?v={revision}", index.read_text(encoding="utf-8")), encoding="utf-8")
    (site / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--data-checkout", type=Path, required=True)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--site", type=Path, default=Path("_site"))
    parser.add_argument("--state-only", action="store_true", help="Archive a validated brief batch without modifying website data")
    args = parser.parse_args()
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    source_run = read_json(args.source_run)
    brief_batch = None
    if is_brief_artifact(args.artifact):
        brief_batch = validate_brief_artifact(
            args.artifact, source_run, repository, require_publishable=not args.state_only,
        )
    elif args.state_only:
        raise ValueError("--state-only requires a brief batch")
    if args.state_only:
        archive = archive_brief_batch(brief_batch, args.data_checkout, source_run)
        publication = {
            "date": brief_batch["day"], "source_run_id": str(source_run["id"]),
            "state_only": True, "ready_for_publication": False,
            "batch_status": brief_batch["summary"]["status"],
            "success_count": brief_batch["summary"]["success_count"],
            "failed_count": brief_batch["summary"]["failed_count"],
            "archive": str(archive.relative_to(args.data_checkout)),
        }
        write_json(ROOT / "publication.json", publication)
        print(json.dumps(publication, ensure_ascii=False, indent=2))
        print("LVR_BRIEF_STATE_ARCHIVED")
        return
    if brief_batch is not None:
        day, raw, enhanced, info, summary, validation = (
            brief_batch["day"], brief_batch["raw"], brief_batch["results"],
            brief_batch["info"], brief_batch["summary"], brief_batch["validation"],
        )
    else:
        day, raw, enhanced, info, summary, validation = validate_legacy_batch(args.artifact, source_run, repository)
    data_dir = args.data_checkout / "data"
    raw_path = data_dir / f"{day}.jsonl"
    ai_path = data_dir / f"{day}_AI_enhanced_Chinese.jsonl"
    old_raw = read_rows(raw_path, optional=True)
    old_enhanced = read_rows(ai_path, optional=True)
    merged_raw = merge_rows(old_raw, raw)
    merged_enhanced = merge_rows(old_enhanced, enhanced)
    batch_status = website_batch_status(brief_batch, source_run) if brief_batch is not None else None
    if batch_status is not None:
        saved_keys = {paper_key(row) for row in merged_enhanced}
        for failure in batch_status["failures"]:
            failure["has_saved_analysis"] = paper_key(failure) in saved_keys
    else:
        # A later legacy batch on the same day must not erase an existing notice.
        status_path = data_dir / f"{day}_brief_status.json"
        if status_path.exists():
            batch_status = read_json(status_path)
    markdown = render_publication(merged_enhanced, batch_status=batch_status)
    build_site(args.site, repository, os.environ.get("ACCESS_PASSWORD", ""))
    if brief_batch is not None:
        archive_brief_batch(brief_batch, args.data_checkout, source_run)
    data_dir.mkdir(parents=True, exist_ok=True)
    for path, rows in ((raw_path, merged_raw), (ai_path, merged_enhanced)):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (data_dir / f"{day}.md").write_text(markdown, encoding="utf-8")
    if batch_status is not None:
        write_json(data_dir / f"{day}_brief_status.json", batch_status)
    assets = args.data_checkout / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "file-list.txt").write_text("".join(path.name + "\n" for path in sorted(data_dir.glob("*.jsonl"))), encoding="utf-8")
    archive = args.data_checkout / "runs" / str(source_run["id"])
    for filename, value in (("run-info.json", info), ("summary.json", summary), ("validation.json", validation)):
        write_json(archive / filename, value)
    old_keys = {paper_key(row) for row in old_enhanced}
    publication = {
        "date": day, "source_run_id": str(source_run["id"]),
        "source_commit": source_run["head_sha"], "incoming_analyses": len(enhanced),
        "new_ids": sum(paper_key(row) not in old_keys for row in enhanced),
        "updated_ids": sum(paper_key(row) in old_keys for row in enhanced),
        "total_saved_for_date": len(merged_enhanced),
        "state_only": False,
        "batch_status": summary.get("status", "complete"),
        "failed_count": summary.get("failed_count", 0),
    }
    write_json(ROOT / "publication.json", publication)
    print(json.dumps(publication, ensure_ascii=False, indent=2))
    print("LVR_PUBLICATION_STAGED")


if __name__ == "__main__":
    main()
