"""Offline stand-in for a real embedding model and LLM.

Used only when no API key is configured, so the project runs (and its tests
pass) for free on any machine. It maps text to a small set of business
"concepts" using keyword lists. It is deliberately simple and limited: with
an API key the app uses real embeddings and an LLM instead (see llm.py).
"""
from __future__ import annotations

import hashlib
import math
import re

LOCAL_MODEL_NAME = "local-concept-v1"

# concept -> (weight, keywords). Weight >= 1.5 marks a "topical" concept (what
# the text is about); lower weights are generic business flavour.
CONCEPTS: dict[str, tuple[float, list[str]]] = {
    "food_delivery": (2.0, ["food delivery", "food order", "restaurant", "dining", "meal", "food", "swiggy", "zomato", "biryani", "pizza", "cloud kitchen"]),
    "grocery": (2.0, ["grocery", "groceries", "quick commerce", "quick-commerce", "instamart", "daily essentials", "fresh produce", "supermarket"]),
    "ecommerce": (2.0, ["e-commerce", "ecommerce", "online shopping", "marketplace", "big billion", "seller", "shopper", "online retail", "retail"]),
    "payments": (2.0, ["payment", "upi", "wallet", "pay later", "checkout", "credit card", "debit card", "card", "cashback", "fintech", "transaction", "merchant"]),
    "banking_credit": (2.0, ["bank", "banking", "loan", "emi", "credit", "interest rate", "repo rate", "lending", "deposit", "nbfc", "financing", "finance"]),
    "automotive": (2.0, ["vehicle", "bike", "motorcycle", "scooter", "two-wheeler", "three-wheeler", "car", "suv", "automotive", "dealership", "showroom", "test ride", "pulsar"]),
    "ev": (2.0, ["electric vehicle", "electric scooter", "electric two-wheeler", "electric", "ev", "e-scooter", "charging station", "charging", "battery swap", "battery"]),
    "fuel": (1.5, ["fuel", "petrol", "diesel", "cng", "crude oil", "oil price"]),
    "travel": (2.0, ["travel", "flight", "hotel", "trip", "holiday", "vacation", "airline", "train ticket", "bus ticket", "tourism", "getaway"]),
    "telecom": (2.0, ["telecom", "recharge", "data plan", "5g", "mobile plan", "broadband", "sim", "prepaid", "postpaid"]),
    "insurance": (2.0, ["insurance", "insurer", "premium", "claim", "health cover", "term plan", "motor cover"]),
    "investing": (2.0, ["stock", "mutual fund", "sip", "ipo", "equity", "nifty", "sensex", "trading", "invest", "investment", "investor", "portfolio", "demat"]),
    "entertainment": (2.0, ["streaming", "ott", "movie", "web series", "cricket", "ipl", "music", "live match"]),
    "electronics": (1.5, ["smartphone", "phone", "laptop", "electronics", "gadget", "appliance", "television"]),
    "offers": (1.0, ["offer", "discount", "% off", "off", "deal", "coupon", "promo", "promotion", "cashback", "sale$", "saving", "free"]),
    "festive": (1.0, ["diwali", "festive", "festival", "dussehra", "navratri", "holi", "eid", "christmas", "new year", "pongal", "onam", "black friday"]),
    "launch": (1.0, ["launch", "launching", "launched", "unveil", "unveiling", "debut", "rollout", "now live"]),
    "logistics": (1.0, ["delivery", "deliveries", "shipping", "logistics", "courier", "last-mile", "supply chain", "warehouse"]),
    "loyalty": (1.0, ["membership", "loyalty", "reward", "points", "subscription", "subscriber"]),
    "partnerships": (0.5, ["partner", "partnership", "tie-up", "co-branded", "co-brand", "collaboration", "alliance", "integration"]),
    "regulation": (1.0, ["regulation", "regulator", "policy", "guideline", "compliance", "rbi", "sebi", "government", "gst", "budget", "subsidy"]),
    "security": (1.5, ["fraud", "scam", "phishing", "data breach", "security", "kyc"]),
    "corporate": (1.0, ["hiring", "funding", "acquisition", "quarterly results", "earnings", "revenue", "expansion"]),
}

FESTIVALS = ["Diwali", "Dussehra", "Navratri", "Holi", "Eid", "Christmas", "New Year", "Pongal", "Onam", "Black Friday"]

_STOPWORDS = set(
    "a an the and or of on in at to for with by from is are was be as it this that our your their we "
    "all any new now get got will can more than up out off per via into over".split()
)


