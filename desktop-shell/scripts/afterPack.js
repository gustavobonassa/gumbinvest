/**
 * Ad-hoc sign the macOS app bundle after packing, before the dmg is built.
 *
 * Without an Apple Developer identity, electron-builder signs nothing — and on
 * Apple Silicon an app bundle without at least an ad-hoc signature is refused
 * outright: Gatekeeper reports it as "damaged", with no user-facing bypass.
 * A coherent ad-hoc signature over the whole bundle downgrades that to the
 * regular unnotarized-app flow, which `xattr -d com.apple.quarantine` (or
 * System Settings → Privacy & Security → Open Anyway) can clear.
 *
 * The PyInstaller server binaries under Resources/ carry their own ad-hoc
 * signatures (PyInstaller signs them itself); `--deep` re-signs the Electron
 * frameworks and helpers, and the outer signature seals the resources.
 */
const { execSync } = require("child_process");
const path = require("path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;
  const appName = `${context.packager.appInfo.productFilename}.app`;
  const appPath = path.join(context.appOutDir, appName);
  console.log(`  • ad-hoc signing ${appName}`);
  execSync(`codesign --force --deep --sign - "${appPath}"`, { stdio: "inherit" });
  execSync(`codesign --verify --verbose=2 "${appPath}"`, { stdio: "inherit" });
};
