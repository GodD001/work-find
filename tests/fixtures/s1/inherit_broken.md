<!-- 边界情况 fixture：继承链被打断，随后开始新链。 -->
<!-- 来源：raw_snapshot.md，三段连续结构： -->
<!--   1) Akuna Capital 锚点 + 7 行 ↳（继承链1，应全部解析为 'Akuna Capital'） -->
<!--   2) Jump Trading Group：显式公司名，独立一行，自己没有 ↳ 子行（打断继承链1） -->
<!--   3) Jump Trading 锚点 + 2 行 ↳（继承链2，应全部解析为 'Jump Trading'，不是 'Jump Trading Group'） -->
<!-- 验证点：链2的 ↳ 绝不能被错误地继承成 'Akuna Capital' 或 'Jump Trading Group'—— -->
<!-- 继承只认『同一张表格中最近一个显式公司单元格』，中间任何显式公司名都会重置锚点。 -->

| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Akuna Capital | Quantitative Research Intern 🇺🇸 | Chicago | <a href="https://akunacapital.com/careers/job/8036614/?gh_jid=8036614&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Quantitative Development & Strategy Intern 🇺🇸 | Chicago | <a href="https://akunacapital.com/careers/job/8021481/?gh_jid=8021481&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Software Engineer Intern, C# .NET Desktop | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018886/?gh_jid=8018886&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Software Engineer Intern, Full Stack Web 🇺🇸 | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018893/?gh_jid=8018893&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Python Software Engineer Intern 🇺🇸 | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018853/?gh_jid=8018853&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Hardware Engineer Intern 🇺🇸 | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018880/?gh_jid=8018880&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Platform Engineer Intern 🇺🇸 | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018856/?gh_jid=8018856&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| ↳ | Software Engineer Intern, C++ 🇺🇸 | Chicago, IL | <a href="https://akunacapital.com/careers/job/8018847/?gh_jid=8018847&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 24 |
| Jump Trading Group | Campus UI Software Engineer Intern | Chicago, IL | <a href="https://www.jumptrading.com/hr/job?gh_jid=8003019&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 09 |
| Jump Trading | Campus Systems Engineer Intern | Chicago, IL | <a href="https://www.jumptrading.com/hr/job?gh_jid=8007788&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 09 |
| ↳ | Software Engineer Intern | Chicago, IL | <a href="https://www.jumptrading.com/hr/job?gh_jid=8002989&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 09 |
| ↳ | Quantitative Trader Intern | Chicago, IL</br>New York, NY | <a href="https://www.jumptrading.com/hr/job?gh_jid=7848371&utm_source=github-vansh-ouckah"><img src="https://i.imgur.com/u1KNU8z.png" width="118" alt="Apply"></a> | Jul 09 |
