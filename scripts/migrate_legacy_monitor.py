"""Update only a copied project's live status numbering, retaining original files."""
from __future__ import annotations
import csv
import io
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _store(path, text):
    tmp=path.with_name(path.name+'.migration-tmp')
    tmp.write_text(text,encoding='utf-8',newline='')
    os.replace(tmp,path)

def migrate(mdir: Path):
    status=mdir/'step-status.txt'
    marker=mdir/'no-voc-migration.json'
    if not status.is_file() or marker.exists(): return 'skipped'
    text=status.read_text(encoding='utf-8-sig')
    lines=text.splitlines()
    nums=[line.split('|',1)[0] for line in lines if '|' in line]
    if '19' not in nums and '20' not in nums: return 'skipped'
    if nums.count('19')>1 or nums.count('20')>1:
        raise ValueError(f'重复的旧步骤记录: {status}')
    if '19' in nums and '20' not in nums and not (mdir.parent/'19_VOC报告').exists():
        raise ValueError(f'无法确认旧步骤19含义，拒绝自动迁移: {status}')
    pairs=[]
    for line in lines:
        num=line.split('|',1)[0]
        if num=='19': continue
        if num=='20': line='19'+line[2:]
        pairs.append(line)
    out='\n'.join(pairs)+'\n'
    original=mdir/'step-status.before-no-voc.txt'
    if original.exists(): raise FileExistsError(f'备份已存在，拒绝覆盖: {original}')
    duration=mdir/'step-duration.csv'
    duration_backup=mdir/'step-duration.before-no-voc.csv'
    new_duration=None
    if duration.exists():
        if duration_backup.exists(): raise FileExistsError(f'备份已存在，拒绝覆盖: {duration_backup}')
        content=duration.read_text(encoding='utf-8-sig')
        rows=list(csv.reader(io.StringIO(content)))
        if rows:
            header=rows[0]
            if '步骤' not in header: raise ValueError(f'无法识别时长文件: {duration}')
            pos=header.index('步骤'); result=[header]
            for row in rows[1:]:
                if len(row)<=pos: result.append(row); continue
                if row[pos].strip()=='19': continue
                if row[pos].strip()=='20': row[pos]='19'
                result.append(row)
            buf=io.StringIO(newline=''); csv.writer(buf,lineterminator='\n').writerows(result)
            new_duration='\ufeff'+buf.getvalue()
    shutil.copy2(status,original)
    if duration.exists(): shutil.copy2(duration,duration_backup)
    _store(status,out)
    if new_duration is not None: _store(duration,new_duration)
    _store(marker,json.dumps({'schema':1,'original_status':original.name,'original_duration':duration_backup.name if duration.exists() else None,'note':'旧19为VOC；旧20为备份与体检；原文件已保留。'},ensure_ascii=False,indent=2))
    return 'migrated'

def main():
    if not (ROOT/'.no-voc-copy').is_file():
        raise SystemExit('缺少独立副本标识，拒绝运行。')
    for category in sorted((ROOT/'data').iterdir()):
        if not category.is_dir() or not (category/'config.yaml').is_file(): continue
        mdir=category/'20_备份与监控'
        outcome=migrate(mdir)
        if outcome=='migrated':print(category.name, outcome)

if __name__=='__main__': main()
