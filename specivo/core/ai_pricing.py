"""Pure AI model pricing: token counts, per-1M-token prices, the cost formula and model names.

Nothing here touches the database. :mod:`specivo.services.ai_model_price_service`
stores prices (``ai_model_prices``) and resolves the one valid at an instant;
this module turns a price and some token counts into money.

**Token classes.** Usage is counted in six classes, in the semantics the AI
usage reporter normalises to:

- ``input`` -- fresh input tokens, excluding cache reads and writes;
- ``output`` -- generated tokens, excluding reasoning when reported separately;
- ``cache_write_5m`` / ``cache_write_1h`` -- prompt cache writes with a five
  minute or one hour lifetime;
- ``cache_read`` -- prompt cache hits;
- ``reasoning`` -- reasoning ("thinking") tokens reported apart from output.

**Prices** are per one million tokens, in one currency, ``NUMERIC(18, 6)``.
A class whose price is unknown (``None``) is *unpriced*, which is different
from a price of zero.

**Long context.** Some providers bill a whole request at higher rates once its
context passes a threshold (Anthropic and Gemini at 200K tokens). The
threshold belongs to the request, not to a token total, so the caller splits
usage into two buckets per request with :func:`is_long_context_request` and
passes them to :func:`price_usage` separately: normal tokens are priced with
the base prices, long-context tokens with the ``long_*`` prices, falling back
to the base price of the same class where no long price is set.

The cost rules follow the providers' published billing: a one-hour cache
write costs twice the input price, and the long-context tier is chosen by the
request's whole context, cached tokens included.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from decimal import Decimal

from specivo.core.money import Money

#: Token classes in a fixed order (also the order of the price columns).
TOKEN_CLASSES: tuple[str, ...] = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read", "reasoning")

#: Price columns of the long-context tier. Reasoning has none of its own: in
#: a long request it is billed like output (``long_output``).
LONG_PRICE_CLASSES: tuple[str, ...] = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")

#: One-hour cache writes cost twice the input price when a source has no
#: explicit price for them (the providers' published one-hour cache write rate).
CACHE_WRITE_1H_INPUT_MULTIPLIER = Decimal(2)

ONE_MILLION = Decimal(1_000_000)

#: Per-1M prices are stored as ``NUMERIC(18, 6)``.
PRICE_MAX_INTEGER_DIGITS = 12
PRICE_MAX_DECIMAL_PLACES = 6


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Token counts per class; every count is a non-negative ``int``."""

    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    reasoning: int = 0

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Token count {f.name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"Token count {f.name} must not be negative")

    @classmethod
    def from_mapping(cls, values: dict[str, int] | None) -> TokenCounts:
        """Build from a ``{class: count}`` mapping; missing classes are zero, unknown keys refused."""
        if not values:
            return cls()
        unknown = set(values) - set(TOKEN_CLASSES)
        if unknown:
            raise ValueError(f"Unknown token classes: {', '.join(sorted(unknown))}")
        return cls(**values)

    def get(self, token_class: str) -> int:
        return int(getattr(self, token_class))

    @property
    def total(self) -> int:
        return sum(self.get(c) for c in TOKEN_CLASSES)

    @property
    def context_tokens(self) -> int:
        """The context a request carried: fresh input plus cache reads and writes."""
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def is_zero(self) -> bool:
        return self.total == 0


