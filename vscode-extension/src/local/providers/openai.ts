import OpenAI from "openai";
import type {
  ContentBlock,
  ConversationMessage,
  LLMProvider,
  LLMResponse,
  StreamEvent,
  ToolDefinition,
} from "./types";

/**
 * OpenAI-compatible provider.
 *
 * Works with: OpenAI, Ollama, vLLM, LM Studio, Together, Groq, etc.
 * Just change `baseURL` and optionally `apiKey`.
 *
 * Differences from Anthropic:
 * - `system` is a message with role "system"
 * - Tool results use a dedicated `tool` role with `tool_call_id`
 * - Tool call arguments come as a JSON string, not parsed object
 */
export class OpenAIProvider implements LLMProvider {
  private readonly _client: OpenAI;

  constructor(apiKey?: string, baseUrl?: string) {
    this._client = new OpenAI({
      apiKey: apiKey || "not-needed",
      baseURL: baseUrl,
    });
  }

  async createMessage(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...this._convertMessages(messages),
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model: "", // set by caller
      messages: openaiMessages,
      max_tokens: maxTokens,
    };

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    const response = await this._client.chat.completions.create(params);
    const choice = response.choices[0];

    if (!choice) {
      return { content: [], stopReason: "end_turn" };
    }

    const content: ContentBlock[] = [];

    // Text content
    if (choice.message.content) {
      content.push({ type: "text", text: choice.message.content });
    }

    // Tool calls
    if (choice.message.tool_calls) {
      for (const tc of choice.message.tool_calls) {
        let input: Record<string, unknown> = {};
        try {
          input = JSON.parse(tc.function.arguments);
        } catch {
          input = { raw: tc.function.arguments };
        }
        content.push({
          type: "tool_use",
          id: tc.id,
          name: tc.function.name,
          input,
        });
      }
    }

    const hasToolCalls =
      choice.finish_reason === "tool_calls" ||
      (choice.message.tool_calls && choice.message.tool_calls.length > 0);

