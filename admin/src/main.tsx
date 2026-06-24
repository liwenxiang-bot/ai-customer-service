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
          borderRadius: 6,
          fontFamily: FONT,
          colorSuccess: "#16A34A",
          colorWarning: "#D97706",
          colorError: "#DC2626",
          colorInfo: "#0EA5E9",
          ...(dark ? {} : { colorBgLayout: "#F6F8FA", colorText: "#18222E", colorTextSecondary: "#5B6573", colorBorderSecondary: "#E3E7EC" }),
        },
        components: dark
          ? { Menu: { itemBorderRadius: 6 } }
          : {
              Layout: { siderBg: "#FFFFFF", headerBg: "#FFFFFF", bodyBg: "#F6F8FA" },
              Table: { headerBg: "#F6F8FA", headerColor: "#5B6573", cellPaddingBlock: 10 },
              Menu: { itemBorderRadius: 6, itemSelectedBg: "#E6F2F0", itemSelectedColor: "#0F766E" },
              Card: { borderRadiusLG: 8 },
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
