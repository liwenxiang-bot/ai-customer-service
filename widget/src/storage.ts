/** localStorage that never throws. Hosts can block storage (privacy modes, sandboxed iframes,
 *  third-party-storage policies); a raw access then throws and the widget would not mount at
 *  all. Fall back to an in-memory map so chat still works (just without persistence). */
const memory = new Map<string, string>();

export const store = {
  get(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return memory.has(key) ? memory.get(key)! : null;
    }
  },
  set(key: string, value: string): void {
    try {
      localStorage.setItem(key, value);
    } catch {
      memory.set(key, value);
    }
  },
  remove(key: string): void {
    try {
      localStorage.removeItem(key);
    } catch {
      memory.delete(key);
    }
  },
};
