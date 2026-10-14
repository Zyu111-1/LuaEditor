#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lua Table Editor 回归测试（headless，无需显示环境）。

对应 HANDOFF.md §6 验证矩阵。运行：
    python test_regressions.py
全部通过输出 "ALL PASSED"；任何失败以非零退出码结束并打印失败项。
"""
import os
import sys
import io
import traceback

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QItemSelectionModel, QSize

app = QApplication([])

from lua_table_editor import (
    LuaParser, LuaSerializer, LuaTableModel, FilterProxyModel,
    LuaTableStr, LuaTableData, RowEntry, MainWindow,
    LuaTableView, LuaTableDelegate, FastHeaderView,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

# 必须包含「表头注释 + return」——biaodan/ClothData.lua 覆盖不到这些分支
FIXTURE_HEADER = """--
-- ChangeClothData.xlsx
--
local data =
{
    [1] = {id=1, Name="a"},
    [2] = {id=2, Name="b"},
    [3] = {id=3, Name="c"},
}
return data"""

FIXTURE_PLAIN = 'T = {\n    {id=1, v=1},\n    {id=2, v=2},\n}'

RESULTS = []

def run_test(fn):
    name = fn.__name__
    try:
        fn()
        RESULTS.append((name, True, ''))
        print(f"  PASS  {name}")
    except Exception:
        err = traceback.format_exc()
        RESULTS.append((name, False, err))
        print(f"  FAIL  {name}")
        print(err)
    return


# ---------------------------------------------------------------------------
# 批次1：解析 / 序列化
# ---------------------------------------------------------------------------

def test_bom_declaration_preserved():
    """BUG-04：带 BOM 文件解析不丢表声明行，保存后 BOM 还原。"""
    src = '\ufefflocal data =\n{\n    {id=1},\n}'
    d = LuaParser.parse(src)
    assert d.has_bom is True, "has_bom 未标记"
    assert d.table_name == 'data', f"表名解析错误: {d.table_name!r}"
    assert d.is_local is True, "local 声明丢失"
    assert len(d.rows) == 1 and d.field_names == ['id']
    # 未修改：原文（含 BOM）原样返回
    out = LuaSerializer.serialize(d)
    assert out == src, "未修改路径未原样返回（含 BOM）"
    # 修改后：BOM + 声明行都必须保留
    d.rows[0].data['id'] = 2
    d.rows[0]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d)
    assert out.startswith('\ufeff'), "修改路径丢失 BOM"
    assert 'local data =' in out, "修改路径丢失表声明行"
    assert out.count('local data =') == 1, "表声明行重复"

def test_crlf_roundtrip_stable():
    """BUG-05：CRLF 文件 读→改→存 行尾不被二次翻译，且连续保存幂等。"""
    src = "T = {\r\n    {id=1},\r\n    {id=2},\r\n}\r\n"
    # 模拟 LuaLoadWorker：newline='' 读取 + 归一化
    if '\r\n' in src:
        eol = '\r\n'
    norm = src.replace('\r\n', '\n')
    d = LuaParser.parse(norm)
    d.eol = eol
    # 修改一行
    d.rows[0].data['id'] = 100
    d.rows[0]._dirty = True
    d.modified = True
    text = LuaSerializer.serialize(d)
    out = text if d.eol == '\n' else text.replace('\n', d.eol)
    assert '\r\r' not in out, f"出现 \\r\\r 双重回车: {out!r}"
    assert out.count('\r\n') == out.count('\n'), "行尾混杂（存在裸 \\n）"
    # 幂等：out 写盘后重新读入（worker 会归一化）→ 再改同一行 → 字节一致
    norm2 = out.replace('\r\n', '\n')
    d2 = LuaParser.parse(norm2)
    d2.eol = eol
    d2.rows[0].data['id'] = 100
    d2.rows[0]._dirty = True
    d2.modified = True
    text2 = LuaSerializer.serialize(d2)
    out2 = text2 if d2.eol == '\n' else text2.replace('\n', d2.eol)
    assert out == out2, "连续保存不幂等"

def test_lf_file_stays_lf():
    """BUG-05：LF 文件保存后仍是 LF（不被平台行尾污染）。"""
    d = LuaParser.parse(FIXTURE_PLAIN)
    d.eol = '\n'
    d.rows[0].data['v'] = 9
    d.rows[0]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d).replace('\n', d.eol)
    assert '\r' not in out

def test_row_key_quoting():
    """BUG-07a：非数字行键自动加引号，数字/已引号键原样。"""
    def ser(key):
        d = LuaParser.parse('T = {\n    {id=1},\n}')
        d.rows[0].row_key = key
        d.rows[0]._dirty = True
        d.modified = True
        for ln in LuaSerializer.serialize(d).split('\n'):
            if ln.startswith('\t['):
                return ln.strip()
        raise AssertionError('未生成行键行')
    assert ser('123') == '[123] = {id=1, },', ser('123')
    assert ser('1.5') == '[1.5] = {id=1, },', ser('1.5')
    assert ser('007') == '[007] = {id=1, },', ser('007')
    assert ser("'q'") == "['q'] = {id=1, },", ser("'q'")
    assert ser('my_key') == '["my_key"] = {id=1, },', ser('my_key')
    assert ser('0x10') == '[0x10] = {id=1, },', ser('0x10')

def test_backslash_escape_roundtrip():
    """BUG-07b：字面反斜杠往返不丢转义。"""
    # Lua 源码 "a\\nb" = 字符串 a\nb（含真实反斜杠字符）
    src = 'T = {\n    {id=1, n="a\\\\nb"},\n}'
    d = LuaParser.parse(src)
    val = d.rows[0].data['n']
    assert val == 'a\\nb', f"解析值错误: {val!r}"
    d.rows[0]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d)
    assert 'n="a\\\\nb"' in out, f"反斜杠未还原转义: {out!r}"
    # 往返：重新解析后再序列化，语义不变
    d2 = LuaParser.parse(out)
    assert d2.rows[0].data['n'] == 'a\\nb'

def test_table_like_string_keeps_quotes():
    """BUG-07c：内容形似表的字符串不丢引号；真子表仍裸写。"""
    src = 'T = {\n    {id=1, tag="{build}", m={1,2}},\n}'
    d = LuaParser.parse(src)
    tag = d.rows[0].data['tag']
    assert isinstance(tag, str) and not isinstance(tag, LuaTableStr), "字符串值被误判为子表"
    sub = d.rows[0].data['m']
    assert isinstance(sub, LuaTableStr), "子表值未标记为 LuaTableStr"
    d.rows[0]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d)
    assert 'tag="{build}"' in out, f"字符串丢引号: {out!r}"
    assert 'm={1,2}' in out, f"子表被误加引号: {out!r}"

def test_hex_number_not_stringified():
    """BUG-07d：十六进制数字解析为 int，保存不变成字符串。"""
    d = LuaParser.parse('T = {\n    {id=0x1F},\n}')
    v = d.rows[0].data['id']
    assert v == 31 and isinstance(v, int), f"十六进制解析错误: {v!r}"
    d.rows[0]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d)
    assert 'id=31' in out and 'id="0x1F"' not in out

def test_empty_keyed_row_is_saved():
    """BUG-11：带行键但无字段的新增行保存时落盘为 [key] = {}。"""
    d = LuaParser.parse('T = {\n    [1] = {id=1},\n}')
    m = LuaTableModel()
    m.set_data(d)
    m.add_data_row(1)
    d.modified = True
    out = LuaSerializer.serialize(d)
    assert '[2] = {}' in out, f"新增空行未落盘: {out!r}"

def test_unmodified_zero_diff():
    """INV-2：未修改时输出与原文完全一致（回归保护）。"""
    for fx in (FIXTURE_HEADER, FIXTURE_PLAIN):
        d = LuaParser.parse(fx)
        assert LuaSerializer.serialize(d) == fx, f"零 diff 破坏: {fx[:20]!r}"

def test_modified_row_minimal_diff():
    """INV-2：只改一行时，其余行保持原文。"""
    d = LuaParser.parse(FIXTURE_PLAIN)
    d.rows[1].data['v'] = 99
    d.rows[1]._dirty = True
    d.modified = True
    out = LuaSerializer.serialize(d)
    lines = out.split('\n')
    assert lines[1] == '    {id=1, v=1},', f"未修改行被重排: {lines[1]!r}"
    assert 'v=99' in lines[2]
    assert lines[3] == '}'

def test_parse_serialize_idempotent_on_real_sample():
    """幂等铁律：parse→serialize 二轮字节一致（用带表头注释+return 夹具）。"""
    d = LuaParser.parse(FIXTURE_HEADER)
    d.modified = True  # 强制走完整序列化路径
    out1 = LuaSerializer.serialize(d)
    d2 = LuaParser.parse(out1)
    d2.modified = True
    out2 = LuaSerializer.serialize(d2)
    assert out1 == out2, "二轮序列化不一致"


# ---------------------------------------------------------------------------
# 批次2/3：模型 / 视图
# ---------------------------------------------------------------------------

def test_modified_not_reset_after_structural_change():
    """BUG-02：加行→改格→撤销，modified 不得被复位（否则关闭标签页静默丢数据）。"""
    d = LuaParser.parse(FIXTURE_PLAIN)
    m = LuaTableModel()
    m.set_data(d)
    m.add_data_row(1)
    assert d.modified is True
    assert d.non_undoable_modified is True
    m.setData(m.index(0, 2), '999')          # 改 id 列旁的 v 列
    assert m.undo() is True                   # 撤销单元格编辑
    assert d.modified is True, "撤销后 modified 被错误复位，新增行将丢失"
    assert len(d.rows) == 3, "新增行丢失"

def test_modified_reset_ok_without_structural_change():
    """BUG-02 回归保护：纯单元格编辑全部撤销后允许复位（零 diff 语义保留）。"""
    d = LuaParser.parse(FIXTURE_PLAIN)
    m = LuaTableModel()
    m.set_data(d)
    m.setData(m.index(0, 2), '999')
    assert d.modified is True
    m.undo()
    assert d.modified is False, "纯编辑撤销后应回到未修改状态"

def test_blank_row_not_editable():
    """BUG-03：空行不可编辑，直接 setData 被拒绝，不产生静默丢失。"""
    d = LuaParser.parse('T = {\n    {id=1},\n\n    {id=2},\n}')
    m = LuaTableModel()
    m.set_data(d)
    assert [r.row_type for r in d.rows] == ['data', 'blank', 'data']
    # 逻辑行1 = 空行
    assert m._resolve_row(1)[0] == LuaTableModel.ROW_BLANK
    assert not (m.flags(m.index(1, 1)) & Qt.ItemIsEditable), "空行不应可编辑"
    assert m.setData(m.index(1, 1), '123') is False, "setData 应拒绝空行"
    assert d.rows[1].data == {}, "空行数据被污染"
    # 空行不占数据序号：行号列 data 行显示 1、2
    assert m.data(m.index(0, 0), Qt.DisplayRole) == '1'
    assert m.data(m.index(2, 0), Qt.DisplayRole) == '2'

def test_row_numbers_after_rowtype_change():
    """PERF-01：注释/取消注释后行号缓存正确刷新（原 O(N²) 逻辑的行为等价验证）。"""
    d = LuaParser.parse('T = {\n    {id=1},\n    {id=2},\n    {id=3},\n}')
    m = LuaTableModel()
    m.set_data(d)
    assert [m.data(m.index(i, 0), Qt.DisplayRole) for i in range(3)] == ['1', '2', '3']
    # 就地把第 2 行变成注释行（模拟 _comment_selected_rows 的效果）
    d.rows[1].row_type = 'comment'
    d.rows[1].comment_text = '-- x'
    m._rebuild_duplicates()
    assert m.data(m.index(2, 0), Qt.DisplayRole) == '2', "注释后行号未顺延"
    # 再变回数据行
    d.rows[1].row_type = 'data'
    m._rebuild_duplicates()
    assert [m.data(m.index(i, 0), Qt.DisplayRole) for i in range(3)] == ['1', '2', '3']

def test_row_numbers_after_insert_remove():
    """PERF-01：增删行后行号缓存正确。"""
    d = LuaParser.parse('T = {\n    {id=1},\n    {id=2},\n}')
    m = LuaTableModel()
    m.set_data(d)
    m.add_data_row(1)
    assert [m.data(m.index(i, 0), Qt.DisplayRole) for i in range(3)] == ['1', '2', '3']
    m.remove_row(0)
    assert [m.data(m.index(i, 0), Qt.DisplayRole) for i in range(2)] == ['1', '2']

def test_filter_no_crash_with_header_and_return():
    """BUG-01：带表头注释+return 的表启用列筛选不再 IndexError，结果正确。"""
    d = LuaParser.parse(FIXTURE_HEADER)
    m = LuaTableModel()
    m.set_data(d)
    hc = m._header_count
    p = FilterProxyModel()
    p.setSourceModel(m)
    p.set_filter(1, 'b', 'contains')     # id 列包含 'b'
    # 每个逻辑行都必须能安全判定（修复前：数据区行错位、return 行 IndexError）
    accepted = [r for r in range(m.rowCount()) if p.filterAcceptsRow(r, m.index(0, 0))]
    # id 列的值是 1/2/3，'b' 不在其中 → 只有结构行（头注释+return）通过
    assert accepted == list(range(hc)) + [m.rowCount() - 1], f"筛选结果异常: {accepted}"
    p.set_filter(1, '2', 'equals')
    # 只统计真正的数据行命中（头注释/return 等结构行设计上始终可见，不算命中）
    hits = []
    for r in range(m.rowCount()):
        if p.filterAcceptsRow(r, m.index(0, 0)):
            rt, ri = m._resolve_row(r)
            if rt == LuaTableModel.ROW_DATA:
                hits.append(ri)
    assert hits == [1], f"等于筛选命中错误: {hits}"

def test_fill_beyond_end_no_crash():
    """新发现：填充柄拖过表尾时 self._next_default_row_key 不存在导致 AttributeError。"""
    from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPoint
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    from lua_table_editor import LuaTableView
    w = MainWindow()
    d = LuaParser.parse('T = {\n    {id=1},\n}')
    tab = w._create_tab(d, 'T.lua')
    td = w._tabs_data[id(tab)]
    view, proxy = td['view'], td['proxy']
    # 选中 (0,1)（唯一数据行的 id 列）
    view.setCurrentIndex(proxy.index(0, 1))
    sel = view.selectionModel()
    sel.select(proxy.index(0, 1), QItemSelectionModel.Select | QItemSelectionModel.Current)
    view._begin_fill()
    view._fill_preview_rows = 3   # 直接拖过表尾 3 行
    view._commit_fill()
    assert len(d.rows) == 4, f"填充追加行数错误: {len(d.rows)}"
    assert d.rows[1].data.get('id') == 1 and d.rows[3].data.get('id') == 1
    assert d.non_undoable_modified is True

def test_backspace_uses_source_index():
    """BUG-10：筛选状态下 Backspace 判定/清空的是当前可见行。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    w = MainWindow()
    d = LuaParser.parse('T = {\n    {id=1, v="a"},\n    {id=2, v="b"},\n}')
    tab = w._create_tab(d, 'T.lua')
    td = w._tabs_data[id(tab)]
    view, proxy = td['view'], td['proxy']
    proxy.set_filter(1, '2', 'equals')      # 只显示 id=2 的行
    view.setCurrentIndex(proxy.index(0, 2))  # 可见行的 v 列
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace, Qt.NoModifier))
    assert d.rows[1].data.get('v', '') == '', "清空作用在了错误的行"
    assert d.rows[0].data.get('v') == 'a', "不该被清空的行被清空"

