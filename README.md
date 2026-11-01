# chatgpt-local-backup

独立的 ChatGPT 本地归档。**不依赖 Octop** 即可 ingest、validate、search、runs。`optional/octop-adapter/` 只是可选的 Octop 薄适配器，不用它也能完整使用。

Standalone local vault for ChatGPT exports. Octop is not required.

开源采集器的导出是主输入。官方 ChatGPT ZIP 只用于对照校验和补档，不是唯一事实来源。图片、文档、代码按原文件留在每次采集的目录里。SQLite 只是可重建的索引。

## 安装 / Install

需要 Python 3.11+。只使用标准库，运行时没有第三方依赖。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

不安装包时也可以：

```bash
PYTHONPATH=. python -m chatgpt_archive --help
```

## 运行 / Run

默认归档根目录是 `~/.chatgpt-local-backup`。设置 `CHATGPT_ARCHIVE_ROOT` 或传入 `--archive-root` 可改到别的磁盘。路径不再位于 `~/.octop/`。

```bash
chatgpt-archive ingest ./path/to/collector-export --account work --source-kind open_source
chatgpt-archive validate ./path/to/collector-export ./path/to/chatgpt-export.zip --report validation.json
chatgpt-archive search "关键词"
chatgpt-archive runs
chatgpt-archive runs --account work
```

`source_kind` 为 `open_source`（默认，主采集）、`official`（把官方 ZIP 当作一次补档入库）或 `manual`。同一账号、同一来源类型、同一份清单再次 ingest 会返回已有 run，不复制行。

`validate` 只比较两边，不覆盖、不删除。分类：

| 结果 | 含义 |
| --- | --- |
| `verified` | 对话 ID、标题、消息条数和消息内容哈希一致 |
| `open_source_only` | 只出现在主采集 |
| `official_only` | 只出现在官方导出 |
| `content_mismatch` | ID 相同，标题或消息签名不同 |
| `not_comparable` | 附件或富引用两边表示方式不同，不能直接当差异删除 |

差异是待复核的证据。CLI 只打印 JSON，不上传路径。

搜索使用 SQLite FTS5（`unicode61`）前缀匹配。词之间有空格或标点时可以单独搜；一段没有分隔的中文是一个词元，用完整片段来搜。

## 数据布局 / Layout

```text
~/.chatgpt-local-backup/
  index.sqlite3
  raw/
    <account_id>/
      <account>-<source_kind>-<manifest_sha16>/
        manifest.json
        <原样复制的导出文件，含图片、文档、代码>
```

- `raw/<account>/<run_id>/` 是事实来源，含 SHA-256 清单（路径、大小、MIME 猜测）。
- `index.sqlite3` 保存对话元数据和 FTS5 索引，删掉后对 raw run 再 ingest 即可重建。
- 账号 ID 里非字母数字的字符会换成 `_`，并截断到 80 字符。

## 安全 / Safety

- 不把 cookie、token、凭证、原始导出、SQLite 索引提交进 git。
- 不新增网页爬虫或 ChatGPT 登录采集。输入是已经落在本地的目录、JSON 或 ZIP。
- ZIP 解压到临时目录，拒绝路径穿越；解压结果不作为第二份事实来源。
- 官方 ZIP 不能替换开源采集。先保留 raw run 和清单，再看校验报告。

## 可选 Octop 适配器

归档核心在 `chatgpt_archive/`。只有要把同一套能力挂进 Octop agent 时，才安装 `optional/octop-adapter/`。说明见该目录的 README。不加载它，上面的 CLI 命令仍然完整可用。
