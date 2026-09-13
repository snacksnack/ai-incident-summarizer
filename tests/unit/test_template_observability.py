"""template.yaml's Globals carry the Datadog wiring every function inherits
(RC1-419). Read with a regex, the way evals/summarizer.py reads the MODEL_ID
pin: SAM's !Ref and !Sub tags keep a plain YAML loader out."""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "template.yaml"


def _globals_env() -> dict[str, str]:
    text = TEMPLATE.read_text()
    block = text.split("Globals:", 1)[1].split("Resources:", 1)[0]
    pairs = re.findall(r'^\s+(DD_\w+|POWERTOOLS_SERVICE_NAME):\s*"?([^"\n]+?)"?\s*$', block, re.M)
    return dict(pairs)


def test_llm_observability_is_on_for_the_stack():
    env = _globals_env()
    assert env["DD_LLMOBS_ENABLED"] == "1"
    assert env["DD_LLMOBS_ML_APP"] == "incident-summarizer"


def test_one_service_name_for_apm_llm_obs_and_the_ml_app():
    env = _globals_env()
    assert env["DD_SERVICE"] == env["DD_LLMOBS_ML_APP"] == env["POWERTOOLS_SERVICE_NAME"]


def test_no_function_overrides_the_datadog_handler():
    resources = TEMPLATE.read_text().split("Resources:", 1)[1]
    assert not re.search(r"^\s+Handler:", resources, re.M)


def test_two_functions_and_one_hand_off():
    """RC1-431: ingest and summarizer, joined by exactly one async invoke. The
    second lambda:InvokeFunction is EventBridge's permission on ingest."""
    resources = TEMPLATE.read_text().split("Resources:", 1)[1]
    functions = re.findall(r"^  (\w+):\n    Type: AWS::Serverless::Function", resources, re.M)
    assert functions == ["IngestFunction", "SummarizerFunction"]
    assert resources.count("Action: lambda:InvokeFunction") == 2
