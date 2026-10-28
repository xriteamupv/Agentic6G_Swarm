import { memo, useState } from "react";
import type { Message } from "@langchain/langgraph-sdk";
import { MarkdownText } from "../markdown-text";

/** Reasoning text from LangChain content blocks (v1 `reasoning` or OpenAI `summary`). */
export function getReasoningString(content: Message["content"]): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((block: any) => {
      if (block?.type !== "reasoning") return "";
      if (typeof block.reasoning === "string") return block.reasoning;
      if (Array.isArray(block.summary)) {
        return block.summary.map((s: any) => s?.text ?? "").join("\n\n");
      }
      return "";
    })
    .filter(Boolean)
    .join("\n\n");
}

/** Collapsible "Thinking" panel: open while the model streams, collapsed afterwards. */
export const Thinking = memo(ThinkingImpl);

function ThinkingImpl({
  reasoning,
  latencyS,
  step,
  streaming,
}: {
  reasoning: string;
  latencyS?: number;
  step?: string;
  streaming: boolean;
}) {
  const [open, setOpen] = useState<boolean | null>(null);
  const isOpen = open ?? streaming;
  const title = streaming
    ? "Thinking…"
    : `Thought${latencyS !== undefined ? ` for ${latencyS.toFixed(1)} s` : ""}`;
  return (
    <div className="border-l-2 border-gray-300 pl-3 text-sm text-gray-500">
      <button
        type="button"
        onClick={() => setOpen(!isOpen)}
        className="flex items-center gap-1 font-medium hover:text-gray-700"
      >
        <span>{isOpen ? "▾" : "▸"}</span>
        <span>💭 {title}</span>
        {step && <span className="font-mono text-xs opacity-70">· {step}</span>}
      </button>
      {isOpen && (
        <div className="mt-1 italic">
          <MarkdownText>{reasoning}</MarkdownText>
        </div>
      )}
    </div>
  );
}
