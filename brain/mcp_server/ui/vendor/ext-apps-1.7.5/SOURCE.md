# ext-apps 1.7.5 (vendored, unmodified)

- Package: `@modelcontextprotocol/ext-apps@1.7.5` from the npm registry, published 2026-07-23. License: MIT (see `LICENSE`).
- npm integrity of the tarball: `sha512-TjPH2S2y5UEGKhmI6+XGFuqfqOV4ppe1x6DA3txnUaEWkgtA4G5vo14jGKFZmegdkZ1H4QMLyujLvoU1BEdnAg==`
- File: `dist/src/app-with-deps.js`, the in-frame `App` client with its dependencies bundled.
  sha256 `5bc0452b9994217df506cb505af20e97e766700513afb7d0b45d255785bb051f` (checked by `tests/test_widgets.py`).
- Why vendored: the widget HTML inlines it (`brain/mcp_server/widgets.py`), so the sandbox needs no external script domains.
- Upgrading: `npm pack @modelcontextprotocol/ext-apps@<version>`, copy the same file and `LICENSE` into a new folder, update the folder name in `widgets.py`, and update the hash in this file and in the test.
