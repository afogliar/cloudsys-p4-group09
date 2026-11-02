import json
import os


def load_device_tokens() -> dict[str, str]:
    raw = os.environ.get("DEVICE_TOKENS_JSON")

    if not raw:
        raise RuntimeError("DEVICE_TOKENS_JSON est obligatoire")

    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("DEVICE_TOKENS_JSON doit être un JSON valide") from exc

    if not isinstance(tokens, dict) or not tokens:
        raise RuntimeError("La configuration des jetons doit être un objet non vide")

    for device_id, token in tokens.items():
        if not isinstance(token, str) or len(token) < 32:
            raise RuntimeError(f"Jeton absent ou trop court pour {device_id}")

    if len(set(tokens.values())) != len(tokens):
        raise RuntimeError("Chaque compteur doit avoir un jeton différent")

    return tokens


DEVICE_TOKENS = load_device_tokens()