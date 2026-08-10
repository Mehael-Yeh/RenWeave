# Changelog

All notable changes to RenWeave are documented here.

## 1.5.0 - 2026-08-10

- Rebuilt the desktop UI around a normative component system with shared color, spacing, sizing, and state tokens.
- Unified buttons, fields, combo boxes, checkboxes, provider selectors, model lists, diagnostic text, and scrollbars across every screen and dialog.
- Standardized page alignment and the fixed workflow footer so navigation, consequence copy, and primary actions never jump between steps.
- Added responsive minimum-window layouts, conditional content scrolling, compact provider and progress grids, and fixed-footer overflow protection.
- Replaced the model picker with a searchable, keyboard-friendly styled table and centered all application-owned dialogs relative to the workspace.
- Added component-structure assertions and a normative visual-QA checklist covering all workflow pages, dialogs, states, and supported window sizes.

## 1.4.0 - 2026-08-10

- Added a read-only preflight Token budget with input, output, and total ranges before translation starts.
- Added an atomically updated `usage.json` ledger with request counts, provider-reported totals, reporting availability, and per-phase breakdowns.
- Added persistent knowledge and refinement usage counters that remain correct across pause and resume.
- Added a live Token strip that compares provider-reported use with the current project estimate and warns when usage metadata is unavailable.
- Added consistent delayed guidance tooltips and explicit action-consequence copy for model, path, language, workflow, pause, and resume controls.
- Polished every workflow screen and modal, including a compact model-first setup, two-column review, budget card, diagnostic paths, and error-detail copying.
- Added desktop assertions for preflight budget and live Token reporting while retaining arbitrary target-language support.

## 1.3.0 - 2026-08-10

- Added weighted 0–100% progress with explicit preparation, understanding, translation, refinement, build, and completion phases.
- Added current-operation and scene status, verified checkpoint counts, model/Token usage, and adaptive ETA.
- Added safe GUI pause and CLI interrupt handling with automatic resume from the same workspace.
- Added content-based project fingerprints and structural revalidation of every reused scene artifact.
- Added cancellation points between analysis stages, narrative batches, scenes, and refinement batches.
- Added persistent readable and JSONL diagnostic logs with tracebacks and checkpoint timing.
- Added a concurrent-writer workspace lock and a repeated-scene-failure circuit breaker.

## 1.2.0 - 2026-08-10

- Added verified quick-connect presets for OpenAI, Google Gemini, Anthropic, DeepSeek, MiniMax, OpenRouter, and custom OpenAI-compatible endpoints.
- Added provider-aware endpoints, environment variable names, default model hints, and JSON-response capability handling.
- Added an exact-model-ID fallback when a provider does not expose a usable model catalog.
- Added a searchable model picker while keeping model IDs directly editable.
- Redesigned the full desktop flow and dialogs as a consistent Calm Technical Workspace with a dark workflow rail, light task canvas, and unified primary, secondary, and ghost actions.
- Documented the interface design system and provider compatibility matrix in English and Simplified Chinese.

## 1.1.0 - 2026-08-10

- Replaced the single-page form with a guided five-step Material-inspired desktop flow.
- Made AI model connection the first step and blocked translation until the selected model is verified.
- Added API endpoint validation, `/models` discovery, editable model selection, and a minimal chat health check.
- Added English-default interface copy with immediate Simplified Chinese switching across screens and dialogs.
- Added a semantic color, typography, spacing, focus, disabled-state, card, and modal design system for the Tk interface.
- Kept desktop API keys memory-only while automatically saving reusable provider profiles without secrets.
- Rewrote the main README in concise English and added complete Simplified Chinese documentation.
- Added a manually dispatched GitHub release workflow with version checks, tests, package validation, checksums, and draft/prerelease options.

## 1.0.0 - 2026-08-10

- Added arbitrary source-to-target language translation through importable OpenAI-compatible model profiles.
- Added safe RPA 2.0/3.0/3.2 reading and selective script acquisition.
- Added pinned, hash-verified, isolated RPYC/RPYMC decompilation with unrpyc 2.0.2.
- Added scene/control-flow indexing and deterministic character, storyline and terminology evidence.
- Added cached hierarchical AI narrative synthesis and compact scene-specific context planning.
- Added scene-level structured translation, tag/placeholder validation and targeted repair.
- Added cached risk-only global refinement for terminology, repeated lines and character voice.
- Added standard Ren'Py translation script emission, safe installation and deterministic verified RPA 3.0 packaging.
- Added strict generated-script validation and optional isolated official Ren'Py SDK compilation.
- Added one-page desktop UI with memory-only API key input and background stage progress.
- Added transient model-request retries, response-size limits, Token counters and persistent scene failure reports.
- Added Windows/Linux CI, an actual local-HTTP CLI end-to-end test and production package metadata.

## 0.1.0 - 0.9.0

The pre-1.0 milestones established the discovery, indexing, translation, build, decompilation, narrative knowledge, refinement, packaging, validation, desktop and reliability layers now included in 1.0.0.
