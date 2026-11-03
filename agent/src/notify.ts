/** Opt-in desktop notification + short chime for new customer activity while the tab is hidden.
 *  No audio asset: a two-tone beep synthesized with WebAudio. Preference persists per browser. */
const KEY = "acs_agent_notify";

export function notifyEnabled(): boolean {
  try {
    return localStorage.getItem(KEY) === "1" && "Notification" in window && Notification.permission === "granted";
  } catch {
    return false;
  }
}

export async function setNotifyEnabled(on: boolean): Promise<boolean> {
  if (on) {
    if (!("Notification" in window)) return false;
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") return false;
  }
  try {
    localStorage.setItem(KEY, on ? "1" : "0");
  } catch {
    /* ignore */
  }
  return on;
}

let audio: AudioContext | null = null;
function chime() {
  try {
    audio = audio || new AudioContext();
    const t = audio.currentTime;
    [880, 1320].forEach((freq, i) => {
      const osc = audio!.createOscillator();
      const gain = audio!.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t + i * 0.16);
      gain.gain.exponentialRampToValueAtTime(0.18, t + i * 0.16 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.16 + 0.15);
      osc.connect(gain).connect(audio!.destination);
      osc.start(t + i * 0.16);
      osc.stop(t + i * 0.16 + 0.16);
    });
  } catch {
    /* autoplay policy / no audio device */
  }
}

/** Alert the agent — only when the page isn't in front (otherwise the UI already shows it). */
export function alertActivity(title: string, body: string, onClick?: () => void) {
  if (!notifyEnabled() || !document.hidden) return;
  chime();
  try {
    const n = new Notification(title, { body, tag: "acs-agent", renotify: true } as NotificationOptions);
    n.onclick = () => {
      window.focus();
      onClick?.();
      n.close();
    };
  } catch {
    /* ignore */
  }
}
