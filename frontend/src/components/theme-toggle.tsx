"use client";

import { useState } from "react";

export function ThemeToggle() {
  // The head script sets data-theme before hydration, so the initial state matches the applied theme.
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    typeof document !== "undefined" && document.documentElement.dataset.theme === "dark" ? "dark" : "light");

  function toggle() {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("tess:theme", next); } catch { /* The choice persists only for this browser. */ }
    setTheme(next);
  }

  return <button type="button" className="text-button" onClick={toggle} aria-label="Alternar tema">{theme === "dark" ? "Tema claro" : "Tema escuro"}</button>;
}
