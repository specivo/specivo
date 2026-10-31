---
description: 让 Gitea 和 Forgejo 提交信息与评论中的工作项引用自动链接到 Specivo，方法是把 Specivo 设为仓库的外部工单系统，并选择字母数字、数字或正则表达式编号格式。
---

# 从 Gitea 和 Forgejo 链接到工作项 {#issue-links-from-gitea-and-forgejo}

Gitea 和 Forgejo 可以把另一个系统设为仓库的 **外部工单系统**（external issue tracker）。把它指向
Specivo 后，代码托管平台上提交信息、合并请求和评论里的每个工作项引用（`ACME-12` 或 `#12`）都会变成
指向 Specivo 中对应工作项（Issue）的链接，仓库的 **Issues** 标签页也可以直接打开你的 Specivo 项目，
而不是平台自带的工单。Gitea 和 Forgejo 的相关设置相同，只是位置略有不同；下文使用 Gitea 的英文标签
（Forgejo 的写法是句首大写，例如“Use external issue tracker”）。

这与[仓库集成](repositories.md)的方向相反：仓库集成是把平台的提交和合并请求发送*到* Specivo。两者可以
配合使用。

## Specivo 的工作项地址 {#where-specivos-issues-live}

每个工作项在你的 Specivo 地址下都有一个页面，路径为 `/issue/{key}/`：

```text
https://specivo.example.com/issue/ACME-12/
```

- 地址中的编号不区分大小写：`/issue/acme-12/` 打开的是同一个工作项。
- 已移动到其他项目的工作项，仍可用原编号打开。
- 管理员[重命名项目标识](../projects/index.md#renaming-a-project-key)后，旧编号下的工作项页面会重定向到
  新编号，现有链接不会失效。但仍应按下文更新平台的设置，让新链接使用新编号。
- 如果你的实例设置了 `STEALTH_PREFIX`，它位于 `/issue/` 之前。
- 平台只负责生成链接，能否看到工作项由 Specivo 决定。没有权限的人会看到登录页或“未找到”，和其他
  链接一样。

不要只用数字链接到 `/issue/12/`：单独的数字是 Specivo 的内部工作项 id，不是 `ACME-12` 里的编号，
会打开另一个工作项。

## 配置步骤 {#set-it-up}

你需要仓库的管理员权限。打开仓库的 **Settings**，然后：

- **Gitea**：在主设置页的 **Advanced Settings** 区域，找到 **Issues**。
- **Forgejo**：在设置侧边栏点击 **Units**，再找到 **Issues** 区域。

如何启用外部工单系统取决于版本：

- **Forgejo，以及 1.27 及更早的 Gitea**：勾选 **Enable Repository Issue Tracker**（启用仓库工单系统），
  然后选择 **Use External Issue Tracker**（使用外部工单系统），而不是 **Use Built-In Issue Tracker**
  （使用内置工单系统）。
- **Gitea 28 及更新版本**：两个选项是独立的复选框。勾选 **Use External Issue Tracker**，并取消勾选
  **Use Built-In Issue Tracker**。只要内置工单系统仍然开启，**Issues** 标签页就继续显示平台自带的工单，
  `#12` 也继续链接到它们，因此下文的数字格式无法指向 Specivo；字母数字和正则表达式引用则不受影响。

然后填写：

| 设置项 | 值 |
|---|---|
| **External Issue Tracker URL** | **Issues** 标签页要打开的地址，即项目的工作项列表：`https://specivo.example.com/projects/ACME/issues/`。 |
| **External Issue Tracker URL Format** | 工作项地址，带一个占位符。平台提供 `{user}`（仓库所有者）、`{repo}`（仓库名）和 `{index}`（工作项引用）。Specivo 只需要 `{index}`，见下文各格式。 |
| **External Issue Tracker Number Format** | **Alphanumeric**（字母数字）、**Numeric**（数字）或 **Regular Expression**（正则表达式），见下文。 |

保存设置。平台是在显示文本时才把引用转成链接，所以修改之前写的提交信息和评论同样会带上链接。

### 字母数字格式：`ACME-12` 这样的引用 {#alphanumeric-references-like-acme-12}

如果团队习惯写完整的 Specivo 编号，就选这种格式；
[仓库集成](repositories.md#how-commits-and-pull-requests-name-an-issue)读取的也是这种写法。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Alphanumeric |

`{index}` 就是整个引用（`ACME-12`），因此一个仓库可以链接到多个项目的工作项。

平台只在满足以下条件时才识别字母数字引用：项目标识为 **1 到 10 个大写字母**，数字不以 `0` 开头，
引用全部大写（`acme-12` 不会被链接），并且独立成词：后面是空格、行尾、`.`、`,`、`:`、右括号或引号，
而不是 `;`、`!` 等其他标点。Specivo 的项目标识还可以包含数字，长度也可以更长（`WEB2`、
`INFRASTRUCTURE`）；带这类标识的引用在此格式下不会被链接，请改用 **Regular Expression**。

### 数字格式：固定项目标识，引用写成 `#12` {#numeric-references-like-12-with-a-fixed-key}

如果仓库只对应一个 Specivo 项目，而你更喜欢写 `#12`，就选这种格式。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/ACME-{index}/` |
| Number Format | Numeric |

`{index}` 只是数字，所以 URL Format 里必须带上项目标识和连字符。这样每个 `#12` 都会打开 ACME-12；
其他项目的工作项无法链接。

平台自己的合并请求也用 `#` 编号。启用数字格式的外部工单系统后，`#12` 一律表示 Specivo 工作项；
要引用平台的第 12 号合并请求，写 `!12`。两者容易混淆，所以能用字母数字格式时优先使用它。

### 正则表达式：其他任意标识 {#regular-expression-any-other-key}

当项目标识包含数字或超过十个字母，或者只想匹配特定标识时，选这种格式。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Regular Expression |
| Regular Expression Pattern | `\b(WEB2-[0-9]+)\b` |

平台会用 **第一个圆括号分组** 捕获到的内容替换 `{index}`，这里是 `WEB2-12`。要接受多个标识，把它们
列出来：`\b((?:WEB2|INFRASTRUCTURE)-[0-9]+)\b`。如果只想捕获数字，像数字格式那样把标识写进
URL Format：`https://specivo.example.com/issue/WEB2-{index}/`，配合模式 `\bWEB2-([0-9]+)\b`。
