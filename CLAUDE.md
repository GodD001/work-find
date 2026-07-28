# CLAUDE.md — AI 职位雷达

> 本文件是本仓库的最高优先级指令。与任何其他文档冲突时，以本文件为准。

## 项目一句话

每天读取 4 个公开 GitHub 职位仓库，识别新增岗位，用 Claude 分档排序，通过 Gmail SMTP 推送；周六出复盘。运行在 GitHub Actions，状态存 Git，无数据库无服务器。

---

## 铁律（违反即为 bug，不是风格问题）

### 1. 不许编造任何职位事实

- 只写入来源**明确出现**的字段。缺失一律 `null` 或 `not_stated`。
- **禁止**根据公司名推断 sponsorship、citizenship、clearance、地点、薪资、学历要求。
  - ❌ "Google 一般都 sponsor" → 不许写 `sponsorship: offers`
  - ❌ "Meta 在 Menlo Park" → 来源没写地点就是 `null`
- `date_posted_raw` 保留原文（如 `Jul 26`），**不补年份**。
- AI 模型只对**已给定的输入字段**做排序和解释，不得新增或改写事实。

### 2. 状态写入顺序不可调换

```
抓取成功 → 邮件发送成功 → 原子更新 seen_jobs.json + job_history.jsonl → git commit → push
```

- 邮件失败 → **绝不**写 `sent_at`。写了就是永久漏报。
- push 失败 → `git pull --rebase` 重试 3 次；最终失败则 workflow 标红，**不回滚**已发送状态，等人工介入。

### 3. 解析器宁可炸不可静默错列

- 列名、表头顺序、图例变化必须**显式检测**。
- 遇到未知结构 → raise + 记录到 `run_manifest.jsonl`，**不许**猜测列位置继续跑。
- 单个 source 失败不影响其他 source；邮件中标明缺失来源。
- **全部 source 失败 → 当次不写任何 seen 状态。**

### 4. 日志和 commit 里不许出现密钥

`ANTHROPIC_API_KEY`、`SMTP_APP_PASSWORD`、完整邮件口令一律不打印、不入库、不进 `data/`。
`data/` 中也不存简历正文、证件、手机号。

---

## 三条已定案的设计决议

这三条是评审后拍板的，**不要重新设计，不要"优化"**。

### D-1 调度错峰

```yaml
# daily.yml
on:
  schedule: [{ cron: "30 1 * * *" }]   # 北京时间 09:30

# weekly.yml
on:
  schedule: [{ cron: "45 1 * * 6" }]   # 北京时间 09:45，错开 15 分钟
```

两个 workflow 使用**同一个** concurrency group：

```yaml
concurrency:
  group: job-radar-state
  cancel-in-progress: false
```

### D-2 bootstrap 模式

首次部署**必须**先跑 bootstrap，否则会永久漏掉存量的几百个岗位。

- `workflow_dispatch` 参数 `bootstrap: true`（也支持 CLI `--bootstrap`）
- 行为：抓取全部存量 → 全部写入 `seen_jobs.json`，`sent_at` 填字符串 `"bootstrap"` → **不发邮件**
- 日常运行时：**只有真正进入邮件正文的岗位才写 `sent_at`**。溢出的（超过单封邮件 80 个上限的部分）保持未发送，留到下次运行。
- `first_seen_at` 与 `sent_at` 的写入必须整体挂在铁律 2 的原子更新步骤上，即**邮件发送成功之后**才发生；邮件失败则本次运行一条也不写（哪怕只是新记录的 `first_seen_at`），保证失败可安全重试，不产生"已见过但未真正处理"的中间态。
- **选发排序用三级键**：`first_seen_at` ASC → `fit_score` DESC → `canonical_id` ASC。
  - `first_seen_at` 是硬约束，防止积压队列被饿死（老的先发）。
  - `fit_score` 在同一批（`first_seen_at` 相同，日常场景下同批新增通常都是这种情况）内决定顺序，实际主导排序。
  - `canonical_id` 只做最终确定性兜底，避免并列时结果随哈希/字典序漂移不可复现。单独用 `canonical_id` 排序等于按哈希随机分配，会让高分岗位被随机挤进积压队列。
