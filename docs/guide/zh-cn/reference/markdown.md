---
description: 介绍 Specivo 在工作项描述、评论和维基页面中支持的 Markdown 语法，包括文本格式、代码块、图表、@用户名提及和 [[维基链接]]。
---

# 文本格式 {#text-formatting}

凡是需要写较长文本的地方，Specivo 都会渲染 **Markdown**：工作项（Issue）描述、评论和维基（Wiki）页面。三处使用完全相同的语法。本页介绍支持的语法，以及 Specivo 特有的两个扩展：`@username` 提及和 `[[wiki links]]` 维基链接。

## 标题 {#headings}

```markdown
# Heading 1
## Heading 2
### Heading 3
```

在维基中，页面标题本身已经作为页面的大标题显示，所以正文请从 `##` 开始，不要再重复写一个 `#` 一级标题。

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

任务列表也支持：

```markdown
- [x] Done
- [ ] Not done yet
```

## 链接与图片 {#links-and-images}

```markdown
[Link text](https://specivo.example.com)
![Alt text](https://specivo.example.com/image.png)
```

要在工作项或维基页面中嵌入图片，先把图片作为[附件](../issues/attachments.md)上传，再在正文中链接它。

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

管道表格会渲染为带样式的表格，适合用来列字段清单和选项矩阵。

## 代码块 {#code-blocks}

用三个反引号包住代码，并加上语言标识以启用语法高亮：

````markdown
```python
def hello(name: str) -> str:
    return f"Hello, {name}"
```
````

语言标识（`python`、`bash`、`json`、`yaml`、`sql` 等）决定高亮配色。

## 图表 {#diagrams}

语言标识为 `mermaid` 的代码块会按 [Mermaid](https://mermaid.js.org/) 语法绘制成图表：流程图、时序图、类图、状态图、甘特图、饼图等。

````markdown
```mermaid
flowchart LR
    New --> InProgress[In progress] --> Review --> Done
    Review -->|changes requested| InProgress
```
````

图表在工作项描述、评论、维基页面和编辑器预览中都可以使用，并且不需要联网。标签只支持纯文本：标签中的 HTML 会原样显示，`click` 链接和回调不起作用。如果图表有语法错误，会在一条简短提示下方以代码块形式显示图表源码。搜索和导出看到的是图表的源文本。

## @用户名提及 {#username-mentions}

输入 `@` 加用户名即可提及某人。提及会链接到对方的个人资料，并 **向对方发送通知**：

```markdown
@alex can you review this before the release?
```

提及可用于工作项描述和评论，适合把特定的人拉进讨论。

## 工作项引用 {#issue-references}

在描述、评论或维基页面中直接写出工作项编号（如 `ACME-123`），Specivo 就会把它转换成指向该工作项的链接。不需要任何特殊语法，写编号本身就够了。

Specivo **只在工作项确实存在时** 才生成链接（如果工作项曾被[移动](../issues/updating.md#move-an-issue-to-another-project)，旧编号仍然可以解析）。看起来像编号但并不对应真实工作项的文本，例如版本字符串 `API-2`、零件号、写错的编号，会保持为纯文本，而不会变成死链接。这样引用才可靠：只要是链接，另一端就一定有真实的工作项。

你无权查看的工作项也是同样处理。如果编号指向私有工作项，或者指向因你的角色而对你隐藏的工作项，它不会被转换为链接，只显示为纯文本，因此引用永远不会泄露该工作项的存在。代码（代码块或 `` `inline code` ``）和原始 HTML 块中的编号保持原样。

## 维基交叉链接 {#wiki-cross-links}

在维基内部，用双方括号链接到同一项目中的另一个页面：

```markdown
See [[Release Process]] for the steps.
```

既可以按标题链接（`[[Release Process]]`），也可以按 slug 链接（`[[release-process]]`）。slug 会被归一化，所以 `[[My Page]]`、`[[my-page]]` 和 `[[My_Page]]` 都指向同一个页面。代码（代码块或 `` `inline code` ``）和原始 HTML 块中的双方括号会原样显示，不会变成链接。详见[链接与历史记录](../wiki/history-linking.md)。

## 大小与复杂度限制 {#size-and-complexity-limits}

描述、评论、维基页面，以及版本或重复任务的描述，最多可容纳 **262,144 个字符**（256 Ki）。保存超长文本时会被拒绝，并提示上限；请把内容拆分到多个页面或评论中。编辑器预览的上限相同。已经存储且超过上限的文本（例如导入的内容）会被保留，并以纯文本显示。

格式化耗时过长的文本会 **以纯文本形式** 显示在一条简短提示下方，内容不会被删减。普通文档不会触发这一机制，哪怕包含上千个链接也不会。它针对的是诸如上千个未闭合的 `[` 或 `<`、同一标题重复上千次、嵌套超过 50 层的列表或引用之类的文本。编辑文本后就会恢复正常渲染。

!!! tip "保持简单"
    请只使用上面列出的元素。Specivo 渲染的是标准 Markdown 加上本页介绍的两个扩展——如果某种写法没有在这里列出，就不要假定它能被渲染。
