import { type RefObject, type UIEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const BOTTOM_SLACK_PX = 20;

function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_SLACK_PX;
}

/**
 * Keeps a scrollable element pinned to its bottom while `follow` is on.
 *
 * Follow turns off only when the user scrolls *up* away from the bottom.
 * Content growing never moves scrollTop up, so new output (however much
 * arrives between a pin and its scroll event) cannot turn follow off.
 * Reaching the bottom by hand turns follow back on.
 *
 * `contentKey` should change whenever the content changes (e.g. line count,
 * filter). While `active` is false nothing is pinned automatically.
 */
export function useFollowBottom(ref: RefObject<HTMLElement | null>, contentKey: unknown, active = true) {
  const [follow, setFollowState] = useState(true);
  const followRef = useRef(true);
  const lastTop = useRef(0);

  const setFollow = useCallback((value: boolean) => {
    followRef.current = value;
    setFollowState(value);
  }, []);

  const pin = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastTop.current = el.scrollTop;
  }, [ref]);

  // Pin before paint so the user never sees (and the scroll handler never
  // measures) a frame where new content sits below the fold.
  useLayoutEffect(() => {
    if (active && follow) pin();
  }, [contentKey, active, follow, pin]);

  // Re-pin when the box itself resizes (window resize re-wrapping lines).
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (active && followRef.current) pin();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref, active, pin]);

  const onScroll = useCallback(
    (e: UIEvent<HTMLElement>) => {
      const el = e.currentTarget;
      const top = el.scrollTop;
      const movedUp = top < lastTop.current;
      lastTop.current = top;
      if (atBottom(el)) {
        if (!followRef.current) setFollow(true);
      } else if (movedUp && followRef.current) {
        setFollow(false);
      }
    },
    [setFollow],
  );

  const resume = useCallback(() => {
    pin();
    setFollow(true);
  }, [pin, setFollow]);

  return { follow, onScroll, resume };
}