    return {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
      usage: response.usage
        ? {
            inputTokens: response.usage.prompt_tokens,
            outputTokens: response.usage.completion_tokens,
          }
        : undefined,
    };
  }

  /** With model injected */
  createMessageWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    return this._createWithModel(model, system, messages, tools, maxTokens);
  }

  private async _createWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...this._convertMessages(messages),
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model,
      messages: openaiMessages,
      max_tokens: maxTokens,
    };

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    const response = await this._client.chat.completions.create(params);
    const choice = response.choices[0];

    if (!choice) {
      return { content: [], stopReason: "end_turn" };
    }

    const content: ContentBlock[] = [];

    if (choice.message.content) {
      content.push({ type: "text", text: choice.message.content });
    }

    if (choice.message.tool_calls) {
      for (const tc of choice.message.tool_calls) {
        let input: Record<string, unknown> = {};
        try {
          input = JSON.parse(tc.function.arguments);
        } catch {
          input = { raw: tc.function.arguments };
        }
        content.push({
          type: "tool_use",
          id: tc.id,
          name: tc.function.name,
          input,
        });
      }
    }

    const hasToolCalls =
      choice.finish_reason === "tool_calls" ||
      (choice.message.tool_calls && choice.message.tool_calls.length > 0);

    return {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
      usage: response.usage
        ? {
            inputTokens: response.usage.prompt_tokens,
            outputTokens: response.usage.completion_tokens,
          }
        : undefined,
    };
  }

  async createMessageStreamWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number,
    onEvent: (event: StreamEvent) => void
  ): Promise<LLMResponse> {
    const openaiMessages: OpenAI.ChatCompletionMessageParam[] = [
      { role: "system", content: system },
      ...this._convertMessages(messages),
    ];

    const params: OpenAI.ChatCompletionCreateParams = {
      model,
      messages: openaiMessages,
      max_tokens: maxTokens,
      stream: true,
    };

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        type: "function" as const,
        function: {
          name: t.name,
          description: t.description,
          parameters: t.input_schema,
        },
      }));
    }

    const stream = await this._client.chat.completions.create(params);

    // Accumulate content
    let textContent = "";
    const toolCalls = new Map<number, { id: string; name: string; args: string }>();

    for await (const chunk of stream as AsyncIterable<OpenAI.ChatCompletionChunk>) {
      const delta = chunk.choices[0]?.delta;
      if (!delta) continue;

      // Text delta
      if (delta.content) {
        textContent += delta.content;
        onEvent({ type: "text_delta", text: delta.content });
      }

      // Tool call deltas
      if (delta.tool_calls) {
        for (const tc of delta.tool_calls) {
          const idx = tc.index;
          if (!toolCalls.has(idx)) {
            toolCalls.set(idx, { id: tc.id || "", name: tc.function?.name || "", args: "" });
            if (tc.id && tc.function?.name) {
              onEvent({ type: "tool_use_start", id: tc.id, name: tc.function.name });
            }
          }
          const entry = toolCalls.get(idx)!;
          if (tc.id) entry.id = tc.id;
          if (tc.function?.name) entry.name = tc.function.name;
          if (tc.function?.arguments) {
            entry.args += tc.function.arguments;
            onEvent({ type: "tool_use_delta", id: entry.id, partialJson: tc.function.arguments });
          }
        }
      }
    }

    // Build final response
    const content: ContentBlock[] = [];
    if (textContent) {
      content.push({ type: "text", text: textContent });
    }
    for (const [, tc] of toolCalls) {
      let input: Record<string, unknown> = {};
      try {
        input = JSON.parse(tc.args);
      } catch {
        input = { raw: tc.args };
      }
      content.push({ type: "tool_use", id: tc.id, name: tc.name, input });
    }

    const hasToolCalls = toolCalls.size > 0;
    const response: LLMResponse = {
      content,
      stopReason: hasToolCalls ? "tool_use" : "end_turn",
    };

    onEvent({ type: "done", response });
    return response;
  }

  /** Convert messages, splitting multi-tool-result blocks and cleaning invalid entries. */
  private _convertMessages(messages: ConversationMessage[]): OpenAI.ChatCompletionMessageParam[] {
    const result: OpenAI.ChatCompletionMessageParam[] = [];
    for (const msg of messages) {
      if (!msg || !msg.content) continue;

      // Skip empty content arrays
      if (Array.isArray(msg.content) && msg.content.length === 0) continue;

      if (typeof msg.content !== "string" && Array.isArray(msg.content)) {
        const toolResults = msg.content.filter((b) => b.type === "tool_result");
        if (toolResults.length > 0) {
          // Split each tool_result into a separate "tool" role message
          for (const tr of toolResults) {
            if (tr.tool_use_id) {
              result.push({
                role: "tool" as const,
                tool_call_id: tr.tool_use_id,
                content: tr.content || "(no output)",
              });
            }
          }
          continue;
        }
      }

      const converted = this._toOpenAIMessage(msg);
      // Skip messages with empty/null content
      if (converted.role === "assistant" && !converted.content && !(converted as any).tool_calls?.length) continue;
      if (converted.role === "user" && !converted.content) continue;

      result.push(converted);
    }
    return result;
  }

  private _toOpenAIMessage(
    msg: ConversationMessage
  ): OpenAI.ChatCompletionMessageParam {
    if (typeof msg.content === "string") {
      return { role: msg.role, content: msg.content };
    }

    // Handle structured content blocks
    // Check if any blocks are tool_result — OpenAI uses a separate `tool` role for those
    const toolResults = msg.content.filter((b) => b.type === "tool_result");
    if (toolResults.length > 0) {
      // OpenAI expects individual tool messages
      // Return the first one; the caller should split into multiple messages
      const tr = toolResults[0];
      return {
        role: "tool" as const,
        tool_call_id: tr.tool_use_id!,
        content: tr.content || "",
      };
    }

    // Assistant message with tool_use blocks
    const toolCalls = msg.content.filter((b) => b.type === "tool_use");
    if (toolCalls.length > 0) {
      const textParts = msg.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");

      return {
        role: "assistant",
        content: textParts || null,
        tool_calls: toolCalls.map((tc) => ({
          id: tc.id!,
          type: "function" as const,
          function: {
            name: tc.name!,
            arguments: JSON.stringify(tc.input || {}),
          },
        })),
      };
    }

    // Plain text
    const text = msg.content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    return { role: msg.role, content: text };
  }
}
