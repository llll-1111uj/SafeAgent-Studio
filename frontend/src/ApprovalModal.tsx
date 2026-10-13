import { useEffect, useState } from "react";
import { Button, Checkbox, Modal } from "antd";
import type { AgentMessage } from "./hooks/useAgentSocket";

type ApprovalModalProps = {
  pending: AgentMessage | null;
  deciding: boolean;
  onDecide: (approved: boolean, rememberSimilar: boolean) => void;
};

export default function ApprovalModal({ pending, deciding, onDecide }: ApprovalModalProps) {
  const [remember, setRemember] = useState(false);
  const action = pending?.action || "命令";
  const headline = (pending?.args || "").split("\n")[0].trim();

  useEffect(() => {
    setRemember(false);
  }, [pending]);

  return (
    <Modal
      title="需要您授权"
      open={pending !== null}
      closable={false}
      mask={{ closable: false }}
      keyboard={false}
      footer={[
        <Button key="reject" danger loading={deciding} onClick={() => onDecide(false, false)}>
          拒绝
        </Button>,
        <Button key="approve" type="primary" loading={deciding} onClick={() => onDecide(true, remember)}>
          同意
        </Button>,
      ]}
    >
      <p className="mb-2">
        需要您授权执行 {action}
        {headline ? `：${headline}` : ""}
      </p>
      {pending?.args ? (
        <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-100 p-3 text-sm">
          {pending.args}
        </pre>
      ) : null}
      <Checkbox
        className="mt-3"
        checked={remember}
        onChange={(event) => setRemember(event.target.checked)}
      >
        对于本次任务，后续同类操作自动批准
      </Checkbox>
      <p className="mb-0 mt-3 text-slate-500">后端已阻塞，必须调用 /api/approve 才能继续。</p>
    </Modal>
  );
}
