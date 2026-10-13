import { Typography } from "antd";
import type { ResultPhase } from "./hooks/useAgentSocket";

type ResultCardProps = {
  phase: ResultPhase;
  answer: string;
};

const PLACEHOLDER: Record<Exclude<ResultPhase, "completed">, string> = {
  idle: "启动任务后，模型的最终回复会显示在这里。",
  running: "任务进行中。完成后，最终回复会显示在这里。",
  approval: "正在等待用户授权...",
  error: "任务失败。错误详情在下方的技术日志里。",
};

export default function ResultCard({ phase, answer }: ResultCardProps) {
  const ready = phase === "completed" && answer.trim().length > 0;
  return (
    <section
      aria-label="任务结果"
      className={`flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl bg-white shadow-md ${
        ready ? "border-2 border-emerald-600 ring-4 ring-emerald-200" : "border border-slate-200"
      }`}
    >
      <header className={`px-5 py-3 ${ready ? "bg-emerald-600 text-white" : "bg-slate-800 text-white"}`}>
        <h2 className="m-0 text-base font-semibold tracking-wide">任务结果</h2>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {ready ? (
          <Typography.Paragraph className="!mb-0 whitespace-pre-wrap text-lg leading-8 text-slate-900">
            {answer}
          </Typography.Paragraph>
        ) : (
          <p className="m-0 text-base text-slate-500">
            {phase === "completed" ? "任务已结束，模型没有返回文本。" : PLACEHOLDER[phase]}
          </p>
        )}
      </div>
    </section>
  );
}
