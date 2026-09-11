"""Closed Phase Authority and task construction for Miner Agents."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from ..agent.contracts import (
    AgentPhase,
    AgentTask,
    AnchorSynthesisAuthority,
    IssueCollectionAuthority,
    PhaseDefinition,
    RootCauseAuthority,
    RuleGenerationAuthority,
    RunLimits,
)
from ..models.analysis import GroundingPolicy, RootCauseAnalysis
from ..models.anchors import (
    AnchorIntent,
    AnchorPlan,
    AnchorSynthesisDelta,
)
from ..models.issue import IssueCollectionInfo
from ..models.vas import VASCoreInfo
from ..utils.config import (
    MINER_MAX_TURNS_ISSUE_COLLECTION,
    MINER_MAX_TURNS_PER_ANCHOR,
    MINER_MAX_TURNS_ROOT_CAUSE,
    MINER_MAX_TURNS_RULE_GENERATION,
)
from .examples import ExampleSuiteIntake
from .validation.analysis import validate_issue_checkout, validate_root_cause_analysis
from .validation.vas import validate_vas_core

_MINER_DIR = Path(__file__).resolve().parents[1]
_INSTRUCTIONS_DIR = _MINER_DIR / "instructions"
AST_GREP_SKILL_ROOT = _MINER_DIR / "skills" / "ast-grep"


def _instructions(name: str) -> str:
    return (_INSTRUCTIONS_DIR / name).read_text(encoding="utf-8")


def _validate_issue_collection(
    value: IssueCollectionInfo,
    authority: object,
    workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, IssueCollectionAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["Issue Collection output received the wrong Phase Authority"]
    return validate_issue_checkout(value, workspace_root=workspace_root)


def _validate_root_cause(
    value: RootCauseAnalysis,
    authority: object,
    _workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, RootCauseAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["RCA output received the wrong Phase Authority"]
    return validate_root_cause_analysis(
        value,
        source_root=authority.source_root,
        cases_dir=authority.cases_dir,
    )


def _validate_rule_generation(
    value: VASCoreInfo,
    authority: object,
    _workspace_root: Path,
) -> list[str]:
    if not isinstance(authority, RuleGenerationAuthority):  # pragma: no cover - AgentTask enforces this.
        return ["Rule Generation output received the wrong Phase Authority"]
    return validate_vas_core(
        value,
        source_root=authority.source_root,
        cases_dir=authority.cases_dir,
        root_cause=authority.root_cause,
        grounding_policy=authority.grounding_policy,
    )


def _validate_anchor_synthesis(
    value: AnchorSynthesisDelta,
    authority: object,
    _workspace_root: Path,
) -> tuple[str, ...]:
    if not isinstance(authority, AnchorSynthesisAuthority):  # pragma: no cover - AgentTask enforces this.
        return ("Anchor Synthesis output received the wrong Phase Authority",)
    intent = next(
        (item for item in authority.plan.intents if item.id == authority.target_anchor_id),
        None,
    )
    if intent is None:  # pragma: no cover - task factory constructs both together.
        return (f"unknown target intent {authority.target_anchor_id!r}",)
    if value.target_anchor_id != intent.id or value.query_weight > intent.behavior_weight:
        return (f"synthesis output drifted from target intent {intent.id!r}",)
    return ()


ISSUE_COLLECTION = PhaseDefinition(
    phase=AgentPhase.ISSUE_COLLECTION,
    agent_name="Issue Collector",
    description="Collect issue evidence, resolve revisions, and prepare one verified checkout.",
    instructions=_instructions("issue_collector.md"),
    output_type=IssueCollectionInfo,
    tools=(
        "fetch_cve",
        "fetch_github_issue",
        "parse_commit",
        "clone_repo",
        "search_commit_by_tag",
        "search_commit_by_time",
        "web_search",
        "web_fetch",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_ISSUE_COLLECTION, output_retries=2),
    validator=_validate_issue_collection,
)

ROOT_CAUSE = PhaseDefinition(
    phase=AgentPhase.ROOT_CAUSE,
    agent_name="Root Cause Analyzer",
    description="Establish the causal chain and write the declared Case Artifacts.",
    instructions=_instructions("root_cause_analyzer.md"),
    output_type=RootCauseAnalysis,
    tools=(
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "write_case_artifact",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_ROOT_CAUSE, output_retries=2),
    validator=_validate_root_cause,
)

RULE_GENERATION = PhaseDefinition(
    phase=AgentPhase.RULE_GENERATION,
    agent_name="Rule Generator",
    description="Own rule semantics and a complete queryless Anchor Plan.",
    instructions=_instructions("rule_generator.md"),
    output_type=VASCoreInfo,
    tools=("list_case_artifacts", "read_case_artifact", "synthesize_anchor_plan"),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_RULE_GENERATION, output_retries=2),
    validator=_validate_rule_generation,
)

AST_GREP_SYNTHESIS = PhaseDefinition(
    phase=AgentPhase.AST_GREP_SYNTHESIS,
    agent_name="AST-Grep Synthesizer",
    description="Compile one target intent into query-only fields.",
    instructions=_instructions("ast_grep_synthesizer.md"),
    output_type=AnchorSynthesisDelta,
    tools=(
        "list_src_files",
        "search_src_files",
        "read_src_file",
        "list_case_artifacts",
        "read_case_artifact",
        "list_skill_resources",
        "read_skill_resource",
        "run_ast_grep_query",
    ),
    limits=RunLimits(request_limit=MINER_MAX_TURNS_PER_ANCHOR, output_retries=2),
    validator=_validate_anchor_synthesis,
)

PHASE_DEFINITIONS = MappingProxyType(
    {definition.phase: definition for definition in (ISSUE_COLLECTION, ROOT_CAUSE, RULE_GENERATION, AST_GREP_SYNTHESIS)}
)


def _root_cause_input_policy(
    *,
    source: IssueCollectionInfo | ExampleSuiteIntake,
    source_root: Path,
) -> str:
    bound_root = source_root.resolve().as_posix()
    if isinstance(source, ExampleSuiteIntake):
        return f"""# Input Context

