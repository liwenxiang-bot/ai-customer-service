/** 判断回车 keydown 是否是输入法选词上屏（中文拼音等场景），
 *  是则不应触发“发送”。兼容 antd 合成事件与原生事件（Safari 旧版走 keyCode 229）。 */
export function isImeComposing(e: unknown): boolean {
  const ev = e as { nativeEvent?: KeyboardEvent; isComposing?: boolean; keyCode?: number };
  const native = ev.nativeEvent ?? (ev as KeyboardEvent);
  return Boolean(native?.isComposing) || native?.keyCode === 229 || ev.keyCode === 229;
}
