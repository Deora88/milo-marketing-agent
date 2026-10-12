from app.config import get_settings
from app.metrics.base import DailyMetrics, MetricsProvider
from app.metrics.simulated import SimulatedInsightsProvider


def get_provider() -> MetricsProvider:
    if get_settings().metrics_provider == "meta":
        from app.meta.client import MetaClient
        from app.metrics.meta_insights import MetaInsightsProvider

        return MetaInsightsProvider(MetaClient())
    return SimulatedInsightsProvider()


__all__ = ["DailyMetrics", "MetricsProvider", "get_provider"]
