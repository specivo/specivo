---
description: 编写一个简短的密码哈希插件，让 Specivo 能验证从其他系统导入的旧密码，再通过 LEGACY_PASSWORD_ALGORITHMS 启用它，用户无需重置密码。
---

# 编写密码哈希插件 {#writing-a-password-hasher-plugin}

把用户从其他系统迁移到 Specivo 时，他们可以继续使用原来的密码。用户首次登录时，Specivo 会校验旧哈希，然后换成自己的 bcrypt 哈希。没有人需要重置密码。

内置支持三种来源格式：

| 算法 id | 来源 | 读取的字段 |
|---|---|---|
| `redmine_sha1` | Redmine 本地账户 | `salt`、`hashed_password` |
| `django_pbkdf2_sha256` | Django 默认的 `pbkdf2_sha256$...` 哈希 | `password` |
| `bcrypt_foreign` | 来自其他应用的 bcrypt（`$2y$`、`$2a$`、`$2b$`） | `hash` |

其他格式需要自己写一个哈希器，大多数只有十五行左右。

## 哈希器的工作原理 {#how-a-hasher-works}

哈希器做两件事：

- **导入时**，把来源系统的原始凭据字段转换成 Specivo 存储的值。它从不接触明文密码。
- **登录时**，拿密码与存储的值比对。

弱哈希格式（无盐或计算很快的哈希，例如 MD5 和 SHA1）绝不能原样存入数据库。`WrappedLegacyHasher` 基类替你处理了这一点：导入时把来源摘要再用 bcrypt 包一层，存储的值形如 `<algorithm>$<parameters>$<bcrypt>`。这样即使数据库被复制走，里面也没有任何比 Specivo 原生哈希更弱的东西；并且无论账户历史如何，每次登录都只花一次 bcrypt 的开销，响应时间不会暴露哪些账户还在用旧哈希。

你只需实现两个方法：

- `source_params(fields)` 从来源记录中取出该算法需要的参数（比如盐）和存储的摘要；
- `inner_digest(password, params)` 根据密码重新计算该摘要。

其余的事都由基类完成：bcrypt 包装、常量时间比较、遇到格式错误的值时不抛异常而直接拒绝，以及把每个这样的哈希标记为下次登录时升级。

## 示例 {#example}

假设旧的项目管理系统把 `md5(salt + password)` 以小写十六进制存在 `password_digest` 列里，旁边还有一列 `salt`。整个插件如下：

```python
import hashlib

from specivo.core.plugin import BasePluginConfig
from specivo.services.password_hashers import WrappedLegacyHasher


class AcmeTrackerMd5(WrappedLegacyHasher):
    algorithm = "acme_md5"

    def source_params(self, fields):  # import side: which source columns matter
        return {"salt": fields["salt"]}, fields["password_digest"]

    def inner_digest(self, password, params):  # login side: recompute the source's digest
        return hashlib.md5((params["salt"] + password).encode()).hexdigest()


class Plugin(BasePluginConfig):
    name, tier, version = "acme-hashers", "core", "1.0"

    def get_password_hashers(self):
        return [AcmeTrackerMd5()]
```

保存为 `acme_hashers.py`。

来源算法的细节必须完全一致：中间摘要是十六进制还是原始字节、大写还是小写、文本编码、盐和密码的先后顺序。下文的 `test` 命令可以让你在正式启用之前，用一条真实记录核对这些细节。

算法 id 长度必须在 2 到 32 个字符之间：以小写字母开头，后接小写字母、数字或下划线。内置的 id 和 `bcrypt` 是保留名；两个哈希器使用相同的 id 会导致 Specivo 无法启动。

## 安装 {#install-it}

这个模块必须能被 Web 应用、Celery worker 和导入命令同时导入。使用 Docker 时，把它挂载到每个服务的 `/app` 目录下，例如在 `docker-compose.override.yml` 中：

```yaml
services:
  api:
    volumes:
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
  celery-worker:
    volumes:
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
  celery-embeddings:
    volumes:
      - ./plugins/acme_hashers.py:/app/acme_hashers.py:ro
```

然后在 `.env` 中注册，两种写法任选其一：

```bash
# As a plugin:
INSTALLED_PLUGINS=["acme_hashers.Plugin"]

# Or just the hasher class, without the Plugin wrapper:
PASSWORD_HASHERS=["acme_hashers.AcmeTrackerMd5"]
```

注册哈希器并不会启用它。

要把账户从来源系统迁过来，让导入工具把原始的 `salt` 和 `password_digest` 写入导入包中每个用户的 `credential` 字段，剩下的交给哈希器。

## 用已知记录核对 {#check-it-against-a-known-row}

从旧系统里挑一个你知道密码的账户（最好是测试账户），运行：

```bash
docker compose exec api python -m specivo.cli.admin password-hashers test acme_md5 \
  --field salt=NaCl \
  --field password_digest=ce48c13da732f51a1bf9c4fac876c638 \
  --password 'open sesame'
```

它会输出 `MATCH` 或 `NO MATCH`，不会改动任何账户。省略 `--password` 则会提示你输入密码，这样密码不会留在 shell 历史里。

## 启用 {#enable-it}

只有列在 `LEGACY_PASSWORD_ALGORITHMS` 中的算法才会用于校验登录，内置哈希器也遵循同一规则。

```bash
# .env
LEGACY_PASSWORD_ALGORITHMS=["acme_md5"]
```

重启整套服务，然后检查结果：

```bash
docker compose exec api python -m specivo.cli.admin password-hashers
```

这条命令会列出每个已注册的算法、它的来源（核心、插件还是配置项）、是否已启用，以及有多少账户持有该算法的哈希。

算法未知或未启用的哈希一律视为不匹配。用户会看到常规的“登录名或密码不正确”提示，可以通过找回密码来处理。

## 后续 {#afterwards}

每个账户在首次成功登录时转为 Specivo 原生的 bcrypt 哈希。每次升级都会以 `password_hash_upgraded` 记入安全审计日志，日志中包含旧算法 id，但绝不包含哈希本身。查看哪些人还没登录过：

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords
```

旧哈希的强度取决于背后的密码，所以不要让它们一直留着。你可以设定一个截止时间，之后任何旧哈希都不再通过校验，剩下的用户改用找回密码：

```bash
LEGACY_PASSWORD_HASHES_EXPIRE_AT=2027-03-31T00:00:00Z
```

也可以立即让它们全部失效：

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

等到没有账户再持有旧格式的哈希，就把该算法从 `LEGACY_PASSWORD_ALGORITHMS` 中移除。
