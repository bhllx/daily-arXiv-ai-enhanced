"""Atomic per-paper results. Successful papers never expire when the model changes."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from .brief_schema import BriefAnalysis, SCHEMA_VERSION

FORMAT = "lvr-brief-checkpoint-v1"
CATEGORIES = ("MAIN", "METHOD", "EXPLORATORY", "PENDING", "EXCLUDE")
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2, None: 3, "NONE": 4}
RESERVED = {"AI", "analysis_schema_version", "analysis_provenance"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def redact(value):
    if isinstance(value, str):
        for name in ("OPENAI_API_KEY", "TOKEN_GITHUB", "GITHUB_TOKEN", "GH_TOKEN"):
            secret = os.environ.get(name, "")
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def error_details(error):
    details, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        details.append({"type": type(error).__name__, "message": str(error)[:16000]})
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return redact(details)


def validate_saved_result(result, original):
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in original.items()):
        raise ValueError("Saved result has changed original metadata")
    version = result.get("analysis_schema_version")
    if version == SCHEMA_VERSION:
        BriefAnalysis.model_validate(result.get("AI"))
    elif version == "lvr-analysis-v1":
        from .analysis_schema import TrackingAnalysis
        TrackingAnalysis.model_validate(result.get("AI"))
    else:
        raise ValueError(f"Unsupported saved analysis version: {version}; explicit migration required")


class BriefCheckpoint:
    """All writes are performed by the collector thread, never worker threads."""
    def __init__(self, directory, rows):
        self.directory = Path(directory)
        self.rows = copy.deepcopy(rows)
        self.inputs, self.states = {}, {}
        for row in self.rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                raise ValueError("Every input must be a paper object with an id")
            if row["id"] in self.inputs or RESERVED.intersection(row):
                raise ValueError("Duplicate id or analysis fields found in raw input")
            self.inputs[row["id"]] = row
        if not self.inputs:
            raise ValueError("Raw batch is empty")
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = {"format": FORMAT, "input_sha256": digest(self.rows), "input_count": len(self.rows)}
        manifest_path = self.directory / "manifest.json"
        if manifest_path.exists():
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise ValueError("Checkpoint belongs to a different raw batch")
            saved_rows = [json.loads(line) for line in (self.directory / "input.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if saved_rows != self.rows:
                raise ValueError("Checkpoint input.jsonl does not match raw input")
        else:
            if (self.directory / "papers").exists():
                raise ValueError("Checkpoint manifest is missing; do not overwrite saved papers")
            atomic_text(self.directory / "input.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows))
            write_json(manifest_path, manifest)
        for paper_id, row in self.inputs.items():
            path = self.record_path(paper_id)
            if not path.exists():
                continue
            entry = json.loads(path.read_text(encoding="utf-8"))
            if (entry.get("format") != FORMAT or entry.get("paper_id") != paper_id
                    or entry.get("input_sha256") != digest(row)
                    or entry.get("status") not in ("success", "failed", "filtered")
                    or not isinstance(entry.get("attempts"), list)):
                raise ValueError(f"Invalid checkpoint for {paper_id}; manual inspection required")
            if entry["status"] == "success":
                validate_saved_result(entry.get("result"), row)
            elif entry["status"] == "failed" and not entry.get("errors"):
                raise ValueError(f"Failure record for {paper_id} has no diagnostic information")
            self.states[paper_id] = entry

    def record_path(self, paper_id):
        return self.directory / "papers" / (digest(paper_id) + ".json")

    def select_ids(self, mode):
        if mode == "pending":
            return [paper_id for paper_id in self.inputs if paper_id not in self.states]
        if mode == "retry_failed":
            return [paper_id for paper_id in self.inputs if self.states.get(paper_id, {}).get("status") == "failed"]
        raise ValueError("mode must be pending or retry_failed")

    def save(self, paper_id, outcome, provenance):
        status = outcome["status"]
        if status not in ("success", "failed", "filtered"):
            raise ValueError("Invalid paper outcome")
        original = self.inputs[paper_id]
        generated_at = now()
        result = None
        if status == "success":
            analysis = BriefAnalysis.model_validate(outcome["analysis"]).model_dump(mode="json")
            result = {**copy.deepcopy(original), "AI": analysis,
                      "analysis_schema_version": SCHEMA_VERSION,
                      "analysis_provenance": {**provenance, "generated_at": generated_at}}
            validate_saved_result(result, original)
        elif status == "failed" and not outcome.get("errors"):
            raise ValueError("Failed outcome must contain errors")
        attempts = list(self.states.get(paper_id, {}).get("attempts", []))
        attempts.append(redact({
            "number": len(attempts) + 1, "finished_at": generated_at,
            "status": status, "stage": outcome.get("stage"),
            "api_called": outcome.get("api_called", False), "usage": outcome.get("usage"),
            "provenance": provenance, "errors": outcome.get("errors"),
        }))
        entry = redact({
            "format": FORMAT, "paper_id": paper_id, "input_sha256": digest(original),
            "status": status, "attempts": attempts, "result": result,
            "stage": outcome.get("stage"), "errors": outcome.get("errors"),
            "raw_response": outcome.get("raw_response"),
        })
        write_json(self.record_path(paper_id), entry)
        self.states[paper_id] = entry

    def summarize(self):
        counts = {status: sum(e["status"] == status for e in self.states.values())
                  for status in ("success", "failed", "filtered")}
        remaining = len(self.inputs) - sum(counts.values())
        if remaining:
            status = "incomplete"
        elif counts["success"]:
            status = "partial" if counts["failed"] else "complete"
        else:
            status = "failed" if counts["failed"] else "empty"
        categories = {key: 0 for key in CATEGORIES}
        priorities = {key: 0 for key in ("P0", "P1", "P2")}
        calls, known_total, unknown = 0, 0, 0
        for entry in self.states.values():
            if entry["status"] == "success":
                analysis = entry["result"]["AI"]
                categories[analysis["category"]] += 1
                if analysis["priority"] in priorities:
                    priorities[analysis["priority"]] += 1
            for attempt in entry["attempts"]:
                if not attempt.get("api_called"):
                    continue
                calls += 1
                total = (attempt.get("usage") or {}).get("total_tokens")
                if type(total) is int and total >= 0:
                    known_total += total
                else:
                    unknown += 1
        return {
            "schema_version": "lvr-brief-batch-v1", "status": status,
            "input_count": len(self.inputs), "success_count": counts["success"],
            "failed_count": counts["failed"], "filtered_count": counts["filtered"],
            "unprocessed_count": remaining, "category_counts": categories,
            "priority_counts": priorities, "api_calls_attempted": calls,
            "reported_total_tokens": known_total, "calls_without_usage": unknown,
            "ready_for_publication": bool(counts["success"] and not remaining),
        }

    def export(self, output_path):
        successes = [entry["result"] for entry in self.states.values() if entry["status"] == "success"]
        successes.sort(key=lambda row: (PRIORITIES[row["AI"]["priority"]], CATEGORIES.index(row["AI"]["category"]), row["id"]))
        failures = [{"paper_id": paper_id, "input": self.inputs[paper_id],
                     "stage": entry["stage"], "errors": entry["errors"],
                     "attempt_count": len(entry["attempts"]), "raw_response": entry.get("raw_response")}
                    for paper_id, entry in self.states.items() if entry["status"] == "failed"]
        atomic_text(output_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in successes))
        write_json(self.directory / "failures.json", failures)
        write_json(self.directory / "unprocessed-ids.json", self.select_ids("pending"))
        summary = self.summarize()
        write_json(self.directory / "summary.json", summary)
        return summary
