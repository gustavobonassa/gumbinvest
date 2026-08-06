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
const { app, BrowserWindow, Tray, Menu, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const DEV = process.argv.includes("--dev");
const SURFACE = "#10151c";
const INK = "#e6efe9";
const CANVAS = "#0b1210";
const TITLEBAR_HEIGHT = 36;

// Must match backend/app/desktop/paths.py: the server writes port.txt into
// its data root and this shell reads it — a mismatch means "server not found".
const dataDir =
  process.platform === "darwin"
    ? path.join(os.homedir(), "Library", "Application Support", "GumbInvest")
    : path.join(process.env.LOCALAPPDATA || os.homedir(), "GumbInvest");
const portFile = path.join(dataDir, "port.txt");

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

/** Poll until the server answers — the first run downloads market data and
 *  can take minutes, hence the very patient deadline. */
async function waitForServer(deadlineMs = 300000) {
  const start = Date.now();
  while (Date.now() - start < deadlineMs) {
    const port = currentPort();
    if (await checkHealth(port)) return port;
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

// ---------------------------------------------------------------- window

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: CANVAS,
    show: false,
    autoHideMenuBar: true,
    // Window Controls Overlay: our HTML is the title bar; Windows draws only
    // the min/max/close buttons, in these colors.
    titleBarStyle: "hidden",
    titleBarOverlay: { color: SURFACE, symbolColor: INK, height: TITLEBAR_HEIGHT },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
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
    backgroundColor: CANVAS,
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
    startServer();
    createTray();
    createWindow();

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
