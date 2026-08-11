from pathlib import Path

from src.miner.runtimes.claude.access_guard import validate_event


def _read_event(workspace: Path, tool_name: str, path: Path) -> dict[str, object]:
    field = "file_path" if tool_name == "Read" else "path"
    tool_input: dict[str, object] = {field: str(path)}
    if tool_name in {"Glob", "Grep"}:
        tool_input["pattern"] = "**/*" if tool_name == "Glob" else "needle"
    return {
        "tool_name": tool_name,
        "cwd": str(workspace),
        "tool_input": tool_input,
    }


def _write_event(workspace: Path, file_path: str) -> dict[str, object]:
    return {
        "tool_name": "Write",
        "cwd": str(workspace),
        "tool_input": {
            "file_path": file_path,
            "content": "int main(void) { return 0; }\n",
        },
    }


def test_glob_rejects_paths_outside_the_vas_workspace(tmp_path: Path):
    workspace = tmp_path / "VAS-0001"
    workspace.mkdir()
    assert (
        validate_event(
            _read_event(workspace, "Glob", tmp_path),
            roots=(workspace.resolve(),),
        )
        == f"Glob is limited to the configured read roots: {workspace.resolve()}"
    )


def test_rule_generator_can_read_cases_and_gets_actionable_source_denial(
    tmp_path: Path,
):
    source_root = tmp_path / "src" / "owner" / "repo"
    cases_dir = tmp_path / "cases"
    source_root.mkdir(parents=True)
    cases_dir.mkdir()
    source_file = source_root / "bug.c"
    case_file = cases_dir / "case1.c"
    source_file.write_text("bug\n", encoding="utf-8")
    case_file.write_text("case\n", encoding="utf-8")
    kwargs = {
        "roots": (cases_dir.resolve(),),
        "restricted_roots": (source_root.resolve(),),
        "role": "rule-generator",
    }

    assert validate_event(_read_event(tmp_path, "Read", case_file), **kwargs) is None
    denied = validate_event(_read_event(tmp_path, "Read", source_file), **kwargs)

    assert denied is not None
    assert f"src area ({source_root.resolve()})" in denied
    assert "Use the authoritative RCA directly" in denied
    assert "mcp__vaminer__synthesize_ast_grep_anchors" in denied


def test_write_accepts_only_a_top_level_case_file(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    assert (
        validate_event(
            _write_event(tmp_path, "cases/case1.c"),
            roots=(cases_dir,),
            write_root=cases_dir.resolve(),
        )
        is None
    )


def test_write_rejects_source_nested_and_symlink_escapes(tmp_path: Path):
    source_root = tmp_path / "src"
    cases_dir = tmp_path / "cases"
    source_root.mkdir()
    cases_dir.mkdir()
    outside = tmp_path / "outside.c"
    outside.write_text("outside\n", encoding="utf-8")
    (cases_dir / "case1.c").symlink_to(outside)

    for path in (
        str(source_root / "bug.c"),
        "cases/nested/case1.c",
        "cases/case1.c",
    ):
        assert (
            validate_event(
                _write_event(tmp_path, path),
                roots=(source_root, cases_dir),
                write_root=cases_dir.resolve(),
            )
            == "Write is limited to top-level files directly under cases_dir"
        )
