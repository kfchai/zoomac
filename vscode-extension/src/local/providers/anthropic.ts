import Anthropic from "@anthropic-ai/sdk";
import type {
  ContentBlock,
  ConversationMessage,
  LLMProvider,
  LLMResponse,
  StreamEvent,
  ToolDefinition,
} from "./types";

export class AnthropicProvider implements LLMProvider {
  private readonly _client: Anthropic;

  constructor(apiKey: string) {
    this._client = new Anthropic({ apiKey });
  }

  async createMessage(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    return this.createMessageWithModel("", system, messages, tools, maxTokens);
  }

  createMessageWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    // Non-streaming fallback
    return this._nonStreaming(model, system, messages, tools, maxTokens);
  }

  async createMessageStream(
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number,
    onEvent: (event: StreamEvent) => void
  ): Promise<LLMResponse> {
    return this.createMessageStreamWithModel(
      "", system, messages, tools, maxTokens, onEvent
    );
  }

  async createMessageStreamWithModel(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number,
    onEvent: (event: StreamEvent) => void,
    thinkingBudget = 4096
  ): Promise<LLMResponse> {
    const anthropicMessages = messages.map((m) => this._toAnthropicMessage(m));

    // Use structured system prompt with cache_control for prompt caching
    const systemBlocks: any[] = [
      {
        type: "text",
        text: system,
        cache_control: { type: "ephemeral" },
      },
    ];

    const params: any = {
      model,
      system: systemBlocks,
      messages: anthropicMessages,
      max_tokens: maxTokens,
      stream: true,
    };

    // Extended thinking — only when budget > 0 and model supports it
    if (thinkingBudget > 0 && (model.includes("sonnet-4") || model.includes("opus-4") || model.includes("claude-4"))) {
      params.thinking = {
        type: "enabled",
        budget_tokens: Math.min(thinkingBudget, maxTokens),
      };
    }

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        name: t.name,
        description: t.description,
        input_schema: t.input_schema as Anthropic.Tool.InputSchema,
      }));
    }

    // Accumulate the full response while streaming
    const contentBlocks: ContentBlock[] = [];
    let currentToolId = "";
    let currentToolName = "";
    let currentToolJson = "";
    let stopReason: "end_turn" | "tool_use" = "end_turn";
    let usage = { inputTokens: 0, outputTokens: 0 };

    const stream = this._client.messages.stream(params);

    stream.on("text", (text: string) => {
      onEvent({ type: "text_delta", text });
    });

    stream.on("contentBlock", (block: Anthropic.ContentBlock) => {
      if (block.type === "text") {
        contentBlocks.push({ type: "text", text: block.text });
      } else if (block.type === "tool_use") {
        contentBlocks.push({
          type: "tool_use",
          id: block.id,
          name: block.name,
          input: block.input as Record<string, unknown>,
        });
      }
    });

    stream.on("inputJson", (partialJson: string) => {
      if (!currentToolId) return;
      onEvent({ type: "tool_use_delta", id: currentToolId, partialJson });
    });

    stream.on("message", (msg: Anthropic.Message) => {
      if (msg.stop_reason === "tool_use") {
        stopReason = "tool_use";
      }
      usage.inputTokens = msg.usage.input_tokens;
      usage.outputTokens = msg.usage.output_tokens;
    });

    stream.on("contentBlock", (block: Anthropic.ContentBlock) => {
      if (block.type === "tool_use") {
        currentToolId = block.id;
        currentToolName = block.name;
        onEvent({ type: "tool_use_start", id: block.id, name: block.name });
      }
    });

    // Wait for stream to finish
    const finalMessage = await stream.finalMessage();

    // Build the final response from the accumulated message
    // Skip thinking blocks and unknown types — only keep text and tool_use
    const finalContent = finalMessage.content
      .filter((block) => block.type === "text" || block.type === "tool_use")
      .map((block): ContentBlock => {
        if (block.type === "text") {
          return { type: "text", text: block.text };
        }
        if (block.type === "tool_use") {
          return {
            type: "tool_use",
            id: block.id,
            name: block.name,
            input: block.input as Record<string, unknown>,
          };
        }
        return { type: "text", text: "" };
      })
      .filter((b) => !(b.type === "text" && !b.text));

    const usageAny = finalMessage.usage as unknown as Record<string, number>;
    const response: LLMResponse = {
      content: finalContent,
      stopReason: finalMessage.stop_reason === "tool_use" ? "tool_use" : "end_turn",
      usage: {
        inputTokens: usageAny.input_tokens || 0,
        outputTokens: usageAny.output_tokens || 0,
        cacheReadTokens: usageAny.cache_read_input_tokens || 0,
        cacheWriteTokens: usageAny.cache_creation_input_tokens || 0,
      },
    };

    onEvent({ type: "done", response });
    return response;
  }

  private async _nonStreaming(
    model: string,
    system: string,
    messages: ConversationMessage[],
    tools: ToolDefinition[],
    maxTokens: number
  ): Promise<LLMResponse> {
    const anthropicMessages = messages.map((m) => this._toAnthropicMessage(m));

    const systemBlocks: any[] = [
      { type: "text", text: system, cache_control: { type: "ephemeral" } },
    ];

    const params: any = {
      model,
      system: systemBlocks,
      messages: anthropicMessages,
      max_tokens: maxTokens,
    };

    if (model.includes("sonnet-4") || model.includes("opus-4") || model.includes("claude-4")) {
      params.thinking = {
        type: "enabled",
        budget_tokens: Math.min(4096, maxTokens),
      };
    }

    if (tools.length > 0) {
      params.tools = tools.map((t) => ({
        name: t.name,
        description: t.description,
        input_schema: t.input_schema as Anthropic.Tool.InputSchema,
      }));
    }

    const response = await this._client.messages.create(params);

    return {
      content: response.content
        .filter((block) => block.type === "text" || block.type === "tool_use")
        .map((block): ContentBlock => {
          if (block.type === "text") {
            return { type: "text", text: block.text };
          }
          if (block.type === "tool_use") {
            return {
              type: "tool_use",
              id: block.id,
              name: block.name,
              input: block.input as Record<string, unknown>,
            };
          }
          return { type: "text", text: "" };
        })
        .filter((b) => !(b.type === "text" && !b.text)),
      stopReason: response.stop_reason === "tool_use" ? "tool_use" : "end_turn",
      usage: {
        inputTokens: (response.usage as any).input_tokens || 0,
        outputTokens: (response.usage as any).output_tokens || 0,
        cacheReadTokens: (response.usage as any).cache_read_input_tokens || 0,
        cacheWriteTokens: (response.usage as any).cache_creation_input_tokens || 0,
      },
    };
  }

  private _toAnthropicMessage(msg: ConversationMessage): Anthropic.MessageParam {
    if (typeof msg.content === "string") {
      return { role: msg.role, content: msg.content };
    }

    const blocks: Anthropic.ContentBlockParam[] = msg.content.map((block) => {
      if (block.type === "text") {
        return { type: "text" as const, text: block.text || "" };
      }
      if (block.type === "tool_use") {
        return {
          type: "tool_use" as const,
          id: block.id!,
          name: block.name!,
          input: block.input || {},
        };
      }
      if (block.type === "tool_result") {
        return {
          type: "tool_result" as const,
          tool_use_id: block.tool_use_id!,
          content: block.content || "",
        };
      }
      // Image blocks — pass through as-is for vision
      if ((block as any).type === "image") {
        return block as any;
      }
      return { type: "text" as const, text: "" };
    });

    return { role: msg.role, content: blocks };
  }
}
