#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lua Table Editor - 游戏策划 Lua 表编辑工具
支持多标签页、Excel 风格编辑、注释行、查找筛选、详情面板
"""

import sys
import os
import re
import json
import subprocess
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QTableView, QHeaderView, QToolBar, QMenuBar, QMenu,
    QStatusBar, QLabel, QPushButton, QLineEdit, QComboBox,
    QDialog, QDialogButtonBox, QCheckBox, QFormLayout, QMessageBox,
    QFileDialog, QAbstractItemView, QSplitter, QFrame, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QSizePolicy,
    QScrollArea, QInputDialog, QToolTip, QStackedWidget, QTextBrowser, QTextEdit,
    QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QColorDialog, QProgressBar
)
from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    Signal, Slot, QSize, QRect, QPoint, QEvent, QTimer, QSettings,
    QUrl, QMimeData, QThread, QObject
)
from PySide6.QtGui import (
    QAction, QKeySequence, QColor, QFont, QBrush, QPainter,
    QPalette, QPen, QIcon, QDragEnterEvent, QDropEvent,
    QTextOption, QFontMetrics, QDesktopServices, QCursor
)

# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class RowEntry:
    """表格中的一行数据"""
    row_type: str = "data"     # "data" 或 "comment"
    comment_text: str = ""     # 注释行文本
    data: dict = field(default_factory=dict)  # 数据行内容 {field_name: value}
    row_key: str = ""          # Lua 表中的键（如 "1", "101", "'name'"），空=无显式键
    _raw_lines: list = field(default_factory=list)  # 原始文件行文本（用于精确还原）
    _dirty: bool = False       # 行数据是否被修改过


@dataclass
class LuaTableData:
    """一个 Lua 表的完整数据"""
    table_name: str = "NewTable"
    field_names: list = field(default_factory=list)  # 所有字段名（列头）
    rows: list = field(default_factory=list)          # list[RowEntry]
    file_path: str = ""                                # 原始文件路径
    modified: bool = False                             # 是否已修改
    # ---- 文件级别元信息（保留原文件结构）----
    header_comments: list = field(default_factory=list)  # 表声明之前的注释行
    is_local: bool = False                                # 是否使用 local 声明
    return_var: str = ""                                  # 末尾 return 的变量名（空=无）
    _raw_text: str = ""                                   # 完整原始文件文本（未修改时直接返回）
    # ---- 修改模式下保留的原始结构文本（零 diff 关键）----
    decl_raw: str = ""        # 表声明行原文（如 "local X = {" 或 "return {"），逐字保留
    close_raw: str = ""       # 表体结束的 "}" 行原文（含缩进/尾随注释），逐字保留
    footer_raw: list = field(default_factory=list)  # "}" 之后到文件末尾的所有行（return、尾注释等），逐字保留
    # ---- 磁盘文件元信息（用于自动刷新判断）----
    file_mtime: float = 0.0   # 最近一次从磁盘读取/写入时的文件修改时间


# ============================================================================
# Lua 解析器
# ============================================================================

class LuaParser:
    """解析 Lua 表代码为结构化数据"""

    # 类级别常量：预编译正则（避免每次 parse() 重新编译）
    _TABLE_PATTERN = re.compile(r'^\s*(?:local\s+)?(\w[\w.]*)\s*=\s*\{')
    _DECL_PATTERN = re.compile(r'^\s*(local\s+)?(\w[\w.]*)\s*=\s*$')  # local Name = (换行后 {)
    _RETURN_PATTERN = re.compile(r'^\s*return\s+(\w[\w.]*)\s*$')      # return data
    _ROW_KEY_PATTERN = re.compile(r'^\s*\[(.+?)\]\s*=\s*\{')          # [1] = { ... } 或 ['k'] = { ... }

    @staticmethod
    def parse(text: str) -> LuaTableData:
        data = LuaTableData()
        data._raw_text = text
        raw_lines = text.split('\n')
        lines = text.strip().split('\n')

        # ---- 找表声明：匹配 Name = {  或  local Name = {  或  return {  ----
        table_start = -1
        table_name = "Table"
        is_local = False

        for i, line in enumerate(lines):
            m = LuaParser._TABLE_PATTERN.match(line)
            if m:
                table_name = m.group(1)
                table_start = i
                is_local = 'local' in line.lower()
                break
            # 检查是否 "local Name =" 单独一行，下一行是 "{"
            m2 = LuaParser._DECL_PATTERN.match(line)
            if m2:
                if i + 1 < len(lines) and lines[i + 1].strip() == '{':
                    table_name = m2.group(2)
                    table_start = i + 1  # { 所在行作为表起始
                    is_local = m2.group(1) is not None
                    # 把声明行内容存入 header_comments（不是注释，但是文件头部）
                    # 将声明行之前的行和声明行本身都标记为 header
                    for k in range(0, i + 1):
                        data.header_comments.append(lines[k])
                    break

        if table_start == -1:
            # 尝试匹配 return {  或单纯的 {
            for i, line in enumerate(lines):
                s = line.strip()
                if s == '{' or s.startswith('{') or s == 'return {' or s.startswith('return {'):
                    table_start = i
                    if 'return' in s:
                        table_name = "ReturnTable"
                    break

        data.table_name = table_name
        data.is_local = is_local
        if table_start >= 0:
            data.decl_raw = lines[table_start]
        if table_start == -1:
            return data

        # ---- 收集表声明之前的注释行 ---- 
        if not data.header_comments:
            for i in range(0, table_start):
                s = lines[i].strip()
                if s.startswith('--') or s == '':
                    data.header_comments.append(lines[i])

        # ---- 找到最外层 { 对应的 }，提取表体部分 ----
        # 从 table_start 行开始，找到第一个 {，然后用深度计数找匹配的 }
        body_start_line = -1   # 第一个 { 所在行（在 table_start 行或之后）
        body_start_col = -1
        body_end_line = -1
        body_end_col = -1

        depth = 0
        started = False
        for i in range(table_start, len(lines)):
            line = lines[i]
            in_string = False
            sq = ''
            escape = False
            for j, ch in enumerate(line):
                if escape:
                    escape = False
                    continue
                if ch == '\\' and in_string:
                    escape = True
                    continue
                if ch in ('"', "'") and not in_string:
                    in_string = True
                    sq = ch
                elif ch == sq and in_string:
                    in_string = False
                elif ch == '{' and not in_string:
                    if not started:
                        started = True
                        body_start_line = i
                        body_start_col = j
                    depth += 1
                elif ch == '}' and not in_string:
                    depth -= 1
                    if depth == 0 and started:
                        body_end_line = i
                        body_end_col = j
                        break
            if body_end_line >= 0:
                break

        if body_end_line < 0:
            # 没找到匹配的 }，尝试把整个文件当表体
            body_end_line = len(lines) - 1

        # 保留表体结束行（"}"）与文件尾部（return、尾注释等）的原始文本，用于零 diff 还原
        if body_end_line >= 0:
            data.close_raw = lines[body_end_line]
            data.footer_raw = lines[body_end_line + 1:]

        # ---- 扫描 } 之后的行，查找 return xxx 语句 ----
        for i in range(body_end_line + 1, len(lines)):
            s = lines[i].strip()
            if s == '':
                continue
            m_ret = LuaParser._RETURN_PATTERN.match(s)
            if m_ret:
                data.return_var = m_ret.group(1)
            # 忽略其他内容（如注释）

        # ---- 提取表体内容：body_start_line 行 { 之后 到 body_end_line 行 } 之前 ----
        raw_rows = []   # ('comment', text) 或 ('data', text, row_key)
        all_fields_set: set = set()        # 字段名集合（O(1) 查找）

        # ---- 预处理：将跨行条目合并为完整的逻辑行 ----
        # 例如 [key] = {..., SetSubID=\n{...},\n} 应合并为一行
        def _build_logical_lines(start_idx: int, end_idx: int) -> list:
            """将物理行合并为逻辑行。

            返回 list of (stripped_text: str, raw_lines: list[str])
            raw_lines 保存原始文件行的完整文本，用于精确还原。"""
            result = []
            buf = ''
            buf_lines = []   # 组成 buf 的原始行
            d_brace = 0
            d_bracket = 0

            def _calc_depths(text: str):
                """返回 (brace_delta, bracket_delta)，忽略字符串内的括号"""
                db, dk = 0, 0
                ins, sq, esc = False, '', False
                for c in text:
                    if esc:
                        esc = False; continue
                    if c == '\\' and ins:
                        esc = True; continue
                    if c in ('"', "'") and not ins:
                        ins = True; sq = c
                    elif c == sq and ins:
                        ins = False
                    elif c == '{' and not ins:
                        db += 1
                    elif c == '}' and not ins:
                        db -= 1
                    elif c == '[' and not ins:
                        dk += 1
                    elif c == ']' and not ins:
                        dk -= 1
                return db, dk

            for i in range(start_idx, end_idx + 1):
                raw = lines[i]
                s = raw.strip()

                if i == start_idx:
                    after = raw[body_start_col + 1:].strip()
                    if after:
                        result.append((after, [raw]))
                    continue
                if i == end_idx:
                    before = raw[:body_end_col].strip()
                    if buf:
                        comb = (buf + ' ' + before).strip()
                        if comb:
                            result.append((comb, buf_lines + [raw]))
                        buf = ''; buf_lines = []; d_brace = 0; d_bracket = 0
                    elif before:
                        result.append((before, [raw]))
                    continue

                if s == '':
                    if buf:
                        result.append((buf, buf_lines))
                        buf = ''; buf_lines = []
                    result.append(('', [raw]))
                    continue

                db, dk = _calc_depths(s)

                if not buf:
                    if s.endswith('=') or db != 0 or dk != 0:
                        buf = s
                        buf_lines = [raw]
                        d_brace = db
                        d_bracket = dk
                    else:
                        result.append((s, [raw]))
                else:
                    buf += ' ' + s
                    buf_lines.append(raw)
                    d_brace += db
                    d_bracket += dk
                    if d_brace == 0 and d_bracket == 0:
                        result.append((buf, buf_lines))
                        buf = ''; buf_lines = []; d_brace = 0; d_bracket = 0

            if buf:
                result.append((buf, buf_lines))
            return result

        for stripped, raw_src_lines in _build_logical_lines(body_start_line, body_end_line):
            if stripped == '':
                raw_rows.append(('blank', raw_src_lines))
            else:
                LuaParser._classify_line(stripped, raw_rows, all_fields_set, raw_src_lines)

        # 设置字段名列顺序：按字段首次出现的行号+在该行中的位置排序（与文本中看到的顺序一一对应）
        field_first_seen = {}  # field_name -> (row_index, position)
        data_row_idx = 0
        for item in raw_rows:
            if item[0] == 'data':
                fields = LuaParser._extract_fields(item[1])
                for pos, f in enumerate(fields):
                    if f not in field_first_seen:
                        field_first_seen[f] = (data_row_idx, pos)
                data_row_idx += 1

        field_list = sorted(all_fields_set, key=lambda f: field_first_seen.get(f, (99999, 99999)))
        if 'id' in all_fields_set:
            data.field_names = ['id'] + [f for f in field_list if f != 'id']
        else:
            data.field_names = field_list

        # 解析每一行
        for item in raw_rows:
            row_type = item[0]
            if row_type == 'blank':
                raw_lines = item[1] if len(item) > 1 else ['']
                data.rows.append(RowEntry(row_type='blank', _raw_lines=raw_lines))
            elif row_type == 'comment':
                raw_lines = item[2] if len(item) > 2 else [item[1]]
                data.rows.append(RowEntry(row_type='comment', comment_text=item[1], _raw_lines=raw_lines))
            else:
                row_text = item[1]
                row_key = item[2] if len(item) > 2 else ''
                raw_lines = item[3] if len(item) > 3 else [item[1]]
                kv = LuaParser._parse_kv_pairs(row_text)
                data.rows.append(RowEntry(row_type='data', data=kv, row_key=row_key, _raw_lines=raw_lines))

        return data

    @staticmethod
    def _classify_line(stripped: str, raw_rows: list, all_fields_set: set, raw_src_lines: list = None):
        """分类一行内容：注释或数据，同时提取行键（如 [1]）"""
        if raw_src_lines is None:
            raw_src_lines = [stripped]
        row_key = ''
        if stripped.startswith('['):
            m = LuaParser._ROW_KEY_PATTERN.match(stripped)
            if m:
                row_key = m.group(1)
        if stripped.startswith('--'):
            raw_rows.append(('comment', stripped, raw_src_lines))
            return
        if '{' in stripped and '}' in stripped:
            raw_rows.append(('data', stripped, row_key, raw_src_lines))
            fields = LuaParser._extract_fields(stripped)
            for f in fields:
                all_fields_set.add(f)
        elif '=' in stripped:
            raw_rows.append(('data', stripped, row_key, raw_src_lines))
            fields = LuaParser._extract_fields(stripped)
            for f in fields:
                all_fields_set.add(f)

    @staticmethod
    @lru_cache(maxsize=8192)
    def _extract_fields(line: str) -> list:
        """从一行数据中提取所有字段名，保持出现顺序。
        用 lru_cache 缓存：parse 中同一行会被分类与字段排序两次调用，命中缓存可省一半开销。"""
        fields_dict = {}  # 使用 dict 实现 O(1) 去重，保持 Python 3.7+ 插入顺序
        content = LuaParser._get_brace_content(line)
        if not content:
            return []

        pairs = LuaParser._split_by_comma(content)
        for pair in pairs:
            if '=' in pair and not pair.strip().startswith('['):
                key = pair.split('=', 1)[0].strip()
                if key and not key.startswith('--'):
                    fields_dict[key] = True
        return list(fields_dict.keys())

    @staticmethod
    def _get_brace_content(line: str) -> str:
        """获取行中最外层 { } 内的内容"""
        start = line.find('{')
        end = line.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return ''
        return line[start + 1:end]

    @staticmethod
    def _split_by_comma(text: str) -> list:
        """按逗号分割，忽略字符串和嵌套花括号内的逗号，处理转义"""
        parts = []
        buf = []  # 使用 list 收集字符，最后 join，避免 O(N²) 字符串拼接
        depth = 0
        in_string = False
        sq = ''
        escape = False

        for ch in text:
            if escape:
                buf.append(ch)
                escape = False
                continue
            if ch == '\\' and in_string:
                buf.append(ch)
                escape = True
                continue
            if ch in ('"', "'") and not in_string:
                in_string = True
                sq = ch
            elif ch == sq and in_string:
                in_string = False
            elif ch == '{' and not in_string:
                depth += 1
            elif ch == '}' and not in_string:
                depth -= 1
            elif ch == ',' and depth == 0 and not in_string:
                token = ''.join(buf).strip()
                if token:
                    parts.append(token)
                buf.clear()
                continue
            buf.append(ch)

        token = ''.join(buf).strip()
        if token:
            parts.append(token)
        return parts

    @staticmethod
    def _parse_kv_pairs(line: str) -> dict:
        """解析一行数据中的所有 key=value 对"""
        result = {}
        # 先去掉行首的 key = 前缀（如 [1] = 或 sword = ）
        content = LuaParser._get_brace_content(line)
        if not content:
            # 如果没有花括号，直接按逗号分割
            pairs = LuaParser._split_by_comma(line)
        else:
            pairs = LuaParser._split_by_comma(content)

        for pair in pairs:
            pair = pair.strip()
            if not pair or '=' not in pair:
                continue
            if pair.startswith('['):
                # [key] = value 格式的键，跳过键提取值
                rb = pair.find(']')
                if rb < 0:
                    continue
                after_bracket = pair[rb + 1:].strip()
                if after_bracket.startswith('='):
                    val = after_bracket[1:].strip()
                    # 这种情况下 value 通常是整个子表，提取它的字段
                    sub_pairs = LuaParser._flatten_subtable(val)
                    result.update(sub_pairs)
                continue
            # 普通 key=value
            eq_pos = pair.find('=')
            key = pair[:eq_pos].strip()
            val = pair[eq_pos + 1:].strip()
            result[key] = LuaParser._parse_value(val)
        return result

    @staticmethod
    def _flatten_subtable(val: str) -> dict:
        """展开子表 {a=1,b=2} 为 {'a':1,'b':2}"""
        val = val.strip()
        if val.startswith('{') and val.endswith('}'):
            inner = val[1:-1]
            pairs = LuaParser._split_by_comma(inner)
            result = {}
            for pair in pairs:
                pair = pair.strip()
                if '=' in pair and not pair.startswith('['):
                    eq_pos = pair.find('=')
                    k = pair[:eq_pos].strip()
                    v = pair[eq_pos + 1:].strip()
                    result[k] = LuaParser._parse_value(v)
            return result
        return {}

    @staticmethod
    def _parse_value(val: str) -> Any:
        """解析 Lua 值（完整处理所有转义序列）"""
        if not val:
            return ''

        # 字符串
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            inner = val[1:-1]
            # 逐字符解析 Lua 转义序列：\\  \"  \'  \n  \r  \t  \0
            _ESCAPE_MAP = {
                'n': '\n', 'r': '\r', 't': '\t', '0': '\0',
                '\\': '\x5c', '"': '"', "'": "\x27",
            }
            result = []
            i = 0
            while i < len(inner):
                if inner[i] == '\\' and i + 1 < len(inner):
                    ch = _ESCAPE_MAP.get(inner[i + 1])
                    if ch is not None:
                        result.append(ch)
                        i += 2
                        continue
                    # 未知的反斜杠序列，保留原样
                result.append(inner[i])
                i += 1
            return ''.join(result)

        # 布尔值
        if val.lower() == 'true':
            return True
        if val.lower() == 'false':
            return False

        # nil
        if val.lower() == 'nil':
            return None

        # 表/数组（保留为字符串）
        if val.startswith('{'):
            return val

        # 数字
        try:
            if '.' in val or 'e' in val.lower():
                return float(val)
            return int(val)
        except ValueError:
            return val


# ============================================================================
# Lua 序列化器
# ============================================================================

class LuaSerializer:
    """将结构化数据序列化回 Lua 代码"""

    @staticmethod
    def serialize(data: LuaTableData, _update_raw: bool = False) -> str:
        """序列化为 Lua 代码。

        未修改时直接返回原始文件文本（零 diff）；
        修改后按行输出，未修改的行使用原始文本，修改的行重新生成。
        表声明行、表体结束行、文件尾部（return/尾注释）一律逐字保留原始文本，
        避免"只要改一个格子就重排整张表头"的问题。

        _update_raw=True 时（保存后调用），把重新生成行的文本写回其 _raw_lines
        并清零 _dirty，使后续序列化与磁盘内容保持一致。"""
        # 未修改时直接返回原始文本，保持与原文件完全一致
        if not data.modified and data._raw_text:
            if _update_raw:
                for r in data.rows:
                    r._dirty = False
            return data._raw_text

        lines = []

        # ---- 文件头部注释（逐字保留）----
        for hc in data.header_comments:
            lines.append(hc)

        # ---- 表声明行（逐字保留原始文本）----
        if data.decl_raw:
            lines.append(data.decl_raw)
        else:
            # 兜底：极少数无声明行的情况
            if data.is_local and data.header_comments:
                last_stripped = data.header_comments[-1].strip()
                if LuaParser._DECL_PATTERN.match(last_stripped):
                    lines.append('{')
                else:
                    lines.append(f"local {data.table_name} = {{")
            elif data.table_name == 'ReturnTable' and data.header_comments:
                pass
            else:
                lines.append(f"local {data.table_name} = {{" if data.is_local else f"{data.table_name} = {{")

        field_names = data.field_names
        if not field_names:
            if data.close_raw:
                lines.append(data.close_raw)
            else:
                lines.append('}')
            if data.footer_raw:
                lines.extend(data.footer_raw)
            if _update_raw:
                for r in data.rows:
                    r._dirty = False
            return '\n'.join(lines)

        INDENT = '\t'

        for row in data.rows:
            # 如果此行未修改且有原始文本，直接使用原始文本（保留格式）
            if not row._dirty and row._raw_lines:
                lines.extend(row._raw_lines)
                continue

            # 否则重新生成该行
            if row.row_type == 'blank':
                gen = ['']
            elif row.row_type == 'comment':
                gen = [f'{INDENT}{row.comment_text}']
            else:
                parts = []
                for fn in field_names:
                    val = row.data.get(fn)
                    if val is None or val == '':
                        continue
                    val_str = LuaSerializer._value_to_lua(val)
                    parts.append(f'{fn}={val_str}')
                if not parts:
                    # 空数据行（如刚创建、尚未填写任何字段的行）不写出，
                    # 否则生成无键的空 {} 列表元素会破坏表结构
                    if _update_raw:
                        row._raw_lines = []
                        row._dirty = False
                    continue
                if row.row_key:
                    line = f'{INDENT}[{row.row_key}] = {{' + ', '.join(parts) + ', },'
                else:
                    line = f'{INDENT}{{' + ', '.join(parts) + ', },'
                gen = [line]

            if _update_raw:
                row._raw_lines = gen
                row._dirty = False
            lines.extend(gen)

        # ---- 表体结束行（逐字保留）----
        if data.close_raw:
            lines.append(data.close_raw)
        else:
            lines.append('}')

        # ---- 文件尾部（return、尾注释等，逐字保留）----
        if data.footer_raw:
            lines.extend(data.footer_raw)

        return '\n'.join(lines)

    @staticmethod
    def _is_lua_table_expr(val: str) -> bool:
        """检查字符串是否是一个完整的 Lua 表表达式（如 {1,2,3} 或 {a=1,b=2}）。

        与简单 startswith('{') 不同，此方法验证：
        1. 花括号配对正确（深度归零）
        2. 最外层 {} 闭合后没有尾部额外字符
        这可以区分 {1,2,3}（表）和 {{build}}闲置下来了。（字符串）。"""
        if not val.startswith('{'):
            return False
        depth = 0
        in_string = False
        sq = ''
        esc = False
        for i, ch in enumerate(val):
            if esc:
                esc = False
                continue
            if ch == '\\' and in_string:
                esc = True
                continue
            if ch in ('"', "'") and not in_string:
                in_string = True
                sq = ch
            elif ch == sq and in_string:
                in_string = False
            elif ch == '{' and not in_string:
                depth += 1
            elif ch == '}' and not in_string:
                depth -= 1
                # 最外层花括号闭合后，不能再有非空白字符
                if depth == 0 and i < len(val) - 1:
                    return False
        return depth == 0

    @staticmethod
    def _value_to_lua(val: Any) -> str:
        if val is None or val == '':
            return 'nil'
        if isinstance(val, bool):
            return 'true' if val else 'false'
        if isinstance(val, str):
            if LuaSerializer._is_lua_table_expr(val):
                return val
            # 逐字符转义，避免链式 replace 的交叉干扰
            # 注意：不转义反斜杠，保留原始文本中的 \n \r \t 等字符
            _ESC_MAP = {
                '"': '\\"', '\n': '\\n',
                '\r': '\\r', '\t': '\\t', '\0': '\\0',
            }
            escaped = ''.join(_ESC_MAP.get(ch, ch) for ch in val)
            return f'"{escaped}"'
        if isinstance(val, (int, float)):
            return str(val)
        return str(val)


# ============================================================================
# 表格数据模型
# ============================================================================

class LuaTableModel(QAbstractTableModel):
    """自定义表格模型，支持数据行、注释行、文件头注释行和return行"""

    # 行类型常量
    ROW_HEADER_COMMENT = 'header_comment'  # 文件头注释（如 -- ActionData.xlsx）
    ROW_DATA = 'data'                      # 数据行
    ROW_INLINE_COMMENT = 'inline_comment'  # 内联注释行
    ROW_RETURN = 'return'                  # return data 行

    # 类级别缓存：行号列字体（避免每行 data() 调用都创建 QFont）
    _row_number_font: QFont = None

    @classmethod
    def _get_row_font(cls) -> QFont:
        if cls._row_number_font is None:
            cls._row_number_font = QFont()
            cls._row_number_font.setPointSize(cls._row_number_font.pointSize() - 1)
        return cls._row_number_font

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: Optional[LuaTableData] = None
        self._headers: list = []  # 包含 "#" 行号的完整列头
        self._undo_stack: list = []  # [(row, col, old_value), ...]
        self._redo_stack: list = []  # [(row, col, new_value), ...]
        self._duplicate_set: dict = {}  # {(col, value_str): True} 用于重复值高亮
        self._resetting: bool = False  # 防重入标志，阻止嵌套模型重置

    def safe_notify_reset(self):
        """安全刷新模型视图：beginResetModel/endResetModel 配对，防重入"""
        if self._resetting:
            return
        try:
            self._resetting = True
            self.beginResetModel()
            self.endResetModel()
        finally:
            self._resetting = False

    def set_data(self, data: LuaTableData):
        self.beginResetModel()
        self._data = data
        self._headers = ['#'] + data.field_names if data else []
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._rebuild_duplicates()
        self.endResetModel()

    @property
    def _header_count(self) -> int:
        """头注释行数"""
        if not self._data:
            return 0
        return len(self._data.header_comments)

    @property
    def _data_start_row(self) -> int:
        """数据区起始行号（= 头注释行数）"""
        return self._header_count

    @property
    def _has_return(self) -> bool:
        """是否有 return 行"""
        if not self._data or not self._data.return_var:
            return False
        return True

    @property
    def _return_row_idx(self) -> int:
        """return 行的索引（如果存在）"""
        if not self._has_return:
            return -1
        return self._header_count + len(self._data.rows)

    def _resolve_row(self, idx: int):
        """
        将表格逻辑行索引映射到 (row_type, real_index)
        - header_comment → ('header_comment', comment_index)
        - data/comment   → ('data'|'comment', data_index)
        - return         → ('return', None)
        """
        hc = self._header_count
        dr_count = len(self._data.rows) if self._data else 0

        if idx < hc:
            return (self.ROW_HEADER_COMMENT, idx)
        data_idx = idx - hc
        if data_idx < dr_count:
            entry = self._data.rows[data_idx]
            if entry.row_type == 'comment':
                return (self.ROW_INLINE_COMMENT, data_idx)
            return (self.ROW_DATA, data_idx)
        if self._has_return and idx == hc + dr_count:
            return (self.ROW_RETURN, None)
        return (None, None)

    def _rebuild_duplicates(self):
        """重建重复值集合，用于高亮显示"""
        self._duplicate_set.clear()
        if not self._data:
            return
        fields = self._data.field_names
        # 统计每列每个值的出现次数
        counts: dict = {}  # {(col, value_str): count}
        for row_entry in self._data.rows:
            if row_entry.row_type != 'data':
                continue
            for c, fn in enumerate(fields):
                val = row_entry.data.get(fn, '')
                if val is None or val == '':
                    continue
                val_str = LuaSerializer._value_to_lua(val)
                key = (c, val_str)
                counts[key] = counts.get(key, 0) + 1
        # 只保留出现次数 >= 2 的项
        for key, cnt in counts.items():
            if cnt >= 2:
                self._duplicate_set[key] = True

    def is_duplicate(self, row: int, col: int) -> bool:
        """检查指定单元格的值是否为重复值"""
        if not self._data or col <= 0 or col - 1 >= len(self._data.field_names):
            return False
        row_type, real_idx = self._resolve_row(row)
        if row_type != self.ROW_DATA:
            return False
        re = self._data.rows[real_idx]
        if re.row_type != 'data':
            return False
        fn = self._data.field_names[col - 1]
        val = re.data.get(fn, '')
        if val is None or val == '':
            return False
        val_str = LuaSerializer._value_to_lua(val)
        return self._duplicate_set.get((col - 1, val_str), False)

    @property
    def table_data(self) -> LuaTableData:
        return self._data

    def rowCount(self, parent=QModelIndex()) -> int:
        if not self._data:
            return 0
        total = self._header_count + len(self._data.rows)
        if self._has_return:
            total += 1
        return total

    def columnCount(self, parent=QModelIndex()) -> int:
        if not self._data:
            return 0
        return len(self._data.field_names) + 1  # +1 for row number

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if 0 <= section < len(self._headers):
                return self._headers[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not self._data or not index.isValid():
            return None

        row_idx = index.row()
        col = index.column()
        row_type, real_idx = self._resolve_row(row_idx)

        # ===== 行号列（col == 0）=====
        if col == 0:
            if role == Qt.DisplayRole:
                if row_type == self.ROW_HEADER_COMMENT:
                    return ''  # 头注释行无行号
                elif row_type == self.ROW_RETURN:
                    return 'return'
                elif row_type in (self.ROW_DATA, self.ROW_INLINE_COMMENT):
                    row_entry = self._data.rows[real_idx]
                    if row_entry.row_key:
                        return row_entry.row_key
                    if row_type == self.ROW_INLINE_COMMENT:
                        return '--'  # 注释行不占数据序号
                    # 数据行：只计算纯数据行（不含内联注释）的位置序号
                    data_num = sum(1 for e in self._data.rows[:real_idx + 1] if e.row_type == 'data')
                    return str(data_num)
                return ''
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.ForegroundRole:
                if row_type in (self.ROW_HEADER_COMMENT, self.ROW_RETURN):
                    return QColor('#9999bb')  # 蓝灰色，区分文件结构行
                return QColor('#888888')
            if role == Qt.FontRole:
                font = LuaTableModel._get_row_font()
                if row_type in (self.ROW_HEADER_COMMENT, self.ROW_RETURN):
                    font.setItalic(True)
                return font
            return None

        # ===== 头注释行 =====
        if row_type == self.ROW_HEADER_COMMENT:
            if col == 1 and role == Qt.DisplayRole:
                return self._data.header_comments[real_idx]
            if col == 1 and role == Qt.UserRole:
                return self._data.header_comments[real_idx]
            if role == Qt.DisplayRole:
                return ''
            if role == Qt.ForegroundRole:
                return QColor('#008000')  # 绿色（Lua 注释色）
            if role == Qt.FontRole:
                font = LuaTableModel._get_row_font()
                font.setItalic(True)
                return font
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            return None

        # ===== Return 行 =====
        if row_type == self.ROW_RETURN:
            ret_text = f'return {self._data.return_var}'
            if col == 1 and role == Qt.DisplayRole:
                return ret_text
            if col == 1 and role == Qt.UserRole:
                return ret_text
            if role == Qt.DisplayRole:
                return ''
            if role == Qt.ForegroundRole:
                return QColor('#800080')  # 紫色（关键字色）
            if role == Qt.FontRole:
                font = LuaTableModel._get_row_font()
                font.setItalic(True)
                return font
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            return None

        # ===== 内联注释行 / 数据行 =====
        if row_type is None:
            return None

        row = self._data.rows[real_idx]
        field_names = self._data.field_names

        # 注释行
        if row.row_type == 'comment':
            if col == 1:
                if role == Qt.DisplayRole:
                    return row.comment_text
                if role == Qt.UserRole:  # 全量文本
                    return row.comment_text
            else:
                # 其他列返回空，合并效果由 delegate 处理
                if role == Qt.DisplayRole:
                    return ''
                if role == Qt.UserRole:
                    return ''
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            if role == Qt.ForegroundRole:
                return QColor('#008000')
            return None

        # 数据行
        if col - 1 < len(field_names):
            fn = field_names[col - 1]
            val = row.data.get(fn, '')

            if role == Qt.DisplayRole:
                if val is None or val == '':
                    return ''
                return LuaSerializer._value_to_lua(val)
            if role == Qt.EditRole:
                if isinstance(val, bool):
                    return 'true' if val else 'false'
                if val is None or val == '':
                    return ''
                return str(val) if not isinstance(val, str) else val
            if role == Qt.UserRole:
                if val is None or val == '':
                    return ''
                return LuaSerializer._value_to_lua(val)
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole):
        if not self._data or not index.isValid():
            return False
        row_idx = index.row()
        col = index.column()
        row_type, real_idx = self._resolve_row(row_idx)

        # 头注释行：允许编辑注释文本
        if row_type == self.ROW_HEADER_COMMENT:
            if col == 1:
                old_val = self._data.header_comments[real_idx]
                if old_val == str(value):
                    return False
                # 使用 4 元组，第4元素 '__hdr__' 标记这是头注释行操作
                self._undo_stack.append((row_idx, col, old_val, '__hdr__'))
                self._redo_stack.clear()
                self._data.header_comments[real_idx] = str(value)
                self._data.modified = True
                self.dataChanged.emit(index, index)
                return True
            return False

        # Return 行不可编辑
        if row_type == self.ROW_RETURN:
            return False

        # 内联行号列不可编辑
        if col == 0:
            return False

        # 内联注释行：可编辑注释内容
        if row_type == self.ROW_INLINE_COMMENT:
            row_entry = self._data.rows[real_idx]
            if col == 1:
                old_val = row_entry.comment_text
                if old_val == str(value):
                    return False
                # real_idx 是 data.rows 索引，undo 需要用此值直接访问 data.rows
                self._undo_stack.append((real_idx, col, old_val))
                self._redo_stack.clear()
                row_entry.comment_text = str(value)
                row_entry._dirty = True
                self._data.modified = True
                self.dataChanged.emit(index, index)
                return True
            return False

        # 数据行
        row_entry = self._data.rows[real_idx]

        field_names = self._data.field_names
        if col - 1 < len(field_names):
            fn = field_names[col - 1]
            old_val = row_entry.data.get(fn, '')
            parsed = LuaParser._parse_value(str(value))

            # 如果新值和旧值类型不同但文本相同，保留原类型
            if str(old_val) == str(value) and type(old_val) != type(parsed):
                parsed = old_val

            # 仅当数据实际发生变化时才标记已修改
            if old_val == parsed or (old_val is None and parsed == ''):
                return False

            # 记录 undo：使用 real_idx（data.rows 索引）而非逻辑行号
            self._undo_stack.append((real_idx, col, old_val))
            self._redo_stack.clear()

            row_entry.data[fn] = parsed
            row_entry._dirty = True
            self._data.modified = True
            self._rebuild_duplicates()
            # 发整列信号，让同行其他重复值也刷新高亮
            top_left = self.index(0, col)
            bottom_right = self.index(self.rowCount() - 1, col)
            self.dataChanged.emit(top_left, bottom_right)
            return True
        return False

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        flags = super().flags(index)
        row_type, real_idx = self._resolve_row(index.row())

        # 头注释行：允许编辑第1列
        if row_type == self.ROW_HEADER_COMMENT and index.column() == 1:
            flags |= Qt.ItemIsEditable
        # Return 行不可编辑
        elif row_type == self.ROW_RETURN:
            pass
        # 内联注释行：允许编辑第1列
        elif row_type == self.ROW_INLINE_COMMENT and index.column() == 1:
            flags |= Qt.ItemIsEditable
        # 数据行：数据列可编辑
        elif row_type == self.ROW_DATA and index.column() > 0:
            flags |= Qt.ItemIsEditable
        return flags

    def is_comment_row(self, row: int) -> bool:
        """检查指定行是否为注释行（含头注释和内联注释）"""
        if not self._data or row < 0:
            return False
        row_type, _ = self._resolve_row(row)
        return row_type in (self.ROW_HEADER_COMMENT, self.ROW_INLINE_COMMENT)

    def is_structural_row(self, row: int) -> bool:
        """检查指定行是否为文件结构行（头注释或return），非数据"""
        if not self._data or row < 0:
            return False
        row_type, _ = self._resolve_row(row)
        return row_type in (self.ROW_HEADER_COMMENT, self.ROW_RETURN)

    # ---- 撤销 / 重做 ----
    def _row_matches_raw(self, row: 'RowEntry') -> bool:
        """判断某行当前内容是否与其原始文本完全一致（用于撤销后清除脏标记）"""
        if not row._raw_lines:
            return False
        if row.row_type == 'data':
            try:
                orig = LuaParser._parse_kv_pairs('\n'.join(row._raw_lines))
            except Exception:
                return False
            return orig == row.data
        if row.row_type == 'comment':
            raw = row._raw_lines[-1].strip()
            return raw == row.comment_text or raw.lstrip() == row.comment_text
        return False

    def _finalize_undo_redo(self):
        """撤销/重做完单次操作后调用：若全部撤销完毕则复位 modified 与脏标记，保证零 diff"""
        if not self._data:
            return
        if not self._undo_stack:
            # 所有操作已撤销 → 内存状态回到加载时，整文件未修改
            self._data.modified = False
            for r in self._data.rows:
                r._dirty = False

    def undo(self) -> bool:
        """撤销最近一次操作（支持单元格编辑和行级别操作）"""
        if not self._undo_stack:
            return False
        try:
            entry = self._undo_stack.pop()
        except IndexError:
            return False

        # 解析 entry 格式：
        #   (row, -1, old_val)           → 行级别操作，row 是 data.rows 索引
        #   (row, col, old_val)          → 单元格操作，row 是 data.rows 索引
        #   (row, col, old_val, '__hdr__') → 头注释行编辑，row 是 header_comments 索引
        is_header = (len(entry) == 4 and entry[3] == '__hdr__')
        row, col, old_val = entry[0], entry[1], entry[2]

        # 行级别操作（col=-1）：恢复整行 RowEntry
        if col == -1:
            import copy
            if not self._data or row < 0 or row >= len(self._data.rows):
                return False
            current_entry = copy.deepcopy(self._data.rows[row])
            self._redo_stack.append((row, -1, current_entry))
            self._data.rows[row] = old_val
            self._data.modified = True
            self._rebuild_duplicates()
            self.safe_notify_reset()
            self._finalize_undo_redo()
            return True

        if is_header:
            # 头注释行编辑撤销
            if not self._data or row < 0 or row >= len(self._data.header_comments):
                return False
            current = self._data.header_comments[row]
            self._data.header_comments[row] = old_val
            self._redo_stack.append((row, col, current, '__hdr__'))
            self._data.modified = True
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx)
            self._finalize_undo_redo()
            return True

        # 单元格级别操作：row 是 data.rows 索引
        if not self._data or row < 0 or row >= len(self._data.rows):
            return False
        re = self._data.rows[row]

        # 记录当前值用于 redo
        if re.row_type == 'comment' and col == 1:
            current = re.comment_text
            re.comment_text = old_val
        elif re.row_type == 'data' and col > 0:
            fn = self._data.field_names[col - 1] if col - 1 < len(self._data.field_names) else None
            if fn is None:
                return False
            current = re.data.get(fn, '')
            re.data[fn] = old_val
            # 清理空值
            if old_val is None or old_val == '':
                re.data.pop(fn, None)
        else:
            return False

        # 撤销后重算脏标记：若该行已回到原始值则不再标脏，避免保存时整行被重排
        re._dirty = not self._row_matches_raw(re)

        self._redo_stack.append((row, col, current))
        self._data.modified = True
        self._rebuild_duplicates()
        idx = self.index(row, col)
        self.dataChanged.emit(idx, idx)
        self._finalize_undo_redo()
        return True

    def redo(self) -> bool:
        """重做最近一次被撤销的操作（支持单元格编辑和行级别操作）"""
        if not self._redo_stack:
            return False
        try:
            entry = self._redo_stack.pop()
        except IndexError:
            return False

        is_header = (len(entry) == 4 and entry[3] == '__hdr__')
        row, col, new_val = entry[0], entry[1], entry[2]

        # 行级别操作（col=-1）：恢复被撤销的行级别变更
        if col == -1:
            import copy
            if not self._data or row < 0 or row >= len(self._data.rows):
                return False
            current_entry = copy.deepcopy(self._data.rows[row])
            self._undo_stack.append((row, -1, current_entry))
            self._data.rows[row] = new_val
            self._data.modified = True
            self._rebuild_duplicates()
            self.safe_notify_reset()
            self._finalize_undo_redo()
            return True

        if is_header:
            # 头注释行编辑重做
            if not self._data or row < 0 or row >= len(self._data.header_comments):
                return False
            current = self._data.header_comments[row]
            self._data.header_comments[row] = new_val
            self._undo_stack.append((row, col, current, '__hdr__'))
            self._data.modified = True
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx)
            self._finalize_undo_redo()
            return True

        # 单元格级别操作：row 是 data.rows 索引
        if not self._data or row < 0 or row >= len(self._data.rows):
            return False
        re = self._data.rows[row]

        # 记录当前值用于 undo
        if re.row_type == 'comment' and col == 1:
            current = re.comment_text
            re.comment_text = new_val
        elif re.row_type == 'data' and col > 0:
            fn = self._data.field_names[col - 1] if col - 1 < len(self._data.field_names) else None
            if fn is None:
                return False
            current = re.data.get(fn, '')
            re.data[fn] = new_val
        else:
            return False

        # 重做后重算脏标记（重做即重新应用修改，通常行仍为脏）
        re._dirty = not self._row_matches_raw(re)

        self._undo_stack.append((row, col, current))
        self._data.modified = True
        self._rebuild_duplicates()
        idx = self.index(row, col)
        self.dataChanged.emit(idx, idx)
        return True

    def has_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def has_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def push_row_undo(self, row: int, old_entry: RowEntry):
        """记录行级别撤销信息（用于注释/取消注释等整体行替换操作）
        
        col=-1 标记表示这是一个行级别操作，old_entry 保存替换前的完整 RowEntry。
        """
        import copy
        self._undo_stack.append((row, -1, copy.deepcopy(old_entry)))
        self._redo_stack.clear()

    @staticmethod
    def _next_default_row_key(data) -> str:
        """为新数据行计算一个默认行键：取现有数字键的最大值 + 1，无数字键则为 1。"""
        max_key = 0
        for r in data.rows:
            if r.row_type == 'data' and r.row_key:
                try:
                    n = int(r.row_key)
                    if n > max_key:
                        max_key = n
                except ValueError:
                    pass
        return str(max_key + 1)

    def add_data_row(self, row_pos: int = -1):
        """添加数据行（row_pos 为表格逻辑行号，负数表示追加到末尾）"""
        if not self._data:
            return
        # 负数 row_pos → 追加到末尾，使用正确的逻辑行号
        if row_pos < 0:
            row_pos = self.rowCount()
        data_row_pos = self._logical_to_data_row(row_pos)
        self.beginInsertRows(QModelIndex(), row_pos, row_pos)
        # 赋予默认行键，避免新行写成无键的空 {} 列表元素破坏表结构
        new_row = RowEntry(row_type='data', row_key=self._next_default_row_key(self._data))
        self._data.rows.insert(data_row_pos, new_row)
        self._data.modified = True
        self.endInsertRows()

    def add_data_rows_batch(self, count: int, row_pos: int = -1):
        """批量添加数据行（一次 begin/end 信号，避免 UI 反复刷新）"""
        if not self._data or count <= 0:
            return
        # 负数 row_pos → 追加到末尾，使用正确的逻辑行号
        if row_pos < 0:
            row_pos = self.rowCount()
        data_row_pos = self._logical_to_data_row(row_pos)
        self.beginInsertRows(QModelIndex(), row_pos, row_pos + count - 1)
        for _ in range(count):
            # 逐行计算默认键，保证批量新增的键互不冲突
            self._data.rows.insert(data_row_pos,
                                   RowEntry(row_type='data', row_key=self._next_default_row_key(self._data)))
        self._data.modified = True
        self.endInsertRows()

    def add_comment_row(self, row_pos: int = -1):
        """添加注释行（row_pos 为表格逻辑行号，负数表示追加到末尾）"""
        if not self._data:
            return
        if row_pos < 0:
            row_pos = self.rowCount()
        data_row_pos = self._logical_to_data_row(row_pos)
        self.beginInsertRows(QModelIndex(), row_pos, row_pos)
        self._data.rows.insert(data_row_pos, RowEntry(row_type='comment', comment_text='-- '))
        self._data.modified = True
        self.endInsertRows()

    def remove_row(self, row: int):
        """删除指定行（row 为表格逻辑行号）"""
        if not self._data or row < 0:
            return
        row_type, real_idx = self._resolve_row(row)
        # 不允许删除头注释和return行
        if row_type in (self.ROW_HEADER_COMMENT, self.ROW_RETURN):
            return
        # 内联注释或数据行：删除 self._data.rows[real_idx]
        if real_idx is None or real_idx >= len(self._data.rows):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._data.rows[real_idx]
        self._data.modified = True
        self.endRemoveRows()

    def get_row_text(self, row: int) -> str:
        """获取一行所有数据的文本拼接（用于查找）"""
        if not self._data or row < 0:
            return ''
        row_type, real_idx = self._resolve_row(row)
        if row_type == self.ROW_HEADER_COMMENT:
            return self._data.header_comments[real_idx]
        if row_type == self.ROW_RETURN:
            return f'return {self._data.return_var}'
        if real_idx is None or real_idx >= len(self._data.rows):
            return ''
        r = self._data.rows[real_idx]
        if r.row_type == 'comment':
            return r.comment_text
        return ' '.join(str(r.data.get(fn, '')) for fn in self._data.field_names)

    def get_full_value(self, row: int, col: int) -> str:
        """获取单元格的完整文本值（用于详情面板）"""
        if not self._data or row < 0 or col <= 0:
            return ''
        row_type, real_idx = self._resolve_row(row)
        if col - 1 >= len(self._data.field_names):
            return ''
        if row_type == self.ROW_HEADER_COMMENT and col == 1:
            return self._data.header_comments[real_idx]
        if row_type == self.ROW_RETURN and col == 1:
            return f'return {self._data.return_var}'
        if real_idx is None or real_idx >= len(self._data.rows):
            return ''
        r = self._data.rows[real_idx]
        if r.row_type == 'comment':
            return r.comment_text
        fn = self._data.field_names[col - 1]
        return LuaSerializer._value_to_lua(r.data.get(fn, ''))

    def _logical_to_data_row(self, logical_row: int) -> int:
        """
        表格逻辑行号 → 数据数组中的实际索引。
        负数或超出范围时追加到末尾。
        """
        hc = self._header_count
        dr_count = len(self._data.rows) if self._data else 0

        if logical_row < 0 or logical_row > hc + dr_count:
            return dr_count
        # 头注释区域内的位置→插入到数据区起始
        if logical_row <= hc:
            return 0
        # 数据/内联注释区域内
        data_idx = logical_row - hc
        return max(0, min(data_idx, dr_count))

    def clear(self):
        """清空模型"""
        self.beginResetModel()
        self._data = None
        self._headers = ['#']
        self.endResetModel()


# ============================================================================
# 自定义委托
# ============================================================================

class LuaTableDelegate(QStyledItemDelegate):
    """自定义绘制委托：注释行渲染 + 长文本省略 + 提示"""

    MAX_CHARS = 40  # 单元格最多显示字符数

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        model = index.model()
        if isinstance(model, QSortFilterProxyModel):
            src = model.sourceModel()
            src_index = model.mapToSource(index)
        else:
            src = model
            src_index = index

        if not isinstance(src, LuaTableModel):
            super().paint(painter, option, index)
            return

        row = src_index.row()
        col = src_index.column()

        # ---- 结构性行（头注释 / return）特殊渲染 ----
        is_structural = src.is_structural_row(row)

        # 注释行特殊渲染（含头注释和内联注释）
        if src.is_comment_row(row) or is_structural:
            painter.save()

            # ---- 获取行列高亮信息 ----
            view = self.parent()
            hl_row = -1
            hl_col = -1
            if view is not None and hasattr(view, '_highlight_row'):
                hl_row = view._highlight_row
                hl_col = view._highlight_col

            is_selected = bool(option.state & QStyle.State_Selected)
            is_hl_row = (row == hl_row) and hl_row >= 0
            is_highlighted = is_hl_row and not is_selected

            # 整行 / 整列 / 交叉点 选中高亮
            sel_row = sel_col = False
            if view is not None:
                sm = view.selectionModel()
                if sm is not None and index.isValid():
                    sel_row = sm.isRowSelected(index.row(), QModelIndex())
                    sel_col = sm.isColumnSelected(index.column(), QModelIndex())
            is_intersect = sel_row and sel_col

            # 背景色：根据行类型区分，叠加选中行/列高亮
            if is_intersect:
                painter.fillRect(option.rect, QColor('#ffd24d'))   # 交叉点：金色强调
            elif sel_row:
                painter.fillRect(option.rect, QColor('#bfe3ff'))   # 整行选中：蓝
            elif sel_col:
                painter.fillRect(option.rect, QColor('#ffd9b3'))   # 整列选中：橙
            elif is_selected:
                painter.fillRect(option.rect, QColor('#3a4a5a'))
            elif is_highlighted:
                painter.fillRect(option.rect, QColor('#d8dfeb'))
            elif is_structural and src._resolve_row(row)[0] == LuaTableModel.ROW_RETURN:
                painter.fillRect(option.rect, QColor('#f0e8f5'))  # return 行淡紫色背景
            elif is_structural:
                painter.fillRect(option.rect, QColor('#f5f5ee'))   # 头注释行浅色背景
            else:
                painter.fillRect(option.rect, QColor('#f0f0e8'))  # 内联注释行

            # 选中/高亮行的行号列左侧标记条
            if col == 0 and (is_selected or is_highlighted):
                marker_rect = QRect(option.rect.left(), option.rect.top(),
                                    4, option.rect.height())
                painter.fillRect(marker_rect, QColor('#4a86e8'))

            # 绘制文本（仅第一列有效数据列）
            if col == 1:
                text = src.data(src_index, Qt.UserRole) or ''
                # 根据行类型设置文字颜色
                if is_structural and src._resolve_row(row)[0] == LuaTableModel.ROW_RETURN:
                    painter.setPen(QColor('#800080'))  # return 紫色
                elif is_structural:
                    painter.setPen(QColor('#006600'))  # 头注释深绿
                else:
                    painter.setPen(QColor('#666666'))  # 内联注释灰
                font = QFont(option.font)
                font.setItalic(True)
                font.setPointSize(font.pointSize() - 1)
                painter.setFont(font)

                # 裁剪文本以适应
                rect = option.rect.adjusted(8, 0, -8, 0)
                elided = painter.fontMetrics().elidedText(
                    text, Qt.ElideRight, rect.width()
                )
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, elided)

            # 绘制底部分隔线
            painter.setPen(QColor('#d0d0c8'))
            painter.drawLine(
                option.rect.bottomLeft(), option.rect.bottomRight()
            )

            painter.restore()
            return

        # 常规数据行
        painter.save()

        # ---- 获取行列高亮信息 ----
        # 注意：setItemDelegate 不会设置委托的 QObject 父对象（self.parent() 为 None），
        # 必须用 option.widget（Qt 绘制时携带的视图实例）来获取视图，
        # 这样委托被主视图与冻结列 overlay 共享时也能各自拿到正确的视图。
        view = option.widget or self.parent()
        hl_row = -1
        hl_col = -1
        if view is not None and hasattr(view, '_highlight_row'):
            hl_row = view._highlight_row
            hl_col = view._highlight_col

        is_selected = bool(option.state & QStyle.State_Selected)
        is_hl_row = (row == hl_row) and hl_row >= 0
        is_hl_col = (col == hl_col) and hl_col >= 0
        is_highlighted = (is_hl_row or is_hl_col) and not is_selected and col > 0
        is_dup = not is_selected and col > 0 and src.is_duplicate(row, col)

        # 整行 / 整列 / 交叉点 选中高亮
        sel_row = sel_col = False
        if view is not None:
            sm = view.selectionModel()
            # 无选区时跳过逐格选择态查询：isRowSelected/isColumnSelected 是 O(选中范围)
            # 的查询，大表无选中时被每个单元格各调用一次会明显拖慢；尤其多个冻结列
            # 浮层共享同一选择模型时该开销成倍放大。普通块选中高亮仍由 option.state 提供。
            if sm is not None and sm.hasSelection() and index.isValid():
                sel_row = sm.isRowSelected(index.row(), QModelIndex())
                sel_col = sm.isColumnSelected(index.column(), QModelIndex())
        is_intersect = sel_row and sel_col
        is_sel_row = sel_row and not is_intersect
        is_sel_col = sel_col and not is_intersect

        # ---- 背景色 ----
        if is_intersect:
            painter.fillRect(option.rect, QColor('#ffd24d'))   # 交叉点：金色强强调
        elif is_sel_row:
            painter.fillRect(option.rect, QColor('#bfe3ff'))   # 整行选中：蓝
        elif is_sel_col:
            painter.fillRect(option.rect, QColor('#ffd9b3'))   # 整列选中：橙
        elif is_selected:
            painter.fillRect(option.rect, QColor('#a8d4f0'))   # 普通块选中：浅蓝
        elif is_dup:
            painter.fillRect(option.rect, QColor('#ffe8e0'))
        elif is_highlighted:
            painter.fillRect(option.rect, QColor('#d8eafc'))
        elif row % 2 == 0:
            painter.fillRect(option.rect, QColor('#ffffff'))
        else:
            painter.fillRect(option.rect, QColor('#f8f8fa'))

        # 行号列：选中/高亮行左侧标记条
        if col == 0 and (is_selected or is_hl_row):
            marker_rect = QRect(option.rect.left(), option.rect.top(),
                                4, option.rect.height())
            painter.fillRect(marker_rect, QColor('#4a86e8'))

        # 绘制文本（省略长文本）
        if col > 0:
            text = src.data(src_index, Qt.UserRole) or ''
            if not isinstance(text, str):
                text = str(text)
            rect = option.rect.adjusted(6, 0, -6, 0)

            # 重复值用橙红色文字 + 底部标记线
            if is_dup:
                painter.setPen(QColor('#c0392b'))
                # 底部橙色标记线
                line_y = option.rect.bottom() - 1
                painter.fillRect(
                    QRect(option.rect.left() + 4, line_y,
                          option.rect.width() - 8, 1),
                    QColor('#e8836b')
                )
                # 左侧小标记条
                dup_marker = QRect(option.rect.left(), option.rect.top(),
                                   3, option.rect.height())
                painter.fillRect(dup_marker, QColor('#e8836b'))
            elif len(text) > self.MAX_CHARS:
                painter.setPen(QColor('#202020'))
            else:
                painter.setPen(QColor('#202020'))

            if '\n' in text or '\r' in text:
                # 文本含真实换行符（如 Lua 里的 \n 经解析所得）：
                # QTableView 默认 textElideMode 不换行，drawText 直接画会把 \n 当普通字符，
                # 导致“换行不渲染”。用多行模式绘制，并裁剪超出单元格的部分。
                painter.drawText(
                    rect,
                    Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                    text,
                )
            elif len(text) > self.MAX_CHARS:
                elided = text[:self.MAX_CHARS] + '...'
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, elided)
            else:
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)
        else:
            # 行号
            super().paint(painter, option, index)

        # 交叉点（整行且整列同时选中）单元格边框强调
        if col > 0 and is_intersect:
            painter.save()
            painter.setPen(QPen(QColor('#e69500'), 2))
            painter.drawRect(option.rect.adjusted(1, 1, -1, -1))
            painter.restore()

        painter.restore()

        # ---- 当前格焦点框（独立于单元格绘制，在最上层） ----
        if col > 0 and (option.state & QStyle.State_HasFocus):
            painter.save()
            painter.setPen(QPen(QColor('#3a7fc0'), 2))
            r = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(r)
            painter.restore()

        # ---- 填充柄指示器（仅单一连续选区时绘制） ----
        self._draw_fill_handle(painter, option, row, col)

    def _draw_fill_handle(self, painter, option, row, col):
        """绘制填充柄指示器"""
        # 延迟获取 view 引用，不在每单元格都 isinstance 检查
        view = self.parent()
        if view is None or not hasattr(view, '_has_single_selection'):
            # 尝试通过 option.widget 获取
            if option.widget and hasattr(option.widget, 'parent'):
                p = option.widget.parent()
                if hasattr(p, '_has_single_selection'):
                    view = p
                else:
                    return
            else:
                return

        if not view._has_single_selection():
            return

        rng = view.selectionModel().selection()[0]
        bottom = rng.bottom()
        right_c = rng.right()
        if row == bottom and col == right_c:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, False)

            hanging = getattr(view, '_fill_hovering', False)
            if hanging:
                painter.setPen(QPen(QColor('#1565C0'), 2))
                painter.setBrush(QColor('#42A5F5'))
            else:
                painter.setPen(QPen(QColor('#1a5276'), 1))
                painter.setBrush(QColor('#2980b9'))

            sz = 7
            cr = QRect(
                option.rect.right() - sz - 2,
                option.rect.bottom() - sz - 2,
                sz, sz
            )
            painter.drawRect(cr)
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 28))
        return size

    def helpEvent(self, event, view, option, index):
        """鼠标悬停显示完整内容的 ToolTip"""
        if event.type() == QEvent.ToolTip and index.isValid():
            model = index.model()
            if isinstance(model, QSortFilterProxyModel):
                src = model.sourceModel()
                src_row = model.mapToSource(index).row()
            else:
                src = model
                src_row = index.row()

            if isinstance(src, LuaTableModel) and index.column() > 0:
                text = src.get_full_value(src_row, index.column())
                if text:
                    QToolTip.showText(event.globalPos(), text, view)
                    return True
        return super().helpEvent(event, view, option, index)


# ============================================================================
# 筛选代理模型
# ============================================================================

class FilterProxyModel(QSortFilterProxyModel):
    """支持列筛选的代理模型"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_column = -1       # 筛选列 (-1 = 全部)
        self._filter_text = ""          # 筛选文本
        self._filter_mode = "contains"  # equals / not_equals / contains / not_contains
        self._search_text = ""          # 查找高亮文本
        self._search_results = []       # 查找结果行号列表
        self._current_search_idx = -1   # 当前查找结果索引

    def set_filter(self, column: int, text: str, mode: str = "contains"):
        """设置列筛选条件"""
        self._filter_column = column
        self._filter_text = text
        self._filter_mode = mode
        self.invalidateFilter()

    def clear_filter(self):
        """清除筛选"""
        self._filter_column = -1
        self._filter_text = ""
        self._filter_mode = "contains"
        self.invalidateFilter()

    @property
    def is_filtering(self) -> bool:
        return self._filter_column >= 0 and bool(self._filter_text)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._filter_column < 0 or not self._filter_text:
            return True

        model = self.sourceModel()
        if not isinstance(model, LuaTableModel):
            return True

        if model.is_comment_row(source_row):
            return True  # 注释行不过滤

        col = self._filter_column
        if col <= 0 or col - 1 >= len(model._data.field_names):
            return True

        fn = model._data.field_names[col - 1]
        row = model._data.rows[source_row]
        val = str(row.data.get(fn, '')).lower()
        ft = self._filter_text.lower()

        if self._filter_mode == 'equals':
            return val == ft
        elif self._filter_mode == 'not_equals':
            return val != ft
        elif self._filter_mode == 'contains':
            return ft in val
        elif self._filter_mode == 'not_contains':
            return ft not in val
        return True

    def set_search(self, text: str):
        """设置查找文本，计算所有匹配行"""
        self._search_text = text
        self._search_results = []
        self._current_search_idx = -1

        if not text:
            return

        model = self.sourceModel()
        if not isinstance(model, LuaTableModel):
            return

        text_lower = text.lower()  # 提到循环外，避免 O(N) 次重复计算
        for row in range(model.rowCount()):
            if text_lower in model.get_row_text(row).lower():
                self._search_results.append(row)

    def next_search_result(self) -> int:
        """获取下一个查找结果（返回源模型行号）"""
        if not self._search_results:
            return -1
        self._current_search_idx = (self._current_search_idx + 1) % len(self._search_results)
        return self._search_results[self._current_search_idx]

    def prev_search_result(self) -> int:
        """获取上一个查找结果"""
        if not self._search_results:
            return -1
        self._current_search_idx = (self._current_search_idx - 1) % len(self._search_results)
        return self._search_results[self._current_search_idx]

    @property
    def search_count(self) -> int:
        return len(self._search_results)

    @property
    def current_search_pos(self) -> int:
        return self._current_search_idx + 1 if self._current_search_idx >= 0 else 0