## Example Suite source

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- Discover the source files yourself with `list_src_files`, `search_src_files`, and `read_src_file`; do not rely on a precomputed file list.
- Analyze source code only. Ignore manifests, configuration, build metadata, and other non-source files as RCA evidence.
- The suite is a flexible collection of demonstrations that share one defect pattern. Files may be flat or nested, and there may be many bad/unsafe cases.
- Distinguish bad/unsafe from good/safe cases using source filenames, directories, comments, or labels only as navigation hints, then verify behavior in the source.
- Analyze and record every concrete bad/unsafe span. Use good/safe cases only as contrastive evidence for isolating the violated invariant; do not emit them as `buggy_components` or Case Artifacts.
- Mark the fixing invariant as inferred unless the suite directly demonstrates the corresponding safe behavior.
"""

    fixed_context = ""
    if source.fixed_commit is not None:
        fixed_context = f"""
- A verified comparison branch `fixed` exists at commit `{source.fixed_commit}`. The `read_patch_diff` tool compares `buggy` to `fixed`; call it without a path for a changed-file overview, then inspect the narrowest relevant path. Use the change to locate the cause quickly, and confirm the causal behavior in the buggy source.
"""
    else:
        fixed_context = """
- No verified fixed revision or patch-diff tool is available. Establish the causal chain from bounded source exploration and mark the fixing invariant explicitly as inferred.
"""
    return f"""# Input Context

## Repository checkout

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is a verified Git repository checkout. Its active `buggy` branch is commit `{source.buggy_commit}`; source reads therefore show the affected revision.
{fixed_context}"""


def _rule_input_policy(grounding: GroundingPolicy) -> str:
    if grounding is GroundingPolicy.BAD_SPAN_COVERAGE:
        return """# Input Policy

## Example Suite grounding

- Design complementary retrieval signals from the RCA and Case Artifacts.
- After synthesis, every Case Artifact and every bad-example source file named by an RCA component should be admitted by at least one Anchor with `query_weight >= 3`.
- RCA component spans identify defect evidence and relevant files; they are not mandatory Anchor match locations.
"""
    return """# Input Policy

## Issue grounding

