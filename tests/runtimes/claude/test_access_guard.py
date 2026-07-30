from pathlib import Path

from src.miner.runtimes.claude.access_guard import validate_event


def _write_event(workspace: Path, file_path: str) -> dict[str, object]:
    return {
        "tool_name": "Write",
        "cwd": str(workspace),
        "tool_input": {
            "file_path": file_path,
            "content": "int main(void) { return 0; }\n",
        },
    }


def test_glob_accepts_the_complete_vas_workspace(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "cases").mkdir()
    event = {
        "tool_name": "Glob",
        "cwd": str(tmp_path),
        "tool_input": {
            "path": str(tmp_path),
            "pattern": "cases/*",
        },
    }

    assert validate_event(event, roots=(tmp_path.resolve(),)) is None


def test_glob_rejects_paths_outside_the_vas_workspace(tmp_path: Path):
    workspace = tmp_path / "VAS-0001"
    workspace.mkdir()
    event = {
        "tool_name": "Glob",
        "cwd": str(workspace),
        "tool_input": {
            "path": str(tmp_path),
            "pattern": "*",
        },
    }

    assert (
        validate_event(event, roots=(workspace.resolve(),))
        == "Glob is limited to workspace_root"
    )


def test_write_accepts_only_a_top_level_case_file(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()

    reason = validate_event(
        _write_event(tmp_path, "cases/case1.c"),
        roots=(cases_dir,),
        write_root=cases_dir.resolve(),
    )

    assert reason is None


def test_write_rejects_a_source_file(tmp_path: Path):
    source_root = tmp_path / "source"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()

    reason = validate_event(
        _write_event(tmp_path, str(source_root / "bug.c")),
        roots=(source_root, cases_dir),
        write_root=cases_dir.resolve(),
    )

    assert reason == "Write is limited to top-level files directly under cases_dir"


def test_write_rejects_a_nested_case_file(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()

    reason = validate_event(
        _write_event(tmp_path, "cases/nested/case1.c"),
        roots=(cases_dir,),
        write_root=cases_dir.resolve(),
    )

    assert reason == "Write is limited to top-level files directly under cases_dir"


def test_write_rejects_a_symlink_escape(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    outside = tmp_path / "outside.c"
    cases_dir.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    (cases_dir / "case1.c").symlink_to(outside)

    reason = validate_event(
        _write_event(tmp_path, "cases/case1.c"),
        roots=(cases_dir,),
        write_root=cases_dir.resolve(),
    )

    assert reason == "Write is limited to top-level files directly under cases_dir"


def test_write_fails_closed_without_a_configured_root(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()

    reason = validate_event(
        _write_event(tmp_path, "cases/case1.c"),
        roots=(cases_dir,),
    )

    assert reason == "Write is not configured for this task"
