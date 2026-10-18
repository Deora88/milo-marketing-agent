from app.config import get_settings
from app.metrics.base import DailyMetrics, MetricsProvider
from app.metrics.simulated import SimulatedInsightsProvider


def effective_provider() -> str:
    """Dry run never calls Meta, so it always uses simulated (clearly labelled) data."""
    s = get_settings()
    return "simulated" if s.meta_dry_run else s.metrics_provider


def get_provider() -> MetricsProvider:
    """Callers must `await provider.aclose()` when done (closes the HTTP client of the Meta provider)."""
    if effective_provider() == "meta":
        from app.meta.client import MetaClient
        from app.metrics.meta_insights import MetaInsightsProvider

        return MetaInsightsProvider(MetaClient())
    return SimulatedInsightsProvider()


__all__ = ["DailyMetrics", "MetricsProvider", "effective_provider", "get_provider"]
