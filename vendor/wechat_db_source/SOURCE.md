# Third-party database module

- Source: https://github.com/fanyuantaier/wechatauto-replica
- Revision: `9a970975c47d1bdd5a5d869f016cc881e1f48f51`
- License: Apache-2.0; see the accompanying LICENSE file.
- `db.py` is copied unchanged and SHA-256 checked by `wechat_reader_worker.py` before loading. The wrapper disables the vendor's key-cache persistence and cross-account switching, restricts the selected database snapshot, and uses a local worker with Python socket auditing.
- The module is supplied for review as source. Do not call it directly expecting the wrapper's restrictions; it contains other upstream functionality.
