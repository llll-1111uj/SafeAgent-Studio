# SafeAgent Studio 活动图

一次任务从页面提交到容器销毁的顺序。

```mermaid
flowchart TD
  start([提交 Prompt]) --> ws[连接 WebSocket]
  ws --> run[POST /run 并启动线程]
  run --> mem[装入会话记忆]
  mem --> box{创建 Docker 沙箱}
  box -->|失败| err([任务失败])
  box -->|成功| prep[创建 outputs 并检索 3 条长期记忆]
  prep --> model[请求模型]
  model --> tool{返回工具调用?}
  tool -->|否| answer[推送最终回复]
  tool -->|是| gate{高风险操作?}
  gate -->|自动放行| exec[沙箱执行或宿主机搜索]
  gate -->|需要审批| hitl{300 秒内点同意?}
  hitl -->|同意| exec
  hitl -->|拒绝或超时| denied[工具结果为拒绝]
  exec --> fuse{同一工具连续失败 3 次?}
  denied --> fuse
  fuse -->|是| stop([中止：检查路径或参数])
  fuse -->|否| model
  answer --> keep{保留沙箱?}
  keep -->|是| held[停容器，卷保留 24 小时]
  keep -->|否| gone[导出 outputs 并销毁容器]
  held --> save[写回会话；成功则写入 Chroma]
  gone --> save
  save --> end([结束])
```

浏览器负责提交、弹窗和结果卡片。FastAPI 线程跑模型循环。Docker 只在沙箱里执行命令。Chroma 在开始时注入历史，在成功结束时再写一条。
