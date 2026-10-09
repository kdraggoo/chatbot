"""Phrases that mean the bot declined to answer.

Shared by the eval runner (refuse cases) and the dashboard's answer flags; no imports,
so evals can load it before main.py reads its -e overrides.
"""

REFUSAL_MARKERS = [
    "don't have", "do not have", "doesn't have", "does not have",
    "no information", "not mention", "no mention", "not provided", "not specified",
    "not available", "not include", "not contain", "isn't mentioned", "is not mentioned",
    "not found", "no record", "not listed", "unable to", "cannot", "can't",
    "couldn't", "could not", "not able to", "does not provide", "doesn't provide",
    "insufficient", "not enough information", "no relevant",
]


def is_refusal(answer: str) -> bool:
    low = answer.lower()
    return any(m in low for m in REFUSAL_MARKERS)
