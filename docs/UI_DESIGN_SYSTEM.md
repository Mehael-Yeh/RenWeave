# RenWeave desktop design system

RenWeave uses an **Aurora Workbench** system: Windows 11–informed component rendering with Material 3 semantic states for a focused desktop workflow. This document is normative: new UI code must use these tokens and component roles rather than creating page-specific widget styling.

## Reference basis

- [Material Design 3](https://m3.material.io/) informs semantic color roles, component states, hierarchy, and accessible interaction feedback.
- Qt Widgets and the PySide6 Essentials controls provide the bundled desktop component layer.
- [UI UX Pro Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) informs the explicit design-system workflow, responsive checks, and pre-delivery visual audit.
- [Awesome Design Systems](https://github.com/alexpate/awesome-design-systems) was used as a catalog of mature system patterns rather than as a source of page-specific styling.

Material Design 3, UI UX Pro Max, and Awesome Design Systems are design references rather than runtime dependencies. PySide6 Essentials is the only UI runtime dependency. RenWeave's Qt Widgets composition, desktop workflow, tokens, and Aurora Workbench identity are project-specific.

## Layout grid

- Default window: `1240 × 840`; minimum: `900 × 640`. Visual regression coverage also includes `1920 × 1080`, `1600 × 900`, `1440 × 900`, `1366 × 768`, and `1280 × 720`.
- Workflow rail: `224 px` fixed width; Qt's layout system handles the remaining width.
- Main canvas horizontal inset: `28 px`; card inset: `18 px`; standard field gap: `9 px`.
- Spacing uses the shared Qt layout rhythm of 8/9/12/14/16/18/28 px.
- Interactive controls target a `32 px` minimum height and use the platform UI font for bilingual consistency.
- Typography uses the semantic Qt stylesheet roles `PageTitle`, `SectionTitle`, `Body`, `Hint`, and `Status`; page code must not add one-off font declarations.
- Page titles, body copy, cards, fields, and activity content share one left edge.
- Top-bar actions, content cards, and footer actions share one right edge; the top and footer reserve the same `13 px` scrollbar gutter as the content host.
- Text and form content is left-aligned. Center alignment is reserved for button labels and compact numeric status.
- Pages should remain structurally fixed within the minimum window. The outer canvas is an overflow fallback, not the normal reading surface; long diagnostics, incremental-scope details, and logs belong in bounded local scroll regions.

## Action placement

| Context | Placement |
| --- | --- |
| Interface language | One top-right button beside Settings; its label names the language it will switch to |
| Section-wide auxiliary action | Top-right of that section |
| Field-specific action | Attached to the right of its field, equal control height |
| Form action | Left edge of its form column |
| Dialog actions | Bottom-right, secondary before primary |
| Workflow actions | Standalone back and primary buttons on the page surface with a stable explanatory center column |

The workflow action row never changes its column geometry between pages. It must not look like a full-width colored bar. Empty slots remain reserved so explanatory text and the main action do not jump when navigating.

## Components

All interactive widgets come from the persistent Qt shell and its page-local component builders.

| Component | Required implementation | Rules |
| --- | --- | --- |
| Primary button | `QPushButton#Primary` | One per action region; fixed semantic role and standard padding |
| Secondary button | `QPushButton#Secondary` | Back, cancel, pause, and non-destructive alternatives |
| Field action | `QPushButton#Secondary` beside a field | Browse and other field-specific actions stay attached to the field |
| Text field | `QLineEdit` | Shared padding, border, focus, disabled, selection, and typography |
| Combo box | `QComboBox` | Same visual height as text fields; editable only when needed |
| Checkbox | `QCheckBox` | Left-aligned with the form text column |
| Provider selector | `QComboBox` | Provider presets stay in one stable control; no height animation |
| Translation activity | `QProgressBar` plus status labels | Show overall completion separately from the worker stage and readable activity text |
| Vertical scroll | `QScrollArea` | Page scroll hosts stay mounted while their local content changes |
| Diagnostic/long text | bounded read-only `QTextEdit` | Fixed-height local scrolling; do not create dozens of wrapping labels that enlarge the whole page |
| Completed outputs | secondary actions on the progress page | Offer the generated output directory without rebuilding the page |
| Outer card | `QFrame#Card` | Main page boundary with the standard outline |
| Section panel | `QFrame#Card` | Bounded summary, option, and detail group |
| Inner layout | `QVBoxLayout` / `QHBoxLayout` | Structural rows inside a card or page |

## Interaction states

- Every clickable control has hover, pressed, disabled, and keyboard-focus states. Focus is a solid accent border/state contrast; dotted focus rectangles are prohibited.
- Disabled controls use both reduced contrast and native disabled semantics.
- Important controls include concise inline consequences and delayed guidance tooltips.
- Review-page output choices use vertically aligned checkboxes followed by consequence text; RPA creation appears before installation because it changes workspace artifacts, while installation changes the selected game.
- The RPA choice is on by default, and its inline copy always states that validated RPY files remain available when it is off.
- API-contacting actions state whether they normally consume Tokens.
- Step 04 has one primary decision per route: extract blank RPY files without a model, or enter step 05 for model-backed translation. The blank route remains on step 04 after completion.
- Step 05 derives its title, percentage, current operation, and actions from the progress payload published by the worker; the page keeps the progress widgets mounted while those values change.
- `100%` is reserved for `completed`. Percentages render only when the total is positive and finite; unknown totals use a completed-unit count without a denominator or progress bar.
- Token estimates describe the main translation text only. They must explicitly exclude context, validation, retry, and other model calls unless a full-task estimator exists.
- Raw logs are collapsed by default. The normal surface shows at most three recent user-readable events, with persistent paths and technical records available under **View log**.
- The model page is step 03 and keeps provider, API key, model ID, and the model-use checkbox in the default reading path. The checkbox is on by default. Base URL and reasoning controls are always visible; there is no separate model discovery, verification, or advanced-settings button. The primary footer action switches between blank extraction and **Continue** from the checkbox state.
- Game and workspace locations render as a friendly folder or executable name plus an abbreviated path. Change, copy, and open actions remain visible; raw path entry is an explicit manual-edit disclosure.
- An automatically detected Ren'Py runtime is a status card, not a third peer path field. The game-safety promise is a distinct success notice with a plain-language consequence.
- Top-bar utilities use the quiet `TopAux.TButton` role and must remain visually subordinate to workflow actions.
- Keyboard focus follows the visual order; dialogs support `Escape`, and confirmation supports `Return` where safe.
- Text contrast targets WCAG AA. Color is never the only status signal.
- Settings and the single language toggle use the same button factory, vertical padding, and top-bar baseline.

## Prohibited patterns

- Do not instantiate page-specific controls outside the Qt page builder methods.
- Do not center form labels, instructions, lists, or arbitrary page sections.
- Do not move the primary workflow action between pages.
- Do not use a card frame for an internal layout row.
- Do not add one-off colors, padding values, fonts, or control heights outside the shared Qt stylesheet and layout helpers.
- Do not use decorative emoji as interface icons.
- Do not add a profile-import control to the desktop model flow; built-in presets and automatic non-secret settings persistence are the supported path.
- Do not rebuild the model page for provider selection, connection, or verification state changes; update existing widgets in place.
- Provider grids use equal columns and equal outer/inter-item gutters; no column receives a wider clickable tile because of asymmetric padding.
- Model catalogs must reflect the current API response only. Never merge hard-coded model IDs into a successful live response.
- API keys use encrypted OS credential storage by default and may be changed to memory-only in the shared Settings dialog. They never enter JSON settings, profiles, logs, screenshots, or packages.

## Visual QA checklist

Before merging UI changes, render all five workflow pages plus both model-route states and the error dialog at default, compact, and minimum window sizes. Verify:

1. Left edges, right edges, field heights, button heights, and footer slots do not move between pages; card and primary-action right edges differ by no more than `1 px`.
2. English and Simplified Chinese copy does not clip.
3. Hover, focus, selected, disabled, paused, failed, and unavailable-usage states remain legible.
4. Text fields, list rows, diagnostic text, and scrollbars use the documented styles.
5. No control is hidden behind the fixed footer and no horizontal scrollbar is required.
6. Provider and API action clicks do not replace the current page widget tree or change the current scroll position.
7. Resize transitions preserve the current page widget identity, keyboard focus, and scroll position at every breakpoint. Live resize coalesces layout work to at most one commit per 16 ms display frame and updates mounted breakpoint geometry in place.
8. Windows builds render one GUI window with an integrated dark title bar and no console window.
9. CI uploads pixel screenshots covering the bilingual workflow, the full seven-size desktop matrix, incremental review details, blank/model route selection, paused states, Settings, and the error dialog; inspect the artifact for clipping and font fallback.
