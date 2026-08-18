# VAMiner domain context

VAMiner turns one Mining Input into a Variant Analysis Specification (VAS). The implementation is intentionally organized around three deep Modules: `VAMiner`, closed Phase Authority, and `AnchorSynthesisSession`.

## Domain language

- **Mining Input**: either an Issue reference or an Example Suite directory.
- **Input Adapter**: the real Seam that prepares one input kind. It hides issue collection/checkout or example inspection/snapshot/registry work.
- **Prepared Analysis**: the typed point where all inputs converge: stable source identity, read root, grounding policy, and typed source state.
- **Src Root**: the already-bound read root for one Prepared Analysis: an Issue repository checkout or an immutable Example Suite snapshot. Every Src tool path is relative to this root.
- **VAS Workspace**: the workspace owned by one VAS id. Agent-visible state is limited to `src/` and `cases/`.
- **Case Artifact**: one bounded, non-empty, top-level `caseN.ext` or `caseN_varM.ext` file produced during RCA. Variants must retain their original case.
- **Root Cause Analysis (RCA)**: the authoritative language, causal explanation, source spans, fixing pattern, and declared Case Artifacts.
- **Phase Authority**: the closed assignment of responsibility and logical tools for Issue Collection, RCA, Rule Generation, or AST-Grep Synthesis. A field or tool is available only when that phase owns it.
- **Anchor Intent**: host-owned behavior, inspection guidance, weight, and required Case Artifacts for one causal-chain hotspot.
- **Anchor Plan**: an ordered, complete set of Anchor Intents plus the normative VAS summary.
- **Synthesis Delta**: child-owned query type, query text, query weight, and advisory notes for exactly one target intent.
- **VAS**: the stable persisted JSON assembled by the host from authoritative input state, RCA, accepted Anchor Plan, Rule Generation draft, and accepted Synthesis Deltas.

## Invariants

- Instructions compile exactly as canonical shared instructions → input policy → Runtime Adapter binding. The Adapter binding names concrete tools and structured output; it cannot redefine responsibility or constraints.
- One mining run uses one Runtime Adapter and one model identity for every parent and child Agent.
- RCA is the only Agent that writes workspace data, and it writes only through typed Case Artifact operations. Cleanup is explicit; acceptance and cache loading are pure.
- Rule Generation cannot read source or author query syntax. It produces semantics and submits at most two Anchor Plans.
- A Synthesizer cannot change RCA, summary, intent fields, or invoke another Agent. Invalid query semantics may degrade that one Anchor to `query: ""`; protocol, authority, and external execution failures propagate.
- Runtime Artifacts, generic filesystem permissions, arbitrary metadata, and capability negotiation are not part of the architecture.

## Real Seams and Adapters

- The **Input Seam** currently has `IssueInputAdapter` and `ExampleSuiteInputAdapter`. A future repository/commit input adds an Adapter without changing the shared workflow.
- The **Runtime Seam** currently has `PydanticAIRuntime` and `ClaudeCodeRuntime`. Both implement the same closed task contract and exact Phase Authority.
- Src navigation, Repository checkout/diff, Case Artifact, cache, and snapshot code are local Implementations behind these Modules, not general-purpose extension points.
