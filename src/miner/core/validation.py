"""Deterministic validation shared by agent output functions and cache loading."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from git import Repo
from git.exc import GitError

from ..anchors.scanner import AnchorMatch, AnchorScanError, scan_anchors
from ..utils.models import (
    AnalysisSubject,
    Anchor,
    AnchorIntent,
    AnchorSynthesisRequest,
    AnchorSynthesisResult,
    AnchorSynthesisRunRequest,
    AnchorSynthesisRunResult,
    AstGrepLanguage,
    CaseCoverage,
    IssueCollectionInfo,
    RepoEvidence,
    RootCauseAnalysis,
    VASCoreInfo,
)

_CASE_RE = re.compile(r"^case(?P<case>\d+)(?:_var(?P<variant>\d+))?(?P<suffix>\.[A-Za-z0-9]+)$")
_LANGUAGE_SUFFIXES: dict[AstGrepLanguage, set[str]] = {
    AstGrepLanguage.C: {".c"},
    AstGrepLanguage.CPP: {".cc", ".cpp", ".cxx"},
    AstGrepLanguage.CSHARP: {".cs"},
    AstGrepLanguage.GO: {".go"},
    AstGrepLanguage.JAVA: {".java"},
    AstGrepLanguage.JAVASCRIPT: {".js", ".mjs", ".cjs"},
    AstGrepLanguage.JSX: {".jsx"},
    AstGrepLanguage.KOTLIN: {".kt", ".kts"},
    AstGrepLanguage.PHP: {".php"},
    AstGrepLanguage.PYTHON: {".py"},
    AstGrepLanguage.RUBY: {".rb"},
    AstGrepLanguage.RUST: {".rs"},
    AstGrepLanguage.SCALA: {".scala"},
    AstGrepLanguage.SWIFT: {".swift"},
    AstGrepLanguage.TSX: {".tsx"},
    AstGrepLanguage.TYPESCRIPT: {".ts"},
}
_VALIDATION_MATCH_LOCATION_LIMIT = 12
_VALIDATION_MISSING_CASE_LIMIT = 4
_VALIDATION_CASE_EXCERPT_LINE_LIMIT = 60
_VALIDATION_CASE_EXCERPT_CHAR_LIMIT = 4_000
_VALIDATION_RCA_COMPONENT_LIMIT = 8
_VALIDATION_RCA_SNIPPET_CHAR_LIMIT = 2_000


def _root_cause_case_files(root_cause: RootCauseAnalysis) -> list[str]:
    return root_cause.extracted_case_files


def _root_cause_source_spans(root_cause: RootCauseAnalysis) -> list[Any]:
    return list(root_cause.buggy_components)


def _relative_case_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").removeprefix("cases/")


def _bounded_text(value: str, limit: int) -> str:
    """Return a deterministic, visibly clipped diagnostic fragment."""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n... <{len(value) - limit} chars omitted>"


def _match_location(match: AnchorMatch) -> str:
    line_range = str(match.start_line)
    if match.end_line != match.start_line:
        line_range += f"-{match.end_line}"
    return f"{match.file}:{line_range}"


def _format_match_summary(scope: str, matches: Sequence[AnchorMatch]) -> str:
    """Describe actual deterministic matches without flooding repair prompts."""
    locations = sorted({_match_location(match) for match in matches})
    if not locations:
        return f"candidate query produced zero {scope} matches"
    shown = locations[:_VALIDATION_MATCH_LOCATION_LIMIT]
    omitted = len(locations) - len(shown)
    suffix = f", ... <{omitted} locations omitted>" if omitted else ""
    file_count = len({match.file for match in matches})
    return (
        f"candidate query {scope} matches ({len(matches)} total across {file_count} file(s)): "
        + ", ".join(shown)
        + suffix
    )


def _read_case_excerpt(cases_dir: Path, relative: str) -> str:
    """Read a bounded, line-numbered case excerpt for one failed recall target."""
    root = cases_dir.resolve()
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return "<case path escapes the case root>"
    if not source.is_file():
        return "<case file is unavailable>"

    rendered: list[str] = []
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number > _VALIDATION_CASE_EXCERPT_LINE_LIMIT:
                rendered.append(f"... <lines after {_VALIDATION_CASE_EXCERPT_LINE_LIMIT} omitted>")
                break
            rendered.append(f"{line_number:>4}: {line.rstrip()}")
            if sum(len(item) + 1 for item in rendered) > _VALIDATION_CASE_EXCERPT_CHAR_LIMIT:
                rendered.append("... <case excerpt clipped>")
                break
    return _bounded_text("\n".join(rendered), _VALIDATION_CASE_EXCERPT_CHAR_LIMIT)


def _format_missing_case_evidence(cases_dir: Path, missing_cases: Sequence[str]) -> str:
    selected = list(missing_cases[:_VALIDATION_MISSING_CASE_LIMIT])
    sections = [f"[{relative}]\n{_read_case_excerpt(cases_dir, relative)}" for relative in selected]
    omitted = len(missing_cases) - len(selected)
    if omitted:
        sections.append(f"... <{omitted} missing case excerpts omitted>")
    return "missing required case source evidence:\n" + "\n\n".join(sections)


def _format_rca_target_evidence(
    root_cause: RootCauseAnalysis,
    *,
    analysis_subject: AnalysisSubject | None = None,
) -> str:
    """Render the bounded set of repository sites accepted by grounding validation."""
    components = _root_cause_source_spans(root_cause)[:_VALIDATION_RCA_COMPONENT_LIMIT]
    rendered = []
    for component in components:
        snippet = _bounded_text(component.snippet, _VALIDATION_RCA_SNIPPET_CHAR_LIMIT)
        rendered.append(
            f"[{component.file}:{component.start_line}-{component.end_line}] {component.role}\n{snippet}"
        )
    omitted = len(_root_cause_source_spans(root_cause)) - len(components)
    if omitted:
        rendered.append(f"... <{omitted} RCA components omitted>")
    label = (
        "accepted RCA bad-span target sites"
        if analysis_subject is not None and analysis_subject.type == "example_suite"
        else "accepted RCA repository target sites"
    )
    return f"{label} (match must overlap at least one):\n" + "\n\n".join(rendered)


def _validate_anchor_case_contract(
    intents: list[AnchorIntent],
    *,
    root_cause: RootCauseAnalysis,
    label: str,
    require_complete_manifest: bool = True,
) -> list[str]:
    """Validate hard per-anchor recall sets against the RCA case manifest."""
    errors: list[str] = []
    manifest = {_relative_case_path(path) for path in _root_cause_case_files(root_cause)}
    covered: set[str] = set()

    for intent in intents:
        raw_required = intent.required_cases
        required = [_relative_case_path(path) for path in raw_required]
        required_set = set(required)
        if raw_required != required:
            errors.append(f"{label} {intent.id!r} required_cases must use paths relative to cases/")
        if len(required) != len(required_set):
            errors.append(f"{label} {intent.id!r} required_cases contains duplicate paths")

        unknown = sorted(required_set - manifest)
        if unknown:
            errors.append(f"{label} {intent.id!r} requires undeclared cases: {', '.join(unknown)}")

        originals: set[str] = set()
        variants: list[tuple[str, str]] = []
        for relative in required_set & manifest:
            match = _CASE_RE.fullmatch(relative)
            if match is None:
                continue
            suffix = match.group("suffix")
            variant_no = match.group("variant")
            if variant_no is None:
                originals.add(relative)
            else:
                original = f"case{match.group('case')}{suffix}"
                variants.append((relative, original))

        if not originals:
            errors.append(f"{label} {intent.id!r} must require at least one original case")
        missing_originals = sorted(variant for variant, original in variants if original not in required_set)
        if missing_originals:
            errors.append(
                f"{label} {intent.id!r} requires variants without their originals: " + ", ".join(missing_originals)
            )
        covered.update(required_set)

    if require_complete_manifest:
        missing_manifest = sorted(manifest - covered)
        if missing_manifest:
            errors.append(f"{label}s do not assign required coverage for: " + ", ".join(missing_manifest))
    return errors


def _match_overlaps_buggy_component(
    *,
    file: str,
    start_line: int,
    end_line: int,
    root_cause: RootCauseAnalysis,
) -> bool:
    """Return whether a real query match overlaps an RCA-declared causal site."""
    return any(
        Path(file).as_posix().removeprefix("./") == Path(span.file).as_posix().removeprefix("./")
        and start_line <= span.end_line
        and end_line >= span.start_line
        for span in _root_cause_source_spans(root_cause)
    )


def validate_issue_checkout(
    issue: IssueCollectionInfo,
    *,
    workspace_root: Path,
) -> list[str]:
    """Return concrete checkout errors without raising."""
    errors: list[str] = []
    repo_path = Path(issue.repo_path).resolve()
    workspace_root = workspace_root.resolve()
    try:
        repo_path.relative_to(workspace_root)
    except ValueError:
        errors.append(f"repo_path must stay inside {workspace_root}: {repo_path}")
        return errors
    if not repo_path.is_dir():
        return [f"repo_path does not exist: {repo_path}"]

    try:
        repo = Repo(repo_path)
        if repo.head.commit.hexsha != issue.buggy_commit:
            errors.append(f"buggy checkout mismatch: HEAD is {repo.head.commit.hexsha}, expected {issue.buggy_commit}")
        if issue.fixed_commit:
            try:
                fixed_sha = repo.commit("fixed").hexsha
            except GitError:
                errors.append("fixed_commit is declared but the fixed branch is missing")
            else:
                if fixed_sha != issue.fixed_commit:
                    errors.append(f"fixed branch is {fixed_sha}, expected {issue.fixed_commit}")
    except (GitError, OSError) as exc:
        errors.append(f"repo_path is not a readable git checkout: {exc}")
    return errors


def validate_root_cause_cases(
    analysis: RootCauseAnalysis,
    *,
    cases_dir: Path,
) -> list[str]:
    """Check that the RCA manifest exactly describes persistent case files."""
    errors: list[str] = []
    if not cases_dir.is_dir():
        return [f"cases directory does not exist: {cases_dir}"]

    actual = sorted(path.relative_to(cases_dir).as_posix() for path in cases_dir.rglob("*") if path.is_file())
    declared = [_relative_case_path(path) for path in _root_cause_case_files(analysis)]
    expected = sorted(set(declared))
    if not actual:
        errors.append("cases directory is empty")
    if len(declared) != len(expected):
        errors.append("extracted_case_files contains duplicate paths")
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        if missing:
            errors.append(f"declared case files are missing: {', '.join(missing)}")
        if unexpected:
            errors.append(f"undeclared case files exist: {', '.join(unexpected)}")

    allowed_suffixes = _LANGUAGE_SUFFIXES[analysis.language]
    originals: set[tuple[str, str]] = set()
    variants: list[tuple[str, str, str]] = []
    for relative in expected:
        if "/" in relative:
            errors.append(f"case files must be directly under cases/: {relative}")
            continue
        match = _CASE_RE.fullmatch(relative)
        if not match:
            errors.append(f"invalid case filename: {relative}")
            continue
        suffix = match.group("suffix").lower()
        if suffix not in allowed_suffixes:
            errors.append(f"case extension {suffix} does not match language {analysis.language.value}: {relative}")
        case_no = match.group("case")
        variant_no = match.group("variant")
        if variant_no is None:
            originals.add((case_no, suffix))
        else:
            variants.append((case_no, suffix, relative))
        path = cases_dir / relative
        if path.is_file() and not path.read_text(encoding="utf-8", errors="replace").strip():
            errors.append(f"case file is empty: {relative}")

    for case_no, suffix, relative in variants:
        if (case_no, suffix) not in originals:
            errors.append(f"variant has no matching original case: {relative}")
    return errors


def validate_root_cause_analysis(
    analysis: RootCauseAnalysis,
    *,
    source_root: Path,
    cases_dir: Path,
) -> list[str]:
    """Validate persistent cases and concrete source evidence as one RCA contract."""
    errors = validate_root_cause_cases(analysis, cases_dir=cases_dir)
    root = source_root.resolve()
    if not root.is_dir():
        return [*errors, f"source root does not exist: {root}"]

    seen_components: set[tuple[str, int, int]] = set()
    for component in analysis.buggy_components:
        relative = Path(component.file)
        if relative.is_absolute():
            errors.append(f"buggy component path must be source-root-relative: {component.file}")
            continue
        source_path = (root / relative).resolve()
        try:
            canonical = source_path.relative_to(root).as_posix()
        except ValueError:
            errors.append(f"buggy component escapes the source root: {component.file}")
            continue
        location = (canonical, component.start_line, component.end_line)
        if location in seen_components:
            errors.append(
                f"duplicate buggy component location: {canonical}:{component.start_line}-{component.end_line}"
            )
        seen_components.add(location)
        if not source_path.is_file():
            errors.append(f"buggy component file does not exist: {canonical}")
            continue
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if component.end_line > len(lines):
            errors.append(
                f"buggy component line range exceeds {canonical} ({len(lines)} lines): "
                f"{component.start_line}-{component.end_line}"
            )
            continue
        excerpt = "\n".join(lines[component.start_line - 1 : component.end_line])
        if component.snippet.strip() not in excerpt:
            errors.append(
                f"buggy component snippet is not contained in "
                f"{canonical}:{component.start_line}-{component.end_line}"
            )
    return errors


def _changed_intent_fields(anchor: Anchor, intent: AnchorIntent) -> list[str]:
    return [
        field
        for field in (
            "behavior",
            "inspect_hint",
            "behavior_weight",
        )
        if getattr(anchor, field) != getattr(intent, field)
    ]


def validate_anchor_synthesis_request(
    request: AnchorSynthesisRequest,
    *,
    root_cause: RootCauseAnalysis,
) -> list[str]:
    """Validate the complete intent contract before starting any model exploration."""
    errors: list[str] = []
    if request.root_cause != root_cause:
        errors.append("synthesis request root_cause must equal the authoritative RCA")
    errors.extend(
        _validate_anchor_case_contract(
            request.anchor_intents,
            root_cause=root_cause,
            label="anchor intent",
        )
    )
    intent_ids = [intent.id for intent in request.anchor_intents]
    if len(intent_ids) != len(set(intent_ids)):
        errors.append("anchor intent ids must be unique")
    return errors


def validate_anchor_synthesis_run(
    value: AnchorSynthesisRunResult,
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    run_request: AnchorSynthesisRunRequest | None,
    analysis_subject: AnalysisSubject | None = None,
) -> list[str]:
    """Validate one isolated anchor without requiring it to cover other intents' cases."""
    source_path = source_root or repo_path
    if source_path is None:
        return ["source_root or repo_path is required to validate anchor synthesis"]
    errors = validate_root_cause_analysis(root_cause, source_root=source_path, cases_dir=cases_dir)
    if run_request is None:
        errors.append("anchor_synthesis_run_request is required to validate an anchor run")
        return errors
    if run_request.root_cause != root_cause:
        errors.append("anchor run request root_cause must equal the authoritative RCA")

    intent = run_request.anchor_intent
    errors.extend(
        _validate_anchor_case_contract(
            [intent],
            root_cause=root_cause,
            label="anchor intent",
            require_complete_manifest=False,
        )
    )
    if value.anchor.id != intent.id:
        errors.append(
            "synthesized anchor must preserve its intent id exactly: "
            f"anchor={value.anchor.id!r}, intent={intent.id!r}"
        )
    else:
        changed = _changed_intent_fields(value.anchor, intent)
        if changed:
            errors.append(f"anchor {value.anchor.id!r} changed immutable intent fields: " + ", ".join(changed))
    if errors:
        return errors

    serialized = [value.anchor.model_dump(mode="json", by_alias=True)]
    try:
        case_scan = scan_anchors(serialized, cases_dir, root_cause.language.value)
    except (AnchorScanError, FileNotFoundError, ValueError, OSError) as exc:
        return [
            f"candidate ast-grep query could not be parsed or executed against required cases: {exc}",
            "required case files: " + ", ".join(sorted(_relative_case_path(path) for path in intent.required_cases)),
            _format_missing_case_evidence(
                cases_dir,
                sorted(_relative_case_path(path) for path in intent.required_cases),
            ),
            _format_rca_target_evidence(root_cause, analysis_subject=analysis_subject),
        ]

    case_summary = _format_match_summary("case", case_scan.matches)
    try:
        source_scan = scan_anchors(serialized, source_path, root_cause.language.value)
    except (AnchorScanError, FileNotFoundError, ValueError, OSError) as exc:
        return [
            case_summary,
            f"candidate ast-grep query could not be executed against the source root: {exc}",
            _format_rca_target_evidence(root_cause, analysis_subject=analysis_subject),
        ]

    matched_cases = {match.file for match in case_scan.matches}
    required_cases = {_relative_case_path(path) for path in intent.required_cases}
    missing_required = sorted(required_cases - matched_cases)
    if missing_required:
        errors.extend(
            (
                f"anchor {value.anchor.id!r} does not match required cases: " + ", ".join(missing_required),
                case_summary,
                _format_missing_case_evidence(cases_dir, missing_required),
            )
        )

    if analysis_subject is None or analysis_subject.type == "issue":
        has_grounding = any(
            _match_overlaps_buggy_component(
                file=match.file,
                start_line=match.start_line,
                end_line=match.end_line,
                root_cause=root_cause,
            )
            for match in source_scan.matches
        )
        if not has_grounding:
            errors.extend(
                (
                    f"anchor {value.anchor.id!r} has no RCA-declared repository-site match",
                    _format_match_summary("repository", source_scan.matches),
                    _format_rca_target_evidence(root_cause, analysis_subject=analysis_subject),
                )
            )
    return errors