# ---------------------------------------------------------------------------
# 独立加载 / 描边 / 性能
# ---------------------------------------------------------------------------

def _wait_until(cond, timeout_ms=8000):
    """处理事件直到条件成立或超时（worker 线程异步完成需要事件循环）"""
    from PySide6.QtCore import QElapsedTimer
    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        app.processEvents()
        if cond():
            return True
        app.processEvents()
    return cond()

def test_tab_loads_async_with_placeholder():
    """独立加载：打开文件先出现占位标签页，解析完成后填充真实表格。"""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'lte_test_async.lua')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('T = {\n    {id=1},\n    {id=2},\n}\nreturn T')
    w = MainWindow()
    w._open_file_path(path)
    # 立即检查：占位页存在（loading 或已完成，取决于调度，但最终必须完成）
    ok = _wait_until(lambda: any(
        not td.get('loading') and td.get('view') is not None
        for td in w._tabs_data.values()))
    assert ok, "后台加载未在超时时间内完成"
    td = next(td for td in w._tabs_data.values()
              if td.get('file_path') == path and td.get('view') is not None)
    assert td['loading'] is False
    assert td['model'].rowCount() >= 3
    # 标签标题不应残留 ⏳ 前缀
    idx = w._tab_widget.indexOf(td['tab'])
    assert '⏳' not in w._tab_widget.tabText(idx)
    # 清理
    w._tabs_data.clear()
    for i in reversed(range(w._tab_widget.count())):
        w._tab_widget.removeTab(i)
    os.remove(path)

