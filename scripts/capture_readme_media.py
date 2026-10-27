from __future__ import annotations

import argparse
import asyncio
import io
import time
from pathlib import Path

from PIL import Image
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "media"
GIF_WIDTH = 960


def save_webp(png: bytes, name: str) -> None:
    Image.open(io.BytesIO(png)).convert("RGB").save(
        OUT / f"{name}.webp", "WEBP", quality=86, method=6
    )


async def screenshots(page: Page, base: str, deck: int) -> None:
    async def go(path: str, wait: int = 2500) -> None:
        await page.goto(base + path, wait_until="networkidle")
        await page.wait_for_timeout(wait)

    async def shot(name: str) -> None:
        save_webp(await page.screenshot(), name)

    await go("/")
    await shot("landing")
    await go(f"/decks/{deck}")
    await shot("deck-editor")
    await page.locator('button[data-tour="deck-row-art"]').first.click()
    await page.mouse.move(5, 895)
    await page.wait_for_timeout(4000)
    await shot("art-picker")
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(800)
    await page.locator('button[\\@click="openStats()"]').click()
    await page.wait_for_timeout(3000)
    await shot("deck-stats")
    await go(f"/decks/{deck}/pdf", 4000)
    await shot("pdf-studio")
    await page.locator('button[\\@click="setPress(shop.key)"]').first.click()
    await page.wait_for_timeout(4000)
    await shot("print-shop")
    await go("/print-planner")
    await page.locator('button[\\@click="selectAll()"]').click()
    await page.wait_for_timeout(3000)
    await shot("print-planner")
    await go(f"/decks/{deck}/proof", 4000)
    await shot("proof")


async def demo(page: Page, base: str, deck: int) -> None:
    frames: list[tuple[Image.Image, float]] = []
    last = time.monotonic()

    async def snap() -> None:
        nonlocal last
        image = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")
        image = image.resize((GIF_WIDTH, round(image.height * GIF_WIDTH / image.width)))
        now = time.monotonic()
        if frames:
            frames[-1] = (frames[-1][0], now - last)
        frames.append((image, 0.1))
        last = now

    async def record(ms: int) -> None:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            await snap()

    async def hold(ms: int) -> None:
        await snap()
        await page.wait_for_timeout(ms)

    await page.goto(f"{base}/decks/{deck}", wait_until="networkidle")
    await page.wait_for_timeout(1200)
    await hold(1500)
    await page.mouse.wheel(0, 450)
    await record(700)
    await hold(900)
    await page.mouse.wheel(0, -450)
    await record(600)
    art = page.locator('button[data-tour="deck-row-art"]').first
    await art.hover()
    await hold(300)
    await art.click()
    await record(1200)
    await page.mouse.move(640, 790)
    await page.wait_for_timeout(1200)
    await hold(1200)
    tile = page.locator('button[\\@click="pickArt(art, false)"]').nth(5)
    await tile.hover()
    await record(600)
    await hold(1500)
    await tile.click()
    await record(1500)
    await page.wait_for_load_state("networkidle")
    await hold(1500)
    stats = page.locator('button[\\@click="openStats()"]')
    await stats.hover()
    await hold(300)
    await stats.click()
    await record(2200)
    await hold(1800)
    await page.keyboard.press("Escape")
    await record(500)
    await page.goto(f"{base}/decks/{deck}/pdf", wait_until="networkidle")
    await page.wait_for_timeout(1500)
    await hold(2000)
    press = page.locator('button[\\@click="setPress(shop.key)"]').first
    await press.hover()
    await hold(300)
    await press.click()
    await record(1500)
    await hold(2500)
    await snap()

    samples = [f for f, _ in frames[:: max(1, len(frames) // 12)]]
    montage = Image.new("RGB", (samples[0].width, samples[0].height * len(samples)))
    for i, sample in enumerate(samples):
        montage.paste(sample, (0, i * sample.height))
    palette = montage.quantize(colors=224, method=Image.Quantize.MEDIANCUT)
    images = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in frames]
    images[0].save(
        OUT / "demo.gif",
        save_all=True,
        append_images=images[1:],
        duration=[max(40, round(d * 1000)) for _, d in frames],
        loop=0,
        optimize=True,
        disposal=1,
    )


async def main(base: str, deck: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for viewport, ambient, task in (
            ({"width": 1440, "height": 900}, True, screenshots),
            ({"width": 1280, "height": 800}, False, demo),
        ):
            ctx = await browser.new_context(viewport=viewport, color_scheme="dark", locale="en-US")
            await ctx.add_cookies([{"name": "lang", "value": "en", "url": base}])
            if not ambient:
                await ctx.add_init_script("localStorage.setItem('mpc-ambient', 'off')")
            await task(await ctx.new_page(), base, deck)
            await ctx.close()
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Regenera las capturas y el GIF del README.")
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8765")
    parser.add_argument("--deck", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(main(args.base.rstrip("/"), args.deck))
