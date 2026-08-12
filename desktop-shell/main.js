/**
 * GumbInvest desktop shell.
 *
 * Electron owns the window (with a Window Controls Overlay title bar — the
 * app's HTML is the title bar, Windows draws only min/max/close in our
 * colors) and the tray. The data lives in the Python server this process
 * spawns: it picks its port, writes it to port.txt in the data dir, and
 * serves API + SPA on 0.0.0.0 — which is why the same app keeps working in
 * any browser and on the phone while the desktop app runs.
 */
const { app, BrowserWindow, Tray, Menu, dialog, shell, ipcMain } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const DEV = process.argv.includes("--dev");
const TITLEBAR_HEIGHT = 36;

/**
 * Window chrome per theme — the native min/max/close buttons Windows draws in
 * our colours, and the paint behind the page before it loads.
 *
 * The page itself is themed by the SPA; these three colours are the only part
 * of the app the stylesheet cannot reach, so the renderer pushes its choice
 * over `theme:set` (see preload.js) and it is remembered in the data dir for
 * the next launch — otherwise a light-theme window would open wearing dark
 * window buttons until the SPA finished booting.
 */
const THEMES = {
  dark: { surface: "#10151c", ink: "#e6efe9", canvas: "#0b1210" },
  light: { surface: "#ffffff", ink: "#171b22", canvas: "#f1f3f7" },
};

// Must match backend/app/desktop/paths.py: the server writes port.txt into
// its data root and this shell reads it — a mismatch means "server not found".
const dataDir =
  process.platform === "darwin"
    ? path.join(os.homedir(), "Library", "Application Support", "GumbInvest")
    : path.join(process.env.LOCALAPPDATA || os.homedir(), "GumbInvest");
const portFile = path.join(dataDir, "port.txt");
const themeFile = path.join(dataDir, "theme.txt");

let mainWindow = null;
let tray = null;
let serverProcess = null;
let quitting = false;

// ---------------------------------------------------------------- server

function serverExePath() {
  const binary = process.platform === "win32" ? "gumbinvest-server.exe" : "gumbinvest-server";
  return path.join(process.resourcesPath, "server", binary);
}

function startServer() {
  if (DEV) return; // dev: run `python -m app.desktop` yourself
  serverProcess = spawn(serverExePath(), [], {
    stdio: "ignore",
    windowsHide: true,
  });
  serverProcess.on("exit", (code) => {
    serverProcess = null;
    // A server that dies while the app is open is fatal — surface it.
    if (!quitting && mainWindow) {
      mainWindow.loadFile("error.html");
    }
  });
}

function stopServer() {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
}

function currentPort() {
  try {
    const value = parseInt(fs.readFileSync(portFile, "utf-8").trim(), 10);
    if (Number.isFinite(value)) return value;
  } catch {
    /* not written yet */
  }
  return 8873;
}