def test_open_same_file_while_loading_switches():
    """同一文件重复打开：加载中切到占位页，已打开切到已有页，不重复开标签。"""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), 'lte_test_dup.lua')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('T = {\n    {id=1},\n}')
    w = MainWindow()
    w._open_file_path(path)
    n1 = w._tab_widget.count()
    w._open_file_path(path)   # 占位中再次打开
    w._open_file_path(path)   # 已完成后再次打开
    assert w._tab_widget.count() == n1, "重复打开产生了多个标签页"
    _wait_until(lambda: any(
        td.get('file_path') == path and not td.get('loading')
        for td in w._tabs_data.values()))
    w._tabs_data.clear()
    for i in reversed(range(w._tab_widget.count())):
        w._tab_widget.removeTab(i)
    os.remove(path)

def test_other_tab_usable_during_load():
    """大表加载期间，其他标签页保持可用（无全局加载锁）。"""
    import tempfile
    big = os.path.join(tempfile.gettempdir(), 'lte_test_big.lua')
    with open(big, 'w', encoding='utf-8') as f:
        f.write('T = {\n' + ''.join(
            f'    [{{id={i}}}] = {{id={i}, name="n{i}", tag={{1,2,3}}}},\n'
            for i in range(20000)) + '}')
    w = MainWindow()
    # 先开一个小表并选中
    d = LuaParser.parse(FIXTURE_PLAIN)
    tab = w._create_tab(d, 'small.lua')
    td_small = w._tabs_data[id(tab)]
    w._open_file_path(big)   # 异步开始加载大表
    # 加载在途时，小表仍可编辑
    model = td_small['model']
    model.setData(model.index(0, 2), '999')
    assert d.rows[0].data.get('v') == 999, "加载期间其他标签页编辑失效"
    # 等大表加载完成
    ok = _wait_until(lambda: any(
        td.get('file_path') == big and not td.get('loading')
        for td in w._tabs_data.values()), timeout_ms=30000)
    assert ok, "大表后台加载超时"
    td_big = next(td for td in w._tabs_data.values() if td.get('file_path') == big)
    assert td_big['view'] is not None and td_big['model'].rowCount() >= 20000
    w._tabs_data.clear()
    for i in reversed(range(w._tab_widget.count())):
        w._tab_widget.removeTab(i)
    os.remove(big)

