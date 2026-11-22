# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
"""How the browser looks to a site (SPEC §3.4 "Looking like a person's browser", test 20).

    uv run python scripts/browser_check.py http://127.0.0.1:18931/mcp [--online]

Connects to a Playwright MCP endpoint (start one for testing with the same image and flags as
deploy/quadlet/aha-browser.container, publishing its port on 127.0.0.1) and prints what a page sees: the
User-Agent and client hints, navigator.webdriver, language, timezone, screen and window sizes, the WebGL
renderer, and whether a console object reaches DevTools (CDP). The page is served by the browser itself, so
nothing leaves the computer. With --online it also opens two public bot-detection pages and a reCAPTCHA v3 demo
and prints their verdicts: that sends ordinary page requests to those sites, nothing of anyone's. Never point it at
a browser with a person's sign-ins in it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PROBE_PAGE = "<html><body>probe</body></html>"
PROBE = r"""async (page) => {
  let headers = {};
  await page.route('https://probe.invalid/**', async (route) => {
    headers = route.request().headers();
    await route.fulfill({ contentType: 'text/html', body: PROBE_BODY });
  });
  await page.goto('https://probe.invalid/');
  const seen = await page.evaluate(async () => {
    let cdp = false;
    const e = new Error();
    Object.defineProperty(e, 'stack', { get() { cdp = true; return ''; } });
    console.debug(e);
    await new Promise((r) => setTimeout(r, 50));
    const gl = document.createElement('canvas').getContext('webgl');
    const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
    const hints = navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues(['platform']) : null;
    return {
      webdriver: navigator.webdriver, userAgent: navigator.userAgent, brands: hints && hints.brands,
      languages: navigator.languages, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen: [screen.width, screen.height], window: [outerWidth, outerHeight], page: [innerWidth, innerHeight],
      webgl: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : null, consoleReachesCdp: cdp,
    };
  });
  await page.unroute('https://probe.invalid/**');
  return JSON.stringify({ headers: { 'user-agent': headers['user-agent'], 'sec-ch-ua': headers['sec-ch-ua'],
    'accept-language': headers['accept-language'] }, page: seen });
}""".replace("PROBE_BODY", json.dumps(PROBE_PAGE))

SANNYSOFT = r"""async (page) => { await page.goto('https://bot.sannysoft.com/', { waitUntil: 'load' });
  await page.waitForTimeout(4000);
  return JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('td.failed, td.warn')]
    .map((td) => (td.parentElement.cells[0]?.innerText || '') + ': ' + td.innerText.slice(0, 80)))); }"""
ARE_YOU_A_BOT = r"""async (page) => {
  await page.goto('https://deviceandbrowserinfo.com/are_you_a_bot', { waitUntil: 'load' });
  await page.waitForTimeout(6000);
  const text = await page.evaluate(() => document.body.innerText);
  const m = text.match(/\{[\s\S]*?"isBot"[\s\S]*?\}\s*\}/);
  return m ? m[0] : 'no verdict found'; }"""
RECAPTCHA = r"""async (page) => { await page.goto('https://recaptcha-demo.appspot.com/recaptcha-v3-request-scores.php');
  await page.waitForTimeout(6000);
  const m = (await page.content()).match(/"score":\s*([0-9.]+)/);
  return m ? m[1] : 'no score found'; }"""


def result(r) -> object:
    text = "\n".join(c.text for c in r.content if hasattr(c, "text"))
    if r.isError or "### Result" not in text:
        return f"error: {text[:300]}"
    value = json.loads(text.split("### Result", 1)[1].split("\n### ", 1)[0].strip())
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


async def main(url: str, online: bool) -> None:
    async with streamablehttp_client(url) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        run = lambda code: s.call_tool("browser_run_code_unsafe", {"code": code})  # noqa: E731
        probe = result(await run(PROBE))
        print(json.dumps(probe, indent=1))
        if isinstance(probe, dict):
            seen = probe["page"]
            problems = []
            if seen["webdriver"]:
                problems.append("navigator.webdriver is true")
            if "Headless" in (seen["userAgent"] or ""):
                problems.append("the User-Agent says HeadlessChrome")
            if seen["timezone"] == "UTC":
                problems.append("the timezone is UTC")
            if seen["screen"] == seen["page"]:
                problems.append("the screen is the size of the page (a headless default)")
            if seen["consoleReachesCdp"]:
                problems.append("console objects reach DevTools (CDP)")
            if not seen["webgl"]:
                problems.append("no WebGL")
            print("\nLooks like a person's browser." if not problems else "\nProblems: " + "; ".join(problems))
        if online:
            print("\nbot.sannysoft.com failures:", result(await run(SANNYSOFT)))
            verdict = result(await run(ARE_YOU_A_BOT))
            flagged = re.findall(r'"(\w+)":\s*true', verdict) if isinstance(verdict, str) else verdict
            print("deviceandbrowserinfo.com flags:", flagged)
            print("reCAPTCHA v3 demo score:", result(await run(RECAPTCHA)))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("url", help="the Playwright MCP endpoint, e.g. http://127.0.0.1:18931/mcp")
    p.add_argument("--online", action="store_true", help="also open public bot-detection pages")
    a = p.parse_args()
    asyncio.run(main(a.url, a.online))
