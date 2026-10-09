#!/usr/bin/env python3
"""submission-review 的机械比对工具。

只在内存里读取 zip 和目录（history 会把 .git 解到临时目录），不执行选手的任何代码。
输出是 Markdown，给审查者当线索用，不是结论。

子命令：
  signals  在智能体包里找线索（写死的题目名、题面专有字符串、复制逻辑、读测试、带回评测信息等）
  app      比对产出和智能体包里的预置文件（逐字节相同的文件 + 行重合比例）
  history  读产出里的版本历史（.git）：每次提交改了多少，起点是什么；可导出起点或最终版本
  tests    比对选手自带的测试和隐藏测试
  origin   在一批选手之间找同源代码（包内容相同 + 行重合）

history 会调用 git，但只在一个新建的空仓库里读选手的对象库，不读选手仓库的配置、钩子和属性文件。
"""
import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

TEXT_EXT = {
    '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.py', '.html', '.htm', '.css', '.scss',
    '.json', '.md', '.txt', '.yaml', '.yml', '.toml', '.sql', '.sh', '.vue', '.svelte',
    '.go', '.rs', '.java', '.rb', '.php', '.ini', '.cfg', '.xml', '.csv', '',
}
SKIP_DIRS = {'node_modules', '.git', '__pycache__', '.venv', 'venv', '.next', '.cache', 'coverage', '__MACOSX'}
SKIP_FILES = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', '.DS_Store'}
MAX_MEMBER = 20 * 1024 * 1024
MAX_DEPTH = 3
MAX_LINE = 400


# ---------- 读取 ----------

def iter_files(src):
    """依次给出 (路径, 字节)。src 可以是目录或 zip，zip 里嵌套的 zip 也会展开。"""
    p = Path(src)
    if p.is_dir():
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                fp = Path(root) / f
                if f in SKIP_FILES or fp.is_symlink() or fp.stat().st_size > MAX_MEMBER:
                    continue
                yield from _expand(str(fp.relative_to(p)), fp.read_bytes(), 0)
    elif p.is_file():
        yield from _expand_zip(p.read_bytes(), '', 0) if zipfile.is_zipfile(p) else iter([(p.name, p.read_bytes())])
    else:
        sys.exit(f'找不到输入：{src}')


def _expand(rel, data, depth):
    if rel.lower().endswith('.zip') and depth < MAX_DEPTH and zipfile.is_zipfile(io.BytesIO(data)):
        yield from _expand_zip(data, rel + '!/', depth + 1)
    else:
        yield rel, data


def _expand_zip(data, prefix, depth):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return
    for info in z.infolist():
        parts = info.filename.split('/')
        if info.is_dir() or info.file_size > MAX_MEMBER or parts[-1] in SKIP_FILES or SKIP_DIRS & set(parts):
            continue
        try:
            yield from _expand(prefix + info.filename, z.read(info), depth)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
            continue  # 加密或损坏的条目跳过


def text_files(src):
    """只保留文本文件：给出 (路径, sha256, 文本)。"""
    for rel, data in iter_files(src):
        if Path(rel).suffix.lower() not in TEXT_EXT or b'\0' in data[:4096]:
            continue
        yield rel, hashlib.sha256(data).hexdigest(), data.decode('utf-8', errors='replace')


def norm_lines(text):
    """去掉空白、纯符号行（如 `});`）和超长的压缩行，用来算行重合。"""
    out = []
    for line in text.splitlines():
        s = ' '.join(line.split())
        if len(s) >= 8 and len(s) <= MAX_LINE and sum(c.isalnum() for c in s) >= 4:
            out.append(s)
    return out


def line_set(srcs):
    s = set()
    for src in srcs or []:
        for _, _, text in text_files(src):
            s.update(norm_lines(text))
    return s


def pct(a, b):
    return f'{100 * a / b:.1f}%' if b else '-'


# ---------- signals ----------

SPEC_EXT = {'.yaml', '.yml', '.md', '.txt', '.json'}


