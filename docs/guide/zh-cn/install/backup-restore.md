---
description: "本文介绍如何备份与恢复 Specivo：导出 PostgreSQL 数据库转储、归档数据目录、妥善保管 SECRET_KEY、按正确步骤恢复两者，并设置定期自动备份。"
---

# 备份与恢复 {#backup-restore}

一份完整的 Specivo 备份包含两部分：**数据库转储**（所有工作项、维基页面、评论和历史记录）和 **数据目录**（附件以及内置数据库的文件）。两者要一起备份，才能保持一致。

## 备份 {#back-up}

### 1. 转储数据库 {#1-dump-the-database}

```bash
docker compose exec db pg_dump -U specivo specivo > backup.sql
```

这会把整个数据库以纯 SQL 格式转储到宿主机上的 `backup.sql`。如果你在 `.env` 中修改过
`POSTGRES_USER` 或 `POSTGRES_DB`，请用修改后的值替换 `specivo`。

### 2. 备份数据目录 {#2-back-up-the-data-directory}

`SPECIVO_DATA_DIR`（默认 `./specivo-data`）存放附件、日志、主题以及内置 Postgres 的文件。
打包归档：

```bash
tar czf specivo-data-backup.tar.gz specivo-data
```

把 `backup.sql` 和 `specivo-data-backup.tar.gz` 放在一起，保存到宿主机之外，并在文件名里加上
时间戳，便于区分不同的快照。

### 3. 把 `SECRET_KEY` 和备份放在一起 {#3-keep-secret_key-with-the-backup}

`SECRET_KEY` 保存在 `.env.local` 中，请把该文件的副本与转储一并保管，存放位置的安全级别要和
转储本身相同。Specivo 用由 `SECRET_KEY` 派生的密钥加密数据库里的部分机密信息：
[单点登录（SSO）](single-sign-on/index.md#the-client-secret) 提供商的客户端密钥，以及单点登录
会话上保存的 ID 令牌。已登录的会话和令牌也依赖它。

!!! tip "停止服务以获得完全一致的快照"
    要获得尽可能一致的备份，数据库转储和数据目录归档应在相近的时间完成。如果需要严格的
    时间点快照，先停止整个服务栈（`docker compose stop`），再归档，然后重新启动。

## 恢复 {#restore}

恢复就是把转储导入数据库，并把数据目录放回原处。

### 1. 恢复数据目录 {#1-restore-the-data-directory}

在服务栈停止的状态下，用归档替换数据目录：

```bash
docker compose down
tar xzf specivo-data-backup.tar.gz      # restores ./specivo-data
```

### 2. 恢复数据库 {#2-restore-the-database}

启动数据库服务，然后把转储导入其中：

```bash
docker compose up -d db
docker compose exec -T db psql -U specivo specivo < backup.sql
```

必须加 `-T` 参数，转储才能通过 stdin 流式导入。导入完成后，启动服务栈的其余部分：

```bash
docker compose up -d
```

登录后确认工作项、维基页面和附件都已齐全。

!!! warning "恢复到相同版本"
    转储只能恢复到它所来自的 **同一 Specivo 版本**（相同的 `SPECIVO_VERSION`），数据库结构
    必须与正在运行的镜像匹配。如果要把数据迁到更新的版本，先在原版本上恢复并确认正常，
    再[升级](upgrading.md)——升级会按正确顺序执行数据库迁移。

## 哪些内容依赖 `SECRET_KEY` {#what-depends-on-secret_key}

数据库转储包含单点登录相关的表（`sso_providers`、`user_identities` 和 `sso_group_mappings`）
以及会话表，因此恢复后，提供商列表、已关联的账户和用户组映射都会一并回来。

如果恢复到的实例使用了不同的 `SECRET_KEY`，数据库里保存的客户端密钥将无法读取：提供商列表
会显示 **无法读取，请重新输入**，你需要打开每个提供商重新输入其密钥。其他数据不会丢失。
提供商从 `SPECIVO_SSO_SECRET_*` 环境变量读取的密钥不存在数据库里，不需要重新输入，但新实例
上必须设置好该变量。之前已登录的用户可能需要重新登录。

## 设置定期备份 {#schedule-regular-backups}

按计划执行数据库转储（例如每晚一次的 cron 任务），并在宿主机之外保留若干天的历史备份。
从未验证过的备份算不上备份——请定期在一台临时实例上演练恢复流程，确保在真正需要时它能
正常工作。
