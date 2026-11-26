import { type RefObject, type UIEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const BOTTOM_SLACK_PX = 20;

function atBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < BOTTOM_SLACK_PX;
}

/**
 * Keeps a scrollable element pinned to its bottom while `follow` is on.
 *
 * Follow turns off only when the user scrolls *up* (scroll position moving
 * up away from the bottom, or wheel/touch/key input that scrolls up).
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

  const draggingScrollbar = useRef(false);

  const pin = useCallback(() => {
    const el = ref.current;
    if (!el || draggingScrollbar.current) return;
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

  // Treat explicit user input that scrolls up as intent to stop following at
  // once, so a smooth/momentum scroll's first few px (still "at the bottom")
  // can't be undone by the next burst re-pinning.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const stop = () => {
      if (followRef.current && el.scrollTop > 0) setFollow(false);
    };
    let touchY = 0;
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) stop();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "ArrowUp" || e.key === "PageUp" || e.key === "Home") stop();
    };
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? 0;
    };
    const onTouchMove = (e: TouchEvent) => {
      if ((e.touches[0]?.clientY ?? 0) > touchY) stop();
    };
    // A press outside the client area is on a scrollbar: don't fight the drag.
    const onPointerDown = (e: PointerEvent) => {
      if (e.target === el && (e.offsetX >= el.clientWidth || e.offsetY >= el.clientHeight)) {
        draggingScrollbar.current = true;
      }
    };
    const onPointerUp = () => {
      draggingScrollbar.current = false;
    };
    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("keydown", onKeyDown);
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, [ref, setFollow]);

  const onScroll = useCallback(
    (e: UIEvent<HTMLElement>) => {
      const el = e.currentTarget;
      const top = el.scrollTop;
      const movedUp = top < lastTop.current;
      lastTop.current = top;
      if (atBottom(el)) {
        // Only moving down onto the bottom resumes; an upward scroll still
        // within the slack (or content shrinking) must not undo a stop.
        if (!movedUp && !followRef.current) setFollow(true);
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
