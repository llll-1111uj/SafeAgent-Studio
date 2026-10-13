import { useCallback, useEffect, useRef, useState } from "react";

const API_ORIGIN = import.meta.env.VITE_API_ORIGIN || "http://127.0.0.1:8000";
const DONE = new Set(["completed", "error"]);

export type AgentMessage = {
  status?: string;
  task_id?: number;
  action?: string;
  args?: string;
  message?: string;
  ui?: string;
};

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
  const [pending, setPending] = useState<AgentMessage | null>(null);
  const [error, setError] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  const taskIdRef = useRef<number | null>(null);
  const lineKey = useRef(0);

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
    lineKey.current = 0;
    setTaskId(id);
    taskIdRef.current = id;
    setRunning(true);

    let socket: WebSocket;
    try {
      socket = await openSocket(id);
    } catch (err) {
      setRunning(false);
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
      if (payload.status === "pending_approval") {
        setPending(payload);
        return;
      }
      if (payload.status && DONE.has(payload.status)) {
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
        body: JSON.stringify({ prompt: text }),
      });
      if (response.status === 409) {
        setError("任务已在运行");
        socket.close();
        setRunning(false);
        return;
      }
      if (!response.ok) {
        setError(await readDetail(response));
        socket.close();
        setRunning(false);
        return;
      }
      push({ status: "started", message: `任务 ${id} 已启动` });
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动任务失败");
      socket.close();
      setRunning(false);
    }
  }, [push]);

  const decide = useCallback(async (approved: boolean) => {
    const id = taskIdRef.current;
    if (id === null) {
      return;
    }
    setDeciding(true);
    setError("");
    try {
      const response = await fetch(`${API_ORIGIN}/api/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_id: id, approved }),
      });
      if (response.status === 409 || response.status === 404) {
        setError("当前没有等待中的审批");
        return;
      }
      if (!response.ok) {
        setError(await readDetail(response));
        return;
      }
      setPending(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "审批请求失败");
    } finally {
      setDeciding(false);
    }
  }, []);

  return { taskId, running, deciding, lines, pending, error, start, decide };
}
