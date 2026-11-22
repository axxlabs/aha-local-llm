// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Axxlabs Limited
// Runs in every page before the page's own scripts (SPEC §3.4 "Looking like a person's browser").
// The page's console calls never reach DevTools: an object logged to the console is previewed over CDP, which a
// page can detect. Nobody reads this browser's console. Proxies keep the functions looking native.
(() => {
  const names = ["debug", "log", "info", "warn", "error", "dir", "dirxml", "table", "trace", "group",
    "groupCollapsed", "assert", "count", "timeLog"];
  for (const name of names) {
    const f = console[name];
    if (typeof f === "function") console[name] = new Proxy(f, { apply() { return undefined; } });
  }
})();
