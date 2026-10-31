---
description: 在 Gitea 和 Forgejo 中把 Specivo 设为仓库的外部工作项追踪器，让提交和评论里的工作项引用直接链接到 Specivo，支持字母数字、纯数字和正则表达式三种编号格式。
---

# 从 Gitea 和 Forgejo 链接到工作项 {#issue-links-from-gitea-and-forgejo}

Gitea 和 Forgejo 可以把另一个系统设为仓库的 **external issue tracker**（外部工作项追踪器）。把它指向 Specivo 后，代码托管平台的提交信息、合并请求（Pull Request）和评论里的每一个工作项（Issue）引用（`ACME-12` 或 `#12`）都会变成指向 Specivo 中对应工作项的链接，仓库的 **Issues** 标签页也可以直接打开你的 Specivo 项目，而不是平台自带的工作项列表。Gitea 和 Forgejo 的相关设置相同，只是位置略有差别；下文使用的是 Gitea 的英文标签（Forgejo 采用句首大写写法，例如 "Use external issue tracker"）。

它与[仓库集成](repositories.md)的方向正好相反：仓库集成是把平台的提交和合并请求发送*到* Specivo。两者可以搭配使用。

## Specivo 的工作项地址 {#where-specivos-issues-live}

每个工作项在你的 Specivo 地址下都有一个 `/issue/{key}/` 页面：

```text
https://specivo.example.com/issue/ACME-12/
```

- 地址中的编号不区分大小写：`/issue/acme-12/` 打开的是同一个工作项。
- 工作项被移到其他项目后，仍可通过旧编号打开。
- 管理员 **重命名项目标识** 后，工作项页面只响应新编号。遇到这种情况，请更新下文中平台侧的设置。
- 如果你的实例使用了 `STEALTH_PREFIX`，它位于 `/issue/` 之前。
- 平台只负责生成链接，能否查看工作项由 Specivo 决定。没有权限的人会看到登录页或“未找到”，与其他链接一样。

不要用单独的数字链接到 `/issue/12/`：单独的数字是 Specivo 的内部工作项 id，不是 `ACME-12` 里的数字，会打开另一个工作项。

## 设置步骤 {#set-it-up}

你需要仓库的管理员权限。打开仓库的 **Settings**，然后：

- **Gitea：** 在主设置页的 **Advanced Settings** 区域，找到 **Issues**。
- **Forgejo：** 在设置侧边栏中点击 **Units**，再找到 **Issues** 区域。

启用外部追踪器的方式取决于版本：

- **Forgejo，以及 Gitea 1.27 及更早版本：** 勾选 **Enable Repository Issue Tracker**，然后选择 **Use External Issue Tracker**，而不是 **Use Built-In Issue Tracker**。
- **Gitea 28 及更高版本：** 两个追踪器是各自独立的复选框。勾选 **Use External Issue Tracker**，并取消勾选 **Use Built-In Issue Tracker**。只要内置追踪器仍处于开启状态，**Issues** 标签页就会继续显示平台自己的工作项，`#12` 也会继续链接到它们，因此下文的纯数字格式不会指向 Specivo；字母数字和正则表达式引用则不受影响。

然后填写：

| 设置项 | 值 |
|---|---|
| **External Issue Tracker URL** | **Issues** 标签页要跳转的地址，即项目的工作项列表：`https://specivo.example.com/projects/ACME/issues/`。 |
| **External Issue Tracker URL Format** | 带占位符的工作项地址。平台提供 `{user}`（仓库所有者）、`{repo}`（仓库名）和 `{index}`（工作项引用）。Specivo 只需要 `{index}`，见下文各格式。 |
| **External Issue Tracker Number Format** | **Alphanumeric**（字母数字）、**Numeric**（纯数字）或 **Regular Expression**（正则表达式），见下文。 |

保存设置。平台是在显示文本时把引用转换为链接的，所以修改之前写下的提交信息和评论同样会生成链接。

### 字母数字：形如 `ACME-12` 的引用 {#alphanumeric-references-like-acme-12}

如果团队习惯写完整的 Specivo 工作项编号，就选这种格式；[仓库集成](repositories.md#how-commits-and-pull-requests-name-an-issue)读取的也是这种写法。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Alphanumeric |

`{index}` 是完整的引用，即 `ACME-12`，因此一个仓库可以链接到多个项目中的工作项。

平台只在满足以下条件时才识别字母数字引用：项目标识为 **1 到 10 个大写字母**，数字不以 `0` 开头，整体为大写（`acme-12` 不会被链接），并且独立成词：后面是空格、行尾、`.`、`,`、`:`、右括号或引号，而不能是 `;`、`!` 等其他标点。Specivo 的项目标识还可以包含数字，也可以更长（`WEB2`、`INFRASTRUCTURE`）；含这类标识的引用在此格式下不会被链接，请改用 **Regular Expression**。

### 纯数字：形如 `#12` 的引用，项目标识固定 {#numeric-references-like-12-with-a-fixed-key}

如果仓库只对应一个 Specivo 项目，而且你更想写 `#12`，就选这种格式。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/ACME-{index}/` |
| Number Format | Numeric |

`{index}` 只包含数字，所以 URL Format 里必须带上项目标识和连字符。此后每个 `#12` 都会打开 ACME-12；无法链接到其他项目的工作项。

平台自己的合并请求也用 `#` 编号。设为纯数字外部追踪器后，`#12` 始终表示 Specivo 的工作项；要引用平台的第 12 号合并请求，请写 `!12`。两者容易混淆，所以能用字母数字格式时，优先使用字母数字格式。

### 正则表达式：其他任意项目标识 {#regular-expression-any-other-key}

项目标识含数字或超过十个字母，或者只想匹配特定标识时，选这种格式。

| 设置项 | 值 |
|---|---|
| URL Format | `https://specivo.example.com/issue/{index}/` |
| Number Format | Regular Expression |
| Regular Expression Pattern | `\b(WEB2-[0-9]+)\b` |

平台会用 **第一个括号分组** 捕获到的内容替换 `{index}`，这里是 `WEB2-12`。要接受多个标识，把它们列出来：`\b((?:WEB2|INFRASTRUCTURE)-[0-9]+)\b`。如果只想捕获数字，就像纯数字格式那样把标识写进 URL Format：`https://specivo.example.com/issue/WEB2-{index}/`，配合模式 `\bWEB2-([0-9]+)\b`。
