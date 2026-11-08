/*
 * Reload the page after the server restarts (a deploy). A page loaded before then
 * sends callbacks the new server doesn't know, so its buttons silently do nothing.
 *
 * Remembers the server's boot id from when the page loaded and checks it again
 * every minute and whenever the tab comes back into view. A changed id reloads the
 * page, but not while a dialog is open, so nothing half-typed is lost; it reloads
 * once the dialog closes. An unreachable server is ignored and checked again later.
 */
(function () {
  "use strict";

  const BOOT_PATH = "/_gwc/boot";
  const CHECK_EVERY_MS = 60 * 1000;
  const DIALOG_WAIT_MS = 2 * 1000;

  let loadedBoot = null;
  let stale = false;
  let checking = false;

  function dialogOpen() {
    return document.querySelector(".mantine-Modal-content, .mantine-Drawer-content") !== null;
  }

  function reloadWhenIdle() {
    if (dialogOpen()) {
      setTimeout(reloadWhenIdle, DIALOG_WAIT_MS);
      return;
    }
    window.location.reload();
  }

  async function check() {
    if (stale || checking) return;
    checking = true;
    try {
      const response = await fetch(BOOT_PATH, { cache: "no-store" });
      if (!response.ok) return;
      const { boot } = await response.json();
      if (loadedBoot === null) {
        loadedBoot = boot;
      } else if (boot !== loadedBoot) {
        stale = true;
        reloadWhenIdle();
      }
    } catch (e) {
      // Server restarting or offline: try again on the next check.
    } finally {
      checking = false;
    }
  }

  check();
  setInterval(check, CHECK_EVERY_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") check();
  });
  window.addEventListener("focus", check);
})();
