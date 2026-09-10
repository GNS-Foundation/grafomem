"""Every build input the Dockerfile COPYs must be covered by railway.toml watchPatterns.

Railway's rule is "deploy only when a changed file matches a watch pattern". So a
build input the patterns omit stops triggering deploys — production then sits
silently stale against main, and nothing surfaces it. That failure is invisible
by construction: the deploy that should have happened simply does not.

This test derives the required set from the Dockerfile itself rather than from a
list someone maintained by hand, so adding a COPY without a watch pattern fails
here instead of in production weeks later.
"""
from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _copy_sources() -> list[str]:
    """Source paths from the Dockerfile's build-stage COPY lines.

    `COPY --from=<stage>` is excluded: those copy from an earlier stage, not from
    the repository, so they are not build inputs in the watch-path sense.
    """
    sources: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        s = line.strip()
        if not s.upper().startswith("COPY "):
            continue
        rest = s[5:].strip()
        if rest.startswith("--from="):
            continue
        parts = rest.split()
        if len(parts) < 2:
            continue
        sources.extend(parts[:-1])          # last token is the destination
    return sources


def _covered(source: str, patterns: list[str]) -> bool:
    src = source.rstrip("/")
    for p in patterns:
        if p == source or p == src:
            return True
        base = p[:-3] if p.endswith("/**") else p
        if base == src or src.startswith(base.rstrip("/") + "/"):
            return True
    return False


def test_every_dockerfile_copy_source_is_watched():
    patterns = tomllib.loads((ROOT / "railway.toml").read_text())["build"]["watchPatterns"]
    sources = _copy_sources()
    assert sources, "no COPY lines parsed from the Dockerfile — the check would pass vacuously"

    missing = [s for s in sources if not _covered(s, patterns)]
    assert not missing, (
        "Dockerfile COPYs these build inputs, but railway.toml does not watch them:\n  "
        + "\n  ".join(missing)
        + "\n\nA change to any of them would not trigger a deploy, leaving production"
          "\nstale against main with nothing to show for it. Add them to watchPatterns"
          "\n(or stop copying them into the image)."
    )


def test_config_files_are_watched():
    """The build recipe and the config that drives it must trigger their own deploys."""
    patterns = tomllib.loads((ROOT / "railway.toml").read_text())["build"]["watchPatterns"]
    for required in ("Dockerfile", "railway.toml"):
        assert required in patterns, f"{required} must be in watchPatterns — it changes every build"


def test_detector_actually_detects():
    """Non-vacuity: the coverage check must reject a set that misses a real input."""
    assert not _covered("corpus/", ["src/**", "pyproject.toml"])
    assert not _covered("README.md", ["src/**"])
    assert _covered("corpus/", ["corpus/**"])
    assert _covered("pyproject.toml", ["pyproject.toml"])
    assert _covered("src/", ["src/**"])
