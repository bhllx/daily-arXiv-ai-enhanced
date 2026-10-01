"""Analyze briefs once, save each outcome, and tolerate individual failures.

This separate entry point is activated by the production workflow in a later step.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.brief_support import BriefFailure, analyze_once, build_brief_chain
from ai.content_filter import is_sensitive
from tracking.brief_checkpoint import BriefCheckpoint, error_details, write_json


def process_one(chain, item, language, filter_fn=is_sensitive):
    if filter_fn(item.get("summary") or ""):
        return {"status": "filtered", "stage": "input_filter", "api_called": False}
    try:
        analysis, usage = analyze_once(chain, item, language)
    except BriefFailure as error:
        return {
            "status": "failed", "stage": error.stage, "errors": error_details(error.error),
            "raw_response": error.raw, "usage": error.usage,
            "api_called": error.api_called, "fatal": error.fatal,
        }
    if filter_fn(json.dumps(analysis, ensure_ascii=False)):
        return {"status": "filtered", "stage": "output_filter", "api_called": True, "usage": usage}
    return {"status": "success", "stage": "complete", "analysis": analysis, "api_called": True, "usage": usage}


def process_batch(checkpoint, chain, provenance, selected_ids, output, max_workers, language, filter_fn=is_sensitive):
    if not 1 <= max_workers <= 8:
        raise ValueError("max_workers must be between 1 and 8")
    iterator = iter(selected_ids)
    fatal = None
    completed = 0
    # At most max_workers requests are in flight, so authentication failures can
    # stop new submissions without launching the whole remaining batch.
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            active = {}

            def submit_next():
                paper_id = next(iterator, None)
                if paper_id is not None:
                    future = executor.submit(process_one, chain, copy.deepcopy(checkpoint.inputs[paper_id]), language, filter_fn)
                    active[future] = paper_id

            for _ in range(max_workers):
                submit_next()
            while active:
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    paper_id = active.pop(future)
                    outcome = future.result()
                    # I/O and programming errors must not be reported as ordinary
                    # paper failures or swallowed by a catch-all retry loop.
                    checkpoint.save(paper_id, outcome, provenance)
                    completed += 1
                    print(f"[{completed}/{len(selected_ids)}] {paper_id}: {outcome['status']}", flush=True)
                    if outcome["status"] == "failed":
                        detail = outcome["errors"][-1]
                        print(f"  {outcome['stage']}: {detail['type']}: {detail['message'][:240]}", file=sys.stderr)
                    if outcome.get("fatal"):
                        fatal = f"Provider configuration/access error for {paper_id}; remaining papers were not submitted"
                if not fatal:
                    for _ in finished:
                        submit_next()
    finally:
        # Also make partial results inspectable after an interruption or exception.
        summary = checkpoint.export(output)
    if fatal:
        summary.update(ready_for_publication=False, system_error=fatal)
        write_json(checkpoint.directory / "summary.json", summary)
        raise RuntimeError(fatal)
    return summary


def main():
    import dotenv

    dotenv.load_dotenv(ROOT / "ai/.env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument("--max_workers", type=int, default=2)
    parser.add_argument("--mode", choices=("pending", "retry_failed"), default="pending")
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--request-timeout", type=float, default=120)
    args = parser.parse_args()
    if not 1 <= args.max_workers <= 8:
        parser.error("--max_workers must be between 1 and 8")
    language = os.environ.get("LANGUAGE", "Chinese")
    if language not in ("Chinese", "English"):
        raise ValueError("LANGUAGE must be Chinese or English")
    source = args.data.resolve()
    output = (args.output or source.with_name(f"{source.stem}_AI_brief_{language}.jsonl")).resolve()
    directory = (args.checkpoint_dir or source.with_name(source.stem + "_brief_progress")).resolve()
    # Generated files must never replace raw input or the checkpoint's internal files.
    if output == source or directory == source or directory in output.parents or directory in source.parents:
        raise ValueError("Keep raw input, exported output and checkpoint directory separate")
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    checkpoint = BriefCheckpoint(directory, rows)
    selected = checkpoint.select_ids(args.mode)
    summary = checkpoint.export(output)
    print(f"Mode={args.mode}; selected={len(selected)}; cached successes={summary['success_count']}", flush=True)
    if selected:
        chain, provenance = build_brief_chain(
            os.environ.get("MODEL_NAME", ""), language, args.max_output_tokens, args.request_timeout,
        )
        summary = process_batch(checkpoint, chain, provenance, selected, output, args.max_workers, language)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # Partial completion is a valid result for the later publication step. Empty,
    # fully failed or interrupted batches must not replace the existing website.
    return 0 if summary["ready_for_publication"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