def test_duplicates_typed_key_equivalence():
    """重复值键与 _value_to_lua 语义等价：同组重复高亮，跨类型不误报。"""
    d = LuaTableData(table_name='T', field_names=['a', 'b'])
    d.rows = [
        RowEntry(row_type='data', data={'a': 1, 'b': 'x'}),
        RowEntry(row_type='data', data={'a': 1, 'b': 'x'}),   # 与上行重复
        RowEntry(row_type='data', data={'a': '1', 'b': 2}),   # 字符串'1'≠数字1
    ]
    m = LuaTableModel()
    m.set_data(d)
    assert m.is_duplicate(0, 1) and m.is_duplicate(1, 1), "同值未标重"
    assert m.is_duplicate(0, 2) and m.is_duplicate(1, 2), "同值未标重"
    assert not m.is_duplicate(2, 1), "字符串 '1' 不应与数字 1 混同"
    assert not m.is_duplicate(2, 2), "无重复值被误标"

def test_precomputed_aux_from_worker():
    """worker 预计算（compute_table_aux）→ set_data 复用，行为与同步计算一致。"""
    from lua_table_editor import compute_table_aux
    d = LuaParser.parse(FIXTURE_PLAIN)   # v 列值 1,2（无重复）
    compute_table_aux(d)
    assert d.dup_counts is not None and d.row_seq_cache is not None
    m = LuaTableModel()
    m.set_data(d)
    assert m._dups_dirty is False, "有预计算时不应再标脏"
    assert m._row_seq == d.row_seq_cache
    # 编辑制造重复：v 列（第2列）第二行改成 1 → 与第一行重复
    m.setData(m.index(1, 2), '1')
    assert m.is_duplicate(0, 2) and m.is_duplicate(1, 2), "增量更新未标重"
    # 再改回去 → 重复消失
    m.setData(m.index(1, 2), '2')
    assert not m.is_duplicate(0, 2) and not m.is_duplicate(1, 2), "增量更新未去重"

