import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Typography } from "antd";
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const { taskId, running, deciding, lines, pending, error, start, decide } = useAgentSocket();

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [lines]);

  const headline = (pending?.args || "").split("\n")[0].trim();

  return (
    <main className="mx-auto flex h-svh max-w-5xl flex-col gap-4 overflow-hidden px-4 py-6">
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
          autoSize={{ minRows: 3, maxRows: 8 }}
          disabled={running}
        />
        <div className="flex items-center gap-3">
          <Button
            type="primary"
            loading={running}
            disabled={!prompt.trim()}
            onClick={() => {
              void start(prompt);
            }}
          >
            启动任务
          </Button>
          {taskId !== null ? (
            <Typography.Text type="secondary">任务 {taskId}</Typography.Text>
          ) : null}
        </div>
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </section>

      <section
        aria-label="终端日志"
        className="min-h-0 flex-1 overflow-y-auto rounded-lg bg-slate-950 p-4 font-mono text-sm leading-6"
      >
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
        <div ref={bottomRef} />
      </section>

      <Modal
        title="需要您授权"
        open={pending !== null}
        closable={false}
        maskClosable={false}
        keyboard={false}
        footer={[
          <Button key="reject" danger loading={deciding} onClick={() => void decide(false)}>
            拒绝
          </Button>,
          <Button key="approve" type="primary" loading={deciding} onClick={() => void decide(true)}>
            同意
          </Button>,
        ]}
      >
        <p className="mb-2">
          需要您授权执行 {pending?.action || "命令"}
          {headline ? `：${headline}` : ""}
        </p>
        {pending?.args ? (
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-100 p-3 text-sm">
            {pending.args}
          </pre>
        ) : null}
        <p className="mb-0 mt-3 text-slate-500">后端已阻塞，必须调用 /api/approve 才能继续。</p>
      </Modal>
    </main>
  );
}
