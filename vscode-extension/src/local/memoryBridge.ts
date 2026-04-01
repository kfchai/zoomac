/**
 * Memory bridge with three backends:
 * 1. MemGate daemon (persistent Python process — fast, semantic search)
 * 2. MemGate subprocess (fallback — spawns per call, slow but works)
 * 3. MEMORY.md file (fallback — simple text file, like Claude Code)
 */

import * as vscode from "vscode";
import { ChildProcess, spawn } from "child_process";
import * as path from "path";
import * as fs from "fs";

const BRIDGE_SCRIPT = path.join(__dirname, "..", "..", "scripts", "memgate_bridge.py");
const DAEMON_SCRIPT = path.join(__dirname, "..", "..", "scripts", "memgate_daemon.py");
const TIMEOUT_MS = 15000;

export interface MemoryBridgeConfig {
  projectDir: string;
  pythonPath?: string;
  /** Workspace root (for MEMORY.md fallback) */
  workspaceRoot: string;
}

type MemoryBackend = "daemon" | "subprocess" | "file";

export class MemoryBridge {
  private readonly _projectDir: string;
  private readonly _python: string;
  private readonly _workspaceRoot: string;
  private readonly _memoryMdPath: string;
  private _backend: MemoryBackend = "file";
  private _daemon: ChildProcess | null = null;
  private _daemonReady = false;
  private _pendingRequests = new Map<string, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  private _requestCounter = 0;
  private _daemonBuffer = "";

  constructor(config: MemoryBridgeConfig) {
    this._projectDir = config.projectDir;
    this._python = config.pythonPath || "python";
    this._workspaceRoot = config.workspaceRoot;
    this._memoryMdPath = path.join(config.workspaceRoot, ".zoomac", "MEMORY.md");
  }

  /** Initialize — try daemon, then subprocess, then fall back to MEMORY.md */
  async init(): Promise<MemoryBackend> {
    // Try daemon first
    try {
      await this._startDaemon();
      this._backend = "daemon";
      return "daemon";
    } catch {
      // Daemon failed
    }

    // Try subprocess
    try {
      await this._subprocessCall("status", {});
      this._backend = "subprocess";
      return "subprocess";
    } catch {
      // Subprocess failed
    }

    // Fall back to MEMORY.md
    this._backend = "file";
    this._ensureMemoryMd();
    return "file";
  }

  get backend(): MemoryBackend {
    return this._backend;
  }

  async retrieveContext(query: string, maxTokens = 2000): Promise<string> {
    if (this._backend === "file") {
      return this._fileRetrieve(query);
    }
    try {
      const result = await this._call("retrieve", { query, max_tokens: maxTokens });
      return result.context || "";
    } catch {
      return this._fileRetrieve(query);
    }
  }

  async search(query: string, topK = 10): Promise<Record<string, unknown>[]> {
    if (this._backend === "file") {
      const content = this._readMemoryMd();
      if (!content) return [];
      return [{ content, score: 1.0 }];
    }
    try {
      const result = await this._call("search", { query, top_k: topK });
      return result.results || [];
    } catch {
      const content = this._readMemoryMd();
      return content ? [{ content, score: 1.0 }] : [];
    }
  }

  async store(
    content: string,
    entities?: Array<{ name: string; attribute: string; value: string }>,
    relationships?: Array<{ a: string; relation: string; b: string }>
  ): Promise<string> {
    // Always append to MEMORY.md as well (durable backup)
    this._appendMemoryMd(content, entities);

    if (this._backend === "file") {
      return "Memory stored in MEMORY.md.";
    }
    try {
      const args: Record<string, unknown> = { content };
      if (entities) args.entities = entities;
      if (relationships) args.relationships = relationships;
      const result = await this._call("store", args);
      return result.stored ? "Memory stored." : "Memory stored in MEMORY.md (MemGate store failed).";
    } catch {
      return "Memory stored in MEMORY.md.";
    }
  }

  async facts(entity?: string): Promise<Record<string, unknown>[]> {
    if (this._backend === "file") {
      return this._fileSearchFacts(entity);
    }
    try {
      const result = await this._call("facts", { entity });
      return result.facts || [];
    } catch {
      return this._fileSearchFacts(entity);
    }
  }

  async status(): Promise<Record<string, unknown>> {
    if (this._backend === "file") {
      const content = this._readMemoryMd();
      const lines = content ? content.split("\n").filter(Boolean).length : 0;
      return { backend: "MEMORY.md", entries: lines };
    }
    try {
      const result = await this._call("status", {});
      return { ...result.status, backend: this._backend };
    } catch {
      return { backend: "file", error: "MemGate unavailable" };
    }
  }

  async isAvailable(): Promise<boolean> {
    return true; // Always available — MEMORY.md is the fallback
  }

  /** Shut down the daemon if running */
  dispose() {
    if (this._daemon) {
      this._daemon.kill();
      this._daemon = null;
      this._daemonReady = false;
    }
  }

  // ── MemGate daemon ──

