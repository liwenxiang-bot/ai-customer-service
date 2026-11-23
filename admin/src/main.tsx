import React, { useEffect, useLayoutEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider, App as AntApp, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "antd/dist/reset.css";
import { AuthProvider } from "./auth";
import { ThemeModeCtx, buildTheme, inkPanel } from "./theme";
import { App } from "./App";
import "./index.css";

dayjs.locale("zh-cn"); // 日期控件周起始/星期与 antd 中文语言包一致

/** 把 antd 主题 token 同步成 CSS 变量，供 index.css 里的自有样式使用 ——
 *  颜色只在 theme.tsx 定义一次，浅色 / 深色自动跟随。 */
function CssVars({ dark }: { dark: boolean }) {
  const { token } = antdTheme.useToken();
  useLayoutEffect(() => {
    const vars: Record<string, string> = {
      "--acs-font": token.fontFamily,
      "--acs-ink": inkPanel(dark),
      "--acs-primary": token.colorPrimary,
      "--acs-primary-bg": token.colorPrimaryBg,
      "--acs-text": token.colorText,
      "--acs-text-2": token.colorTextSecondary,
      "--acs-text-3": token.colorTextTertiary,
      "--acs-line": token.colorBorderSecondary,
      "--acs-surface": token.colorBgContainer,
      "--acs-bg": token.colorBgLayout,
      "--acs-fill": token.colorFillQuaternary,
      "--acs-fill-hover": token.colorFillTertiary,
      "--acs-success": token.colorSuccess,
      "--acs-warning": token.colorWarning,
      "--acs-error": token.colorError,
    };
    const root = document.documentElement.style;
    Object.entries(vars).forEach(([k, v]) => root.setProperty(k, v));
  }, [token, dark]);
  return null;
}

function Root() {
  const [dark, setDark] = useState(() => localStorage.getItem("acs_admin_dark") === "1");

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    localStorage.setItem("acs_admin_dark", dark ? "1" : "0");
  }, [dark]);

  return (
    <ConfigProvider locale={zhCN} theme={buildTheme(dark)}>
      <CssVars dark={dark} />
      <AntApp>
        <BrowserRouter>
          <AuthProvider>
            <ThemeModeCtx.Provider value={{ dark, toggle: () => setDark((d) => !d) }}>
              <App />
            </ThemeModeCtx.Provider>
          </AuthProvider>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
