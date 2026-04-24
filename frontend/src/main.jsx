import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

(() => {
  try {
    const stored = localStorage.getItem("theme");
    const theme = stored === "light" ? "light" : "dark";
    const root = document.documentElement;
    root.classList.remove("theme-light", "theme-dark");
    root.classList.add(theme === "light" ? "theme-light" : "theme-dark");
  } catch {
    document.documentElement.classList.add("theme-dark");
  }
})();

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
