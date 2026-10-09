#!/usr/bin/env python3
"""submission-review 的机械比对工具。

只在内存里读取 zip 和目录，不解压到磁盘，不执行选手的任何代码。
输出是 Markdown，给审查者当线索用，不是结论。

子命令：
  signals  在智能体包里找硬编码线索（写死的题目名、题面专有字符串、复制逻辑、读测试目录等）
  app      比对交上去的应用和智能体包里的预置文件（逐字节相同的文件 + 行重合比例）
  tests    比对选手自带的测试和隐藏测试
  origin   在一批选手之间找同源代码（压缩包逐字节相同 + 行重合）
"""
import argparse
import hashlib
import io
import os
import re
import sys
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

def load_requirements(example):
    """在题目目录下找 requirements.yaml，取题目名和题面里引号括起来的专有字符串。"""
    titles, phrases = [], set()
    for req in sorted(Path(example).rglob('requirements.yaml')):
        raw = req.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'^name:\s*(.+)$', raw, re.M)
        t = m.group(1).strip().strip('"\'') if m else ''
        if len(t) >= 15 and t not in titles:  # 太短的名字（如 "GitHub"）会到处命中，不算
            titles.append(t)
        flat = ' '.join(raw.split())
        for q in re.findall(r'“([^”]{6,80})”|"([^"]{6,80})"|`([^`]{6,80})`', flat):
            phrases.add(next(x for x in q if x))
    return titles, phrases


SIGNAL_PATTERNS = {
    '复制目录或文件': r'copytree|copyfile|shutil\.copy|shutil\.move|cpSync|copySync|fs\.cp\(|\bcp -[a-zA-Z]*r|\brsync\b',
    '读取测试目录': r'/workspace/tests|workspace/tests|\.spec\.ts\b|playwright\.config',
    '不经模型的说法': r'(?i)no model calls?|without (calling )?(the |any )?model|\breplay\b|\bsnapshots?\b|precomputed|pre-?built|prepared (stage|app)',
    '模型地址与密钥': r'base_url|BASE_URL|api_key|API_KEY|baseURL|apiKey',
    '计费相关': r'(?i)(cost|usage|billing|token)[\w ]{0,20}(calibrat|pad|target|inflate|fake|spoof)|cost_calibration',
}


def cmd_signals(a):
    titles, phrases = load_requirements(a.example)
    if not titles:
        sys.exit(f'{a.example} 下没有找到 requirements.yaml')
    files = list(text_files(a.agent))
    print(f'# 硬编码线索：{a.agent}\n')
    print(f'题目：{"；".join(titles)}。智能体包内文本文件 {len(files)} 个。以下只是线索，每条都要打开原文件确认。\n')

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
        rx = re.compile(pat)
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
    for x in map(Path, a.apps):  # 目录里直接放着 zip 时，每个 zip 算一个应用；否则整个目录算一个应用
        zips = sorted(x.glob('*.zip')) if x.is_dir() else []
        apps += zips or [x]
    print(f'# 应用与预置文件比对：{a.agent}\n')
    print(f'智能体包内文本文件 {len(agent_files)} 个。' + (f'公共代码基线 {len(base)} 行，已从两边扣除。' if a.baseline else '没有提供公共代码基线，重合里可能包含官方模板等公共代码。') + '\n')
    print('| 应用 | 文件数 | 与预置文件逐字节相同 | 有效行 | 来自公共基线 | 其余行中与预置文件重合 |\n| --- | --- | --- | --- | --- | --- |')
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
        print('## 各应用之间相同文件的比例\n')
        print('同一道题的不同阶段，如果交的是同一份文件，说明后面的阶段没有重新生成。\n')
        for i, x in enumerate(names):
            for y in names[i + 1:]:
                inter = len(sha_sets[x] & sha_sets[y])
                print(f'- {x} 与 {y}：{inter} 个文件相同（占较小一方的 {pct(inter, min(len(sha_sets[x]), len(sha_sets[y])))}）')


# ---------- tests ----------

def is_test_path(rel):
    r = rel.lower()
    return bool(re.search(r'(^|/|!/)(tests?|e2e|spec|__tests__|local_tests)(/|$)|\.(spec|test)\.[jt]sx?$', r))


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

def agent_package(d):
    zips = sorted(Path(d).glob('agent/*.zip')) or sorted(Path(d).glob('*.zip'))
    return zips[0] if zips else (Path(d) / 'agent' if (Path(d) / 'agent').is_dir() else None)


def cmd_origin(a):
    base = line_set(a.baseline)
    subs = []
    for d in sorted(p for p in Path(a.submissions).iterdir() if p.is_dir()):
        pkg = agent_package(d)
        if pkg is None:
            continue
        sha = hashlib.sha256(pkg.read_bytes()).hexdigest() if pkg.is_file() else ''
        subs.append((d.name, sha, line_set([pkg]) - base))
    print(f'# 同源比对：{a.submissions}\n')
    print(f'共 {len(subs)} 位选手。' + ('已扣除公共代码基线。' if a.baseline else '没有提供公共代码基线：公开框架（官方模板、公开开源项目）带来的相似会被算进来，结论前务必补上基线重跑。') + '\n')

    by_sha = defaultdict(list)
    for name, sha, _ in subs:
        if sha:
            by_sha[sha].append(name)
    print('## 智能体压缩包逐字节相同\n')
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

    s = sub.add_parser('signals', help='在智能体包里找硬编码线索')
    s.add_argument('--example', required=True, help='题目目录（递归查找 requirements.yaml）')
    s.add_argument('--agent', required=True, help='智能体包：zip 或目录')
    s.add_argument('--limit', type=int, default=30, help='每类线索最多列出几处')
    s.set_defaults(fn=cmd_signals)

    s = sub.add_parser('app', help='比对交上去的应用和预置文件')
    s.add_argument('--agent', required=True, help='智能体包：zip 或目录')
    s.add_argument('--apps', required=True, nargs='+', help='应用 zip 或目录，可多个；直接放着多个 zip 的目录会逐个展开')
    s.add_argument('--baseline', nargs='*', help='公共代码（官方模板、公开框架），zip 或目录，可多个')
    s.add_argument('--top', type=int, default=12)
    s.set_defaults(fn=cmd_app)

    s = sub.add_parser('tests', help='比对选手自带测试和隐藏测试')
    s.add_argument('--own', required=True, help='选手智能体包或应用：zip 或目录')
    s.add_argument('--hidden', required=True, nargs='+', help='隐藏测试目录，可多个')
    s.add_argument('--baseline', nargs='*')
    s.add_argument('--top', type=int, default=12)
    s.set_defaults(fn=cmd_tests)

    s = sub.add_parser('origin', help='在一批选手之间找同源代码')
    s.add_argument('--submissions', required=True, help='每个子目录是一位选手，内含 agent/*.zip')
    s.add_argument('--baseline', nargs='*')
    s.add_argument('--threshold', type=float, default=0.5)
    s.add_argument('--min-lines', type=int, default=200, help='有效行太少的智能体不参与比较')
    s.add_argument('--limit', type=int, default=60, help='选手对最多列出几行')
    s.set_defaults(fn=cmd_origin)

    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
