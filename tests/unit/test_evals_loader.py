"""The eval suite loads the shipped summarizer by path; it must also find the
shared layer the Lambda imports (RC1-376: `python -m evals` died on
`No module named 'common'` after RC1-374 added `common.duration`)."""
import sys

from evals import fixtures, summarizer


def test_loader_puts_the_shared_layer_on_the_path_itself(monkeypatch):
    layer = str(summarizer.LAYER_PATH)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != layer])
    for name in [m for m in sys.modules if m == "common" or m.startswith("common.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(summarizer, "_app", None)

    prompt = summarizer.build_prompt(fixtures.BY_ID["single-cloudwatch"].incident())

    assert "payments-service" in prompt
