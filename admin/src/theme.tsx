import { createContext, useContext } from "react";
import { theme as antdTheme, type ThemeConfig } from "antd";

/** Light/dark mode shared via context so the header toggle (in App) can flip the
 *  algorithm set on ConfigProvider (in main). */
export const ThemeModeCtx = createContext<{ dark: boolean; toggle: () => void }>({
  dark: false,
  toggle: () => {},
});

export const useThemeMode = () => useContext(ThemeModeCtx);

/** 品牌主色。换品牌色只改这一处，其余色阶由 antd 算法派生。 */
export const BRAND = "#24616C";
/** 深色底上的主色：同色相提亮一档，保证链接与选中态的对比度。 */
const BRAND_ON_DARK = "#74B4C0";

/** 登录页品牌区的底色：主色压深一档。深色模式下再压深一档，但仍是墨青 ——
 *  不能退成中性深灰，否则和右侧表单面板同色，左右两栏糊成一片。 */
export const inkPanel = (dark: boolean) => (dark ? "#15343A" : "#1B444C");

const FONT =
  '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Microsoft YaHei", "Source Han Sans SC", "Noto Sans CJK SC", "Segoe UI", sans-serif';

/** 后台主题：低饱和墨青主色、中性浅灰底、6px 圆角、发丝线分区、无投影；状态色只留语义四色。
 *  底色与线条保持中性（不带黄调）：暖灰在大面积留白处会显脏。 */
export function buildTheme(dark: boolean): ThemeConfig {
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: dark ? BRAND_ON_DARK : BRAND,
      colorInfo: dark ? BRAND_ON_DARK : BRAND,
      colorSuccess: "#2E7D5B",
      colorWarning: "#B7791F",
      colorError: "#C8402F",
      colorLink: dark ? BRAND_ON_DARK : BRAND,
      borderRadius: 6,
      fontFamily: FONT,
      ...(dark
        ? { colorBgLayout: "#121413", colorBgContainer: "#1A1D1C", colorBgElevated: "#202423", colorBorderSecondary: "#2A2E2D", colorBorder: "#3A3F3E" }
        : { colorBgLayout: "#F6F7F8", colorText: "#1E2424", colorTextSecondary: "#5E6669", colorBorder: "#D9DDDF", colorBorderSecondary: "#EBEDEE" }),
    },
    components: {
      Layout: { headerBg: dark ? "#1A1D1C" : "#FFFFFF", headerHeight: 52, headerPadding: "0 16px 0 12px" },
      Menu: {
        itemBorderRadius: 6,
        itemMarginInline: 8,
        itemMarginBlock: 2,
        itemHeight: 38,
        activeBarBorderWidth: 0,
        itemBg: "transparent",
        subMenuItemBg: "transparent",
        ...(dark ? {} : { itemSelectedBg: "#E6EFF0", itemHoverBg: "#F3F5F5", itemColor: "#3C4446" }),
      },
      Table: {
        cellPaddingBlock: 11,
        headerSplitColor: "transparent",
        headerBorderRadius: 0,
        ...(dark ? {} : { headerBg: "#F8F9FA", headerColor: "#5E6669", rowHoverBg: "#F4F8F8" }),
      },
      Card: { headerFontSize: 15, headerFontSizeSM: 14 },
      Tabs: { titleFontSize: 14, horizontalItemGutter: 28 },
      // 深色模式的主色是提亮的浅墨青，主按钮上的白字对比度不够，改用深色字
      Button: { primaryShadow: "none", defaultShadow: "none", dangerShadow: "none", ...(dark ? { primaryColor: "#0E1B1E" } : {}) },
      Tag: dark ? {} : { defaultBg: "#F3F5F5" },
      Statistic: { titleFontSize: 13 },
    },
  };
}
