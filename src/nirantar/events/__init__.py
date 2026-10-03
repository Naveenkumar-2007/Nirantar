"""Event backbone: outbox relay, typed consumers, dead-letter handling."""

TOPIC_PREFIX = "nirantar"
DLQ_TOPIC = "nirantar.dlq"


def kafka_topic(event_topic: str) -> str:
    """'payment' -> 'nirantar.payment'."""
    return f"{TOPIC_PREFIX}.{event_topic}"
