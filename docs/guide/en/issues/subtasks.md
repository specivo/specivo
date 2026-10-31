---
description: Parent and child issues in Specivo — build a hierarchy up to 16 levels deep and roll subtasks up under a parent.
---

# Subtasks & hierarchy

Large work rarely fits in a single issue. In Specivo you break it down by giving an issue a **parent**,
which turns it into a **subtask**. The parent becomes the umbrella; the subtasks are the concrete
pieces underneath it.

## Make an issue a subtask

Set the **parent issue** field on the child to point at the parent. You can do this when
[creating](creating.md) the issue or later when [editing](updating.md) it. To build a parent with
several children, create the parent first, then point each child at it.

## Build a hierarchy

Subtasks can have subtasks of their own, so you can model real structure — an epic with features, each
feature with tasks. Specivo supports nesting **up to 16 levels deep**, which is far more than most teams
ever need.

```
ACME-1  Checkout redesign            (parent)
├─ ACME-2  Cart page
│  ├─ ACME-5  Update line-item layout
│  └─ ACME-6  Wire up quantity stepper
└─ ACME-3  Payment step
   └─ ACME-7  Add saved-card selector
```

## How subtasks roll up

Open a parent issue and its subtasks are listed under it, so the parent gives you a single view of all
the work beneath it and how far along each piece is. Use the parent to track the whole effort and the
subtasks to assign and work the individual pieces.

!!! tip "Subtasks vs relations"
    Use a **parent/child** link when one issue is genuinely *part of* a bigger one. Use a
    [relation](relations.md) (blocks, relates, duplicates) when two issues are peers that simply depend
    on or reference each other.

!!! note "Detach before moving between projects"
    Only a **standalone** issue can be [moved to another project](updating.md#move-an-issue-to-another-project).
    An issue with a parent or its own subtasks has to be detached from the hierarchy first, so a parent
    and its children never end up split across projects.
