"""The health response has no edition tier.

The template and wording rules live in ``scripts/check_static_rules.py``
(run by ``make lint`` and CI).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_health_response_has_no_tier():
    from specivo.schemas.common import HealthResponse

    assert "tier" not in HealthResponse.model_fields
    body = HealthResponse(status="ok", database="ok", redis="ok", version="").model_dump()
    assert set(body) == {"status", "database", "redis", "version"}
