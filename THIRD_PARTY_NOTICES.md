# Third-party notices

RenWeave includes the following third-party component so required game-processing tools are available offline.

## unrpyc

- Project: [CensoredUsername/unrpyc](https://github.com/CensoredUsername/unrpyc)
- Version: 2.0.2
- Commit: `e16a767bbdd75abcf47a318b20480db4a07f7dfa`
- Source archive SHA-256: `25a273473cdf205a5ada8e0e9681dc5d31de2ba8bfec29d3f51faa49111b4e0d`
- License: MIT
- Packaged license: `src/renweave/_vendor/unrpyc/LICENSE`

RenWeave redistributes only the files required to run unrpyc: `unrpyc.py`, `deobfuscate.py`, and the Python modules under `decompiler/`. The source files are copied unchanged from the pinned upstream commit and verified as a deterministic tree before use.
