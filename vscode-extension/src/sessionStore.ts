import * as vscode from "vscode";

export interface SessionMeta {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}

export interface SessionData {
  meta: SessionMeta;
  messages: unknown[];
}

/**
 * Persists chat sessions to VS Code workspace storage.
 * Each session is stored as a separate key to avoid large single blobs.
 */
export class SessionStore {
  private static readonly INDEX_KEY = "zoomac.sessions.index";
  private static readonly DATA_PREFIX = "zoomac.session.";
  private static readonly LAST_ACTIVE_KEY = "zoomac.sessions.lastActive";

  constructor(private readonly _storage: vscode.Memento) {}

  /** List all sessions, most recent first. */
  listSessions(): SessionMeta[] {
    const index = this._storage.get<SessionMeta[]>(SessionStore.INDEX_KEY, []);
    return index.sort((a, b) => b.updatedAt - a.updatedAt);
  }

  /** Load a session's full data. */
  loadSession(id: string): SessionData | undefined {
    return this._storage.get<SessionData>(SessionStore.DATA_PREFIX + id);
  }

  /** Save a session (creates or updates). */
  async saveSession(data: SessionData): Promise<void> {
    // Save data
    await this._storage.update(SessionStore.DATA_PREFIX + data.meta.id, data);

    // Update index
    const index = this._storage.get<SessionMeta[]>(SessionStore.INDEX_KEY, []);
    const existing = index.findIndex((s) => s.id === data.meta.id);
    if (existing >= 0) {
      index[existing] = data.meta;
    } else {
      index.push(data.meta);
    }
    await this._storage.update(SessionStore.INDEX_KEY, index);
  }

  /** Delete a session. */
  async deleteSession(id: string): Promise<void> {
    await this._storage.update(SessionStore.DATA_PREFIX + id, undefined);
    const index = this._storage.get<SessionMeta[]>(SessionStore.INDEX_KEY, []);
    const filtered = index.filter((s) => s.id !== id);
    await this._storage.update(SessionStore.INDEX_KEY, filtered);
  }

  /** Track the last active session for auto-restore on reload. */
  async setLastActive(id: string): Promise<void> {
    await this._storage.update(SessionStore.LAST_ACTIVE_KEY, id);
  }

  /** Get the last active session ID. */
  getLastActive(): string | undefined {
    return this._storage.get<string>(SessionStore.LAST_ACTIVE_KEY);
  }

  /** Generate a new session ID. */
  static newId(): string {
    return Date.now().toString(36) + Math.random().toString(36).substring(2, 6);
  }
}
