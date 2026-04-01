/**
 * TypeScript bridge to MemGate via Python subprocess.
 *
 * Calls the memgate_bridge.py script with JSON args and parses JSON output.
 * Used for auto-retrieve (before each LLM call) and memory tools.
 */

import { spawn } from "child_process";
import * as path from "path";

const BRIDGE_SCRIPT = path.join(__dirname, "..", "..", "scripts", "memgate_bridge.py");
const TIMEOUT_MS = 15000;

export interface MemoryBridgeConfig {
  /** Zoomac project directory (where .memgate.db lives) */
  projectDir: string;
  /** Python executable path (default: "python") */
  pythonPath?: string;
}

export class MemoryBridge {
  private readonly _projectDir: string;
  private readonly _python: string;

  constructor(config: MemoryBridgeConfig) {
    this._projectDir = config.projectDir;
    this._python = config.pythonPath || "python";
  }

  /** Retrieve context for auto-injection into system prompt. */
  async retrieveContext(query: string, maxTokens = 2000): Promise<string> {
    const result = await this._call("retrieve", {
      query,
      max_tokens: maxTokens,
    });
    return result.context || "";
  }

  /** Search memories by semantic similarity. */
  async search(query: string, topK = 10): Promise<Record<string, unknown>[]> {
    const result = await this._call("search", { query, top_k: topK });
    return result.results || [];
  }

  /** Store content into memory. */
  async store(
    content: string,
    entities?: Array<{ name: string; attribute: string; value: string }>,
    relationships?: Array<{ a: string; relation: string; b: string }>
  ): Promise<string> {
    const args: Record<string, unknown> = { content };
    if (entities) args.entities = entities;
    if (relationships) args.relationships = relationships;
    const result = await this._call("store", args);
    return result.stored ? "Memory stored successfully." : `Store failed: ${result.error}`;
  }

  /** Look up facts about an entity. */
  async facts(entity?: string): Promise<Record<string, unknown>[]> {
    const result = await this._call("facts", { entity });
    return result.facts || [];
  }

  /** Get memory system status. */
  async status(): Promise<Record<string, unknown>> {
    const result = await this._call("status", {});
    return result.status || {};
  }

  /** Check if MemGate is available (Python + memgate importable). */
  async isAvailable(): Promise<boolean> {
    try {
      await this._call("status", {});
      return true;
    } catch (err) {
      console.error("[MemoryBridge] isAvailable check failed:", err);
      return false;
    }
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private _call(
    command: string,
    args: Record<string, unknown>
  ): Promise<any> {
    return new Promise((resolve, reject) => {
      // Use spawn with stdin to avoid shell quoting issues with JSON
      const child = spawn(
        this._python,
        [BRIDGE_SCRIPT, this._projectDir, command, "-"],
        { timeout: TIMEOUT_MS, shell: true }
      );

      let stdout = "";
      let stderr = "";

      child.stdout.on("data", (data: Buffer) => { stdout += data.toString(); });
      child.stderr.on("data", (data: Buffer) => { stderr += data.toString(); });

      child.on("error", (err: Error) => {
        reject(new Error(`MemGate bridge error: ${err.message}`));
      });

      child.on("close", (code: number | null) => {
        if (code !== 0) {
          reject(new Error(`MemGate bridge error: ${stderr || `exit code ${code}`}`));
          return;
        }
        try {
          const result = JSON.parse(stdout.trim());
          if (result.error) {
            reject(new Error(`MemGate: ${result.error}`));
          } else {
            resolve(result);
          }
        } catch {
          reject(new Error(`MemGate bridge: invalid JSON output: ${stdout}`));
        }
      });

      // Write JSON args to stdin
      child.stdin.write(JSON.stringify(args));
      child.stdin.end();
    });
  }
}
