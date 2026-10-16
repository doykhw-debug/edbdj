"""서버에서 헤드리스 브라우저(Playwright Chromium)가 실제로 켜지는지 확인한다. 켜지면 0.
    .venv/bin/python deploy/browser_check.py
"""

import os
import platform
import sys


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [브라우저] playwright 가 설치되지 않았습니다.")
        return 1
    exe = os.environ.get("RADIO_HELPER_CHROMIUM")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **({"executable_path": exe} if exe else {}))
            page = browser.new_page()
            page.set_content("<title>ok</title>")
            title = page.title()
            version = browser.version
            browser.close()
    except Exception as e:
        print(f"  [브라우저] 켜지지 않음 ({platform.machine()}): {str(e).strip().splitlines()[0][:200]}")
        return 1
    print(f"  [브라우저] 헤드리스 Chromium {version} 정상 ({platform.machine()}, 화면: {title})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
