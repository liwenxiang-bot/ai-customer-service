import { createContext, useContext } from "react";

/** Light/dark mode shared via context so the header toggle (in App) can flip the
 *  algorithm set on ConfigProvider (in main). */
export const ThemeModeCtx = createContext<{ dark: boolean; toggle: () => void }>({
  dark: false,
  toggle: () => {},
});

export const useThemeMode = () => useContext(ThemeModeCtx);
