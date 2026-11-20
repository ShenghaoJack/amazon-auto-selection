"""Per-group export identity, checkpoint and business-data validation."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from core import read_json, write_json
from core.errors import EmptyExportPause, StepDataError
from steps._month_fallback import is_recent30


def identity(asins, site, period):
    return {'asins': list(asins), 'site': str(site), 'period': str(period),
            'day': date.today().isoformat() if is_recent30(period) else ''}


def identity_key(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def checksum(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def check_rolling_retry(directory, ident):
    """Do not silently reset an unfinished rolling-window retry across midnight."""
    if not ident.get('day'):
        return
    for path in (Path(directory) / '_checkpoints').glob('*.json'):
        record = read_json(str(path), default=None)
        if not isinstance(record, dict) or not isinstance(record.get('identity'), dict):
            raise StepDataError('导出断点记录损坏，无法安全判断是否存在未完成重试')
        old = record['identity']
        same = all(old.get(k) == ident.get(k) for k in ('asins', 'site', 'period'))
        if same and old.get('day') != ident['day'] and record.get('status') in ('initial_export_started', 'first_empty', 'empty_retry_started', 'paused'):
            raise EmptyExportPause('最近30天的未完成导出已跨日，无法保证重导周期与原任务相同', hint='请核对原任务和周期')


def workbook_state(path):
    """Return nonempty/empty/invalid; ignore Notes and require an ASIN column."""
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            sheets = [s for s in wb.worksheets if s.title.strip().casefold() not in ('notes', '说明', '备注')]
            if not sheets:
                return 'invalid'
            sheet = sheets[0]
            rows = sheet.iter_rows(values_only=True)
            header = next(rows, None)
            if not header or not any(cell is not None and str(cell).strip() for cell in header):
                return 'invalid' if any(any(cell is not None and str(cell).strip() for cell in row) for row in rows) else 'empty'
            columns = [i for i, cell in enumerate(header) if str(cell or '').strip().casefold() in ('asin', '父asin', 'parent asin', 'parent_asin')]
            if not columns:
                return 'invalid'
            malformed_row = False
            for row in rows:
                ids = [str(row[i]).strip().upper() for i in columns if i < len(row) and row[i] is not None]
                if any(re.fullmatch(r'[A-Z0-9]{10}', value) for value in ids):
                    return 'nonempty'
                values = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                if values and not (len(values) == 1 and ('导出时会自动' in values[0] or 'export automatically' in values[0].casefold())):
                    malformed_row = True
            return 'invalid' if malformed_row else 'empty'
        finally:
            wb.close()
    except Exception:
        return 'invalid'


class GroupCheckpoint:
    def __init__(self, directory, ident):
        self.ident = ident
        self.key = identity_key(ident)
        self.path = Path(directory) / '_checkpoints' / (self.key + '.json')

    def read(self):
        if not self.path.exists():
            return {}
        result = read_json(str(self.path), default=None)
        if not isinstance(result, dict) or result.get('identity') != self.ident:
            raise StepDataError('导出断点记录损坏或与本组输入不一致，请核对后继续')
        return result

    def save(self, **values):
        data = self.read()
        data.update(values)
        data['identity'] = self.ident
        write_json(str(self.path), data)
        return data
