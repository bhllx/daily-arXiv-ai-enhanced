# import json
# import argparse
# import os
# from itertools import count

# if __name__ == "__main__":
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--data", type=str, help="Path to the jsonline file")
#     args = parser.parse_args()
#     data = []
#     preference = os.environ.get('CATEGORIES', 'cs.CV, cs.CL').split(',')
#     preference = list(map(lambda x: x.strip(), preference))
#     def rank(cate):
#         if cate in preference:
#             return preference.index(cate)
#         else:
#             return len(preference)

#     with open(args.data, "r") as f:
#         for line in f:
#             data.append(json.loads(line))

#     categories = set([item["categories"][0] for item in data])
#     template = open("paper_template.md", "r").read()
#     categories = sorted(categories, key=rank)
#     cnt = {cate: 0 for cate in categories}
#     for item in data:
#         if item["categories"][0] not in cnt.keys():
#             continue
#         cnt[item["categories"][0]] += 1

#     markdown = f"<div id=toc></div>\n\n# Table of Contents\n\n"
#     for idx, cate in enumerate(categories):
#         markdown += f"- [{cate}](#{cate}) [Total: {cnt[cate]}]\n"

#     idx = count(1)
#     for cate in categories:
#         markdown += f"\n\n<div id='{cate}'></div>\n\n"
#         markdown += f"# {cate} [[Back]](#toc)\n\n"
#         papers = []
#         for item in data:
#             if item["categories"][0] == cate:
#                 # Safely access AI fields with default values
#                 ai_data = item.get('AI', {})
#                 if not ai_data or not isinstance(ai_data, dict):
#                     print(f"Skipping item '{item.get('title', 'Unknown')}' due to missing or invalid AI data")
#                     continue
                
#                 # Check if all required AI fields are present
#                 required_fields = ['tldr', 'motivation', 'method', 'result', 'conclusion']
#                 if not all(field in ai_data for field in required_fields):
#                     print(f"Skipping item '{item.get('title', 'Unknown')}' due to incomplete AI fields")
#                     continue
                
#                 papers.append(
#                     template.format(
#                         title=item["title"],
#                         authors=",".join(item["authors"]),
#                         summary=item["summary"],
#                         url=item['abs'],
#                         tldr=ai_data.get('tldr', ''),
#                         motivation=ai_data.get('motivation', ''),
#                         method=ai_data.get('method', ''),
#                         result=ai_data.get('result', ''),
#                         conclusion=ai_data.get('conclusion', ''),
#                         cate=item['categories'][0],
#                         idx=next(idx)
#                     )
#                 )
#         markdown += "\n\n".join(papers)
#     with open(args.data.split('_')[0] + '.md', "w") as f:
#         f.write(markdown)

import argparse
import html
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.lvr_support import build_tracking_sources, validate_tracking_response
from tracking.batch import finalize_tracking_batch


LEGACY_FIELDS = {
    "tldr": "一句话摘要",
    "motivation": "研究动机",
    "method": "方法",
    "result": "结果",
    "conclusion": "结论",
}
CATEGORY_LABELS = {
    "MAIN": "直接相关",
    "METHOD": "方法迁移",
    "EXPLORATORY": "探索性关联",
    "PENDING": "待补材料",
    "EXCLUDE": "排除",
}
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
NATURE_LABELS = {
    "author_claim": "作者声称",
    "source_description": "来源描述",
    "transfer_hypothesis": "迁移假设",
}
ASSESSMENT_LABELS = {
    "research_relevance": "研究相关性",
    "topic_impact": "对选题的影响",
    "evidence_sufficiency": "证据充分性",
    "result_credibility": "结果可信度",
    "reproducibility_readiness": "复现准备度",
}


def md(value):
    """将数据作为普通文本展示，保留数学表达中的美元符号。"""
    text = " ".join(str(value if value is not None else "").split())
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", html.escape(text, quote=False))


def link(label, url):
    if not isinstance(url, str):
        return md(label)
    url = url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return md(label)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return md(label)
    return f"[{md(label)}](<{quote(url, safe=':/?#[]@!$&+,;=%~.-_')}>)"


def joined(values):
    if isinstance(values, list):
        return "、".join(str(value) for value in values)
    return str(values or "未提供")