def _compile(keywords: list[str]) -> re.Pattern:
    """One regex per concept. Longest keyword first so "credit card" wins over "card".

    Simple plurals are allowed unless the keyword ends with "$" ("sale$" matches "sale" but not "sales").
    """
    parts = []
    for keyword in sorted(keywords, key=len, reverse=True):
        exact = keyword.endswith("$")
        parts.append(re.escape(keyword.rstrip("$")) + ("" if exact else "(?:s|es)?"))
    return re.compile(r"(?<![a-z0-9-])(?:" + "|".join(parts) + r")(?![a-z0-9])")


_PATTERNS = {name: _compile(keywords) for name, (_w, keywords) in CONCEPTS.items()}
CONCEPT_ORDER = list(CONCEPTS)


def extract_concepts(text: str) -> dict[str, float]:
    """Return {concept: weight} for every concept mentioned in the text."""
    lowered = text.lower()
    return {name: CONCEPTS[name][0] for name, pattern in _PATTERNS.items() if pattern.search(lowered)}


def detect_festival(text: str) -> str | None:
    lowered = text.lower()
    return next((f for f in FESTIVALS if f.lower() in lowered), None)


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9₹%]+", text.lower()) if t not in _STOPWORDS and len(t) > 1]


def local_embed(text: str, dim: int) -> list[float]:
    """Concept dimensions first (strong), then hashed word dimensions (weak)."""
    vec = [0.0] * dim
    for name, weight in extract_concepts(text).items():
        vec[CONCEPT_ORDER.index(name)] += weight
    offset = len(CONCEPT_ORDER)
    for tok in set(tokens(text)):
        bucket = int(hashlib.md5(tok.encode()).hexdigest(), 16) % (dim - offset)
        vec[offset + bucket] += 0.25
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def split_clauses(manifesto: str) -> list[tuple[str, bool]]:
    """Split a manifesto into separate interests. Returns (text, is_half) pairs.

    Commas, semicolons, colons and full stops always separate interests. "A and B" is also
    tried as two halves; local_judge only lets a half count on its own when it is specific
    enough, so "restaurant and dining trends" stays one interest while
    "food delivery discounts and travel booking offers" becomes two.
    """
    clauses = [c.strip() for c in re.split(r"[,;.:]", manifesto) if c.strip()]
    halves = []
    for clause in clauses:
        pieces = [h.strip() for h in re.split(r"\b(?:and|or)\b", clause) if h.strip()]
        if len(pieces) > 1:
            halves += pieces
    return [(c, False) for c in clauses] + [(h, True) for h in halves]


MIN_CLAUSE_WEIGHT = 1.5   # a lone generic word such as "partnerships" is not an interest
MIN_HALF_WEIGHT = 3.0     # half of an "A and B" clause needs a topical concept plus one more
OWN_DOMAIN_WEIGHT = 0.25


def local_judge(post_text: str, manifesto: str, own_domain: str = "") -> tuple[float, str]:
    """Rule-based relevance for one (post, agent) pair. Returns (score 0..1, reason).

    A manifesto is read as a list of interests, one per clause, for example
    "food delivery festive discounts". A clause is satisfied when the post
    mentions the concepts in it; the score is the best-satisfied clause.

    Concepts describing the agent's OWN business count for little: an insurer
    interested in "vehicle launches for motor insurance" wants posts about
    vehicle launches, and those posts will rarely mention insurance.
    """
    post = extract_concepts(post_text)
    if not post:
        return 0.0, "Post has no recognisable business topic (offline judge)."
    own = set(extract_concepts(own_domain))

    best_score, best_shared = 0.0, []
    for clause, is_half in split_clauses(manifesto):
        wanted = {c: w * (OWN_DOMAIN_WEIGHT if c in own else 1.0) for c, w in extract_concepts(clause).items()}
        total = sum(wanted.values())
        if total < (MIN_HALF_WEIGHT if is_half else MIN_CLAUSE_WEIGHT):
            continue
        shared = [c for c in wanted if c in post]
        score = sum(wanted[c] for c in shared) / total
        topical_wanted = [c for c, w in wanted.items() if w >= 1.5]
        if not any(c in post for c in topical_wanted):
            # Sharing only generic words ("offers", "festive") is never enough on its own.
            score = min(score, 0.6 if not topical_wanted else 0.4)
        if score > best_score:
            best_score, best_shared = score, shared

    if not best_shared:
        return 0.0, "No overlap with the agent's stated interests."
    names = ", ".join(c.replace("_", " ") for c in best_shared)
    if best_score >= 0.75:
        return round(best_score, 3), f"Matches a stated interest: {names}."
    return round(best_score, 3), f"Only a partial match with its interests ({names})."
