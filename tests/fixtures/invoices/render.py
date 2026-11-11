#!/usr/bin/env python3
"""将本目录中的发票 HTML 渲染为 PNG 夹具。

用法（在仓库根目录或本目录均可）：

    python tests/fixtures/invoices/render.py

依赖本机的 Google Chrome（默认 /usr/bin/google-chrome）。输出只写到本目录，
不会在仓库其他位置留下临时文件。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

HERE = Path(__file__).resolve().parent


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("未找到 Chrome / Chromium，请设置 CHROME_PATH")


def render_one(chrome: str, html_path: Path, image_path: Path, width: int, height: int) -> None:
    url = html_path.resolve().as_uri()
    with tempfile.TemporaryDirectory(prefix="invoice_render_") as tmp:
        tmp_png = Path(tmp) / "capture.png"
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            f"--screenshot={tmp_png}",
            url,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0 or not tmp_png.exists():
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
            raise SystemExit(f"渲染失败：{html_path.name}")
        shutil.move(str(tmp_png), image_path)


def main() -> None:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    chrome = find_chrome()
    for item in manifest["invoices"]:
        html_path = HERE / item["html"]
        image_path = HERE / item["image"]
        if not html_path.exists():
            raise SystemExit(f"缺少 HTML 源文件：{html_path.name}")
        render_one(chrome, html_path, image_path, int(item["width"]), int(item["height"]))
        print(f"已生成 {item['image']} （{item['title']}，¥{item['amount']}）")


if __name__ == "__main__":
    main()
