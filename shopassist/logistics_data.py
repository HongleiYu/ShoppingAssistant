"""Frozen synthetic shipment snapshots. Not a carrier integration."""

SHIPMENTS = {
    "TRK-OK": {
        "status": "ok",
        "tracking_number": "TRK-OK",
        "logistics_status": "in_transit",
        "eta": "2026-02-02",
        "freshness": "current",
    },
    "TRK-EMPTY": {"status": "empty", "tracking_number": "TRK-EMPTY", "freshness": "current"},
    "TRK-STALE": {
        "status": "stale",
        "tracking_number": "TRK-STALE",
        "logistics_status": "in_transit",
        "eta": "2026-01-02",
        "freshness": "stale",
    },
    "TRK-CONFLICT": {"status": "conflict", "tracking_number": "TRK-CONFLICT", "freshness": "current"},
}


def lookup_shipment(tracking_id: str) -> dict:
    row = SHIPMENTS.get(tracking_id)
    if row is None:
        return {"status": "empty", "tracking_number": tracking_id, "freshness": "current"}
    return dict(row)
