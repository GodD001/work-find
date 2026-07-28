<!-- 边界情况 fixture：D-3 第 3 类哈希兜底真实触发的合并场景。 -->
<!-- 来源：raw_snapshot.md，Kudu Dynamics 锚点行 + 其后 2 行 ↳，逐字节验证过。 -->
<!-- 关键事实：后 2 行 ↳（"| ↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |"）
     原始行文本完全逐字节相同——不是抽取时手滑复制粘贴，是上游快照里真实存在的重复。
     见 CLAUDE.md 常见陷阱条目11。 -->

<!-- 断言（供未来写 parser/dedup 测试时参照，本文件本身不含任何代码。对应 CLAUDE.md D-3 两层合并规则，
     已定案——见 D-3「真实反例」）： -->
<!--   1. 3 行原始输入 → parser 解析出 3 条 Job：锚点 1 条（company="Kudu Dynamics"，显式给出），
        ↳ 继承 2 条（company 均正确解析为 "Kudu Dynamics"，不是 null，不是 "↳"）。
        3 条的 role/location/date_posted_raw 都是 "Software Engineer Intern"/"Chantilly, VA"/"May 22"，
        application_url 均为 null，is_closed 均为 true（🔒，且与 application_url 独立判定，见陷阱8）。 -->
<!--   2. dedup 第一层（行哈希）：后 2 行 ↳ 的原始行文本逐字节相同 → 哈希相同、source_repo 相同 →
        得到相同的 canonical_id，合并为 1 条。锚点行原始文本含显式 "Kudu Dynamics"，与 "↳" 文本不同，
        第一层不合并它，暂时保留为独立的 canonical_id。 -->
<!--   3. dedup 第二层（fallback_key）：只对第一层的产出生效。此时剩两组记录——
        [锚点] 和 [已合并的 ↳ 对]，都属于第 3 类（无 URL 无 job ID，满足约束 a），
        fallback_key=(Kudu Dynamics, Software Engineer Intern) 两组相同，
        location（都是 Chantilly, VA）和 date_posted_raw（都是 May 22）也两组相同——
        不满足约束 b 的"不同则不合并"，所以第二层继续合并，三行最终收敛成 **1 条**。
        这不是漏洞：company+role+location+date 四个字段两组全同，唯一差异只是锚点用显式公司名、
        另两行用 ↳ 简写，这只是上游排版选择，不构成"是不同岗位"的证据。 -->
<!--   4. run_manifest.jsonl 必须新增 1 条：
        {"event": "dedup_merged", "canonical_id": "<三行共同收敛到的那个哈希兜底 canonical_id>",
         "source_repo": "vanshb03/Summer2027-Internships", "merged_count": 3, "reason": "hash_fallback"} -->

| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Kudu Dynamics | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
| ↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
| ↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
