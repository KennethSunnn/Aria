(() => {
  const STORAGE_KEY = "aria_locale";
  const DEFAULT_LOCALE = "en";
  const SUPPORTED = ["en", "zh"];
  let locale = DEFAULT_LOCALE;
  let dict = {};

  function normalizeLocale(raw) {
    const v = String(raw || "").toLowerCase();
    if (v.startsWith("zh")) return "zh";
    if (v.startsWith("en")) return "en";
    return DEFAULT_LOCALE;
  }

  async function loadLocale(next) {
    const target = SUPPORTED.includes(next) ? next : DEFAULT_LOCALE;
    const localeBase = location.protocol === "file:" ? "./static/locales" : "/static/locales";
    const resp = await fetch(`${localeBase}/${target}.json`, { cache: "no-cache" });
    if (!resp.ok) throw new Error(`Failed to load locale: ${target}`);
    dict = await resp.json();
    locale = target;
    localStorage.setItem(STORAGE_KEY, target);
    document.documentElement.setAttribute("lang", target === "zh" ? "zh-CN" : "en");
  }

  function t(key, fallback = "") {
    return dict[key] ?? fallback ?? key;
  }

  function format(str, vars) {
    let out = String(str || "");
    Object.entries(vars || {}).forEach(([k, v]) => {
      out = out.replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
    });
    return out;
  }

  function apply(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      const attr = el.getAttribute("data-i18n-attr");
      const value = t(key, el.textContent || "");
      if (attr) {
        el.setAttribute(attr, value);
      } else {
        el.textContent = value;
      }
    });
  }

  function buildLangSwitcher(containerSelector = "[data-lang-switcher]") {
    const host = document.querySelector(containerSelector);
    if (!host) return;
    host.innerHTML = `
      <div class="btn-group btn-group-sm" role="group" aria-label="Language switcher">
        <button type="button" class="btn btn-outline-secondary" data-lang-btn="en">EN</button>
        <button type="button" class="btn btn-outline-secondary" data-lang-btn="zh">中文</button>
      </div>
    `;
    const updateActive = () => {
      host.querySelectorAll("[data-lang-btn]").forEach((btn) => {
        const active = btn.getAttribute("data-lang-btn") === locale;
        btn.classList.toggle("btn-primary", active);
        btn.classList.toggle("btn-outline-secondary", !active);
      });
    };
    host.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-lang-btn]");
      if (!btn) return;
      const next = btn.getAttribute("data-lang-btn");
      await loadLocale(next);
      apply();
      window.dispatchEvent(new CustomEvent("aria:localeChanged", { detail: { locale } }));
      updateActive();
    });
    updateActive();
  }

  async function init(opts = {}) {
    const preferred = normalizeLocale(localStorage.getItem(STORAGE_KEY) || navigator.language || DEFAULT_LOCALE);
    await loadLocale(preferred);
    apply();
    if (opts.withSwitcher !== false) {
      buildLangSwitcher(opts.switcherSelector || "[data-lang-switcher]");
    }
  }

  window.ariaI18n = {
    init,
    t,
    format,
    apply,
    loadLocale,
    get locale() {
      return locale;
    }
  };
})();