# ============================================================================
# 自定义表格视图（Excel风格：多单元格选中、填充柄）
# ============================================================================

class LuaTableView(QTableView):
    """支持多单元格选中、填充柄拖拽、当前格高亮的自定义表格视图"""

    FILL_SIZE = 7  # 填充柄大小（像素）
    fill_completed = Signal()  # 填充完成信号
    copy_requested = Signal()  # Ctrl+C 复制信号
    paste_requested = Signal()  # Ctrl+V 粘贴信号
    header_context_menu_requested = Signal(object, QPoint)  # (header, pos) 供冻结列菜单

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fill_dragging = False
        self._fill_source_rows: list = []
        self._fill_source_cols: list = []
        self._fill_preview_rows = 0
        self._fill_hovering = False  # 鼠标是否悬停在填充柄上

        # 当前焦点格的行/列高亮跟踪（源模型坐标）
        self._highlight_row = -1
        self._highlight_col = -1

        # ---- 列固定（冻结）状态 ----
        self._frozen = []          # [{'col': int, 'side': 'left'/'right'}]
        # 每侧最多一个 overlay QTableView（合并该侧所有冻结列，避免多开视图拖慢）
        self._frozen_views = {'left': None, 'right': None}
        self._syncing = False      # 垂直滚动同步防递归
        self.verticalScrollBar().valueChanged.connect(self._on_main_vscroll)

        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

    # ---- 键盘输入即编辑 / Backspace 清空 ---- 
    def keyPressEvent(self, event):
        """按键事件：可输入字符 → 进入编辑模式；Backspace → 清空内容"""
        # 如果已经在编辑状态，交给默认处理
        if self.state() == QAbstractItemView.EditingState:
            super().keyPressEvent(event)
            return

        # Ctrl+C 复制
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_C:
            self.copy_requested.emit()
            return

        # Ctrl+V 粘贴
        if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_V:
            self.paste_requested.emit()
            return

        idx = self.currentIndex()
        if not idx.isValid() or idx.column() <= 0:
            super().keyPressEvent(event)
            return

        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            src = model.sourceModel()
        else:
            src = model

        key = event.key()
        text = event.text()

        # Backspace：清空当前单元格，不进入编辑模式
        if key == Qt.Key_Backspace or key == Qt.Key_Delete:
            if isinstance(src, LuaTableModel) and src.flags(idx) & Qt.ItemIsEditable:
                proxy_idx = self.model().index(idx.row(), idx.column()) \
                    if isinstance(self.model(), QSortFilterProxyModel) else idx
                self.model().setData(proxy_idx, '')
                self.viewport().update()
            return

        # 可打印字符 → 直接进入编辑模式并输入
        if text and len(text) > 0 and text.isprintable():
            if isinstance(src, LuaTableModel) and src.flags(idx) & Qt.ItemIsEditable:
                self.edit(idx)
                # edit() 后视图进入 EditingState，super 会将事件转发给编辑器
                super().keyPressEvent(event)
            return

        # 其他按键（方向键、Tab等）交给默认处理
        super().keyPressEvent(event)

    # ---- 填充柄矩形计算 ----
    def _has_single_selection(self) -> bool:
        """检查当前是否为单一连续选区（非 Ctrl+Click 散点）"""
        sel = self.selectionModel()
        if not sel:
            return False
        ranges = sel.selection()
        return len(ranges) == 1 and ranges[0].isValid()

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex):
        """当前单元格变化时更新行列高亮状态（使用源模型坐标）"""
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            src_cur = model.mapToSource(current) if current.isValid() else current
        else:
            src_cur = current

        if src_cur.isValid():
            self._highlight_row = src_cur.row()
            self._highlight_col = src_cur.column()
        else:
            self._highlight_row = -1
            self._highlight_col = -1
        self.viewport().update()

    def _fill_rect(self) -> QRect:
        """计算当前填充柄指示器的矩形区域（仅单一连续选区时有效）"""
        if not self._has_single_selection():
            return QRect()

        rng = self.selectionModel().selection()[0]
        bottom = rng.bottom()
        right = rng.right()
        vr = self.visualRect(self.model().index(bottom, right))
        if not vr.isValid():
            return QRect()
        return QRect(
            vr.right() - self.FILL_SIZE - 2,
            vr.bottom() - self.FILL_SIZE - 2,
            self.FILL_SIZE, self.FILL_SIZE
        )

    # ---- 列固定（冻结）功能 ----
    def is_column_frozen(self, col: int) -> bool:
        return any(e['col'] == col for e in self._frozen)

    def has_frozen_columns(self) -> bool:
        return len(self._frozen) > 0

    def freeze_column(self, col: int, side: str = 'left'):
        """固定某列到左侧或右侧（支持复数、非连续列；重复固定先移除旧的）"""
        if col <= 0:
            return
        self._frozen = [e for e in self._frozen if e['col'] != col]
        w = self.columnWidth(col)
        if w <= 0:
            w = self.horizontalHeader().sectionSizeHint(col) or 100
        self._frozen.append({'col': col, 'side': side, 'width': w})
        self._rebuild_frozen()

    def _frozen_width(self, e: dict) -> int:
        """获取冻结列宽度（隐藏列读不到宽度，必须用冻结时记录的值）"""
        w = e.get('width') or 0
        if w <= 0:
            w = self.columnWidth(e['col'])
        if w <= 0:
            w = self.horizontalHeader().sectionSizeHint(e['col']) or 100
        return int(w)

    def unfreeze_column(self, col: int):
        self._frozen = [e for e in self._frozen if e['col'] != col]
        self._rebuild_frozen()

    def unfreeze_all(self):
        self._frozen = []
        self._rebuild_frozen()

    def _rebuild_frozen(self):
        """销毁旧 overlay 并按当前 _frozen 重建。

        性能优化：每侧（left/right）只用一个 QTableView 浮层合并显示该侧所有
        冻结列（隐藏其余列），而非每列一个浮层。这样每帧最多重绘 2 个浮层 +
        主视图，避免“冻结列数”放大重绘开销（原来每冻结一列就多开一个完整视图）。
        """
        for ov in self._frozen_views.values():
            if ov is not None:
                ov.deleteLater()
        self._frozen_views = {'left': None, 'right': None}

        model = self.model()
        # 先恢复所有列可见，再隐藏冻结列
        if model is not None:
            for c in range(model.columnCount()):
                self.setColumnHidden(c, False)
            for e in self._frozen:
                self.setColumnHidden(e['col'], True)

        if model is None:
            return

        # 每侧创建一个合并浮层
        for side in ('left', 'right'):
            cols = [e for e in self._frozen if e['side'] == side]
            if not cols:
                continue
            cols.sort(key=lambda e: e['col'])  # 冻结列按列号顺序紧挨显示

            ov = QTableView(self)
            ov._side = side
            ov.setModel(model)
            ov.setSelectionModel(self.selectionModel())
            ov.setItemDelegate(self.itemDelegate())
            ov.horizontalHeader().setVisible(True)
            ov.verticalHeader().setVisible(False)
            ov.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            ov.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            ov.setSelectionBehavior(self.selectionBehavior())
            ov.setSelectionMode(self.selectionMode())
            ov.setEditTriggers(self.editTriggers())
            ov.setShowGrid(self.showGrid())
            ov.verticalHeader().setDefaultSectionSize(
                self.verticalHeader().defaultSectionSize())
            border = ("border-right: 2px solid #b0b8c0;"
                      if side == 'left' else "border-left: 2px solid #b0b8c0;")
            ov.setStyleSheet(f"QTableView {{ border: none; background: #ffffff; {border} }}")

            # 隐藏所有非本侧冻结列（冻结列按列号顺序自动紧挨显示）
            frozen_cols = {e['col'] for e in cols}
            for c in range(model.columnCount()):
                ov.setColumnHidden(c, c not in frozen_cols)
            # 设置各冻结列宽度
            for e in cols:
                ov.setColumnWidth(e['col'], self._frozen_width(e))

            # 垂直滚动同步（双向，带防递归）
            ov.verticalScrollBar().valueChanged.connect(
                lambda v, o=ov: self._sync_vscroll(v, o))
            # 浮层中改列宽 -> 同步回主视图并重排浮层
            ov.horizontalHeader().sectionResized.connect(
                lambda idx, old, new, ov=ov: self._on_frozen_section_resized(ov, idx, new))
            ov.horizontalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
            ov.horizontalHeader().customContextMenuRequested.connect(
                lambda pos, h=ov.horizontalHeader(): self._emit_header_menu(h, pos))
            ov.verticalScrollBar().setValue(self.verticalScrollBar().value())
            ov.installEventFilter(self)
            ov.raise_()
            # 关键：overlay 是在主窗口已显示后运行时创建的子控件，
            # 必须显式 show()，否则它保持隐藏 -> 主视图隐藏了该列而 overlay 不可见，
            # 表现就是“被固定的列直接消失”。
            ov.show()
            self._frozen_views[side] = ov

        self._layout_frozen()
        self.viewport().update()

    def _emit_header_menu(self, header, pos):
        self.header_context_menu_requested.emit(header, pos)

    def _frozen_side_width(self, side: str) -> int:
        """该侧所有冻结列宽度之和（即该侧浮层的整体宽度）"""
        return sum(self._frozen_width(e) for e in self._frozen if e['side'] == side)

    def _on_frozen_section_resized(self, ov, idx: int, new: int):
        """浮层内调整某冻结列宽度 -> 同步回主视图并重排浮层宽度"""
        for e in self._frozen:
            if e['col'] == idx:
                e['width'] = new
                self.setColumnWidth(idx, new)
                ov.setColumnWidth(idx, new)
                break
        self._layout_frozen()

    def _layout_frozen(self):
        """定位左右两个合并浮层（每侧一个整体控件）"""
        if not any(v is not None for v in self._frozen_views.values()):
            return
        vhw = self.verticalHeader().width()
        vscroll_w = self.verticalScrollBar().width() if self.verticalScrollBar().isVisible() else 0
        hscroll_h = self.horizontalScrollBar().height() if self.horizontalScrollBar().isVisible() else 0
        total_h = self.height() - hscroll_h
        ov_l = self._frozen_views.get('left')
        if ov_l is not None:
            ov_l.setGeometry(vhw, 0, self._frozen_side_width('left'), total_h)
        ov_r = self._frozen_views.get('right')
        if ov_r is not None:
            w = self._frozen_side_width('right')
            ov_r.setGeometry(self.width() - vscroll_w - w, 0, w, total_h)

    def _on_main_vscroll(self, value):
        self._sync_vscroll(value, self)

    def _sync_vscroll(self, value, src):
        if self._syncing:
            return
        self._syncing = True
        try:
            self.verticalScrollBar().setValue(value)
            for ov in self._frozen_views.values():
                if ov is not None and ov is not src:
                    ov.verticalScrollBar().setValue(value)
        finally:
            self._syncing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_frozen()

    def showEvent(self, event):
        super().showEvent(event)
        self._layout_frozen()

    # ---- 事件过滤器：拦截视口鼠标事件 ----
    def eventFilter(self, obj, event):
        # 冻结列 overlay：转发滚轮到主视图，保持滚动一致
        if obj in getattr(self, '_frozen_views', {}).values() and event.type() == QEvent.Wheel:
            QApplication.sendEvent(self.viewport(), event)
            return True
        if obj != self.viewport():
            return super().eventFilter(obj, event)

        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            fr = self._fill_rect()
            if fr.isValid() and fr.contains(event.pos()):
                self._begin_fill()
                return True

        elif event.type() == QEvent.MouseMove:
            if self._fill_dragging:
                self._update_fill(event.pos())
            else:
                fr = self._fill_rect()
                hovering = fr.isValid() and fr.contains(event.pos())
                if hovering != self._fill_hovering:
                    self._fill_hovering = hovering
                    # 更新填充柄外观
                    self.viewport().update()
                if hovering:
                    self.viewport().setCursor(Qt.SizeVerCursor)
                else:
                    self.viewport().unsetCursor()

        elif event.type() == QEvent.MouseButtonRelease:
            if self._fill_dragging:
                self._commit_fill()
                return True

        return False

    # ---- 填充拖拽逻辑 ----
    def _begin_fill(self):
        self._fill_dragging = True
        self._fill_hovering = False
        self._fill_source_rows = []
        self._fill_source_cols = []
        for idx in self.selectionModel().selectedIndexes():
            if idx.column() > 0:  # 跳过行号列
                if idx.row() not in self._fill_source_rows:
                    self._fill_source_rows.append(idx.row())
                if idx.column() not in self._fill_source_cols:
                    self._fill_source_cols.append(idx.column())
        self._fill_source_rows.sort()
        self._fill_source_cols.sort()
        self._fill_preview_rows = 0
        self.viewport().setCursor(Qt.SizeVerCursor)

    def _update_fill(self, pos):
        if not self._fill_source_rows:
            return
        last_row = self._fill_source_rows[-1]
        vr = self.visualRect(self.model().index(last_row, 0))
        if vr.isValid():
            h = max(vr.height(), 1)
            self._fill_preview_rows = max(0, (pos.y() - vr.bottom()) // h)
            self.viewport().update()

    def _commit_fill(self):
        n = self._fill_preview_rows
        if n <= 0:
            self._cleanup_fill()
            return

        model = self.model()
        src = model.sourceModel() if isinstance(model, QSortFilterProxyModel) else model
        if not isinstance(src, LuaTableModel) or not src.table_data:
            self._cleanup_fill()
            return

        data = src.table_data
        fields = data.field_names

        # 将代理行映射到源模型行
        src_rows = []
        for pr in self._fill_source_rows:
            if isinstance(model, QSortFilterProxyModel):
                src_rows.append(model.mapToSource(model.index(pr, 0)).row())
            else:
                src_rows.append(pr)
        src_rows.sort()

        # 读取被选中区域各列的值
        src_values = {}  # col -> [val1, val2, ...]
        for pc in self._fill_source_cols:
            vals = []
            for pr in self._fill_source_rows:
                pi = model.index(pr, pc)
                if isinstance(model, QSortFilterProxyModel):
                    si = model.mapToSource(pi)
                    v = src.data(si, Qt.EditRole)
                else:
                    v = src.data(pi, Qt.EditRole)
                vals.append(v)
            src_values[pc] = vals

        # ---- 索引修复：将逻辑行号转换为 data.rows 索引 ----
        hc = src._header_count
        last_logical = src_rows[-1]
        last_data_idx = last_logical - hc  # 最后一个选中行在 data.rows 中的索引

        # 确保目标行存在：需要 data.rows 至少 (last_data_idx + n + 1) 个条目
        need_rows = max(0, last_data_idx + n + 1 - len(data.rows))
        if need_rows > 0:
            # 在 data.rows 末尾追加，使用安全刷新而不调用有问题的 beginInsertRows
            for _ in range(need_rows):
                data.rows.append(RowEntry(row_type='data',
                                          row_key=self._next_default_row_key(data)))
            data.modified = True
            src.safe_notify_reset()

        # 执行填充 — 使用正确的 data.rows 索引
        for i in range(1, n + 1):
            di = last_data_idx + i  # data.rows 索引
            if di >= len(data.rows):
                break
            re = data.rows[di]
            if re.row_type != 'data':
                re.row_type = 'data'
                re.data = {}
            for pc, vals in src_values.items():
                if pc - 1 >= len(fields):
                    continue
                fn = fields[pc - 1]
                new_val = self._compute_fill(vals, i)
                old_val = re.data.get(fn, '')
                if old_val != new_val:
                    # 记录 undo（使用 data.rows 索引，与 setData 保持一致）
                    src._undo_stack.append((di, pc, old_val))
                    src._redo_stack.clear()
                re.data[fn] = new_val
                re._dirty = True

        # 通知视图更新填充区域（转换 data.rows 索引 → 逻辑行号）
        if len(data.rows) > last_data_idx:
            first_visual = last_data_idx + 1 + hc
            last_visual = min(last_data_idx + n, len(data.rows) - 1) + hc
            src.dataChanged.emit(
                src.index(first_visual, min(self._fill_source_cols)),
                src.index(last_visual, max(self._fill_source_cols))
            )
        data.modified = True
        self.fill_completed.emit()
        self._cleanup_fill()

    def _cleanup_fill(self):
        self._fill_dragging = False
        self._fill_hovering = False
        self._fill_source_rows = []
        self._fill_source_cols = []
        self._fill_preview_rows = 0
        self.viewport().unsetCursor()
        self.viewport().update()

    # ---- 填充值计算 ----
    @staticmethod
    def _compute_fill(values, offset):
        """根据选中值列表和偏移量计算填充值
        - 单个值 + 数字 → 同值填充
        - 多个值 + 数字等差 → 延续等差序列
        - 多个值 + 全部相同数字 → 同值填充
        - 其他情况 → 循环模式
        """
        if not values:
            return ''
        if len(values) == 1:
            v = values[0]
            if isinstance(v, (int, float)):
                return v
            if isinstance(v, str) and v != '':
                try:
                    return int(v) if '.' not in v else float(v)
                except ValueError:
                    pass
            return v

        # 尝试提取数字
        nums = []
        for v in values:
            if isinstance(v, (int, float)):
                nums.append(float(v))
            elif isinstance(v, str) and v != '':
                try:
                    nums.append(float(v))
                except ValueError:
                    nums = None
                    break
            else:
                nums = None
                break

        if nums and len(nums) >= 2:
            diff = nums[1] - nums[0]
            diffs = [nums[j] - nums[j - 1] for j in range(1, len(nums))]
            is_seq = all(abs(d - diff) < 0.0001 for d in diffs)
            if is_seq:
                nxt = nums[-1] + diff * offset
                return int(nxt) if nxt == int(nxt) else nxt

        # 循环模式
        return values[(offset - 1) % len(values)]

    # ---- 绘制填充柄指示器 ----
    def draw_fill_handle(self, painter: QPainter):
        """在视口上绘制填充柄（右下角小方块），支持悬停高亮"""
        if self._fill_dragging:
            return
        fr = self._fill_rect()
        if not fr.isValid():
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)

        if self._fill_hovering:
            # 悬停状态：醒目的蓝色，带边框光晕
            painter.setPen(QPen(QColor('#1565C0'), 2))
            painter.setBrush(QColor('#42A5F5'))
        else:
            # 正常状态：深蓝色小方块
            painter.setPen(QPen(QColor('#1a5276'), 1))
            painter.setBrush(QColor('#2980b9'))

        painter.drawRect(fr)
        painter.restore()

    def paintEvent(self, event):
        """在表格绘制后叠加填充柄指示器"""
        super().paintEvent(event)
        # 无选区或无填充柄需要绘制时直接返回，避免创建 QPainter
        if not self._has_single_selection():
            return
        painter = QPainter(self.viewport())
        self.draw_fill_handle(painter)
        painter.end()


