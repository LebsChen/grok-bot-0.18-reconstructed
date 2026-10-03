import path from "node:path";
import { fileURLToPath } from "node:url";

const thisDir = path.dirname(fileURLToPath(import.meta.url));

export const repoRoot = path.resolve(thisDir, "../..");
export const target = process.env.GROK_BOT_TARGET?.trim() || "darwin-arm64";
if (target !== "darwin-arm64" && target !== "win32-x64") {
  throw new Error(`Unsupported GROK_BOT_TARGET ${target}; expected darwin-arm64 or win32-x64`);
}
export const sourceAppDir = target === "win32-x64"
  ? path.join(repoRoot, ".cache", "source-payloads", "win32-x64", "app")
  : path.join(repoRoot, "src", "app");
export const cacheDir = path.join(repoRoot, ".cache");
export const cachedRuntimeApp = target === "win32-x64"
  ? path.join(cacheDir, "runtime", "win32-x64", "app")
  : path.join(cacheDir, "runtime", "Grok Bot.app");
export const runtimeResourcesDir = app => target === "win32-x64"
  ? path.join(app, "resources")
  : path.join(app, "Contents", "Resources");
export const cachedDmg = path.join(cacheDir, "downloads", "Grok_Bot_0.18.0.dmg");
export const archivedDmg = path.join(repoRoot, "research-archives", "original", "0.18.0", "macos-arm64", "Grok_Bot_0.18.0.dmg");
export const windowsInstaller = {
  archived: path.join(repoRoot, "research-archives", "original", "0.18.0", "windows-x64", "Grok_Bot_0.18.0_Setup.exe"),
  cached: path.join(cacheDir, "downloads", "Grok_Bot_0.18.0_Setup.exe"),
  url: "https://downloads.cursor.com/grokbot/stable/win32-x64/0.18.0/Grok_Bot_0.18.0_Setup.exe",
  sha256: "464079a15ef5fa8b61ccea8fffcc78f63cfcf6df65fb0ad5e725d8b95f7e437e",
};
export const windowsOutputApp = path.join(repoRoot, "dist", "Grok Bot 0.18 Reconstructed-win32-x64");
export const buildDir = path.join(repoRoot, ".build");
export const stagedAppDir = path.join(buildDir, "app");
export const builtAsar = path.join(buildDir, "app.asar");
export const builtAsarUnpacked = `${builtAsar}.unpacked`;
export const fidelityBuildDir = path.join(buildDir, "fidelity");
export const fidelityStagedAppDir = path.join(fidelityBuildDir, "app");
export const fidelityBuiltAsar = path.join(fidelityBuildDir, "app.asar");
export const fidelityBuiltAsarUnpacked = `${fidelityBuiltAsar}.unpacked`;
export const fidelityCandidateManifest = path.join(fidelityBuildDir, "release-candidate.json");
export const fidelityE2ECandidateManifest = path.join(fidelityBuildDir, "e2e-candidate.json");
export const fidelityReleaseEvidenceDir = path.join(fidelityBuildDir, "release-evidence");
export const outputDir = path.join(repoRoot, "dist");
const configuredOutputName = process.env.GROK_BOT_OUTPUT_APP_NAME?.trim();
export const outputApp = path.join(
  outputDir,
  configuredOutputName ? path.basename(configuredOutputName) : "Grok Bot 0.18 Reconstructed.app"
);
export const fidelityOutputApp = path.join(outputDir, "Grok Bot 0.18 Fidelity.app");
export const fidelityOutputAppForAsarHash = asarHash => {
  if (!/^[0-9a-f]{64}$/.test(asarHash)) throw new TypeError("A full lowercase ASAR SHA-256 is required");
  return path.join(outputDir, `Grok Bot 0.18 Fidelity-${asarHash.slice(0, 12)}.app`);
};
export const fidelityInstalledAppForAsarHash = asarHash => path.join("/Applications", path.basename(fidelityOutputAppForAsarHash(asarHash)));
export const recoveredFrontendDir = path.join(repoRoot, "recovered", "frontend");
export const recoveredRendererDir = path.join(recoveredFrontendDir, "app");
export const frontendDir = path.join(repoRoot, "frontend");
export const devOutputApp = path.join(outputDir, "Grok Bot 0.18 Dev.app");
export const devProfileDir = path.join(cacheDir, "dev-profile");

export const upstreamVersion = "0.18.0";
export const reconstructedBundleId = "com.anysphere.sand.reconstructed";
export const reconstructedName = "Grok Bot 0.18 Reconstructed";
export const fidelityBundleId = "com.anysphere.sand.reconstructed.fidelity";
export const fidelityName = "Grok Bot 0.18 Fidelity";
export const dmgUrl = "https://downloads.cursor.com/grokbot/stable/darwin-arm64/0.18.0/Grok_Bot_0.18.0.dmg";
export const dmgSha256 = "a253ccd8aab01e083f9812a0264354c5034d8ba7f0610bbb557e82ae77d203eb";
export const upstreamAsarSha256 = target === "win32-x64"
  ? "38e85c0e5042c0257db7925e1e55709d6d155d90d92fe26ad654127d509766e0"
  : "6665408168466f9cacc6087e917890c17f59d2e2e9c2404a5c4a59ad79c1de58";
