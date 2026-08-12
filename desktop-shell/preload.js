/**
 * The only bridge between the SPA and Electron.
 *
 * The window loads the SPA from the local Python server, so the page and the
 * shell are separate worlds: `contextIsolation` keeps Node out of the page and
 * this file hand-picks what crosses over. Most of it is about updates — the
 * version to display, the three actions, and a subscription to the updater's
 * progress — plus the one thing the page's own stylesheet cannot reach: the
 * colour of the native window buttons. A browser or a phone has no
 * `window.gumbinvest`, which is exactly how the UI knows to hide the update
 * card (and how the theme switch knows there is no window to recolour).
 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("gumbinvest", {
  version: () => ipcRenderer.invoke("app:version"),
  /** "dark" | "light" — repaints the window chrome and remembers the choice. */
  setTheme: (theme) => ipcRenderer.invoke("theme:set", theme),
  checkForUpdates: () => ipcRenderer.invoke("update:check"),
  downloadUpdate: () => ipcRenderer.invoke("update:download"),
  /** Quits and runs the installer; the app relaunches on the new version. */
  installUpdate: () => ipcRenderer.invoke("update:install"),
  /** Current updater state, so a freshly opened screen isn't blank. */
  updateState: () => ipcRenderer.invoke("update:state"),
  /** Progress/state pushes. Returns an unsubscribe for React cleanup. */
  onUpdateState: (handler) => {
    const listener = (_event, state) => handler(state);
    ipcRenderer.on("update:state", listener);
    return () => ipcRenderer.removeListener("update:state", listener);
  },
});
