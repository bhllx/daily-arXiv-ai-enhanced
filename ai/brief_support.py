"""Prompt and single-request provider adapter for short paper briefs."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tracking.brief_schema import BriefAnalysis, SCHEMA_VERSION


class BriefFailure(Exception):
    def __init__(self, stage, error, *, raw=None, usage=None, fatal=False, api_called=True):
        super().__init__(str(error))
        self.stage = stage
        self.error = error
        self.raw = raw
        self.usage = usage
        self.fatal = fatal
        self.api_called = api_called


def build_brief_input(item):
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Paper title must be a non-empty string")
    summary = item.get("summary")
    payload = {"id": item["id"], "title": title, "abstract": "" if summary is None else summary}
    if not isinstance(payload["abstract"], str):
        raise ValueError("Paper summary must be text or null")
    if item.get("comment") is not None:
        if not isinstance(item["comment"], str):
            raise ValueError("Paper comment must be text or null")
        payload["comment"] = item["comment"]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_brief_chain(model_name, language, max_output_tokens, request_timeout):
    from langchain_core.messages import SystemMessage
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI
    from ai.runtime import build_chat_openai_kwargs

    if not 128 <= max_output_tokens <= 8192 or not 10 <= request_timeout <= 600:
        raise ValueError("Output token limit must be 128..8192; timeout must be 10..600 seconds")
    profile = (ROOT / "tracking/brief_profile.md").read_text(encoding="utf-8").strip()
    if not profile:
        raise ValueError("brief_profile.md is empty")
    kwargs = build_chat_openai_kwargs(
        model_name=model_name,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    hostname = urlparse(kwargs["base_url"]).hostname or ""
    if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
        kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "thinking": {"type": "disabled"}}
    # One generated response per selected paper, including SDK-level retries.
    kwargs.update(max_retries=0, max_tokens=max_output_tokens, timeout=request_timeout)
    model = ChatOpenAI(**kwargs).with_structured_output(
        BriefAnalysis, method="function_calling", include_raw=True,
    )
    prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=profile),
        ("human", "输出语言：{language}\n当前论文材料：\n{content}"),
    ])
    provenance = {
        "schema_version": SCHEMA_VERSION,
        "model": model_name.strip(), "language": language,
        "profile_sha256": hashlib.sha256(profile.encode("utf-8")).hexdigest(),
        "schema_sha256": hashlib.sha256(json.dumps(BriefAnalysis.model_json_schema(), sort_keys=True).encode("utf-8")).hexdigest(),
        "max_output_tokens": max_output_tokens,
    }
    return prompt | model, provenance


def response_info(raw):
    if raw is None:
        return None, None
    metadata = getattr(raw, "response_metadata", {}) or {}
    usage = getattr(raw, "usage_metadata", None)
    if not usage:
        tokens = metadata.get("token_usage") or {}
        usage = {
            "input_tokens": tokens.get("prompt_tokens"),
            "output_tokens": tokens.get("completion_tokens"),
            "total_tokens": tokens.get("total_tokens"),
        } if tokens else None
    snapshot = {
        "content": getattr(raw, "content", None),
        "tool_calls": (getattr(raw, "additional_kwargs", {}) or {}).get("tool_calls"),
        "parsed_tool_calls": getattr(raw, "tool_calls", None),
        "invalid_tool_calls": getattr(raw, "invalid_tool_calls", None),
        "finish_reason": metadata.get("finish_reason"),
    }
    return snapshot, usage


def analyze_once(chain, item, language):
    try:
        content = build_brief_input(item)
    except (ValueError, TypeError, KeyError) as error:
        raise BriefFailure("input", error, api_called=False) from error
    try:
        packet = chain.invoke({"language": language, "content": content})
    except Exception as error:
        # Stop launching further paid work when credentials/endpoint are unusable.
        fatal = getattr(error, "status_code", None) in (401, 403, 404)
        raise BriefFailure("request", error, fatal=fatal) from error
    if not isinstance(packet, dict):
        raise BriefFailure("parse", ValueError("Missing structured response envelope"))
    raw, usage = response_info(packet.get("raw"))
    error = packet.get("parsing_error")
    if error is not None:
        stage = "validate" if isinstance(error, ValidationError) else "parse"
        raise BriefFailure(stage, error, raw=raw, usage=usage)
    try:
        parsed = packet.get("parsed")
        analysis = BriefAnalysis.model_validate(
            parsed.model_dump() if isinstance(parsed, BriefAnalysis) else parsed
        )
    except (ValueError, TypeError) as error:
        raise BriefFailure("validate", error, raw=raw, usage=usage) from error
    return analysis.model_dump(mode="json"), usage
