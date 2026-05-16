import hashlib
import json


def generate_fingerprint(source: str, alert_name: str, affected_service: str) -> str:
    """Deterministic SHA-256 fingerprint identifying what is alerting, not its state.

    Inputs are sorted-key JSON serialised before hashing so insertion order
    never affects the output. Returns a 64-character lowercase hex string.
    """
    key = json.dumps(
        {"affected_service": affected_service, "alert_name": alert_name, "source": source},
        sort_keys=True,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
