from __future__ import annotations

import json
import os
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "printroom", "static")

# 基础校验：HTML 里引用的静态资源都存在，且关键元素 id 与 JS 一致。
EXPECTED_IDS = {
    "banner", "doc-form", "doc-title", "doc-editor", "documents",
    "printers", "jobs", "records", "refresh", "server-time",
}


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.assets: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.add(values["id"])
        for key in ("src", "href"):
            if key in values:
                self.assets.append(values[key])


def main() -> int:
    failures: list[str] = []
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    parser = IdCollector()
    parser.feed(html)

    missing_ids = EXPECTED_IDS - parser.ids
    if missing_ids:
        failures.append(f"index.html 缺少元素 id: {sorted(missing_ids)}")

    for asset in parser.assets:
        if asset.startswith("/static/"):
            path = os.path.join(STATIC, asset[len("/static/"):])
            if not os.path.isfile(path):
                failures.append(f"静态资源不存在: {asset}")

    # app.js 里引用的 getElementById 必须能在 HTML 中找到，或由脚本动态生成。
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    dynamic = {"page-input"}  # 由 renderDocuments 动态插入
    import re
    referenced = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    unknown = referenced - parser.ids - dynamic
    if unknown:
        failures.append(f"app.js 引用了 HTML 中不存在的 id: {sorted(unknown)}")

    if failures:
        for failure in failures:
            print("FAIL:", failure, file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "ids": sorted(parser.ids), "assets": parser.assets}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
