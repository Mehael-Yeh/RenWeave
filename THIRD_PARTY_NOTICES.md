# Third-party notices

## Sun Valley ttk theme

RenWeave uses `sv-ttk` to provide its Windows 11–informed desktop component theme.

- Project: https://github.com/rdbende/Sun-Valley-ttk-theme
- License: MIT
- Copyright: rdbende and contributors

The full package license is included in the installed `sv-ttk` distribution metadata and in standalone executable builds.

RenWeave includes the following third-party component so required game-processing tools are available offline.

## unrpyc

- Project: [CensoredUsername/unrpyc](https://github.com/CensoredUsername/unrpyc)
- Release: 2.0.4 (the unchanged upstream CLI reports `v2.0.3`)
- Commit: `3ae8334ed71a05535927dcc559663d3aca51215b`
- Source archive SHA-256: `36a0e8d05b00939f45c07c7a7d1e7eca37c3b28347d2baea9007ea3b2b5a41b8`
- License: MIT
- Packaged license: `src/renweave/_vendor/unrpyc/LICENSE`

RenWeave redistributes only the files required to run unrpyc: `unrpyc.py`, `deobfuscate.py`, and the Python modules under `decompiler/`. The source files are copied unchanged from the pinned upstream commit and verified as a deterministic tree before use.
