/** localStorage key holding the theme preference ("light" | "dark"; absent = follow the OS). */
export const THEME_KEY = "acvm.theme";

/**
 * Inline script for <head>: applies the stored/OS theme before first paint so there is no
 * light→dark flash. Kept in a plain (non-"use client") module so the server layout can inline it.
 */
export const THEME_BOOTSTRAP = `(function(){try{var p=localStorage.getItem('${THEME_KEY}')||'system';var d=p==='dark'||(p!=='light'&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.setAttribute('data-theme',d?'dark':'light');}catch(e){}})();`;
