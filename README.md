# AI 职位雷达

每天读取 4 个公开 GitHub 职位仓库，识别新增岗位，用 Claude 分档排序，通过 Gmail SMTP 推送；周六出复盘。
运行在 GitHub Actions，状态存 Git，无数据库无服务器。

设计决议、铁律、常见陷阱见 [CLAUDE.md](./CLAUDE.md)（最高优先级文档，与本文件冲突以它为准）。

---

## GitHub Actions Secrets 配置

`.github/workflows/daily.yml` 需要以下 5 个 repository secrets 才能真正发信
（仓库路径：**Settings → Secrets and variables → Actions → New repository secret**）：

| Secret | 说明 |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587`（STARTTLS） |
| `SMTP_USER` | 发信用的 Gmail 地址，例如 `you@gmail.com` |
| `SMTP_APP_PASSWORD` | Gmail **应用专用密码**，不是账号登录密码——账号需先开启两步验证，再在 [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) 生成一个 16 位密码 |
| `MAIL_TO` | 收件地址，可以和 `SMTP_USER` 相同 |

> 铁律4：这 5 个值只作为 GitHub Actions secrets 存在，不会出现在代码、`config/`、日志或 commit 里。
> `--dry-run` 模式下完全不读取这些变量（不会因为没配置而报错），可以先在没有配好 secrets 的情况下用
> `workflow_dispatch` + `dry_run: true` 验证抓取/去重/排序/渲染是否正常。

`ANTHROPIC_API_KEY` 目前**不需要配置**——`ranker.py` 目前只接了关键词降级路径（FR-011），AI
分档排序（CLAUDE.md「实现顺序」步骤5剩余部分）还没接入 daily 编排；等接入后会在这里补充这个 secret
以及对应的 `config/models.yaml` 说明。

---

## 手动触发（workflow_dispatch）

Actions 页面手动运行 `daily` workflow 时有两个布尔参数：

- **`bootstrap`**：首次部署用。抓取全部存量岗位，整份写入 `data/seen_jobs.json`（`sent_at` 全部
  填 `"bootstrap"`），不发邮件、不打分。只能在 `seen_jobs.json` 为空/不存在时跑；已有内容时会直接
  拒绝退出，不写任何东西（避免误跑把真实发送历史冲掉）。
- **`dry_run`**：不发邮件、不写 `data/`、不 commit/push，只在 runner 上把渲染好的邮件 HTML 输出到
  `/tmp/preview.html`（GitHub Actions 里看不到这个文件，需要本地跑 `python -m job_radar.daily --dry-run`
  才能真正打开检查；workflow 里的 `dry_run` 主要用来验证抓取/去重/排序全流程在 CI 环境里跑不跑得通、
  权限够不够，而不是看邮件渲染效果）。

两个参数可以同时勾选（`bootstrap` + `dry_run` = 验证 bootstrap 全流程但不落盘不 commit）。

正常的每日调度不需要手动做任何事：cron 在 `01:30 UTC`（北京时间 09:30）自动跑，不带任何参数，
等价于本地的 `python -m job_radar.daily`。

---

## 本地开发

见 [CLAUDE.md「开发命令」](./CLAUDE.md#开发命令)。
