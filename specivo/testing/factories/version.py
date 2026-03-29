"""factory_boy factory for the Version model."""

from __future__ import annotations

import factory

from specivo.models.version import Version


class VersionFactory(factory.Factory):
    """Builds Version model instances.

    ``project_id`` must be overridden with a real project ID from fixtures.

    Usage::

        version = VersionFactory.build(
            project_id=project.id,
            name="v1.0",
        )
    """

    class Meta:
        model = Version

    project_id = 1
    name = factory.Sequence(lambda n: f"v1.{n}")
    description = None
    status = "open"
    effective_date = None
    sharing = "none"
    wiki_page_title = None