def aggregate_anchor_synthesis_runs(
    request: AnchorSynthesisRequest,
    runs: Sequence[AnchorSynthesisRunResult],
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject,
) -> AnchorSynthesisResult:
    """Validate and deterministically aggregate isolated per-anchor run outputs."""
    source_path = source_root or repo_path
    if source_path is None:
        raise ValueError("anchor synthesis aggregation failed:\n- source_root or repo_path is required")
    errors = validate_root_cause_analysis(root_cause, source_root=source_path, cases_dir=cases_dir)
    errors.extend(validate_anchor_synthesis_request(request, root_cause=root_cause))

    intent_ids = [intent.id for intent in request.anchor_intents]
    run_ids = [run.anchor.id for run in runs]
    if len(run_ids) != len(set(run_ids)):
        errors.append("per-anchor synthesis runs returned duplicate anchor ids")
    missing_ids = [anchor_id for anchor_id in intent_ids if anchor_id not in run_ids]
    unexpected_ids = [anchor_id for anchor_id in run_ids if anchor_id not in intent_ids]
    if missing_ids:
        errors.append("per-anchor synthesis runs are missing intents: " + ", ".join(missing_ids))
    if unexpected_ids:
        errors.append("per-anchor synthesis runs returned unexpected intents: " + ", ".join(unexpected_ids))

    runs_by_id = {run.anchor.id: run for run in runs}
    ordered_runs = [runs_by_id[anchor_id] for anchor_id in intent_ids if anchor_id in runs_by_id]
    intents_by_id = {intent.id: intent for intent in request.anchor_intents}
    for run in ordered_runs:
        changed = _changed_intent_fields(run.anchor, intents_by_id[run.anchor.id])
        if changed:
            errors.append(f"anchor {run.anchor.id!r} changed immutable intent fields: " + ", ".join(changed))
    if errors:
        raise ValueError("anchor synthesis aggregation failed:\n- " + "\n- ".join(errors))

    anchors = [run.anchor for run in ordered_runs]
    serialized = [anchor.model_dump(mode="json", by_alias=True) for anchor in anchors]
    try:
        case_scan = scan_anchors(serialized, cases_dir, root_cause.language.value)
        source_scan = scan_anchors(serialized, source_path, root_cause.language.value)
    except (AnchorScanError, FileNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"anchor synthesis aggregation failed: ast-grep validation failed: {exc}") from exc

    case_files = sorted(path.relative_to(cases_dir).as_posix() for path in cases_dir.rglob("*") if path.is_file())
    matches_by_case: dict[str, set[str]] = {path: set() for path in case_files}
    matches_by_anchor: dict[str, set[str]] = {anchor_id: set() for anchor_id in intent_ids}
    for match in case_scan.matches:
        matches_by_case.setdefault(match.file, set()).add(match.anchor_id)
        matches_by_anchor.setdefault(match.anchor_id, set()).add(match.file)

    for intent in request.anchor_intents:
        required = {_relative_case_path(path) for path in intent.required_cases}
        missing_required = sorted(required - matches_by_anchor.get(intent.id, set()))
        if missing_required:
            errors.append(f"anchor {intent.id!r} does not match required cases: " + ", ".join(missing_required))
    missing_cases = [path for path in case_files if not matches_by_case[path]]
    if missing_cases:
        errors.append("anchors do not cover case files: " + ", ".join(missing_cases))

    grounded_by_anchor: dict[str, list[tuple[str, int]]] = {anchor_id: [] for anchor_id in intent_ids}
    if analysis_subject.type == "example_suite":
        uncovered_spans = [
            f"{span.file}:{span.start_line}-{span.end_line}"
            for span in _root_cause_source_spans(root_cause)
            if not any(
                Path(match.file).as_posix().removeprefix("./") == Path(span.file).as_posix().removeprefix("./")
                and match.start_line <= span.end_line
                and match.end_line >= span.start_line
                for match in source_scan.matches
            )
        ]
        if uncovered_spans:
            errors.append("inferred bad spans are not covered by anchors: " + ", ".join(uncovered_spans))
    else:
        for match in source_scan.matches:
            if _match_overlaps_buggy_component(
                file=match.file,
                start_line=match.start_line,
                end_line=match.end_line,
                root_cause=root_cause,
            ):
                grounded_by_anchor.setdefault(match.anchor_id, []).append((match.file, match.start_line))
        missing_grounding = [anchor_id for anchor_id in intent_ids if not grounded_by_anchor[anchor_id]]
        if missing_grounding:
            errors.append("anchors without an RCA-declared repository-site match: " + ", ".join(missing_grounding))
    if errors:
        raise ValueError("anchor synthesis aggregation failed:\n- " + "\n- ".join(errors))

    return AnchorSynthesisResult(
        anchors=anchors,
        case_coverage=[
            CaseCoverage(
                path=path,
                anchor_ids=[anchor_id for anchor_id in intent_ids if anchor_id in matches_by_case[path]],
            )
            for path in case_files
        ],
        repo_evidence=(
            []
            if analysis_subject.type == "example_suite"
            else [
                RepoEvidence(
                    anchor_id=anchor_id,
                    file=location[0],
                    line=location[1],
                )
                for anchor_id in intent_ids
                for location in [min(set(grounded_by_anchor[anchor_id]))]
            ]
        ),
        adjustments=[f"{run.anchor.id}: {adjustment}" for run in ordered_runs for adjustment in run.adjustments],
    )


