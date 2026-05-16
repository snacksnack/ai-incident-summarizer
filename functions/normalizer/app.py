import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> None:
    # Full implementation: RC1-34
    logger.info("Normalizer received event: %s", json.dumps(event))