def test_mark_dirty_deferred_rebuild():
    """结构性变更后 _mark_dups_dirty：行号缓存立即正确，重复值在事件循环后修正。"""
    d = LuaParser.parse('T = {\n    {id=1, v=7},\n    {id=2, v=7},\n}')
    m = LuaTableModel()
    m.set_data(d)
    assert m.is_duplicate(0, 2) and m.is_duplicate(1, 2)
    # 模拟注释转换路径：改行类型 + 标脏
    d.rows[0].row_type = 'comment'
    d.rows[0].comment_text = '-- x'
    m._mark_dups_dirty()
    # 行号缓存立即修正（注释行不占序号）
    assert m.data(m.index(1, 0), Qt.DisplayRole) == '1', "行号缓存未同步重建"
    # 事件循环处理后重复值集合重建
    app.processEvents()
    assert m._dups_dirty is False, "脏标记未被消费"
    assert not m.is_duplicate(1, 2), "注释转换后重复值未重建"

def test_selection_outline_smoke():
    """选中描边：块选/整行选/当前格渲染不崩溃（冒烟）。"""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import QSize, QItemSelectionModel
    d = LuaParser.parse(FIXTURE_HEADER)
    m = LuaTableModel()
    m.set_data(d)
    p = FilterProxyModel()
    p.setSourceModel(m)
    from lua_table_editor import LuaTableView
    view = LuaTableView()
    view.setModel(p)
    from lua_table_editor import LuaTableDelegate
    view.setItemDelegate(LuaTableDelegate())
    view.resize(600, 300)
    view.show()
    # 块选 2x2
    sm = view.selectionModel()
    sm.select(p.index(1, 1), QItemSelectionModel.Select)
    sm.select(p.index(2, 2), QItemSelectionModel.Select | QItemSelectionModel.Current)
    img = QImage(QSize(600, 300), QImage.Format_ARGB32)
    view.render(img)
    # 整行选
    view.selectRow(1)
    view.render(img)
    view.hide()

def test_auto_resize_samples_not_full_scan():
    """自适应列宽：抽样完成且列宽落在 [100, 280] 区间。"""
    d = LuaTableData(table_name='T', field_names=['short', 'wide'])
    for i in range(2000):
        d.rows.append(RowEntry(row_type='data',
                               data={'short': i, 'wide': 'W' * 400 + str(i)}))
    m = LuaTableModel()
    m.set_data(d)
    p = FilterProxyModel()
    p.setSourceModel(m)
    from lua_table_editor import LuaTableView
    view = LuaTableView()
    view.setModel(p)
    w = MainWindow.__new__(MainWindow)  # 只用 _auto_resize_columns，绕过完整初始化
    w._tabs_data = {1: {'view': view}}
    w._auto_resize_columns(view)
    header = view.horizontalHeader()
    for col in range(1, header.count()):
        size = header.sectionSize(col)
        assert 100 <= size <= 280, f"列 {col} 宽度越界: {size}"

# ---------------------------------------------------------------------------
# 整列选择性能（Qt 表头逐行选区查询陷阱）
# ---------------------------------------------------------------------------