- **`sent_at` 三态定义**：

  | 取值 | 含义 | 是否进选发候选 |
  |---|---|---|
  | `"bootstrap"` | bootstrap 模式写入的存量岗位，从未也不会补发 | 否 |
  | ISO 8601 UTC 字符串 | 已成功进入某封邮件正文 | 否 |
  | `null` | 已见过、但从未推送（溢出积压） | **是** |

  去重判据 = `canonical_id` 是否存在于 `seen_jobs.json`；
  选发判据 = `sent_at is null`。
  两者是两个独立的表达式，**不许合并成一个条件**——合并后积压队列永远发不出去。

- 邮件顶部必须分别显示：`本次新增 N` / `本次推送 M` / `队列积压 N-M`
- **候选池为空（`本次新增 = 0` 且 `队列积压 = 0`）时跳过发信**，不发一封全是 0 的空邮件（PRD §9.1；此前漏抄进本文件，现在补上）。
- **`first_seen_at` 的定义**：本系统首次成功抓取到该 `canonical_id` 的时刻（ISO 8601 UTC），**禁止**从 `date_posted_raw` 反推。
  - `date_posted_raw` 是来源声称的发布时间，是**事实字段**——只能原样保留来源怎么写就怎么存，不补年份、不做换算（铁律1）。
  - `first_seen_at` 是本系统的观察时间，是**系统字段**——由抓取成功的那次运行时刻决定，与岗位实际何时发布无关。
  - 两者不可互相推导或替代：把 `first_seen_at` 算成 `date_posted_raw` 等于给一个不可信、无年份的原文字段强行赋予精确时间语义，是铁律1"不许编造事实"的直接推论。

### D-3 canonical_id 兜底加二次比对

`canonical_id` 优先级：

1. 规范化 `application_url` 的 SHA-256
2. 来源明确给出的 job ID
3. 兜底：`source_repo + 原始行哈希`

**第 3 类（纯哈希兜底）的合并分两层，顺序固定，不可颠倒**：

- **第一层 — 行哈希完全相同 → 合并**。语义是"同一条数据在快照里出现了两次"。
- **第二层 — `fallback_key = (normalize(company), normalize(role))` 相同 → 合并**，但有三条约束：
  - a. 只在都属于第 3 类（既无 `application_url` 也无显式 job ID）的记录之间生效。第 1 类（有 URL）和第 2 类（有 job ID）不参与第二层。
  - b. 若两条记录的 `location` 或 `date_posted_raw` 不同，不合并。这两个字段是原文保留、未 normalize 的，能区分"同公司同职位的不同岗位"；而哈希漂移场景（上游改一个 emoji）下这两个字段通常不变。
  - c. 第二层只作用于第一层的产出，不回头重新分组。

**winner 选取**：一组被合并的记录中，取所有参与合并的 `canonical_id` 按**字典序最小**的那个
作为存活 id。不依赖行序、不依赖哪行是锚点——上游调整行顺序或删掉锚点行都不得改变结果。

**跨 run 稳定性**：`seen_jobs.json` 每条第 3 类记录必须额外存 `fallback_key`、`location`、
`date_posted_raw`。新抓到的第 3 类记录若 `canonical_id` 查不到，**先查 `fallback_key` 索引**：
命中且 `location` 与 `date_posted_raw` 均相同 → 复用旧记录的 `canonical_id`，判定为旧岗位，不推送。

> 约束 c 说的"第二层只作用于第一层产出、不回头扫全量"指的是**不重新分组**，
> 不是"不查历史状态"。少了跨 run 这一步，D-3 只在单次快照内生效，
> 上游隔天改一个 emoji 照样触发重发——那正是 D-3 存在的全部理由。


命中即视为旧岗位，不推送。

> 与 §7.1「不能证明相同时不强行合并」的取舍：本规则**仅**作用于既无 URL 又无 job ID 的兜底记录，且要求 company + role 完全一致。理由是上游改一个 emoji 就会让行哈希变化、导致同一岗位天天重发。宁可漏一个，不要每天重复推送。**这是有意为之，不要改回去。**

