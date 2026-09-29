"""Seed the Non member role and keep builtin roles system-managed.

Create Date: 2026-09-15
Revision ID: 0032
Revises: 0031

A signed-in user without a membership on a public project now resolves to one
seeded role, ``roles.builtin = 1``, instead of hardcoded rules scattered across
search and issue visibility. It is read-only: ``permissions = ["view_issues"]``
with ``issues_visibility = 'default'``, which is exactly what such a user could
already see through issue listing and search. Unlike Redmine's Non member role
it grants no ``add_issues`` or ``add_issue_notes``, so the upgrade gives nobody
write access to a public project.

An instance imported from Redmine may already hold a ``builtin = 1`` row,
created by the importer with no permissions. That row is adopted and brought
to the seeded definition instead of adding a second one. A ``builtin = 2`` row
is left as it is: nothing creates or reads one, because anonymous visitors
get a transient role from the project's ``anonymous_permissions``.

The database keeps builtin roles system-managed, whatever code path writes:

- ``uq_roles_single_builtin``: one row per builtin kind;
- ``ck_roles_builtin_not_assignable``: builtin roles are never assignable;
- ``trg_protect_builtin_roles``: a builtin role cannot be deleted, and no
  role's ``builtin`` can change;
- ``trg_reject_builtin_role_membership``: a builtin role cannot be put on a
  membership.

Their permissions stay editable.

The upgrade stops with an explanation instead of guessing when an existing
role blocks it: more than one row for a builtin kind, a builtin role held by
a membership, or a custom role already named "Non member".

Downgrade drops the triggers, the constraint and the index, and keeps the
role row, which nothing reads at 0031. Upgrading again adopts it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"

_NAME = "Non member"


def _refuse_blocking_rows(bind: sa.engine.Connection) -> None:
    duplicate = bind.execute(
        sa.text("SELECT builtin, count(*) AS n FROM roles WHERE builtin > 0 GROUP BY builtin HAVING count(*) > 1")
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            f"{duplicate.n} roles have builtin={duplicate.builtin}, but there can only be one. "
            "Delete or merge the extra roles and run the upgrade again."
        )

    held = bind.execute(
        sa.text(
            "SELECT r.id, r.name, count(*) AS n FROM member_roles mr JOIN roles r ON r.id = mr.role_id "
            "WHERE r.builtin > 0 GROUP BY r.id, r.name ORDER BY r.id"
        )
    ).first()
    if held is not None:
        raise RuntimeError(
            f"Role id={held.id} ({held.name!r}) is a builtin role but {held.n} project membership(s) hold it. "
            "Builtin roles apply to users without a membership and cannot be assigned. "
            "Remove those assignments and run the upgrade again."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_blocking_rows(bind)

    existing = bind.execute(sa.text("SELECT id FROM roles WHERE builtin = 1")).first()
    if existing is None:
        clash = bind.execute(sa.text("SELECT id FROM roles WHERE name = :name"), {"name": _NAME}).first()
        if clash is not None:
            raise RuntimeError(
                f"Role id={clash.id} is already named {_NAME!r}, the name reserved for the builtin Non member "
                "role. Rename that role and run the upgrade again."
            )
        bind.execute(
            sa.text(
                "INSERT INTO roles (name, position, assignable, builtin, permissions, issues_visibility, settings) "
                "VALUES (:name, 0, false, 1, CAST(:permissions AS jsonb), 'default', CAST('{}' AS jsonb))"
            ),
            {"name": _NAME, "permissions": '["view_issues"]'},
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE roles SET assignable = false, permissions = CAST(:permissions AS jsonb), "
                "issues_visibility = 'default' WHERE id = :id"
            ),
            {"id": existing.id, "permissions": '["view_issues"]'},
        )

    op.execute("UPDATE roles SET assignable = false WHERE builtin > 0 AND assignable")
    op.execute("CREATE UNIQUE INDEX uq_roles_single_builtin ON roles (builtin) WHERE builtin > 0")
    op.execute("ALTER TABLE roles ADD CONSTRAINT ck_roles_builtin_not_assignable CHECK (builtin = 0 OR NOT assignable)")

    op.execute(
        """
        CREATE FUNCTION protect_builtin_roles() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.builtin > 0 THEN
                    RAISE EXCEPTION 'A builtin role cannot be deleted (role id=%)', OLD.id
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.builtin IS DISTINCT FROM OLD.builtin THEN
                RAISE EXCEPTION 'A role''s builtin cannot change (role id=%)', OLD.id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_protect_builtin_roles
            BEFORE UPDATE OR DELETE ON roles
            FOR EACH ROW EXECUTE FUNCTION protect_builtin_roles()
        """
    )

    op.execute(
        """
        CREATE FUNCTION reject_builtin_role_membership() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM roles WHERE id = NEW.role_id AND builtin > 0) THEN
                RAISE EXCEPTION 'A builtin role cannot be assigned to a project membership (role id=%)', NEW.role_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reject_builtin_role_membership
            BEFORE INSERT OR UPDATE OF role_id ON member_roles
            FOR EACH ROW EXECUTE FUNCTION reject_builtin_role_membership()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_reject_builtin_role_membership ON member_roles")
    op.execute("DROP FUNCTION reject_builtin_role_membership()")
    op.execute("DROP TRIGGER trg_protect_builtin_roles ON roles")
    op.execute("DROP FUNCTION protect_builtin_roles()")
    op.execute("ALTER TABLE roles DROP CONSTRAINT ck_roles_builtin_not_assignable")
    op.execute("DROP INDEX uq_roles_single_builtin")
