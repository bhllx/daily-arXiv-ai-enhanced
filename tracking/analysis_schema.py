from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


SCHEMA_VERSION = "lvr-analysis-v1"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Category = Literal["MAIN", "METHOD", "EXPLORATORY", "PENDING", "EXCLUDE"]
TrackCategory = Literal["MAIN", "METHOD", "EXPLORATORY"]
Priority = Literal["P0", "P1", "P2", "NONE"]


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(SchemaModel):
    source_id: Text = Field(description="输入材料的来源编号，不能自行编造")
    location: Text = Field(description="输入中的句子、段落编号或可定位短片段")
    support: Text = Field(description="该材料具体支持什么判断，优先简短转述")
    nature: Literal["author_claim", "source_description", "transfer_hypothesis"] = Field(
        description="区分作者声称、来源描述和自身迁移假设"
    )


class Assessments(SchemaModel):
    research_relevance: Text = Field(description="与研究问题的具体联系")
    topic_impact: Text = Field(description="对选题或实验设计的潜在影响")
    evidence_sufficiency: Text = Field(description="现有材料能支持和不能支持什么")
    result_credibility: Text = Field(
        description="区分结果声称与核验；材料不足时写 unknown，不能因摘要有数值就视为已验证"
    )
    reproducibility_readiness: Text = Field(
        description="依据输入的资源事实说明复现准备度，信息缺失时写 unknown"
    )


class ResearchBridge(SchemaModel):
    mode: Literal["transfer", "reframing"] = Field(
        description="METHOD 使用 transfer；EXPLORATORY 使用 reframing"
    )
    original_problem: Text | None = Field(default=None, description="原论文解决的问题")
    mechanism: Text | None = Field(default=None, description="可迁移的方法机制")
    lvr_bottleneck: Text | None = Field(default=None, description="对应的具体 LVR 瓶颈")
    adaptation: Text | None = Field(default=None, description="具体如何迁移，明确属于假设")
    failure_condition: Text | None = Field(default=None, description="迁移可能失败的条件")
    borrowed_concept: Text | None = Field(default=None, description="借鉴的概念")
    reframed_problem: Text | None = Field(default=None, description="如何重述一个 LVR 问题")
    first_check: Text | None = Field(default=None, description="首先验证什么")

    @model_validator(mode="after")
    def check_bridge(self):
        transfer_fields = (
            "original_problem", "mechanism", "lvr_bottleneck",
            "adaptation", "failure_condition",
        )
        reframing_fields = ("borrowed_concept", "reframed_problem", "first_check")
        required = transfer_fields if self.mode == "transfer" else reframing_fields
        unused = reframing_fields if self.mode == "transfer" else transfer_fields
        for name in required:
            if getattr(self, name) is None:
                raise ValueError(f"{self.mode} requires {name}")
        for name in unused:
            if getattr(self, name) is not None:
                raise ValueError(f"{self.mode} requires {name}=null")
        return self


class UpdateAnalysis(SchemaModel):
    change_type: Literal["material_update", "minor_update", "unchanged", "unknown"]
    comparison_scope: Literal[
        "title", "abstract", "full_text", "code", "project_page"
    ] | None = Field(default=None, description="仅判断实际提供的比较材料，不推定全文变化")
    material_delta: Text | None = Field(default=None, description="有依据的变化说明")
    evidence: list[Evidence] = Field(default_factory=list, description="旧、新材料比较依据")

    @model_validator(mode="after")
    def check_update(self):
        if self.change_type == "unknown":
            if self.material_delta is not None:
                raise ValueError("unknown update requires material_delta=null")
        else:
            if self.comparison_scope is None or self.material_delta is None:
                raise ValueError("known update requires scope and material_delta")
            if not any(e.nature != "transfer_hypothesis" for e in self.evidence):
                raise ValueError("known update requires source evidence")
        return self


class TrackingAnalysis(SchemaModel):
    """模型只返回逐篇分析；身份、资源事实和历史状态由程序合并。"""

    category: Category
    priority: Priority | None = Field(
        description="入选为 P0/P1/P2，PENDING 为 null，EXCLUDE 为 NONE"
    )
    classification_confidence: Literal["high", "medium", "low"] = Field(
        description="对分类判断的把握，不表示论文结果已被验证"
    )
    candidate_category: TrackCategory | None = Field(
        default=None, description="仅 PENDING 填写最可能的候选类别"
    )
    topic_tags: list[Text] = Field(default_factory=list)
    core_mechanism: Text | None = Field(default=None, description="来源支持的核心机制")
    reason_to_track: Text | None = Field(default=None, description="具体追踪理由或待核验线索")
    lvr_connection: Text | None = Field(default=None, description="与 LVR 问题的具体联系")
    transfer_path_or_reframing: ResearchBridge | None = Field(default=None)
    main_assumption_or_risk: Text | None = Field(default=None)
    what_to_check_next: Text | None = Field(default=None)
    assessments: Assessments | None = Field(default=None)
    evidence: list[Evidence] = Field(default_factory=list)
    update_analysis: UpdateAnalysis | None = Field(
        default=None, description="仅输入提供旧、新比较材料时填写，否则 null"
    )
    exclusion_reason: Text | None = Field(default=None)

    @model_validator(mode="after")
    def check_category_rules(self):
        tracked = self.category in ("MAIN", "METHOD", "EXPLORATORY")

        if tracked:
            if self.priority not in ("P0", "P1", "P2"):
                raise ValueError("tracked category requires P0/P1/P2")
            for name in (
                "core_mechanism", "reason_to_track", "lvr_connection",
                "main_assumption_or_risk", "what_to_check_next", "assessments",
            ):
                if getattr(self, name) is None:
                    raise ValueError(f"tracked category requires {name}")
            if not any(e.nature != "transfer_hypothesis" for e in self.evidence):
                raise ValueError("tracked category requires source evidence")

        if self.category == "PENDING":
            if self.priority is not None:
                raise ValueError("PENDING requires priority=null")
            for name in ("candidate_category", "reason_to_track", "what_to_check_next"):
                if getattr(self, name) is None:
                    raise ValueError(f"PENDING requires {name}")
        elif self.candidate_category is not None:
            raise ValueError("candidate_category is only allowed for PENDING")

        if self.category == "EXCLUDE":
            if self.priority != "NONE" or self.exclusion_reason is None:
                raise ValueError("EXCLUDE requires NONE and exclusion_reason")
        elif self.exclusion_reason is not None:
            raise ValueError("exclusion_reason is only allowed for EXCLUDE")

        bridge = self.transfer_path_or_reframing
        if self.category == "METHOD":
            if bridge is None or bridge.mode != "transfer":
                raise ValueError("METHOD requires a complete transfer bridge")
        elif self.category == "EXPLORATORY":
            if bridge is None or bridge.mode != "reframing":
                raise ValueError("EXPLORATORY requires a complete reframing bridge")
        elif bridge is not None:
            raise ValueError("this category requires transfer_path_or_reframing=null")

        return self
