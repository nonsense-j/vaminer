"""Issue-to-VAS workflow implemented with Pydantic AI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .anchors.review import render_root_cause_analysis, review_anchors
from .configs import MINER_LOG_DIR, flush_tracing, make_agent_usage_limits, trace_pipeline
from .core.agents import (
    make_ast_grep_synthesizer,
    make_issue_collector,
    make_root_cause_analyzer,
    make_rule_generator,
)
from .core.context import MinerContext
from .utils.cache import (
    AgentCache,
    load_collection_cache,
    load_root_cause_cache,
    load_rule_cache,
)
from .utils.hooks import make_cli_hooks
from .utils.logger import logger, run_log_file
from .utils.models import (
    IssueCollectionInfo,
    VASCoreInfo,
    VASFull,
    VASSource,
)
from .utils.workspace import Workspace

RC_ANALYSIS_INPUT_TEMPLATE = """\
Analyze the root cause based on the collected issue information.

- Issue summary: {issue_summary}
- Issue details: {issue_details}
- Fixed branch available: {fixed_branch_available}
- Repository path: {repo_path}
"""

RULE_GEN_INPUT_TEMPLATE = """\
Generate one VAS rule from the authoritative typed RCA.

- Root cause analysis
{root_cause_analysis}
"""


def assemble_vas(vas_id: str, collection: IssueCollectionInfo, core: VASCoreInfo) -> VASFull:
    """Assemble the public VAS schema from validated stage outputs."""
    return VASFull(
        vas_id=vas_id,
        category=core.category,
        language=core.language,
        sources=[
            VASSource(
                issue_id=collection.issue_id,
                repo_url=collection.repo_url,
                buggy_commit=collection.buggy_commit,
                fixed_commit=collection.fixed_commit,
                root_cause_summary=core.root_cause_summary,
            )
        ],
        summary=core.summary,
        scenarios=core.scenarios,
        anchors=core.anchors,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "issue_input",
        nargs="+",
        help="Issue input(s), e.g. a CVE ID or GitHub issue URL.",
    )
    parser.add_argument("--use-cache", action="store_true", help="Use valid cached agent outputs.")
    return parser.parse_args()


def parse_issue_inputs(issue_inputs: list[str]) -> list[str]:
    return [issue for item in issue_inputs for issue in (part.strip() for part in item.split(",")) if issue]


async def _execute_issue_workflow(
    issue_input: str,
    args,
    *,
    vas_id: str,
    ws: Workspace,
) -> VASFull:
    cli_hooks = make_cli_hooks()
    additional_capabilities = [cli_hooks]
    logger.info("Entering issue collection")
    issue_cache = AgentCache("Issue Collector", ws.cache_dir)
    collection = load_collection_cache(issue_cache, workspace_root=ws.root) if args.use_cache else None
    if collection is None:
        collector = make_issue_collector(additional_capabilities=additional_capabilities)
        result = await collector.run(
            f"issue_input: {issue_input}",
            deps=MinerContext(workspace_root=ws.root),
            usage_limits=make_agent_usage_limits(),
        )
        collection = result.output
        issue_cache.set(collection)
    else:
        logger.info("Issue collection loaded from cache")

    repo_path = Path(collection.repo_path)
    logger.info("Issue collection completed. Repo path: %s", repo_path)

    logger.info("Entering root cause analysis")
    rca_cache = AgentCache("Root Cause Analyzer", ws.cache_dir)
    root_cause = (
        load_root_cause_cache(
            rca_cache,
            repo_path=repo_path,
            cases_dir=ws.cases_dir,
        )
        if args.use_cache
        else None
    )
    if root_cause is None:
        ws.clear_cases()
        analyzer = make_root_cause_analyzer(
            repo_path,
            ws.cases_dir,
            additional_capabilities=additional_capabilities,
        )
        result = await analyzer.run(
            RC_ANALYSIS_INPUT_TEMPLATE.format(
                issue_summary=collection.issue_summary,
                issue_details=collection.issue_details,
                fixed_branch_available="Yes" if collection.fixed_commit else "No",
                repo_path=repo_path,
            ),
            deps=MinerContext(
                workspace_root=ws.root,
                repo_path=repo_path,
                cases_dir=ws.cases_dir,
            ),
            usage_limits=make_agent_usage_limits(),
        )
        root_cause = result.output
        rca_cache.set(root_cause)
    else:
        logger.info("Root cause analysis loaded from cache")
    ws.save_analysis(render_root_cause_analysis(root_cause))
    logger.info("Root cause analysis completed")

    logger.info("Entering rule generation")
    rule_cache = AgentCache("Rule Generator", ws.cache_dir)
    core = (
        load_rule_cache(
            rule_cache,
            repo_path=repo_path,
            cases_dir=ws.cases_dir,
            root_cause=root_cause,
        )
        if args.use_cache
        else None
    )
    if core is None:
        synthesizer = make_ast_grep_synthesizer(
            repo_path,
            ws.cases_dir,
            additional_capabilities=additional_capabilities,
        )
        generator = make_rule_generator(
            ws.cases_dir,
            synthesizer,
            additional_capabilities=additional_capabilities,
        )
        result = await generator.run(
            RULE_GEN_INPUT_TEMPLATE.format(
                root_cause_analysis=root_cause.model_dump_json(indent=2),
            ),
            deps=MinerContext(
                workspace_root=ws.root,
                repo_path=repo_path,
                cases_dir=ws.cases_dir,
                root_cause=root_cause,
            ),
            usage_limits=make_agent_usage_limits(),
        )
        core = result.output
        rule_cache.set(core)
    else:
        logger.info("Rule generation loaded from cache")
    logger.info("Rule generation completed. Anchors: %s", len(core.anchors))

    review_anchors(
        ws.vas_id,
        core,
        repo_path,
        ws.cases_dir,
        output_path=ws.anchor_review_path,
    )
    logger.info("Anchor review saved: %s", ws.anchor_review_path)

    vas = assemble_vas(vas_id, collection, core)
    rule_path = ws.save_rule(vas)
    logger.info("VAS saved: %s", rule_path)
    return vas


async def run_issue_workflow(issue_input: str, args) -> VASFull:
    """Run one complete issue pipeline under a single active trace root."""
    vas_id = Workspace.get_vas_id(issue_input)
    ws = Workspace.from_id(vas_id)
    with run_log_file(MINER_LOG_DIR, vas_id) as log_path:
        logger.info("Starting miner workflow for issue input: %s", issue_input)
        logger.info("Storing run log in %s", log_path)
        try:
            with trace_pipeline(
                issue_input=issue_input,
                vas_id=vas_id,
            ) as pipeline_span:
                vas = await _execute_issue_workflow(
                    issue_input,
                    args,
                    vas_id=vas_id,
                    ws=ws,
                )
                if pipeline_span is not None:
                    pipeline_span.update(output=vas.model_dump(mode="json"))
                return vas
        except BaseException:
            logger.exception("Miner workflow failed for issue input: %s", issue_input)
            raise


async def main(args):
    try:
        issue_inputs = parse_issue_inputs(args.issue_input)
        if not issue_inputs:
            raise ValueError("At least one non-empty issue input is required.")
        results = []
        for issue_input in issue_inputs:
            results.append(await run_issue_workflow(issue_input, args))
        return results[0] if len(results) == 1 else results
    finally:
        flush_tracing()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
