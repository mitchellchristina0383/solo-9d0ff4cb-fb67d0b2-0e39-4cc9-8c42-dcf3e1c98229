"""命令行演示：跑通“半张卡纸”和“停稳前出纸”两条验收路径。

用法：
    python3 scripts/demo.py [数据库路径]

脚本只通过 HTTP 接口操作，结束后打印关键出纸记录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402

from printroom.server import create_server  # noqa: E402


def call(base, method, path, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title):
    print(f"\n=== {title} ===")


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        tempfile.mkdtemp(prefix="printroom-demo-"), "demo.db"
    )
    server = create_server(db_path, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body=None):
        code, data = call(base, "POST", path, body if body is not None else {})
        if code >= 400:
            print(f"  ! {path} -> {code} {data.get('error')}")
        return code, data

    def make_job(title, pages):
        _, doc = post("/api/documents", {"title": title})
        for i in range(pages):
            post(f"/api/documents/{doc['id']}/pages", {"content": f"第 {i + 1} 页"})
        _, job = post(f"/api/documents/{doc['id']}/finalize")
        print(f"  作业 #{job['id']} 《{title}》 {pages} 页 / {job['total_sheets']} 张")
        return job["id"]

    def sheet(job_id, no, sides=("front", "back")):
        _, job = post(f"/api/jobs/{job_id}/print", {"side": "front", "sheet_no": no})
        for side in sides[1:]:
            _, job = post(f"/api/jobs/{job_id}/print", {"side": side, "sheet_no": no})
        return job

    try:
        show("试跑一：半张卡纸 → 停稳 → 换机续印（6 页 / 3 张）")
        job_id = make_job("半张卡纸演示", 6)
        post(f"/api/jobs/{job_id}/start", {"printer_id": "P1"})
        print("  一号机完整出第 1 张")
        sheet(job_id, 1)
        print("  第 2 张只出了正面，报告卡纸")
        post(f"/api/jobs/{job_id}/print", {"side": "front", "sheet_no": 2})
        post(f"/api/jobs/{job_id}/jam")
        code, _ = post(f"/api/jobs/{job_id}/switch", {})
        print(f"  停稳前尝试换机 -> {code}（应被拒绝）")
        post(f"/api/jobs/{job_id}/stop")
        print("  原机已确认停稳，派续单到二号机")
        post(f"/api/jobs/{job_id}/switch", {})
        _, job = post(f"/api/jobs/{job_id}/switch", {})
        conts = [s for s in job["segments"] if s["kind"] == "continuation"]
        print(f"  续单数量 = {len(conts)}（重复换机不重复派单），从第 {conts[0]['start_sheet']} 张起")
        sheet(job_id, 2)
        sheet(job_id, 3)
        _, job = call(base, "GET", f"/api/jobs/{job_id}")
        print(f"  作业状态 {job['status']}，完整出纸 {job['printed_sheets']}/{job['total_sheets']} 张")

        show("试跑二：停稳前完整出纸都算数（8 页 / 4 张）")
        job2 = make_job("停稳前出纸演示", 8)
        post(f"/api/jobs/{job2}/start", {"printer_id": "P1"})
        sheet(job2, 1)
        sheet(job2, 2)
        print("  第 1、2 张已完整出纸")
        post(f"/api/jobs/{job2}/print", {"side": "front", "sheet_no": 3})
        post(f"/api/jobs/{job2}/jam")
        post(f"/api/jobs/{job2}/stop")
        post(f"/api/jobs/{job2}/switch", {})
        _, job = call(base, "GET", f"/api/jobs/{job2}")
        print(f"  停稳后仍保留完整出纸 {job['printed_sheets']} 张，从第 {job['current_sheet']} 张续印")
        sheet(job2, 3)
        sheet(job2, 4)
        _, job = call(base, "GET", f"/api/jobs/{job2}")
        print(f"  作业状态 {job['status']}，完整出纸 {job['printed_sheets']}/{job['total_sheets']} 张")

        show("出纸记录（网页“出纸记录”表同源）")
        _, state = call(base, "GET", "/api/state")
        for rec in reversed(state["records"]):
            back = "-" if rec["back_page"] is None else rec["back_page"]
            print(
                f"  作业#{rec['job_id']} {rec['printer_name']} 第{rec['sheet_no']}张 "
                f"正{rec['front_page']}/背{back} {rec['label']} {rec['scrap_reason'] or ''}"
            )
        print(f"\n数据库：{db_path}")
        return 0
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
