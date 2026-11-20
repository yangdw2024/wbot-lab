# W-BOT Lab

A Windows local-first prototype for analysing a WeChat group: import the locally available history, inspect messages through a local interface or MCP, and generate daily, weekly, monthly and yearly evidence-linked statistical reports.

> **Early prototype.** Reports use fixed keyword rules and counts; they are not AI-generated semantic summaries. Image, video, voice and other media contents are not analysed. A local snapshot does not prove that the group's entire history has been collected.

## What it does

- Reads one explicitly configured group from locally stored WeChat 4.x database snapshots. The read-only adapter was exercised against one Windows WeChat 4.2.0.13 installation; compatibility with other versions is unverified.
- Provides a localhost dashboard, search, CSV/JSON/TXT import, SQLite online backup and report export.
- Groups records into day, Monday–Sunday week, month and year, using Asia/Singapore time. Reports are calculated from recorded messages and include message IDs as evidence.
- Optionally refreshes reports once per day while the application runs. Automatic updates are off on first launch unless `--auto` is supplied or the user enables them in the interface.
- Offers eight stdio MCP tools for local group metadata, search, coverage, date-range pagination and calendar statistics. MCP queries the analysis database; it does not itself start a WeChat sync.

## Requirements

- Windows, Node.js 18 or newer and a separately installed Python 3.10 or newer.
- To read WeChat history, use an existing Python environment with the packages listed in `requirements-wechat.txt`. The npm package does **not** install Python or Python packages for you.
- A logged-in WeChat desktop client and the absolute path containing its `xwechat_files` account directories. The reader requires one unambiguous account with message data.
- The application needs a writable, explicitly chosen data directory **outside** the installed package directory. Database, reports, backups, temporary copies and browser profile go there.

Optional screen OCR requires additional local PaddleOCR dependencies and model files. They are not included in the package; live window capture has not been validated successfully. The database sync and OCR are separate features.

## Install

Example using an E-drive prefix and cache on Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force E:\wbot-temp | Out-Null
$env:TMP = 'E:\wbot-temp'
$env:TEMP = $env:TMP
npm install --prefix E:\wbot-tools --cache E:\wbot-npm-cache @yangdw2024/wbot-lab
```

Create or select a Python environment outside the system drive and install the tested dependencies there if they are not already present:

```powershell
E:\Python\python.exe -m venv E:\wbot-venv
E:\wbot-venv\Scripts\python.exe -m pip install -r E:\wbot-tools\node_modules\@yangdw2024\wbot-lab\requirements-wechat.txt
```

The example paths are placeholders. Select paths that exist on your computer.

## Start the dashboard

```powershell
E:\wbot-tools\node_modules\.bin\wbot-lab.cmd `
  --python E:\wbot-venv\Scripts\python.exe `
  --data-dir E:\wbot-data `
  --group 'Exact group name' `
  --wechat-dir E:\WeChatData\xwechat_files `
  --auto --browser
```

Open `http://127.0.0.1:8765` if the browser does not open. Use **同步本机群历史** to sync manually. Only the exact group specified on launch can be synced. Do not pass `--auto` to keep automatic sync disabled by default; it can later be enabled or paused in the dashboard. The daily schedule is 00:10 Asia/Singapore, with same-day catch-up on a later start and a 30-minute retry after an error. Keep both the app and WeChat running; there is no OS startup service. Saved automatic settings are bound to the group: use a separate data directory for a different group.

The dashboard can also be started without `--group` and `--wechat-dir` to work with files previously imported by the user; history sync and automatic updates then remain unavailable.

## MCP

The stdio entry point can be configured in an MCP client using the same data directory as the dashboard. Example:

```json
{
  "mcpServers": {
    "wbot-lab": {
      "command": "E:\\nodejs\\node.exe",
      "args": ["E:\\wbot-tools\\node_modules\\@yangdw2024\\wbot-lab\\bin\\wbot.js", "mcp", "--python", "E:\\wbot-venv\\Scripts\\python.exe", "--data-dir", "E:\\wbot-data"]
    }
  }
}
```

Tools: `list_groups`, `get_group_stats`, `get_daily_report`, `get_member_profile`, `search_messages`, `get_data_coverage`, `get_period_report`, and `get_messages_by_range`. For subsequent pages pass both `next_cursor` and `snapshot_max_id` from the first page. The MCP process uses stdout for protocol frames only.

**Privacy:** a client can retrieve message contents and preserved source text. A cloud-connected client may transmit retrieved data to its model provider. Use a trusted local client when data must remain on the machine.

## Data handling and limits

The history worker reads database keys from the owner's logged-in WeChat process in memory, snapshots the contact/message databases, checks page authentication and applicable WAL boundaries, and decrypts temporary copies under the chosen data directory. Temporary decrypted databases may contain other conversations even though only the configured group is imported. Keys are not intentionally written to a key cache. Normal completion and handled errors remove the temporary session; a crash or forced termination may leave a session under `runtime/wechat_tmp`. If one remains, the next sync stops and asks for inspection.

Only the data already present on the local machine can be collected. Deleted, unsynchronised, incompatible or malformed records may be absent. Unknown senders and undated records are disclosed; undated records are excluded from calendar counts. Non-text records may contain metadata only. Matching an example ID to a message does not establish that a statement in the group is true. Imported records without a stable source ID use content-based deduplication, which can merge identical real messages.

The web service listens on `127.0.0.1` and uses request-origin and session-token checks; it is not a strong security boundary against other processes running as the same user. Secure the selected data directory and backups. No chat content, database, backup, key, OCR model or screenshot is included in the npm package or repository.

## Development and validation

From a checkout on Windows, use a disposable data directory and install `requirements-dev.txt` into a separate Python environment. Run `python -B -m unittest discover -s tests -p test_workflow.py -v` and `node tests/ui_smoke.js`. The tests use synthetic data. A prior machine-specific integration test verified one group's local import and deduplication; this does not validate other WeChat versions or complete history.

## Licensing and attribution

The original project has no confirmed open-source license and the package is marked `UNLICENSED`; public availability does not grant a reuse license for the original project code. The separately included upstream `vendor/wechat_db_source/db.py` is distributed with its Apache-2.0 license and attribution in that directory. No third-party model weights are bundled.
