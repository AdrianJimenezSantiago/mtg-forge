from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
AXE = ROOT / "node_modules" / "axe-core" / "axe.min.js"
PAGES = (
    "/",
    "/decks",
    "/history",
    "/collection",
    "/settings",
    "/print-planner",
    "/art-library",
    "/calibrate",
    "/no-existe",
)
BLOCKING = {"critical", "serious"}
RUN_AXE = """() => axe.run(document, { resultTypes: ['violations'] }).then(r => r.violations.map(v => ({
  id: v.id, impact: v.impact, help: v.help, targets: v.nodes.slice(0, 3).map(n => n.target.join(' ')),
})))"""


async def audit(base: str) -> int:
    failures = 0
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH") or None
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900}, bypass_csp=True
        )
        for path in PAGES:
            page = await context.new_page()
            await page.goto(base + path, wait_until="networkidle")
            await page.add_script_tag(path=str(AXE))
            for violation in await page.evaluate(RUN_AXE):
                if violation["impact"] not in BLOCKING:
                    continue
                failures += 1
                print(f"{path}: {violation['id']} [{violation['impact']}] {violation['help']}")
                for target in violation["targets"]:
                    print(f"    {target}")
            await page.close()
        await browser.close()
    print(f"{failures} violaciones bloqueantes en {len(PAGES)} páginas")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(audit(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000")))
