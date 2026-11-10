"use client";

import { useEffect, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Keyboard contract for a modal pane (drawer, side sheet):
 *  - focus moves into the pane when it opens (initial element or first
 *    focusable),
 *  - Tab / Shift+Tab cycle inside the pane,
 *  - Escape calls onClose,
 *  - focus returns to the element that opened the pane when it closes.
 *
 * Both modal surfaces declared role="dialog" aria-modal="true" without
 * any of this (audit 2026-11-10, item 11). Native <dialog> would give
 * it for free but these panes are positioned overlays with scrims the
 * existing layout depends on, so the trap lives here.
 */
export function useFocusTrap(
  active: boolean,
  containerRef: RefObject<HTMLElement | null>,
  onClose: () => void,
  initialRef?: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    if (!active) return;
    const maybe = containerRef.current;
    if (!maybe) return;
    const container: HTMLElement = maybe;
    const opener = document.activeElement as HTMLElement | null;

    const focusables = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );

    const initial = initialRef?.current ?? focusables()[0] ?? container;
    if (initial === container && !container.hasAttribute("tabindex")) {
      container.setAttribute("tabindex", "-1");
    }
    initial.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (current === first || !container.contains(current))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (current === last || !container.contains(current))) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (opener && typeof opener.focus === "function" && document.contains(opener)) {
        opener.focus();
      }
    };
    // onClose identity may change per render; the latest is read via
    // closure on each open, which is the behavior we want.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, containerRef, initialRef]);
}