def is_long_context_request(request_tokens: TokenCounts, threshold: int | None) -> bool:
    """Whether one request is billed at the long-context tier.

    The tier is chosen by the request's whole context, cached or not: a turn
    that re-reads 500K cached tokens with 10K fresh ones is a long-context
    request. Exactly at the threshold it is not; one token more and the whole
    request is (two-stage pricing, not a marginal breakpoint).
    """
    return threshold is not None and request_tokens.context_tokens > threshold


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-1M-token prices of one model in one currency (one validity period of it).

    ``reasoning`` ``None`` means reasoning tokens are billed at the output
    price. Any other ``None`` price leaves its class unpriced.
    """

    currency: str
    input: Decimal
    output: Decimal
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None
    cache_read: Decimal | None = None
    reasoning: Decimal | None = None
    long_context_threshold: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None
    long_cache_write_5m: Decimal | None = None
    long_cache_write_1h: Decimal | None = None
    long_cache_read: Decimal | None = None

    def rate(self, token_class: str, *, long_context: bool = False) -> Decimal | None:
        """The price per 1M tokens of *token_class*, or ``None`` when it is unpriced.

        In the long-context tier a class uses its ``long_*`` price when set,
        else the base price of the class. Reasoning uses its own price when one
        is set (in both tiers; there is no long reasoning price), otherwise it
        is billed like output in the same tier.
        """
        if token_class not in TOKEN_CLASSES:
            raise ValueError(f"Unknown token class {token_class!r}")
        # Candidate price columns, most specific first; the first one set wins.
        billed_as = "output" if token_class == "reasoning" else token_class
        candidates: list[str] = ["reasoning"] if token_class == "reasoning" else []
        if long_context:
            candidates.append(f"long_{billed_as}")
        candidates.append(billed_as)
        for column in candidates:
            value: Decimal | None = getattr(self, column)
            if value is not None:
                return value
        return None


@dataclass(frozen=True, slots=True)
class PricedUsage:
    """The API-equivalent cost of some usage.

    ``money`` sums every priced class. ``unpriced_classes`` names the classes
    that had tokens but no price; their tokens are left out of ``money``, so
    the result is *partial* (never silently complete).
    """

    money: Money
    unpriced_classes: tuple[str, ...] = field(default=())

    @property
    def is_partial(self) -> bool:
        return bool(self.unpriced_classes)

    @property
    def basis(self) -> str:
        """``calculated`` or ``partial`` (the ``cost_basis`` of a usage entry)."""
        return "partial" if self.unpriced_classes else "calculated"


def price_usage(tokens: TokenCounts, long_context_tokens: TokenCounts | None, price: ModelPrice) -> PricedUsage:
    """``Σ tokens_class × price_class / 1M`` over both buckets, in the price's currency.

    *tokens* (the requests below the threshold) are priced with the base
    prices, *long_context_tokens* (the requests :func:`is_long_context_request`
    put in the long tier; ``None`` for none) with the long-context prices. The
    two buckets are disjoint and additive: a request is counted wholly in one
    of them, so each token is priced exactly once. Exact ``Decimal`` arithmetic, never
    rounded. Classes with tokens but no price are reported in
    ``unpriced_classes``.
    """
    tiers = {False: tokens, True: long_context_tokens or TokenCounts()}
    # Every (class, tier) with tokens, base tier first, classes in column order.
    lines = [
        (token_class, counts.get(token_class), price.rate(token_class, long_context=is_long))
        for is_long, counts in tiers.items()
        for token_class in TOKEN_CLASSES
        if counts.get(token_class)
    ]
    token_cost = sum((Decimal(count) * rate for _, count, rate in lines if rate is not None), Decimal(0))
    # dict keys keep first-seen order and drop a class missing in both tiers twice.
    unpriced = tuple(dict.fromkeys(token_class for token_class, _, rate in lines if rate is None))
    return PricedUsage(Money(token_cost / ONE_MILLION, price.currency), unpriced)


def default_cache_write_1h(input_price: Decimal | None) -> Decimal | None:
    """The one-hour cache write price when none is published: twice the input price."""
    if input_price is None:
        return None
    return CACHE_WRITE_1H_INPUT_MULTIPLIER * input_price


# ---------------------------------------------------------------------------
# Model names
# ---------------------------------------------------------------------------

#: Leading path segments that name a provider (or a router in front of one),
#: mapped to the provider they imply; ``None`` implies none.
PROVIDER_PREFIXES: dict[str, str | None] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
    "google": "gemini",
    "models": None,  # the Gemini API's "models/gemini-2.5-pro"
    "xai": "xai",
    "x-ai": "xai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "mistralai": "mistral",
    "moonshot": "moonshot",
    "moonshotai": "moonshot",
    "zai": "zai",
    "z-ai": "zai",
    "dashscope": "dashscope",
    "qwen": "dashscope",
    "openrouter": None,
    "litellm": None,
}

PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,49}$")
MODEL_NAME_MAX_LENGTH = 200


def normalize_provider(value: str | None) -> str | None:
    """A provider slug in lower case (``"Anthropic "`` -> ``"anthropic"``); ``None`` for empty."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    return PROVIDER_PREFIXES.get(text) or text


def normalize_model_name(name: str) -> tuple[str | None, str]:
    """Split a reported model name into ``(provider hint, model)``.

    Lower case, surrounding white space removed, and leading provider or router
    segments stripped: ``"Anthropic/claude-opus-4-1"`` gives
    ``("anthropic", "claude-opus-4-1")``, ``"openrouter/openai/gpt-5"`` gives
    ``("openai", "gpt-5")`` and ``"models/gemini-2.5-pro"`` gives
    ``(None, "gemini-2.5-pro")``. Unknown segments are kept as part of the name.
    """
    text = name.strip().lower()
    hint: str | None = None
    while "/" in text:
        head, rest = text.split("/", 1)
        if head not in PROVIDER_PREFIXES or not rest:
            break
        hint = PROVIDER_PREFIXES[head] or hint
        text = rest
    return hint, text
