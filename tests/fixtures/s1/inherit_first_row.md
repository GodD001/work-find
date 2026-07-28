<!-- 边界情况 fixture：表格首行就是 ↳，没有任何锚点公司可继承。 -->
<!-- 【人工构造，非真实抓取】：2026-07-28 的 raw_snapshot.md 里表格首行是显式公司（Intel Corporation），
     没有出现"首行即 ↳"这种情况，本文件是为覆盖这一边界情况手工构造的，不代表上游真实内容。
     行内容（角色名/地点/链接/日期）参照 raw_snapshot.md 里的真实字段格式拼装，但组合本身是虚构的。 -->
<!-- 验证点：parser 遇到没有可继承锚点的 ↳ 行必须 raise（铁律3：遇到未知结构不许猜测继续跑），
     不许静默跳过该行，也不许把 company 留空/填 null 后继续正常处理。 -->

| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| ↳ | Software Engineer Intern | New York, NY | <a href="https://example.com/careers/synthetic-first-row?utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| Example Corp | Software Engineer Intern, Backend | New York, NY | <a href="https://example.com/careers/synthetic-second-row?utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
