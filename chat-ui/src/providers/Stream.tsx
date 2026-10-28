import React, {
  createContext,
  useContext,
  ReactNode,
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
} from "react";
import { useStream } from "@langchain/langgraph-sdk/react";
import {
  Client,
  type Message,
  type ThreadState,
} from "@langchain/langgraph-sdk";
import {
  uiMessageReducer,
  isUIMessage,
  isRemoveUIMessage,
  type UIMessage,
  type RemoveUIMessage,
} from "@langchain/langgraph-sdk/react-ui";
import { useQueryState } from "nuqs";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { AppLogo } from "@/components/icons/app-logo";
import { APP_NAME } from "@/lib/branding";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ArrowRight } from "lucide-react";
import { PasswordInput } from "@/components/ui/password-input";
import { getApiKey, setApiKey as storeApiKey } from "@/lib/api-key";
import { resolveApiUrl } from "@/lib/resolve-api-url";
import { useThreads } from "./Thread";
import { toast } from "sonner";

export type StateType = { messages: Message[]; ui?: UIMessage[] };

const useTypedStream = useStream<
  StateType,
  {
    UpdateType: {
      messages?: Message[] | Message | string;
      ui?: (UIMessage | RemoveUIMessage)[] | UIMessage | RemoveUIMessage;
      context?: Record<string, unknown>;
    };
    CustomEventType: UIMessage | RemoveUIMessage;
  }
>;

type StreamContextType = ReturnType<typeof useTypedStream> & {
  apiUrl: string;
};
const StreamContext = createContext<StreamContextType | undefined>(undefined);

async function sleep(ms = 4000) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function checkGraphStatus(
  apiUrl: string,
  apiKey: string | null,
  authScheme?: string,
): Promise<boolean> {
  try {
    const headers = new Headers();
    if (apiKey) headers.set("X-Api-Key", apiKey);
    if (authScheme) headers.set("X-Auth-Scheme", authScheme);

    const res = await fetch(`${apiUrl}/info`, {
      headers,
    });

    return res.ok;
  } catch (e) {
    console.error(e);
    return false;
  }
}

/** How often an idle chat looks for messages the swarm added on its own (ms). */
const POLL_MS = 5000;

type ThreadHead = ThreadState<StateType>;

function signature(head: ThreadHead): string {
  const messages = head.values?.messages ?? [];
  const interrupts = (head.tasks ?? []).flatMap((t) =>
    (t.interrupts ?? []).map((i) => i.id ?? ""),
  );
  return [
    messages.length,
    messages.at(-1)?.id ?? "",
    (head.next ?? []).join(","),
    interrupts.join(","),
  ].join("|");
}

/**
 * The thread's current state for useStream, refreshed while no run streams. A mission
 * thread also grows between the operator's messages (robot reports, link losses, laps):
 * without this, those messages only appeared on the next submit, under the previous answer.
 * Only a change of messages or interrupts re-renders the (long) thread.
 */
function usePolledThread(
  client: Client,
  threadId: string | null,
  streaming: React.MutableRefObject<boolean>,
) {
  const [state, setState] = useState<{
    data: ThreadHead[] | undefined;
    error: unknown;
    isLoading: boolean;
  }>({ data: undefined, error: undefined, isLoading: threadId != null });
  const seen = useRef("");
  const current = useRef(threadId);
  useEffect(() => {
    current.current = threadId;
  }, [threadId]);

  const mutate = useCallback(
    async (id?: string | null): Promise<ThreadHead[]> => {
      const tid = id ?? current.current;
      if (!tid) {
        setState({ data: undefined, error: undefined, isLoading: false });
        return [];
      }
      try {
        const head = await client.threads.getState<StateType>(tid);
        const data = head.checkpoint == null ? [] : [head];
        if (tid !== current.current) return data; // switched thread meanwhile
        const sig = `${tid}|${signature(head)}`;
        if (sig !== seen.current) {
          seen.current = sig;
          setState({ data, error: undefined, isLoading: false });
        } else {
          setState((prev) =>
            prev.isLoading ? { ...prev, isLoading: false } : prev,
          );
        }
        return data;
      } catch (error) {
        setState((prev) => ({ ...prev, error, isLoading: false }));
        throw error;
      }
    },
    [client],
  );

  useEffect(() => {
    seen.current = "";
    setState({
      data: undefined,
      error: undefined,
      isLoading: threadId != null,
    });
    if (!threadId) return;
    // One request at a time: the next poll is scheduled when this one has answered, and
    // a slow server is asked less often (never piling requests onto a busy dev server).
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const started = Date.now();
      if (!streaming.current && document.visibilityState === "visible") {
        await mutate(threadId).catch(() => undefined); // retried by the next poll
      }
      if (!stopped) {
        const took = Date.now() - started;
        timer = setTimeout(poll, Math.max(POLL_MS, took * 4));
      }
    };
    mutate(threadId)
      .catch(console.error)
      .finally(() => {
        if (!stopped) timer = setTimeout(poll, POLL_MS);
      });
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [threadId, mutate, streaming]);

  return { ...state, mutate };
}

