/* Runs synchronously in <head> so the theme is set before first paint.
   Not a module and not inline - inline would need 'unsafe-inline' in the CSP. */
(function () {
  var KEY = "hv:theme";
  var stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch (err) {
    /* private mode - fall back to the system preference */
  }
  if (stored === "light" || stored === "dark") {
    document.documentElement.dataset.theme = stored;
  }

  window.hvTheme = {
    cycle: function () {
      var root = document.documentElement;
      var systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      var current = root.dataset.theme || (systemDark ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem(KEY, next);
      } catch (err) {
        /* ignore */
      }
      return next;
    },
  };
})();
