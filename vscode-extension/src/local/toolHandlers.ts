import * as vscode from "vscode";
import { exec } from "child_process";
import * as path from "path";
import { MemoryBridge } from "./memoryBridge";

export type ToolHandler = (
  input: Record<string, unknown>
) => Promise<string>;

/**
 * Build a map of tool name → handler function.
 * All file paths are resolved relative to workspaceRoot.
 */
export function createToolHandlers(
  workspaceRoot: string,
  memoryBridge?: MemoryBridge
): Record<string, ToolHandler> {
  const handlers: Record<string, ToolHandler> = {
    read: readFile(workspaceRoot),
    write: writeFile(workspaceRoot),
    edit: editFile(workspaceRoot),
    bash: runBash(workspaceRoot),
    glob: globFiles(workspaceRoot),
    grep: grepFiles(workspaceRoot),
  };

  if (memoryBridge) {
    handlers.memory_search = memorySearch(memoryBridge);
    handlers.memory_store = memoryStore(memoryBridge);
    handlers.memory_facts = memoryFacts(memoryBridge);
  }

  return handlers;
}

function resolvePath(workspaceRoot: string, filePath: string): string {
  if (path.isAbsolute(filePath)) {
    return filePath;
  }
  return path.join(workspaceRoot, filePath);
}

// ── Read ──

function readFile(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const filePath = resolvePath(
      workspaceRoot,
      input.file_path as string
    );
    const uri = vscode.Uri.file(filePath);

    try {
      const bytes = await vscode.workspace.fs.readFile(uri);
      const text = Buffer.from(bytes).toString("utf-8");
      const lines = text.split("\n");

      const offset = (input.offset as number) || 0;
      const limit = (input.limit as number) || lines.length;
      const slice = lines.slice(offset, offset + limit);

      const numbered = slice.map(
        (line, i) => `${offset + i + 1}\t${line}`
      );
      return numbered.join("\n");
    } catch (err: unknown) {
      return `Error reading ${filePath}: ${err}`;
    }
  };
}

// ── Write ──

function writeFile(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const filePath = resolvePath(
      workspaceRoot,
      input.file_path as string
    );
    const content = input.content as string;
    const uri = vscode.Uri.file(filePath);

    try {
      // Create parent directories
      const dir = vscode.Uri.file(path.dirname(filePath));
      try {
        await vscode.workspace.fs.stat(dir);
      } catch {
        await vscode.workspace.fs.createDirectory(dir);
      }

      const bytes = Buffer.from(content, "utf-8");
      await vscode.workspace.fs.writeFile(uri, bytes);
      const lineCount = content.split("\n").length;
      return `Wrote ${content.length} bytes (${lineCount} lines) to ${input.file_path}`;
    } catch (err: unknown) {
      return `Error writing ${filePath}: ${err}`;
    }
  };
}

// ── Edit ──

function editFile(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const filePath = resolvePath(
      workspaceRoot,
      input.file_path as string
    );
    const oldString = input.old_string as string;
    const newString = input.new_string as string;
    const uri = vscode.Uri.file(filePath);

    try {
      const bytes = await vscode.workspace.fs.readFile(uri);
      const text = Buffer.from(bytes).toString("utf-8");

      const index = text.indexOf(oldString);
      if (index === -1) {
        return `Error: old_string not found in ${input.file_path}`;
      }

      // Check uniqueness
      const secondIndex = text.indexOf(oldString, index + 1);
      if (secondIndex !== -1) {
        return `Error: old_string is not unique in ${input.file_path} (found at positions ${index} and ${secondIndex}). Provide more context.`;
      }

      const newText = text.replace(oldString, newString);
      await vscode.workspace.fs.writeFile(
        uri,
        Buffer.from(newText, "utf-8")
      );

      const oldLines = oldString.split("\n").length;
      const newLines = newString.split("\n").length;
      const diff = newLines - oldLines;
      const diffStr =
        diff > 0
          ? `(+${diff} lines)`
          : diff < 0
            ? `(${diff} lines)`
            : "(same line count)";

      return `Edited ${input.file_path}: replaced ${oldLines} lines with ${newLines} lines ${diffStr}`;
    } catch (err: unknown) {
      return `Error editing ${filePath}: ${err}`;
    }
  };
}

// ── Bash ──