function checkHealth(port) {
  return new Promise((resolve) => {
    const request = http.get(
      { host: "127.0.0.1", port, path: "/api/health", timeout: 2000 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    request.on("error", () => resolve(false));
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
  });
}

/** Poll until the server answers. Normally seconds — the heavy startup work
 *  (downloads, auto-import) runs behind the served app — but the deadline
 *  stays patient as a safety margin for slow disks and antivirus scans. */
async function waitForServer(deadlineMs = 300000) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    const port = currentPort();
    if (await checkHealth(port)) return port;
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

// ---------------------------------------------------------------- updates

/**
 * Update state, mirrored to the renderer.
 *
 * One object rather than a stream of events, because the settings screen can
 * open at any point in the process: it asks for the current state once and
 * then subscribes to changes, instead of trying to reconstruct where things
 * stand from events it wasn't listening for.
 *
 * `status`: idle | checking | available | downloading | downloaded | current | error
 */
let updateState = { status: "idle", version: null, percent: 0, error: null };
let autoUpdater = null;

function setUpdateState(patch) {
  updateState = { ...updateState, ...patch };
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("update:state", updateState);
  }
}

/** Why in-app updating is unavailable here, or null when it works. */
function updaterUnavailableReason() {
  if (!app.isPackaged) return "Atualizações só funcionam no app instalado.";
  // Squirrel.Mac refuses to swap an unsigned bundle, so on macOS the download
  // would succeed and the install would silently do nothing. Better to say so
  // and send the user to the release page than to fake a working button.
  if (process.platform === "darwin") {
    return "No macOS a atualização automática exige app assinado. Baixe a nova versão em github.com/gustavobonassa/gumbinvest/releases.";
  }
  return null;
}

/** Loaded lazily: an unpackaged dev run has no update feed to talk to. */
function getUpdater() {
  if (updaterUnavailableReason()) return null;
  if (autoUpdater) return autoUpdater;

  autoUpdater = require("electron-updater").autoUpdater;
  // The user decides when to spend the bandwidth — checking is silent, the
  // download is a button.
  autoUpdater.autoDownload = false;
  // Installing on quit would surprise someone who just wanted to close the
  // app; the restart is an explicit action in the settings screen.
  autoUpdater.autoInstallOnAppQuit = false;

  autoUpdater.on("update-available", (info) =>
    setUpdateState({ status: "available", version: info.version, percent: 0, error: null }),
  );
  autoUpdater.on("update-not-available", () =>
    setUpdateState({ status: "current", version: app.getVersion(), percent: 0, error: null }),
  );
  autoUpdater.on("download-progress", (progress) =>
    setUpdateState({ status: "downloading", percent: Math.round(progress.percent) }),
  );
  autoUpdater.on("update-downloaded", (info) =>
    setUpdateState({ status: "downloaded", version: info.version, percent: 100 }),
  );
  autoUpdater.on("error", (error) =>
    setUpdateState({ status: "error", error: String(error?.message || error) }),
  );
  return autoUpdater;
}

function registerUpdateIpc() {
  ipcMain.handle("app:version", () => app.getVersion());
  ipcMain.handle("update:state", () => updateState);

  ipcMain.handle("update:check", async () => {
    const updater = getUpdater();
    if (!updater) {
      // A dev run or an unsigned mac reports why rather than pretending to be
      // up to date.
      setUpdateState({ status: "error", error: updaterUnavailableReason() });
      return updateState;
    }
    setUpdateState({ status: "checking", error: null });
    try {
      await updater.checkForUpdates();
    } catch (error) {
      setUpdateState({ status: "error", error: String(error?.message || error) });
    }
    return updateState;
  });

  ipcMain.handle("update:download", async () => {
    const updater = getUpdater();
    if (!updater) return updateState;
    setUpdateState({ status: "downloading", percent: 0, error: null });
    try {
      await updater.downloadUpdate();
    } catch (error) {
      setUpdateState({ status: "error", error: String(error?.message || error) });
    }
    return updateState;
  });

  ipcMain.handle("update:install", () => {
    const updater = getUpdater();
    if (!updater) return updateState;
    // The Python server holds the SQLite file; stop it before the installer
    // replaces the files underneath it.
    quitting = true;
    stopServer();
    // isSilent=false so the NSIS progress is visible; isForceRunAfter=true
    // brings the app back on the new version.
    updater.quitAndInstall(false, true);
    return updateState;
  });
}

/** One quiet check a little after launch, so the badge is already right when
 *  the user wanders into Configurações. Never downloads on its own. */
function scheduleStartupCheck() {
  const updater = getUpdater();
  if (!updater) return;
  setTimeout(() => {
    updater.checkForUpdates().catch(() => {
      /* offline is not worth an error banner on startup */
    });
  }, 15000);
}

// ---------------------------------------------------------------- theme

function storedTheme() {
  try {
    const value = fs.readFileSync(themeFile, "utf8").trim();
    if (value === "light" || value === "dark") return value;
  } catch {
    /* never written, or unreadable: dark is the default */
  }
  return "dark";
}

function applyTheme(theme) {
  const palette = THEMES[theme] || THEMES.dark;
  if (!mainWindow) return;
  mainWindow.setBackgroundColor(palette.canvas);
  try {
    mainWindow.setTitleBarOverlay({
      color: palette.surface,
      symbolColor: palette.ink,
      height: TITLEBAR_HEIGHT,
    });
  } catch {
    // Only Windows (and newer Linux) draws the overlay; elsewhere the OS owns
    // those buttons and there is nothing to recolour.
  }
}

function registerThemeIpc() {
  ipcMain.handle("theme:set", (_event, theme) => {
    if (theme !== "light" && theme !== "dark") return;
    applyTheme(theme);
    try {
      fs.mkdirSync(dataDir, { recursive: true });
      fs.writeFileSync(themeFile, theme, "utf8");
    } catch {
      // Forgetting it only costs a wrongly-coloured title bar for the first
      // second of the next launch; never a reason to fail the call.
    }
  });
}

// ---------------------------------------------------------------- window

function createWindow() {
  const palette = THEMES[storedTheme()];
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: palette.canvas,
    show: false,
    autoHideMenuBar: true,
    // Window Controls Overlay: our HTML is the title bar; Windows draws only
    // the min/max/close buttons, in these colors.
    titleBarStyle: "hidden",
    titleBarOverlay: { color: palette.surface, symbolColor: palette.ink, height: TITLEBAR_HEIGHT },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
    if (!quitting) app.quit();
  });
  mainWindow.loadFile("loading.html");
}

