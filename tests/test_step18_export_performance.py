import openpyxl
import pytest

from tests.test_step18_workspace import workspace as workspace_factory


@pytest.fixture
def export_workspace(tmp_path):
    return workspace_factory.__wrapped__(tmp_path)


def test_fast_export_preserves_formula_like_source_text_on_every_sheet(export_workspace, monkeypatch):
    ws = export_workspace
    state = ws.open()
    state['rows'][0]['input']['标题'] = '=SUM(1,2)'
    state['rows'][0]['attributes']['材质'].update(value='=1+1', source='=SOURCE()',
                                                 basis='=HYPERLINK("https://example.com")')
    state['operation']['requirement'] = '=UNTRUSTED()'
    monkeypatch.setattr(ws, '_current', lambda: state)
    monkeypatch.setattr(ws, 'chain', lambda *a, **kw: [state])
    output = ws.export(state['version'])
    book = openpyxl.load_workbook(ws.root / '18_标准化' / output['filename'], read_only=True)
    try:
        for name, expected in [('属性标注', '=1+1'), ('来源', '=SOURCE()'),
                               ('依据', '=HYPERLINK("https://example.com")')]:
            sheet = book[name]
            rows = list(sheet.iter_rows())
            headers = [c.value for c in rows[0]]
            assert rows[1][headers.index('材质')].value == expected
            assert rows[1][headers.index('材质')].data_type == 's'
            assert rows[1][headers.index('标题')].value == '=SUM(1,2)'
            assert rows[1][headers.index('标题')].data_type == 's'
            assert len(rows) == len(state['rows']) + 1
            assert all(c.data_type != 'f' for row in rows for c in row)
        assert book['操作记录']['D2'].value == '=UNTRUSTED()'
        assert book['操作记录']['D2'].data_type == 's'
    finally:
        book.close()
