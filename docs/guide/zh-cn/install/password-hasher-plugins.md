---
description: 编写一个小巧的密码哈希插件，让 Specivo 能校验从其他系统导入的密码，再通过 LEGACY_PASSWORD_ALGORITHMS 启用它。
---

# 编写密码哈希插件 {#writing-a-password-hasher-plugin}

把用户从其他系统迁移到 Specivo 时，他们可以继续使用原来的密码。Specivo 会在用户首次登录时校验旧哈希，然后换成自己的 bcrypt 哈希。没有人需要重置密码。

内置支持三种来源格式：

| 算法 ID | 来源 | 读取的字段 |
|---|---|---|
| `redmine_sha1` | Redmine 本地账户 | `salt`、`hashed_password` |
| `django_pbkdf2_sha256` | Django 默认的 `pbkdf2_sha256$...` 哈希 | `password` |
| `bcrypt_foreign` | 来自其他应用的 bcrypt（`$2y$`、`$2a$`、`$2b$`） | `hash` |

其他格式需要自己写一个哈希器，大多数只要十五行左右。

## 哈希器的工作方式 {#how-a-hasher-works}

哈希器做两件事：

- **导入时**，把源系统的原始凭据字段转换成 Specivo 要存储的值。它从不接触明文密码。
- **登录时**，用存储的值校验用户输入的密码。

弱哈希格式（无盐或计算很快的哈希，例如 MD5 和 SHA1）绝不能原样存进数据库。`WrappedLegacyHasher` 基类负责解决这个问题：导入时它用 bcrypt 把源摘要包裹一层，存储的值形如 `<algorithm>$<parameters>$<bcrypt>`。这样即使数据库被复制走，里面也没有比 Specivo 原生哈希更弱的东西；而且无论账户历史如何，每次登录都只消耗一次 bcrypt 计算，响应时间不会暴露哪些账户还在用旧哈希。

你需要实现两个方法：

- `source_params(fields)` 从源数据行中取出该方案所需的参数（比如盐值）和存储的摘要；
- `inner_digest(password, params)` 根据密码重新计算这个摘要。

其余工作由基类完成：bcrypt 包裹、常量时间比较、在不抛异常的情况下拒绝格式错误的值，以及把每个这类哈希标记为下次登录时升级。

## 示例 {#example}

假设旧系统把 `md5(salt + password)` 以小写十六进制存在 `password_digest` 列中，旁边还有一个 `salt` 列。完整的插件如下：

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
    name, version = "acme-hashers", "1.0"

    def get_password_hashers(self):
        return [AcmeTrackerMd5()]
```

保存为 `acme_hashers.py`。

源方案的细节必须完全准确：中间摘要是十六进制还是原始字节、大写还是小写、文本编码是什么、盐和密码的先后顺序。下面的 `test` 命令可以让你在上线前用一条真实数据验证这些细节。

算法 ID 长度必须为 2 到 32 个字符：以小写字母开头，后面是小写字母、数字或下划线。内置的 ID 和 `bcrypt` 是保留的；两个哈希器使用相同 ID 会导致 Specivo 无法启动。

## 安装 {#install-it}

这个模块必须能被 Web 应用、Celery worker 和导入命令导入。使用 Docker 时，把它挂载到每个服务的 `/app` 目录，例如在 `docker-compose.override.yml` 中：

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

然后在 `.env` 中注册，两种方式任选其一：

```bash
# As a plugin:
INSTALLED_PLUGINS=["acme_hashers.Plugin"]

# Or just the hasher class, without the Plugin wrapper:
PASSWORD_HASHERS=["acme_hashers.AcmeTrackerMd5"]
```

注册哈希器并不等于启用它。

要把账户从源系统迁移过来，先让迁移工具把原始的 `salt` 和 `password_digest` 写入导入包中每个用户的 `credential` 字段，剩下的交给哈希器处理。

## 用已知数据验证 {#check-it-against-a-known-row}

从旧系统中挑一个你知道密码的账户（最好是测试账户），运行：

```bash
docker compose exec api python -m specivo.cli.admin password-hashers test acme_md5 \
  --field salt=NaCl \
  --field password_digest=ce48c13da732f51a1bf9c4fac876c638 \
  --password 'open sesame'
```

它会输出 `MATCH` 或 `NO MATCH`，不会改动任何账户。省略 `--password` 则会提示你输入密码，这样密码不会留在 shell 历史记录里。

## 启用 {#enable-it}

只有列在 `LEGACY_PASSWORD_ALGORITHMS` 中的算法才会参与登录校验，内置哈希器也遵守同样的规则。

```bash
# .env
LEGACY_PASSWORD_ALGORITHMS=["acme_md5"]
```

重启整个服务栈，然后检查结果：

```bash
docker compose exec api python -m specivo.cli.admin password-hashers
```

这会列出所有已注册的算法、各自的来源（核心、插件或配置项）、是否已启用，以及有多少账户持有该算法的哈希。

算法未知或未启用的哈希一律不匹配。用户会看到常规的“登录名或密码不正确”提示，可以通过找回密码功能重设。

## 之后 {#afterwards}

每个账户在首次成功登录时迁移到原生 bcrypt 哈希。每次升级都会以 `password_hash_upgraded` 记入安全审计日志，其中包含旧算法 ID，但绝不包含哈希本身。查看哪些人还没登录过：

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords
```

旧哈希的强度取决于背后的密码，所以不要让它们一直留着。你可以设置一个截止时间，到期后所有旧哈希都不再通过校验，剩下的人通过找回密码重设：

```bash
LEGACY_PASSWORD_HASHES_EXPIRE_AT=2027-03-31T00:00:00Z
```

也可以立即让它们全部失效：

```bash
docker compose exec api python -m specivo.cli.admin legacy-passwords expire --yes
```

当没有账户再持有旧格式的哈希时，把该算法从 `LEGACY_PASSWORD_ALGORITHMS` 中移除。