def load_task(task):
    """读题面（目录下的 yaml/md/txt/json，跳过测试目录），取题目名和题面里引号括起来的专有字符串。"""
    p = Path(task)
    if p.is_file():
        specs = [p]
    elif p.is_dir():
        specs = sorted(f for f in p.rglob('*') if f.is_file() and f.suffix.lower() in SPEC_EXT
                       and not is_test_path(str(f.relative_to(p))))
    else:
        sys.exit(f'找不到题面：{task}')
    titles, phrases = [], set()
    for spec in specs:
        raw = spec.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'^(?:name|title):\s*(.+)$', raw, re.M) or re.search(r'^#\s+(.+)$', raw, re.M)
        t = m.group(1).strip().strip('"\'') if m else ''
        if len(t) >= 15 and t not in titles:  # 太短的名字（如 "GitHub"）会到处命中，不算
            titles.append(t)
        flat = ' '.join(raw.split())
        for q in re.findall(r'“([^”]{6,80})”|"([^"]{6,80})"|`([^`]{6,80})`', flat):
            phrases.add(next(x for x in q if x))
    return titles, phrases


# 全部按不区分大小写匹配。比赛特有的测试路径用 --test-path 补充。
SIGNAL_PATTERNS = {
    '复制目录或文件': r'copytree|copyfile|shutil\.copy|shutil\.move|cpSync|copySync|fs\.cp\(|\bcp -[a-z]*r|\brsync\b',
    '读取测试': r'hidden[_ -]?tests?|acceptance[_ -]?(tests?|specs?)|grader|tests?_dir|\.spec\.[jt]sx?\b|playwright\.config|conftest\.py',
    '带回评测信息': r'eval[-_ ]?traffic|telemetry|platform eval|after (the )?agent (exits|finished)|playwright-report|test-results|junit\.xml',
    '不经模型的说法': r'no model calls?|without (calling )?(the |any )?model|\breplay\b|\bsnapshots?\b|precomputed|pre-?built|prepared (stage|app)',
    '模型地址与密钥': r'(?-i:base_url|BASE_URL|api_key|API_KEY|baseURL|apiKey)',  # 不区分大小写会把前端的 baseUrl 等全带进来
    '计费相关': r'(cost|usage|billing|token)[\w ]{0,20}(calibrat|pad|target|inflate|fake|spoof)|cost_calibration',
}


def cmd_signals(a):
    titles, phrases = load_task(a.task)
    if not titles and not phrases:
        sys.exit(f'{a.task} 下没有读到题面')
    files = list(text_files(a.agent))
    print(f'# 硬编码线索：{a.agent}\n')
    print(f'题目：{"；".join(titles) or "（题面里没有找到标题）"}。智能体包内文本文件 {len(files)} 个。以下只是线索，每条都要打开原文件确认。\n')

    print('## 写死的题目名\n')
    hits = [(rel, i, t) for rel, _, text in files for i, line in enumerate(text.splitlines(), 1) for t in titles if t in line]
    for rel, i, t in hits[:40]:
        print(f'- `{rel}:{i}` 含题目名 “{t}”')
    print('- 无\n' if not hits else '')

    print('## 题面专有字符串最多的文件\n')
    print(f'从题面提取了 {len(phrases)} 条引号内的字符串（按钮名、报错文案、示例值等）。命中多的代码文件可能是预先写好的应用；命中多的提示词文件通常只是复述题面。\n')
    rows = []
    for rel, _, text in files:
        n = sum(1 for ph in phrases if ph in text)
        if n:
            rows.append((n, rel, len(text.splitlines())))
    rows.sort(reverse=True)
    print('| 命中条数 | 文件 | 行数 |\n| --- | --- | --- |')
    for n, rel, lines in rows[:20]:
        print(f'| {n} | `{rel}` | {lines} |')
    print()

    for name, pat in SIGNAL_PATTERNS.items():
        if name == '读取测试' and a.test_path:
            pat = '|'.join(map(re.escape, a.test_path)) + '|' + pat
        rx = re.compile(pat, re.I)
        found = [(rel, i, line.strip()[:160]) for rel, _, text in files
                 for i, line in enumerate(text.splitlines(), 1) if rx.search(line)]
        print(f'## {name}（{len(found)} 处）\n')
        for rel, i, line in found[:a.limit]:
            print(f'- `{rel}:{i}` {line}')
        if len(found) > a.limit:
            print(f'- ……另有 {len(found) - a.limit} 处，用 --limit 调大')
        print()

    hosts = Counter(h for _, _, text in files for h in re.findall(r'https?://([a-zA-Z0-9.-]+)', text))
    print('## 代码里出现的外部地址\n')
    for h, n in hosts.most_common(30):
        print(f'- {h}（{n} 处）')
    print()

    vers = [(rel, text.strip()[:120]) for rel, _, text in files
            if re.search(r'(^|/)(VERSION|version\.txt|README_\d+[^/]*\.md)$', rel)]
    if vers:
        print('## 版本文件（可用于同源判断）\n')
        for rel, head in vers[:30]:
            print(f'- `{rel}`：{" ".join(head.split())}')


