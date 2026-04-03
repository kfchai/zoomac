/**
 * Native Ollama provider using /api/chat directly.
 * Better tool calling support than the OpenAI-compatible /v1 layer.
 */

import type {
  ContentBlock,
  ConversationMessage,
  LLMProvider,
  LLMResponse,
  ToolDefinition,
} from "./types";

interface OllamaMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_calls?: Array<{
    function: { name: string; arguments: Record<string, unknown> };
  }>;
}

interface OllamaChatResponse {
  model: string;
  message: OllamaMessage;
  done: boolean;
  total_duration?: number;
  eval_count?: number;
  prompt_eval_count?: number;
}

export class OllamaProvider implements LLMProvider {
  private readonly _baseUrl: string;

  constructor(baseUrl?: string) {
    // Strip /v1 if someone passes the OpenAI-compat URL
    const url = baseUrl || "http://localhost:11434";
    this._baseUrl = url.replace(/\/v1\/?$/, "");
  }

  async createMessage(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    return this.createMessageWithModel("", system, messages, tools, maxTokens);
  }

  async createMessageWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const ollamaMessages = this._convertMessages(system, messages);

    const body: Record<string, unknown> = {
      model,
      messages: ollamaMessages,
      stream: false,
      options: {
        num_predict: maxTokens,
      },
    };

    if (tools.length > 0) {
      body.tools = tools.map((t) => ({
        type: "function",
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    const resp = await fetch(`${this._baseUrl}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Ollama error ${resp.status}: ${text || resp.statusText}`);
    }

    const data = (await resp.json()) as OllamaChatResponse;
    const content: ContentBlock[] = [];
    let hasToolCalls = false;

    // Text content
    if (data.message.content) {
      content.push({ type: "text", text: data.message.content });
    }

    // Tool calls
    if (data.message.tool_calls && data.message.tool_calls.length > 0) {
      for (let i = 0; i < data.message.tool_calls.length; i++) {
        const tc = data.message.tool_calls[i];
        content.push({
          type: "tool_use",
          id: `ollama_tc_${Date.now()}_${i}`,
          name: tc.function.name,
          input: tc.function.arguments || {},
        });
      }
      hasToolCalls = true;
    }

    return {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
      usage: {
        inputTokens: data.prompt_eval_count || 0,
        outputTokens: data.eval_count || 0,
      },
    };
  }

  /** Convert our ConversationMessage format to Ollama's message format. */
  private _convertMessages(
    system: string,
    messages: ConversationMessage[]
  ): OllamaMessage[] {
    const result: OllamaMessage[] = [
      { role: "system", content: system },
    ];

    for (const msg of messages) {
      if (!msg) continue;

      if (typeof msg.content === "string") {
        if (msg.content) {
          result.push({ role: msg.role, content: msg.content });
        }
        continue;
      }

      if (!Array.isArray(msg.content) || msg.content.length === 0) continue;

      // Handle tool_result blocks — Ollama uses "tool" role
      const toolResults = msg.content.filter((b) => b.type === "tool_result");
      if (toolResults.length > 0) {
        for (const tr of toolResults) {
          result.push({
            role: "tool",
            content: tr.content || "(no output)",
          });
        }
        continue;
      }

      // Handle assistant messages with tool_use blocks
      const toolCalls = msg.content.filter((b) => b.type === "tool_use");
      if (toolCalls.length > 0) {
        const textParts = msg.content
          .filter((b) => b.type === "text" && b.text)
          .map((b) => b.text!)
          .join("");

        result.push({
          role: "assistant",
          content: textParts || "",
          tool_calls: toolCalls.map((tc) => ({
            function: {
              name: tc.name || "unknown",
              arguments: tc.input || {},
            },
          })),
        });
        continue;
      }

      // Plain text
      const text = msg.content
        .filter((b) => b.type === "text" && b.text)
        .map((b) => b.text!)
        .join("");
      if (text) {
        result.push({ role: msg.role, content: text });
      }
    }

    // Enforce alternation — merge consecutive same-role messages
    const merged: OllamaMessage[] = [];
    for (const msg of result) {
      if (msg.role === "tool" || msg.role === "system") {
        merged.push(msg);
        continue;
      }
      const prev = merged.length > 0 ? merged[merged.length - 1] : null;
      if (prev && prev.role === msg.role && !prev.tool_calls && !msg.tool_calls) {
        prev.content = (prev.content + "\n\n" + msg.content).trim();
      } else {
        merged.push(msg);
      }
    }

    return merged;
  }
}
