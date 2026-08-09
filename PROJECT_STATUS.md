# RenWeave project status

## Release target

RenWeave 1.0 provides a complete context-aware Ren'Py localization path from a game directory to an installable directory and verified RPA language pack. It accepts arbitrary source and target languages supported by the selected OpenAI-compatible model.

## Requirement coverage

| Requirement | Status | Implementation |
| --- | --- | --- |
| Import an AI model | Complete | JSON profiles for OpenAI-compatible endpoints, environment or memory-only API keys, provider validation, retry and response limits |
| Unpack Ren'Py games | Complete | Safe RPA 2.0/3.0/3.2 reader and selective script acquisition |
| Decompile compiled scripts | Complete | Pinned and hash-verified unrpyc 2.0.2, isolated subprocess and workspace copies |
| Understand the complete game | Complete | Scene graph, deterministic evidence, storyline chunks, hierarchical narrative synthesis, character/relationship/term profiles |
| Avoid line-by-line translation | Complete | Whole-scene translation payloads with control-flow neighbors and relevant compact narrative context |
| Minimize additional Token use | Complete | Zero-Token pre-analysis, content-addressed knowledge/refinement caches, bounded relevant context, risk-only global refinement and usage counters |
| Fully automatic translation | Complete | Analyze → acquire → decompile → index → synthesize → translate → repair → refine → emit → validate → package → optional install |
| Preserve Ren'Py semantics | Complete | Stable text IDs plus validation of coverage, tags, interpolation and placeholders |
| Global quality optimization | Complete | Cross-scene inconsistency/term/voice risk audit, cached AI corrections and per-change structural validation |
| Build and distribute | Complete | Standard `game/tl/<language>` scripts, deterministic verified RPA 3.0 and safe installer |
| One-click user interface | Complete | Single-page Tk desktop UI with background progress and no manual lore/character/term forms |
| Recover from failures | Complete | Atomic workspace artifacts, completed-scene checkpoints, targeted repair, request retries and scene diagnostics |

## Deliberate boundaries

- RenWeave does not redistribute Ren'Py, unrpyc or any game assets. unrpyc is downloaded only when required and is pinned by commit and archive hash.
- Official Ren'Py engine compilation runs when an SDK is discovered or specified. Static generated-script validation always runs; `--require-renpy-validation` makes an SDK mandatory.
- Visual overflow, font coverage and screenshot comparison require running the translated game and remain a post-build QA activity. They do not weaken the translation or package validation guarantees above.
- Translation quality still depends on the selected model. RenWeave minimizes hallucination with evidence-bound prompts, structural checks and selective refinement, but cannot guarantee literary judgment from an incapable model.
