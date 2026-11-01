# 可选：Octop 适配器

这个目录是 **Octop 专用的薄适配器**，不是归档本身。

不安装 Octop、不加载本目录，也可以用 `chatgpt-archive` 完成 ingest、validate、search、runs。默认库在 `~/.chatgpt-local-backup`（或 `$CHATGPT_ARCHIVE_ROOT`），不再使用 `~/.octop/`。

只有在 Octop 里把同一套核心暴露成工具时才需要它：

```bash
pip install -e .
octop plugin install ./optional/octop-adapter --force
```

工具名：`archive_ingest`、`archive_validate`、`archive_search`、`archive_runs`。

This directory is an optional Octop-only adapter. The archive CLI does not import it.
