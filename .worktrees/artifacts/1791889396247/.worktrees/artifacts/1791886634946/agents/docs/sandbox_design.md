现有实现是「任务做控制面、git worktree 做执行面」：隔离只发生在目录和工作目录上，进程、用户、网络和仓库对象库仍与宿主机共享。改成 Docker 沙箱时，任务板和事件日志保留，执行面换成一任务一容器，默认到期销毁。

现有实现
控制面是 TaskManager，执行面是 WorktreeManager，两者用 task_id 绑定。可观测性靠 EventBus 往 .worktrees/events.jsonl 追加事件。

任务板。 每个任务写成 REPO_ROOT/.tasks/task_{id}.json，字段包括 status（pending / in_progress / completed）、owner、worktree、blockedBy。create 用内存里的 _next_id 发号。bind_worktree 写入 worktree 名，并把 pending 推进到 in_progress。unbind_worktree 只清空任务上的名字。

执行面。 worktree_create 校验名字（1–40 位字母数字及 . _ -），执行 git worktree add -b wt/{name} .worktrees/{name} {base_ref}，把条目写入 .worktrees/index.json（active），再绑定任务。失败只记 worktree.create.failed。worktree_run 在该目录里 subprocess.run(..., shell=True, timeout=300)。worktree_status 在该目录跑 git status。worktree_keep 把索引标成 kept，目录和分支留下。worktree_remove 执行 git worktree remove，可选 --force，可选把任务标 completed 并解绑，索引标 removed。

没有进隔离面的工具。 bash、read_file、write_file、edit_file 仍以进程的 WORKDIR 为根。safe_path 只禁止逃出整个仓库，.tasks、.worktrees、.git 都在这个根里面。

生命周期就是：create → active → keep 或 remove。没有租约，没有回收协程，超时只包住单条命令。

风险点
目录隔离不是安全边界。 worktree 与 Agent 同用户、同文件系统、同网络。worktree_run 可以用绝对路径读写仓库外文件，也能访问宿主机环境变量（含 API Key）。
黑名单挡不住命令。 拦截是子串匹配：rm -rf /、sudo、shutdown、reboot、> /dev/。shell=True 下可以用引号、编码、别的删除命令绕开。当前环境是 Windows，这条名单还是按 Unix 命令写的。
命令超时杀不干净进程树。 timeout=300 结束的是 shell 本身。shell 拉起的子进程会留在宿主机上，没有进程组或 cgroup 回收。
共享同一个 .git。 各 worktree 共用对象库和引用。任务可以改远端、删别人的分支、读 .git/config 里的凭证，也可以把仓库写坏。
宿主机工具绕开车道。 bash / write_file 能直接改 index.json、events.jsonl 和别的任务目录。task_bind_worktree 也不检查 worktree 是否真的存在。
没有资源上限。 CPU、内存、进程数、磁盘、网络都没有配额。并行任务可以打满宿主机。
索引和发号没有锁。 index.json 是读改写，_next_id 只在内存里。两个进程会丢更新、发重复任务号。create 失败时，git 可能已经留下半成品目录，索引里却没有记录。
keep 没有回收期限。 标成 kept 的目录和分支会一直占磁盘。remove 若在 git 成功之后、写索引之前失败，控制面和磁盘会不一致。
Docker 沙箱设计
任务板和 EventBus 保持不动。WorktreeManager 换成 SandboxManager：一条车道对应一个容器，不再对应一个 git worktree。工具名可以先做别名（worktree_* → sandbox_*），避免 Agent 循环一次改完。

索引条目扩展为：


{
  "name": "auth-refactor",
  "task_id": 12,
  "status": "active",
  "container_name": "sa-12-auth-refactor",
  "container_id": "…",
  "image": "safeagent-sandbox:py310",
  "workspace_volume": "sa-ws-12",
  "output_volume": "sa-out-12",
  "created_at": 0,
  "deadline_ts": 0,
  "limits": {"cpus": 1.0, "memory": "512m", "pids": 128}
}
状态：creating → active → kept | removing → removed，异常走 failed 或 timed_out。

容器生命周期
镜像 safeagent-sandbox:py310 预装 git、Python 3.10 和构建所需的系统包。容器以非 root（uid 1000）启动，入口是 sleep infinity，作为车道的常驻进程。根文件系统只读，--restart=no。

