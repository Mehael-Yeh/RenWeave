# Security policy

## Supported version

Security fixes are applied to the latest release and the default development branch.

## Reporting a vulnerability

Please use GitHub's private security advisory feature for this repository. Do not put API keys, proprietary game scripts, model responses, or unreleased game assets in a public issue.

## Trust boundaries

- RenWeave reads game scripts and RPA archives locally. The original game directory remains read-only unless the user explicitly enables installation.
- RPA extraction rejects absolute paths, parent traversal, unsafe index globals, oversized indexes and oversized members.
- RPYC/RPYMC decompilation necessarily processes serialized data. Only decompile games obtained from a trusted source; unrpyc runs in a separate process over workspace copies.
- Model API keys can be supplied by environment variable or the desktop password field. The desktop field is memory-only. Keys must never be committed to provider JSON files.
- Game text and existing translations are treated as untrusted data in all AI prompts, not as executable instructions.
- Installing a translation can overwrite user-owned files only when the user explicitly enables the overwrite option.
