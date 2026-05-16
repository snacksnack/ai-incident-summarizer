import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> None:
    # Full implementation: RC1-33
    logger.info("Normalizer received event from source=%s", event.get("source"))
