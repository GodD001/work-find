# Open Questions

尚未拍板的产品/设计问题。解决后把结论迁回 CLAUDE.md 对应章节，并从本文件删除该条。

## OQ-1：积压项要不要用最新 profile 重新打分

**现状**：`tests/integration/test_overflow_queue.py` 的参照实现 `run_cycle` 里，`fit_score` 在记录首次写入 `seen_jobs` 时被冻结——积压项（`sent_at is None` 留到下一轮的记录）复用当初写入时的 `fit_score`，不会在后续每次运行时用当前 `config/profile.yaml` 重新打分。

**问题**：如果 `profile.yaml` 里的关键词/权重在积压期间被人改过（比如用户调整了自己的求职偏好），积压项要不要跟着重新打分、重新排队，还是保持首次判断不变？

**触发面评估**：在当前的三级排序键（`first_seen_at` ASC → `fit_score` DESC → `canonical_id` ASC）下，这个问题的实际影响范围很窄：
- `fit_score` 只在**同一个 `first_seen_at` 批次内部**决定顺序，不影响跨批次的先后——积压项永远因为 `first_seen_at` 更早而排在新记录前面，profile 改不改都不会让积压项被新记录插队。
- 积压通常一次运行内就能发完（参见 `test_backlog_drains_across_two_runs_with_no_loss_or_duplication`：150 条、cap=80，两轮清空），很少会出现"同一批积压内部因为打分过期导致排序显著失真"的场景。

**结论（当前）**：先按冻结实现，不阻塞其他工作。等实际发生"用户改过 `profile.yaml` 后积压项排序是否合理"的真实场景（或写 `ranker.py` 时）再决定要不要改成每次重新打分，并把结论写回 CLAUDE.md D-2。