# ---------- app ----------

def cmd_app(a):
    base = line_set(a.baseline)
    agent_files = list(text_files(a.agent))
    agent_sha = {sha: rel for rel, sha, _ in agent_files}
    owner = defaultdict(set)  # 行 -> 含有这一行的智能体文件
    for rel, _, text in agent_files:
        for ln in norm_lines(text):
            if ln not in base:
                owner[ln].add(rel)

    apps = []
    for x in map(Path, a.apps):  # 目录里直接放着 zip 时，每个 zip 算一份产出；否则整个目录算一份
        zips = sorted(x.glob('*.zip')) if x.is_dir() else []
        apps += zips or [x]
    print(f'# 应用与预置文件比对：{a.agent}\n')
    print(f'智能体包内文本文件 {len(agent_files)} 个。' + (f'基线（公共代码、平台给的起点）{len(base)} 行，已从两边扣除。' if a.baseline else '没有提供基线，重合里可能包含官方模板、公开框架或平台给的起点。') + '\n')
    print('| 产出 | 文件数 | 与预置文件逐字节相同 | 有效行 | 来自基线 | 其余行中与预置文件重合 |\n| --- | --- | --- | --- | --- | --- |')
    per_app, sha_sets = [], {}
    for app in apps:
        files = list(text_files(app))
        sha_sets[app.name] = {sha for _, sha, _ in files}
        same = [rel for rel, sha, _ in files if sha in agent_sha]
        total = from_base = hit = 0
        detail = []
        for rel, sha, text in files:
            lines = norm_lines(text)
            src = Counter()
            fh = 0
            for ln in lines:
                if ln in base:
                    from_base += 1
                    continue
                total += 1
                if ln in owner:
                    hit += 1
                    fh += 1
                    src.update(owner[ln])
            rest = len([ln for ln in lines if ln not in base])
            if rest:
                detail.append((rest, fh, rel, src.most_common(1)[0][0] if src else '', sha in agent_sha))
        print(f'| {app.name} | {len(files)} | {len(same)} | {total + from_base} | {from_base} | {hit}/{total}（{pct(hit, total)}） |')
        per_app.append((app.name, detail))
    print()
    for name, detail in per_app:
        detail.sort(reverse=True)
        print(f'## {name}：行数最多的文件\n')
        print('| 文件 | 有效行 | 与预置重合 | 主要来源（智能体包内） | 逐字节相同 |\n| --- | --- | --- | --- | --- |')
        for rest, fh, rel, src, same in detail[:a.top]:
            print(f'| `{rel}` | {rest} | {pct(fh, rest)} | `{src}` | {"是" if same else ""} |')
        print()
    names = list(sha_sets)
    if len(names) > 1:
        print('## 各份产出之间相同文件的比例\n')
        print('同一道题的不同阶段，如果交的是同一份文件，说明后面的阶段没有重新生成。\n')
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                inter = len(sha_sets[x] & sha_sets[y])
                print(f'- {x} 与 {y}：{inter} 个文件相同（占较小一方的 {pct(inter, min(len(sha_sets[x]), len(sha_sets[y])))}）')


# ---------- history ----------

def _git_dir(app, tmp):
    """找产出里的 .git。zip 只把 .git/ 下的条目解到临时目录。"""
    p = Path(app)
    if p.is_dir():
        g = p / '.git'
        return g if g.is_dir() else None
    try:
        z = zipfile.ZipFile(p)
    except zipfile.BadZipFile:
        sys.exit(f'{app} 打不开，可能被截断：先用 bsdtar -xf 解到一个新的空目录，再把目录传进来')
    picked, prefix = [], None
    for info in z.infolist():
        parts = info.filename.split('/')
        if '.git' not in parts or info.is_dir() or '..' in parts or info.filename.startswith('/'):
            continue
        i = parts.index('.git')
        prefix = prefix if prefix is not None else parts[:i]
        if parts[:i] == prefix:  # 只取最外层的仓库
            picked.append((info, '/'.join(parts[i:])))
    if not picked:
        return None
    root = Path(tmp) / 'extracted'
    for info, rel in picked:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(z.read(info))
    return root / '.git'


