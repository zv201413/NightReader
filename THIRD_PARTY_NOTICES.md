# Third-party software

NightReader is distributed under GNU AGPL version 3. See [LICENSE](LICENSE).

The Ubuntu binary package includes unmodified PyPI wheels of:

| Component | Version | License | Source |
| --- | --- | --- | --- |
| PyMuPDF / MuPDF | 1.27.2 / 1.27.2 | AGPL-3.0 (open-source licensing option) | https://pypi.org/project/PyMuPDF/1.27.2/ |
| NumPy | 2.5.2 | BSD and bundled-component notices | https://pypi.org/project/numpy/2.5.2/ |
| Pillow | 12.3.0 | MIT-CMU and bundled-component notices | https://pypi.org/project/Pillow/12.3.0/ |

Their original metadata and license notices remain in `/opt/nightreader/lib/`,
including each distribution's `.dist-info` directory. Bundled native libraries
have additional notices there. Consult those notices for the full terms.

Each release provides `NightReader-VERSION-dependency-sources.tar.gz` with the
upstream source distributions, the exact download URLs and SHA-256 hashes.
The separate MuPDF 1.27.2 source archive (including its third-party sources) is
included alongside PyMuPDF's source distribution. It is the source URL recorded
in the bundled wheel's `pymupdf/_build.py`. NightReader's
corresponding source and build instructions are available in this repository
at the matching release tag and in the GitHub source archives.

Python, GTK, PyGObject, Cairo and system fonts are installed by Ubuntu's package
manager, not bundled in this package. Their licenses and source packages are
available from Ubuntu. No commercial Artifex license is claimed.