def validate_anchors(
    value: AnchorSynthesisResult | VASCoreInfo,
    *,
    repo_path: Path | None = None,
    source_root: Path | None = None,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject,
    synthesis_request: AnchorSynthesisRequest | None = None,
) -> list[str]:
    """Run ast-grep and verify immutable intent, recall, and RCA grounding."""
    source_path = source_root or repo_path
    if source_path is None:
        return ["source_root or repo_path is required to validate anchors"]
    errors = validate_root_cause_analysis(root_cause, source_root=source_path, cases_dir=cases_dir)
    if errors:
        return errors

    anchor_ids = [anchor.id for anchor in value.anchors]
    if len(anchor_ids) != len(set(anchor_ids)):
        errors.append("anchor ids must be unique")
    if isinstance(value, AnchorSynthesisResult):
        if synthesis_request is None:
            errors.append("anchor_synthesis_request is required to validate live synthesis")
        else:
            if synthesis_request.root_cause != root_cause:
                errors.append("synthesis request root_cause must equal the authoritative RCA")
            errors.extend(
                _validate_anchor_case_contract(
                    synthesis_request.anchor_intents,
                    root_cause=root_cause,
                    label="anchor intent",
                )
            )
            intent_ids = [intent.id for intent in synthesis_request.anchor_intents]
            if len(intent_ids) != len(set(intent_ids)):
                errors.append("anchor intent ids must be unique")
            if anchor_ids != intent_ids:
                errors.append(
                    "synthesized anchors must preserve intent ids and order exactly: "
                    f"anchors={anchor_ids}, intents={intent_ids}"
                )
            else:
                for anchor, intent in zip(
                    value.anchors,
                    synthesis_request.anchor_intents,
                    strict=True,
                ):
                    changed = _changed_intent_fields(anchor, intent)
                    if changed:
                        errors.append(f"anchor {anchor.id!r} changed immutable intent fields: " + ", ".join(changed))
    if isinstance(value, VASCoreInfo) and value.language != root_cause.language:
        errors.append(f"rule language {value.language.value} does not match RCA language {root_cause.language.value}")
    if errors:
        return errors

    try:
        anchors = [anchor.model_dump(mode="json", by_alias=True) for anchor in value.anchors]
        case_scan = scan_anchors(anchors, cases_dir, root_cause.language.value)
        source_scan = scan_anchors(anchors, source_path, root_cause.language.value)
    except (AnchorScanError, FileNotFoundError, ValueError, OSError) as exc:
        return [f"ast-grep validation failed: {exc}"]

    case_files = sorted(path.relative_to(cases_dir).as_posix() for path in cases_dir.rglob("*") if path.is_file())
    actual_coverage: dict[str, set[str]] = {path: set() for path in case_files}
    anchor_case_matches: dict[str, set[str]] = {anchor.id: set() for anchor in value.anchors}
    for match in case_scan.matches:
        actual_coverage.setdefault(match.file, set()).add(match.anchor_id)
        anchor_case_matches.setdefault(match.anchor_id, set()).add(match.file)

    missing_cases = [path for path, anchor_ids in actual_coverage.items() if not anchor_ids]
    if missing_cases:
        errors.append(f"anchors do not cover case files: {', '.join(missing_cases)}")

    if isinstance(value, AnchorSynthesisResult):
        assert synthesis_request is not None
        required_cases_by_id = {
            intent.id: {_relative_case_path(path) for path in intent.required_cases}
            for intent in synthesis_request.anchor_intents
        }
        for anchor in value.anchors:
            required = required_cases_by_id[anchor.id]
            missing_required = sorted(required - anchor_case_matches.get(anchor.id, set()))
            if missing_required:
                errors.append(f"anchor {anchor.id!r} does not match required cases: " + ", ".join(missing_required))
    else:
        missing_case_matches = [anchor.id for anchor in value.anchors if not anchor_case_matches.get(anchor.id)]
        if missing_case_matches:
            errors.append("anchors without a case match: " + ", ".join(missing_case_matches))

    grounded_repo_matches: set[tuple[str, str, int]] = set()
    if analysis_subject.type == "example_suite":
        uncovered_spans = [
            f"{span.file}:{span.start_line}-{span.end_line}"
            for span in _root_cause_source_spans(root_cause)
            if not any(
                Path(match.file).as_posix().removeprefix("./") == Path(span.file).as_posix().removeprefix("./")
                and match.start_line <= span.end_line
                and match.end_line >= span.start_line
                for match in source_scan.matches
            )
        ]
        if uncovered_spans:
            errors.append("inferred bad spans are not covered by anchors: " + ", ".join(uncovered_spans))
    else:
        grounded_repo_matches = {
            (match.anchor_id, match.file, match.start_line)
            for match in source_scan.matches
            if _match_overlaps_buggy_component(
                file=match.file,
                start_line=match.start_line,
                end_line=match.end_line,
                root_cause=root_cause,
            )
        }
        grounded_ids = {anchor_id for anchor_id, _, _ in grounded_repo_matches}
        missing_grounding = [anchor.id for anchor in value.anchors if anchor.id not in grounded_ids]
        if missing_grounding:
            errors.append("anchors without an RCA-declared repository-site match: " + ", ".join(missing_grounding))

    if isinstance(value, AnchorSynthesisResult):
        coverage_paths = [_relative_case_path(item.path) for item in value.case_coverage]
        if len(coverage_paths) != len(set(coverage_paths)):
            errors.append("case_coverage contains duplicate paths")
        declared_coverage = {_relative_case_path(item.path): set(item.anchor_ids) for item in value.case_coverage}
        duplicate_coverage_ids = [
            _relative_case_path(item.path)
            for item in value.case_coverage
            if len(item.anchor_ids) != len(set(item.anchor_ids))
        ]
        if duplicate_coverage_ids:
            errors.append(
                "case_coverage contains duplicate anchor ids for: " + ", ".join(sorted(duplicate_coverage_ids))
            )
        if declared_coverage != actual_coverage:
            errors.append(
                "declared case_coverage does not equal actual coverage: "
                f"declared={declared_coverage}, actual={actual_coverage}"
            )

        if analysis_subject.type == "example_suite":
            if value.repo_evidence:
                errors.append("example-suite synthesis must not declare repo_evidence")
        else:
            declared_evidence_items = [(item.anchor_id, item.file, item.line) for item in value.repo_evidence]
            declared_evidence = set(declared_evidence_items)
            if len(declared_evidence_items) != len(declared_evidence):
                errors.append("repo_evidence contains duplicate locations")
            invalid_evidence = sorted(declared_evidence - grounded_repo_matches)
            if invalid_evidence:
                errors.append("repo_evidence contains non-RCA-site matches: " f"{invalid_evidence}")
            evidence_ids = {anchor_id for anchor_id, _, _ in declared_evidence}
            missing_evidence = [anchor.id for anchor in value.anchors if anchor.id not in evidence_ids]
            if missing_evidence:
                errors.append(f"anchors missing declared repo evidence: {', '.join(missing_evidence)}")

    return errors


