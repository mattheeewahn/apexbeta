// APEX desktop app (Windows / macOS).
// A thin window around the live site, so server updates show up without reinstalling.
// Set APEX_URL to point it at another server (e.g. http://localhost:5000 for testing).
const { app, BrowserWindow, Menu, shell } = require('electron');
const path = require('path');

const APP_URL = process.env.APEX_URL || 'https://horizonatlas.org';
const APP_ORIGIN = new URL(APP_URL).origin;

// Google refuses sign-in from browsers that identify as "Electron", so present a normal Chrome UA.
app.userAgentFallback = app.userAgentFallback
  .replace(/\sElectron\/\S+/i, '')
  .replace(/\sapex-desktop\/\S+/i, '');

// Windows needs an app id for desktop notifications to appear.
if (process.platform === 'win32') app.setAppUserModelId('org.horizonatlas.apex');

let win = null;

// Pages that may load inside the app window: our site and Google sign-in.
function isInAppUrl(url) {
  try {
    const u = new URL(url);
    if (u.origin === APP_ORIGIN) return true;
    return u.protocol === 'https:' && /(^|\.)google\.[a-z.]+$/i.test(u.hostname);
  } catch (_) {
    return false;
  }
}

function openExternally(url) {
  if (/^https?:\/\//i.test(url) || /^mailto:/i.test(url)) shell.openExternal(url);
}

const OFFLINE_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>APEX</title>
<style>body{font-family:system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;
height:100vh;margin:0;background:#f8f9fa;color:#212529}button{margin-top:16px;padding:10px 18px;border:0;border-radius:8px;
color:#fff;background:linear-gradient(to right,#0033ff,#00ffbf);font-size:15px;cursor:pointer}</style></head>
<body><h2>Can't reach APEX</h2><p>Check your internet connection and try again.</p>
<button onclick="location.href='${APP_URL}'">Try again</button></body></html>`;

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 360,
    minHeight: 560,
    title: 'APEX',
    backgroundColor: '#f8f9fa',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'icon.png'),
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });

  // New windows (target=_blank, window.open): admin dashboard stays in-app, everything else opens in the browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isInAppUrl(url)) {
      return { action: 'allow', overrideBrowserWindowOptions: { autoHideMenuBar: true, icon: path.join(__dirname, 'icon.png') } };
    }
    openExternally(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (!isInAppUrl(url) && !url.startsWith('data:')) {
      event.preventDefault();
      openExternally(url);
    }
  });

  // Show a friendly page instead of a blank window when offline.
  win.webContents.on('did-fail-load', (_e, code, _desc, _url, isMainFrame) => {
    if (isMainFrame && code !== -3 /* aborted */) {
      win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(OFFLINE_PAGE));
    }
  });

  // Basic right-click menu for text (copy / paste).
  win.webContents.on('context-menu', (_e, params) => {
    const items = [];
    if (params.isEditable) items.push({ role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { type: 'separator' }, { role: 'selectAll' });
    else if (params.selectionText) items.push({ role: 'copy' });
    if (items.length) Menu.buildFromTemplate(items).popup({ window: win });
  });

  win.loadURL(APP_URL);
}

// Only one copy of the app; a second launch focuses the existing window.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    createWindow();
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });
}
