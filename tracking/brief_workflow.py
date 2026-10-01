"""Workflow plumbing for new briefs, checkpoint recovery and publication gates.

This module makes no AI requests. Publication writes/pushes are separate steps.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tracking.brief_batch import (
    finalize_brief_artifact, inspect_brief_batch, read_json, read_rows, same_json,
    validate_brief_artifact, website_batch_status,
)
from tracking.brief_checkpoint import BriefCheckpoint, digest, write_json


def numeric_id(value):
    value = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Run ID must be a positive integer")
    return value


def emit(name, value):
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if '\n' in text or '\r' in text:
        raise ValueError("Workflow output must be a single line")
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.write(f'{name}={text}\n')
    print(f'{name}={text}')


def note(text):
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write(text + '\n\n')


def check_config():
    mode = os.environ.get('RUN_MODE', 'new')
    if mode not in ('new', 'retry_failed', 'resume_pending'):
        raise ValueError('Unknown RUN_MODE')
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise ValueError('Use Run workflow with retry_failed/resume_pending and the source run ID; do not use Re-run jobs for collection.')
    if int(os.environ.get('MAX_PAPERS', '3')) < 0 or not 1 <= int(os.environ.get('AI_WORKERS', '2')) <= 8:
        raise ValueError('MAX_PAPERS must be >= 0; AI_WORKERS must be 1..8')
    if mode != 'new':
        numeric_id(os.environ.get('SOURCE_RUN_ID', ''))
    elif os.environ.get('SOURCE_RUN_ID', '').strip():
        raise ValueError('For an existing batch choose retry_failed/resume_pending; leave source_run_id empty for new.')
    missing = [key for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MODEL_NAME') if not os.environ.get(key, '').strip()]
    if missing:
        raise ValueError('Missing configuration: ' + ', '.join(missing))
    import ast
    for name in ('tracking/prepare_batch.py', 'tracking/brief_workflow.py', 'tracking/brief_batch.py',
                 'tracking/brief_checkpoint.py', 'tracking/brief_schema.py', 'ai/brief_support.py',
                 'ai/enhance_brief.py', 'tracking/publish_results.py', 'to_md/convert.py'):
        ast.parse((ROOT / name).read_text(encoding='utf-8'))
    if not (ROOT / 'tracking/brief_profile.md').read_text(encoding='utf-8').strip():
        raise ValueError('brief_profile.md is empty')
    note(f'运行模式：{mode}。恢复模式复用原批次输入，max_papers 只对 new 生效。')


def checked_archive(data_checkout, run_id):
    run_id = numeric_id(run_id)
    folder = Path(data_checkout) / 'runs' / run_id / 'brief-batch'
    if not folder.is_dir():
        raise ValueError(f'No archived brief checkpoint for {run_id}. Wait for publication/archival to complete; old long-analysis runs cannot be resumed here.')
    batch = inspect_brief_batch(folder)
    if (str(batch['info']['run_id']) != run_id
            or not same_json(read_json(folder / 'validation.json'), batch['validation'])
            or not same_json(read_json(folder / 'summary.json'), batch['summary'])):
        raise ValueError('Archived checkpoint verification failed')
    return batch


def latest_archive(data_checkout, requested_id):
    """Follow an archived retry lineage so an older ID does not repeat saved successes."""
    initial = checked_archive(data_checkout, requested_id)
    origin = str(initial['info'].get('origin_run_id', initial['info']['run_id']))
    latest = initial
    for path in (Path(data_checkout) / 'runs').glob('*/brief-batch/run-info.json'):
        candidate_id = path.parent.parent.name
        if not candidate_id.isdecimal() or int(candidate_id) <= int(latest['info']['run_id']):
            continue
        info = read_json(path)
        if str(info.get('origin_run_id', info.get('run_id'))) != origin:
            continue
        candidate = checked_archive(data_checkout, candidate_id)
        if (digest(candidate['raw']) != digest(initial['raw']) or candidate['day'] != initial['day']
                or candidate['info']['branch'] != initial['info']['branch']):
            raise ValueError('Conflicting checkpoint lineage; no AI calls were made')
        latest = candidate
    return latest, origin


def prepare(output, data_checkout, mode, source_id, limit):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty output directory')
    run_id = numeric_id(os.environ['GITHUB_RUN_ID'])
    branch = os.environ['GITHUB_REF_NAME']
    commit = os.environ['GITHUB_SHA']
    if branch not in ('main', 'lvr-tracking') or not re.fullmatch(r'[a-fA-F0-9]{40}', commit):
        raise ValueError('Unexpected code branch or commit')
    if mode == 'new':
        subprocess.run([sys.executable, str(ROOT / 'tracking/prepare_batch.py'),
                        '--max-papers', str(limit), '--output', str(output)], check=True)
        info = read_json(output / 'run-info.json')
        info.update(run_mode='new', origin_run_id=run_id, run_attempt=1)
        write_json(output / 'run-info.json', info)
    else:
        parent, origin = latest_archive(data_checkout, source_id)
        if parent['info']['branch'] != branch or int(parent['info']['run_id']) >= int(run_id):
            raise ValueError('Recovery must use an earlier run on the same branch')
        output.mkdir(parents=True, exist_ok=True)
        (output / 'data').mkdir()
        day = parent['day']
        shutil.copyfile(parent['artifact'] / 'data' / f'{day}.jsonl', output / 'data' / f'{day}.jsonl')
        shutil.copytree(parent['artifact'] / 'brief-progress', output / 'brief-progress')
        info = {**parent['info'], 'run_id': run_id, 'commit': commit, 'branch': branch,
                'run_attempt': 1, 'run_mode': mode, 'origin_run_id': origin,
                'resume_requested_run_id': numeric_id(source_id),
                'resume_from_run_id': str(parent['info']['run_id']),
                'resume_input_sha256': digest(parent['raw']),
                'live_arxiv_fetch': False, 'origin_live_arxiv_fetch': True}
        write_json(output / 'run-info.json', info)
        note(f"沿用批次 {info['resume_from_run_id']}（最初批次 {origin}），保留原日期 {day}；未重新抓取。")
    day = info['run_date_utc']
    rows = read_rows(output / 'data' / f'{day}.jsonl')
    cp = BriefCheckpoint(output / 'brief-progress', rows)
    analysis_mode = 'retry_failed' if mode == 'retry_failed' else 'pending'
    selected = cp.select_ids(analysis_mode)
    # Retain an unresolved systemic error if there is nothing to retry.
    previous = read_json(cp.directory / 'summary.json') if (cp.directory / 'summary.json').exists() else {}
    summary = cp.export(output / 'data' / f'{day}_AI_brief_Chinese.jsonl')
    if not selected and previous.get('system_error'):
        summary.update(system_error=previous['system_error'], ready_for_publication=False)
        write_json(cp.directory / 'summary.json', summary)
    write_json(output / 'selection.json', {'mode': analysis_mode, 'ids': selected})
    emit('run_date', day)
    emit('analysis_mode', analysis_mode)
    emit('selected_count', len(selected))
    note(f"本次待处理 {len(selected)} 篇；复用成功记录 {summary['success_count']} 篇。每个选中项最多生成一次简评。")
    return selected


def finish(output, analysis_outcome):
    """Regenerate exports from atomic per-paper records even after a step fails."""
    output = Path(output)
    info = read_json(output / 'run-info.json')
    day = info['run_date_utc']
    cp = BriefCheckpoint(output / 'brief-progress', read_rows(output / 'data' / f'{day}.jsonl'))
    previous = read_json(cp.directory / 'summary.json')
    summary = cp.export(output / 'data' / f'{day}_AI_brief_Chinese.jsonl')
    if previous.get('system_error'):
        summary.update(system_error=previous['system_error'], ready_for_publication=False)
    elif analysis_outcome == 'failure' and summary['ready_for_publication']:
        summary.update(system_error='Analysis step failed unexpectedly; archive saved outcomes for inspection.', ready_for_publication=False)
    write_json(cp.directory / 'summary.json', summary)
    batch = finalize_brief_artifact(output)
    from to_md.convert import render_report
    status = website_batch_status(batch, {'id': info['run_id']})
    write_json(output / 'batch-status.json', status)
    if summary['success_count']:
        (output / 'data' / f'{day}.md').write_text(render_report(batch['results'], batch_status=status), encoding='utf-8')
    emit('ready_for_publication', summary['ready_for_publication'])
    note(f"批次 {info['run_id']}：输入 {summary['input_count']}，成功 {summary['success_count']}，失败 {summary['failed_count']}，过滤 {summary['filtered_count']}，未处理 {summary['unprocessed_count']}。")
    note('成功数包含 PENDING 和 EXCLUDE。状态文件随结果附件保存，main 分支由发布流程持久保存到 data 分支。')
    return batch


def github_json(path):
    repository = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    request = Request(f'https://api.github.com/repos/{repository}/{path}', headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
    })
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def source_run(run_id):
    run_id = numeric_id(run_id)
    run = github_json(f'actions/runs/{run_id}')
    if (str(run.get('id')) != run_id or run.get('status') != 'completed'
            or run.get('path') != '.github/workflows/run.yml' or run.get('head_branch') != 'main'
            or run.get('event') not in ('schedule', 'workflow_dispatch')
            or (run.get('head_repository') or {}).get('full_name') != os.environ['GITHUB_REPOSITORY']):
        raise ValueError('Choose a completed collection run from this repository/main branch')
    if run.get('run_attempt', 1) != 1:
        raise ValueError('This collection was re-run under the same ID. Use a new Run workflow to keep checkpoints unambiguous.')
    write_json(Path('source-run.json'), run)
    artifacts, page = [], 1
    while True:
        entries = github_json(f'actions/runs/{run_id}/artifacts?per_page=100&page={page}')['artifacts']
        artifacts.extend(a for a in entries if a.get('name') == 'lvr-batch-result' and not a.get('expired'))
        if len(entries) < 100:
            break
        page += 1
    if len(artifacts) > 1:
        raise ValueError('Ambiguous batch artifact')
    emit('has_artifact', bool(artifacts))
    if not artifacts:
        note('此运行没有可下载的结果附件。网页保持不变；若已归档，请直接用归档批次 ID 补跑。')
    return run


def publication_plan(artifact, source_path):
    artifact = Path(artifact)
    source = read_json(source_path)
    if (artifact / 'brief-progress').is_dir():
        batch = validate_brief_artifact(artifact, source, os.environ['GITHUB_REPOSITORY'], require_publishable=False)
        action = 'publish' if batch['summary']['ready_for_publication'] and source['conclusion'] == 'success' else 'archive'
        emit('is_brief', True)
    elif (artifact / 'validation.json').is_file():
        from tracking.publish_results import validate_legacy_batch
        validate_legacy_batch(artifact, source, os.environ['GITHUB_REPOSITORY'])
        action = 'publish'
        emit('is_brief', False)
    else:
        action = 'skip'
        emit('is_brief', False)
        note('抓取或准备阶段未生成有效分析批次；保留诊断附件，现有网页保持不变。')
    emit('action', action)
    return action


def publication_summary():
    result = read_json(Path('publication.json'))
    if result['state_only']:
        note(f"批次 {result['source_run_id']} 的输入、成功结果和失败记录已保存到 data 分支；本次不更新网页。")
    else:
        note(f"数据与网页发布成功。日期 {result['date']}，本批成功结果 {result['incoming_analyses']} 篇，本批失败 {result['failed_count']} 篇，该日期累计保存 {result['total_saved_for_date']} 篇。")
        page_url = os.environ.get('PAGE_URL', '')
        if page_url:
            note(f'网页：{page_url}')
    note('发布和归档均复用已有结果，不调用 DeepSeek。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('check', 'prepare', 'finish', 'source', 'publication-plan', 'publication-summary'))
    parser.add_argument('--output', type=Path, default=Path('run-output'))
    parser.add_argument('--data-checkout', type=Path, default=Path('data-store'))
    parser.add_argument('--artifact', type=Path, default=Path('batch-result'))
    parser.add_argument('--source-run', type=Path, default=Path('source-run.json'))
    args = parser.parse_args()
    if args.command == 'check':
        check_config()
    elif args.command == 'prepare':
        prepare(args.output, args.data_checkout, os.environ.get('RUN_MODE', 'new'),
                os.environ.get('SOURCE_RUN_ID', ''), int(os.environ.get('MAX_PAPERS', '3')))
    elif args.command == 'finish':
        finish(args.output, os.environ.get('ANALYSIS_OUTCOME', 'unknown'))
    elif args.command == 'source':
        source_run(os.environ['SOURCE_RUN_ID'])
    elif args.command == 'publication-plan':
        publication_plan(args.artifact, args.source_run)
    else:
        publication_summary()


if __name__ == '__main__':
    main()
