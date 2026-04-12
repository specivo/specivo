"""factory_boy factory for Project model."""

from __future__ import annotations

import factory

from specivo.models.project import Project


class ProjectFactory(factory.Factory):
    """Builds Project model instances.

    All fields have sensible defaults. Override any field by passing kwargs::

        project = ProjectFactory.build(key="ACME", is_public=False)
    """

    class Meta:
        model = Project

    name = factory.Sequence(lambda n: f"Test Project {n}")
    identifier = factory.Sequence(lambda n: f"test-project-{n}")
    key = factory.Sequence(lambda n: f"TP{n:02d}")
    description = None
    parent_id = None
    path = factory.LazyAttribute(lambda obj: obj.identifier.replace("-", "_"))
    is_public = False
    inherit_members = False
    status = 1
    issue_sequence = 0
    settings = factory.LazyFunction(dict)