const StreamSession = ({
  children,
  apiKey,
  apiUrl,
  assistantId,
  authScheme,
}: {
  children: ReactNode;
  apiKey: string | null;
  apiUrl: string;
  assistantId: string;
  authScheme?: string;
}) => {
  const [threadId, setThreadId] = useQueryState("threadId");
  const { getThreads, setThreads } = useThreads();
  const client = useMemo(
    () =>
      new Client({
        apiUrl,
        apiKey: apiKey ?? undefined,
        ...(authScheme && { defaultHeaders: { "X-Auth-Scheme": authScheme } }),
      }),
    [apiUrl, apiKey, authScheme],
  );
  const streaming = useRef(false);
  const thread = usePolledThread(client, threadId ?? null, streaming);
  const streamValue = useTypedStream({
    client,
    apiUrl,
    apiKey: apiKey ?? undefined,
    assistantId,
    ...(authScheme && {
      defaultHeaders: {
        "X-Auth-Scheme": authScheme,
      },
    }),
    threadId: threadId ?? null,
    // Only the thread's current state: with the full history useStream rescans every
    // checkpoint for each message's branch metadata on every render, and a swarm mission
    // thread gains checkpoints with every robot report. Branching is moot anyway: the a6g
    // gateway runs every message on the thread's latest state (LatestCheckpointOnly).
    fetchStateHistory: false,
    // The thread's state, polled while idle (see usePolledThread). After a run the view
    // goes back to it (an onFinish taking the state makes useStream refetch and drop the
    // run's own copy), so later swarm messages show up as they come.
    thread,
    onFinish: (_state: unknown) => undefined,
    // Batch streamed tokens: one render per 100 ms instead of one per token. With the
    // memoised ToolCalls/ToolResult/Thinking this keeps a long mission thread live while a
    // reply streams (it used to freeze for minutes on "Weather alert").
    throttle: 100,
    onCustomEvent: (event, options) => {
      if (isUIMessage(event) || isRemoveUIMessage(event)) {
        options.mutate((prev) => {
          const ui = uiMessageReducer(prev.ui ?? [], event);
          return { ...prev, ui };
        });
      }
    },
    onThreadId: (id) => {
      setThreadId(id);
      // Refetch threads list when thread ID changes.
      // Wait for some seconds before fetching so we're able to get the new thread that was created.
      sleep().then(() => getThreads().then(setThreads).catch(console.error));
    },
  });

  useEffect(() => {
    streaming.current = streamValue.isLoading;
  }, [streamValue.isLoading]);

  useEffect(() => {
    checkGraphStatus(apiUrl, apiKey, authScheme).then((ok) => {
      if (!ok) {
        toast.error("Failed to connect to LangGraph server", {
          description: () => (
            <p>
              Please ensure your graph is running at <code>{apiUrl}</code> and
              your API key is correctly set (if connecting to a deployed graph).
            </p>
          ),
          duration: 10000,
          richColors: true,
          closeButton: true,
        });
      }
    });
  }, [apiKey, apiUrl, authScheme]);

  return (
    <StreamContext.Provider value={{ ...streamValue, apiUrl }}>
      {children}
    </StreamContext.Provider>
  );
};

// Default values for the form
const DEFAULT_API_URL = "http://localhost:2024";
const DEFAULT_ASSISTANT_ID = "agent";
const AGENT_BUILDER_AUTH_SCHEME = "langsmith-api-key";

