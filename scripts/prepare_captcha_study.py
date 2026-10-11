"""Snapshot public site code for the dated CAPTCHA investigation; no login."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

BASE = "https://epe.pku.edu.cn/venue/"
ROOT = Path(__file__).resolve().parents[1] / "debugging/investigations/captcha-verification"


def fetch(url: str) -> bytes:
    if urlsplit(url).netloc != "epe.pku.edu.cn":
        raise ValueError("Only public PKU assets are allowed")
    with urlopen(url, timeout=20) as response:
        if urlsplit(response.url).netloc != "epe.pku.edu.cn":
            raise ValueError("Unexpected public asset redirect")
        body = response.read(8_000_001)
    if len(body) > 8_000_000:
        raise ValueError("Public asset too large")
    return body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day", help="Beijing scheduled run date, YYYYMMDD")
    args = parser.parse_args()
    datetime.strptime(args.day, "%Y%m%d")
    output = ROOT / args.day / "site-study"
    output.mkdir(parents=True, exist_ok=True)

    class Scripts(HTMLParser):
        urls: list[str] = []

        def handle_starttag(self, tag, attrs):
            src = dict(attrs).get("src", "")
            if tag == "script" and src.endswith(".js") and (
                "chingo." in src or src.endswith("/static/js/app.js")
            ):
                self.urls.append(urljoin(BASE, src))

    html = fetch(urljoin(BASE, "home")).decode("utf-8")
    p = Scripts()
    p.feed(html)
    sources: dict[str, bytes] = {url: fetch(url) for url in p.urls}
    # The reservation page's async chunk is named in the webpack runtime.
    for content in list(sources.values()):
        source = content.decode("utf-8", errors="replace")
        match = re.search(r'"chunk-c3869bc6"\s*:\s*"([a-f0-9]+)"', source)
        if match:
            url = urljoin(BASE, f"js/chunk-c3869bc6.{match.group(1)}.js")
            sources[url] = fetch(url)

    manifest = {"prepared_at": datetime.now().isoformat(), "assets": [], "algorithm": {}}
    for url, body in sources.items():
        name = Path(urlsplit(url).path).name
        (output / name).write_bytes(body)  # generated public-study artifact
        source = body.decode("utf-8", errors="replace")
        manifest["assets"].append({"path": urlsplit(url).path, "sha256": hashlib.sha256(body).hexdigest()})
        if "name:\"VerifyPoints\"" in source:
            checks = {
                "aes_ecb_pkcs7": all(x in source for x in ("AES.encrypt", "mode.ECB", "pad.Pkcs7")),
                "verification_token_separator": 'backToken+"---"+JSON.stringify' in source,
                "check_encrypts_point_json": "d(JSON.stringify(t.checkPosArr),t.secretKey)" in source,
                "success_emits_token": "t.success(e,t.backToken)" in source,
                "reservation_submits_emitted_values": "{captchaVerification:e,captchaToken:t}" in source,
            }
            manifest["algorithm"] = {"source_sha256": hashlib.sha256(body).hexdigest(), **checks}
    manifest["algorithm_confirmed"] = bool(manifest["algorithm"]) and all(
        value is True for name, value in manifest["algorithm"].items() if name != "source_sha256"
    )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    if not manifest["algorithm_confirmed"]:
        raise SystemExit("Public widget changed or chunk unavailable: inspect the dated assets before enabling generation checks")


if __name__ == "__main__":
    main()
