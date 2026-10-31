---
description: "介绍 Specivo 中的父子工作项：如何把工作项设为子工作项、构建最多 16 层的层级结构，并在父工作项下汇总查看所有子工作项的进展。"
---

# 子工作项与层级结构 {#subtasks-hierarchy}

大块工作很少能塞进单个工作项（Issue）。在 Specivo 中，你可以给一个工作项指定 **父工作项**，它就成了 **子工作项**。父工作项是总纲，子工作项是挂在它下面的具体内容。

## 把工作项设为子工作项 {#make-an-issue-a-subtask}

在子工作项上设置 **父工作项** 字段，指向父工作项即可。可以在[创建](creating.md)工作项时设置，也可以之后[编辑](updating.md)时再设。要给一个父工作项挂多个子工作项，先创建父工作项，再把每个子工作项指向它。

## 构建层级结构 {#build-a-hierarchy}

子工作项自身还可以再有子工作项，因此你能照着实际结构来建模：一个 Epic 下有多个功能，每个功能下又有多个任务。Specivo 支持 **最多 16 层** 嵌套，远超绝大多数团队的实际需要。

```
ACME-1  Checkout redesign            (parent)
├─ ACME-2  Cart page
│  ├─ ACME-5  Update line-item layout
│  └─ ACME-6  Wire up quantity stepper
└─ ACME-3  Payment step
   └─ ACME-7  Add saved-card selector
```

## 子工作项如何汇总 {#how-subtasks-roll-up}

打开父工作项，它的所有子工作项都会列在下方。这样，在父工作项里就能一眼看到全部下级工作以及每项的进展。用父工作项追踪整体进度，用子工作项指派和推进具体任务。

!!! tip "子工作项与关联的区别"
    当一个工作项确实是另一个更大工作项的*组成部分*时，使用 **父/子** 层级。当两个工作项是平级关系，只是彼此依赖或互相引用时，使用[关联](relations.md)（阻塞、关联到、重复于）。

!!! note "跨项目移动前先脱离层级"
    只有 **独立** 的工作项才能[移动到其他项目](updating.md#move-an-issue-to-another-project)。
    带有父工作项或自身带有子工作项的工作项，必须先从层级结构中脱离出来，这样父工作项和它的子工作项就不会被拆散到不同项目。
