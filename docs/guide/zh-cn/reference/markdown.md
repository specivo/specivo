---
description: 介绍 Specivo 在工作项描述、评论和维基页面中渲染的 Markdown 语法，包括文本格式、代码块、图表、@用户名提及和 [[维基链接]]。
---

# 文本格式 {#text-formatting}

Specivo 在所有需要撰写较长文本的地方都渲染 **Markdown**：工作项（Issue）描述、评论和维基（Wiki）页面。三处使用完全相同的语法。本页介绍支持的语法，以及两个 Specivo 特有的扩展：`@username` 提及和 `[[wiki links]]`。

## 标题 {#headings}

```markdown
# Heading 1
## Heading 2
### Heading 3
```

在维基中，页面标题已经作为页面大标题显示，因此正文请从 `##` 开始，不要再重复一个 `#` 一级标题。

## 强调 {#emphasis}

| Markdown | 效果 |
|---|---|
| `**bold**` | **粗体** |
| `*italic*` | *斜体* |
| `***bold italic***` | ***粗斜体*** |
| `` `inline code` `` | `inline code` |

## 列表 {#lists}

```markdown
- First item
- Second item
  - Nested item

1. Step one
2. Step two
```

任务列表同样可用：

```markdown
- [x] Done
- [ ] Not done yet
```

## 链接与图片 {#links-and-images}

```markdown
[Link text](https://specivo.example.com)
![Alt text](https://specivo.example.com/image.png)
```

要在工作项或维基页面中插入图片，先把它作为[附件](../issues/attachments.md)上传，再链接到它。

## 引用 {#blockquotes}

```markdown
> A quoted line.
> Continues the same quote.
```

## 表格 {#tables}

```markdown
| Field    | Type   | Required |
|----------|--------|----------|
| subject  | string | yes      |
| due date | date   | no       |
```

管道表格会渲染为带样式的表格，适合用于字段列表和选项矩阵。

## 代码块 {#code-blocks}

用三个反引号包裹代码，并加上语言提示以启用语法高亮：

````markdown
```python
def hello(name: str) -> str:
    return f"Hello, {name}"
```
````

语言提示（`python`、`bash`、`json`、`yaml`、`sql` 等）决定高亮配色。

## 图表 {#diagrams}

语言提示为 `mermaid` 的代码块会按 [Mermaid](https://mermaid.js.org/) 语法绘制成图表，支持流程图、时序图、类图、状态图、甘特图、饼图等。

````markdown
```mermaid
flowchart LR
    New --> InProgress[In progress] --> Review --> Done
    Review -->|changes requested| InProgress
```
````

图表可用于工作项描述、评论、维基页面和编辑器预览，且无需联网。标签只是纯文本：标签中的 HTML 会原样显示，`click` 链接和回调不会生效。如果图表有语法错误，会在一条简短提示下方以代码块形式显示其源码。搜索和导出看到的是图表的源文本。比页面更宽的图表在宽屏上会缩小以适应页面；在手机上则保持原始大小，可横向滚动查看。

## @用户名提及 {#username-mentions}

输入 `@` 加用户名即可提及某人。提及会链接到对方的个人资料，并**通知对方**：

```markdown
@alex can you review this before the release?
```

提及可用于工作项描述和评论。用它把特定的人拉进讨论。

## 工作项引用 {#issue-references}

在描述、评论或维基页面中写下工作项编号（例如 `ACME-123`），Specivo 会自动把它变成指向该工作项的链接。无需任何特殊语法，直接写编号即可。

Specivo **只在工作项确实存在时**才会生成链接（或者该工作项曾经存在、后来被[移动](../issues/updating.md#move-an-issue-to-another-project)到了其他项目，此时旧编号仍能解析）。看起来像编号但并不对应真实工作项的文本，例如版本号 `API-2`、零件编号或写错的编号，会保持为纯文本，而不会变成死链。这样引用才可信：只要是链接，另一端就一定有一个真实的工作项。

对你无权查看的工作项也同理。私有工作项或因你的角色而对你隐藏的工作项，其编号不会为你生成链接，只显示为纯文本，因此引用不会泄露该工作项的存在。代码中（代码块或 `` `inline code` ``）或原始 HTML 块中的编号保持原样。

## 维基交叉链接 {#wiki-cross-links}

在维基中，用双方括号链接到同一项目内的另一个页面：

```markdown
See [[Release Process]] for the steps.
```

可以按标题（`[[Release Process]]`）或按 slug（`[[release-process]]`）链接。slug 会被规范化，因此 `[[My Page]]`、`[[my-page]]` 和 `[[My_Page]]` 都指向同一个页面。代码中（代码块或 `` `inline code` ``）或原始 HTML 块中的双方括号会原样显示，不会变成链接。更多内容见[链接与历史](../wiki/history-linking.md)。

## 长度与复杂度限制 {#size-and-complexity-limits}

描述、评论、维基页面，以及版本或重复任务的描述，最多可容纳 **262,144 个字符**（256 Ki）。保存超长文本会被拒绝，并提示该限制；请把内容拆分到多个页面或评论中。编辑器预览适用同样的限制。已存储的超出限制的文本（例如导入的内容）会保留，并以纯文本显示。

格式化耗时过长的文本会在一条简短提示下方**以纯文本显示**，内容不会被删减。普通文档永远不会触发这一机制，哪怕包含数千个链接。它针对的是诸如数千个未闭合的 `[` 或 `<`、同一标题重复数千次，或嵌套超过 50 层的列表和引用之类的文本。编辑文本后即可恢复正常渲染。

!!! tip "保持简单"
    请只使用上面列出的元素。Specivo 渲染标准 Markdown 以及本页介绍的两个扩展；如果某个语法没有列在这里，不要假定它能被渲染。
