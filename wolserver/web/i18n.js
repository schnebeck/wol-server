// Languages: English is the source language in code and HTML; translations come from catalogs
// such as i18n-de.js (window.I18N_DE = { "English text": "translated text", … }).
// Keys with {0}, {1} … are patterns: t("{0} of {1} online", 3, 5) fills them in, tr() also matches
// finished texts (e.g. messages from the server) and translates the inserted values recursively.

const I18N = (() => {
  const LANGS = { en: "English", de: "Deutsch" };
  const catalogs = { de: window.I18N_DE || {} };
  let stored = "";
  try {
    stored = localStorage.getItem("lang") || "";
  } catch {
    stored = "";
  }
  const browser = (navigator.language || "en").toLowerCase().startsWith("de") ? "de" : "en";
  const lang = LANGS[stored] ? stored : browser;
  const catalog = catalogs[lang] || {};

  const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = Object.keys(catalog)
    .filter((key) => /\{\d+\}/.test(key))
    .sort((a, b) => b.length - a.length) // more specific (longer) patterns first
    .map((key) => {
      const order = [...key.matchAll(/\{(\d+)\}/g)].map((m) => Number(m[1]));
      const source = escape(key).replace(/\\\{\d+\\\}/g, "(.+?)");
      return { key, order, re: new RegExp(`^${source}$`, "s") };
    });

  function fill(text, args) {
    return text.replace(/\{(\d+)\}/g, (m, i) => (args[i] !== undefined ? args[i] : m));
  }

  /** Text from the code: t("Wake"), t("{0} of {1} online", 3, 5). */
  function t(key, ...args) {
    return fill(catalog[key] !== undefined ? catalog[key] : key, args);
  }

  /** Finished text, e.g. a server message: exact entry, else the first matching pattern. */
  function tr(text) {
    if (lang === "en" || typeof text !== "string" || !text) return text;
    if (catalog[text] !== undefined) return catalog[text];
    for (const p of patterns) {
      const m = p.re.exec(text);
      if (!m) continue;
      const args = [];
      p.order.forEach((index, pos) => { args[index] = tr(m[pos + 1]); });
      return fill(catalog[p.key], args);
    }
    // Lists built from several messages ("A; B", "IP a → b, inactive"): translate the parts
    if (/[;,] /.test(text)) {
      const parts = text.split(/([;,] )/);
      const translated = parts.map((part, i) => (i % 2 ? part : tr(part)));
      if (translated.some((part, i) => part !== parts[i])) return translated.join("");
    }
    return text;
  }

  const SKIP = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA", "KBD"]);
  const ATTRS = ["placeholder", "title", "aria-label", "alt"];

  /** Static HTML: text nodes, attributes and elements with data-i18n (whole innerHTML as key). */
  function translateDom(root) {
    if (lang === "en") return;
    for (const node of root.querySelectorAll("[data-i18n]")) {
      const key = node.innerHTML.replace(/\s+/g, " ").trim();
      if (catalog[key] !== undefined) node.innerHTML = catalog[key];
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && (SKIP.has(n.parentElement.nodeName) || n.parentElement.closest("[data-i18n]"))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const text = n.nodeValue.trim();
      if (text && catalog[text] !== undefined) n.nodeValue = n.nodeValue.replace(text, catalog[text]);
    }
    for (const el of root.querySelectorAll(ATTRS.map((a) => `[${a}]`).join(","))) {
      for (const attr of ATTRS) {
        const value = el.getAttribute(attr);
        if (value && catalog[value] !== undefined) el.setAttribute(attr, catalog[value]);
      }
    }
  }

  function setLang(next) {
    try {
      localStorage.setItem("lang", next);
    } catch {
      // without storage the choice cannot persist; the browser language applies after reload
    }
    location.reload();
  }

  document.documentElement.lang = lang;
  document.addEventListener("DOMContentLoaded", () => translateDom(document.body));

  return { lang, langs: LANGS, locale: lang === "de" ? "de-DE" : "en-GB", t, tr, translateDom, setLang };
})();

const t = I18N.t;
const tr = I18N.tr;
