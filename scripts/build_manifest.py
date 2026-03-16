"""
Build botc_scripts/manifest.json from all JSON files in botc_scripts/.

Usage:
    python scripts/build_manifest.py

Put new script JSONs into botc_scripts/, run this script, then git push.
"""
import json
import os
import glob
import sys

scripts_dir = os.path.join(os.path.dirname(__file__), '..', 'botc_scripts')
output = []

for filepath in sorted(glob.glob(os.path.join(scripts_dir, '**', '*.json'), recursive=True)):
    if os.path.basename(filepath) == 'manifest.json':
        continue
    try:
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f'跳过 {os.path.basename(filepath)}: {e}', file=sys.stderr)
        continue

    if not isinstance(data, list):
        print(f'跳过 {os.path.basename(filepath)}: 不是数组格式', file=sys.stderr)
        continue

    meta = next((e for e in data if isinstance(e, dict) and e.get('id') == '_meta'), {})
    chars = [
        e for e in data
        if isinstance(e, dict)
        and e.get('id') != '_meta'
        and e.get('team') not in ('fabled', 'a jinxed')
        and e.get('name')
    ]

    output.append({
        'name':      meta.get('name') or os.path.splitext(os.path.basename(filepath))[0],
        'author':    meta.get('author', ''),
        'logo':      meta.get('logo', ''),
        'file':      os.path.relpath(filepath, scripts_dir).replace(os.sep, '/'),
        'charNames': [c['name'] for c in chars if c.get('name')],
    })

output.sort(key=lambda x: x['name'])

out_path = os.path.join(scripts_dir, 'manifest.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f'✅ 生成 manifest.json，共 {len(output)} 个剧本')