function runBash(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const command = input.command as string;
    const timeout = (input.timeout as number) || 30000;

    return new Promise<string>((resolve) => {
      exec(
        command,
        {
          cwd: workspaceRoot,
          timeout,
          maxBuffer: 1024 * 1024, // 1MB
          shell: process.platform === "win32" ? "bash" : "/bin/bash",
        },
        (error, stdout, stderr) => {
          let output = "";
          if (stdout) {
            output += stdout;
          }
          if (stderr) {
            output += output ? `\nSTDERR:\n${stderr}` : stderr;
          }
          if (error && error.killed) {
            output += `\n[TIMED OUT after ${timeout}ms]`;
          } else if (error && !stdout && !stderr) {
            output = `Error: ${error.message}`;
          }
          if (!output) {
            output = "(Bash completed with no output)";
          }
          // Truncate very large output
          if (output.length > 10000) {
            output =
              output.substring(0, 10000) +
              `\n... [truncated, ${output.length} total chars]`;
          }
          resolve(output);
        }
      );
    });
  };
}

// ── Glob ──

function globFiles(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const pattern = input.pattern as string;

    try {
      const exclude = "**/node_modules/**";
      const files = await vscode.workspace.findFiles(pattern, exclude, 500);

      if (files.length === 0) {
        return "No files found matching: " + pattern;
      }

      const relativePaths = files.map((f) =>
        path.relative(workspaceRoot, f.fsPath)
      );
      relativePaths.sort();
      return relativePaths.join("\n");
    } catch (err: unknown) {
      return `Error searching: ${err}`;
    }
  };
}

// ── Grep ──

function grepFiles(workspaceRoot: string): ToolHandler {
  return async (input) => {
    const pattern = input.pattern as string;
    const searchPath = input.path
      ? resolvePath(workspaceRoot, input.path as string)
      : workspaceRoot;
    const fileGlob = (input.glob as string) || "";

    // Try ripgrep first
    return new Promise<string>((resolve) => {
      let cmd = `rg -n --no-heading "${pattern.replace(/"/g, '\\"')}"`;
      if (fileGlob) {
        cmd += ` --glob "${fileGlob}"`;
      }
      cmd += ` "${searchPath}"`;

      exec(
        cmd,
        {
          cwd: workspaceRoot,
          timeout: 15000,
          maxBuffer: 1024 * 1024,
        },
        (error, stdout) => {
          if (stdout) {
            let output = stdout;
            if (output.length > 10000) {
              output =
                output.substring(0, 10000) +
                `\n... [truncated, ${output.length} total chars]`;
            }
            resolve(output);
          } else if (error) {
            // rg not found or no matches
            if (error.code === 1) {
              resolve("No matches found.");
            } else {
              resolve(`Grep error: ${error.message}`);
            }
          } else {
            resolve("No matches found.");
          }
        }
      );
    });
  };
}

// ── Memory tools ──

function memorySearch(bridge: MemoryBridge): ToolHandler {
  return async (input) => {
    const query = input.query as string;
    try {
      const results = await bridge.search(query, 10);
      if (results.length === 0) {
        return "No memories stored yet. This is a fresh memory — information will accumulate as we work together.";
      }
      return results
        .map((r: Record<string, unknown>, i: number) => {
          const content = (r as { content?: string }).content || "";
          const score = (r as { score?: number }).score;
          return `[${i + 1}] ${content}${score != null ? ` (score: ${(score as number).toFixed(2)})` : ""}`;
        })
        .join("\n");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("No module") || msg.includes("bridge error")) {
        return "Memory system is not available (memgate not installed). Continuing without long-term memory.";
      }
      return "No memories found. Memory will build up as we work together.";
    }
  };
}

function memoryStore(bridge: MemoryBridge): ToolHandler {
  return async (input) => {
    const content = input.content as string;
    const entities = input.entities as
      | Array<{ name: string; attribute: string; value: string }>
      | undefined;
    const relationships = input.relationships as
      | Array<{ a: string; relation: string; b: string }>
      | undefined;
    try {
      return await bridge.store(content, entities, relationships);
    } catch (err: unknown) {
      return `Memory store error: ${err instanceof Error ? err.message : String(err)}`;
    }
  };
}

function memoryFacts(bridge: MemoryBridge): ToolHandler {
  return async (input) => {
    const entity = input.entity as string;
    try {
      const facts = await bridge.facts(entity);
      if (facts.length === 0) {
        return `No facts stored about '${entity}' yet. Facts accumulate as we work together.`;
      }
      return facts
        .map((f: Record<string, unknown>) => {
          const e = (f as { entity?: string }).entity || "";
          const attr = (f as { attribute?: string }).attribute || "";
          const val = (f as { value?: string }).value || "";
          return `- ${e}.${attr} = ${val}`;
        })
        .join("\n");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("No module") || msg.includes("bridge error")) {
        return "Memory system is not available. Continuing without long-term memory.";
      }
      return `No facts found for '${entity}'.`;
    }
  };
}
