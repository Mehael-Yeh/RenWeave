# RenWeave project status

## Release target

RenWeave 1.8 provides a complete, context-aware and resumable Ren'Py localization path from a game directory to an installable language directory and verified RPA package. It accepts any source and target languages understood by the selected model, discovers live account catalogs across major official and aggregated APIs, protects keys through encrypted OS storage by default, exposes preflight and provider-reported Token usage, includes its required decompiler for offline use, and applies one responsive, no-jump component and alignment system across the entire desktop workflow.

## Requirement coverage

| Requirement | Status | Implementation |
| --- | --- | --- |
| Guided one-click UI | Complete | English-default five-step Calm Technical Workspace with an English/Chinese segment, in-place no-jump state updates, standalone bottom actions, 268/88 px adaptive navigation, responsive overflow handling, and unified dialogs |
| Complete model setup | Complete | OpenAI, Google, Anthropic, DeepSeek, MiniMax, Alibaba Cloud, Zhipu, Moonshot, SiliconFlow, OpenRouter, and custom presets; current-account model discovery without hard-coded IDs; exact-ID fallback; provider-aware thinking levels; encrypted per-provider keys; minimal health check |
| Unpack Ren'Py games | Complete | Safe RPA 2.0/3.0/3.2 reader and selective script acquisition |
| Decompile compiled scripts | Complete | Bundled pinned and hash-verified unrpyc, isolated subprocess, workspace copies, and no runtime tool downloads |
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

- RenWeave does not redistribute Ren'Py or game assets. It bundles the required MIT-licensed unrpyc runtime, pinned by commit and verified before installation; provenance and license details are recorded in `THIRD_PARTY_NOTICES.md` and the packaged vendor directory.
- Static generated-script validation always runs. Official Ren'Py engine compilation runs when an SDK is discovered or specified; `--require-renpy-validation` makes it mandatory.
- Visual overflow, font coverage, and in-game screenshot comparison require launching the translated game and remain post-build QA.
- Translation quality depends on the selected model. Evidence-bound prompts and validation limit failures but cannot replace literary judgment in an incapable model.
- Token ranges are planning estimates, not currency quotes. Retries are excluded, compiled/archive-only preflight ranges are intentionally wide, and the provider billing dashboard remains authoritative.
- Users must have the legal right to process and translate the selected game.
