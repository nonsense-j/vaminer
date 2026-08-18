from io import StringIO

from rich.console import Console

from src.miner.utils.log import RuntimeLog, run_log_file


def test_runtime_log_mirrors_one_redacted_rich_panel_to_stdout_and_run_file(tmp_path):
    stdout = StringIO()
    runtime_log = RuntimeLog(
        console=Console(file=stdout, width=80, color_system=None, force_terminal=False),
        width=80,
    )

    with run_log_file(
        tmp_path,
        "VAS-0001",
        input_id="input",
        trace_id="trace",
        runtime="test",
    ) as path:
        event = runtime_log.event(
            "Root Cause Analyzer",
            "thinking",
            {"authorization": "Bearer top-secret-token", "plan": "inspect the copy"},
        )

    rendered = stdout.getvalue()
    persisted = path.read_text(encoding="utf-8")
    assert "🤖 Root Cause Analyzer — 🧠 Thinking" in rendered
    assert "inspect the copy" in rendered
    assert "top-secret-token" not in rendered
    assert rendered == persisted
    assert event.content["authorization"] == "<redacted>"


def test_runtime_log_can_disable_stdout_for_mcp_stdio(capsys):
    RuntimeLog(emit_console=False).event("AST-Grep Synthesizer", "message", "nested run")

    assert capsys.readouterr().out == ""