def field(lines, label, value):
    if value is not None and value != "":
        lines.append(f"**{label}：** {md(value)}\n")


def paper_header(paper, number):
    lines = [
        f'<a id="paper-{number}"></a>\n',
        f"### {number}. {link(paper.get('title', '未提供标题'), paper.get('abs'))}\n",
    ]
    field(lines, "arXiv ID", paper.get("id", "未提供"))
    field(lines, "作者", joined(paper.get("authors")))
    field(lines, "arXiv 学科", joined(paper.get("categories")))
    lines.append(
        link("论文页面", paper.get("abs")) + " · "
        + link("PDF", paper.get("pdf")) + "\n"
    )
    return lines


def source_details(paper):
    sources = build_tracking_sources(paper)
    if not sources:
        return []
    lines = ["<details>", "<summary>来源材料：标题、摘要与备注</summary>\n"]
    for source in sources:
        for paragraph in source["paragraphs"]:
            field(lines, f"{source['source_id']}/{paragraph['id']}", paragraph["text"])
    lines.append("</details>\n")
    return lines


def render_lvr_paper(paper, number):
    ai = paper["AI"]
    category = ai["category"]
    lines = paper_header(paper, number)
    field(lines, "研究分类", f"{category} · {CATEGORY_LABELS[category]}")
    priority = ai["priority"] if ai["priority"] is not None else "未分配（待补材料）"
    field(lines, "优先级", priority)
    field(lines, "分类把握", CONFIDENCE_LABELS[ai["classification_confidence"]])
    if ai["topic_tags"]:
        field(lines, "主题标签", joined(ai["topic_tags"]))

    if category == "EXCLUDE":
        field(lines, "排除原因", ai["exclusion_reason"])
    elif category == "PENDING":
        field(lines, "候选分类", ai["candidate_category"])
        field(lines, "待核验线索", ai["reason_to_track"])
        field(lines, "需要补充或核查", ai["what_to_check_next"])
        field(lines, "主要风险", ai["main_assumption_or_risk"])
    else:
        for key, label in (
            ("core_mechanism", "核心机制"),
            ("reason_to_track", "追踪理由"),
            ("lvr_connection", "与 LVR 的联系"),
            ("main_assumption_or_risk", "主要假设与风险"),
            ("what_to_check_next", "下一步核查"),
        ):
            field(lines, label, ai[key])

    bridge = ai["transfer_path_or_reframing"]
    if bridge:
        if bridge["mode"] == "transfer":
            lines.append("#### 方法迁移分析\n")
            labels = {
                "original_problem": "原问题", "mechanism": "可迁移机制",
                "lvr_bottleneck": "LVR 瓶颈", "adaptation": "拟议迁移",
                "failure_condition": "失败条件",
            }
        else:
            lines.append("#### 探索性问题重述\n")
            labels = {
                "borrowed_concept": "借鉴概念", "reframed_problem": "问题重述",
                "first_check": "首项验证",
            }
        for key, label in labels.items():
            field(lines, label, bridge[key])

    if ai["assessments"] or ai["evidence"]:
        lines.extend(["<details>", "<summary>查看评估与证据</summary>\n"])
        if ai["assessments"]:
            for key, label in ASSESSMENT_LABELS.items():
                field(lines, label, ai["assessments"][key])
        if ai["evidence"]:
            lines.append("**证据依据：**\n")
            for evidence in ai["evidence"]:
                nature = NATURE_LABELS[evidence["nature"]]
                ref = f"{evidence['source_id']}/{evidence['location']}"
                lines.append(f"- **{nature}** · {md(ref)}：{md(evidence['support'])}")
            lines.append("")
        lines.append("</details>\n")

    lines.extend(source_details(paper))
    return "\n".join(lines)


def legacy_sort_key(paper, preference):
    categories = paper.get("categories") or []
    primary = categories[0] if isinstance(categories, list) and categories else ""
    rank = preference.index(primary) if primary in preference else len(preference)
    return rank, str(primary), str(paper.get("id", ""))