def test_selection_helpers_equivalence():
    """selection_rows/cells/source_rows 与 selectedIndexes 语义一致（含筛选映射）。"""
    import time
    from lua_table_editor import (selection_rows, selection_cells,
                                  selection_source_rows)
    d = LuaParser.parse('T = {\n    {id=1, v="a"},\n    {id=2, v="b"},\n'
                        '    {id=3, v="c"},\n}')
    m = LuaTableModel()
    m.set_data(d)
    p = FilterProxyModel()
    p.setSourceModel(m)
    view = LuaTableView()
    view.setModel(p)
    sm = view.selectionModel()
    # 块选 (0,1)-(1,2)。注意不能用 Select|Current——那等于 SelectCurrent，
    # 选的是"当前索引"而非传入索引，会把已有选择清空
    sm.select(p.index(0, 1), QItemSelectionModel.Select)
    sm.select(p.index(1, 2), QItemSelectionModel.Select)
    sm.setCurrentIndex(p.index(1, 2), QItemSelectionModel.Current)
    rows = selection_rows(sm)
    assert rows == [0, 1], f"selection_rows 错: {rows}"
    cells = selection_cells(sm)
    # 两次 Select 产生两个单格 range，逐 range 枚举即得两格
    assert cells == [(0, 1), (1, 2)], f"selection_cells 错: {cells}"
    src = selection_source_rows(view, p)
    assert src == [0, 1], f"selection_source_rows 错: {src}"
    # 大选区下必须远快于 selectedIndexes（5 万行整列：~800ms vs ~5ms）
    big = LuaTableData(table_name='T', field_names=['id'])
    for i in range(20000):
        big.rows.append(RowEntry(row_type='data', data={'id': i}))
    m2 = LuaTableModel()
    m2.set_data(big)
    p2 = FilterProxyModel()
    p2.setSourceModel(m2)
    v2 = LuaTableView()
    v2.setModel(p2)
    sm2 = v2.selectionModel()
    sm2.select(p2.index(0, 1), QItemSelectionModel.Select | QItemSelectionModel.Columns)
    t0 = time.perf_counter()
    r = selection_rows(sm2)
    t_ours = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = sm2.selectedIndexes()
    t_qt = time.perf_counter() - t0
    assert len(r) == 20000
    assert t_ours < t_qt / 5, f"selection_rows 未显著快于 selectedIndexes: {t_ours:.4f}s vs {t_qt:.4f}s"

def test_column_selection_repaint_fast():
    """整列选中后的重绘必须快（修复前 5 万行一次 render 约 10 秒，UI 卡死）。"""
    import time
    from lua_table_editor import FastHeaderView
    from PySide6.QtGui import QImage
    n = 20000
    d = LuaTableData(table_name='T', field_names=['id', 'name', 'type'])
    for i in range(n):
        d.rows.append(RowEntry(row_type='data',
                               data={'id': i + 1, 'name': f'i{i}', 'type': i % 9}))
    m = LuaTableModel()
    m.set_data(d)
    p = FilterProxyModel()
    p.setSourceModel(m)
    view = LuaTableView()
    view.setModel(p)
    view.setItemDelegate(LuaTableDelegate())
    view.setHorizontalHeader(FastHeaderView(Qt.Horizontal, view))
    view.resize(900, 500)
    view.show()
    img = QImage(QSize(900, 500), QImage.Format_ARGB32)
    sm = view.selectionModel()
    sm.select(p.index(0, 1), QItemSelectionModel.Select | QItemSelectionModel.Columns)
    app.processEvents()
    t0 = time.perf_counter()
    view.render(img)
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"整列选中重绘过慢: {dt*1000:.0f}ms（Qt 表头逐行选区查询未绕过）"
    view.hide()

def test_header_click_selects_column():
    """点击列头会选中整列（Qt 内置行为），且走 FastHeaderView 不卡顿。"""
    import time
    from PySide6.QtGui import QImage, QMouseEvent
    from PySide6.QtCore import QEvent, QPoint
    w = MainWindow()
    d = LuaParser.parse('T = {\n    {id=1, v="a"},\n    {id=2, v="b"},\n}')
    tab = w._create_tab(d, 'T.lua')
    td = w._tabs_data[id(tab)]
    view, proxy = td['view'], td['proxy']
    assert isinstance(view.horizontalHeader(), FastHeaderView), "未安装 FastHeaderView"
    hdr = view.horizontalHeader()
    x = hdr.sectionViewportPosition(2) + 5
    for etype in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        ev = QMouseEvent(etype, QPoint(x, 5), Qt.LeftButton,
                         Qt.LeftButton, Qt.NoModifier)
        app.sendEvent(hdr.viewport(), ev)
    app.processEvents()
    ranges = view.selectionModel().selection()
    assert len(ranges) == 1 and ranges[0].left() == 2 and ranges[0].right() == 2, \
        f"点击列头未选中整列: {[(r.top(), r.bottom(), r.left(), r.right()) for r in ranges]}"
    # 选中态下重绘不卡
    img = QImage(QSize(600, 300), QImage.Format_ARGB32)
    t0 = time.perf_counter()
    view.render(img)
    assert time.perf_counter() - t0 < 0.5, "列头选中后重绘过慢"

def test_flags_cache_invalidation():
    """flags() 记忆化必须在结构/行类型变化后失效（否则可编辑性判定错乱）。"""
    d = LuaParser.parse('T = {\n    {id=1},\n\n    {id=2},\n}')
    m = LuaTableModel()
    m.set_data(d)
    assert m._resolve_row(1)[0] == LuaTableModel.ROW_BLANK
    # 空行不可编辑
    assert not (m.flags(m.index(1, 1)) & Qt.ItemIsEditable)
    # 缓存里有这一项
    assert (1, 1) in m._flags_cache
    # 就地转成注释行后，缓存必须失效：注释行第 1 列可编辑
    # （逻辑行 1 对应 data.rows[1]，即那个空行）
    d.rows[1].row_type = 'comment'
    d.rows[1].comment_text = '-- x'
    m._mark_dups_dirty()
    assert (1, 1) not in m._flags_cache, "行类型变化后 flags 缓存未失效"
    assert m.flags(m.index(1, 1)) & Qt.ItemIsEditable, "注释行第 1 列应可编辑"

