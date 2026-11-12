"""A schema property's title is the field's name; without one the key is humanised."""

from __future__ import annotations

import pytest

from specivo.services.metadata_fields import field_label, humanise

pytestmark = pytest.mark.unit

THAI = "ระดับความเสี่ยง"


def test_a_title_is_the_name_and_a_key_reads_as_one_without():
    assert field_label("customfield_10044", {"title": THAI}) == THAI
    assert field_label("qa_owner", {"type": "integer"}) == "Qa Owner"
    assert field_label("qa_owner", {"title": "  "}) == "Qa Owner"
    assert humanise("customfield_10044") == "Customfield 10044"
