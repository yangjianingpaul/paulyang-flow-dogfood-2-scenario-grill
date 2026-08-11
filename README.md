# paulyang-flow-dogfood-2-scenario-grill
Dogfood for paulyang-flow scenario-first grilling

## 实现 Session 冷启动入口

新 Session 的全部输入就是这一节。逐字执行，不附加任何旧会话摘要。

1. 运行 `frontier`；对它给出的 Ticket 运行 `frontier --claim <N>`
2. `cd` 到 `--claim` 输出的 worktree 路径；此后的每一条命令都在该 worktree 内执行
3. 读 Ticket 正文 → Parent Spec 的 `Technical contract` 与 `Runbook` 两节
   → Ticket 引用的每个 `#N/Sn` 的硬断言原文。不读任何聊天历史
4. TDD 实现，只做本 Ticket 拥有的 S；本地 verify 必须绿。verify 命令的唯一权威是
   **当前 worktree 的 `flow.yml`**，执行当时现场读：
   `sed -n 's/^verify: *//p' flow.yml | head -1`
5. push `ticket/<N>`，开 PR，body 含 `Closes #<N>`；在 PR 上贴人工验收指令
   （worktree 路径、启动命令、要重放的场景前缀、本 Ticket 负责判定的 S），
   然后**停在人工验收之前**
6. 不勾任何 AC、不 merge、不关闭任何 Issue —— 这三件事只由人类做。
   人工验收不通过时，在同一 claim / 分支 / PR 上修复并重新 push
   （见 Parent Spec Runbook 的「验收节奏与失败处置」）