# ---------------------------------------------------------------------------
# 批次4：主窗口
# ---------------------------------------------------------------------------

def _open_fixture(fixture, name='t.lua'):
    w = MainWindow()
    d = LuaParser.parse(fixture)
    tab = w._create_tab(d, name)
    td = w._tabs_data[id(tab)]
    return w, d, td

def test_comment_rows_on_header_table():
    """BUG-01 端到端：带表头注释的表，注释选中行作用于正确的数据行。"""
    from PySide6.QtCore import QItemSelectionModel
    w, d, td = _open_fixture(FIXTURE_HEADER)
    view, proxy, m = td['view'], td['proxy'], td['model']
    hc = m._header_count
    view.selectionModel().select(proxy.index(hc, 0),
                                 QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert w._check_selected_row_types() == (True, False), \
        f"类型判断错误: {w._check_selected_row_types()}"
    w._comment_selected_rows()
    assert d.rows[0].row_type == 'comment', "第 1 条数据行未被注释"
    assert d.rows[1].row_type == 'data', "第 2 条数据行被误注释"
    assert d.rows[0].row_key == '1', "注释时行键丢失"

def test_uncomment_rows_on_header_table():
    """BUG-01 端到端：带表头注释的表，取消注释作用于正确的行。"""
    from PySide6.QtCore import QItemSelectionModel
    w, d, td = _open_fixture(FIXTURE_HEADER)
    view, proxy, m = td['view'], td['proxy'], td['model']
    hc = m._header_count
    # 先把第 1 条数据行变成注释行（直接操作数据，模拟历史状态）
    d.rows[0] = RowEntry(row_type='comment', comment_text='-- {id=1, Name="a"}', row_key='1')
    m.safe_notify_reset()
    view.selectionModel().select(proxy.index(hc, 0),
                                 QItemSelectionModel.Select | QItemSelectionModel.Rows)
    w._uncomment_selected_rows()
    assert d.rows[0].row_type == 'data', "第 1 行未被恢复为数据行"
    assert d.rows[0].data.get('id') == 1, "恢复的数据内容错误"

def test_add_rows_appends_at_end_with_header():
    """BUG-06：带表头注释的表无选中时，批量添加行应追加到表尾（return 之前）。"""
    w, d, td = _open_fixture(FIXTURE_HEADER)
    td['view'].selectionModel().clearSelection()
    w._do_add_rows(2)
    assert len(d.rows) == 5, f"追加位置错误，行数: {len(d.rows)}"
    assert d.rows[3].row_type == 'data' and d.rows[4].row_type == 'data'
    # 原第 3 条数据行保持在原位（此前会被插到表首）
    assert d.rows[2].data.get('id') == 3

def test_repair_keys_skips_duplicates():
    """BUG-25：修复行键遇到与现有键重复的 id 时跳过，不生成重复键。"""
    src = 'T = {\n    [10] = {id=10},\n    {id=10},\n    {id=20},\n}'
    w, d, td = _open_fixture(src)
    from PySide6.QtWidgets import QMessageBox
    msgs = []
    orig = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: msgs.append(a[2] if len(a) > 2 else ''))
    try:
        w._repair_row_keys()
    finally:
        QMessageBox.information = orig
    assert d.rows[1].row_key == '', "与现有键 [10] 重复，应跳过"
    assert d.rows[2].row_key == '20', "无冲突的 id=20 应被恢复"
    assert any('重复' in m for m in msgs), "应提示存在冲突"

def test_search_options_take_effect():
    """BUG-14：区分大小写 / 单列搜索选项生效。"""
    src = 'T = {\n    {id=1, n="one"},\n    {id=2, n="1two"},\n}'
    d = LuaParser.parse(src)
    m = LuaTableModel()
    m.set_data(d)
    p = FilterProxyModel()
    p.setSourceModel(m)
    p.set_search('one', case_sensitive=True, all_columns=True)
    assert p.search_count == 1
    p.set_search('ONE', case_sensitive=True, all_columns=True)
    assert p.search_count == 0, "区分大小写未生效"
    p.set_search('ONE', case_sensitive=False, all_columns=True)
    assert p.search_count == 1
    # 单列模式：只搜 n 列（列 2），'1' 不应命中 id=1 的行
    p.set_search('1', case_sensitive=False, all_columns=False, column=2)
    assert p.search_count == 1, f"单列搜索结果错误: {p.search_count}"
    p.set_search('1', case_sensitive=False, all_columns=True)
    assert p.search_count == 2, "全列搜索结果错误"

def test_statusbar_counts_match_view():
    """BUG-16：状态栏行数与视图 rowCount 口径一致。"""
    w, d, td = _open_fixture(FIXTURE_HEADER)
    w._update_statusbar()
    expected = td['model'].rowCount()
    assert f'共 {expected} 行' in w._status_label.text(), \
        f"状态栏: {w._status_label.text()!r}，rowCount: {expected}"

