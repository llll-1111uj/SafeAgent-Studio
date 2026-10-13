import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Spin, Typography } from "antd";
import ApprovalModal from "./ApprovalModal";
import ResultCard from "./ResultCard";
import { useAgentSocket } from "./hooks/useAgentSocket";

const TONE: Record<string, string> = {
  thinking: "text-slate-300",
  tool_call: "text-sky-300",
  tool_result: "text-emerald-300",
  pending_approval: "text-amber-300",
  started: "text-slate-200",
  approved: "text-emerald-200",
  rejected: "text-orange-300",
  timeout: "text-orange-300",
  error: "text-red-300",
  completed: "text-emerald-200",
};

export default function App() {
  const [prompt, setPrompt] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const { taskId, running, deciding, spinning, lines, phase, finalAnswer, pending, error, start, decide } = useAgentSocket();

  useEffect(() => {
    const node = logRef.current;
    if (!node) {
      return;
    }
    node.scrollTop = node.scrollHeight;
  }, [lines, finalAnswer]);

  return (
    <main className="mx-auto flex h-svh max-w-5xl flex-col gap-3 overflow-hidden px-4 py-4">
      <header>
        <Typography.Title level={3} className="!mb-1">
          SafeAgent Studio
        </Typography.Title>
        <Typography.Text type="secondary">
          先连上 WebSocket，再启动任务。高风险操作会停在审批弹窗，同意后才会继续。
        </Typography.Text>
      </header>

      <section className="flex flex-col gap-3 rounded-lg bg-white p-4 shadow-sm">
        <Input.TextArea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="请输入你的 Prompt"
          autoSize={{ minRows: 2, maxRows: 4 }}
          disabled={running}
        />
        <div className="flex items-center gap-3">
          <Button
            type="primary"
            disabled={!prompt.trim() || running}
            onClick={() => {
              void start(prompt);
            }}
          >
            启动任务
          </Button>
          {spinning ? <Spin size="small" aria-label="正在处理" /> : null}
          {taskId !== null ? (
            <Typography.Text type="secondary">任务 {taskId}</Typography.Text>
          ) : null}
        </div>
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </section>

      <ResultCard phase={phase} answer={finalAnswer} />

      <section
        aria-label="终端日志"
        className="flex h-32 shrink-0 flex-col overflow-hidden rounded-lg bg-slate-950"
      >
        <p className="m-0 border-b border-slate-800 px-4 py-2 text-xs tracking-wide text-slate-400">
          技术日志
        </p>
        <div ref={logRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-3 font-mono text-sm leading-6">
        {lines.length === 0 ? (
          <p className="m-0 text-slate-500">等待任务输出…</p>
        ) : (
          lines.map((line) => (
            <p
              key={line.key}
              className={`m-0 whitespace-pre-wrap ${line.failed ? "font-medium text-red-400" : TONE[line.status] || "text-slate-100"}`}
            >
              {line.text}
            </p>
          ))
        )}
        </div>
      </section>

      <ApprovalModal
        pending={pending}
        deciding={deciding}
        onDecide={(approved, rememberSimilar, keepSandbox) => void decide(approved, rememberSimilar, keepSandbox)}
      />
    </main>
  );
}
