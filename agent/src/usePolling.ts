import { useEffect, useRef } from "react";

/** 周期轮询：挂载后立即执行一次 `fn`，之后每 `intervalMs` 执行；
 *  页面切到后台（document.hidden）时暂停，回到前台立刻补一次并恢复。
 *  `enabled: false` 时完全停止；`deps` 变化会重启轮询并立即执行一次。 */
export function usePolling(
  fn: () => void,
  intervalMs: number,
  opts?: { enabled?: boolean; deps?: unknown[] }
) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const enabled = opts?.enabled ?? true;
  const deps = opts?.deps ?? [];

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer) return;
      fnRef.current();
      timer = setInterval(() => fnRef.current(), intervalMs);
    };
    const stop = () => {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => (document.hidden ? stop() : start());
    document.addEventListener("visibilitychange", onVisibility);
    if (!document.hidden) start();
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, ...deps]);
}
