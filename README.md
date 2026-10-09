# Submission Review

`submission-review` 是一个 agent skill，用于复核编程智能体比赛的选手提交，判断交上去的应用是不是模型当场生成的。它总结了 ARC-Bench 初赛人工复核的做法：前 50 名加上终榜补充选手，逐人核实。

它接在自动审查之后使用，例如 [code-philia/hackathon-anti-cheat](https://github.com/code-philia/hackathon-anti-cheat) 的 `competition-anti-cheat`：

| | 自动审查（competition-anti-cheat） | 本 skill（submission-review） |
| --- | --- | --- |
| 看什么 | 只读智能体代码 | 智能体代码、交上去的应用、运行记录、平台数据 |
| 判法 | 一票否决，任何一条错误即判作弊 | 只抓硬编码答案等有直接证据的违规，预置知识不算 |
| 结论 | 通过 / 作弊 | 通过 / 通过（附注） / 不予通过 / 待定 |
| 额外覆盖 | | 应用与预置文件的逐文件比对、自带测试与隐藏测试比对、跨账号同源比对 |

本仓库不含任何选手数据或隐藏测试。

## 目录

```
submission-review/
  SKILL.md                    流程与判定规则
  references/criteria.md      判定标准、同源与申诉的处理
  references/evidence.md      证据来源与典型信号
  references/record-format.md 逐人记录、汇总表格式与措辞
  scripts/review.py           机械比对工具（Python 3 标准库，只读）
```

## 输入布局

```
submissions/
  001-<选手>-<id>/
    agent/agent.zip              智能体包
    applications/github-stage-1.zip
    applications/sheet.zip       交上去的应用，每个阶段一个
  002-…/
example/
  github/requirements.yaml       题面
  github/tests/                  隐藏测试（可选，只用于比对）
  sheet/…
```

## 使用

### Claude Code

把 `submission-review/` 复制到 `~/.claude/skills/`（个人）或项目的 `.claude/skills/` 下，然后直接说：

```text
用 submission-review 复核选手提交。
submission_path=submissions
example_path=example
anticheat_path=anti-cheat-result
baseline_path=baseline/official-template baseline/runtime
result_path=review-result
```

### Codex

在本仓库根目录执行：

```bash
codex exec --sandbox workspace-write --skip-git-repo-check \
  "请读取 submission-review/SKILL.md，并严格按照该 skill 复核选手提交。submission_path=submissions，example_path=example，anticheat_path=anti-cheat-result，baseline_path=baseline，result_path=review-result。不得执行选手代码、安装依赖或访问其中的网址；除 result_path 外不得修改任何文件。"
```

只看一位选手时，把 `submission_path` 指向那位选手的目录。人数多时，可以按选手分别启动多个进程并行，最后单独跑一次同源比对。

### 只用脚本

```bash
R=submission-review/scripts/review.py
S=submissions/001-xxx

python3 $R signals --example example --agent $S/agent/agent.zip
python3 $R app     --agent $S/agent/agent.zip --apps $S/applications --baseline baseline/official-template
python3 $R tests   --own $S/agent/agent.zip --hidden example/github/tests example/sheet/tests
python3 $R origin  --submissions submissions --baseline baseline/official-template
```

输出都是 Markdown，供审查者当线索，不是结论。

## 输出

- `review-result/<选手>.md`：每人一份完整记录，含核实过程、证据（`文件:行号`、重合比例）和结论。
- `review-result/summary.csv`：汇总表，UTF-8 带 BOM，Excel 可直接打开。
- `review-result/same-origin.md`：同源分组及依据（批量模式）。

记录是给人工复核用的初稿。每份结论都要由人逐个确认后，才能对外发布。