def _resolve_head(gitdir):
    head = (gitdir / 'HEAD').read_text(errors='replace').strip()
    sha = head
    if head.startswith('ref: '):
        ref, sha = head[5:].strip(), ''
        if not ref.startswith('refs/') or '..' in ref.split('/'):
            return None
        if (gitdir / ref).is_file():
            sha = (gitdir / ref).read_text().strip()
        elif (gitdir / 'packed-refs').is_file():
            for line in (gitdir / 'packed-refs').read_text(errors='replace').splitlines():
                parts = line.split(' ')
                if len(parts) == 2 and parts[1] == ref:
                    sha = parts[0]
    return sha if re.fullmatch(r'[0-9a-f]{40}([0-9a-f]{24})?', sha) else None


class Repo:
    """在新建的空仓库里读选手的对象库：选手仓库的 config、hooks、attributes 都不会生效。"""

    def __init__(self, gitdir, tmp):
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_ATTR_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}
        empty = Path(tmp) / 'empty.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(empty)], env=self.env, check=True)
        self.env.update(GIT_DIR=str(empty), GIT_OBJECT_DIRECTORY=str(Path(gitdir) / 'objects'))

    def run(self, *args):
        r = subprocess.run(['git', *args], env=self.env, capture_output=True)
        if r.returncode:
            sys.exit(f'git {args[0]} 失败：{r.stderr.decode(errors="replace").strip()[:300]}')
        return r.stdout

    def export(self, rev, out):
        out = Path(out)
        if out.exists() and any(out.iterdir()):
            sys.exit(f'导出目录必须是空的：{out}')
        n = 0
        for entry in self.run('ls-tree', '-r', '-z', rev).split(b'\0'):
            if not entry:
                continue
            meta, path = entry.split(b'\t', 1)
            mode, kind, sha = meta.decode().split()
            path = path.decode(errors='replace')
            if kind != 'blob' or mode == '120000' or path.startswith('/') or '..' in path.split('/'):
                continue  # 跳过子模块、符号链接和越界路径
            dest = out / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.run('cat-file', 'blob', sha))
            n += 1
        return n


def is_meta_path(path):
    """版本统计里不算的路径：以 . 开头的目录（运行记录、工具状态）、依赖和锁文件。"""
    parts = path.split('/')
    return any(x.startswith('.') for x in parts[:-1]) or bool(SKIP_DIRS & set(parts)) or parts[-1] in SKIP_FILES


def cmd_history(a):
    with tempfile.TemporaryDirectory() as tmp:
        gitdir = _git_dir(a.app, tmp)
        if gitdir is None:
            sys.exit(f'{a.app} 里没有 .git')
        head = _resolve_head(gitdir)
        if head is None:
            sys.exit('读不出 HEAD 指向的提交')
        repo = Repo(gitdir, tmp)
        raw = repo.run('log', '--reverse', '--root', '--no-renames', '--format=@@%H%x09%ct%x09%s', '--numstat', head)
        commits = []
        for line in raw.decode(errors='replace').splitlines():
            if line.startswith('@@'):
                sha, ts, subject = (line[2:].split('\t', 2) + [''])[:3]
                commits.append({'sha': sha, 'ts': int(ts), 'subject': subject, 'files': 0, 'add': 0, 'del': 0, 'meta': 0})
            elif line.strip() and commits:
                add, dele, path = line.split('\t', 2)
                c = commits[-1]
                if is_meta_path(path):
                    c['meta'] += 1
                    continue
                c['files'] += 1
                c['add'] += int(add) if add.isdigit() else 0
                c['del'] += int(dele) if dele.isdigit() else 0
        fmt = lambda ts: time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))
        start, end = commits[0], commits[-1]
        print(f'# 版本历史：{a.app}\n')
        print(f'共 {len(commits)} 次提交，{fmt(start["ts"])} 到 {fmt(end["ts"])}（UTC），历时 {(end["ts"] - start["ts"]) / 60:.0f} 分钟。'
              '下表不计以 . 开头的目录、依赖和锁文件。\n')
        print(f'起点提交 `{start["sha"][:7]}`「{start["subject"]}」含 {start["files"]} 个文件、{start["add"]} 行。'
              '它可能是平台给的起点（官方模板、继承的上一轮产出），也可能是智能体初始化时放进去的内容，要对照智能体包和平台说明确认。\n')
        print('| 时间 | 提交 | 说明 | 改动文件 | 增 | 删 |\n| --- | --- | --- | --- | --- | --- |')
        for c in commits[1:]:
            print(f'| {fmt(c["ts"])} | `{c["sha"][:7]}` | {c["subject"][:100]} | {c["files"]} | {c["add"]} | {c["del"]} |')
        if len(commits) > 1:
            stat = repo.run('diff', '--numstat', '--no-renames', start['sha'], head).decode(errors='replace')
            rows = [ln.split('\t', 2) for ln in stat.splitlines() if ln.strip()]
            rows = [r for r in rows if not is_meta_path(r[2])]
            add = sum(int(r[0]) for r in rows if r[0].isdigit())
            dele = sum(int(r[1]) for r in rows if r[1].isdigit())
            print(f'\n起点到最终：改动 {len(rows)} 个文件，增 {add} 行，删 {dele} 行。')
        else:
            print('\n只有起点一次提交：这次运行没有留下任何修改记录。')
        for label, rev, out in (('起点', start['sha'], a.export_start), ('最终版本', head, a.export_end)):
            if out:
                print(f'\n已把{label}的 {repo.export(rev, out)} 个文件导出到 `{out}`。')


