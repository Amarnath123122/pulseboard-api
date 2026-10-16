"""Initial metric catalog. Loaded into the `metrics` table once (empty database); after that the
database is the source of truth and metrics are managed through /api/v1/metrics."""

from typing import Any

INITIAL_METRICS: dict[str, dict[str, Any]] = {
    "sales.bookings": {
        "area": "sales", "title": "New bookings", "unit": "USD", "base": 840_000, "volatility": 0.07,
        "higher_is_better": True, "owners": ["Revenue Operations"], "sources": ["crm.deals", "warehouse.bookings"],
    },
    "sales.pipeline_coverage": {
        "area": "sales", "title": "Pipeline coverage", "unit": "x", "base": 3.2, "volatility": 0.06,
        "higher_is_better": True, "owners": ["Sales Strategy"], "sources": ["crm.pipeline", "warehouse.quota"],
    },
    "customers.churn_rate": {
        "area": "customers", "title": "Logo churn", "unit": "%", "base": 2.1, "volatility": 0.10,
        "higher_is_better": False, "owners": ["Customer Success"], "sources": ["billing.subscriptions"],
    },
    "customers.nps": {
        "area": "customers", "title": "Net promoter score", "unit": "pts", "base": 41.0, "volatility": 0.06,
        "higher_is_better": True, "owners": ["Customer Insights"], "sources": ["survey.responses"],
    },
    "operations.delivery_lead_time": {
        "area": "operations", "title": "Order-to-delivery time", "unit": "days", "base": 4.8, "volatility": 0.07,
        "higher_is_better": False, "owners": ["Fulfilment"], "sources": ["oms.orders", "carrier.events"],
    },
    "operations.support_backlog": {
        "area": "operations", "title": "Open support tickets", "unit": "tickets", "base": 320, "volatility": 0.12,
        "higher_is_better": False, "owners": ["Support"], "sources": ["helpdesk.tickets"],
    },
    "engineering.availability": {
        "area": "engineering", "title": "Service availability", "unit": "%", "base": 99.6, "volatility": 0.004,
        "higher_is_better": True, "owners": ["Site Reliability"], "sources": ["monitoring.probes"],
    },
    "engineering.change_failure_rate": {
        "area": "engineering", "title": "Change failure rate", "unit": "%", "base": 12.0, "volatility": 0.12,
        "higher_is_better": False, "owners": ["Platform Engineering"], "sources": ["ci.deployments", "incidents.log"],
    },
    "finance.cloud_spend": {
        "area": "finance", "title": "Cloud spend", "unit": "USD", "base": 210_000, "volatility": 0.05,
        "higher_is_better": False, "owners": ["FinOps"], "sources": ["cloud.billing_export"],
    },
}
