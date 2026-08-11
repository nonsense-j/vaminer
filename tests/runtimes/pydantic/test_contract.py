"""Pure construction tests for the Pydantic AI runtime contract."""

from pathlib import Path
from typing import get_args, get_type_hints

from pydantic_ai.models.test import TestModel

from src.miner.mining.tasks import make_rule_generation_task
from src.miner.models import AnchorSynthesisRequest
from src.miner.runtimes.pydantic.runtime import PydanticAIRuntime
from src.miner.runtimes.shared.synthesis import AnchorSynthesisContext
from tests.support.factories import (
    BEHAVIOR,
    INSPECT_HINT,
    analysis_subject,
    root_cause,
)


def test_synthesizer_exposes_src_cases_and_workspace_without_running_agent(
    tmp_path: Path,
):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    rca = root_cause()
    parent = make_rule_generation_task(
        rca,
        workspace_root=tmp_path,
        source_root=source_root,
        repo_path=source_root,
        cases_dir=cases_dir,
        analysis_subject=analysis_subject(source_root, cases_dir),
    )
    request = AnchorSynthesisRequest.model_validate(
        {
            "root_cause": rca.model_dump(mode="json"),
            "summary": "Dangerous operations require their guard.",
            "anchor_intents": [
                {
                    "id": "danger-call",
                    "behavior_weight": 5,
                    "behavior": BEHAVIOR,
                    "inspect_hint": INSPECT_HINT,
                    "required_cases": ["case1.c"],
                }
            ],
        }
    )
    task = AnchorSynthesisContext.from_task(parent).child_task(
        request,
        request.anchor_intents[0],
    )
    model = TestModel()

    agent = PydanticAIRuntime(model=model).build_agent(task, model=model)

    assert [toolset.prefix for toolset in agent._user_toolsets] == [
        "workspace",
        "src",
        "cases",
    ]
    runner = agent._function_toolset.tools["run_ast_grep_query"].function
    assert get_args(get_type_hints(runner)["target"]) == ("src", "cases")
