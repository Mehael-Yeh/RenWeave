# RenWeave project status

## Release target

RenWeave 1.5 provides a complete, context-aware and resumable Ren'Py localization path from a game directory to an installable language directory and verified RPA package. It accepts any source and target languages understood by the selected model, includes guided presets for major official and aggregated APIs, exposes both preflight Token expectations and provider-reported usage throughout the run, and applies one normative component and alignment system across the entire desktop workflow.

## Requirement coverage

| Requirement | Status | Implementation |
| --- | --- | --- |
| Guided one-click UI | Complete | English-default, Chinese-switchable five-step Calm Technical Workspace with shared component factories, fixed action geometry, responsive overflow handling, unified dialogs, field guidance, tooltips, and explicit action consequences |
| Complete model setup | Complete | OpenAI, Google, Anthropic, DeepSeek, MiniMax, OpenRouter, and custom presets; editable endpoints; model discovery; exact-ID fallback; minimal health check; secret-free persistence |
| Unpack Ren'Py games | Complete | Safe RPA 2.0/3.0/3.2 reader and selective script acquisition |
| Decompile compiled scripts | Complete | Pinned and hash-verified unrpyc, isolated subprocess, and workspace copies |
| Understand the complete game | Complete | Scene graph, deterministic evidence, storyline chunks, hierarchical narrative synthesis, character, relationship, and term profiles |
| Avoid line-by-line translation | Complete | Whole-scene translation with control-flow neighbors and compact relevant narrative context |
| Minimize additional Token use | Complete | Zero-Token deterministic pre-analysis, content-addressed caches, bounded context, targeted repair, risk-only refinement, and resume-safe usage counters |
| Token cost awareness | Complete | Read-only preflight range, indexed refinement, live provider-reporting status, input/output totals, and atomic `usage.json` phase ledger |
| Fully automatic translation | Complete | Discover → acquire → decompile → index → synthesize → translate → repair → refine → emit → validate → package → optional install |
| Preserve Ren'Py semantics | Complete | Stable text IDs and validation of coverage, tags, interpolation, placeholders, and generated scripts |
| Build and distribute | Complete | Standard `game/tl/<language>` scripts, deterministic verified RPA 3.0, safe installer, CI, and manual release automation |
| Recover from failures | Complete | Atomic workspace artifacts, completed-scene checkpoints, request retries, targeted repair, and persistent diagnostics |
| Visible progress and ETA | Complete | Weighted 0–100% pipeline progress, phase and scene status, checkpoint counts, provider-reported versus estimated Token usage, and adaptive ETA |
| Safe pause and resume | Complete | Cooperative phase/scene cancellation, content fingerprints, checkpoint revalidation, CLI interrupt handling, workspace locking, and failure circuit breaking |

## Deliberate boundaries

- RenWeave does not redistribute Ren'Py, unrpyc, or game assets. unrpyc is downloaded only when needed and is pinned by commit and archive hash.
- Static generated-script validation always runs. Official Ren'Py engine compilation runs when an SDK is discovered or specified; `--require-renpy-validation` makes it mandatory.
- Visual overflow, font coverage, and in-game screenshot comparison require launching the translated game and remain post-build QA.
- Translation quality depends on the selected model. Evidence-bound prompts and validation limit failures but cannot replace literary judgment in an incapable model.
- Token ranges are planning estimates, not currency quotes. Retries are excluded, compiled/archive-only preflight ranges are intentionally wide, and the provider billing dashboard remains authoritative.
- Users must have the legal right to process and translate the selected game.
