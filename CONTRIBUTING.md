# Contributing

See the README for the source installation and test commands. Tests use generated
PDFs and temporary configuration directories; do not add personal PDFs or paths.

Please preserve these implementation constraints:

- `DocWorker` owns every PyMuPDF document. GTK callbacks submit tasks and consume
  returned data; they must not access a live document from the UI thread.
- Incremental saving is followed immediately by closing and reopening the PDF.
  Keep the generation checks that reject stale render and search results.
- Search and selection share the character index. Partial indexes must be
  completed before a whole-document search; coordinates refer to the source PDF.
- Keep page rendering within the cache budget, and retain the original pixels
  when applying night modes or display adjustments.
- UI regressions should exercise real keyboard or mouse input under Xvfb when
  practical. Keep configuration and modified PDFs inside temporary directories.

For a release, update `pyproject.toml`, `nightread/__init__.py`, README download
examples and `RELEASE_NOTES.md` together. Dependency changes also require updating
`packaging/dependencies.json` and `THIRD_PARTY_NOTICES.md`, verifying the bundled
native library's source version, and running the installed-package smoke test.

The Ubuntu package uses the system Python/GTK and bundles the pinned PDF/image
libraries. It deliberately targets Ubuntu 24.04 amd64; do not widen the advertised
platform support without testing the new environment.

Contributions are distributed under this project's AGPL-3.0 license.
