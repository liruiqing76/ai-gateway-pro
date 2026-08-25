# 分析打包产物体积: 解析 Analysis-00.toc 的 binaries/datas 清单
import os
import ast

t = open('build/ai_gateway_pro/Analysis-00.toc', encoding='utf-8', errors='ignore').read()
d = ast.literal_eval(t)

entries = []
for field in d:
    if isinstance(field, list) and field and isinstance(field[0], (list, tuple)):
        for it in field:
            if len(it) >= 2 and isinstance(it[0], str) and isinstance(it[1], str):
                entries.append((it[0], it[1]))

seen, files = set(), []
for name, path in entries:
    if name in seen:
        continue
    seen.add(name)
    try:
        sz = os.path.getsize(path)
    except OSError:
        continue
    files.append((sz, name, path))

total = sum(f[0] for f in files)
print(f"unique files: {len(files)}, TOTAL: {total / 1e6:.1f} MB")

BS = chr(92)


def grp(path):
    p = path.lower().replace('/', BS).replace(BS + BS, BS)
    key = '.venv' + BS + 'lib' + BS + 'site-packages' + BS
    if key in p:
        return p.split(key, 1)[1].split(BS)[0]
    if (BS + 'backend' + BS) in p and p.endswith('.py'):
        return '(backend-src)'
    base = os.path.basename(p)
    if base.startswith('python') and base.endswith('.dll'):
        return '(py-runtime)'
    if 'vcruntime' in base or base.startswith('api-ms') or base.startswith('ucrt'):
        return '(py-runtime)'
    return '(other)'


sizes = {}
for sz, name, path in files:
    g = grp(path)
    sizes[g] = sizes.get(g, 0) + sz

print()
print('--- 按来源包分组 TOP 18 ---')
for g, s in sorted(sizes.items(), key=lambda x: -x[1])[:18]:
    print(f"{s / 1e6:9.1f} MB  {g}")

print()
print('--- 单文件 TOP 20 ---')
for sz, name, path in sorted(files, reverse=True)[:20]:
    print(f"{sz / 1e6:8.2f} MB  {name[:55]}  <- {grp(path)}")