function showWindow() {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
}

// ---------------------------------------------------------------- tray

function lanIp() {
  for (const entries of Object.values(os.networkInterfaces())) {
    for (const entry of entries || []) {
      if (entry.family === "IPv4" && !entry.internal) return entry.address;
    }
  }
  return "127.0.0.1";
}

async function openPhoneQr() {
  const url = `http://${lanIp()}:${currentPort()}`;
  const QRCode = require("qrcode");
  const dataUrl = await QRCode.toDataURL(url, { width: 280, margin: 2 });
  const qrWindow = new BrowserWindow({
    width: 360,
    height: 470,
    resizable: false,
    minimizable: false,
    maximizable: false,
    autoHideMenuBar: true,
    // qr.html is a dark page of its own, like the loading and error screens —
    // shell surfaces, not app surfaces, so they do not follow the app's theme.
    backgroundColor: THEMES.dark.canvas,
    title: "Abrir no celular",
  });
  const query = new URLSearchParams({ qr: dataUrl, url }).toString();
  qrWindow.loadFile("qr.html", { search: query });
}

function createTray() {
  tray = new Tray(path.join(__dirname, "assets", "tray.png"));
  tray.setToolTip("GumbInvest");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Abrir", click: showWindow },
      { label: "Abrir no celular (QR)", click: () => openPhoneQr() },
      { label: "Abrir pasta de dados", click: () => shell.openPath(dataDir) },
      { type: "separator" },
      { label: "Sair", click: () => app.quit() },
    ]),
  );
  tray.on("double-click", showWindow);
}

// ---------------------------------------------------------------- lifecycle

const hasLock = app.requestSingleInstanceLock();
if (!hasLock) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  app.whenReady().then(async () => {
    registerUpdateIpc();
    registerThemeIpc();
    startServer();
    createTray();
    createWindow();
    scheduleStartupCheck();

    const port = await waitForServer();
    if (!mainWindow) return;
    if (port === null) {
      mainWindow.loadFile("error.html");
      return;
    }
    mainWindow.loadURL(`http://127.0.0.1:${port}`);
  });

  app.on("before-quit", () => {
    quitting = true;
    stopServer();
  });

  app.on("window-all-closed", () => app.quit());
}
