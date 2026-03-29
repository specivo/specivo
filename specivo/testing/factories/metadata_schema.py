"""factory_boy factory for the MetadataSchema model."""

from __future__ import annotations

import factory

from specivo.models.metadata_schema import MetadataSchema


class MetadataSchemaFactory(factory.Factory):
    """Builds MetadataSchema model instances.

    ``project_id`` must be overridden with a real project ID from fixtures.

    Usage::

        schema = MetadataSchemaFactory.build(
            project_id=project.id,
            name="Bug Fields",
            schema_definition={"type": "object", "properties": {"severity": {"type": "string"}}},
        )
    """

    class Meta:
        model = MetadataSchema

    project_id = 1
    tracker_id = None
    name = factory.Sequence(lambda n: f"Schema {n}")
    description = None
    schema_definition = factory.LazyFunction(lambda: {"type": "object"})