def test_paste_full_row_skips_structural_rows():
    """BUG-12：整行粘贴选区含结构行时不漂移。"""
    from PySide6.QtCore import QItemSelectionModel
    w, d, td = _open_fixture(FIXTURE_HEADER)
    view, proxy, m = td['view'], td['proxy'], td['model']
    hc = m._header_count
    view.selectionModel().select(proxy.index(hc, 0),
                                 QItemSelectionModel.Select | QItemSelectionModel.Rows)
    entries = [{'__row_type': 'data', '__row_key': '9', 'id': '9', 'Name': '"z"'}]
    w._paste_internal(view, m, d, proxy, list(d.field_names), entries)
    assert len(d.rows) == 3, f"粘贴后行数错误: {len(d.rows)}"
    assert d.rows[0].data.get('id') == 9 and d.rows[0].row_key == '9'
    assert d.rows[1].data.get('id') == 2 and d.rows[2].data.get('id') == 3

def test_paste_into_blank_row_kept():
    """BUG-12 关联：粘贴到空行目标时就地转换，内容不丢失。"""
    src = 'T = {\n    {id=1},\n\n    {id=2},\n}'
    w, d, td = _open_fixture(src)
    view, proxy, m = td['view'], td['proxy'], td['model']
    from PySide6.QtCore import QItemSelectionModel
    # 该夹具无表头注释（hc=0），空行在逻辑行 1
    view.selectionModel().select(proxy.index(1, 1), QItemSelectionModel.Select)
    entries = [{'__row_type': 'data', '__row_key': '5', 'id': '5'}]
    w._paste_internal(view, m, d, proxy, list(d.field_names), entries)
    assert d.rows[1].row_type == 'data', "空行未转换为数据行"
    assert d.rows[1].data.get('id') == 5, "粘贴内容丢失"

def test_new_table_has_id_column():
    """BUG-27：新建表默认带 id 列，可直接编辑。"""
    w = MainWindow()
    w._new_table()
    d = w._get_data()
    assert d.field_names == ['id'], f"新表字段: {d.field_names}"

def test_comment_text_normalization():
    """新发现：注释行编辑后必须保持 '--' 形态，否则保存再加载时行被丢弃。"""
    src = 'T = {\n    -- {id=1}\n    {id=2},\n}'
    d = LuaParser.parse(src)
    m = LuaTableModel()
    m.set_data(d)
    # data.rows[0] 是注释行；把文本改成裸内容（如 '999999'）
    ok = m.setData(m.index(0, 1), '999999')
    assert ok is True
    assert d.rows[0].comment_text == '-- 999999', \
        f"注释形态未保持: {d.rows[0].comment_text!r}"
    # 往返：保存→重新解析，注释行不丢失
    d.modified = True
    out = LuaSerializer.serialize(d)
    d2 = LuaParser.parse(out)
    comments = [r for r in d2.rows if r.row_type == 'comment']
    assert len(comments) == 1, f"注释行在往返中丢失: {out!r}"
    assert comments[0].comment_text == '-- 999999'


def main():
    tests = [
        test_bom_declaration_preserved,
        test_crlf_roundtrip_stable,
        test_lf_file_stays_lf,
        test_row_key_quoting,
        test_backslash_escape_roundtrip,
        test_table_like_string_keeps_quotes,
        test_hex_number_not_stringified,
        test_empty_keyed_row_is_saved,
        test_unmodified_zero_diff,
        test_modified_row_minimal_diff,
        test_parse_serialize_idempotent_on_real_sample,
        test_modified_not_reset_after_structural_change,
        test_modified_reset_ok_without_structural_change,
        test_blank_row_not_editable,
        test_row_numbers_after_rowtype_change,
        test_row_numbers_after_insert_remove,
        test_filter_no_crash_with_header_and_return,
        test_fill_beyond_end_no_crash,
        test_backspace_uses_source_index,
        test_comment_rows_on_header_table,
        test_uncomment_rows_on_header_table,
        test_add_rows_appends_at_end_with_header,
        test_repair_keys_skips_duplicates,
        test_search_options_take_effect,
        test_statusbar_counts_match_view,
        test_paste_full_row_skips_structural_rows,
        test_paste_into_blank_row_kept,
        test_new_table_has_id_column,
        test_comment_text_normalization,
        test_tab_loads_async_with_placeholder,
        test_open_same_file_while_loading_switches,
        test_other_tab_usable_during_load,
        test_duplicates_typed_key_equivalence,
        test_precomputed_aux_from_worker,
        test_mark_dirty_deferred_rebuild,
        test_selection_outline_smoke,
        test_auto_resize_samples_not_full_scan,
        test_selection_helpers_equivalence,
        test_column_selection_repaint_fast,
        test_header_click_selects_column,
        test_flags_cache_invalidation,
    ]
    print("=" * 68)
    print("Lua Table Editor 回归测试")
    print("=" * 68)
    for t in tests:
        run_test(t)
    failed = [r for r in RESULTS if not r[1]]
    print("-" * 68)
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        sys.exit(1)
    print("ALL PASSED")


if __name__ == '__main__':
    main()
