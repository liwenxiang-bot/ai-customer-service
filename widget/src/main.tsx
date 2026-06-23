import { render } from "preact";
import { App, WidgetConfig } from "./App";
import { buildStyles } from "./styles";

declare global {
  interface Window {
    ACS_CONFIG?: { channelKey?: string; mode?: "popup" | "fullscreen"; baseUrl?: string };
    __ACS_MOUNTED__?: boolean;
  }
}

function resolveBaseUrl(): string {
  if (window.ACS_CONFIG?.baseUrl) return window.ACS_CONFIG.baseUrl.replace(/\/$/, "");
  const cs = document.currentScript as HTMLScriptElement | null;
  let src = cs?.src || "";
  if (!src) {
    const found = Array.from(document.getElementsByTagName("script")).find((s) =>
      s.src.includes("/embed/widget.js")
    );
    src = found?.src || "";
  }
  try {
    return src ? new URL(src).origin : window.location.origin;
  } catch {
    return window.location.origin;
  }
}

async function boot() {
  if (window.__ACS_MOUNTED__) return;
  window.__ACS_MOUNTED__ = true;

  const cfg = window.ACS_CONFIG || {};
  const baseUrl = resolveBaseUrl();
  const channelKey = cfg.channelKey || "default";
  const mode = cfg.mode || "popup";

  let branding: WidgetConfig["branding"];
  try {
    const resp = await fetch(`${baseUrl}/api/chat/config?channel_key=${encodeURIComponent(channelKey)}`, {
      headers: { "Content-Type": "application/json" },
    });
    const data = await resp.json();
    if (data.allowed === false) {
      // Domain not whitelisted — refuse to load on this origin.
      console.warn("[ACS] widget not allowed on this domain");
      return;
    }
    branding = data.branding;
    if (branding && (data.branding.enabled === false)) {
      console.warn("[ACS] widget disabled");
      return;
    }
  } catch (e) {
    console.warn("[ACS] failed to load config", e);
    return;
  }

  const host = document.createElement("div");
  host.id = "acs-widget-host";
  if (mode === "fullscreen") {
    const target = document.getElementById("acs-root") || document.body;
    target.appendChild(host);
    host.style.cssText = "position:fixed;inset:0;";
  } else {
    document.body.appendChild(host);
  }

  const shadow = host.attachShadow({ mode: "open" });
  const styleEl = document.createElement("style");
  styleEl.textContent = buildStyles(branding.theme_color || "#4f46e5");
  shadow.appendChild(styleEl);
  const mount = document.createElement("div");
  shadow.appendChild(mount);

  const config: WidgetConfig = { baseUrl, channelKey, mode, branding };
  render(<App config={config} />, mount);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