  private _startDaemon(): Promise<void> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error("Daemon startup timeout"));
      }, 10000);

      try {
        this._daemon = spawn(
          this._python,
          [DAEMON_SCRIPT, this._projectDir],
          { shell: true, stdio: ["pipe", "pipe", "pipe"] }
        );

        this._daemon.stdout!.on("data", (data: Buffer) => {
          this._daemonBuffer += data.toString();
          const lines = this._daemonBuffer.split("\n");
          this._daemonBuffer = lines.pop() || "";

          for (const line of lines) {
            if (!line.trim()) continue;
            try {
              const msg = JSON.parse(line);
              if (msg.type === "ready") {
                this._daemonReady = true;
                clearTimeout(timeout);
                resolve();
              } else if (msg.id && this._pendingRequests.has(msg.id)) {
                const req = this._pendingRequests.get(msg.id)!;
                this._pendingRequests.delete(msg.id);
                if (msg.error) {
                  req.reject(new Error(msg.error));
                } else {
                  req.resolve(msg);
                }
              }
            } catch {
              // Ignore non-JSON output
            }
          }
        });

        this._daemon.stderr!.on("data", () => {
          // Ignore stderr (model loading messages etc.)
        });

        this._daemon.on("exit", () => {
          this._daemonReady = false;
          this._daemon = null;
          // Reject all pending requests
          for (const [, req] of this._pendingRequests) {
            req.reject(new Error("Daemon exited"));
          }
          this._pendingRequests.clear();
        });

        this._daemon.on("error", (err) => {
          clearTimeout(timeout);
          reject(err);
        });
      } catch (err) {
        clearTimeout(timeout);
        reject(err);
      }
    });
  }

  private _daemonCall(command: string, args: Record<string, unknown>): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!this._daemon || !this._daemonReady) {
        reject(new Error("Daemon not ready"));
        return;
      }

      const id = "req_" + (++this._requestCounter);
      const timer = setTimeout(() => {
        this._pendingRequests.delete(id);
        reject(new Error("Daemon request timeout"));
      }, TIMEOUT_MS);

      this._pendingRequests.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });

      this._daemon!.stdin!.write(JSON.stringify({ id, command, args }) + "\n");
    });
  }

  // ── Subprocess (per-call) ──

  private _subprocessCall(command: string, args: Record<string, unknown>): Promise<any> {
    return new Promise((resolve, reject) => {
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
          reject(new Error(`Invalid JSON: ${stdout}`));
        }
      });

      child.stdin.write(JSON.stringify(args));
      child.stdin.end();
    });
  }

  // ── Unified call dispatcher ──

  private async _call(command: string, args: Record<string, unknown>): Promise<any> {
    if (this._backend === "daemon" && this._daemonReady) {
      return this._daemonCall(command, args);
    }
    return this._subprocessCall(command, args);
  }

  // ── MEMORY.md file backend ──

  private _ensureMemoryMd(): void {
    const dir = path.dirname(this._memoryMdPath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    if (!fs.existsSync(this._memoryMdPath)) {
      fs.writeFileSync(this._memoryMdPath, "# Zoomac Memory\n\nMemories are stored below.\n\n---\n\n", "utf-8");
    }
  }

  private _readMemoryMd(): string {
    try {
      if (fs.existsSync(this._memoryMdPath)) {
        return fs.readFileSync(this._memoryMdPath, "utf-8");
      }
    } catch {}
    return "";
  }

  private _appendMemoryMd(
    content: string,
    entities?: Array<{ name: string; attribute: string; value: string }>
  ): void {
    this._ensureMemoryMd();
    const timestamp = new Date().toISOString().split("T")[0];
    let entry = `\n### ${timestamp}\n\n${content}\n`;
    if (entities && entities.length > 0) {
      entry += "\n**Facts:**\n";
      for (const e of entities) {
        entry += `- ${e.name}.${e.attribute} = ${e.value}\n`;
      }
    }
    entry += "\n---\n";
    fs.appendFileSync(this._memoryMdPath, entry, "utf-8");
  }

  private _fileRetrieve(query: string): string {
    const content = this._readMemoryMd();
    if (!content) return "";
    // Simple keyword matching — return sections containing query words
    const words = query.toLowerCase().split(/\s+/).filter(w => w.length > 2);
    const sections = content.split("---").filter(Boolean);
    const matches = sections.filter(s => {
      const lower = s.toLowerCase();
      return words.some(w => lower.includes(w));
    });
    if (matches.length === 0) return content.substring(0, 2000); // Return all if no keyword match
    return matches.join("\n---\n").substring(0, 2000);
  }

  private _fileSearchFacts(entity?: string): Record<string, unknown>[] {
    const content = this._readMemoryMd();
    if (!content) return [];
    const facts: Record<string, unknown>[] = [];
    const factPattern = /- (.+?)\.(.+?) = (.+)/g;
    let match;
    while ((match = factPattern.exec(content))) {
      if (!entity || match[1].toLowerCase().includes(entity.toLowerCase())) {
        facts.push({ entity: match[1], attribute: match[2], value: match[3] });
      }
    }
    return facts;
  }
}
