import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConfigProvider, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import "antd/dist/reset.css";
import { AuthProvider } from "./auth";
import { App } from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#0F766E",
          borderRadius: 6,
          fontFamily:
            '"IBM Plex Sans", "Source Han Sans SC", system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
          colorBgLayout: "#F6F8FA",
          colorText: "#18222E",
          colorTextSecondary: "#5B6573",
          colorBorderSecondary: "#E3E7EC",
          colorSuccess: "#16A34A",
          colorWarning: "#D97706",
          colorError: "#DC2626",
          colorInfo: "#0EA5E9",
        },
        components: {
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
            <App />
          </AuthProvider>
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>
);