> **真实反例（不是假设）**：`tests/fixtures/s1/duplicate_identical_rows.md` 里 Kudu Dynamics 的锚点行和它继承出的两条 `↳` 行，company/role/location/date_posted_raw 四个字段两两全同（唯一差异只是"公司名是显式打出来的还是靠 ↳ 继承的"，这只是上游排版选择，不是能证明"是不同岗位"的信号）。按上面的两层规则：第一层先把两条 `↳` 行（原始行哈希相同）合并成 1 条；第二层再比对这条结果和锚点行——都是第 3 类、fallback_key 相同、location 和 date 也相同（都不满足约束 b 的"不同"条件），所以**继续合并，三行最终收敛成 1 条**。这不是漏洞，是规则忠实执行的结果——**不要**为了让锚点"看起来应该独立"而额外加一条"锚点不参与第二层"之类的例外，那种例外没有事实依据（company+role+location+date 全同的两条记录，凭什么因为一个用了 ↳ 一个没用就认定是不同岗位）。

**第 3 类兜底触发合并时必须写审计日志**：每次因为「原始行哈希相同」或「`fallback_key`（company+role）命中已有记录」而把一条新抓到的行判定为旧岗位、不推送，都要往 `run_manifest.jsonl` 追加一条：

```json
{"event": "dedup_merged", "canonical_id": "...", "source_repo": "...", "merged_count": N, "reason": "hash_fallback"}
```

`merged_count` = 这个 `canonical_id` 目前为止累计合并过的原始行数（第一次命中时是 2：原记录 + 这次被合并的新行；之后每再合并一行递增 1）。

理由：第 3 类兜底的合并是不可逆的静默丢弃——两行一旦判定为同一岗位，被合并掉的那条再也不会单独出现在邮件或 `job_history.jsonl` 里。如果从不记录，你永远没有机会发现某次合并其实是误判（比如两个不同岗位凑巧 company+role 完全一样）。有了日志，某天发现漏了个岗位时才能回头查是不是被这条规则吃掉的。

---

## 目录结构

```
.github/workflows/{daily,weekly}.yml
src/job_radar/
  fetchers/{vansh,speedyapply,zshah,sndsh}.py   # 四个独立适配器，互不共享列索引
  models.py normalize.py deduplicate.py
  filters.py ranker.py emailer.py
  daily.py weekly.py
config/{profile,sources,models}.yaml
templates/{daily,weekly}.{html,txt}.j2
data/{seen_jobs.json,job_history.jsonl,run_manifest.jsonl}
tests/fixtures/<source>/
tests/{unit,integration}/
```

**约束：**

- 四个 adapter 各自实现 `fetch()` / `parse(raw)` / `validate(records)`，**不共享列索引常量**。
- 模型 ID 只写在 `config/models.yaml`，**禁止**在代码里硬编码。
- 阈值、关键词、邮件上限、时区一律走 config，不写死。

---

## 数据源

| ID | 仓库 | 解析要点 |
|---|---|---|
| S1 | `vanshb03/Summer2027-Internships` | README Markdown 表格；`↳` 继承公司名；🛂/🇺🇸/🔒 图例 |
| S2 | `speedyapply/2027-SWE-College-Jobs` | 以实际文件结构为准，单独 fixture 锁定契约 |
| S3 | `zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships` | 自动生成列表，保留原始字段 |
| S4 | `sndsh404/summer-2027-internships` | 区分实习与 off-season；保留关闭状态 |

> **S1 默认分支实测为 `dev`（不是 main/master）**：`GET /repos/vanshb03/Summer2027-Internships/branches` 只返回 `dev`，`default_branch` 字段也是 `dev`。`raw.githubusercontent.com` 对 `main`/`master` 这类常见分支名有静默 fallback 到默认分支的行为——`curl .../main/README.md` 会返回 200 且内容与 `dev` 完全一致，**这不能作为 main 分支真实存在的证据**（`git clone` / Contents API / GraphQL 走 `main` 会直接失败）。抓取必须显式 pin 实测到的分支（已写入 `config/sources.yaml` 的 `s1.branch: dev`）。S2–S4 尚未验证，实现时同样要先查各自的实际默认分支，不可假设 main。

