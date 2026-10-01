"""Validate a brief batch independently of its success rate, and archive its state."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tracking.brief_checkpoint import (
    BriefCheckpoint, CATEGORIES, PRIORITIES, atomic_text, write_json,
)

VALIDATION_FORMAT = "lvr-brief-validation-v1"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def same_json(actual, expected):
    # Booleans must not pass integer count checks as True == 1.
    return json.dumps(actual, sort_keys=True, ensure_ascii=False) == json.dumps(expected, sort_keys=True, ensure_ascii=False)


def inspect_brief_batch(artifact):
    """Read and verify all outputs. Never regenerate a missing checkpoint here."""
    artifact = Path(artifact)
    info = read_json(artifact / "run-info.json")
    day = info.get("run_date_utc")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("Invalid batch date")
    if (info.get("status") != "prepared" or info.get("live_arxiv_fetch") is not True
            or not re.fullmatch(r"[0-9]+", str(info.get("run_id", "")))
            or info.get("branch") not in ("main", "lvr-tracking")
            or not isinstance(info.get("commit"), str) or not info["commit"]):
        raise ValueError("Batch has no valid preparation/provenance record")

    raw_path = artifact / "data" / f"{day}.jsonl"
    result_path = artifact / "data" / f"{day}_AI_brief_Chinese.jsonl"
    raw = read_rows(raw_path)
    checkpoint_dir = artifact / "brief-progress"
    for name in ("manifest.json", "input.jsonl", "summary.json", "failures.json", "unprocessed-ids.json"):
        if not (checkpoint_dir / name).is_file():
            raise ValueError(f"Missing brief checkpoint file: {name}")
    checkpoint = BriefCheckpoint(checkpoint_dir, raw)
    if (type(info.get("metadata_count")) is not int or info["metadata_count"] != len(raw)
            or info.get("selected_ids") != [row["id"] for row in raw]):
        raise ValueError("Raw metadata does not match the selected IDs/counts")
    limit = info.get("max_papers")
    if type(limit) is not int or limit < 0 or (limit and len(raw) > limit):
        raise ValueError("Raw batch exceeds its recorded limit")

    results, failures = [], []
    for paper_id in checkpoint.inputs:
        entry = checkpoint.states.get(paper_id)
        if entry is None:
            continue
        attempts = entry["attempts"]
        allowed_stages = {"success": ("complete",), "failed": ("input", "request", "parse", "validate"),
                          "filtered": ("input_filter", "output_filter")}
        if attempts and entry.get("stage") not in allowed_stages[entry["status"]]:
            raise ValueError(f"Paper status and processing stage disagree: {paper_id}")
        if entry["status"] != "success" and entry.get("result") is not None:
            raise ValueError(f"Unsuccessful paper has a success result: {paper_id}")
        if not attempts:
            # Imported historical analyses need an explicit version, but no invented usage.
            if not (entry["status"] == "success" and entry["result"].get("analysis_schema_version") == "lvr-analysis-v1"):
                raise ValueError(f"Paper has no recorded attempt: {paper_id}")
        else:
            for index, attempt in enumerate(attempts, start=1):
                if (not isinstance(attempt, dict) or type(attempt.get("number")) is not int
                        or attempt["number"] != index or type(attempt.get("api_called")) is not bool
                        or attempt.get("status") not in ("success", "failed", "filtered")):
                    raise ValueError(f"Invalid attempt history: {paper_id}")
                usage = attempt.get("usage")
                if usage is not None:
                    if not isinstance(usage, dict):
                        raise ValueError(f"Invalid token usage: {paper_id}")
                    for name in ("input_tokens", "output_tokens", "total_tokens"):
                        value = usage.get(name)
                        if value is not None and (type(value) is not int or value < 0):
                            raise ValueError(f"Invalid token count: {paper_id}")
            if attempts[-1]["status"] != entry["status"]:
                raise ValueError(f"Paper state differs from its latest attempt: {paper_id}")
        if entry["status"] == "success":
            results.append(entry["result"])
        elif entry["status"] == "failed":
            errors = entry.get("errors")
            if (not isinstance(errors, list) or not errors
                    or any(not isinstance(e, dict) or not isinstance(e.get("type"), str)
                           or not isinstance(e.get("message"), str) for e in errors)):
                raise ValueError(f"Failure has no usable diagnostics: {paper_id}")
            failures.append({"paper_id": paper_id, "input": checkpoint.inputs[paper_id],
                             "stage": entry["stage"], "errors": errors,
                             "attempt_count": len(attempts), "raw_response": entry.get("raw_response")})
    results.sort(key=lambda row: (PRIORITIES[row["AI"]["priority"]], CATEGORIES.index(row["AI"]["category"]), row["id"]))
    saved_results = read_rows(result_path)
    if not same_json(saved_results, results):
        raise ValueError("Saved results differ from validated checkpoint successes or priority order")
    saved_failures = read_json(checkpoint_dir / "failures.json")
    if (not isinstance(saved_failures, list)
            or not same_json(sorted(saved_failures, key=lambda entry: entry["paper_id"]),
                             sorted(failures, key=lambda entry: entry["paper_id"]))):
        raise ValueError("Failure list does not match failed papers")
    unprocessed = checkpoint.select_ids("pending")
    if not same_json(read_json(checkpoint_dir / "unprocessed-ids.json"), unprocessed):
        raise ValueError("Unprocessed IDs are inconsistent")

    summary = read_json(checkpoint_dir / "summary.json")
    expected = checkpoint.summarize()
    if "system_error" in summary:
        if not isinstance(summary["system_error"], str) or not summary["system_error"].strip():
            raise ValueError("Invalid system_error marker")
        expected.update(system_error=summary["system_error"], ready_for_publication=False)
    if not same_json(summary, expected):
        raise ValueError("Summary counts, status or token usage do not match paper states")
    if sum(summary[key] for key in ("success_count", "failed_count", "filtered_count", "unprocessed_count")) != len(raw):
        raise ValueError("Success/failure/filter/unprocessed counts do not cover raw input")
    validation = {
        "format": VALIDATION_FORMAT, "status": "passed", "commit": info["commit"],
        "run_id": str(info["run_id"]), "run_date_utc": day,
        "live_arxiv_fetch": True, "batch_status": summary["status"],
        "ready_for_publication": summary["ready_for_publication"],
        **{name: summary[name] for name in ("input_count", "success_count", "failed_count", "filtered_count", "unprocessed_count")},
    }
    return {"artifact": artifact, "day": day, "raw": raw, "results": results,
            "info": info, "summary": summary, "validation": validation,
            "failures": failures, "checkpoint": checkpoint}


def finalize_brief_artifact(artifact):
    """Write a validation marker only after independent checks have passed."""
    artifact = Path(artifact)
    (artifact / "validation.json").unlink(missing_ok=True)
    batch = inspect_brief_batch(artifact)
    write_json(artifact / "summary.json", batch["summary"])
    write_json(artifact / "validation.json", batch["validation"])
    return batch


def validate_brief_artifact(artifact, source_run, repository, *, require_publishable=True):
    batch = inspect_brief_batch(artifact)
    artifact = Path(artifact)
    if not same_json(read_json(artifact / "summary.json"), batch["summary"]) or not same_json(read_json(artifact / "validation.json"), batch["validation"]):
        raise ValueError("Final batch summary or validation marker does not match current files")
    info = batch["info"]
    if (source_run.get("status") != "completed"
            or source_run.get("path") != ".github/workflows/run.yml"
            or source_run.get("event") not in ("schedule", "workflow_dispatch")
            or (source_run.get("head_repository") or {}).get("full_name") != repository
            or source_run.get("head_branch") != info["branch"]
            or str(source_run.get("id")) != str(info["run_id"])
            or source_run.get("head_sha") != info["commit"]):
        raise ValueError("Artifact provenance does not match the selected collection run")
    if require_publishable:
        if source_run.get("conclusion") != "success" or batch["summary"]["ready_for_publication"] is not True:
            raise ValueError("Batch cannot update the website; archive its state only")
    return batch


def archive_brief_batch(batch, data_checkout, source_run):
    """Stage recovery data in the data checkout, without git/network/site changes."""
    artifact = batch["artifact"]
    archive = Path(data_checkout) / "runs" / str(source_run["id"]) / "brief-batch"
    names = ["run-info.json", "summary.json", "validation.json",
             f"data/{batch['day']}.jsonl", f"data/{batch['day']}_AI_brief_Chinese.jsonl"]
    names += [f"brief-progress/{name}" for name in ("manifest.json", "input.jsonl", "summary.json", "failures.json", "unprocessed-ids.json")]
    checkpoint = batch["checkpoint"]
    names += [str(checkpoint.record_path(paper_id).relative_to(artifact)).replace("\\", "/") for paper_id in checkpoint.states]
    payload = {name: (artifact / name).read_text(encoding="utf-8-sig") for name in names}
    # A second publication of this run is idempotent. Do not silently overwrite a
    # different snapshot from a GitHub rerun using the same run ID.
    for name, text in payload.items():
        destination = archive / name
        if destination.exists() and destination.read_text(encoding="utf-8") != text:
            raise ValueError(f"Different archived content for this run: {name}; use a new collection run")
    for name, text in payload.items():
        atomic_text(archive / name, text)
    return archive


def website_batch_status(batch, source_run):
    """Only short public-facing failure facts; detailed responses stay in the archive."""
    return {
        "schema_version": "lvr-brief-day-status-v1", "run_id": str(source_run["id"]),
        "run_date_utc": batch["day"], "summary": batch["summary"],
        "failures": [{"id": row["paper_id"], "title": row["input"].get("title", row["paper_id"]),
                      "abs": row["input"].get("abs"), "stage": row["stage"],
                      "error_type": row["errors"][-1]["type"], "attempt_count": row["attempt_count"]}
                     for row in batch["failures"]],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    batch = finalize_brief_artifact(args.artifact)
    print(json.dumps(batch["summary"], ensure_ascii=False, indent=2))
    # All-failed/empty/incomplete batches can be valid archives, but the explicit
    # ready_for_publication flag controls whether they can update the website.
    print("LVR_BRIEF_ARTIFACT_VALIDATED")


if __name__ == "__main__":
    main()
