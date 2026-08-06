/** Title bar for the Electron desktop window (Window Controls Overlay).
 *
 * Electron hides the OS chrome and overlays only the native min/max/close
 * buttons (colored from main.js) on the top-right; this strip is everything
 * else — brand on the left, and the whole bar drags the window via
 * `-webkit-app-region: drag`, handled natively by Chromium. No JS bridge of
 * any kind. In a normal browser or on the phone the user agent has no
 * "Electron" and this renders nothing.
 */
import { ChartPie } from "lucide-react";
import { useEffect } from "react";

const IS_ELECTRON = navigator.userAgent.includes("Electron");

export default function TitleBar() {
  useEffect(() => {
    // Lets plain CSS move the sidebar and scroll container down 36px only
    // inside the desktop window.
    document.documentElement.classList.toggle("desktop-shell", IS_ELECTRON);
  }, []);

  if (!IS_ELECTRON) return null;

  return (
    <div
      className="fixed inset-x-0 top-0 z-[60] flex h-9 select-none items-center gap-2 border-b border-line bg-surface px-3"
      // Native drag: Chromium treats this region as the window caption.
      // The overlay buttons Electron draws on the right are excluded
      // automatically. pr leaves room for them on narrow windows.
      style={{ WebkitAppRegion: "drag", paddingRight: 150 } as React.CSSProperties}
    >
      <span className="grid h-5 w-5 place-items-center rounded-md bg-accent text-white">
        <ChartPie size={11} strokeWidth={2.4} aria-hidden />
      </span>
      <span className="text-xs font-medium tracking-wide text-ink-muted">GumbInvest</span>
    </div>
  );
}
