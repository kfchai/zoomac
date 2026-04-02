/**
 * Auto-context: gather project structure and config on first message.
 * Injected into the system prompt so the LLM understands the project
 * without needing to run tools first.
 */

import * as fs from "fs";
import * as path from "path";
import { exec } from "child_process";

/** Config files to auto-read (checked in order, first found wins per category) */
const CONFIG_FILES: Record<string, string[]> = {
  package: ["package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json"],
  lock: ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Pipfile.lock", "Cargo.lock", "go.sum"],
  config: ["tsconfig.json", "vite.config.ts", "next.config.js", "next.config.ts", "webpack.config.js", ".eslintrc.json", "turbo.json"],
};

/** Directories to skip when scanning */
const SKIP_DIRS = new Set([
  "node_modules", ".git", ".next", ".turbo", "__pycache__", ".venv",
  "venv", "dist", "build", "out", ".cache", "coverage", ".zoomac",
  ".claude", "target", "vendor",
]);

export interface ProjectContext {
  tree: string;
  configs: Record<string, string>;
  gitLog: string;
  gitStatus: string;
  language: string;
}

/**
 * Gather project context — called once on first session message.
 * Returns a compact string to inject into the system prompt.
 */
export async function gatherProjectContext(workspaceRoot: string): Promise<string> {
  const sections: string[] = [];

  // 1. Directory tree (top 2 levels)
  const tree = buildDirectoryTree(workspaceRoot, 2);
  if (tree) {
    sections.push("## Project Structure\n```\n" + tree + "\n```");
  }

  // 2. Key config files (first 50 lines each)
  const configs = readConfigFiles(workspaceRoot);
  if (Object.keys(configs).length > 0) {
    for (const [name, content] of Object.entries(configs)) {
      const lines = content.split("\n");
      const preview = lines.slice(0, 50).join("\n");
      sections.push(`## ${name}\n\`\`\`\n${preview}\n\`\`\``);
    }
  }

  // 3. Git context
  const [gitLog, gitStatus] = await Promise.all([
    runCommand("git log --oneline -10", workspaceRoot),
    runCommand("git status --short", workspaceRoot),
  ]);

  if (gitLog) {
    sections.push("## Recent Commits\n```\n" + gitLog.trim() + "\n```");
  }
  if (gitStatus) {
    const statusLines = gitStatus.trim().split("\n");
    const preview = statusLines.slice(0, 20).join("\n");
    sections.push("## Git Status\n```\n" + preview +
      (statusLines.length > 20 ? `\n... (${statusLines.length - 20} more)` : "") +
      "\n```");
  }

  if (sections.length === 0) return "";

  return "\n\n## Auto-detected Project Context\n\n" +
    "The following was gathered automatically. Use it to understand the project.\n\n" +
    sections.join("\n\n");
}

/** Build a directory tree string, limited to `maxDepth` levels. */
function buildDirectoryTree(dir: string, maxDepth: number, prefix = "", depth = 0): string {
  if (depth > maxDepth) return "";

  let entries: string[];
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return "";
  }

  // Sort: directories first, then files
  const items = entries
    .filter((name) => !name.startsWith(".") || name === ".env.example" || name === ".gitignore")
    .filter((name) => !SKIP_DIRS.has(name))
    .map((name) => {
      const fullPath = path.join(dir, name);
      let isDir = false;
      try { isDir = fs.statSync(fullPath).isDirectory(); } catch { return null; }
      return { name, isDir, fullPath };
    })
    .filter(Boolean) as { name: string; isDir: boolean; fullPath: string }[];

  items.sort((a, b) => {
    if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  // Limit items shown per level
  const maxItems = depth === 0 ? 30 : 15;
  const shown = items.slice(0, maxItems);
  const hidden = items.length - shown.length;

  const lines: string[] = [];
  for (let i = 0; i < shown.length; i++) {
    const item = shown[i];
    const isLast = i === shown.length - 1 && hidden === 0;
    const connector = isLast ? "└── " : "├── ";
    const childPrefix = isLast ? "    " : "│   ";

    if (item.isDir) {
      lines.push(prefix + connector + item.name + "/");
      if (depth < maxDepth) {
        const subtree = buildDirectoryTree(item.fullPath, maxDepth, prefix + childPrefix, depth + 1);
        if (subtree) lines.push(subtree);
      }
    } else {
      lines.push(prefix + connector + item.name);
    }
  }

  if (hidden > 0) {
    lines.push(prefix + "└── ... (" + hidden + " more)");
  }

  return lines.join("\n");
}

/** Read key config files from the workspace. */
function readConfigFiles(workspaceRoot: string): Record<string, string> {
  const result: Record<string, string> = {};

  // Always try package manifest
  for (const name of CONFIG_FILES.package) {
    const fullPath = path.join(workspaceRoot, name);
    if (fs.existsSync(fullPath)) {
      try {
        const content = fs.readFileSync(fullPath, "utf-8");
        // Skip lock files (too large)
        if (content.length < 10000) {
          result[name] = content;
        }
      } catch {}
      break; // Only read the first matching manifest
    }
  }

  // Try main config files (tsconfig, vite, etc.)
  for (const name of CONFIG_FILES.config) {
    const fullPath = path.join(workspaceRoot, name);
    if (fs.existsSync(fullPath)) {
      try {
        const content = fs.readFileSync(fullPath, "utf-8");
        if (content.length < 5000) {
          result[name] = content;
        }
      } catch {}
    }
  }

  return result;
}

/** Run a shell command and return stdout. */
function runCommand(cmd: string, cwd: string): Promise<string> {
  return new Promise((resolve) => {
    exec(cmd, { cwd, timeout: 5000, maxBuffer: 100000 }, (error, stdout) => {
      resolve(error ? "" : stdout);
    });
  });
}
