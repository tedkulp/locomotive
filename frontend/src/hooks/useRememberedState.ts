import { useEffect, useState } from "react";

/** useState that this browser remembers across page changes and reloads (a per-viewer convenience only:
 *  storage can be missing or blocked, in which case it behaves like plain useState). */
export function useRememberedState<T>(key: string, initial: T) {
  const storageKey = `locomotive:${key}`;
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(value));
    } catch {
      /* storage unavailable: keep it in memory only */
    }
  }, [storageKey, value]);
  return [value, setValue] as const;
}
