"""M9 분석·어트리뷰션 (Analytics & Attribution), design §4 M9 and the §9.3 dashboards."""

from acvm.modules.m9_analytics.dashboards import (
    channel_kpis,
    learning_view,
    money_view,
    pipeline_view,
    risk_view,
)
from acvm.modules.m9_analytics.service import AnalyticsService, PublishedRef, load_published
from acvm.modules.m9_analytics.sources import CoupangReports, InsightsClient, aggregate_reports

__all__ = [
    "AnalyticsService",
    "CoupangReports",
    "InsightsClient",
    "PublishedRef",
    "aggregate_reports",
    "channel_kpis",
    "learning_view",
    "load_published",
    "money_view",
    "pipeline_view",
    "risk_view",
]