HTTP 超时 20s，指数退避最多 3 次。只读默认分支公开内容。

**`↳` 继承规则**：只能继承**同一张表格中最近一个明确的公司单元格**。跨表格、跨文件不继承。

---

## AI 调用约束

- 每日排序：`claude-haiku-4-5`；周报：`claude-sonnet-5`（ID 从 `config/models.yaml` 读）
- **分批**：每批最多 25–40 条。禁止一次性提交全部新增——300 条会顶爆 `max_tokens`，Pydantic 校验失败，当天完全没有 AI 排序。
- 单批失败只重试该批（1 次），不影响其他批次。全部批次失败才进降级路径。
- 输出用 Pydantic 校验：`canonical_id` 必须存在于本批输入；`fit_score` ∈ [0,100]；`matching_evidence` 和 `explicit_risks` 每一项都要能在输入记录或 `profile.yaml` 中逐项定位。
- 降级路径：确定性关键词打分，照常发邮件，邮件中标注「AI 排序不可用」。

---

## 开发命令

```bash
# 环境
uv venv && uv pip install -r requirements.lock

# 测试
pytest tests/ -v
pytest tests/unit/fetchers/ --cov=src/job_radar/fetchers --cov-fail-under=90

# 本地跑，不发邮件不写状态
python -m job_radar.daily --dry-run
python -m job_radar.daily --dry-run --source s1     # 只跑一个 source

# lint
ruff check src/ tests/ && ruff format src/ tests/
```

`--dry-run` 必须做到：不发邮件、不写 `data/`、把渲染好的 HTML 输出到 `/tmp/preview.html` 供人工检查。

---

## 测试要求

- **`fetchers/` 覆盖率硬线 90%**，其余模块不设限。bug 基本都出在 parser。
- 每个 source 存 2–5 个脱敏 fixture，必须覆盖：
  - 开放岗位 / 已关闭岗位
  - 跨行地点
  - `↳` 继承公司名
  - 缺失字段（无链接、无日期）
- 集成测试验证幂等：**同一份 fixture 连跑两次，第二次新增数必须为 0。**
- **积压队列排序测试**（`tests/integration/test_overflow_queue.py`），锁 D-2 三级排序键，禁止用真 AI（注入确定性打分器，走 FR-011 关键词降级路径）：
  - 场景一（验证原子写入 + 无遗漏无重复）：150 条全新岗位、cap=80。Run 1 发 80 条、70 条 `sent_at` 为空；Run 2 输入不变，新增数须为 0，但要把上次的 70 条积压发出去；Run 3 输入不变，队列已空，不应调用邮件发送。三轮跑完，150 条必须全部有非空 `sent_at`，且 `canonical_id` 不重复。
  - 场景二（专门锁三级键第一层 `first_seen_at` 优先于 `fit_score`）：Run 1 之后，输入变为原 150 条 + 100 条全新岗位；fixture 把积压那 70 条设计成低分（~20），100 条新岗位设计成高分（95+，其中 10 条明显最高）。断言 Run 2b 发出的 80 条 = 70 条积压 + 新岗位中分最高的 10 条。如果实现把排序键顺序写反（先按 `fit_score` 排），这条必红。

---

## 常见陷阱