# ============================================================================
# 查找对话框
# ============================================================================

class FindDialog(QDialog):
    """查找对话框"""

    def __init__(self, main_window=None):
        super().__init__(main_window)
        self._main_window = main_window
        self.setWindowTitle("查找")
        self.setFixedSize(420, 180)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)

        # 查找输入
        fl = QFormLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入查找内容...")
        self.search_input.setMinimumHeight(28)
        fl.addRow("查找内容:", self.search_input)

        # 选项
        opts_layout = QHBoxLayout()
        self.case_sensitive = QCheckBox("区分大小写")
        self.case_sensitive.setChecked(False)
        self.whole_column = QCheckBox("搜索所有列")
        self.whole_column.setChecked(True)
        opts_layout.addWidget(self.case_sensitive)
        opts_layout.addWidget(self.whole_column)
        opts_layout.addStretch()
        fl.addRow("", opts_layout)
        layout.addLayout(fl)

        # 结果标签
        self.result_label = QLabel("")
        self.result_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.result_label)

        # 按钮
        btn_layout = QHBoxLayout()
        self.prev_btn = QPushButton("上一个")
        self.next_btn = QPushButton("下一个")
        close_btn = QPushButton("关闭")
        self.prev_btn.setMinimumHeight(30)
        self.next_btn.setMinimumHeight(30)
        close_btn.setMinimumHeight(30)
        btn_layout.addWidget(self.prev_btn)
        btn_layout.addWidget(self.next_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        # 连接
        self.next_btn.clicked.connect(self.on_next)
        self.prev_btn.clicked.connect(self.on_prev)
        close_btn.clicked.connect(self.close)
        self.search_input.textChanged.connect(self.on_text_changed)
        self.search_input.returnPressed.connect(self.on_next)

        self.search_input.setFocus()

    def _mw(self):
        return self._main_window

    def on_text_changed(self, text):
        if self._mw():
            self._mw().perform_search(text)
            self._update_result_label()

    def on_next(self):
        if self._mw():
            self._mw().search_next()
            self._update_result_label()

    def on_prev(self):
        if self._mw():
            self._mw().search_prev()
            self._update_result_label()

    def _update_result_label(self):
        if self._mw():
            proxy = self._mw()._get_proxy()
            if proxy and proxy.search_count > 0:
                self.result_label.setText(
                    f"找到 {proxy.search_count} 个结果，当前第 {proxy.current_search_pos} 个"
                )
            elif proxy:
                self.result_label.setText("未找到结果")
            else:
                self.result_label.setText("")

    def reset(self):
        self.search_input.clear()
        self.result_label.setText("")
        self.search_input.setFocus()


# ============================================================================
# 单元格详情面板
# ============================================================================

class CellDetailPanel(QWidget):
    """可展开/收起的单元格详情面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self.setMaximumHeight(120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        # 信息标签
        info_layout = QHBoxLayout()
        self.field_label = QLabel("字段: -")
        self.field_label.setStyleSheet("font-weight: bold; color: #333;")
        self.row_label = QLabel("行号: -")
        self.row_label.setStyleSheet("color: #666;")
        info_layout.addWidget(self.field_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.row_label)
        info_layout.addStretch()

        copy_btn = QPushButton("复制")
        copy_btn.setFixedSize(50, 24)
        copy_btn.clicked.connect(self._copy_content)
        info_layout.addWidget(copy_btn)
        layout.addLayout(info_layout)

        # 完整内容
        self.content_label = QLabel("")
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content_label.setStyleSheet("""
            QLabel {
                background-color: #fafafa;
                border: 1px solid #ddd;
                border-radius: 3px;
                padding: 6px;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                color: #202020;
            }
        """)
        self.content_label.setMinimumHeight(40)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.content_label)
        scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(scroll)

        self.setStyleSheet("""
            CellDetailPanel {
                background-color: #f5f5f0;
                border-bottom: 2px solid #ccc;
            }
        """)

    def show_detail(self, field_name: str, row_num: int, content: str):
        """显示单元格详情"""
        self.field_label.setText(f"字段: {field_name}")
        self.row_label.setText(f"行号: {row_num}")
        self.content_label.setText(content)
        self.setVisible(True)

    def hide_detail(self):
        """隐藏详情面板"""
        self.setVisible(False)

    def _copy_content(self):
        """复制内容到剪贴板"""
        QApplication.clipboard().setText(self.content_label.text())


# ============================================================================
# 欢迎页
# ============================================================================

class WelcomeWidget(QWidget):
    """无标签页时显示的欢迎引导界面"""

    open_requested = Signal()
    new_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._main_window = parent

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        # 标题
        title = QLabel("Lua Table Editor")
        title.setStyleSheet("font-size: 28px; font-weight: bold; color: #333; margin-bottom: 10px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("游戏策划 Lua 配置表编辑工具")
        subtitle.setStyleSheet("font-size: 14px; color: #888; margin-bottom: 30px;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        # 拖放区域
        drop_zone = QLabel()
        drop_zone.setFixedSize(500, 150)
        drop_zone.setAlignment(Qt.AlignCenter)
        drop_zone.setStyleSheet("""
            QLabel {
                border: 3px dashed #c0c0c0;
                border-radius: 12px;
                background-color: #fafaf8;
                color: #999;
                font-size: 15px;
            }
        """)
        drop_zone.setText("📂  拖拽 .lua 文件到此处打开\n\n或使用下方按钮")
        layout.addWidget(drop_zone, alignment=Qt.AlignCenter)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setAlignment(Qt.AlignCenter)

        open_btn = QPushButton("📂  打开 Lua 文件")
        open_btn.setMinimumSize(160, 42)
        open_btn.setStyleSheet("""
            QPushButton {
                font-size: 14px;
                background-color: #4a90d9;
                color: white;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #3a7bc8; }
            QPushButton:pressed { background-color: #2a6ab8; }
        """)
        open_btn.clicked.connect(lambda: self.open_requested.emit())
        btn_layout.addWidget(open_btn)

        new_btn = QPushButton("✨  新建空白表")
        new_btn.setMinimumSize(160, 42)
        new_btn.setStyleSheet("""
            QPushButton {
                font-size: 14px;
                background-color: #5cb85c;
                color: white;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #4cae4c; }
            QPushButton:pressed { background-color: #3c9e3c; }
        """)
        new_btn.clicked.connect(lambda: self.new_requested.emit())
        btn_layout.addWidget(new_btn)

        layout.addLayout(btn_layout)

        # 提示文字
        tips = QLabel(
            "💡 提示: Ctrl+O 打开文件 | Ctrl+N 新建 | 将 .lua 文件拖入窗口即可打开\n"
            "F1 查看完整使用帮助"
        )
        tips.setStyleSheet("font-size: 12px; color: #aaa; margin-top: 20px;")
        tips.setAlignment(Qt.AlignCenter)
        layout.addWidget(tips)


# ============================================================================
# 工作区面板
# ============================================================================

class WorkspacePanel(QWidget):
    """左侧工作区，以树形结构显示多个文件夹中的 .lua 文件，支持嵌套子文件夹"""

    file_double_clicked = Signal(str)

    # 预设工作区背景色（循环使用）
    DEFAULT_COLORS = [
        "#d8e8d8", "#e8d8e8", "#d8d8e8", "#e8e8d0",
        "#e8d8d0", "#d0e0e8", "#f0e8d0", "#d0e8e0"
    ]

    # ---- 信号 ----
    note_changed = Signal(str, str)   # file_path, note_text

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(180)
        self.setMaximumWidth(500)
        self._workspaces: list = []   # [(folder, color, root_item), ...]
        self._color_index = 0
        self._file_notes: dict = {}   # {file_path: note_text}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 标题栏 ----
        header = QWidget()
        header.setStyleSheet("background-color: #e8e8e0; border-bottom: 1px solid #ccc;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 4, 4)

        title = QLabel("📁 工作区")
        title.setStyleSheet("font-weight: bold; font-size: 13px; border: none;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        add_btn = QPushButton("＋")
        add_btn.setFixedSize(24, 24)
        add_btn.setToolTip("添加文件夹")
        add_btn.setStyleSheet("""
            QPushButton { border: 1px solid #ccc; border-radius: 3px; background: #fff; font-weight: bold; }
            QPushButton:hover { background: #e0e8f0; }
        """)
        add_btn.clicked.connect(self._add_folder)
        header_layout.addWidget(add_btn)

        layout.addWidget(header)

        # ---- 文件搜索栏 ----
        self._search_bar = QLineEdit()
        self._search_bar.setPlaceholderText("🔍 搜索文件名...")
        self._search_bar.setClearButtonEnabled(True)
        self._search_bar.setStyleSheet("""
            QLineEdit {
                border: none;
                border-bottom: 1px solid #d0d0c8;
                padding: 4px 8px;
                font-size: 12px;
                background-color: #fafaf8;
            }
            QLineEdit:focus {
                border-bottom: 2px solid #4a90d9;
            }
        """)
        self._search_bar.textChanged.connect(self._filter_tree)
        layout.addWidget(self._search_bar)

        # ---- 树形视图 ----
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.setAnimated(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setStyleSheet("""
            QTreeWidget {
                background-color: #f8f8f4;
                border: none;
                font-size: 13px;
            }
            QTreeWidget::item {
                padding: 3px 4px;
            }
            QTreeWidget::item:hover {
                background-color: #e8eef4;
            }
            QTreeWidget::item:selected {
                background-color: #d0dce8;
                color: #000;
            }
        """)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        layout.addWidget(self._tree)

    # ---- 公共 API ----

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return
        folder = os.path.abspath(folder)

        for fpath, _, _ in self._workspaces:
            if fpath == folder:
                return

        color = self.DEFAULT_COLORS[self._color_index % len(self.DEFAULT_COLORS)]
        self._color_index += 1
        self.add_folder(folder, color)

    def add_folder(self, folder: str, color: str = None):
        """添加文件夹到工作区"""
        folder = os.path.abspath(folder)
        for fpath, _, _ in self._workspaces:
            if fpath == folder:
                return

        if color is None:
            color = self.DEFAULT_COLORS[self._color_index % len(self.DEFAULT_COLORS)]
            self._color_index += 1

        # ---- 与其他工作区之间用分隔线隔开 ----
        if self._workspaces:
            sep = QTreeWidgetItem()
            sep.setFlags(Qt.NoItemFlags)
            sep.setSizeHint(0, QSize(0, 5))
            sep.setBackground(0, QBrush(QColor("#b0b0a8")))
            self._tree.addTopLevelItem(sep)

        # ---- 工作区根节点 ----
        name = os.path.basename(folder) or folder
        root_item = QTreeWidgetItem()
        root_item.setText(0, f"📂 {name}")
        root_item.setData(0, Qt.UserRole, folder)
        root_item.setData(0, Qt.UserRole + 1, 'workspace_root')
        root_item.setToolTip(0, folder)
        font = root_item.font(0)
        font.setBold(True)
        root_item.setFont(0, font)
        root_item.setFlags(root_item.flags() | Qt.ItemIsAutoTristate)

        bg_color = QColor(color)
        root_item.setBackground(0, QBrush(bg_color))

        self._tree.addTopLevelItem(root_item)
        self._workspaces.append((folder, color, root_item))
        self._populate_recursive(root_item, folder)
        root_item.setExpanded(True)

    # ---- 递归扫描 ----
    def _populate_recursive(self, parent_item: QTreeWidgetItem, folder: str):
        """递归扫描目录：子目录 → 文件夹节点，.lua → 文件节点"""
        parent_item.takeChildren()
        try:
            entries = sorted(os.listdir(folder))
        except PermissionError:
            return

        dirs = []
        files = []
        for entry in entries:
            if entry.startswith('.'):
                continue
            full = os.path.join(folder, entry)
            if os.path.isdir(full):
                dirs.append(entry)
            elif os.path.isfile(full) and entry.lower().endswith('.lua'):
                files.append(entry)

        # 子目录
        for d in dirs:
            full = os.path.join(folder, d)
            dir_item = QTreeWidgetItem()
            dir_item.setText(0, f"📁 {d}")
            dir_item.setData(0, Qt.UserRole, full)
            dir_item.setData(0, Qt.UserRole + 1, 'directory')
            dir_item.setToolTip(0, full)
            parent_item.addChild(dir_item)
            self._populate_recursive(dir_item, full)

        # .lua 文件
        for f in files:
            full = os.path.join(folder, f)
            file_item = QTreeWidgetItem()
            has_note = full in self._file_notes and bool(self._file_notes[full].strip())
            prefix = "📝 " if has_note else "📄 "
            file_item.setText(0, f"{prefix}{f}")
            file_item.setData(0, Qt.UserRole, full)
            file_item.setData(0, Qt.UserRole + 1, 'file')
            if has_note:
                note = self._file_notes[full]
                tip = note if len(note) <= 100 else note[:100] + "..."
                file_item.setToolTip(0, f"{full}\n\n备注: {tip}")
            else:
                file_item.setToolTip(0, full)
            parent_item.addChild(file_item)

    def remove_folder(self, folder: str):
        """移除工作区中的文件夹"""
        folder = os.path.abspath(folder)
        for i, (fpath, color, item) in enumerate(self._workspaces):
            if fpath == folder:
                idx = self._tree.indexOfTopLevelItem(item)
                if idx >= 0:
                    self._tree.takeTopLevelItem(idx)
                self._workspaces.pop(i)
                self._cleanup_separators()
                return

    def _cleanup_separators(self):
        """删除多余的空白分隔行"""
        remove_indices = []
        for i in range(self._tree.topLevelItemCount()):
            ti = self._tree.topLevelItem(i)
            if ti.flags() == Qt.NoItemFlags:
                remove_indices.append(i)
        for i in reversed(remove_indices):
            self._tree.takeTopLevelItem(i)

    def refresh_folder(self, folder: str = None):
        """刷新指定文件夹或全部文件夹"""
        for fpath, color, item in self._workspaces:
            if folder is None or fpath == os.path.abspath(folder):
                self._populate_recursive(item, fpath)

    def refresh_all(self):
        self.refresh_folder()

    # ---- 文件名搜索 ----
    def _filter_tree(self, text: str):
        """根据搜索文本过滤树形视图"""
        text = text.strip().lower()
        # 阻止树刷新信号避免性能问题
        self._tree.setUpdatesEnabled(False)
        try:
            for i in range(self._tree.topLevelItemCount()):
                item = self._tree.topLevelItem(i)
                node_type = item.data(0, Qt.UserRole + 1)
                if node_type == 'workspace_root':
                    self._filter_item_recursive(item, text)
        finally:
            self._tree.setUpdatesEnabled(True)

    def _filter_item_recursive(self, item: QTreeWidgetItem, text: str):
        """递归过滤单个树节点及其子节点，返回是否需要保持可见"""
        node_type = item.data(0, Qt.UserRole + 1)

        if not text:
            # 无搜索文本：全部显示
            item.setHidden(False)
            for i in range(item.childCount()):
                self._filter_item_recursive(item.child(i), text)
            return True

        # 有搜索文本时检查叶子节点
        if node_type == 'file':
            name = item.text(0).lower()
            visible = text in name
            item.setHidden(not visible)
            return visible

        # 目录/根节点：有匹配子节点时可见
        any_child_visible = False
        for i in range(item.childCount()):
            child = item.child(i)
            if self._filter_item_recursive(child, text):
                any_child_visible = True

        item.setHidden(not any_child_visible)
        if any_child_visible:
            item.setExpanded(True)
        return any_child_visible

    # ---- 文件备注 ----
    def set_file_notes(self, notes: dict):
        """批量设置文件备注 {'file_path': 'note'}"""
        self._file_notes = notes or {}
        # 重新刷新树节点显示
        for fpath, color, item in self._workspaces:
            self._populate_recursive(item, fpath)

    def get_file_notes(self) -> dict:
        """获取所有文件备注"""
        return self._file_notes

    def set_file_note(self, file_path: str, note_text: str):
        """设置单个文件的备注（空文本=删除备注）"""
        file_path = os.path.abspath(file_path)
        if note_text.strip():
            self._file_notes[file_path] = note_text.strip()
        elif file_path in self._file_notes:
            del self._file_notes[file_path]
        # 刷新树中的显示
        self._update_file_display(file_path)
        self.note_changed.emit(file_path, note_text)

    def _update_file_display(self, file_path: str):
        """根据备注更新树中该文件的显示"""
        file_path = os.path.abspath(file_path)
        has_note = file_path in self._file_notes and bool(self._file_notes[file_path].strip())

        def _update_item(item: QTreeWidgetItem):
            fp = item.data(0, Qt.UserRole)
            if fp and os.path.abspath(fp) == file_path:
                fname = os.path.basename(file_path)
                prefix = "📝 " if has_note else "📄 "
                item.setText(0, f"{prefix}{fname}")
                if has_note:
                    note = self._file_notes[file_path]
                    # 截取前 100 字符作为 tooltip
                    tip = note if len(note) <= 100 else note[:100] + "..."
                    item.setToolTip(0, f"{file_path}\n\n备注: {tip}")
                else:
                    item.setToolTip(0, file_path)
                return True

            for i in range(item.childCount()):
                if _update_item(item.child(i)):
                    return True
            return False

        for i in range(self._tree.topLevelItemCount()):
            _update_item(self._tree.topLevelItem(i))

    def _edit_note(self, file_path: str):
        """弹出对话框编辑文件备注"""
        file_path = os.path.abspath(file_path)
        fname = os.path.basename(file_path)
        current_note = self._file_notes.get(file_path, "")

        dialog = QDialog(self)
        dialog.setWindowTitle(f"编辑备注 - {fname}")
        dialog.setMinimumSize(450, 300)

        dlg_layout = QVBoxLayout(dialog)

        hint_label = QLabel(f"文件: {fname}")
        hint_label.setStyleSheet("font-size: 11px; color: #666; padding: 4px 0;")
        dlg_layout.addWidget(hint_label)

        text_edit = QTextEdit()
        text_edit.setPlaceholderText("输入备注内容...")
        text_edit.setPlainText(current_note)
        text_edit.setStyleSheet("font-size: 13px; padding: 6px;")
        dlg_layout.addWidget(text_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        dlg_layout.addWidget(btn_box)

        if dialog.exec() == QDialog.Accepted:
            new_note = text_edit.toPlainText()
            self.set_file_note(file_path, new_note)

    # ---- 事件 ----
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """双击：文件夹节点 → 展开/折叠，文件节点 → 打开"""
        node_type = item.data(0, Qt.UserRole + 1)
        file_path = item.data(0, Qt.UserRole)

        if node_type == 'file' and file_path and os.path.isfile(file_path):
            self.file_double_clicked.emit(file_path)
        elif node_type in ('directory', 'workspace_root'):
            item.setExpanded(not item.isExpanded())

    def _on_context_menu(self, pos: QPoint):
        """右键菜单"""
        item = self._tree.itemAt(pos)
        if not item:
            return

        node_type = item.data(0, Qt.UserRole + 1)
        file_path = item.data(0, Qt.UserRole)
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { font-size: 13px; }")

        if node_type == 'workspace_root':
            refresh_act = menu.addAction("🔄 刷新")
            color_act = menu.addAction("🎨 更改背景色...")
            reveal_act = menu.addAction("📂 打开文件夹位置")
            menu.addSeparator()
            remove_act = menu.addAction("✖ 移除文件夹")

            action = menu.exec(self._tree.viewport().mapToGlobal(pos))

            if action == refresh_act:
                self._populate_recursive(item, file_path)
            elif action == color_act:
                self._change_workspace_color(file_path)
            elif action == reveal_act:
                self._reveal_in_explorer(file_path)
            elif action == remove_act:
                self.remove_folder(file_path)

        elif node_type == 'directory':
            refresh_act = menu.addAction("🔄 刷新子目录")
            reveal_act = menu.addAction("📂 打开文件夹")
            action = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if action == refresh_act:
                self._populate_recursive(item, file_path)
            elif action == reveal_act:
                self._reveal_in_explorer(file_path)

        elif node_type == 'file':
            open_act = menu.addAction("📄 打开")
            reveal_act = menu.addAction("📂 打开文件所在文件夹")
            menu.addSeparator()
            has_note = file_path in self._file_notes and bool(self._file_notes[file_path].strip())
            if has_note:
                note_act = menu.addAction("📝 编辑备注...")
                view_note_act = menu.addAction("👁 查看备注")
                del_note_act = menu.addAction("🗑 删除备注")
            else:
                note_act = menu.addAction("📝 添加备注...")
                view_note_act = None
                del_note_act = None
            action = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if action == open_act:
                if os.path.isfile(file_path):
                    self.file_double_clicked.emit(file_path)
            elif action == reveal_act:
                self._reveal_in_explorer(file_path)
            elif action == note_act:
                self._edit_note(file_path)
            elif view_note_act and action == view_note_act:
                note_text = self._file_notes.get(file_path, "")
                QMessageBox.information(self, f"备注 - {os.path.basename(file_path)}", note_text)
            elif del_note_act and action == del_note_act:
                self.set_file_note(file_path, "")

    # ---- 在文件管理器中打开 ----
    def _reveal_in_explorer(self, path: str):
        """在系统文件管理器中打开 path 所在位置（文件则定位并选中）"""
        if not path or not os.path.exists(path):
            return
        try:
            if sys.platform.startswith('win'):
                if os.path.isfile(path):
                    # explorer /select, 让资源管理器定位并高亮该文件
                    subprocess.Popen(['explorer', '/select,',
                                      os.path.normpath(path)])
                else:
                    os.startfile(os.path.normpath(path))
            elif sys.platform == 'darwin':
                if os.path.isfile(path):
                    subprocess.Popen(['open', '-R', path])
                else:
                    subprocess.Popen(['open', path])
            else:
                target = path if os.path.isdir(path) else os.path.dirname(path)
                subprocess.Popen(['xdg-open', target])
        except Exception as e:
            QMessageBox.warning(self, "无法打开文件夹", f"打开文件夹失败：\n{e}")

    # ---- 颜色自定义 ----
    def _change_workspace_color(self, folder: str):
        """弹出颜色选择器，更改工作区背景色"""
        for i, (fpath, color, item) in enumerate(self._workspaces):
            if fpath == folder:
                current = QColor(color)
                new_color = QColorDialog.getColor(current, self, "选择工作区背景色")
                if new_color.isValid():
                    hex_c = new_color.name()
                    self._workspaces[i] = (fpath, hex_c, item)
                    item.setBackground(0, QBrush(new_color))
                return

    def set_workspace_colors(self, color_map: dict):
        """批量设置工作区颜色 {folder_path: color}"""
        for i, (fpath, color, item) in enumerate(self._workspaces):
            if fpath in color_map:
                new_c = color_map[fpath]
                self._workspaces[i] = (fpath, new_c, item)
                item.setBackground(0, QBrush(QColor(new_c)))

    # ---- 查询接口 ----
    def get_workspace_for_file(self, file_path: str) -> Optional[str]:
        """根据文件路径找到所属工作区文件夹"""
        fp = os.path.abspath(file_path)
        for fpath, _, _ in self._workspaces:
            try:
                if os.path.commonpath([fp, fpath]) == fpath:
                    return fpath
            except ValueError:
                pass
        return None

    def get_workspace_color(self, workspace_folder: str) -> Optional[str]:
        """获取工作区背景色"""
        for fpath, color, _ in self._workspaces:
            if fpath == workspace_folder:
                return color
        return None

    def get_folders(self) -> list:
        """获取所有工作区文件夹路径"""
        return [fpath for fpath, _, _ in self._workspaces]

    def get_all_workspace_info(self) -> list:
        """获取 [(folder, color), ...] 用于持久化"""
        return [(fpath, color) for fpath, color, _ in self._workspaces]


# ============================================================================
# 帮助对话框
# ============================================================================

class HelpDialog(QDialog):
    """内置帮助文档"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("帮助 - Lua Table Editor")
        self.resize(720, 560)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)

        tab_widget = QTabWidget()
        tab_widget.setStyleSheet("QTabWidget::pane { border: 1px solid #ccc; }")

        # ---- 标签页1: 快速入门 ----
        quick_tab = QTextBrowser()
        quick_tab.setOpenExternalLinks(False)
        quick_tab.setHtml(self._quick_start_html())
        tab_widget.addTab(quick_tab, "快速入门")

        # ---- 标签页2: 快捷键 ----
        shortcuts_tab = QTextBrowser()
        shortcuts_tab.setHtml(self._shortcuts_html())
        tab_widget.addTab(shortcuts_tab, "快捷键")

        # ---- 标签页3: Lua 格式 ----
        format_tab = QTextBrowser()
        format_tab.setHtml(self._format_html())
        tab_widget.addTab(format_tab, "支持的 Lua 格式")

        # ---- 标签页4: 使用技巧 ----
        tips_tab = QTextBrowser()
        tips_tab.setHtml(self._tips_html())
        tab_widget.addTab(tips_tab, "使用技巧")

        layout.addWidget(tab_widget)

        close_btn = QPushButton("关闭")
        close_btn.setMinimumHeight(32)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

    def _base_css(self) -> str:
        return """
            body { font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif;
                   font-size: 14px; color: #333; line-height: 1.8; padding: 16px; }
            h2 { color: #4a90d9; border-bottom: 2px solid #4a90d9; padding-bottom: 8px; }
            h3 { color: #555; margin-top: 20px; }
            table { border-collapse: collapse; width: 100%; margin: 10px 0; }
            th { background-color: #f0f0e8; padding: 8px 12px; text-align: left;
                 border: 1px solid #ddd; font-weight: bold; }
            td { padding: 6px 12px; border: 1px solid #ddd; }
            tr:nth-child(even) { background-color: #fafafa; }
            code { background-color: #f4f4f0; padding: 2px 6px; border-radius: 3px;
                   font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; }
            pre { background-color: #f8f8f4; padding: 12px; border-radius: 6px;
                  border: 1px solid #e0e0d0; font-family: 'Consolas', monospace;
                  font-size: 13px; overflow-x: auto; }
            ul { padding-left: 20px; }
            li { margin: 4px 0; }
            .tip { background-color: #fff8e0; border-left: 4px solid #f0c040;
                   padding: 8px 12px; margin: 10px 0; }
        """

    def _quick_start_html(self) -> str:
        return f"""<html><head><style>{self._base_css()}</style></head><body>
<h2>🚀 快速入门</h2>

<h3>打开 Lua 表文件</h3>
<ul>
    <li>点击工具栏 <b>"📂 打开"</b> 按钮，或按 <code>Ctrl+O</code></li>
    <li>直接将 <code>.lua</code> 文件<b>拖拽</b>到程序窗口</li>
    <li>支持同时打开<b>多个文件</b>，通过标签页切换</li>
</ul>

<h3>编辑数据</h3>
<ul>
    <li><b>双击</b>任意单元格即可进入编辑模式</li>
    <li>选中单元格后<b>直接输入任意字符</b>，自动进入编辑模式</li>
    <li>选中单元格后按 <b>Backspace</b> 键，直接<b>清空</b>该单元格内容</li>
    <li>按 <code>Enter</code> 键确认编辑并移动到下一行</li>
    <li>按 <code>Tab</code> 移动到下一个单元格</li>
    <li>编辑后标签页标题会显示 <b>*</b> 标记，表示有未保存更改</li>
</ul>

<h3>撤销与重做</h3>
<ul>
    <li>按 <code>Ctrl+Z</code> <b>撤销</b>最近一次单元格编辑</li>
    <li>按 <code>Ctrl+Y</code> 或 <code>Ctrl+Shift+Z</code> <b>重做</b>被撤销的编辑</li>
    <li>撤销历史记录逐格追踪，每次填充操作也独立记录</li>
    <li><b>新建/打开</b>文件时会清空当前撤销历史</li>
</ul>

<h3>多单元格选择</h3>
<ul>
    <li>按住 <b>Ctrl</b> 并点击单元格，可<b>不连续多选</b>独立单元格（高亮显示）</li>
    <li>按住 <b>Shift</b> 并点击单元格，以当前选中格为起点<b>框选矩形区域</b></li>
    <li>鼠标拖动可直接框选连续矩形区域</li>
    <li><b>提示</b>：Ctrl 点选用于非连续多选，Shift 点选用于区域跨度选择</li>
</ul>

<h3>保存文件</h3>
<ul>
    <li>按 <code>Ctrl+S</code> 快速保存当前文件</li>
    <li>按 <code>Ctrl+Shift+S</code> 另存为新文件</li>
    <li>关闭程序时会自动提示保存所有未保存的标签页</li>
</ul>

<h3>添加和删除行</h3>
<ul>
    <li>工具栏 <b>"➕ 数据行"</b> 在当前行下方添加数据行</li>
    <li>工具栏 <b>"💬 注释行"</b> 添加灰色斜体的注释行</li>
    <li>选中行后按 <code>Delete</code> 或工具栏 <b>"➖ 删除行"</b></li>
</ul>

<div class='tip'>💡 <b>注释行</b>使用灰色斜体显示，横跨所有列，不影响数据结构。</div>
</body></html>"""

    def _shortcuts_html(self) -> str:
        return f"""<html><head><style>{self._base_css()}</style></head><body>
<h2>⌨️ 快捷键一览</h2>

<table>
<tr><th>快捷键</th><th>功能</th></tr>
<tr><td><code>Ctrl+O</code></td><td>打开 Lua 文件</td></tr>
<tr><td><code>Ctrl+N</code></td><td>新建空白表</td></tr>
<tr><td><code>Ctrl+S</code></td><td>保存当前文件</td></tr>
<tr><td><code>Ctrl+Shift+S</code></td><td>另存为</td></tr>
<tr><td><code>F5</code></td><td>刷新当前文件</td></tr>
<tr><td><code>Ctrl+W</code></td><td>关闭当前标签页</td></tr>
<tr><td><code>Ctrl+F</code></td><td>查找数据</td></tr>
<tr><td><code>Ctrl+B</code></td><td>展开/收起工作区面板</td></tr>
<tr><td><code>Ctrl+D</code></td><td>展开/收起详情栏</td></tr>
<tr><td><code>Ctrl+Shift+N</code></td><td>添加数据行</td></tr>
<tr><td><code>Ctrl+Shift+M</code></td><td>添加复数行（弹出菜单）</td></tr>
<tr><td><code>Ctrl+Shift+L</code></td><td>添加新列（字段）</td></tr>
<tr><td><code>Ctrl+Shift+C</code></td><td>添加注释行</td></tr>
<tr><td><code>Delete</code></td><td>删除选中行</td></tr>
<tr><td><code>Ctrl+Z</code></td><td>撤销编辑</td></tr>
<tr><td><code>Ctrl+Y</code></td><td>重做编辑</td></tr>
<tr><td><code>F1</code></td><td>打开帮助文档</td></tr>
<tr><td><code>Enter</code></td><td>确认编辑并跳到下一行（编辑时）</td></tr>
<tr><td><code>Tab</code></td><td>移动到下一个单元格</td></tr>
</table>
</body></html>"""

    def _format_html(self) -> str:
        return f"""<html><head><style>{self._base_css()}</style></head><body>
<h2>📄 支持的 Lua 表格式</h2>

<h3>格式1: 匿名数组（最常用）</h3>
<pre>ItemTable = {{
    {{id=1, name="Iron Sword", type="weapon", price=100}},
    {{id=2, name="Shield", type="armor", price=50}},
}}</pre>

<h3>格式2: [key] = {{value}}（中文策划常用）</h3>
<pre>ConfigTable = {{
    [10001] = {{id=10001, name="Sword", type="weapon"}},
    [10002] = {{id=10002, name="Staff", type="magic"}},
}}</pre>

<h3>格式3: return {{}}（Lua 模块格式）</h3>
<pre>return {{
    {{id=1, name="Test", value=100}},
    {{id=2, name="Test2", value=200}},
}}</pre>

<h3>格式4: local 变量声明</h3>
<pre>local ItemTable = {{
    {{id=1, name="Item"}},
}}</pre>

<h3>注释行支持</h3>
<pre>WeaponTable = {{
    {{id=1, name="Sword"}},
    -- 这是注释行，会保留在输出中
    {{id=2, name="Staff"}},
}}</pre>

<div class='tip'>💡 注释行以 <code>--</code> 开头，在编辑器中以灰色斜体显示，导出时会保留。</div>
</body></html>"""

    def _tips_html(self) -> str:
        return f"""<html><head><style>{self._base_css()}</style></head><body>
<h2>💡 使用技巧</h2>

<h3>筛选功能</h3>
<ul>
    <li><b>右键点击列头</b> → 选择筛选条件（等于/不等于/包含/不包含）</li>
    <li>筛选后仅显示符合条件的行，<b>数据不会被删除</b></li>
    <li>再次右键列头 → <b>"清除筛选"</b> 恢复全部数据</li>
    <li>注释行始终显示，不参与筛选</li>
</ul>

<h3>查找功能</h3>
<ul>
    <li>按 <code>Ctrl+F</code> 打开查找对话框</li>
    <li>输入关键词后会显示匹配数量</li>
    <li>点击 <b>"上一个"/"下一个"</b> 在结果间跳转</li>
</ul>

<h3>详情栏</h3>
<ul>
    <li>按 <code>Ctrl+D</code> 展开详情栏</li>
    <li>点击任意单元格即可在详情栏查看<b>完整内容</b></li>
    <li>对于超长文本（大段描述、JSON 数据等），在详情栏可以完整查看</li>
    <li>详情栏中的内容<b>可选择和复制</b></li>
</ul>

<h3>工作区面板</h3>
<ul>
    <li>按 <code>Ctrl+B</code> 展开/收起左侧工作区面板</li>
    <li>点击 <b>"＋"</b> 按钮添加文件夹到工作区</li>
    <li>支持<b>添加多个工作区文件夹</b>，不同工作区之间用分隔线和<b>自定义背景色</b>区分</li>
    <li>工作区支持<b>嵌套子文件夹</b>，以树形结构展示</li>
    <li>右键工作区根节点 → <b>"🎨 更改背景色"</b>可自定义颜色</li>
    <li>双击文件夹节点可展开/折叠子目录</li>
    <li>工作区中<b>双击</b> .lua 文件即可打开编辑</li>
    <li><b>标签页顶部会显示对应工作区的颜色带条</b>，直观标识表所属工作区</li>
    <li><b>右键文件夹</b>可刷新或移除</li>
    <li>工作区文件夹列表和颜色会<b>自动记忆</b>，下次启动恢复</li>
</ul>

<h3>表格操作</h3>
<ul>
    <li><b>点击单元格</b>选中，会出现<b>黑色边框</b>提示</li>
    <li><b>拖拽鼠标</b>可连续选中多个单元格</li>
    <li>鼠标<b>悬停</b>单元格可显示完整 ToolTip</li>
    <li><b>拖拽列头边缘</b>可调整列宽</li>
    <li><b>右键表格</b>可快速添加/删除行、添加列</li>
    <li><b>Shift+点击</b>可多选行进行批量删除</li>
</ul>

<h3>自动填充（填充柄）</h3>
<ul>
    <li>选中<b>多个单元格</b>后，右下角会出现一个<b>蓝色小方块</b>（填充柄）</li>
    <li>鼠标<b>悬停在填充柄上</b>时，方块会变为<b>亮蓝色</b>，光标变为<b>上下箭头 ↔</b>，提示可拖拽</li>
    <li><b>拖拽填充柄</b>向下，可自动填充数据</li>
    <li>若同列为<b>等差数字</b>（如 2, 3, 4），拖拽后自动延续序列（5, 6, 7...）</li>
    <li>若仅<b>选中一个单元格</b>，拖拽后将<b>重复该值</b></li>
    <li>支持<b>多列同时填充</b>，各列独立计算</li>
</ul>

<h3>批量操作</h3>
<ul>
    <li>工具栏 <b>"➕ 数据行"</b> → 弹出菜单选 3/5/20 行或自定义数量</li>
    <li>工具栏 <b>"➕ 添加列"</b> → 输入新字段名添加列</li>
    <li>选中多行 → 右键 <b>"删除选中行"</b> 批量删除</li>
    <li>支持 <b>Ctrl+Click</b> 和 <b>Shift+Click</b> 多选</li>
</ul>
</body></html>"""


# ============================================================================
# 关于对话框
# ============================================================================

class AboutDialog(QDialog):
    """关于对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于 - Lua Table Editor")
        self.setFixedSize(400, 260)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        title = QLabel("Lua Table Editor")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #333;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        ver = QLabel("版本 1.0")
        ver.setStyleSheet("font-size: 13px; color: #888;")
        ver.setAlignment(Qt.AlignCenter)
        layout.addWidget(ver)

        layout.addSpacing(15)

        desc = QLabel(
            "专为游戏策划设计的 Lua 配置表编辑工具\n\n"
            "• Excel 风格表格编辑\n"
            "• 支持多标签页、注释行\n"
            "• 列筛选、全文查找\n"
            "• 拖拽打开、快捷键操作\n"
            "• 支持 [key]={value} 等多种 Lua 格式"
        )
        desc.setStyleSheet("font-size: 13px; color: #555; line-height: 1.6;")
        desc.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc)

        layout.addSpacing(15)

        close_btn = QPushButton("确定")
        close_btn.setMinimumSize(80, 32)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn, alignment=Qt.AlignCenter)

# ============================================================================
# 后台加载线程：把文件读取 + 解析放到子线程，避免大表解析阻塞 UI 主线程
# ============================================================================
class _LoadSignals(QObject):
    finished = Signal(object, str)   # (LuaTableData, file_path)
    error = Signal(str)               # error_message


class LuaLoadWorker(QThread):
    """在子线程中读取并解析 Lua 文件，完成后通过信号把结果抛回主线程。"""

    def __init__(self, file_path: str):
        super().__init__()
        self._file_path = file_path
        self.signals = _LoadSignals()

    def run(self):
        try:
            with open(self._file_path, 'r', encoding='utf-8') as f:
                text = f.read()
            data = LuaParser.parse(text)
            data.file_path = self._file_path
            data.modified = False
            data.file_mtime = os.path.getmtime(self._file_path)
            self.signals.finished.emit(data, self._file_path)
        except Exception as e:  # 解析/读取异常统一回传
            self.signals.error.emit(str(e))


class MainWindow(QMainWindow):
    """Lua Table Editor 主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lua Table Editor")
        self.resize(1200, 750)
        self.setMinimumSize(800, 500)

        self._tabs_data: dict = {}
        self._find_dialog: Optional[FindDialog] = None
        self._untitled_counter = 1
        self._loading = False  # 防止重复加载请求（手动打开/刷新）
        self._workers: list = []  # 后台加载线程引用，避免被提前回收
        self._context_row = -1  # 右键所在行（用于复制/粘贴整行）

        # 定时自动刷新：对“未修改且磁盘已变更”的已打开表静默重载
        self._auto_refresh_enabled = True
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(30000)  # 30 秒
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_all)
        self._auto_refresh_timer.start()

        # 最近文件
        self._settings = QSettings("LuaTableEditor", "LuaTableEditor")
        self._recent_files = self._load_recent_files()
        self._workspace_folders = self._load_workspace_folders()
        self._file_notes: dict = self._load_file_notes()

        self._setup_menu()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()

        self.setAcceptDrops(True)

        # 连接工作区备注变更信号
        self._workspace_panel.note_changed.connect(self._on_note_changed)

        # 恢复工作区文件夹（含颜色信息）
        for item in self._workspace_folders:
            folder, color = item if isinstance(item, tuple) else (item, '')
            if os.path.isdir(folder):
                self._workspace_panel.add_folder(folder, color if color else None)

        # 恢复文件备注
        self._workspace_panel.set_file_notes(self._file_notes)

        # 首次启动显示欢迎页
        self._show_welcome()

    # ---- 菜单栏 ----
    def _setup_menu(self):
        menubar = self.menuBar()

        # 文件菜单
        file_menu = menubar.addMenu("文件(&F)")
        self._act_new = self._add_action(file_menu, "新建(&N)", "Ctrl+N", self._new_table)
        self._act_open = self._add_action(file_menu, "打开(&O)...", "Ctrl+O", self._open_file)
        self._recent_menu = file_menu.addMenu("最近打开(&R)")
        self._refresh_recent_menu()
        file_menu.addSeparator()
        self._act_save = self._add_action(file_menu, "保存(&S)", "Ctrl+S", self._save_file)
        self._act_save_as = self._add_action(file_menu, "另存为(&A)...", "Ctrl+Shift+S", self._save_file_as)
        file_menu.addSeparator()
        self._act_refresh = self._add_action(file_menu, "刷新(&R)", "F5", self._refresh_file)
        file_menu.addSeparator()
        self._act_close_tab = self._add_action(file_menu, "关闭标签页(&W)", "Ctrl+W", self._close_tab)
        file_menu.addSeparator()
        self._add_action(file_menu, "退出(&Q)", "Alt+F4", self.close)

        # 编辑菜单
        edit_menu = menubar.addMenu("编辑(&E)")
        self._act_add_row = self._add_action(edit_menu, "添加数据行", "Ctrl+Shift+N", self._add_row)
        self._act_add_rows = self._add_action(edit_menu, "添加复数行...", "Ctrl+Shift+M", self._add_rows_batch)
        self._act_add_column = self._add_action(edit_menu, "添加列...", "Ctrl+Shift+L", self._add_column)
        edit_menu.addSeparator()
        self._act_add_comment = self._add_action(edit_menu, "添加注释行", "Ctrl+Shift+C", self._add_comment_row)
        self._act_del_row = self._add_action(edit_menu, "删除当前行", "Delete", self._delete_row)
        edit_menu.addSeparator()
        self._act_comment_row = self._add_action(edit_menu, "注释选中行", "Ctrl+/", self._comment_selected_rows)
        self._act_uncomment_row = self._add_action(edit_menu, "取消注释选中行", "Ctrl+Shift+/", self._uncomment_selected_rows)
        edit_menu.addSeparator()
        self._act_repair_keys = self._add_action(edit_menu, "修复行键 (从 id 恢复 [key]=)", "Ctrl+Shift+K", self._repair_row_keys)

        # 查看菜单
        view_menu = menubar.addMenu("查看(&V)")
        self._act_toggle_workspace = self._add_action(view_menu, "工作区面板(&W)", "Ctrl+B", self._toggle_workspace)
        self._act_toggle_detail = self._add_action(view_menu, "详情栏(&D)", "Ctrl+D", self._toggle_detail)
        view_menu.addSeparator()
        self._act_auto_refresh = QAction("自动刷新打开的表(&A)", self)
        self._act_auto_refresh.setCheckable(True)
        self._act_auto_refresh.setChecked(self._auto_refresh_enabled)
        self._act_auto_refresh.triggered.connect(self._toggle_auto_refresh)
        view_menu.addAction(self._act_auto_refresh)

        # 查找菜单
        find_menu = menubar.addMenu("查找(&S)")
        self._act_find = self._add_action(find_menu, "查找(&F)...", "Ctrl+F", self._show_find_dialog)

        # 帮助菜单
        help_menu = menubar.addMenu("帮助(&H)")
        self._act_help = self._add_action(help_menu, "使用帮助(&H)", "F1", self._show_help)
        help_menu.addSeparator()
        self._act_about = self._add_action(help_menu, "关于(&A)", "", self._show_about)

    def _add_action(self, menu, text, shortcut, slot) -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        menu.addAction(act)
        return act

    def _toggle_auto_refresh(self, checked: bool):
        """切换定时自动刷新开关"""
        self._auto_refresh_enabled = checked
        if checked:
            # 立即检查一次，避免开启后还要等一个周期
            self._auto_refresh_all()
        self._status_label.setText(f"自动刷新已{'开启' if checked else '关闭'}")

    # ---- 工具栏 ----
    def _setup_toolbar(self):
        self._toolbar = QToolBar("工具栏")
        self._toolbar.setMovable(False)
        self._toolbar.setIconSize(QSize(20, 20))
        self._toolbar.setStyleSheet("""
            QToolBar { spacing: 4px; padding: 4px 8px; background: #e8e8e0; border-bottom: 1px solid #ccc; }
            QToolButton { padding: 5px 12px; border: 1px solid #ccc; border-radius: 3px; background: #fff; }
            QToolButton:hover { background: #e0e8f0; border-color: #aaa; }
            QToolButton:pressed { background: #c8d0d8; }
        """)
        self.addToolBar(self._toolbar)

        self._tb_open = self._add_toolbar_btn("📂 打开", self._open_file)
        self._tb_save = self._add_toolbar_btn("💾 保存", self._save_file)
        self._tb_refresh = self._add_toolbar_btn("🔄 刷新", self._refresh_file)
        self._toolbar.addSeparator()
        self._tb_add = self._add_toolbar_btn("➕ 数据行", self._add_rows_batch)
        self._tb_add_col = self._add_toolbar_btn("➕ 添加列", self._add_column)
        self._tb_comment = self._add_toolbar_btn("💬 注释行", self._add_comment_row)
        self._tb_del = self._add_toolbar_btn("➖ 删除行", self._delete_row)
        self._toolbar.addSeparator()
        self._tb_detail = self._add_toolbar_btn("📋 详情栏", self._toggle_detail)
        self._tb_workspace = self._add_toolbar_btn("📁 工作区", self._toggle_workspace)
        self._tb_find = self._add_toolbar_btn("🔍 查找", self._show_find_dialog)
        self._toolbar.addSeparator()
        self._tb_help = self._add_toolbar_btn("❓ 帮助", self._show_help)

    def _add_toolbar_btn(self, text, slot):
        btn = QPushButton(text)
        btn.setMinimumHeight(30)
        btn.clicked.connect(slot)
        self._toolbar.addWidget(btn)
        return btn

    # ---- 中央区域 ----
    def _setup_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # 欢迎页
        self._welcome_widget = WelcomeWidget(self)
        self._welcome_widget.open_requested.connect(self._open_file)
        self._welcome_widget.new_requested.connect(self._new_table)

        # 标签页容器
        tab_container = QWidget()
        tab_layout = QVBoxLayout(tab_container)
        tab_layout.setSpacing(0)
        tab_layout.setContentsMargins(0, 0, 0, 0)

        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.setMovable(True)
        self._tab_widget.setDocumentMode(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_request)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background: #f0f0f0;
            }
            QTabBar::tab {
                padding: 6px 16px;
                margin-right: 2px;
                border: 1px solid #ccc;
                border-bottom: none;
                background: #e0e0d8;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-bottom: 2px solid #4a90d9;
            }
            QTabBar::tab:hover:!selected {
                background: #ecece4;
            }
        """)
        tab_layout.addWidget(self._tab_widget)

        # 详情面板
        self._detail_panel = CellDetailPanel()
        tab_layout.addWidget(self._detail_panel)

        # 堆叠布局：欢迎页 / 标签页容器
        self._stack = QStackedWidget()
        self._stack.addWidget(self._welcome_widget)   # index 0
        self._stack.addWidget(tab_container)           # index 1

        # 工作区面板（左侧）
        self._workspace_panel = WorkspacePanel(self)
        self._workspace_panel.file_double_clicked.connect(self._open_file_path)

        # 分割器：工作区 | 主内容区
        self._splitter = QSplitter(Qt.Horizontal)
        self._splitter.addWidget(self._workspace_panel)
        self._splitter.addWidget(self._stack)
        self._splitter.setStretchFactor(0, 0)  # 工作区不拉伸
        self._splitter.setStretchFactor(1, 1)  # 主内容区拉伸
        self._splitter.setSizes([220, 980])
        self._splitter.setHandleWidth(3)
        self._splitter.setStyleSheet("QSplitter::handle { background-color: #ccc; }")

        layout.addWidget(self._splitter)

    # ---- 状态栏 ----
    def _setup_statusbar(self):
        self._statusbar = QStatusBar()
        self._statusbar.setStyleSheet("QStatusBar { background: #e8e8e0; border-top: 1px solid #ccc; }")
        self.setStatusBar(self._statusbar)
        self._status_label = QLabel("就绪")
        self._statusbar.addWidget(self._status_label)

        # 加载进度条（interminate 动画，默认隐藏）
        self._loading_progress = QProgressBar()
        self._loading_progress.setRange(0, 0)  # 不确定模式：滚动动画
        self._loading_progress.setMaximumWidth(180)
        self._loading_progress.setMaximumHeight(18)
        self._loading_progress.setTextVisible(False)
        self._loading_progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #c0c0c0;
                border-radius: 3px;
                background: #f0f0e8;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #5b9bd5, stop:0.5 #7db9e6, stop:1 #5b9bd5);
                border-radius: 2px;
            }
        """)
        self._loading_progress.hide()
        self._statusbar.addPermanentWidget(self._loading_progress)

        self._filter_status = QLabel("")
        self._statusbar.addPermanentWidget(self._filter_status)

    # ---- 欢迎页 / 标签页切换 ----
    def _show_welcome(self):
        self._stack.setCurrentIndex(0)

    def _show_tabs(self):
        self._stack.setCurrentIndex(1)

    # ---- 最近文件 ----
    def _load_recent_files(self) -> list:
        files = self._settings.value("recentFiles", [])
        if isinstance(files, str):
            files = [files]
        return [f for f in (files or []) if os.path.exists(f)][:8]

    def _save_recent_files(self):
        self._settings.setValue("recentFiles", self._recent_files)

    def _add_recent_file(self, file_path: str):
        file_path = os.path.abspath(file_path)
        if file_path in self._recent_files:
            self._recent_files.remove(file_path)
        self._recent_files.insert(0, file_path)
        self._recent_files = self._recent_files[:8]
        self._save_recent_files()
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        if not self._recent_files:
            self._recent_menu.addAction("(无最近文件)").setEnabled(False)
        else:
            for f in self._recent_files:
                act = self._recent_menu.addAction(os.path.basename(f))
                act.setToolTip(f)
                act.triggered.connect(lambda checked, path=f: self._open_recent(path))
        self._recent_menu.addSeparator()
        clear_act = self._recent_menu.addAction("清除最近文件")
        clear_act.triggered.connect(self._clear_recent_files)

    def _open_recent(self, file_path: str):
        if not os.path.exists(file_path):
            QMessageBox.warning(self, "文件不存在", f"文件已不存在:\n{file_path}")
            self._recent_files.remove(file_path)
            self._save_recent_files()
            self._refresh_recent_menu()
            return
        self._open_file_path(file_path)

    def _clear_recent_files(self):
        self._recent_files = []
        self._save_recent_files()
        self._refresh_recent_menu()

    def _load_workspace_folders(self) -> list:
        """返回 [(folder, color), ...]"""
        raw = self._settings.value("workspaceFolders", [])
        if isinstance(raw, str):
            raw = [raw]
        if not raw:
            return []

        # 支持旧格式（纯路径列表）和新格式（含颜色信息）
        result = []
        for item in (raw or []):
            if isinstance(item, dict):
                folder = item.get('path', '')
                color = item.get('color', '')
                if os.path.isdir(folder):
                    result.append((folder, color))
            elif isinstance(item, str):
                if os.path.isdir(item):
                    result.append((item, ''))
        return result

    def _save_workspace_folders(self):
        """保存工作区文件夹及颜色到设置"""
        info_list = self._workspace_panel.get_all_workspace_info()
        # 保存为 dict 列表以兼容扩展
        data = [{'path': f, 'color': c} for f, c in info_list]
        self._settings.setValue("workspaceFolders", data)

    def _load_file_notes(self) -> dict:
        """从设置中加载文件备注"""
        raw = self._settings.value("fileNotes", {})
        if isinstance(raw, dict):
            return raw
        return {}

    def _save_file_notes(self):
        """保存文件备注到设置"""
        notes = self._workspace_panel.get_file_notes()
        self._settings.setValue("fileNotes", notes)
        # 同步本地缓存
        self._file_notes = notes

    def _on_note_changed(self, file_path: str, note_text: str):
        """备注变更后的回调"""
        self._save_file_notes()

    # ---- 帮助 ----
    def _show_help(self):
        dialog = HelpDialog(self)
        dialog.exec()

    def _show_about(self):
        dialog = AboutDialog(self)
        dialog.exec()

    # ---- 快捷键 ----
    def _setup_shortcuts(self):
        pass  # 已通过菜单 QAction 绑定

    # ---- 标签页管理 ----
    def _create_tab(self, data: LuaTableData, title: str,
                    workspace_color: str = None) -> QWidget:
        """创建新标签页"""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 工作区颜色带条 ----
        if workspace_color:
            color_strip = QFrame()
            color_strip.setFixedHeight(3)
            color_strip.setStyleSheet(
                f"background-color: {workspace_color}; border: none;"
            )
            layout.addWidget(color_strip)

        # 表格视图
        table_view = LuaTableView()
        table_view.setAlternatingRowColors(False)
        table_view.setSelectionBehavior(QAbstractItemView.SelectItems)  # 单元格级选中
        table_view.setSelectionMode(QAbstractItemView.ExtendedSelection)  # Ctrl/Shift 多选
        table_view.setShowGrid(True)
        table_view.setSortingEnabled(False)
        table_view.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        table_view.customContextMenuRequested.connect(self._on_context_menu)

        # ---- 行头：可点击选中整行 + 可拖动调整行高 ----
        vheader = table_view.verticalHeader()
        vheader.setVisible(True)
        vheader.setSectionsClickable(True)
        vheader.setSectionResizeMode(QHeaderView.Interactive)
        vheader.setDefaultSectionSize(28)
        vheader.setMinimumSectionSize(20)
        vheader.sectionClicked.connect(
            lambda idx: table_view.selectRow(idx)
        )

        # ---- 列头：可拖动调整列宽 ----
        header = table_view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.sectionClicked.connect(self._on_header_clicked)
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos: self._on_header_context_menu(header, pos))
        # overlay 表头（冻结列）的右键菜单也走同一处理函数
        table_view.header_context_menu_requested.connect(self._on_header_context_menu)

        # 模型和代理
        model = LuaTableModel()
        model.set_data(data)
        proxy = FilterProxyModel()
        proxy.setSourceModel(model)
        table_view.setModel(proxy)

        # 委托
        delegate = LuaTableDelegate()
        table_view.setItemDelegate(delegate)

        # 选中变化时更新详情面板
        table_view.selectionModel().selectionChanged.connect(
            lambda sel, desel: self._on_selection_changed(table_view)
        )

        # 当前单元格变化时更新行列高亮
        table_view.selectionModel().currentChanged.connect(
            table_view._on_current_changed
        )

        # 数据变化时更新标题
        model.dataChanged.connect(lambda: self._update_tab_title())
        table_view.fill_completed.connect(lambda: self._on_fill_completed(tab))
        table_view.copy_requested.connect(self._copy_selection)
        table_view.paste_requested.connect(self._paste_at_selection)

        # 安装事件过滤器处理 Enter 键导航
        table_view.installEventFilter(self)

        # 设置列宽
        header.setMinimumSectionSize(60)
        header.setDefaultSectionSize(120)
        header.resizeSection(0, 50)  # 行号列窄一点
        header.setStretchLastSection(True)

        # 存储关联数据
        idx = self._tab_widget.addTab(tab, title)
        self._tab_widget.setCurrentIndex(idx)
        self._show_tabs()  # 切换到标签页视图

        self._tabs_data[id(tab)] = {
            'data': data,
            'model': model,
            'proxy': proxy,
            'view': table_view,
            'tab': tab,
            'title': title,
            'workspace_folder': '',
            'workspace_color': workspace_color,
        }

        # 延迟自动调整列宽（延迟 150ms 等 UI 渲染稳定后再测量）
        QTimer.singleShot(150, lambda: self._auto_resize_columns(table_view))

        # 样式和布局
        table_view.setStyleSheet("""
            QTableView {
                gridline-color: #e0e0e0;
                font-size: 13px;
                selection-background-color: #b8d4f0;
                selection-color: #000;
            }
            QHeaderView::section {
                background-color: #f0f0e8;
                padding: 6px;
                border: 1px solid #d0d0c8;
                border-top: none;
                font-weight: bold;
                font-size: 13px;
            }
        """)

        layout.addWidget(table_view)

        return tab

    def _auto_resize_columns(self, view: QTableView):
        """自动调整列宽以适应内容"""
        header = view.horizontalHeader()
        header.resizeSection(0, 50)  # 行号列固定
        # 一次性测量所有列，避免重复 O(n_cols * n_rows * n_cols) 的代价
        header.resizeSections(QHeaderView.ResizeToContents)
        # 然后对个别过宽的列进行缩窄
        for col in range(1, header.count()):
            if header.sectionSize(col) > 280:
                header.resizeSection(col, 280)

    def eventFilter(self, obj, event):
        """事件过滤器：Undo/Redo + Enter 键导航"""
        if event.type() == QEvent.KeyPress:
            td = self._get_current_tab_data()
            if td and obj is td['view']:
                view = td['view']
                idx = view.currentIndex()
                model = view.model()
                if isinstance(model, QSortFilterProxyModel):
                    src = model.sourceModel()
                else:
                    src = model

                # ---- Ctrl+Z 撤销 ----
                if event.matches(QKeySequence.StandardKey.Undo):
                    if isinstance(src, LuaTableModel) and src.has_undo():
                        if view.state() == QAbstractItemView.EditingState:
                            return super().eventFilter(obj, event)
                        if src.undo():
                            self._update_tab_title()
                            self._status_label.setText("已撤销 (Ctrl+Z)")
                            self._update_statusbar()
                        return True

                # ---- Ctrl+Y / Ctrl+Shift+Z 重做 ----
                is_redo = event.matches(QKeySequence.StandardKey.Redo) or (
                    event.key() == Qt.Key_Z and
                    event.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier)
                )
                if is_redo:
                    if isinstance(src, LuaTableModel) and src.has_redo():
                        if view.state() == QAbstractItemView.EditingState:
                            return super().eventFilter(obj, event)
                        if src.redo():
                            self._update_tab_title()
                            self._status_label.setText("已重做 (Ctrl+Y)")
                            self._update_statusbar()
                        return True

                if not idx.isValid():
                    return super().eventFilter(obj, event)

                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    # Enter 键：如果正在编辑则不处理，否则移动到下一行同列
                    if view.state() == QAbstractItemView.EditingState:
                        return super().eventFilter(obj, event)
                    next_row = idx.row() + 1
                    if next_row < model.rowCount():
                        next_idx = model.index(next_row, idx.column())
                        view.setCurrentIndex(next_idx)
                        view.edit(next_idx)
                    return True

                if event.key() == Qt.Key_Tab:
                    # Tab：移动到下一列，到最后一列时换行
                    next_col = idx.column() + 1
                    next_row = idx.row()
                    if next_col >= model.columnCount():
                        next_col = 1  # 跳过行号列
                        next_row += 1
                    if next_row < model.rowCount():
                        next_idx = model.index(next_row, next_col)
                        view.setCurrentIndex(next_idx)
                    return True

                if event.key() == Qt.Key_Backtab:
                    # Shift+Tab：移动到上一列
                    prev_col = idx.column() - 1
                    prev_row = idx.row()
                    if prev_col <= 0:
                        prev_col = model.columnCount() - 1
                        prev_row -= 1
                    if prev_row >= 0:
                        prev_idx = model.index(prev_row, prev_col)
                        view.setCurrentIndex(prev_idx)
                    return True

        return super().eventFilter(obj, event)

    def _get_current_tab_data(self):
        """获取当前标签页的数据"""
        tab = self._tab_widget.currentWidget()
        if tab is None:
            return None
        return self._tabs_data.get(id(tab))

    def _get_proxy(self) -> Optional[FilterProxyModel]:
        td = self._get_current_tab_data()
        return td['proxy'] if td else None

    def _get_model(self) -> Optional[LuaTableModel]:
        td = self._get_current_tab_data()
        return td['model'] if td else None

    def _get_view(self) -> Optional[QTableView]:
        td = self._get_current_tab_data()
        return td['view'] if td else None

    def _get_data(self) -> Optional[LuaTableData]:
        td = self._get_current_tab_data()
        return td['data'] if td else None

    def _update_tab_path_label(self, td: dict):
        """更新单个标签页的路径显示和悬浮提示"""
        file_path = td.get('file_path', '')
        base_title = td.get('title', '')
        # 去 * 前缀获得纯文件名
        clean_title = base_title.lstrip('*')
        if file_path:
            folder = os.path.dirname(file_path)
            display_title = f"{clean_title}  [{folder}]"
            self._tab_widget.setTabToolTip(
                self._tab_widget.indexOf(td['tab']),
                file_path
            )
        else:
            display_title = clean_title
        td['display_title'] = display_title

    def _update_tab_title(self):
        """更新当前标签页标题（加 * 表示已修改）"""
        td = self._get_current_tab_data()
        if not td:
            return
        data = td['data']
        base_title = td.get('title', '')
        # 去 * 获得纯粹文件名
        clean_title = base_title.lstrip('*')
        if data.modified:
            modified_prefix = '*' if not base_title.startswith('*') else ''
        else:
            modified_prefix = ''
        # 构建显示标题：*文件名  [路径]
        file_path = td.get('file_path', '')
        if file_path:
            display_title = f"{modified_prefix}{clean_title}"
            self._update_tab_path_label(td)
        else:
            display_title = modified_prefix + clean_title
        idx = self._tab_widget.currentIndex()
        if idx >= 0:
            self._tab_widget.setTabText(idx, display_title)

    def _on_tab_changed(self, index):
        """标签页切换"""
        self._detail_panel.hide_detail()
        self._update_statusbar()

    def _on_tab_close_request(self, index):
        """请求关闭标签页"""
        self._tab_widget.setCurrentIndex(index)
        self._close_tab()

    # ---- 文件操作 ----
    def _new_table(self):
        """新建空白表"""
        c = self._untitled_counter
        self._untitled_counter += 1
        data = LuaTableData(table_name=f"NewTable{c}", field_names=[])
        data.modified = False
        title = f"未命名{c}.lua"
        self._create_tab(data, title)
        self._update_statusbar()
        self._status_label.setText(f"新建空白表 - {title}")

    def _open_file(self):
        """打开 Lua 文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "打开 Lua 表文件", "",
            "Lua 文件 (*.lua);;所有文件 (*)"
        )
        if not file_path:
            return
        self._open_file_path(file_path)

    def _open_file_path(self, file_path: str):
        """打开指定路径的文件（后台线程解析，UI 不再卡顿）"""
        # 检查文件是否已打开（加载前检查可快速返回，无需进度条）
        for tab_id, td in self._tabs_data.items():
            if td['data'].file_path == file_path:
                for i in range(self._tab_widget.count()):
                    if self._tab_widget.widget(i) == td['tab']:
                        self._tab_widget.setCurrentIndex(i)
                        self._status_label.setText(f"已切换到: {file_path}")
                        return

        # 防止重复加载请求（手动加载期间不重复触发）
        if self._loading:
            self._status_label.setText("正在加载中，请稍候...")
            return

        self._start_load(file_path, lambda data, fp: self._finalize_open(data, fp))

    def _finalize_open(self, data: LuaTableData, file_path: str):
        """后台加载完成后创建标签页并收尾（主线程执行）"""
        # 若加载期间该文件已被打开（例如重复点击），直接切换
        for tab_id, td in self._tabs_data.items():
            if td['data'].file_path == file_path:
                for i in range(self._tab_widget.count()):
                    if self._tab_widget.widget(i) == td['tab']:
                        self._tab_widget.setCurrentIndex(i)
                        self._status_label.setText(f"已切换到: {file_path}")
                        return

        title = os.path.basename(file_path)

        # 检测文件所属工作区
        ws_folder = self._workspace_panel.get_workspace_for_file(file_path)
        ws_color = None
        if ws_folder:
            ws_color = self._workspace_panel.get_workspace_color(ws_folder)

        tab = self._create_tab(data, title, workspace_color=ws_color)
        td = self._tabs_data.get(id(tab))
        if td and ws_folder:
            td['workspace_folder'] = ws_folder
        if td:
            td['file_path'] = file_path
            self._update_tab_path_label(td)

        self._add_recent_file(file_path)
        self._update_statusbar()
        self._status_label.setText(f"已打开: {file_path}")

    # ---- 后台加载编排 ----
    def _start_load(self, file_path: str, on_loaded, show_progress=True, auto=False):
        """在后台线程加载文件。

        on_loaded(data, file_path) 在加载完成后于主线程调用。
        show_progress=True 时显示进度条并设置全局 _loading 锁（用于用户主动打开/刷新）；
        auto=True 时不弹进度条、不设全局锁（用于静默的定时自动刷新）。
        """
        if show_progress:
            if self._loading:
                self._status_label.setText("正在加载中，请稍候...")
                return
            self._loading = True
            self._loading_progress.show()
            self._status_label.setText(f"正在加载: {os.path.basename(file_path)}...")
            QApplication.processEvents()  # 让进度条先渲染出来

        worker = LuaLoadWorker(file_path)
        self._workers.append(worker)  # 保持引用，避免被回收
        worker.signals.finished.connect(
            lambda d, fp, cb=on_loaded, sp=show_progress:
                self._on_load_finished(d, fp, cb, sp))
        worker.signals.error.connect(
            lambda msg, sp=show_progress, a=auto: self._on_load_error(msg, sp, a))
        worker.finished.connect(lambda w=worker: self._on_worker_done(w))
        worker.start()

    def _on_load_finished(self, data, file_path, on_loaded, show_progress):
        try:
            on_loaded(data, file_path)
        finally:
            if show_progress:
                self._loading_progress.hide()
                self._loading = False

    def _on_load_error(self, msg, show_progress, auto):
        if show_progress:
            self._loading_progress.hide()
            self._loading = False
        if auto:
            self._status_label.setText(f"自动刷新失败: {msg}")
        else:
            QMessageBox.critical(self, "加载失败", f"无法加载文件:\n{msg}")

    def _on_worker_done(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)

    def _save_file(self):
        """保存当前文件"""
        data = self._get_data()
        if not data:
            return
        if not data.file_path:
            self._save_file_as()
            return

        self._do_save(data.file_path)

    def _save_file_as(self):
        """另存为"""
        data = self._get_data()
        if not data:
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存 Lua 表文件", data.file_path or "untitled.lua",
            "Lua 文件 (*.lua);;所有文件 (*)"
        )
        if not file_path:
            return
        self._do_save(file_path)

    def _do_save(self, file_path: str):
        """执行保存"""
        data = self._get_data()
        if not data:
            return

        text = LuaSerializer.serialize(data, _update_raw=True)

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(text)
            # 保存成功后刷新原始文本并清零脏标记：
            # 否则再次保存（未继续修改）会命中 not modified 分支返回旧文本，造成静默回滚
            data._raw_text = text
            data.file_path = file_path
            data.modified = False
            data.file_mtime = os.path.getmtime(file_path)  # 更新 mtime，避免被自动刷新误判为变更
            td = self._get_current_tab_data()
            td['title'] = os.path.basename(file_path)
            td['file_path'] = file_path
            self._update_tab_title()
            self._add_recent_file(file_path)
            self._status_label.setText(f"已保存: {file_path}")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存失败:\n{e}")

    def _refresh_file(self):
        """刷新当前文件，从磁盘重新读取（后台线程）"""
        td = self._get_current_tab_data()
        if not td:
            return

        data = td['data']
        if not data.file_path:
            self._status_label.setText("无法刷新: 当前文件尚未保存到磁盘")
            return

        if not os.path.exists(data.file_path):
            QMessageBox.warning(self, "文件不存在",
                                f"文件已不存在:\n{data.file_path}")
            return

        # 防止重复加载
        if self._loading:
            self._status_label.setText("正在加载中，请稍候...")
            return

        # 如果有未保存修改，询问用户
        if data.modified:
            reply = QMessageBox.question(
                self, "确认刷新",
                f"当前文件有未保存的修改，刷新将丢失这些更改。\n确定要刷新吗？",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        self._reload_tab(td, show_status=True)

    def _reload_tab(self, td, show_status=False, auto=False):
        """后台重新解析某标签页对应的磁盘文件并更新模型。"""
        data = td['data']
        fp = data.file_path
        if not fp or not os.path.exists(fp):
            return

        def on_loaded(new_data, file_path):
            # 重新校验：若加载期间该表被改成已修改，放弃覆盖以免丢失编辑
            if data.modified:
                return
            model = td['model']
            new_data.modified = False
            model.set_data(new_data)

            # 更新存储的数据引用
            td['data'] = new_data
            td['title'] = os.path.basename(file_path)

            # 清除筛选
            proxy = td['proxy']
            proxy.clear_filter()
            self._filter_status.setText("")

            # 更新标签页标题
            self._update_tab_title()

            # 自动调整列宽
            QTimer.singleShot(50, lambda: self._auto_resize_columns(td['view']))

            self._update_statusbar()
            if show_status:
                self._status_label.setText(f"已刷新: {file_path}")

        self._start_load(fp, on_loaded, show_progress=not auto, auto=auto)

    def _auto_refresh_all(self):
        """定时自动刷新：仅刷新“未修改且磁盘文件已变更”的已打开标签页。"""
        if not self._auto_refresh_enabled:
            return
        # 用户主动加载/刷新进行中时，本 tick 跳过，避免互相干扰
        if self._loading:
            return
        for td in list(self._tabs_data.values()):
            data = td['data']
            fp = data.file_path
            if not fp or data.modified or not os.path.exists(fp):
                continue
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                continue
            # 文件比上次读取/保存更新 -> 重新加载（静默，不弹进度条）
            if mtime > data.file_mtime + 1e-6:
                self._reload_tab(td, auto=True)

    def _close_tab(self):
        """关闭当前标签页"""
        td = self._get_current_tab_data()
        if not td:
            return

        data = td['data']
        if data.modified:
            title = td['title'].lstrip('*')
            reply = QMessageBox.question(
                self, "未保存的更改",
                f"'{title}' 已被修改，是否保存？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if reply == QMessageBox.Save:
                self._save_file()
                if data.modified:  # 保存失败/取消
                    return
            elif reply == QMessageBox.Cancel:
                return

        idx = self._tab_widget.currentIndex()
        self._tab_widget.removeTab(idx)
        tab = td['tab']
        del self._tabs_data[id(tab)]

        if self._tab_widget.count() == 0:
            self._show_welcome()

    # ---- 行操作 ----
    def _insert_pos_after_view_row(self, view_row) -> int:
        """根据视图行号计算“其下一行”的插入位置（源模型逻辑行号）。
        view_row 为 None / -1 时返回 None，由调用方决定兜底策略。"""
        model = self._get_model()
        proxy = self._get_proxy()
        if view_row is None or view_row < 0:
            return None
        src_row = proxy.mapToSource(proxy.index(view_row, 0)).row() if proxy else view_row
        return src_row + 1

    def _add_row(self, context_row=None):
        """添加数据行。context_row 为右键所处视图行号（>=0），
        传入时插在该行下一行；否则按选中行，再否则追加到末尾。"""
        model = self._get_model()
        if not model:
            return
        view = self._get_view()
        proxy = self._get_proxy()
        pos = self._insert_pos_after_view_row(context_row)
        if pos is None:
            sel = view.selectionModel().selectedRows()
            if sel:
                vrow = sel[-1].row()
                # 将视图行号转换为源模型逻辑行号
                src_row = proxy.mapToSource(proxy.index(vrow, 0)).row() if proxy else vrow
                pos = src_row + 1
            else:
                # 无选中：追加到数据区末尾（return 行之前）
                pos = model.rowCount() - 1 if model._has_return else model.rowCount()
        model.add_data_row(pos)
        self._update_statusbar()

    def _add_comment_row(self, context_row=None):
        """添加注释行。插入位置规则同 _add_row。"""
        model = self._get_model()
        if not model:
            return
        view = self._get_view()
        proxy = self._get_proxy()
        pos = self._insert_pos_after_view_row(context_row)
        if pos is None:
            sel = view.selectionModel().selectedRows()
            if sel:
                vrow = sel[-1].row()
                src_row = proxy.mapToSource(proxy.index(vrow, 0)).row() if proxy else vrow
                pos = src_row + 1
            else:
                pos = model.rowCount() - 1 if model._has_return else model.rowCount()
        model.add_comment_row(pos)
        self._update_statusbar()

    # ---- 复制 / 粘贴 ---- 
    _CLIPBOARD_MIME = 'application/x-luatable-rows'

    def _copy_selection(self):
        """复制选中内容到剪贴板（Ctrl+C / 右键）"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not view or not model or not data:
            return

        sel = view.selectionModel()
        sel_indexes = sel.selectedIndexes()
        if not sel_indexes:
            return

        # 收集选中区域：{(src_row, src_col): ...}
        cells_by_row: dict = {}
        all_cols: set = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            r, c = src_idx.row(), src_idx.column()
            if c <= 0:
                continue  # 跳过行号列
            if r not in cells_by_row:
                cells_by_row[r] = {}
            cells_by_row[r][c] = True
            all_cols.add(c)

        if not cells_by_row:
            return

        fields = data.field_names
        sorted_rows = sorted(cells_by_row.keys())
        sorted_cols = sorted(all_cols)

        # 检测是否为整行复制（选中了整行或所有列）
        is_full_rows = all(
            len(cells_by_row[r]) == len(fields)
            for r in sorted_rows
        )

        # ---- 构建内部格式 JSON ----
        entries = []
        for r in sorted_rows:
            # r 是逻辑行（含头注释），转换到 data.rows 索引；跳过结构行
            row_type, data_idx = model._resolve_row(r)
            if data_idx is None or row_type not in (LuaTableModel.ROW_DATA, LuaTableModel.ROW_INLINE_COMMENT):
                continue
            re_entry = data.rows[data_idx]
            entry = {'__row_type': re_entry.row_type, '__row_key': re_entry.row_key}
            if re_entry.row_type == 'comment':
                entry['comment_text'] = re_entry.comment_text
            else:
                for c in sorted_cols:
                    fn = fields[c - 1]
                    val = re_entry.data.get(fn, '')
                    entry[fn] = LuaSerializer._value_to_lua(val) if val != '' else None
            entries.append(entry)

        clip_data = {
            'source': 'lua_table_editor',
            'version': 1,
            'fields': fields,
            'is_full_rows': is_full_rows,
            'entries': entries,
        }

        mime_data = QMimeData()
        mime_data.setData(self._CLIPBOARD_MIME, json.dumps(clip_data, ensure_ascii=False).encode('utf-8'))

        # ---- 构建文本格式（Tab 分隔，兼容外部粘贴）----
        if is_full_rows:
            # 整行格式：每行一个 Lua 表项（复用已正确构建的 entries，避免再拼 data.rows）
            text_lines = []
            for e in entries:
                if e['__row_type'] == 'comment':
                    text_lines.append(e.get('comment_text', '-- '))
                else:
                    parts = []
                    for fn in fields:
                        val = e.get(fn)
                        if val is not None:
                            parts.append(f'{fn}={val}')
                    row_body = '{' + ', '.join(parts) + '}'
                    rk = e.get('__row_key', '')
                    if rk:
                        row_body = f'[{rk}] = {row_body}'
                    text_lines.append(row_body)
            mime_data.setText('\n'.join(text_lines))
        else:
            # 部分单元格：Tab 分隔（复用 entries）
            text_lines = []
            header_parts = [fields[c - 1] for c in sorted_cols]
            text_lines.append('\t'.join(header_parts))
            for e in entries:
                if e['__row_type'] == 'comment':
                    text_lines.append(e.get('comment_text', '-- '))
                else:
                    cell_texts = []
                    for c in sorted_cols:
                        fn = fields[c - 1]
                        val = e.get(fn)
                        cell_texts.append(str(val) if val is not None else '')
                    text_lines.append('\t'.join(cell_texts))
            mime_data.setText('\n'.join(text_lines))

        QApplication.clipboard().setMimeData(mime_data)

    def _paste_at_selection(self):
        """粘贴剪贴板内容到当前选中位置（Ctrl+V / 右键）"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not view or not model or not data:
            return

        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()

        # ---- 优先解析内部格式 ----
        if mime_data.hasFormat(self._CLIPBOARD_MIME):
            try:
                raw = bytes(mime_data.data(self._CLIPBOARD_MIME)).decode('utf-8')
                clip = json.loads(raw)
            except Exception:
                return
            src_fields = clip.get('fields', [])
            entries = clip.get('entries', [])
            self._paste_internal(view, model, data, proxy, src_fields, entries)
            return

        # ---- 降级：解析纯文本（Tab 分隔）----
        if mime_data.hasText():
            text = mime_data.text().strip()
            if not text:
                return
            lines = text.split('\n')
            if len(lines) < 2:
                # 单行文本，可能是复制自 Excel 的单行
                pass
            self._paste_text(view, model, data, proxy, lines)

    def _paste_internal(self, view, model, data, proxy, src_fields, entries):
        """粘贴内部格式的行数据"""
        dst_fields = data.field_names
        sel = view.selectionModel()
        sel_indexes = sel.selectedIndexes()

        if not sel_indexes:
            # 无选中：追加到数据区末尾（return 行之前）
            for entry in entries:
                if model._has_return:
                    pos = model.rowCount() - 1  # return 行的逻辑位置，在其之前插入
                else:
                    pos = model.rowCount()      # 末尾
                model.add_data_row(pos)
                rt = entry.get('__row_type', 'data')
                new_data_idx = model._logical_to_data_row(pos)
                data.rows[new_data_idx].row_key = entry.get('__row_key', '')
                if rt == 'comment':
                    data.rows[new_data_idx].row_type = 'comment'
                    data.rows[new_data_idx].comment_text = entry.get('comment_text', '-- ')
                else:
                    for fn, val_str in entry.items():
                        if fn.startswith('__') or val_str is None or fn not in dst_fields:
                            continue
                        data.rows[new_data_idx].data[fn] = LuaParser._parse_value(val_str)
            model.safe_notify_reset()
            self._update_statusbar()
            return

        # 有选中：确定目标行
        target_rows_set = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            target_rows_set.add(src_idx.row())

        # 判断是否整行粘贴（选中的行都是完整行选中）
        sel_rows = sel.selectedRows()
        is_full_row_paste = len(sel_rows) > 0

        if is_full_row_paste:
            # 整行粘贴：替换选中的行
            proxy_rows = sorted(set(idx.row() for idx in sel_rows))
            src_rows = sorted(set(
                proxy.mapToSource(proxy.index(r, 0)).row() if proxy else r
                for r in proxy_rows
            ))
            # 先删除选中行，再插入
            for r in reversed(src_rows):
                model.remove_row(r)
            insert_at = min(src_rows)  # 逻辑行
            for i, entry in enumerate(entries):
                pos = insert_at + i   # 逻辑行
                model.add_data_row(pos)
                rt = entry.get('__row_type', 'data')
                data_idx = model._logical_to_data_row(pos)
                data.rows[data_idx].row_key = entry.get('__row_key', '')
                if rt == 'comment':
                    data.rows[data_idx].row_type = 'comment'
                    data.rows[data_idx].comment_text = entry.get('comment_text', '-- ')
                else:
                    for fn, val_str in entry.items():
                        if fn.startswith('__') or val_str is None or fn not in dst_fields:
                            continue
                        data.rows[data_idx].data[fn] = LuaParser._parse_value(val_str)
            model.safe_notify_reset()
        else:
            # 单元格粘贴：填入选区（行从选区顶部开始堆叠，字段按选区起始列对齐）
            sel_cols = [idx.column() for idx in sel_indexes if idx.column() > 0]
            target_min_row = min(target_rows_set) if target_rows_set else \
                (model.rowCount() - 1 if model._has_return else model.rowCount())
            target_min_col = min(sel_cols) if sel_cols else 1

            for i, entry in enumerate(entries):
                tr_logical = target_min_row + i  # 逻辑行
                t_row_type, t_data_idx = model._resolve_row(tr_logical)
                if t_data_idx is None or t_row_type not in (LuaTableModel.ROW_DATA, LuaTableModel.ROW_INLINE_COMMENT):
                    # 超出数据行范围，追加新行
                    apos = model.rowCount() - 1 if model._has_return else model.rowCount()
                    model.add_data_row(apos)
                    t_data_idx = model._logical_to_data_row(apos)
                rt = entry.get('__row_type', 'data')
                if rt == 'comment':
                    data.rows[t_data_idx].row_type = 'comment'
                    data.rows[t_data_idx].comment_text = entry.get('comment_text', '-- ')
                    data.rows[t_data_idx]._dirty = True
                else:
                    # 按字段顺序映射到选区列，从 target_min_col 开始逐列填充
                    field_items = [(fn, val_str) for fn, val_str in entry.items()
                                   if not fn.startswith('__') and val_str is not None and fn in dst_fields]
                    for j, (fn, val_str) in enumerate(field_items):
                        tc = target_min_col + j
                        if tc - 1 < len(dst_fields):
                            data.rows[t_data_idx].data[fn] = LuaParser._parse_value(val_str)
                    data.rows[t_data_idx]._dirty = True
            model.safe_notify_reset()

        data.modified = True
        model._rebuild_duplicates()
        self._update_statusbar()
        self._update_tab_title()

    def _paste_text(self, view, model, data, proxy, lines):
        """粘贴纯文本（Tab 分隔或单值格式）"""
        dst_fields = data.field_names
        sel = view.selectionModel()
        sel_indexes = sel.selectedIndexes()

        # 解析文本行
        rows_data = []
        has_header = False

        # 判断第一行是否为标题（包含 = 或与字段名匹配）
        if lines:
            first_line = lines[0]
            if '\t' in first_line:
                tabs = first_line.split('\t')
                match_count = sum(1 for t in tabs if t.strip() in dst_fields)
                if match_count >= len(tabs) * 0.5:
                    has_header = True

        start = 1 if has_header else 0
        for line in lines[start:]:
            line = line.strip()
            if not line:
                continue
            if '\t' in line:
                cells = line.split('\t')
                rows_data.append(cells)
            else:
                rows_data.append([line])

        if not rows_data:
            return

        if not sel_indexes:
            # 无选中：追加到数据区末尾（return 行之前）
            for cells in rows_data:
                if model._has_return:
                    pos = model.rowCount() - 1
                else:
                    pos = model.rowCount()
                model.add_data_row(pos)
                data_idx = model._logical_to_data_row(pos)
                for ci, cell_val in enumerate(cells):
                    if ci < len(dst_fields):
                        fn = dst_fields[ci]
                        val = LuaParser._parse_value(cell_val.strip()) if cell_val.strip() else ''
                        data.rows[data_idx].data[fn] = val
            model.safe_notify_reset()
            data.modified = True
            model._rebuild_duplicates()
            self._update_statusbar()
            return

        # 有选中：确定从哪行哪列开始
        target_rows = set()
        target_cols = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            target_rows.add(src_idx.row())
            if src_idx.column() > 0:
                target_cols.add(src_idx.column())

        target_min_row = min(target_rows)
        target_min_col = min(target_cols) if target_cols else 1

        sel_rows = sel.selectedRows()
        is_full_rows = len(sel_rows) > 0

        if is_full_rows:
            proxy_rows = sorted(set(idx.row() for idx in sel_rows))
            src_rows = sorted(set(
                proxy.mapToSource(proxy.index(r, 0)).row() if proxy else r
                for r in proxy_rows
            ))
            for r in reversed(src_rows):
                model.remove_row(r)
            insert_at = min(src_rows)  # 逻辑行
            for i, cells in enumerate(rows_data):
                pos = insert_at + i  # 逻辑行
                model.add_data_row(pos)
                data_idx = model._logical_to_data_row(pos)
                for ci, cell_val in enumerate(cells):
                    if ci < len(dst_fields):
                        fn = dst_fields[ci]
                        val = LuaParser._parse_value(cell_val.strip()) if cell_val.strip() else ''
                        data.rows[data_idx].data[fn] = val
            model.safe_notify_reset()
        else:
            # 从目标格开始填充
            for ri, cells in enumerate(rows_data):
                tr_logical = target_min_row + ri  # 逻辑行
                t_row_type, t_data_idx = model._resolve_row(tr_logical)
                if t_data_idx is None or t_row_type not in (LuaTableModel.ROW_DATA, LuaTableModel.ROW_INLINE_COMMENT):
                    # 超出数据行范围，追加新行
                    if model._has_return:
                        apos = model.rowCount() - 1
                    else:
                        apos = model.rowCount()
                    model.add_data_row(apos)
                    t_data_idx = model._logical_to_data_row(apos)
                for ci, cell_val in enumerate(cells):
                    tc = target_min_col + ci
                    if tc - 1 < len(dst_fields):
                        fn = dst_fields[tc - 1]
                        val = LuaParser._parse_value(cell_val.strip()) if cell_val.strip() else ''
                        data.rows[t_data_idx].data[fn] = val
                        # 覆盖已有单元格时必须标脏，否则保存时该行仍输出旧原始文本导致静默丢失
                        data.rows[t_data_idx]._dirty = True
            model.safe_notify_reset()

        data.modified = True
        model._rebuild_duplicates()
        self._update_statusbar()
        self._update_tab_title()

    # ---- 整行复制 / 粘贴（右键菜单）----
    def _copy_current_row(self):
        """复制右键所在行（整行）"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not view or not model or not data:
            return

        # 使用右键点击位置获取所在行
        cursor_row = self._context_row
        src_row = proxy.mapToSource(proxy.index(cursor_row, 0)).row() if proxy else cursor_row

        # src_row 是逻辑行（含头注释），转换到 data.rows 索引
        row_type, data_idx = model._resolve_row(src_row)
        if data_idx is None or row_type not in (LuaTableModel.ROW_DATA, LuaTableModel.ROW_INLINE_COMMENT):
            return

        re_entry = data.rows[data_idx]
        fields = data.field_names

        entry = {'__row_type': re_entry.row_type, '__row_key': re_entry.row_key}
        if re_entry.row_type == 'comment':
            entry['comment_text'] = re_entry.comment_text
        else:
            for fn in fields:
                val = re_entry.data.get(fn, '')
                entry[fn] = LuaSerializer._value_to_lua(val) if val != '' else None

        clip_data = {
            'source': 'lua_table_editor',
            'version': 1,
            'fields': fields,
            'is_full_rows': True,
            'entries': [entry],
        }

        mime_data = QMimeData()
        mime_data.setData(self._CLIPBOARD_MIME, json.dumps(clip_data, ensure_ascii=False).encode('utf-8'))

        # 文本格式
        if re_entry.row_type == 'comment':
            mime_data.setText(re_entry.comment_text)
        else:
            parts = []
            for fn in fields:
                val = re_entry.data.get(fn, '')
                if val is not None and val != '':
                    parts.append(f'{fn}={LuaSerializer._value_to_lua(val)}')
            row_body = '{' + ', '.join(parts) + '}'
            rk = re_entry.row_key
            if rk:
                row_body = f'[{rk}] = {row_body}'
            mime_data.setText(row_body)

        QApplication.clipboard().setMimeData(mime_data)

    def _paste_row_after_cursor(self):
        """在右键所在行之后粘贴剪贴板中的行内容"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not view or not model or not data:
            return

        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()

        cursor_row = self._context_row
        target_row = proxy.mapToSource(proxy.index(cursor_row, 0)).row() if proxy else cursor_row

        if mime_data.hasFormat(self._CLIPBOARD_MIME):
            try:
                raw = bytes(mime_data.data(self._CLIPBOARD_MIME)).decode('utf-8')
                clip = json.loads(raw)
            except Exception:
                return
            entries = clip.get('entries', [])
            dst_fields = data.field_names

            # target_row 是逻辑行，在其后插入
            insert_at = target_row + 1 if target_row >= 0 else (model.rowCount() - 1 if model._has_return else model.rowCount())
            for entry in entries:
                model.add_data_row(insert_at)
                rt = entry.get('__row_type', 'data')
                data_idx = model._logical_to_data_row(insert_at)
                data.rows[data_idx].row_key = entry.get('__row_key', '')
                if rt == 'comment':
                    data.rows[data_idx].row_type = 'comment'
                    data.rows[data_idx].comment_text = entry.get('comment_text', '-- ')
                else:
                    for fn, val_str in entry.items():
                        if fn.startswith('__') or val_str is None or fn not in dst_fields:
                            continue
                        data.rows[data_idx].data[fn] = LuaParser._parse_value(val_str)
                insert_at += 1

            model.safe_notify_reset()
            data.modified = True
            model._rebuild_duplicates()
            self._update_statusbar()
            self._update_tab_title()
            return

        # 降级：粘贴纯文本为单行
        if mime_data.hasText():
            text = mime_data.text().strip()
            if text:
                insert_at = target_row + 1 if target_row >= 0 else (model.rowCount() - 1 if model._has_return else model.rowCount())
                model.add_data_row(insert_at)
                data_idx = model._logical_to_data_row(insert_at)
                data.rows[data_idx].data[data.field_names[0]] = LuaParser._parse_value(text) if data.field_names else text
                model.safe_notify_reset()
                data.modified = True
                model._rebuild_duplicates()
                self._update_statusbar()
                self._update_tab_title()

    def _delete_row(self):
        """删除选中行（无需选中整行，选中任意单元格即可删除所在行）"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not view or not model:
            return

        # 使用 selectedIndexes() 获取所有选中行，不要求整行选中
        sel_indexes = view.selectionModel().selectedIndexes()
        if not sel_indexes:
            return

        # 从所有选中索引中收集唯一的行号
        rows_set = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            rows_set.add(src_idx.row())

        rows = sorted(rows_set, reverse=True)

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除选中的 {len(rows)} 行吗？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        for row in rows:
            model.remove_row(row)
        self._update_statusbar()

    # ---- 修复 row_key（从 id 字段恢复 [key] = {} 格式）----
    def _repair_row_keys(self):
        """为所有没有 row_key 的数据行，尝试从 id 字段恢复 row_key"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        if not model or not data:
            return

        if 'id' not in data.field_names:
            QMessageBox.information(self, "无法修复", "当前表没有 'id' 字段，无法自动恢复行键。")
            return

        restored_count = 0
        for row_entry in data.rows:
            if row_entry.row_type == 'data' and not row_entry.row_key:
                row_id = row_entry.data.get('id')
                if row_id is not None and row_id != '':
                    row_entry.row_key = str(row_id)
                    row_entry._dirty = True
                    restored_count += 1

        if restored_count > 0:
            data.modified = True
            model.safe_notify_reset()
            self._update_statusbar()
            self._update_tab_title()
            QMessageBox.information(self, "修复完成",
                                    f"已从 'id' 字段恢复了 {restored_count} 行的 [key] = 格式。\n"
                                    "请保存文件以保留更改。")
        else:
            QMessageBox.information(self, "无需修复", "所有数据行都已有行键，无需修复。")

    # ---- 批量同步选中行的行键（与 id 一致）----
    def _batch_sync_row_keys(self):
        """将选中的多行数据行的行键同步成各自的 id 值（覆盖式同步）"""
        view = self._get_view()
        model = self._get_model()
        data = self._get_data()
        proxy = self._get_proxy()
        if not (view and model and data):
            return

        if 'id' not in data.field_names:
            QMessageBox.information(self, "无法同步", "当前表没有 'id' 字段，无法同步行键。")
            return

        sel_indexes = view.selectionModel().selectedIndexes()
        if not sel_indexes:
            QMessageBox.information(self, "提示",
                                    "请先选中要同步的行（按住 Ctrl/Shift 可多选）。")
            return

        # 通过 model._resolve_row 映射到 data.rows 的真实下标，并去重
        target_idx = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            rt, real_idx = model._resolve_row(src_idx.row())
            if rt == model.ROW_DATA and 0 <= real_idx < len(data.rows):
                target_idx.add(real_idx)

        if not target_idx:
            QMessageBox.information(self, "提示", "选中的行中没有数据行。")
            return

        synced = 0
        skipped = 0
        for i in target_idx:
            row_entry = data.rows[i]
            rid = row_entry.data.get('id')
            if rid is None or rid == '':
                skipped += 1
                continue
            row_entry.row_key = str(rid)
            row_entry._dirty = True
            synced += 1

        if synced == 0:
            QMessageBox.information(self, "提示",
                                    "没有可同步的行（所选数据行均缺少 id 字段）。")
            return

        data.modified = True
        model._rebuild_duplicates()
        model.safe_notify_reset()
        self._update_tab_title()
        self._update_statusbar()
        msg = f"已同步 {synced} 行的行键与 id"
        if skipped:
            msg += f"（{skipped} 行缺少 id 已跳过）"
        self._status_label.setText(msg)

    # ---- 修改 / 设置行键（右键菜单）----
    def _resolve_context_data(self):
        """安全解析右键所在行，返回 (model, data, data_idx, row_entry) 或 None"""
        try:
            view = self._get_view()
            model = self._get_model()
            data = self._get_data()
            proxy = self._get_proxy()
            if not view or not model or not data:
                return None

            cursor_row = self._context_row
            if cursor_row < 0:
                return None

            if proxy:
                proxy_idx = proxy.index(cursor_row, 0)
                if not proxy_idx.isValid():
                    return None
                src_idx = proxy.mapToSource(proxy_idx)
                if not src_idx.isValid():
                    return None
                src_row = src_idx.row()
            else:
                src_row = cursor_row

            row_type, data_idx = model._resolve_row(src_row)
            if data_idx is None or data_idx >= len(data.rows):
                return None
            if row_type not in (LuaTableModel.ROW_DATA, LuaTableModel.ROW_INLINE_COMMENT):
                return None

            row_entry = data.rows[data_idx]
            return (model, data, data_idx, row_entry)
        except Exception:
            return None

    def _edit_row_key(self):
        """手动修改右键所在行的行键"""
        try:
            res = self._resolve_context_data()
            if res is None:
                return
            model, data, data_idx, row_entry = res
        except Exception:
            return

        if row_entry.row_type == 'comment':
            QMessageBox.information(self, "不支持", "注释行没有行键。")
            return

        current_key = row_entry.row_key
        new_key, ok = QInputDialog.getText(
            self, "修改行键",
            f"请输入新的行键（留空则清除行键）:\n"
            f"例如: 1 或 101 或 'my_key'",
            text=current_key
        )
        if not ok:
            return

        new_key = new_key.strip()
        if new_key == current_key:
            return

        try:
            # 保存旧值用于撤销（深拷贝整行状态）
            import copy
            model._undo_stack.append((data_idx, -1, copy.deepcopy(row_entry)))
            model._redo_stack.clear()

            row_entry.row_key = new_key
            row_entry._dirty = True
            data.modified = True
            model.safe_notify_reset()
            model._rebuild_duplicates()
            self._update_statusbar()
            self._update_tab_title()
        except Exception:
            pass

    def _set_row_key_from_id(self):
        """从右键所在行的 id 字段值设置行键"""
        try:
            res = self._resolve_context_data()
            if res is None:
                return
            model, data, data_idx, row_entry = res
        except Exception:
            return

        if row_entry.row_type == 'comment':
            QMessageBox.information(self, "不支持", "注释行没有行键。")
            return

        if 'id' not in data.field_names:
            QMessageBox.information(self, "无法设置", "当前表没有 'id' 字段。")
            return

        row_id = row_entry.data.get('id')
        if row_id is None or row_id == '':
            QMessageBox.information(self, "无法设置", "该行的 id 字段为空，无法设置行键。")
            return

        new_key = str(row_id)
        if new_key == row_entry.row_key:
            QMessageBox.information(self, "无需修改", f"行键已经是 '{new_key}'。")
            return

        try:
            # 保存旧值用于撤销（深拷贝整行状态）
            import copy
            model._undo_stack.append((data_idx, -1, copy.deepcopy(row_entry)))
            model._redo_stack.clear()

            row_entry.row_key = new_key
            row_entry._dirty = True
            data.modified = True
            model.safe_notify_reset()
            model._rebuild_duplicates()
            self._update_statusbar()
            self._update_tab_title()
        except Exception:
            pass

    # ---- 注释 / 取消注释 ----
    @staticmethod
    def _row_data_to_comment_text(data: dict, field_names: list) -> str:
        """将一行数据序列化为 Lua 注释文本（-- {key=val, ...}）"""
        parts = []
        for fn in field_names:
            val = data.get(fn)
            if val is None or val == '':
                continue
            val_str = LuaSerializer._value_to_lua(val)
            parts.append(f'{fn}={val_str}')
        return '-- {' + ', '.join(parts) + '}'

    @staticmethod
    def _comment_text_to_row_data(comment_text: str) -> dict:
        """尝试将注释文本解析为数据行字典"""
        # 去掉 Lua 注释前缀 --
        if comment_text.startswith('-- '):
            content = comment_text[3:].strip()
        elif comment_text.startswith('--'):
            content = comment_text[2:].strip()
        else:
            content = comment_text.strip()

        if not content:
            return {}

        # 如果以 { 开头（之前由 comment 功能生成的格式），尝试解析
        if content.startswith('{'):
            try:
                return LuaParser._parse_kv_pairs(content)
            except Exception:
                pass

        # 否则当作纯文本内容返回（标记为不可解析）
        return {'_comment_text': content}

    def _comment_selected_rows(self):
        """将选中的数据行转换为注释行"""
        model = self._get_model()
        view = self._get_view()
        data = self._get_data()
        if not model or not view or not data:
            return

        proxy = self._get_proxy()
        # 使用 selectedIndexes() 而非 selectedRows()，以兼容 SelectItems 模式
        sel_indexes = view.selectionModel().selectedIndexes()

        # 收集需要注释的所有数据行索引（去重，映射到源模型）
        rows_to_comment = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            row = src_idx.row()
            if row < 0 or row >= len(data.rows):
                continue
            entry = data.rows[row]
            if entry.row_type == 'data':
                rows_to_comment.add(row)

        if not rows_to_comment:
            QMessageBox.information(self, "提示", "选中的行中没有数据行可注释。")
            return

        # 从后往前处理，避免索引变化；同时记录撤销信息
        for row in sorted(rows_to_comment, reverse=True):
            entry = data.rows[row]
            # 记录撤销：保存注释前的原始数据行
            model.push_row_undo(row, entry)
            comment_text = self._row_data_to_comment_text(entry.data, data.field_names)
            data.rows[row] = RowEntry(row_type='comment', comment_text=comment_text, row_key=entry.row_key)

        # 通知视图刷新受影响的区域
        model._rebuild_duplicates()
        min_row = min(rows_to_comment)
        max_row = max(rows_to_comment)
        top_left = model.index(min_row, 0)
        bottom_right = model.index(max_row, model.columnCount() - 1)
        model.dataChanged.emit(top_left, bottom_right)

        data.modified = True
        self._update_tab_title()
        self._update_statusbar()
        self._status_label.setText(
            f"已注释 {len(rows_to_comment)} 行数据（Lua 语法: --）"
        )

    def _uncomment_selected_rows(self):
        """将选中的注释行恢复为数据行"""
        model = self._get_model()
        view = self._get_view()
        data = self._get_data()
        if not model or not view or not data:
            return

        proxy = self._get_proxy()
        # 使用 selectedIndexes() 而非 selectedRows()，以兼容 SelectItems 模式
        sel_indexes = view.selectionModel().selectedIndexes()

        # 收集需要取消注释的行索引（去重）
        rows_to_uncomment = set()
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            row = src_idx.row()
            if row < 0 or row >= len(data.rows):
                continue
            entry = data.rows[row]
            if entry.row_type == 'comment':
                rows_to_uncomment.add(row)

        if not rows_to_uncomment:
            QMessageBox.information(self, "提示", "选中的行中没有注释行可取消注释。")
            return

        restored_count = 0
        # 从后往前处理
        for row in sorted(rows_to_uncomment, reverse=True):
            entry = data.rows[row]
            parsed = self._comment_text_to_row_data(entry.comment_text)

            # 如果解析出 _comment_text（不可解析的注释），忽略
            if '_comment_text' in parsed:
                # 仅当纯文本注释也只含 _comment_text 键时跳过
                if len(parsed) == 1:
                    continue

            # 记录撤销：保存取消注释前的注释行状态
            model.push_row_undo(row, entry)
            data.rows[row] = RowEntry(row_type='data', data=parsed, row_key=entry.row_key)
            restored_count += 1

        if restored_count == 0:
            QMessageBox.information(self, "提示",
                                    "选中的注释行无法解析为数据行。\n"
                                    "请确保注释格式为 -- {key=val, ...}")
            return

        # 通知视图刷新
        model._rebuild_duplicates()
        min_row = min(rows_to_uncomment)
        max_row = max(rows_to_uncomment)
        top_left = model.index(min_row, 0)
        bottom_right = model.index(max_row, model.columnCount() - 1)
        model.dataChanged.emit(top_left, bottom_right)

        data.modified = True
        self._update_tab_title()
        self._update_statusbar()
        self._status_label.setText(
            f"已取消注释 {restored_count} 行数据"
        )

    # ---- 批量添加行 ----
    def _add_rows_batch(self):
        """添加复数行数据"""
        model = self._get_model()
        if not model:
            return

        menu = QMenu(self)
        menu.setStyleSheet("QMenu { font-size: 13px; }")

        for label, n in [("添加 3 行", 3), ("添加 5 行", 5), ("添加 20 行", 20)]:
            act = menu.addAction(label)
            act.triggered.connect(lambda checked, cnt=n: self._do_add_rows(cnt))

        menu.addSeparator()
        custom_act = menu.addAction("自定义数量...")
        custom_act.triggered.connect(self._add_rows_custom)

        # 在工具栏按钮下方弹出
        btn = self.sender()
        if isinstance(btn, QPushButton):
            menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))
        else:
            menu.exec(QCursor.pos())

    def _do_add_rows(self, count: int, view_row=None):
        """执行添加指定数量行。view_row 为右键所处视图行号（>=0）时，
        插在该行下一行；否则按选中区域，再否则追加到末尾。"""
        model = self._get_model()
        if not model:
            return
        proxy = self._get_proxy()
        view = self._get_view()
        pos = None
        if view_row is not None and view_row >= 0:
            src_row = proxy.mapToSource(proxy.index(view_row, 0)).row() if proxy else view_row
            pos = src_row + 1
        if pos is None:
            sel = view.selectionModel().selectedIndexes()
            if sel:
                # 找到选中区域最后一行，插在其后
                pos = max(idx.row() for idx in sel) + 1
            else:
                pos = len(model.table_data.rows)

        for i in range(count):
            model.add_data_row(pos + i)
        self._update_statusbar()
        self._status_label.setText(f"已添加 {count} 行")

    def _add_rows_custom(self, view_row=None):
        """自定义数量添加行"""
        text, ok = QInputDialog.getText(
            self, "自定义添加行数",
            "请输入要添加的行数:"
        )
        if ok and text.strip().isdigit():
            n = int(text.strip())
            if n > 0 and n <= 1000:
                self._do_add_rows(n, view_row)
            else:
                QMessageBox.warning(self, "无效输入", "请输入 1-1000 之间的数字。")

    # ---- 添加列 ----
    def _add_column(self):
        """添加新列（字段）"""
        model = self._get_model()
        if not model or not model.table_data:
            return

        text, ok = QInputDialog.getText(
            self, "添加新列",
            "请输入新字段名 (英文/数字):"
        )
        if not ok or not text.strip():
            return

        field_name = text.strip()
        if field_name in model.table_data.field_names:
            QMessageBox.warning(self, "字段已存在", f"字段 '{field_name}' 已存在。")
            return

        data = model.table_data
        data.field_names.append(field_name)

        # 为所有数据行的新字段设空值
        for row in data.rows:
            if row.row_type == 'data':
                row.data[field_name] = ''
                row._dirty = True

        data.modified = True
        model.set_data(data)  # 刷新模型以更新列头
        self._update_tab_title()
        self._update_statusbar()
        self._status_label.setText(f"已添加字段: {field_name}")

    # ---- 填充完成回调 ----
    def _on_fill_completed(self, tab):
        """填充柄操作完成后的回调"""
        self._update_tab_title()
        self._update_statusbar()
        self._status_label.setText("已自动填充数据")

    # ---- 工作区面板 ----
    def _toggle_workspace(self):
        """展开/收起工作区面板"""
        visible = self._workspace_panel.isVisible()
        self._workspace_panel.setVisible(not visible)
        if visible:
            self._status_label.setText("工作区已收起")
        else:
            self._status_label.setText("工作区已展开")

    # ---- 详情面板 ----
    def _toggle_detail(self):
        """展开/收起详情面板"""
        if self._detail_panel.isVisible():
            self._detail_panel.hide_detail()
            self._status_label.setText("详情栏已收起")
        else:
            self._status_label.setText("详情栏已展开 - 双击单元格查看详情")
            # 如果有当前选中，重新显示
            view = self._get_view()
            if view:
                self._on_selection_changed(view)

    def _on_selection_changed(self, view: QTableView):
        """选中变化时更新详情面板"""
        if not self._detail_panel.isVisible():
            return

        sel = view.selectionModel().selectedIndexes()
        if not sel:
            return

        # 取第一个选中单元格
        idx = sel[0]
        proxy = view.model()
        if isinstance(proxy, QSortFilterProxyModel):
            model = proxy.sourceModel()
            src_idx = proxy.mapToSource(idx)
        else:
            model = proxy
            src_idx = idx

        if not isinstance(model, LuaTableModel):
            return

        field = model.headerData(idx.column(), Qt.Horizontal, Qt.DisplayRole) or '-'
        row_num = src_idx.row() + 1
        content = model.get_full_value(src_idx.row(), idx.column())

        self._detail_panel.show_detail(str(field), str(row_num), content)

    # ---- 查找功能 ----
    def _show_find_dialog(self):
        """显示查找对话框"""
        if self._find_dialog is None:
            self._find_dialog = FindDialog(self)
        self._find_dialog.reset()
        self._find_dialog.show()
        self._find_dialog.raise_()
        self._find_dialog.activateWindow()

    def perform_search(self, text: str):
        """执行查找"""
        proxy = self._get_proxy()
        if not proxy:
            return
        proxy.set_search(text)

    def search_next(self):
        """查找下一个"""
        proxy = self._get_proxy()
        view = self._get_view()
        if not proxy or not view:
            return

        src_row = proxy.next_search_result()
        if src_row < 0:
            return

        # 映射到代理模型中的行
        for proxy_row in range(proxy.rowCount()):
            if proxy.mapToSource(proxy.index(proxy_row, 0)).row() == src_row:
                view.selectRow(proxy_row)
                view.scrollTo(proxy.index(proxy_row, 0), QAbstractItemView.PositionAtCenter)
                break

        self._status_label.setText(
            f"查找: 第 {proxy.current_search_pos}/{proxy.search_count} 个结果"
        )

    def search_prev(self):
        """查找上一个"""
        proxy = self._get_proxy()
        view = self._get_view()
        if not proxy or not view:
            return

        src_row = proxy.prev_search_result()
        if src_row < 0:
            return

        for proxy_row in range(proxy.rowCount()):
            if proxy.mapToSource(proxy.index(proxy_row, 0)).row() == src_row:
                view.selectRow(proxy_row)
                view.scrollTo(proxy.index(proxy_row, 0), QAbstractItemView.PositionAtCenter)
                break

        self._status_label.setText(
            f"查找: 第 {proxy.current_search_pos}/{proxy.search_count} 个结果"
        )

    # ---- 筛选功能 ----
    def _on_header_clicked(self, section: int):
        """点击列头"""
        pass  # 保留以备扩展

    def _on_header_context_menu(self, header, pos: QPoint):
        """表头右键菜单 - 列固定 + 列筛选"""
        section = header.logicalIndexAt(pos)

        # 行号列不固定/不筛选
        if section <= 0:
            return

        view = self._get_view()
        proxy = self._get_proxy()
        if not view or not proxy:
            return

        menu = QMenu(self)
        menu.setStyleSheet("QMenu { font-size: 13px; }")

        # ---- 整列删除 ----
        del_act = menu.addAction("🗑 删除此列")
        del_act.triggered.connect(lambda: self._delete_column(section))
        menu.addSeparator()

        # ---- 列固定（冻结）选项 ----
        if view.is_column_frozen(section):
            act_u = menu.addAction("❌ 取消固定此列")
            act_u.triggered.connect(lambda: view.unfreeze_column(section))
        else:
            act_l = menu.addAction("📌 固定此列到左侧")
            act_l.triggered.connect(lambda: view.freeze_column(section, 'left'))
            act_r = menu.addAction("📌 固定此列到右侧")
            act_r.triggered.connect(lambda: view.freeze_column(section, 'right'))
        if view.has_frozen_columns():
            menu.addSeparator()
            act_all = menu.addAction("❌ 取消所有固定列")
            act_all.triggered.connect(lambda: view.unfreeze_all())

        menu.addSeparator()

        # 清除筛选
        clear_act = menu.addAction("清除筛选")
        clear_act.triggered.connect(lambda: self._clear_filter(section))

        menu.addSeparator()

        # 筛选选项
        filter_actions = [
            ("等于 (=)", "equals"),
            ("不等于 (≠)", "not_equals"),
            ("包含 (contains)", "contains"),
            ("不包含 (excludes)", "not_contains"),
        ]

        for label, mode in filter_actions:
            act = menu.addAction(label)
            act.triggered.connect(lambda checked, m=mode: self._show_filter_input(section, m))

        menu.exec(header.mapToGlobal(pos))

    # ---- 整列删除 ----
    def _delete_column(self, proxy_col: int):
        """删除整列（字段）。proxy_col 为视图（proxy）列号，0 为行号列不可删。"""
        if proxy_col <= 0:
            return

        proxy = self._get_proxy()
        model = self._get_model()
        data = self._get_data()
        view = self._get_view()
        if not (proxy and model and data and view):
            return

        # proxy 列 -> 源列（FilterProxyModel 不重排列，用 mapToSource；
        # 空表无数据行时 index(0) 无效，退化为恒等映射）
        idx = proxy.index(0, proxy_col)
        if idx.isValid():
            src_col = proxy.mapToSource(idx).column()
        else:
            src_col = proxy_col
        field_idx = src_col - 1
        if not (0 <= field_idx < len(data.field_names)):
            return

        field_name = data.field_names[field_idx]
        reply = QMessageBox.question(
            self, "确认删除列",
            f"确定删除整列「{field_name}」吗？\n该列的所有数据将被移除，且不可撤销。",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # 1) 从字段名中移除
        data.field_names.pop(field_idx)

        # 2) 从每一行数据中移除该字段
        for row_entry in data.rows:
            if row_entry.row_type == 'data':
                row_entry.data.pop(field_name, None)
                row_entry._dirty = True

        # 3) 重新映射冻结列索引（删除列后，右侧列的 proxy 列号整体左移 1）
        new_frozen = []
        for e in view._frozen:
            if e['col'] == proxy_col:
                continue  # 被删列若是冻结列，直接移除
            if e['col'] > proxy_col:
                e = dict(e)
                e['col'] -= 1
            new_frozen.append(e)
        view._frozen = new_frozen

        # 4) 标记修改并重建模型
        data.modified = True
        model.set_data(data)          # 会重建列头、重复值集合，并重置视图
        view._rebuild_frozen()        # 用新的冻结索引重建浮层

        # 5) 刷新界面状态
        self._update_tab_title()
        self._update_statusbar()
        # 清除可能引用了被删列的筛选
        proxy.clear_filter()
        self._filter_status.setText("")
        self._status_label.setText(f"已删除列「{field_name}」")

    def _show_filter_input(self, column: int, mode: str):
        """弹出筛选输入对话框"""
        mode_labels = {
            'equals': '等于',
            'not_equals': '不等于',
            'contains': '包含',
            'not_contains': '不包含',
        }

        text, ok = QInputDialog.getText(
            self,
            f"筛选 - {mode_labels.get(mode, mode)}",
            f"请输入筛选条件 ({mode_labels.get(mode, mode)}):"
        )
        if ok:
            self._apply_filter(column, text, mode)

    def _apply_filter(self, column: int, text: str, mode: str):
        """应用筛选"""
        proxy = self._get_proxy()
        if not proxy:
            return
        proxy.set_filter(column, text, mode)
        self._filter_status.setText(
            f"筛选: 列 {column} {mode} \"{text}\""
        )
        self._status_label.setText(f"已应用筛选: {mode} \"{text}\"")

    def _clear_filter(self, column: int = -1):
        """清除筛选"""
        proxy = self._get_proxy()
        if not proxy:
            return
        proxy.clear_filter()
        self._filter_status.setText("")
        self._status_label.setText("筛选已清除")

    # ---- 右键菜单 ----
    def _on_context_menu(self, pos: QPoint):
        """表格右键菜单"""
        view = self.sender()
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { font-size: 13px; }")

        # 记录右键所在行（用于复制/粘贴整行）
        proxy = self._get_proxy()
        idx = view.indexAt(pos)
        self._context_row = idx.row() if idx.isValid() else -1

        # ---- 复制 / 粘贴整行（基于右键位置）----
        has_context_row = self._context_row >= 0
        copy_row_act = menu.addAction("📋 复制当前行")
        copy_row_act.setEnabled(has_context_row)
        paste_row_act = menu.addAction("📥 粘贴行")
        paste_row_act.setEnabled(has_context_row)
        menu.addSeparator()

        add_data = menu.addAction("➕ 插入 1 行")
        
        # 批量添加子菜单
        batch_menu = menu.addMenu("➕ 插入复数行")
        for label, n in [("插入 3 行", 3), ("插入 5 行", 5), ("插入 20 行", 20)]:
            act = batch_menu.addAction(label)
            act.triggered.connect(lambda checked, cnt=n, cr=self._context_row: self._do_add_rows(cnt, cr))
        custom_act = batch_menu.addAction("自定义数量...")
        custom_act.triggered.connect(lambda checked, cr=self._context_row: self._add_rows_custom(cr))

        add_col = menu.addAction("➕ 添加列...")
        menu.addSeparator()

        # ---- 注释 / 取消注释（根据选中行状态决定启用/禁用） ----
        has_data_rows, has_comment_rows = self._check_selected_row_types()

        comment_act = menu.addAction("🗨 注释选中行 (--)")
        comment_act.setEnabled(has_data_rows)
        if not has_data_rows:
            comment_act.setToolTip("请至少选中一行数据行")

        uncomment_act = menu.addAction("🔓 取消注释选中行")
        uncomment_act.setEnabled(has_comment_rows)
        if not has_comment_rows:
            uncomment_act.setToolTip("请至少选中一行注释行")

        menu.addSeparator()

        add_comment = menu.addAction("💬 添加注释行")
        menu.addSeparator()
        del_row = menu.addAction("➖ 删除当前行")
        menu.addSeparator()
        repair_keys = menu.addAction("🔧 修复行键 (从 id 恢复 [key]=)")
        clear_filter = menu.addAction("清除筛选")
        menu.addSeparator()

        # ---- 行键操作（基于右键所在行）----
        edit_key_act = menu.addAction("✏️ 修改行键")
        edit_key_act.setEnabled(has_context_row)
        set_key_from_id_act = menu.addAction("🆔 从 id 获得本行行键")
        set_key_from_id_act.setEnabled(has_context_row)
        sync_keys_act = menu.addAction("🆔 批量同步选中行键 (与 id 一致)")
        sync_keys_act.setEnabled(has_data_rows)

        action = menu.exec(view.viewport().mapToGlobal(pos))

        if action == copy_row_act:
            self._copy_current_row()
        elif action == paste_row_act:
            self._paste_row_after_cursor()
        elif action == add_data:
            self._add_row(self._context_row)
        elif action == add_col:
            self._add_column()
        elif action == comment_act:
            self._comment_selected_rows()
        elif action == uncomment_act:
            self._uncomment_selected_rows()
        elif action == add_comment:
            self._add_comment_row(self._context_row)
        elif action == del_row:
            self._delete_row()
        elif action == repair_keys:
            self._repair_row_keys()
        elif action == clear_filter:
            self._clear_filter()
        elif action == edit_key_act:
            self._edit_row_key()
        elif action == set_key_from_id_act:
            self._set_row_key_from_id()
        elif action == sync_keys_act:
            self._batch_sync_row_keys()

    def _check_selected_row_types(self) -> tuple:
        """检查当前选中行的类型，返回 (has_data_rows, has_comment_rows)"""
        model = self._get_model()
        view = self._get_view()
        data = self._get_data()
        if not model or not view or not data:
            return (False, False)

        proxy = self._get_proxy()
        # 使用 selectedIndexes() 以兼容 SelectItems 模式
        sel_indexes = view.selectionModel().selectedIndexes()
        has_data = False
        has_comment = False
        for idx in sel_indexes:
            src_idx = proxy.mapToSource(idx) if proxy else idx
            row = src_idx.row()
            if row < 0 or row >= len(data.rows):
                continue
            if data.rows[row].row_type == 'data':
                has_data = True
            else:
                has_comment = True
            if has_data and has_comment:
                break
        return (has_data, has_comment)

    # ---- 拖放支持 ----
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith('.lua'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            file_path = url.toLocalFile()
            if file_path.endswith('.lua'):
                self._open_file_path(file_path)
                return
        event.ignore()

    # ---- 关闭事件 ----
    def closeEvent(self, event):
        """关闭主窗口时检查所有未保存标签页"""
        # 检查所有标签页
        for tab_id, td in list(self._tabs_data.items()):
            if td['data'].modified:
                title = td['title'].lstrip('*')
                # 切换到该标签页
                for i in range(self._tab_widget.count()):
                    if self._tab_widget.widget(i) == td['tab']:
                        self._tab_widget.setCurrentIndex(i)
                        break

                reply = QMessageBox.question(
                    self, "未保存的更改",
                    f"'{title}' 已被修改，是否保存？",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
                )
                if reply == QMessageBox.Save:
                    self._save_file()
                    if td['data'].modified:
                        event.ignore()
                        return
                elif reply == QMessageBox.Cancel:
                    event.ignore()
                    return

        self._save_workspace_folders()
        self._save_file_notes()
        event.accept()

    # ---- 状态栏 ----
    def _update_statusbar(self):
        data = self._get_data()
        proxy = self._get_proxy()
        if data:
            total = len(data.rows)
            data_rows = sum(1 for r in data.rows if r.row_type == 'data')
            comment_rows = total - data_rows

            if proxy and proxy.is_filtering:
                visible = proxy.rowCount()
                self._status_label.setText(
                    f"共 {total} 行 (数据: {data_rows}, 注释: {comment_rows}) | "
                    f"显示: {visible} 行 (已筛选)"
                )
            else:
                self._status_label.setText(
                    f"共 {total} 行 | 数据行: {data_rows} | 注释行: {comment_rows}"
                )
        else:
            self._status_label.setText("就绪")


# ============================================================================
# 入口
# ============================================================================

def _global_exception_hook(exc_type, exc_value, exc_tb):
    """全局未捕获异常处理器，防止闪退"""
    import traceback
    import io
    buf = io.StringIO()
    traceback.print_exception(exc_type, exc_value, exc_tb, file=buf)
    msg = buf.getvalue()

    # 尝试弹出错误对话框
    try:
        from PySide6.QtWidgets import QMessageBox, QApplication
        app = QApplication.instance()
        if app and app.activeWindow():
            QMessageBox.critical(app.activeWindow(), "程序异常",
                                 f"发生未捕获的异常:\n\n{msg[:500]}")
    except Exception:
        pass

    # 后备：输出到 stderr
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    # 安装全局异常钩子
    sys.excepthook = _global_exception_hook

    app = QApplication(sys.argv)
    app.setApplicationName("Lua Table Editor")
    app.setOrganizationName("LuaTableEditor")

    # 全局样式
    app.setStyleSheet("""
        QMainWindow {
            background-color: #f0f0f0;
        }
        QMenuBar {
            background-color: #e8e8e0;
            border-bottom: 1px solid #ccc;
            padding: 2px;
            font-size: 13px;
        }
        QMenuBar::item:selected {
            background-color: #d0d0d0;
            border-radius: 3px;
        }
        QMenu {
            background-color: #fff;
            border: 1px solid #ccc;
            padding: 4px;
            font-size: 13px;
        }
        QMenu::item:selected {
            background-color: #e0e8f0;
        }
    """)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
