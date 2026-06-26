import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider, App as AntApp, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import { AuthProvider } from "./auth";
import { ThemeModeCtx } from "./theme";
import { App } from "./App";
import "./index.css";

const FONT =
  '"IBM Plex Sans", "Source Han Sans SC", system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';

function Root() {
  const [dark, setDark] = useState(() => localStorage.getItem("acs_admin_dark") === "1");

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    localStorage.setItem("acs_admin_dark", dark ? "1" : "0");
  }, [dark]);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: "#0F766E",
          borderRadius: 8,
          fontFamily: FONT,
          colorSuccess: "#16A34A",
          colorWarning: "#D97706",
          colorError: "#DC2626",
          colorInfo: "#0EA5E9",
          ...(dark ? {} : { colorBgLayout: "#F5F7FA", colorText: "#18222E", colorTextSecondary: "#5B6573", colorBorder: "#E5E9EE", colorBorderSecondary: "#EAEDF1" }),
        },
        components: dark
          ? {
              // Keep the sider, its trigger and the header on one dark surface (#141414)
              // so the menu area doesn't read as a different shade from the brand bar.
              Layout: { siderBg: "#141414", headerBg: "#141414", triggerBg: "#141414", triggerColor: "rgba(255,255,255,0.65)" },
              Menu: { itemBorderRadius: 8, itemBg: "transparent", itemSelectedBg: "rgba(45,212,191,0.16)", itemSelectedColor: "#5eead4", itemMarginInline: 8 },
              Table: { cellPaddingBlock: 12, headerSplitColor: "transparent" },
              Card: { borderRadiusLG: 12 },
              Tabs: { titleFontSize: 14 },
              Button: { fontWeight: 500 },
            }
          : {
              Layout: { siderBg: "#FFFFFF", headerBg: "#FFFFFF", bodyBg: "#F5F7FA" },
              Table: { headerBg: "#F6F8FA", headerColor: "#5B6573", cellPaddingBlock: 12, headerSplitColor: "transparent", rowHoverBg: "#F7FAF9" },
              Menu: { itemBorderRadius: 8, itemSelectedBg: "#E6F2F0", itemSelectedColor: "#0F766E", itemMarginInline: 8 },
              Card: { borderRadiusLG: 12 },
              Tabs: { titleFontSize: 14 },
              Button: { fontWeight: 500 },
            },
      }}
    >
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
