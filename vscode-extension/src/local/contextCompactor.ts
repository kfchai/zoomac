/**
 * Context compaction — summarizes older conversation turns when
 * approaching the model's context limit, like Claude Code does.
 *
 * Flow:
 * 1. Estimate total tokens in system prompt + messages
 * 2. If > threshold, split messages into "old" and "recent"
 * 3. Ask the LLM to summarize the old portion
 * 4. Replace old messages with a single summary message
 */

import type {
  ConversationMessage,
  ContentBlock,
  LLMProvider,
  LLMResponse,
} from "./providers/types";

/** Rough token estimate: ~4 chars per token for English text */
function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}

/** Estimate tokens for a single message */
function messageTokens(msg: ConversationMessage): number {
  if (typeof msg.content === "string") {
    return estimateTokens(msg.content) + 4; // role overhead
  }
  let total = 4;
  for (const block of msg.content) {
    if (block.type === "text" && block.text) {
      total += estimateTokens(block.text);
    } else if (block.type === "tool_use") {
      total += estimateTokens(block.name || "") + estimateTokens(JSON.stringify(block.input || {}));
    } else if (block.type === "tool_result") {
      total += estimateTokens(block.content || "");
    }
  }
  return total;
}

/** Estimate total tokens for all messages */
export function estimateTotalTokens(
  systemPrompt: string,
  messages: ConversationMessage[]
): number {
  let total = estimateTokens(systemPrompt);
  for (const msg of messages) {
    total += messageTokens(msg);
  }
  return total;
}

/** Extract a text summary of a message for the compaction prompt */
function summarizeMessage(msg: ConversationMessage): string {
  if (typeof msg.content === "string") {
    return `[${msg.role}]: ${msg.content.substring(0, 300)}`;
  }

  const parts: string[] = [];
  for (const block of msg.content) {
    if (block.type === "text" && block.text) {
      parts.push(block.text.substring(0, 200));
    } else if (block.type === "tool_use") {
      const inputSummary = JSON.stringify(block.input || {}).substring(0, 100);
      parts.push(`[tool: ${block.name}(${inputSummary})]`);
    } else if (block.type === "tool_result") {
      const resultPreview = (block.content || "").substring(0, 100);
      parts.push(`[result: ${resultPreview}]`);
    }
  }
  return `[${msg.role}]: ${parts.join(" ")}`;
}

export interface CompactionResult {
  /** New message array after compaction */
  messages: ConversationMessage[];
  /** How many messages were compacted */
  compactedCount: number;
  /** Estimated tokens saved */
  tokensSaved: number;
}

/**
 * Compact conversation context when it gets too large.
 *
 * @param provider - LLM provider to generate the summary
 * @param model - model name
 * @param systemPrompt - current system prompt
 * @param messages - full conversation history
 * @param maxContextTokens - model's context limit (e.g., 200000 for Claude)
 * @param threshold - fraction of limit to trigger compaction (default 0.7)
 * @param keepRecent - number of recent message pairs to keep intact (default 6)
 */
export async function compactContext(
  provider: LLMProvider,
  model: string,
  systemPrompt: string,
  messages: ConversationMessage[],
  maxContextTokens: number,
  threshold = 0.7,
  keepRecent = 6
): Promise<CompactionResult | null> {
  const totalTokens = estimateTotalTokens(systemPrompt, messages);
  const limit = Math.floor(maxContextTokens * threshold);

  if (totalTokens < limit) {
    return null; // No compaction needed
  }

  // Don't compact if too few messages
  if (messages.length <= keepRecent * 2) {
    return null;
  }

  // Split: old messages to summarize, recent to keep
  const splitIndex = messages.length - keepRecent * 2;
  const oldMessages = messages.slice(0, splitIndex);
  const recentMessages = messages.slice(splitIndex);

  // Build a condensed representation of old messages for the summarizer
  const conversationText = oldMessages
    .map(summarizeMessage)
    .join("\n");

  // Ask the LLM to summarize
  const summaryPrompt =
    "Summarize the following conversation history concisely. " +
    "Focus on: decisions made, files modified, key facts learned, user preferences stated, " +
    "and any important context needed to continue the conversation. " +
    "Be concise but don't lose critical details.\n\n" +
    conversationText;

  const castProvider = provider as {
    createMessageWithModel?: (
      model: string,
      system: string,
      messages: ConversationMessage[],
      tools: never[],
      maxTokens: number
    ) => Promise<LLMResponse>;
    createMessage: (
      system: string,
      messages: ConversationMessage[],
      tools: never[],
      maxTokens: number
    ) => Promise<LLMResponse>;
  };

  let response: LLMResponse;
  try {
    if (castProvider.createMessageWithModel) {
      response = await castProvider.createMessageWithModel(
        model,
        "You are a conversation summarizer. Produce a concise summary.",
        [{ role: "user", content: summaryPrompt }],
        [],
        2048
      );
    } else {
      response = await castProvider.createMessage(
        "You are a conversation summarizer. Produce a concise summary.",
        [{ role: "user", content: summaryPrompt }],
        [],
        2048
      );
    }
  } catch {
    // Summary generation failed — skip compaction this time
    return null;
  }

  // Extract summary text
  const summaryText = response.content
    .filter((b) => b.type === "text" && b.text)
    .map((b) => b.text!)
    .join("\n");

  if (!summaryText) {
    return null;
  }

  // Build compacted message list
  const summaryMessage: ConversationMessage = {
    role: "user",
    content:
      "[Conversation context — summarized from earlier messages]\n\n" +
      summaryText +
      "\n\n[End of summary. Recent conversation continues below.]",
  };

  // The assistant needs to acknowledge the summary to maintain alternation
  const ackMessage: ConversationMessage = {
    role: "assistant",
    content: [{ type: "text", text: "Understood. I have the conversation context. Continuing." }],
  };

  const newMessages = [summaryMessage, ackMessage, ...recentMessages];

  const oldTokens = oldMessages.reduce((sum, m) => sum + messageTokens(m), 0);
  const summaryTokens = messageTokens(summaryMessage) + messageTokens(ackMessage);

  return {
    messages: newMessages,
    compactedCount: oldMessages.length,
    tokensSaved: oldTokens - summaryTokens,
  };
}

/** Model context limits (conservative estimates) */
export function getMaxContextTokens(model: string): number {
  const m = model.toLowerCase();
  if (m.includes("claude-3-5") || m.includes("claude-sonnet-4") || m.includes("claude-opus")) {
    return 200000;
  }
  if (m.includes("claude")) {
    return 100000;
  }
  if (m.includes("gpt-4o")) {
    return 128000;
  }
  if (m.includes("gpt-4-turbo") || m.includes("gpt-4-1")) {
    return 128000;
  }
  if (m.includes("gpt-4")) {
    return 8192;
  }
  if (m.includes("gpt-3.5")) {
    return 16384;
  }
  // Local models — conservative default
  if (m.includes("qwen") || m.includes("deepseek") || m.includes("codestral")) {
    return 32000;
  }
  if (m.includes("llama")) {
    return 8192;
  }
  // Fallback
  return 32000;
}