1. **别把 GitHub 行链接当 `application_url`**。没有具体申请链接就是 `null`。
2. **URL 规范化只删已知追踪参数**（`utm_*` / `gh_src` / `source` / `ref`）。其他 query 参数可能影响职位定位，保留。原始 URL 永久保存。
3. **短链接不依赖重定向结果做主键**。
4. **`is_closed` 只认来源明确的 🔒 / closed 文字**，不推断。
5. **身份风险默认只展示不淘汰**。`profile.yaml` 里没配置身份约束时，不因未配置而自动排除岗位。
6. **Actions 第三方组件 pin 完整 commit SHA**，不用 tag。
7. **`job_history.jsonl` 按月轮转**为 `data/history/job_history_YYYY-MM.jsonl`，别让单文件无限增长。
8. **`is_closed` 与 `application_url` 必须独立判定，不许互相推导**。2026-07-28 抓取的 S1 快照里"标 🔒 的行"和"没有申请链接的行"恰好完全重合（都是 32 行），这是那一次抓取的巧合观察，不是数据契约保证。不许写"有链接就等于未关闭"或"没链接就等于已关闭"这类互推逻辑——上游随时可能出现"链接还在但文案已改成 closed"或"链接暂时缺失但没标 🔒"的行。两个字段各自只认来源里明确出现的信号（🔒 图例 → `is_closed`，见陷阱4；`<a href>` → `application_url`，见陷阱1），缺一个不能替对方赋值。
9. **S1 的 `location` 字段原样存字符串，包括 `</br>` 和 `<details>` 标签**。不拆成数组、不清洗、不做地理推断（比如不猜"哪个是主地点"、不把 `</br>` 换算成真实换行、不展开 `<details>` 摘要）。这是铁律1"不许编造事实"的推论：拆分/清洗都需要对原文做结构性假设，而这些假设本身就是一种编造。美化（渲染成列表、折叠展示等）留给邮件模板层做，不在抓取/解析阶段做。
10. **图例出现的列是解析契约，不是巧合**：🛂（不 sponsor）/ 🇺🇸（需美国公民）只允许出现在 Role 列；🔒（已关闭）只允许出现在 Application/Link 列。这是铁律3的直接应用——parser 发现某个图例出现在非预期列（比如 🔒 混进了 Role 文本、或 🛂 出现在 Company 列）时必须 raise，**不许**"反正认识这个 emoji 就当同样的意思处理"式地兼容，因为那意味着上游改了表格约定，静默兼容等于在没通知任何人的情况下悄悄吞掉一次结构变化。
11. **S1 快照中真实存在完全逐字节相同的两行，并且会带着它们的锚点一起被合并**（2026-07-28 抓取，Kudu Dynamics：锚点行 + 两条 `↳ | Software Engineer Intern | Chantilly, VA | 🔒 | May 22`，两条 `↳` 一字不差；见 `tests/fixtures/s1/duplicate_identical_rows.md`）。三行都没有 URL、没有 job ID：D-3 第一层（行哈希）先把两条 `↳` 合并成 1 条；第二层（`fallback_key` = company+role，且 location、date_posted_raw 也都相同）再把锚点也并进去——**三行最终收敛成 1 条**，不是 2 条。**这是既定规则（见 D-3 两层合并 + 真实反例）下的正确行为，不是 bug，不要"修复"它、不要试图靠"这行是显式公司名还是 ↳ 简写"之类的排版信号把锚点从合并里摘出来**——那不是能证明"是不同岗位"的事实信号。唯一要做的是按 D-3 的审计日志要求把这次合并记进 `run_manifest.jsonl`（`merged_count: 3`）——因为关闭状态下这三行没有任何字段能证明它们是"同一个岗位挂了三次"还是"三个不同岗位凑巧长得一样"，合并是不可逆的，日志是唯一能让人事后判断这次合并对不对的手段。

---

## 依赖版本

`requirements.lock` 生成前**去 PyPI 核对当前实际版本**，不要照抄 PRD 里的数字（那份是 2026-07-28 的快照，可能已过期）。
生产依赖精确 pin，每月 Dependabot 提 PR，过 fixture + 集成测试后再合。

---

## 实现顺序

**不要一次性写四个 adapter。** 按这个顺序：

1. `models.py` + `normalize.py` + 测试
2. **只做 S1**：真抓一次 README → 存 fixture → 写 parser → 跑测试 → 迭代
3. `deduplicate.py` + bootstrap 模式 + 幂等测试
4. `emailer.py` + 模板，用 `--dry-run` 检查渲染
5. `ranker.py` + 分批 + 降级路径
6. `daily.yml`，先手动 `workflow_dispatch --dry-run` 验证
7. 跑一次真 bootstrap
8. 再扩 S2 → S3 → S4，每个都先存 fixture
9. `weekly.py` + `weekly.yml`

每完成一步跑一次全量测试再往下走。