# ---------- tests ----------

def is_test_path(rel):
    r = rel.lower()
    return bool(re.search(r'(^|/|!/)(tests?|e2e|specs?|__tests__|local_tests)(/|$)|\.(spec|test)\.[jt]sx?$'
                          r'|(^|/)test_[^/]*\.py$|_test\.(py|go)$', r))


def cmd_tests(a):
    base = line_set(a.baseline)
    own = [(rel, norm_lines(text)) for rel, _, text in text_files(a.own) if is_test_path(rel)]
    own_all = {ln for _, ls in own for ln in ls if ln not in base}
    print(f'# 自带测试与隐藏测试比对：{a.own}\n')
    print(f'选手包里测试类文件 {len(own)} 个，有效行 {len(own_all)} 条。\n')
    if not own:
        print('没有找到测试类文件。')
        return
    for hidden in a.hidden:
        h = line_set([hidden]) - base
        inter = h & own_all
        print(f'## 对照 {hidden}\n')
        print(f'- 隐藏测试 {len(h)} 行，其中 {len(inter)} 行（{pct(len(inter), len(h))}）在选手测试里出现。')
        print(f'- 选手测试 {len(own_all)} 行，其中 {len(inter)} 行（{pct(len(inter), len(own_all))}）来自这套隐藏测试。')
        rows = sorted(((sum(1 for ln in ls if ln in h), len(ls), rel) for rel, ls in own), reverse=True)
        print('\n| 选手测试文件 | 行数 | 与隐藏测试相同的行 |\n| --- | --- | --- |')
        for n, total, rel in rows[:a.top]:
            if n:
                print(f'| `{rel}` | {total} | {n}（{pct(n, total)}） |')
        print()
    print('注意：题面里的按钮名、报错文案、示例账号本来就是公开的，照题面写的自检脚本也会和隐藏测试有少量相同的行。几十个百分点以上、且结构和辅助函数也一致，才值得深究。')


# ---------- origin ----------

OUTPUT_DIRS = {'applications', 'apps', 'outputs', 'output', 'artifacts', 'results', 'logs'}


def agent_package(d, pattern=None):
    """找一位选手目录里的智能体包，返回路径列表。布局不同的比赛用 --agent-glob 指定。"""
    d = Path(d)
    if pattern:
        return sorted(d.glob(pattern))
    for found in (sorted(d.glob('agent/*.zip')), [d / 'agent'] if (d / 'agent').is_dir() else [],
                  sorted(d.glob('*.zip')),
                  sorted(p for p in d.iterdir() if p.is_dir() and p.name not in OUTPUT_DIRS and not p.name.startswith('.'))):
        if found:
            return found
    return []


def package_digest(paths):
    """单个文件取 sha256；目录（已解压的包）对里面的文件逐个取 sha256 再汇总，内容相同的目录得到相同的值。"""
    if len(paths) == 1 and paths[0].is_file():
        return hashlib.sha256(paths[0].read_bytes()).hexdigest()
    h = hashlib.sha256()
    for p in paths:
        for rel, data in sorted(iter_files(p)):
            h.update(f'{rel}\0{hashlib.sha256(data).hexdigest()}\n'.encode())
    return h.hexdigest()


