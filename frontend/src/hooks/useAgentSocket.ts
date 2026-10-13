import { useCallback, useEffect, useRef, useState } from "react";

const API_ORIGIN = import.meta.env.VITE_API_ORIGIN || "http://127.0.0.1:8000";

export type AgentMessage = {
  status?: string;
  task_id?: number;
  action?: string;
  args?: string;
  message?: string;
  ui?: string;
  final_answer?: string;
};

export type ResultPhase = "idle" | "running" | "approval" | "completed" | "error";

export type LogLine = {
  key: number;
  status: string;
  text: string;
  failed: boolean;
};

function wsOrigin(httpOrigin: string): string {
  const url = new URL(httpOrigin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.origin;
}

function isToolError(payload: AgentMessage): boolean {
  return payload.status === "tool_result" && (payload.message || "").trimStart().startsWith("Error:");
}

function formatLine(payload: AgentMessage): string {
  const status = payload.status || "event";
  const message = payload.message || "";
  const action = payload.action || "";
  if (status === "thinking" || status === "tool_result" || status === "tool_call") {
    return action ? `[${status}] ${message} action=${action}` : `[${status}] ${message}`;
  }
  if (status === "pending_approval") {
    const headline = (payload.args || "").split("\n")[0].trim();
    return `[pending_approval] 需要您授权执行 ${action || "命令"}${headline ? "：" + headline : ""}`;
  }
  return message ? `[${status}] ${message}` : `[${status}]`;
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return body.detail;
    }
  } catch {
    return response.statusText || "请求失败";
  }
  return response.statusText || "请求失败";
}

function loadSessionId(): string {
  const key = "safeagent.session_id";
  const existing = sessionStorage.getItem(key);
  if (existing) {
    return existing;
  }
  const created = crypto.randomUUID();
  sessionStorage.setItem(key, created);
  return created;
}

function openSocket(taskId: number): Promise<WebSocket> {
  const socket = new WebSocket(`${wsOrigin(API_ORIGIN)}/ws/agent?task_id=${taskId}`);
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      socket.close();
      reject(new Error("WebSocket 连接超时"));
    }, 8000);
    socket.addEventListener("open", () => {
      window.clearTimeout(timer);
      resolve(socket);
    }, { once: true });
    socket.addEventListener("error", () => {
      if (socket.readyState === WebSocket.OPEN) {
        return;
      }
      window.clearTimeout(timer);
      reject(new Error("WebSocket 连接失败"));
    }, { once: true });
  });
}