def render_report(data):
    lvr, legacy, missing = [], [], []
    for paper in data:
        if not isinstance(paper, dict):
            raise ValueError("Every record must be a paper object")
        ai = paper.get("AI")
        if isinstance(ai, dict) and "category" in ai:
            analysis = validate_tracking_response(ai, paper)
            lvr.append({**paper, "AI": analysis.model_dump(mode="json")})
        elif isinstance(ai, dict) and all(
            isinstance(ai.get(key), str) and ai[key].strip() for key in LEGACY_FIELDS
        ):
            legacy.append(paper)
        else:
            missing.append(paper)

    ordered, summary = finalize_tracking_batch(lvr)
    lines = ["# LVR 论文日报\n"]
    lines.append(
        f"本文件共 **{len(data)}** 篇：LVR 分析 **{len(ordered)}** 篇，"
        f"旧版摘要 **{len(legacy)}** 篇，无完整 AI 分析 **{len(missing)}** 篇。\n"
    )
    lines.append("LVR 分析依据标题、摘要及可用备注；论文链接不表示已阅读全文。分类把握不等于结果可信度。\n")
    lines.extend(["## LVR 分类统计\n", "| 类别 | 数量 |", "| --- | ---: |"])
    for category, label in CATEGORY_LABELS.items():
        lines.append(f"| {category} · {label} | {summary['category_counts'][category]} |")
    counts = summary["priority_counts"]
    lines.append(f"\n入选优先级：P0 **{counts['P0']}** · P1 **{counts['P1']}** · P2 **{counts['P2']}**。\n")
    lines.append("统计仅覆盖本文件中保存的记录，不代表抓取总量或跨日去重数量。\n")
    lines.append("## 深读入口\n")
    lines.append("按优先级、研究类别和论文 ID 排序，最多列出 5 篇；不包含 PENDING 和 EXCLUDE。\n")
    positions = {paper["id"]: (number, paper) for number, paper in enumerate(ordered, 1)}
    for paper_id in summary["deep_read_ids"]:
        number, paper = positions[paper_id]
        ai = paper["AI"]
        title = paper.get("title") or paper_id
        lines.append(f"- [{md(title)}](#paper-{number}) — {ai['category']} · {ai['priority']}")
    if not summary["deep_read_ids"]:
        lines.append("本文件没有符合条件的深读条目。")

    number = 0
    if ordered:
        lines.append("\n## LVR 论文分析\n")
        for number, paper in enumerate(ordered, 1):
            lines.append(render_lvr_paper(paper, number))

    if legacy:
        lines.extend(["\n## 旧版 AI 摘要\n", "以下记录保留原有五字段摘要，尚未按 LVR 规则分类。\n"])
        preference = [value.strip() for value in os.environ.get("CATEGORIES", "cs.CV,cs.CL").split(",")]
        for paper in sorted(legacy, key=lambda item: legacy_sort_key(item, preference)):
            number += 1
            block = paper_header(paper, number)
            for key, label in LEGACY_FIELDS.items():
                field(block, label, paper["AI"][key])
            block.extend(source_details(paper))
            lines.append("\n".join(block))

    if missing:
        lines.extend(["\n## 无完整 AI 分析的记录\n", "以下记录未计入 LVR 分类，也未被标为 PENDING 或 EXCLUDE。\n"])
        for paper in missing:
            number += 1
            block = paper_header(paper, number)
            block.extend(source_details(paper))
            lines.append("\n".join(block))

    return "\n".join(lines).rstrip() + "\n"


def load_papers(path):
    if path.suffix.lower() == ".json":
        content = json.loads(path.read_text(encoding="utf-8-sig"))
        data = content if isinstance(content, list) else [content]
    else:
        data = []
        with path.open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    data.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid JSON at {path}:{line_number}") from error
    if not all(isinstance(paper, dict) for paper in data):
        raise ValueError("Input must contain paper objects")
    return data


def default_output_path(path):
    stem = path.stem.split("_AI_enhanced_", 1)[0]
    return path.with_name(stem + ".md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True, help="JSONL data or smoke-test JSON")
    parser.add_argument("--output", type=Path, help="Optional Markdown output path")
    args = parser.parse_args()
    output = args.output or default_output_path(args.data)
    if output.resolve() == args.data.resolve():
        raise ValueError("Markdown output must not overwrite the input data")
    markdown = render_report(load_papers(args.data))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    print(f"Markdown saved: {output}")


if __name__ == "__main__":
    main()