def cmd_origin(a):
    base = line_set(a.baseline)
    subs = []
    for d in sorted(p for p in Path(a.submissions).iterdir() if p.is_dir()):
        pkg = agent_package(d, a.agent_glob)
        if not pkg:
            print(f'跳过 {d.name}：没有找到智能体包', file=sys.stderr)
            continue
        subs.append((d.name, package_digest(pkg), line_set(pkg) - base))
    print(f'# 同源比对：{a.submissions}\n')
    print(f'共 {len(subs)} 位选手。' + ('已扣除公共代码基线。' if a.baseline else '没有提供公共代码基线：公开框架（官方模板、公开开源项目）带来的相似会被算进来，结论前务必补上基线重跑。') + '\n')

    by_sha = defaultdict(list)
    for name, sha, _ in subs:
        by_sha[sha].append(name)
    print('## 智能体包内容完全相同\n')
    groups = [v for v in by_sha.values() if len(v) > 1]
    for g in groups:
        print('- ' + '、'.join(g))
    print('- 无\n' if not groups else '')

    print(f'## 行重合不低于 {a.threshold:.0%} 的选手对\n')
    print('重合 = 两人共有的有效行 ÷ 较小一方的有效行。\n')
    print('| 选手 A | 选手 B | 共有行 | 重合 |\n| --- | --- | --- | --- |')
    parent = {n: n for n, _, _ in subs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shown = 0
    for i, (n1, _, s1) in enumerate(subs):
        for n2, _, s2 in subs[i + 1:]:
            m = min(len(s1), len(s2))
            if m < a.min_lines:
                continue
            inter = len(s1 & s2)
            if inter / m >= a.threshold:
                shown += 1
                if shown <= a.limit:
                    print(f'| {n1} | {n2} | {inter} | {pct(inter, m)} |')
                parent[find(n1)] = find(n2)
    if shown > a.limit:
        print(f'\n……另有 {shown - a.limit} 对未列出，用 --limit 调大；分组不受影响。')
    for g in by_sha.values():
        for x in g[1:]:
            parent[find(x)] = find(g[0])
    clusters = defaultdict(list)
    for n, _, _ in subs:
        clusters[find(n)].append(n)
    print('\n## 分组\n')
    k = 0
    for members in sorted((sorted(m) for m in clusters.values() if len(m) > 1), key=lambda m: m[0]):
        k += 1
        print(f'- 第 {k} 组：' + '、'.join(members))
    if not k:
        print('- 无')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('signals', help='在智能体包里找线索')
    s.add_argument('--task', '--example', dest='task', required=True, help='题面：文件或目录（读 yaml/md/txt/json，跳过测试目录）')
    s.add_argument('--agent', required=True, help='智能体包：zip 或目录')
    s.add_argument('--test-path', nargs='*', default=[], help='比赛特有的隐藏测试路径或变量名，加进"读取测试"一类')
    s.add_argument('--limit', type=int, default=30, help='每类线索最多列出几处')
    s.set_defaults(fn=cmd_signals)

    s = sub.add_parser('app', help='比对产出和预置文件')
    s.add_argument('--agent', required=True, help='智能体包：zip 或目录')
    s.add_argument('--apps', required=True, nargs='+', help='产出：zip 或目录，可多个；直接放着多个 zip 的目录会逐个展开')
    s.add_argument('--baseline', nargs='*', help='要扣掉的代码：官方模板、公开框架、平台给的起点；zip 或目录，可多个')
    s.add_argument('--top', type=int, default=12)
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser('history', help='读产出里的版本历史')
    s.add_argument('--app', required=True, help='一份产出：目录或 zip（zip 只取其中的 .git）')
    s.add_argument('--export-start', help='把起点提交的文件导出到这个空目录（可作为 app 的 --baseline）')
    s.add_argument('--export-end', help='把最终提交的文件导出到这个空目录（产出压缩包损坏时用来恢复源码）')
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser('tests', help='比对选手自带测试和隐藏测试')
    s.add_argument('--own', required=True, help='选手智能体包或产出：zip 或目录')
    s.add_argument('--hidden', required=True, nargs='+', help='隐藏测试目录，可多个')
    s.add_argument('--baseline', nargs='*')
    s.add_argument('--top', type=int, default=12)
    s.set_defaults(fn=cmd_tests)

    s = sub.add_parser('origin', help='在一批选手之间找同源代码')
    s.add_argument('--submissions', required=True, help='每个子目录是一位选手')
    s.add_argument('--agent-glob', help='智能体包在选手目录里的位置，如 "agent/*.zip"；不给时自动找')
    s.add_argument('--baseline', nargs='*')
    s.add_argument('--threshold', type=float, default=0.5)
    s.add_argument('--min-lines', type=int, default=200, help='有效行太少的智能体不参与比较')
    s.add_argument('--limit', type=int, default=60, help='选手对最多列出几行')
    s.set_defaults(fn=cmd_origin)

    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
