"""Docker image carries the [pipeline] extras.

Spec: SPEC-capa3-pipeline-v1
Covers:
- AC-9 — the runtime image must be able to ``import torch, whisperx,
  pyannote.audio`` without error so the lifespan can load the models on
  startup. ``[pipeline]`` extras (whisperx, pyannote.audio, ffmpeg-python,
  torch, torchaudio) live in pyproject.toml; the Dockerfile must install
  them.

This test runs ``docker run --rm transcription-api:latest python -c
"import ..."`` and asserts the imports succeed. It is auto-skipped when
the Docker daemon is unreachable (``requires_docker`` marker handled in
``conftest.py``) and explicitly skipped when the image has not been
built locally (CI/CPU dev machines do not pre-build it; the rig builds
it as part of the smoke flow in Task 7.3).
"""
from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.requires_docker


def _image_present(tag: str) -> bool:
    inspect = subprocess.run(  # noqa: S603,S607
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return inspect.returncode == 0


def test_docker_image_imports_pipeline_extras():
    """
    Spec: SPEC-capa3-pipeline-v1
    Criterion: AC-9 — the built image can `import torch, whisperx,
    pyannote.audio` cleanly. Validates that pyproject.toml's `[pipeline]`
    extras are pinned into the runtime image.
    """
    tag = "transcription-api:latest"
    if not _image_present(tag):
        pytest.skip(
            f"image {tag} not built locally; run `docker compose build` on the rig "
            "(Task 7.3 smoke procedure)"
        )

    out = subprocess.check_output(  # noqa: S603
        [
            "docker",
            "run",
            "--rm",
            tag,
            "python",
            "-c",
            "import torch, whisperx, pyannote.audio; print('ok')",
        ],
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    assert b"ok" in out, out.decode(errors="replace")
