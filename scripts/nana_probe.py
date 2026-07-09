from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data" / "nana"
DEFAULT_CDP_URL = "http://127.0.0.1:9223"
DEFAULT_PROJECT_URL = "https://labs.google/fx/zh/tools/flow/project/aae032c7-27b7-4ee2-85b1-944b48351403"
FLOW_LANDING_CTA = "Create with Google Flow"


def _clean(value: object) -> str:
    return str(value or "").strip().lstrip("\ufeff")


def _default_cdp_url() -> str:
    env_url = _clean(os.getenv("NANA_CDP_URL"))
    if env_url:
        return env_url
    cdp_url_file = BASE_DIR / "data" / "nana_cdp_url.txt"
    if cdp_url_file.exists():
        file_url = _clean(cdp_url_file.read_text(encoding="utf-8", errors="replace"))
        if file_url:
            return file_url
    return DEFAULT_CDP_URL


def _is_flow_project_url(url: str) -> bool:
    normalized = _clean(url).lower()
    return (
        "labs.google" in normalized
        and "/tools/flow/project/" in normalized
    )


def _is_google_login_url(url: str) -> bool:
    normalized = _clean(url).lower()
    return "accounts.google." in normalized


async def _page_title(page: Page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def _page_summary(page: Page) -> dict[str, str]:
    return {
        "url": page.url,
        "title": await _page_title(page),
    }


async def _inner_text_head(page: Page, limit: int = 2000) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""
    return text[:limit]


async def _visible_count(page: Page, selector: str) -> int:
    try:
        count = await page.locator(selector).count()
        visible = 0
        for index in range(min(count, 50)):
            try:
                if await page.locator(selector).nth(index).is_visible(timeout=300):
                    visible += 1
            except Exception:
                pass
        return visible
    except Exception:
        return 0


async def _click_flow_landing_cta(page: Page) -> bool:
    candidates = [
        page.get_by_role("button", name=FLOW_LANDING_CTA).first,
        page.locator("button").filter(has_text=FLOW_LANDING_CTA).first,
        page.get_by_text(FLOW_LANDING_CTA, exact=True).first,
    ]
    for locator in candidates:
        try:
            await locator.wait_for(state="visible", timeout=3000)
            await locator.scroll_into_view_if_needed(timeout=3000)
            await locator.click(timeout=10000)
            return True
        except Exception:
            pass
    return False


async def _detect_flow_state(page: Page) -> dict[str, Any]:
    body_head = await _inner_text_head(page)
    normalized_url = _clean(page.url).lower()
    lower_body = body_head.lower()
    state = "unknown"
    reason = ""

    if _is_google_login_url(page.url):
        state = "login_required"
        reason = "google_login_url"
    elif not _is_flow_project_url(page.url):
        state = "not_flow_project"
        reason = "url_not_flow_project"
    elif FLOW_LANDING_CTA.lower() in lower_body and "your ai creative studio" in lower_body:
        state = "landing"
        reason = "flow_marketing_landing"
    elif "智能体设置" in body_head or "图片生成默认设置" in body_head:
        state = "workspace"
        reason = "flow_settings_panel"
    elif "所有媒体内容" in body_head and ("图片" in body_head or "角色" in body_head or "场景" in body_head):
        state = "workspace"
        reason = "flow_project_media_ui"
    else:
        textbox_count = await _visible_count(page, "textarea, [contenteditable='true'], [role='textbox']")
        if textbox_count > 0:
            state = "workspace"
            reason = "visible_prompt_like_input"
        elif any(marker in lower_body for marker in ("flow sessions", "overview", "pricing")):
            state = "landing"
            reason = "flow_landing_nav_text"
        elif "/tools/flow/project/" in normalized_url:
            state = "flow_project_unknown"
            reason = "project_url_without_known_workspace_marker"

    return {
        "flow_state": state,
        "reason": reason,
        "body_text_head": body_head,
        "visible_prompt_like_inputs": await _visible_count(page, "textarea, [contenteditable='true'], [role='textbox']"),
    }


async def _find_flow_page(pages: list[Page], project_url: str) -> Page | None:
    target = _clean(project_url)
    if target:
        target_without_hash = target.split("#", 1)[0]
        for page in pages:
            current = _clean(page.url)
            if current == target or current.split("#", 1)[0] == target_without_hash:
                return page
    for page in pages:
        if _is_flow_project_url(page.url):
            return page
    return None


async def probe(
    cdp_url: str,
    project_url: str,
    open_if_missing: bool,
    screenshot: bool,
    click_landing_cta: bool,
) -> dict[str, Any]:
    started = time.perf_counter()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probes_dir = DATA_DIR / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "ok": False,
        "cdp_url": cdp_url,
        "project_url": project_url,
        "browser_connected": False,
        "flow_tab_bound": False,
        "needs_flow_tab": False,
        "needs_login": False,
        "flow_state": "unknown",
        "tabs": [],
    }

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            result["error"] = f"cdp_connect_failed: {exc}"
            return result

        result["browser_connected"] = True
        try:
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            pages = list(context.pages)
            result["tabs"] = [await _page_summary(page) for page in pages]

            page = await _find_flow_page(pages, project_url)
            if page is None and open_if_missing and project_url:
                page = await context.new_page()
                await page.goto(project_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                pages = list(context.pages)
                result["tabs"] = [await _page_summary(item) for item in pages]

            if page is None:
                result["needs_flow_tab"] = True
                result["error"] = "flow_project_tab_not_found"
                return result

            await page.bring_to_front()
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            selected = await _page_summary(page)
            result["selected_tab"] = selected
            result["flow_tab_bound"] = _is_flow_project_url(page.url)
            result["needs_login"] = _is_google_login_url(page.url)

            if click_landing_cta and result["flow_tab_bound"]:
                flow_state = await _detect_flow_state(page)
                if flow_state.get("flow_state") == "landing":
                    result["clicked_landing_cta"] = await _click_flow_landing_cta(page)
                    if result["clicked_landing_cta"]:
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                        try:
                            await page.wait_for_load_state("networkidle", timeout=30000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(3000)
                        selected = await _page_summary(page)
                        result["selected_tab"] = selected
                        result["flow_tab_bound"] = _is_flow_project_url(page.url)
                        result["needs_login"] = _is_google_login_url(page.url)

            result.update(await _detect_flow_state(page))

            if screenshot:
                screenshot_path = probes_dir / f"nana_probe_{time.strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)
                result["screenshot"] = str(screenshot_path)

            result["ok"] = bool(result["browser_connected"] and result.get("flow_state") == "workspace")
            if not result["ok"] and not result.get("error"):
                if result.get("flow_state") == "login_required":
                    result["error"] = "google_login_required"
                elif result.get("flow_state") == "landing":
                    result["error"] = "flow_landing_page_not_workspace"
                else:
                    result["error"] = "flow_workspace_not_ready"
            return result
        finally:
            await browser.close()
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Probe Nana Banana / Google Labs Flow Chrome CDP connection.")
    parser.add_argument("--cdp-url", default=_default_cdp_url())
    parser.add_argument("--project-url", default=os.getenv("NANA_PROJECT_URL", DEFAULT_PROJECT_URL))
    parser.add_argument("--no-open", action="store_true", help="Do not open project URL when no Flow tab is found.")
    parser.add_argument("--no-screenshot", action="store_true", help="Do not capture a probe screenshot.")
    parser.add_argument("--click-landing-cta", action="store_true", help="Click the Flow landing CTA before detecting workspace state.")
    args = parser.parse_args()

    result = asyncio.run(
        probe(
            cdp_url=args.cdp_url,
            project_url=args.project_url,
            open_if_missing=not args.no_open,
            screenshot=not args.no_screenshot,
            click_landing_cta=args.click_landing_cta,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
