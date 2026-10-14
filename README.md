# SafeAgent Studio

企业级智能体工作台。用户在页面上提交任务，控制面为每个任务启动一只一次性的 Docker 沙箱，模型只通过工具在 `/workspace` 里读写和执行。高风险操作停在审批弹窗里，任务结束后容器销毁，对话和产物留下来给下一轮用。

## 架构设计理念

**无状态计算。** 沙箱不保存计算过程。一个任务对应一个容器，正常结束或失败后立刻销毁。工作区来自 git 归档，不是上一只容器的残留进程。需要调试时，审批弹窗里可以勾选保留沙箱，容器会停掉、卷再留 24 小时。

**有状态记忆。** 计算可以丢掉，结论不能丢。记忆分三层：

- 短期会话：按 `session_id` 记住本轮用户原话和助手最终回复。上下文大约超过 5000 token 时，更早的轮次会被收成一段摘要。这份状态在控制面进程内存里。
- 物理产物：删除容器之前，把 `/workspace` 导出到 `.worktrees/artifacts/{task_id}/`。下一轮带着 `parent_task_id` 创建沙箱时，这些文件会被拷回 `/workspace`。
- 长期检索：任务成功后，把请求、最终回答和新增产物文件名写入本地 Chroma（`.worktrees/chroma_db/`）。新任务启动时取出最相似的 3 条，放进这一次调用的 System Prompt。

**双层安全防线。**

1. 物理隔离。沙箱使用 `--network=none`、`--cap-drop=ALL`、只读根文件系统和 `uid 1000`。工具路径必须落在 `/workspace` 内。实时搜索不进沙箱，由宿主机上的 `web_search` 完成。
2. 人工审批。改文件、安装软件、删除和写重定向会停住，等页面上点同意。只读命令和一部分明确的写入可以自动通过。审批在独立线程里等待，HTTP 接口负责唤醒，超时按拒绝处理。

## 目录结构

```text
main.py                      FastAPI、WebSocket、审批和任务线程
reaper.py                    按截止时间回收过期沙箱
session_memory.py            短期会话记忆
memory_store.py              Chroma 长期记忆
utils/logger.py              控制台和 logs/app.log
agents/core_agent.py         正在运行的工具循环
agents/sandbox_manager.py    沙箱创建、导出、恢复和销毁
agents/docker/Dockerfile     沙箱镜像 safeagent-sandbox:py310
agents/full_agent.py         把规划、子任务、团队和压缩放在一起的参考实现
agents/agent_loop.py         最小循环
agents/todo_planner.py       步骤清单
agents/subagent.py           子任务
agents/skill_loader.py       按需加载技能
agents/context_compact.py    上下文压缩
agents/task_graph.py         持久化任务图
agents/background_tasks.py   后台命令
agents/agent_teams.py        多智能体
agents/team_protocols.py     团队协议
agents/autonomous_agents.py  自主认领
agents/worktree_isolation.py 任务目录隔离
frontend/                    React + Vite 工作台，单独启动
tests/                       沙箱、工具和审批脚本
Dockerfile                   控制面镜像
docker-compose.yml           只编排后端
start.ps1                    拉起后端并启动前端
.env.example                 配置模板
```

## Quick Start

需要 Docker Desktop、Node.js，以及一份可用的模型网关。Windows 上在仓库根目录执行。

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY 和 MODEL_ID
.\start.ps1
```

`start.ps1` 会执行 `docker-compose up -d --build`，再在 `frontend/` 里运行 `npm run dev`。浏览器打开 http://127.0.0.1:3000 。接口在 http://127.0.0.1:8000 ，健康检查是 `GET /health`。

第一次启动如果还没有 `.env`，脚本会从 `.env.example` 复制一份并停下来，等密钥填好后再运行。

控制面容器挂载了仓库目录和 Docker 套接字，这样它才能 `git archive` 播种工作区，并在宿主机上创建沙箱容器。日志写到 `logs/app.log`，同时打在容器标准输出里。

### 不使用 Compose，直接在本机跑后端

```powershell
pip install -r requirements.txt
python main.py
```

另开一个终端：

```powershell
cd frontend
npm install
npm run dev
```

本机直接运行时，后端默认只监听 `127.0.0.1:8000`。

## 配置

| 变量 | 作用 |
| --- | --- |
| `ANTHROPIC_API_KEY` | 模型网关密钥 |
| `ANTHROPIC_BASE_URL` | 网关地址 |
| `MODEL_ID` | 模型名 |
| `SAFEAGENT_HOST` / `SAFEAGENT_PORT` | 控制面监听地址。Compose 里主机会被设成 `0.0.0.0` |
| `VITE_API_ORIGIN` | 前端请求的后端地址，见 `frontend/.env.development` |

`.env`、`logs/`、`__pycache__/`、`.tasks/` 和 `.worktrees/artifacts/` 不进入版本库。