export const StreamProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  // Get environment variables
  const envApiUrl: string | undefined = process.env.NEXT_PUBLIC_API_URL;
  const envAssistantId: string | undefined =
    process.env.NEXT_PUBLIC_ASSISTANT_ID;
  const envAuthScheme: string | undefined = process.env.NEXT_PUBLIC_AUTH_SCHEME;

  // Use URL params with env var fallbacks
  const [apiUrl, setApiUrl] = useQueryState("apiUrl", {
    defaultValue: envApiUrl || "",
  });
  const [assistantId, setAssistantId] = useQueryState("assistantId", {
    defaultValue: envAssistantId || "",
  });
  const [authScheme, setAuthScheme] = useQueryState("authScheme", {
    defaultValue: envAuthScheme || "",
  });
  const [isAgentBuilder, setIsAgentBuilder] = useState(
    () =>
      (authScheme || envAuthScheme || "").toLowerCase() ===
      AGENT_BUILDER_AUTH_SCHEME,
  );

  const finalApiUrl = resolveApiUrl(apiUrl, envApiUrl);
  const finalAssistantId = assistantId || envAssistantId;
  const finalAuthScheme = authScheme || envAuthScheme || "";

  const apiKey = useMemo(() => getApiKey(finalApiUrl) || "", [finalApiUrl]);

  // Show the form if we: don't have an API URL, or don't have an assistant ID
  if (!finalApiUrl || !finalAssistantId) {
    return (
      <div className="flex min-h-screen w-full items-center justify-center p-4">
        <div className="animate-in fade-in-0 zoom-in-95 bg-background flex max-w-3xl flex-col rounded-lg border shadow-lg">
          <div className="flex flex-col gap-2 border-b p-6">
            <div className="flex flex-col items-start gap-2">
              <AppLogo height={120} />
              <h1 className="text-xl font-semibold tracking-tight">
                {APP_NAME}
              </h1>
            </div>
            <p className="text-muted-foreground">
              Welcome to {APP_NAME}! Before you get started, you need to enter
              the URL of the deployment and the assistant / graph ID.
            </p>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();

              const form = e.target as HTMLFormElement;
              const formData = new FormData(form);
              const apiUrl = formData.get("apiUrl") as string;
              const assistantId = formData.get("assistantId") as string;
              const apiKey = formData.get("apiKey") as string;

              setApiUrl(apiUrl);
              storeApiKey(apiUrl, apiKey);
              setAssistantId(assistantId);
              setAuthScheme(isAgentBuilder ? AGENT_BUILDER_AUTH_SCHEME : "");

              form.reset();
            }}
            className="bg-muted/50 flex flex-col gap-6 p-6"
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="apiUrl">
                Deployment URL<span className="text-rose-500">*</span>
              </Label>
              <p className="text-muted-foreground text-sm">
                This is the URL of your LangGraph deployment. Can be a local, or
                production deployment.
              </p>
              <Input
                id="apiUrl"
                name="apiUrl"
                className="bg-background"
                defaultValue={apiUrl || DEFAULT_API_URL}
                required
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="assistantId">
                Assistant / Graph ID<span className="text-rose-500">*</span>
              </Label>
              <p className="text-muted-foreground text-sm">
                This is the ID of the graph (can be the graph name), or
                assistant to fetch threads from, and invoke when actions are
                taken.
              </p>
              <Input
                id="assistantId"
                name="assistantId"
                className="bg-background"
                defaultValue={assistantId || DEFAULT_ASSISTANT_ID}
                required
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="apiKey">LangSmith API Key</Label>
              <p className="text-muted-foreground text-sm">
                This is <strong>NOT</strong> required if using a local LangGraph
                server. This value is stored in your browser's local storage and
                is only used to authenticate requests sent to your LangGraph
                server.
              </p>
              <PasswordInput
                id="apiKey"
                name="apiKey"
                defaultValue={apiKey ?? ""}
                className="bg-background"
                placeholder="lsv2_pt_..."
              />
            </div>

            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-4">
                <div className="flex flex-col gap-1">
                  <Label htmlFor="agentBuilderEnabled">
                    Built with Agent Builder
                  </Label>
                  <p className="text-muted-foreground text-sm">
                    Enable this for Agent Builder deployments.
                  </p>
                </div>
                <Switch
                  id="agentBuilderEnabled"
                  checked={isAgentBuilder}
                  onCheckedChange={setIsAgentBuilder}
                />
              </div>
            </div>

            <div className="mt-2 flex justify-end">
              <Button
                type="submit"
                size="lg"
              >
                Continue
                <ArrowRight className="size-5" />
              </Button>
            </div>
          </form>
        </div>
      </div>
    );
  }

  return (
    <StreamSession
      apiKey={apiKey}
      apiUrl={finalApiUrl}
      assistantId={finalAssistantId}
      authScheme={finalAuthScheme || undefined}
    >
      {children}
    </StreamSession>
  );
};

// Create a custom hook to use the context
export const useStreamContext = (): StreamContextType => {
  const context = useContext(StreamContext);
  if (context === undefined) {
    throw new Error("useStreamContext must be used within a StreamProvider");
  }
  return context;
};

export default StreamContext;
