"""引数をそのまま JSON で出力するだけのプローブ (C8.6)。

スケジューラ計画の ``/TR`` が、実際の PowerShell を経由しても **1 つの引数**
として届くことを検証するために使う。schtasks の代わりにこれを呼ぶので、
タスクは一切登録されない。
"""

from __future__ import annotations

import json
import sys

if __name__ == "__main__":
    print(json.dumps(sys.argv[1:], ensure_ascii=False))