export function useAgentSocket() {
  const [taskId, setTaskId] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [phase, setPhase] = useState<ResultPhase>("idle");
  const [spinning, setSpinning] = useState(false);
  const [finalAnswer, setFinalAnswer] = useState("");
  const [pending, setPending] = useState<AgentMessage | null>(null);
  const [error, setError] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  const taskIdRef = useRef<number | null>(null);
  const sessionRef = useRef<string>(loadSessionId());
  const [sessionId, setSessionId] = useState<string>(sessionRef.current);
  const lineKey = useRef(0);
  const pendingRef = useRef<AgentMessage | null>(null);
  const autoActionsRef = useRef<Set<string>>(new Set());

  const push = useCallback((payload: AgentMessage) => {
    const key = lineKey.current + 1;
    lineKey.current = key;
    setLines((current) => [
      ...current,
      {
        key,
        status: payload.status || "event",
        text: formatLine(payload),
        failed: isToolError(payload),
      },
    ]);
  }, []);

  useEffect(() => {
    return () => {
      socketRef.current?.close();
    };
  }, []);

  const start = useCallback(async (prompt: string) => {
    const text = prompt.trim();
    if (!text || socketRef.current) {
      return;
    }
    const id = Date.now();
    setError("");
    setPending(null);
    setLines([]);
    setPhase("running");
    setSpinning(true);
    setFinalAnswer("");
    lineKey.current = 0;
    setTaskId(id);
    taskIdRef.current = id;
    autoActionsRef.current = new Set();
    setRunning(true);

    let socket: WebSocket;
    try {
      socket = await openSocket(id);
    } catch (err) {
      setRunning(false);
      setSpinning(false);
      setPhase("error");
      setError(err instanceof Error ? err.message : "WebSocket 连接失败");
      return;
    }
    socketRef.current = socket;
    socket.onmessage = (event) => {
      let payload: AgentMessage;
      try {
        payload = JSON.parse(String(event.data)) as AgentMessage;
      } catch {
        push({ status: "event", message: String(event.data) });
        return;
      }
      push(payload);
      if (payload.status === "thinking" || payload.status === "tool_call" || payload.status === "tool_result") {
        setSpinning(true);
        setPhase((current) => (current === "approval" || current === "completed" || current === "error" ? current : "running"));
      }
      if (payload.status === "pending_approval") {
        const action = payload.action || "";
        setSpinning(true);
        if (action && autoActionsRef.current.has(action)) {
          void approveCurrent(true, false, payload);
          return;
        }
        pendingRef.current = payload;
        setPending(payload);
        setPhase("approval");
        return;
      }
      if (payload.status === "completed") {
        setSpinning(false);
        setPending(null);
        pendingRef.current = null;
        setPhase("completed");
        setFinalAnswer(payload.final_answer || "");
        setRunning(false);
        socket.close();
        return;
      }
      if (payload.status === "error") {
        setSpinning(false);
        setPending(null);
        pendingRef.current = null;
        setPhase("error");
        setRunning(false);
        socket.close();
      }
    };
    socket.onclose = () => {
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      setRunning(false);
    };

    try {
      const response = await fetch(`${API_ORIGIN}/api/tasks/${id}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: text, session_id: sessionRef.current }),
      });
      if (response.status === 409) {
        setError("任务已在运行");
        setPhase("error");
        setSpinning(false);
        socket.close();
        setRunning(false);
        return;
      }
      if (!response.ok) {
        setError(await readDetail(response));
        setPhase("error");
        setSpinning(false);
        socket.close();
        setRunning(false);
        return;
      }
      push({ status: "started", message: `任务 ${id} 已启动` });
      try {
        const started = (await response.json()) as { session_id?: unknown };
        if (typeof started.session_id === "string" && started.session_id) {
          sessionRef.current = started.session_id;
          setSessionId(started.session_id);
          sessionStorage.setItem("safeagent.session_id", started.session_id);
        }
      } catch {
        // The task is already running. A missing session id keeps the local one.
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动任务失败");
      setPhase("error");
      setSpinning(false);
      socket.close();
      setRunning(false);
    }
  }, [push]);

  const approveCurrent = useCallback(async (
    approved: boolean,
    rememberSimilar: boolean,
    source: AgentMessage | null,
    keepSandbox = false,
  ) => {
    const id = taskIdRef.current;
    const current = source || pendingRef.current;
    if (id === null || current === null) {
      return;
    }
    const action = current.action || "";
    setDeciding(true);
    setError("");
    try {
      const response = await fetch(`${API_ORIGIN}/api/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: id, approved, keep_sandbox: keepSandbox }),
      });
      if (response.status === 409 || response.status === 404) {
        setError("当前没有等待中的审批");
        pendingRef.current = current;
        setPending(current);
        return;
      }
      if (!response.ok) {
        setError(await readDetail(response));
        pendingRef.current = current;
        setPending(current);
        return;
      }
      if (approved && rememberSimilar && action) {
        autoActionsRef.current.add(action);
      }
      if (source && action && autoActionsRef.current.has(action)) {
        const headline = (current.args || "").split("\n")[0].trim();
        push({
          status: "approved",
          action,
          message: `已自动批准同类操作 ${action}${headline ? "：" + headline : ""}`,
        });
      }
      if (pendingRef.current === current) {
        pendingRef.current = null;
        setPending(null);
        setPhase("running");
        setSpinning(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "审批请求失败");
      pendingRef.current = current;
      setPending(current);
    } finally {
      setDeciding(false);
    }
  }, [push]);

  const decide = useCallback((approved: boolean, rememberSimilar = false, keepSandbox = false) => {
    return approveCurrent(approved, rememberSimilar, null, keepSandbox);
  }, [approveCurrent]);

  return { taskId, sessionId, running, deciding, spinning, lines, phase, finalAnswer, pending, error, start, decide };
}