动作	行为
create
校验名字和任务存在 → 建 workspace / output 两个 volume → 用 git archive {base_ref} 把代码灌进 workspace（不挂宿主机 .git）→ docker run -d → 写索引 → bind_worktree → 记下 deadline_ts
run
docker exec -w /workspace -u 1000，命令级超时只杀这次 exec 的进程组，容器继续活着
status
在容器内执行 git status；workspace 在首次 run 前由 init 做成独立仓库（git init + 首次提交），与宿主机对象库分开
keep
docker commit 不做。把 workspace volume 留住，容器 stop 后保留，状态改为 kept，取消销毁租约，改成更长的卷保留期（默认 24h）
remove
docker stop -t 10 → docker rm -f → 删除两个 volume → 索引标 removed → 解绑任务。complete_task=true 时才把任务标完成
启动对账
列出标签 safeagent.managed=true 的容器，和索引比对。索引有、容器没有 → 标 failed。容器有、索引没有 → 直接删除
池化放在第二步：先保证「一任务一容器、用完即毁」。池只预热镜像和少量待机容器；容器复用前必须删掉 workspace volume 再建，避免上一个任务的文件留下来。并发上限默认 4，超出则 create 失败并记事件。

create 中途失败时按相反顺序清理：删容器、删 volume、索引标 failed，任务不绑车道。

挂载策略
容器看不到仓库根、.tasks、.worktrees、宿主机家目录和 Docker socket。环境变量使用白名单，不继承宿主机进程环境。

挂载	模式	内容
/workspace
rw，命名卷 sa-ws-{task_id}
仅 git archive 出来的源码树
/outputs
rw，命名卷 sa-out-{task_id}
任务需要带回宿主机的产物
/tmp
tmpfs，rw,noexec,nosuid,size=64m
临时文件
根文件系统
只读
镜像内的工具链
代码灌入在宿主机完成：git archive base_ref | docker run --rm -i -v sa-ws-{id}:/workspace alpine tar -x -C /workspace。这样车道里没有宿主机对象库，也没有 .git/config 里的凭证。base_ref 只允许解析成仓库内已有的 commit，由宿主机 git 解析后再归档。

当前是 Windows 宿主机。workspace 用命名卷，不用 bind mount，避免盘符、权限和路径分隔符问题。事件日志留在宿主机 .worktrees/events.jsonl，不挂进容器，容器内进程改不了审计记录。

bash / read_file / write_file / edit_file 在这一步改为对绑定车道做 docker exec 或从 volume 读写。它们继续写 WORKDIR 的话，容器边界会被同一套工具绕开。

资源限制
每个容器启动参数：


--cpus=1.0
--memory=512m
--memory-swap=512m
--pids-limit=128
--ulimit nofile=1024:1024
--cap-drop=ALL
--security-opt no-new-privileges:true
--read-only
--network=none
--user 1000:1000
--label safeagent.managed=true
--label safeagent.task_id=12
默认 --network=none。需要拉依赖时，单独建网络 sa-egress，只放行指定代理，不开 host 网络。workspace 卷用固定大小的稀疏文件做上限（默认 2GB），避免任务把 Docker 数据盘写满。单条命令输出仍截断到 50KB，stdout 和 stderr 分开返回。

索引里的 limits 记录实际生效值，方便前端以后展示。

任务超时销毁
两级超时，互不替代。

命令级（沿用现在的 300 秒）。 docker exec 包在客户端超时里。超时后向容器内该次命令的进程组发 SIGTERM，10 秒后 SIGKILL。容器和 volume 保留，返回 Error: Timeout (300s)，事件为 sandbox.exec.timeout。

车道级（新增，默认 30 分钟）。 deadline_ts = created_at + TASK_TTL 写入索引。后台 reaper 每 5 秒扫一次 active 且 deadline_ts <= now 的条目（以后可以换成 Redis key 过期）。触发顺序：

发 sandbox.timeout 事件。
docker stop -t 10，然后 docker rm -f。
删除 workspace 与 output 卷。
索引标 timed_out，任务解绑，状态留在 in_progress，不标 completed。
keep 取消这条租约，卷保留期默认 24 小时，到期只删卷和已停止的容器。进程重启后 reaper 根据索引里的 deadline_ts 补杀，不依赖内存定时器。

事件名与现有日志对齐，便于以后接到 WebSocket：sandbox.create.before/after/failed、sandbox.exec.timeout、sandbox.keep、sandbox.remove.before/after/failed、sandbox.timeout、task.completed。



Agent