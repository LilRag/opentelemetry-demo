"""The allowlisted incident catalog.

Every entry is backed by a mechanism verified in the OpenTelemetry Demo source:
flag incidents by a flag in src/flagd/demo.flagd.json that a service reads,
container incidents by a container defined in compose.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FLAG = "flag"
CONTAINER_STOP = "container-stop"

OFF = "off"

# Containers faultctl needs running before it touches flags. They are inspected
# (to prove they belong to the configured compose project) but never modified.
FLAG_CONTROL_PLANE = ("frontend-proxy", "flagd", "flagd-ui")


@dataclass(frozen=True)
class Incident:
    id: str
    summary: str
    target: str
    mechanism: str
    effects: str
    verification: str
    limitations: str = ""
    flag: str | None = None
    default_variant: str | None = None
    variants: tuple[str, ...] = ()
    targeting_rule: bool = False
    eval_context: tuple[tuple[str, str], ...] = ()
    container: str | None = None
    requires: tuple[str, ...] = field(default=())

    @property
    def context(self) -> dict[str, str]:
        return dict(self.eval_context)


_FLAG_VERIFY = (
    "Injection: flagd-ui stores the new variant, then flagd itself is asked to "
    "evaluate the flag through the frontend proxy (/flagservice) and must return "
    "that variant. Reset: same checks against the 'off' variant."
)

_CONTAINER_VERIFY = (
    "Injection: docker inspect reports the container is no longer running. "
    "Reset: docker inspect reports it running (and healthy, if it has a healthcheck)."
)

_PERCENT = ("10%", "25%", "50%", "75%", "90%", "100%")

INCIDENTS: dict[str, Incident] = {
    i.id: i
    for i in (
        Incident(
            id="payment-failure",
            summary="Payment service fails a share of charge requests",
            target="payment",
            mechanism=FLAG,
            flag="paymentFailure",
            default_variant="50%",
            variants=_PERCENT,
            effects=(
                "payment Charge spans error with 'Payment request failed. Invalid token'; "
                "checkout PlaceOrder errors; frontend checkout returns 5xx."
            ),
            verification=_FLAG_VERIFY,
            limitations="The '90%' variant is defined as 0.95 in the demo's flag file.",
        ),
        Incident(
            id="payment-unreachable",
            summary="Checkout cannot reach the payment service",
            target="checkout -> payment",
            mechanism=FLAG,
            flag="paymentUnreachable",
            default_variant="on",
            variants=("on",),
            effects="checkout PlaceOrder fails with connection errors to payment; no payment spans for those orders.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="cart-failure",
            summary="Cart service fails intermittently (EmptyCart)",
            target="cart",
            mechanism=FLAG,
            flag="cartFailure",
            default_variant="50%",
            variants=_PERCENT,
            effects="cart EmptyCart calls fail for a share of requests; checkout errors after order placement.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="product-catalog-failure",
            summary="Product catalog fails for one specific product (OLJCESPC7Z)",
            target="product-catalog",
            mechanism=FLAG,
            flag="productCatalogFailure",
            default_variant="on",
            variants=("on",),
            targeting_rule=True,
            eval_context=(("product_id", "OLJCESPC7Z"),),
            effects="GetProduct errors only for product OLJCESPC7Z; frontend product page and recommendations that include it fail.",
            verification=_FLAG_VERIFY + " Evaluation uses product_id=OLJCESPC7Z.",
            limitations="Toggled through the flag's targeting rule, the same way the flagd-ui dashboard edits it.",
        ),
        Incident(
            id="product-catalog-lock-contention",
            summary="Lock contention on the product catalog database",
            target="product-catalog / astronomy-db",
            mechanism=FLAG,
            flag="productCatalogLockContention",
            default_variant="on",
            variants=("on",),
            effects="product-catalog database query latency rises; slow GetProduct/ListProducts spans.",
            verification=_FLAG_VERIFY,
            limitations="product-catalog polls this flag every 10 seconds, so the effect starts with a delay.",
        ),
        Incident(
            id="recommendation-cache-leak",
            summary="Memory leak in the recommendation service from a runaway cache",
            target="recommendation",
            mechanism=FLAG,
            flag="recommendationCacheFailure",
            default_variant="on",
            variants=("on",),
            effects="recommendation memory grows steadily; ListRecommendations latency rises; possible OOM restarts.",
            verification=_FLAG_VERIFY,
            limitations="Memory already leaked is not reclaimed by resetting the flag; the container may need a restart via the Action API.",
        ),
        Incident(
            id="ad-high-cpu",
            summary="High CPU load in the ad service",
            target="ad",
            mechanism=FLAG,
            flag="adHighCpu",
            default_variant="on",
            variants=("on",),
            effects="ad container CPU rises; GetAds latency increases.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="ad-manual-gc",
            summary="Full manual garbage collections in the ad service",
            target="ad",
            mechanism=FLAG,
            flag="adManualGc",
            default_variant="on",
            variants=("on",),
            effects="JVM GC metrics for ad spike; GetAds latency increases.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="ad-failure",
            summary="Ad service fails GetAds requests",
            target="ad",
            mechanism=FLAG,
            flag="adFailure",
            default_variant="on",
            variants=("on",),
            effects="ad GetAds spans error; frontend ad component fails.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="email-memory-leak",
            summary="Memory leak in the email service",
            target="email",
            mechanism=FLAG,
            flag="emailMemoryLeak",
            default_variant="100x",
            variants=("1x", "10x", "100x", "1000x", "10000x"),
            effects="email container memory grows with each confirmation email sent.",
            verification=_FLAG_VERIFY,
            limitations="Leaked memory is not reclaimed by resetting the flag.",
        ),
        Incident(
            id="homepage-flood",
            summary="Traffic flood on the homepage from the load generator",
            target="load-generator -> frontend",
            mechanism=FLAG,
            flag="loadGeneratorFloodHomepage",
            default_variant="on",
            variants=("on",),
            effects="frontend request rate and latency spike; frontend-proxy and frontend CPU rise.",
            verification=_FLAG_VERIFY,
            requires=("load-generator",),
        ),
        Incident(
            id="image-slow-load",
            summary="Slow product image loading in the frontend",
            target="frontend / image-provider",
            mechanism=FLAG,
            flag="imageSlowLoad",
            default_variant="5sec",
            variants=("5sec", "10sec"),
            effects="Browser-side image requests are delayed; visible mainly in browser (RUM) spans.",
            verification=_FLAG_VERIFY,
            limitations="The delay is applied in the browser, so server-side telemetry shows little.",
        ),
        Incident(
            id="intl-shipping-slowdown",
            summary="International shipping quotes are delayed",
            target="shipping",
            mechanism=FLAG,
            flag="intlShippingSlowdown",
            default_variant="5sec",
            variants=("5sec", "10sec"),
            effects="shipping GetQuote latency rises for non-US addresses.",
            verification=_FLAG_VERIFY,
        ),
        Incident(
            id="kafka-queue-problems",
            summary="Fraud-detection consumer is throttled; Kafka lag climbs",
            target="fraud-detection / kafka",
            mechanism=FLAG,
            flag="kafkaQueueProblems",
            default_variant="1sec",
            variants=("1sec", "3sec", "5sec"),
            effects="Kafka consumer lag for the orders topic grows; fraud-detection processing latency rises.",
            verification=_FLAG_VERIFY,
            limitations="Needs the full stack (compose.full.yaml) with kafka and fraud-detection.",
            requires=("kafka", "fraud-detection"),
        ),
        Incident(
            id="kafka-consumer-dead",
            summary="Fraud-detection leaves the orders consumer group",
            target="fraud-detection / kafka",
            mechanism=FLAG,
            flag="kafkaConsumerDead",
            default_variant="on",
            variants=("on",),
            effects="orders consumer group has zero members; lag climbs while checkout keeps producing.",
            verification=_FLAG_VERIFY,
            limitations="Needs the full stack (compose.full.yaml) with kafka and fraud-detection.",
            requires=("kafka", "fraud-detection"),
        ),
        Incident(
            id="cache-unavailable",
            summary="The cart cache (valkey-cart) is stopped",
            target="valkey-cart -> cart",
            mechanism=CONTAINER_STOP,
            container="valkey-cart",
            effects="cart service errors connecting to valkey; add-to-cart and checkout fail; valkey-cart metrics stop.",
            verification=_CONTAINER_VERIFY,
            limitations="valkey-cart keeps carts in memory, so carts that existed before the stop are lost. No volumes are touched.",
        ),
        Incident(
            id="database-unavailable",
            summary="The product database (astronomy-db) is stopped",
            target="astronomy-db -> product-catalog",
            mechanism=CONTAINER_STOP,
            container="astronomy-db",
            effects="product-catalog database errors; product listing and product pages fail.",
            verification=_CONTAINER_VERIFY,
            limitations="Data lives in the container's storage and survives a stop/start; faultctl never removes containers or volumes.",
        ),
        Incident(
            id="currency-down",
            summary="The currency service container is stopped",
            target="currency",
            mechanism=CONTAINER_STOP,
            container="currency",
            effects="frontend and checkout calls to currency fail with connection errors; no currency spans.",
            verification=_CONTAINER_VERIFY,
        ),
    )
}

# The only containers faultctl may ever stop or start.
CONTROLLABLE_CONTAINERS = frozenset(i.container for i in INCIDENTS.values() if i.container)


def get_incident(incident_id: str) -> Incident | None:
    return INCIDENTS.get(incident_id)


def flag_incidents() -> list[Incident]:
    return [i for i in INCIDENTS.values() if i.mechanism == FLAG]
