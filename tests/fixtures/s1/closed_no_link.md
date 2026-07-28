<!-- 边界情况 fixture：🔒 已关闭 / 无申请链接行，覆盖 4 种组合： -->
<!--   Rakuten International: 单行关闭，无特殊 location -->
<!--   Capital One: 关闭 + Location 用 </br> 多地点 -->
<!--   Kudu Dynamics (+2 ↳): 关闭 + 继承链（锚点和 ↳ 子行都关闭） -->
<!--   Salesforce: 关闭 + Location 用 <details> 折叠多地点 -->
<!-- 验证点：这 6 行 Application/Link 列均为裸 🔒、无 href；is_closed 与 application_url 必须独立判定 -->
<!-- （见 CLAUDE.md 常见陷阱条目8，不许用『无链接』反推『已关闭』，反之亦然，本 fixture 只是巧合全部重合）。 -->

| Company | Role | Location | Application/Link | Date Posted |
| ------- | ---- | -------- | ---------------- | ----------- |
| Rakuten International | Software Engineer Intern | San Mateo, California | 🔒 | Jul 09 |
| Capital One | Product Development Internship Program | Mclean, VA</br>Plano, TX | 🔒 | Jul 07 |
| Kudu Dynamics | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
| ↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
| ↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22 |
| Salesforce | Software Engineer Intern(Futureforce Summer 2027) | <details><summary>**5 locations**</summary>San Francisco, CA</br>Palo Alto, CA</br>New York, NY</br>Seattle, WA</br>Burlington, MA</details> | 🔒 | May 09 |