def validate_vas_core(
    value: VASCoreInfo,
    *,
    source_root: Path,
    cases_dir: Path,
    root_cause: RootCauseAnalysis,
    analysis_subject: AnalysisSubject | None,
) -> list[str]:
    """Validate a complete Rule Generator result against the authoritative RCA."""

    if analysis_subject is None:
        return ["analysis_subject is required to validate a generated VAS core"]
    errors: list[str] = []
    if value.language != root_cause.language:
        errors.append(
            f"rule language {value.language.value!r} differs from RCA language "
            f"{root_cause.language.value!r}"
        )
    if value.root_cause_summary != root_cause.root_cause_summary:
        errors.append("rule root_cause_summary must equal the authoritative RCA")
    errors.extend(
        validate_anchors(
            value,
            source_root=source_root,
            cases_dir=cases_dir,
            root_cause=root_cause,
            analysis_subject=analysis_subject,
        )
    )
    return errors


def validate_vas_core_synthesis(
    value: VASCoreInfo,
    *,
    request: AnchorSynthesisRequest | None,
    synthesis: AnchorSynthesisResult | None,
) -> list[str]:
    """Require the parent output to consume exactly its validated child batch."""

    if request is None or synthesis is None:
        return ["Rule Generator must call synthesize_ast_grep_anchors before final submission"]
    errors: list[str] = []
    if value.summary != request.summary:
        errors.append("rule summary must equal the summary delegated for anchor synthesis")
    if value.anchors != synthesis.anchors:
        errors.append("rule anchors must equal the deterministically finalized synthesis batch")
    return errors
