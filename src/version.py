"""Doorman Deluxe release version - single source of truth.

To cut a release:
  1. Bump __version__ (semantic versioning, MAJOR.MINOR.PATCH).
  2. Commit to main.
  3. Tag the commit and push:  git tag v0.1.0 && git push github main v0.1.0
.github/workflows/release.yml refuses to build if the tag doesn't match this
file, then publishes the image to ghcr.io/ryang3d/doorman-deluxe and creates
a GitHub Release.
"""

__version__ = "0.1.0"
