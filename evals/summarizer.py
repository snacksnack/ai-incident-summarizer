"""The shipped summarizer, loaded as-is (RC1-267).

`functions/summarizer/app.py` is a Lambda module, not a package, so it is
loaded by path. Its module body creates boto3 clients, which only need a
region to construct — no call is ever made, so no credential is needed.

The versions reported to the run store are read off the shipped artifacts
rather than declared by hand: the model from `template.yaml`'s pin, the
prompt version from a hash of `_build_prompt`'s source, the code version from
a hash of the whole module. A version that moves with the artifact cannot be
forgotten; one bumped by hand can.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "functions" / "summarizer" / "app.py"
# The Lambda imports `common` from the shared layer (RC1-374); in production the
# layer is on sys.path, here it has to be put there before the module body runs.
LAYER_PATH = REPO_ROOT / "layers" / "common" / "python"
TEMPLATE_PATH = REPO_ROOT / "template.yaml"

#: Mirrors the shipped `_call_llm` call, which hardcodes 1024. The eval must
#: spend the same budget the Lambda does or a truncation regression hides.
MAX_TOKENS = 1024

_app = None


def app():
    global _app
    if _app is None:
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        if str(LAYER_PATH) not in sys.path:
            sys.path.insert(0, str(LAYER_PATH))
        spec = importlib.util.spec_from_file_location("incident_summarizer_app", APP_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _app = module
    return _app


def build_prompt(incident: dict) -> str:
    return app()._build_prompt(incident)


def fallback(incident: dict) -> dict:
    return app()._fallback_summary(incident)


def model() -> str:
    """The model the template deploys, read from the pin itself."""
    match = re.search(r"MODEL_ID:\s*(\S+)", TEMPLATE_PATH.read_text())
    if not match:
        raise RuntimeError(f"no MODEL_ID pin found in {TEMPLATE_PATH}")
    return match.group(1)


def prompt_version() -> str:
    source = inspect.getsource(app()._build_prompt)
    return f"prompt-sha256:{hashlib.sha256(source.encode()).hexdigest()[:12]}"


def code_version() -> str:
    return f"app-sha256:{hashlib.sha256(APP_PATH.read_bytes()).hexdigest()[:12]}"
