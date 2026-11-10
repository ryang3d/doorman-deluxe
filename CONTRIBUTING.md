# Contributing to Doorman Deluxe

Thanks for helping. This is a self-hosted AI doorbell service: a long-running
Python process that listens for Frigate door events, talks to visitors through
the doorbell camera (Gemini Live or a fully local STT→LLM→TTS pipeline), and
snapshots/notify the homeowner via Home Assistant.

## Repository layout

- `src/` - the service (flat modules, run as `python src/doorman.py`).
- `src/ui/` - web UI frontend (vanilla JS, no build step).
- `tests/` - pytest unit tests **and** manual diagnostic scripts (the
  diagnostics are listed in `conftest.py:collect_ignore`; they run ad-hoc
  against a live deployment, not in CI).
- `deploy/` - bare-metal/systemd extras, the optional faster-whisper STT
  service, and a health-check script.
- `tools/` - one-off diagnostics (WebRTC consumer probe).
- `docker-compose.yml` - local development stack (`doorman` + optional `stt` profile).

## Development setup

Requires Python 3.13 and ffmpeg (for audio tests).

```bash
git clone https://github.com/ryang3d/doorman-deluxe.git
cd doorman-deluxe
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pre-commit install
```

Run the tests (unit tests only; the diagnostic scripts are excluded):

```bash
.venv/bin/python -m pytest -q
```

Run the linter/formatter:

```bash
ruff check --fix . && ruff format .
```

## Running locally

- Docker (recommended): `cp .env.example .env`, fill in your values,
  `docker compose up -d` (add `--profile stt` to also start the local voice
  STT service, which needs an NVIDIA GPU; it uses the official
  `ghcr.io/ryang3d/doorman-deluxe-stt:latest` image, or `--build` to build it
  from `deploy/whisper.Dockerfile` instead).
- Bare metal: `.venv/bin/python src/doorman.py` from the repo root, with your
  `DOORMAN_*` variables in the environment. See the README for configuration.

## Making changes

- One logical change per PR; small is good.
- New behavior gets a test in `tests/` (TDD: write the failing test first).
- No hardcoded hostnames, IPs, paths, or credentials in `src/` - all
  configuration goes through `doorman_config.py` (env vars > optional legacy
  profile files > neutral defaults).
- Keep the public surface stable: `DOORMAN_*` env var names and the
  `/api/*` endpoints are user-facing; changing them is a release-notes item.

## Releases

Releases are cut by the maintainer, not contributors:

1. Bump `src/version.py` (MAJOR.MINOR.PATCH).
2. Commit to `main`.
3. `git tag v<version> && git push origin main v<version>`.

`.github/workflows/release.yml` then builds the multi-arch Docker image,
publishes it to `ghcr.io/ryang3d/doorman-deluxe` (tag + `latest`), and creates
the GitHub Release. The workflow refuses to build if the tag and
`src/version.py` disagree.
