# Submission Review

`submission-review` 是一个 agent skill，用来复核编程智能体比赛的提交。这类比赛里，选手交一个智能体，由它在比赛平台上生成应用、代码或其他产物，再由隐藏测试评分。skill 要回答三个问题：

- 产物是不是智能体在这次运行里当场做出来的？
- 它有没有用到不该用的信息？
- 有没有一人多号？

它接在自动审查之后使用，自动审查工具例如 [code-philia/hackathon-anti-cheat](https://github.com/code-philia/hackathon-anti-cheat)。两者的分工：

| | 常见的自动审查 | 本 skill |
| --- | --- | --- |
| 看什么 | 只读智能体代码 | 智能体代码、产出、版本历史、运行记录、平台数据 |
| 判法 | 一票否决，任何一条错误即判作弊 | 只认定有直接证据的违规，预置知识不算；比赛有自己的口径时以比赛为准 |
| 结论 | 通过 / 作弊 | 通过 / 通过（附注） / 不予通过 / 待定 |
| 额外覆盖 | | 产出与预置文件逐文件比对（可扣除公共代码和平台给的起点）、版本历史、自带测试与隐藏测试比对、跨账号同源比对 |

不同比赛的目录布局、起点、测试时机和审查口径都不一样。skill 先把这些情况弄清楚，再动手审查。`references/examples/arc-bench.md` 是一份写好的比赛说明，可以照着它的结构给别的比赛写一份。

本仓库不含任何选手数据或隐藏测试。

## 目录

```
submission-review/
  SKILL.md                          流程与判定规则
  references/criteria.md            默认判定口径、同源与申诉的处理
  references/evidence.md            证据来源与典型信号
  references/record-format.md       逐人记录、汇总表格式与措辞
  references/examples/arc-bench.md  比赛说明示例（ARC-Bench 初赛和决赛）
  scripts/review.py                 机械比对工具（Python 3 标准库，只读）
```

## 使用

### Claude Code

把 `submission-review/` 复制到 `~/.claude/skills/`（个人）或项目的 `.claude/skills/` 下，然后直接说：

```text
用 submission-review 复核选手提交。
submission_path=submissions
task_path=tasks
result_path=review-result
rules_path=rules.md                 # 可选：组织方的审查口径
prior_review_path=anti-cheat-result # 可选：自动审查结果
baseline_path=baseline/template baseline/runtime  # 可选：公共代码
```

### Codex

在本仓库根目录执行：

```bash
codex exec --sandbox workspace-write --skip-git-repo-check \
  "请读取 submission-review/SKILL.md，并严格按照该 skill 复核选手提交。submission_path=submissions，task_path=tasks，result_path=review-result，prior_review_path=anti-cheat-result，baseline_path=baseline。不得执行选手代码、安装依赖或访问其中的网址；除 result_path 外不得修改任何文件。"
```

只看一位选手时，把 `submission_path` 指向那位选手的目录。人数多时，可以按选手分别启动多个进程并行，最后单独跑一次同源比对。

### 只用脚本

```bash
R=submission-review/scripts/review.py
A=submissions/<选手>/agent.zip      # 智能体包：zip 或目录
O=submissions/<选手>/applications   # 产出：zip 或目录

python3 $R signals --task tasks --agent $A
python3 $R history --app $O/<某份产出>.zip --export-start /tmp/start   # 产出带 .git 时
python3 $R app     --agent $A --apps $O --baseline baseline/template /tmp/start
python3 $R tests   --own $A --hidden hidden-tests/<题>
python3 $R origin  --submissions submissions --baseline baseline/template
```

输出都是 Markdown，供审查者当线索，不是结论。`history` 会调用 git，但它在一个新建的空仓库里读选手的对象库，选手仓库的配置、钩子和属性文件都不会生效。

## 输出

- `review-result/context.md`：这场比赛的布局、起点、测试时机、反馈渠道和口径。没确认的项目会写明。
- `review-result/<选手>.md`：每人一份完整记录，包括核实过程、证据（`文件:行号`、重合比例、提交记录）和结论。
- `review-result/summary.csv`：汇总表，UTF-8 带 BOM，Excel 可直接打开。
- `review-result/same-origin.md`：批量模式下的同源分组及依据。

记录是给人工复核用的初稿。每份结论都要由人逐个确认后，才能对外发布。