- Design complementary retrieval signals from the RCA and Case Artifacts.
- After synthesis, every Case Artifact should be admitted by at least one Anchor with `query_weight >= 3`.
- Each enabled query must faithfully match its target behavior in at least one source file named by an RCA component; exact component-span overlap is not required.
"""


def _synthesis_input_policy(
    *,
    source_root: Path,
    grounding_policy: GroundingPolicy,
) -> str:
    bound_root = source_root.resolve().as_posix()
    if grounding_policy is GroundingPolicy.BAD_SPAN_COVERAGE:
        return f"""# Input Context

## Example Suite snapshot

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is the complete immutable Example Suite snapshot analyzed by RCA. Files may be flat or nested and may contain multiple bad/unsafe and good/safe demonstrations.
- Analyze source code only; ignore manifests, configuration, build metadata, and other non-source files.
- Ground the target behavior in at least one bad-example source file named by an RCA component. The faithful query match may be outside the exact component span.
- Good/safe source is contrastive evidence only and is not a required positive match.
- Interpret additional `src` matches as other suite examples; accept them only when they remain plausible instances of the target behavior.
"""
    return f"""# Input Context

## Repository checkout

- The Src Root bound to every src tool is `{bound_root}`. All tool paths are relative to this root.
- `src` is the affected repository source corpus analyzed by RCA.
- Ground the target behavior in at least one source file named by an RCA component. The faithful query match may be outside the exact component span.
- Treat other repository matches as precision evidence, not automatically as required positives or confirmed defects.
"""


def _render_block(label: str, value: str) -> str:
    """Render multiline evidence without JSON escaping or structural noise."""

    return f"{label}:\n{value.strip()}"


def _render_root_cause(root_cause: RootCauseAnalysis) -> str:
    sections = [
        f"Language: {root_cause.language.value}",
        _render_block("Root cause summary", root_cause.root_cause_summary),
        _render_block("Analysis", root_cause.analysis),
        "Buggy components:",
    ]
    for index, component in enumerate(root_cause.buggy_components, start=1):
        sections.append(
            "\n".join(
                (
                    f"{index}. File: {component.file}",
                    f"   Lines: {component.start_line}-{component.end_line}",
                    f"   Role: {component.role}",
                    f"   Source snippet:\n{component.snippet.strip()}",
                )
            )
        )
    sections.extend(
        (
            _render_block("Fixing pattern", root_cause.fixing_pattern),
            "Declared Case Artifacts:\n"
            + "\n".join(f"- {name}" for name in root_cause.extracted_case_files),
        )
    )
    return "\n\n".join(sections)


def _render_issue_evidence(source: IssueCollectionInfo) -> str:
    return "\n\n".join(
        (
            f"Issue ID: {source.issue_id}",
            _render_block("Issue summary", source.issue_summary),
            _render_block("Issue details", source.issue_details),
            f"Repository: {source.repo_url}",
            f"Buggy commit: {source.buggy_commit}",
            f"Fixed commit: {source.fixed_commit or 'not identified'}",
        )
    )


def _render_target_intent(intent: AnchorIntent) -> str:
    return "\n\n".join(
        (
            f"ID: {intent.id}",
            f"Behavior weight: {intent.behavior_weight}",
            _render_block("Behavior", intent.behavior),
            _render_block("Inspect hint", intent.inspect_hint),
            "Required Case Artifacts:\n"
            + "\n".join(f"- {name}" for name in intent.required_cases),
        )
    )


def make_issue_collection_task(
    issue_reference: str,
    *,
    workspace_root: Path,
    task_id: str | None = None,
) -> AgentTask[IssueCollectionInfo]:
    reference = issue_reference.strip()
    if not reference:
        raise ValueError("issue_reference must be non-empty")
    return AgentTask(
        task_id=task_id or f"issue-collection:{reference}",
        definition=ISSUE_COLLECTION,
        authority=IssueCollectionAuthority(reference),
        prompt=(
            "Collect verified issue evidence and prepare the checkout for root-cause analysis.\n\n"
            f"Issue reference: {reference}\n"
        ),
        workspace_root=workspace_root,
    )


def make_root_cause_task(
    source: IssueCollectionInfo | ExampleSuiteIntake,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    task_id: str | None = None,
) -> AgentTask[RootCauseAnalysis]:
    is_issue = isinstance(source, IssueCollectionInfo)
    fixed_revision = is_issue and source.fixed_commit is not None
    repo_path = source_root if is_issue else None
    if is_issue:
        prompt = (
            "Analyze the supplied issue and extract minimal defective Case Artifacts.\n\n"
            + _render_issue_evidence(source)
            + "\n"
        )
    else:
        prompt = (
            "Analyze the supplied Example Suite directory and extract minimal defective "
            "Case Artifacts from source code only.\n\n"
            f"Example Suite directory: {source_root.resolve().as_posix()}\n"
        )
    authority = RootCauseAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        repo_path=repo_path,
        fixed_diff=bool(fixed_revision),
    )
    return AgentTask(
        task_id=task_id or ("root-cause" if is_issue else f"root-cause:{source.exp_id}"),
        definition=ROOT_CAUSE,
        authority=authority,
        prompt=prompt,
        workspace_root=workspace_root,
        input_policy=_root_cause_input_policy(
            source=source,
            source_root=source_root,
        ),
        extra_tools=("read_patch_diff",) if authority.fixed_diff else (),
    )


def make_rule_generation_task(
    root_cause: RootCauseAnalysis,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    task_id: str | None = None,
) -> AgentTask[VASCoreInfo]:
    authority = RuleGenerationAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        root_cause=root_cause,
    )
    return AgentTask(
        task_id=task_id or "rule-generation",
        definition=RULE_GENERATION,
        authority=authority,
        prompt=(
            "Define repository-independent rule semantics and a queryless Anchor Plan from the authoritative RCA.\n\n"
            "[Root Cause Analysis]\n"
            + _render_root_cause(root_cause)
            + "\n"
        ),
        workspace_root=workspace_root,
        input_policy=_rule_input_policy(grounding_policy),
    )


def make_ast_grep_synthesis_task(
    plan: AnchorPlan,
    intent: AnchorIntent,
    *,
    workspace_root: Path,
    source_root: Path,
    cases_dir: Path,
    grounding_policy: GroundingPolicy,
    root_cause: RootCauseAnalysis,
    iteration: int = 1,
    task_id: str | None = None,
    limits: RunLimits | None = None,
) -> AgentTask[AnchorSynthesisDelta]:
    if iteration < 1:
        raise ValueError("iteration must be positive")
    position = next(
        (index for index, item in enumerate(plan.intents, start=1) if item.id == intent.id),
        None,
    )
    if position is None:
        raise ValueError(f"intent {intent.id!r} is not present in the Anchor Plan")
    authority = AnchorSynthesisAuthority(
        source_root=source_root,
        cases_dir=cases_dir,
        grounding_policy=grounding_policy,
        root_cause=root_cause,
        plan=plan,
        target_anchor_id=intent.id,
        skill_root=AST_GREP_SKILL_ROOT,
    )
    requirement = (
        "Match the target behavior in at least one source file named by an "
        "RCA component; exact component-span overlap is not required."
    )
    return AgentTask(
        task_id=task_id or f"ast-grep-synthesis:{iteration}:{intent.id}",
        definition=replace(
            AST_GREP_SYNTHESIS,
            agent_name=f"AST-Grep Synthesizer [{iteration}.{position}/{len(plan.intents)}]",
        ),
        authority=authority,
        prompt=(
            f'Generate and validate only the ast-grep query for target anchor "{intent.id}"; '
            "keep it within that anchor's behavior.\n\n"
            "[Target AnchorIntent]\n"
            + _render_target_intent(intent)
            + "\n\n[Root Cause Analysis]\n"
            + _render_root_cause(root_cause)
            + "\n"
        ),
        workspace_root=workspace_root,
        input_policy=_synthesis_input_policy(
            source_root=source_root,
            grounding_policy=grounding_policy,
        ),
        limit_override=limits,
    )


__all__ = [
    "AST_GREP_SKILL_ROOT",
    "AST_GREP_SYNTHESIS",
    "ISSUE_COLLECTION",
    "PHASE_DEFINITIONS",
    "ROOT_CAUSE",
    "RULE_GENERATION",
    "make_ast_grep_synthesis_task",
    "make_issue_collection_task",
    "make_root_cause_task",
    "make_rule_generation_task",
]
