# Lua Table Editor — AI-Agent Technical Reference

> **Target:** AI coding agents for reading, understanding, and maintaining this codebase.
> **Language:** Chinese (project context) / English (code identifiers)
> **Version:** 2.0
> **Generated:** 2026-10-10（v1.0 初版；v2.0 反映 P1 缺陷修复、Theme 美化、每标签页独立加载、大表性能优化后的现状）
> **Entry Point:** `lua_table_editor.py` (~6050 lines, single-file PySide6 desktop app)
> **Latest status:** 见 `HANDOFF.md` 顶部状态快照；回归测试 `python test_regressions.py`（37 项，offscreen headless）

---

## 1. Architecture Overview

### 1.1 System Topology

```
┌──────────────────────────────────────────────────────────┐
│                     MainWindow (QMainWindow)              │
│  ┌────────────────────┬───────────────────────────────┐  │
│  │  WorkspacePanel    │  QStackedWidget               │  │
│  │  (QWidget, left)   │  ┌──────────────────────────┐ │  │
│  │  - QTreeWidget     │  │ WelcomeWidget (index=0)   │ │  │
│  │  - file search     │  │ TabContainer  (index=1)  │ │  │
│  │  - file notes      │  │  ┌────────────────────┐  │ │  │
│  └────────────────────┘  │  │ QTabWidget         │  │ │  │
│                          │  │  per-tab:           │  │ │  │
│                          │  │  - LuaTableView     │  │ │  │
│                          │  │  - LuaTableModel    │  │ │  │
│                          │  │  - FilterProxyModel │  │ │  │
│                          │  │  - LuaTableDelegate │  │ │  │
│                          │  └────────────────────┘  │ │  │
│                          │  CellDetailPanel (bottom)│ │  │
│                          └──────────────────────────┘ │  │
│  StatusBar: QLabel + QProgressBar (loading indicator) │  │
└──────────────────────────────────────────────────────────┘
```

### 1.2 Core Data Flow

```
┌──────────┐     parse()      ┌──────────────┐     set_data()     ┌───────────────┐
│ .lua     │ ────────────────→ │ LuaTableData │ ────────────────→  │ LuaTableModel │
│ file     │    LuaParser      │ (dataclass)  │                   │ (QAbstract-   │
│ (UTF-8)  │                   │              │                   │  TableModel)  │
└──────────┘                   └──────────────┘                   └───────┬───────┘
                              serialize()                                  │
                            ←──────────────                               │
                             LuaSerializer                           ┌────▼──────┐
                                                                     │ FilterProxy│
 User edits → setData() → model._data.modified=True                  │ Model      │
                                                                     │ (QSortFilter│
                                                                     │ ProxyModel)│
                                                                     └────┬───────┘
                                                                          │
                                                                     ┌────▼──────┐
                                                                     │ LuaTableView│
                                                                     │ + Delegate  │
                                                                     └────────────┘
```

### 1.3 Module Dependency Graph

```
lua_table_editor.py  (monolithic, no external imports beyond stdlib + PySide6)
├── dataclasses       (RowEntry, LuaTableData)
├── re                (regex patterns for Lua parsing)
├── json              (clipboard internal format)
├── copy              (deepcopy for undo/redo row-level ops)
├── PySide6.QtWidgets (UI widgets)
├── PySide6.QtCore    (models, signals, settings)
└── PySide6.QtGui     (painting, fonts, clipboard)
```

**No internal module dependencies** — the entire application is a single Python file.

---

## 2. Environment Configuration

### 2.1 Runtime Requirements

| Dependency    | Version Constraint | Purpose |
|---------------|-------------------|---------|
| Python        | ≥ 3.7             | Language runtime |
| PySide6       | ≥ 6.5.0           | Qt for Python GUI framework |
| PyInstaller   | ≥ 5.13.0          | (optional) Build standalone .exe |

### 2.2 Install Commands

```bash
pip install -r requirements.txt
```

**`requirements.txt` content:**
```
PySide6>=6.5.0
PyInstaller>=5.13.0
```

### 2.3 Run (Development)

```bash
python lua_table_editor.py
```

### 2.4 Build (Standalone .exe)

```bash
build.bat
```

**`build.bat` 等价命令:**
```bash
pyinstaller --onefile --windowed --name "LuaTableEditor" \
  --add-data "requirements.txt;." --clean lua_table_editor.py
```

**Output:** `dist/LuaTableEditor.exe`

**PyInstaller .spec 关键配置:**
- `console=False` (no terminal window)
- `name='LuaTableEditor'`
- `upx=True` (compression enabled)

### 2.5 Persistent Storage

Settings stored via `QSettings("LuaTableEditor", "LuaTableEditor")`:
- `recentFiles` — list of paths (max 8)
- `workspaceFolders` — list of `{path, color}` dicts
- `fileNotes` — dict `{file_path: note_text}`
- `autoRefresh` — bool, 定时自动刷新开关（启动时恢复）

---

## 3. Core Logic

### 3.1 Data Classes

```python
@dataclass
class RowEntry:
    row_type: str = "data"       # "data" | "comment" | "blank"
    comment_text: str = ""       # comment text (only when row_type=="comment")
    data: dict = field(...)      # {field_name: parsed_value} (only when row_type=="data")
    row_key: str = ""            # Lua table key, e.g. "1", "101", "'name'"
                                 # EMPTY means anonymous row: {id=1, ...}
                                 # NON-EMPTY means keyed row: [1]={id=1, ...}

@dataclass
class LuaTableData:
    table_name: str = "NewTable" # e.g. "data", "ItemTable"
    field_names: list = []       # ordered column names; "id" always first if present
    rows: list = []              # list[RowEntry] — excludes header_comments
    file_path: str = ""          # absolute path to source file
    modified: bool = False       # dirty flag
    header_comments: list = []   # lines BEFORE the table declaration (e.g. "-- xxx.xlsx")
    is_local: bool = False       # whether table uses "local Name = {" syntax
    return_var: str = ""         # e.g. "data" for "return data" at file end
    # ---- v2.0 新增 ----
    non_undoable_modified: bool  # 存在过不可撤销修改（增删行/列、粘贴等）；
                                 # _finalize_undo_redo 只有它为 False 才允许复位 modified
    has_bom: bool                # 源文件 UTF-8 BOM（保存时还原）
    eol: str                     # 源文件行尾 '\n'/'\r\n'/'\r'（内部统一 LF，保存时还原）
    dup_counts: dict | None      # worker 预计算的重复值计数（compute_table_aux 填充）
    row_seq_cache: list | None   # worker 预计算的行号前缀缓存
```

另有 `class LuaTableStr(str)`：解析自**未加引号**表表达式的值（如 `{1,2,3}`），序列化时裸写不加引号；普通 `str` 一律加引号——这是"字符串 `\"{build}\"` 不丢引号"的关键区分标记。

**Critical invariants:**
- `data.rows` contains ONLY inline comments, blank rows and data rows — NOT `header_comments`.
- `row_key == ""` → serialized as anonymous `{...}`；匿名数组表（无任何数字行键）新增行保持匿名（强赋 `[1]` 会覆盖第 1 个数组元素）。
- `row_key != ""` → serialized as `[key] = {...}`；字符串键由 `_row_key_to_lua` 自动加引号（`[my_key]` → `["my_key"]`）。
- `field_names[0]` is always `"id"` if `id` exists in any row.
- `blank` 行不可编辑/删除，序列化固定输出空行（原地保留原文）。

### 3.2 LuaParser (Lines 67–419)

**Static class. No instance state.** All methods are `@staticmethod`.

#### 3.2.1 Entry Point: `parse(text: str) -> LuaTableData`

Algorithm:
1. Find table declaration: `Name = {` or `local Name = {` or `{` or `return {`
2. Extract `header_comments`: all `--` lines before the declaration
3. Find matching `}` using depth counter with string-aware skipping
4. Detect `return var` after `}`
5. For each line inside `{...}`:
   - Skip empty lines
   - Classify via `_classify_line()` → `('comment', text)` or `('data', text, row_key)`
6. Collect all field names (`all_fields_set`), determine order by first occurrence
7. Parse each data line key-value pairs via `_parse_kv_pairs()`
8. Return `LuaTableData`

#### 3.2.2 Key Regex Constants

```python
_TABLE_PATTERN = re.compile(r'^\s*(?:local\s+)?(\w[\w.]*)\s*=\s*\{')
_DECL_PATTERN  = re.compile(r'^\s*(local\s+)?(\w[\w.]*)\s*=\s*$')
_RETURN_PATTERN = re.compile(r'^\s*return\s+(\w[\w.]*)\s*$')
_ROW_KEY_PATTERN = re.compile(r'^\s*\[(.+?)\]\s*=\s*\{')
```

#### 3.2.3 `_classify_line(stripped, raw_rows, all_fields_set)`

Input: a single stripped line from inside the table body.
Output: appends `('comment', text)` or `('data', text, row_key)` to `raw_rows`.

Logic:
- Starts with `--` → comment
- Starts with `[` and matches `[...]= {` → data with `row_key` extracted
- Contains `{` and `}` or `=` → data
- Otherwise → data (fallback)

#### 3.2.4 `_parse_kv_pairs(line: str) -> dict`

Parses `{id=1, name="Sword", nested={a=1,b=2}}` style lines.

Steps:
1. Extract content between outermost `{` and `}`
2. `_split_by_comma()` — comma-split with brace/string/escape awareness
3. For each pair: split on `=`, parse value with `_parse_value()`
4. `[key]=value` pairs are handled specially: if value is a subtable, flatten via `_flatten_subtable()`

#### 3.2.5 `_parse_value(val: str) -> Any`

| Lua value     | Python type | Example |
|---------------|-------------|---------|
| `"str"`       | `str`       | `"hello"` → `hello` |
| `'str'`       | `str`       | `'hello'` → `hello` |
| `true/false`  | `bool`      | `true` → `True` |
| `nil`         | `None`      | `nil` → `None` |
| `{a=1}`       | `str`       | kept as raw string |
| `123`         | `int`       | |
| `1.5` / `1e2` | `float`     | |
| other         | `str`       | fallback |

#### 3.2.6 `_split_by_comma(text: str) -> list`

Tokenizes by `,` while respecting:
- Single/double-quoted strings
- Backslash escapes (`\`, `\"`, `\'`)
- Nested brace depth (`{...}`)
- O(N) time, single pass

### 3.3 LuaSerializer (Lines 426–500)

**Static class. No instance state.**

#### `serialize(data: LuaTableData) -> str`

Output reconstruction:
1. Write `header_comments` lines verbatim
2. Write declaration: `local Name = {` or `Name = {` (or `{` for return tables)
3. For each `RowEntry`:
   - comment → `    -- text`
   - data with `row_key` → `    [key] = {k1=v1, k2=v2},`
   - data without `row_key` → `    {k1=v1, k2=v2},`
4. Write closing `}`
5. Write `return var` if present

#### `_value_to_lua(val) -> str`

Reverse of `LuaParser._parse_value()`:
- `None`/`""` → `"nil"`
- `bool` → `"true"`/`"false"`
- `str` starting with `{` → pass through (subtable)
- `str` → `"escaped"`
- `int`/`float` → `str(val)`

### 3.4 LuaTableModel (Lines 507–1108)

**Inherits:** `QAbstractTableModel`

#### 3.4.1 Dual-Index System (CRITICAL)

The model has TWO indexing layers:

| Concept | Name | Source |
|---------|------|--------|
| **Logical row** | Row index in the Qt view (what user sees) | Includes header_comments + data rows + return row |
| **Data index** | Index into `data.rows[]` array | Excludes header_comments, excludes return row |

```
Logical Row Layout:
  0..N-1  → header_comments[N]     (ROW_HEADER_COMMENT)
  N..M-1  → data.rows[0..M-N-1]    (ROW_DATA or ROW_INLINE_COMMENT)
  M       → return row              (ROW_RETURN, only if return_var is set)
```

**Row type constants:**
```python
ROW_HEADER_COMMENT = 'header_comment'  # header comments (non-data, non-editable)
ROW_DATA           = 'data'            # data rows
ROW_INLINE_COMMENT = 'inline_comment'  # inline comments within data.rows[]
ROW_BLANK          = 'blank'           # blank lines (non-editable, kept verbatim)
ROW_RETURN         = 'return'          # "return data" line
```

#### 3.4.2 Key Methods

| Method | Signature | Purpose |
|--------|-----------|---------|
| `_resolve_row` | `(idx: int) -> (row_type, real_index)` | Logical→data mapping |
| `_logical_to_data_row` | `(logical_row: int) -> int` | Logical row to data.rows index |
| `_header_count` | `-> int` | Number of header comment rows |
| `_data_start_row` | `-> int` | First data row logical index (=header_count) |
| `_has_return` | `-> bool` | Whether return row exists |
| `_return_row_idx` | `-> int` | Logical index of return row |

#### 3.4.3 Undo/Redo System

```python
_undo_stack: list  # [(row, col, old_value), ...]  col=-1 means row-level op
_redo_stack: list  # [(row, col, new_value), ...]
```

Two operation types:
- **Cell-level** (`col >= 0`): stores `(logical_row, col, old_value_any_type)`
- **Row-level** (`col == -1`): stores `(data_index, -1, deepcopy(RowEntry))`

Methods:
- `undo()` / `redo()` — pop and apply
- `push_row_undo(row, old_entry)` — used by comment/uncomment operations
- `has_undo()` / `has_redo()` — check availability

#### 3.4.4 Duplicate Detection（v2.0 重构：持久计数 + 三条维护路径）

计数键为 **`(col, type(val).__name__, val)`**（类型化键，与 `_value_to_lua` 分组语义等价，但无逐字符转义开销）。持久存储在 `LuaTableModel._dup_counts`，派生集合 `_duplicate_set`（count≥2）。

| 路径 | 触发场景 | 方法 | 代价 |
|------|---------|------|------|
| worker 预计算 | 打开/刷新文件 | `compute_table_aux(data)` 在加载线程执行，`set_data` 复用 | UI 零重扫 |
| 增量更新 | 单格编辑 `setData` | `_dup_count_adjust(col, val, ±1)`（跨阈值时才发整列 dataChanged） | O(1) |
| 延迟全量 | 结构性变更（粘贴/注释转换/填充/撤销） | `_mark_dups_dirty()` → 行号缓存同步重建，计数在事件循环空闲由 `_refresh_duplicates()` 全量重建 | 一次 O(N×M) |

**规则**：就地修改 `row_type` 后必须调用 `_mark_dups_dirty()`（不要直接调 `_rebuild_duplicates()`，它不会安排延迟刷新与重绘）。

#### 3.4.5 Row Insertion

| Method | Batch Signal | Purpose |
|--------|-------------|---------|
| `add_data_row(pos)` | Single `beginInsertRows` | Add 1 data row at logical position |
| `add_data_rows_batch(count, pos)` | Single `beginInsertRows` for all | Add N data rows efficiently（键连续递增，单次求最大值） |
| `add_comment_row(pos)` | Single `beginInsertRows` | Add 1 comment row |
| `remove_row(row)` | `beginRemoveRows` | Delete row (rejects header/return/blank rows) |

All `pos` parameters are logical row indices. Internal conversion to `data.rows` index uses `_logical_to_data_row()`.

**Modified-flag rule:** 以上方法及粘贴/填充/修复行键等所有**不可撤销**的结构性修改，必须同时置 `data.non_undoable_modified = True`——否则"加行→改格→撤销"会把未保存数据伪装成已保存（BUG-02）。

### 3.4A Per-Tab Loading Pipeline（v2.0，替代全局加载锁）

```
_open_file_path(path)
  ├─ 已打开/加载中/失败占位 → 切换（失败占位可原位重试 _reset_placeholder）
  └─ _create_loading_tab(path)          # 立即可见的 ⏳ 占位页
       └─ _start_load(path, td, cb)     # td['loading']=True，_active_loads 登记
            └─ LuaLoadWorker.run（QThread.LowPriority）
                 ├─ 读盘（newline=''，检测 BOM/行尾）→ LuaParser.parse
                 └─ compute_table_aux(data)  # 重复值计数+行号缓存预计算
                      └─ _on_tab_data_ready → _finalize_tab_load（主线程）
                           └─ _build_tab_content(td)  # set_data 复用预计算，UI 零重扫
```

- **无全局锁**：`td['loading']` 是单页互斥位，多文件并发加载互不影响；`_active_loads` 仅驱动状态栏进度条。
- `_get_current_tab_data()` 对占位/加载中页返回 `None`（下游操作自动 no-op）；操作"可能加载中"的标签页须用原始 `_tabs_data` 查找并 `.get()` 防护。
- 加载失败：占位页就地显示错误并保留（`load_failed=True`），重开同路径文件原位重试。

### 3.5 FilterProxyModel (Lines 1354–1453)

**Inherits:** `QSortFilterProxyModel`

#### Filtering

```python
set_filter(column: int, text: str, mode: str)
```
- `column`: 1-based (col 0 is row number, not filterable)
- `mode`: `"equals"` | `"not_equals"` | `"contains"` | `"not_contains"`
- Comment rows are ALWAYS shown (never filtered)
- `is_filtering` property for status check

**`filterAcceptsRow()`** — uses `LuaTableModel.is_comment_row()` to bypass comment rows.

#### Search

```python
set_search(text: str)
next_search_result() -> int   # returns source model row index
prev_search_result() -> int
search_count: int
current_search_pos: int
```

Search iterates all rows via `model.get_row_text()` for case-insensitive matching.

### 3.6 LuaTableView (Lines 1459–1798)

**Inherits:** `QTableView`
**Signals:** `fill_completed()`, `copy_requested()`, `paste_requested()`

#### Key Features

1. **Inline editing trigger:** Any printable key starts editing; Backspace clears cell
2. **Fill handle (填充柄):** Blue square at bottom-right of selection. Drag to auto-fill:
   - Single numeric value → repeat same value
   - Multiple numeric values with constant diff → continue arithmetic sequence
   - Non-numeric → cycle pattern
3. **Ctrl+C/V:** Emits `copy_requested`/`paste_requested` signals (handled by MainWindow)
4. **Row/Column highlighting:** Tracks `_highlight_row`/`_highlight_col` for visual feedback

#### Fill Algorithm (`_compute_fill`)

```python
_compute_fill(values: list, offset: int) -> Any
```
- 1 value → return same value (numbers kept as number)
- N values → check if all numeric with constant difference → continue sequence
- Otherwise → cycle: `values[(offset-1) % len(values)]`

### 3.7 Copy/Paste System (Lines 3750–4208)

#### Internal MIME Format

MIME type: `application/x-luatable-rows`

JSON structure:
```json
{
  "source": "lua_table_editor",
  "version": 1,
  "fields": ["id", "name", "type"],
  "is_full_rows": true,
  "entries": [
    {
      "__row_type": "data",
      "__row_key": "1",
      "id": "1",
      "name": "\"Sword\"",
      "type": "\"weapon\""
    },
    {
      "__row_type": "comment",
      "__row_key": "",
      "comment_text": "-- divider"
    }
  ]
}
```

**Key design rules:**
- `__row_type` and `__row_key` are metadata fields (always included)
- Values are serialized Lua strings (using `LuaSerializer._value_to_lua()`)
- `is_full_rows` flag: true when all fields are selected (treat as row paste)
- Text format (fallback): tab-separated with optional header line

#### Paste Branches

| Function | Trigger | Behavior |
|----------|---------|----------|
| `_copy_selection()` | Ctrl+C | Copies selected cells as internal JSON + tab text |
| `_paste_at_selection()` | Ctrl+V | Routes to `_paste_internal` or `_paste_text` |
| `_paste_internal()` | Internal format | 3 sub-branches: no-selection, full-row paste, cell paste |
| `_paste_text()` | Plain text | Parses tab-separated lines, maps columns by position |
| `_copy_current_row()` | Right-click | Copies entire row at cursor |
| `_paste_row_after_cursor()` | Right-click | Inserts clipboard rows after cursor |

**Critical safety rule:** ALL paste functions MUST use `model._logical_to_data_row()` or `model._resolve_row()` to convert logical view rows to `data.rows` indices. Direct indexing of `data.rows[logical_row]` is BUGGY when header comments exist.

### 3.8 Comment/Uncomment (Lines 4276–4426)

```python
_comment_selected_rows():
  for each selected data row:
    push_row_undo(row, deepcopy(old_entry))
    replace with RowEntry(row_type='comment', comment_text='-- {k=v,...}', row_key=old_key)

_uncomment_selected_rows():
  for each selected comment row:
    parse comment_text → dict
    push_row_undo(row, deepcopy(old_entry))
    replace with RowEntry(row_type='data', data=parsed_dict, row_key=old_key)
```

**row_key preservation:** Both operations preserve the original `row_key` in the new `RowEntry`.

### 3.9 Row Key Repair (Lines 4244–4274)

```python
_repair_row_keys():
  for each data row without row_key:
    if row has 'id' field:
      row.row_key = str(id)
      restored_count += 1
```

Triggered by menu item `Ctrl+Shift+K` or right-click context menu. Designed to recover from historical bug where copy/paste lost row_key.

---

## 4. Data Model

### 4.1 Entity-Relationship

```
LuaTableData (1) ──── (N) RowEntry
      │
      ├── field_names: list[str]
      │     └── determines column order in table view
      │
      ├── rows: list[RowEntry]
      │     ├── row_type="data"  → data dict {field: value}
      │     └── row_type="comment" → comment_text
      │
      ├── header_comments: list[str]
      │     └── lines preserved verbatim before table declaration
      │
      ├── is_local: bool
      │     └── controls "local Name = {" vs "Name = {" output
      │
      └── return_var: str
            └── if non-empty, appends "return X" at file end
```

### 4.2 Supported Lua Table Formats

| Format | Example | Parsed As |
|--------|---------|-----------|
| Anonymous array | `ItemTable = {\n    {id=1, name="Sword"},\n}` | Standard table |
| Keyed table | `Config = {\n    [10001] = {id=10001, ...},\n}` | Each entry has `row_key="10001"` |
| Return table | `return {\n    {id=1, ...},\n}` | `return_var="Table"` |
| Local table | `local ItemTable = {\n    ...}` | `is_local=True` |
| Multi-line decl | `local data =\n{\n    ...}` | `is_local=True`, declaration split across lines |
| Mixed comments | `-- header\nName = {\n    -- inline\n    {id=1},\n}` | Both header and inline comments preserved |
| Nested subtables | `{id=1, pos={x=0,y=0}}` | Subtable kept as raw string |

### 4.3 Field Value Types

| Stored Type | Display | Edit Input | Serialize |
|-------------|---------|------------|-----------|
| `int` | number | number | number |
| `float` | number | number | number |
| `str` | text | text | `"text"` |
| `bool` | `true`/`false` | `true`/`false` | `true`/`false` |
| `None` | empty | `nil` | `nil` |
| `str` starting with `{` | subtable text | leave as-is | subtable text |

### 4.4 Tab Data Dictionary

Each open tab is tracked in `MainWindow._tabs_data[id(widget)]`:
```python
{
    'data': LuaTableData | None, # current data instance (None while placeholder)
    'model': LuaTableModel | None,
    'proxy': FilterProxyModel | None,
    'view': LuaTableView | None,
    'tab': QWidget,              # tab container widget
    'title': str,                # base filename
    'display_title': str,        # full title with path
    'file_path': str,            # absolute path
    'loading': bool,             # worker in flight (per-tab mutex)
    'placeholder': bool,         # True until real table is built
    'load_failed': bool,         # placeholder error state (retry on reopen)
    'page': QWidget | None,      # placeholder loading page widget
    'workspace_folder': str,     # matching workspace folder
    'workspace_color': str,      # matching workspace color hex
}
```
占位页阶段 model/proxy/view 为 `None`；`_get_current_tab_data()` 对此类页返回 `None`。

### 4.5 Workspace Data

```python
# WorkspacePanel internal state
_workspaces: list  # [(folder_abs_path, color_hex, QTreeWidgetItem), ...]
_file_notes: dict  # {file_abs_path: note_text}
_color_index: int  # cycles through DEFAULT_COLORS

DEFAULT_COLORS = [
    "#d8e8d8", "#e8d8e8", "#d8d8e8", "#e8e8d0",
    "#e8d8d0", "#d0e0e8", "#f0e8d0", "#d0e8e0"
]
```

---

## 5. Interface Specification

### 5.1 File Operations

| Operation | Trigger | Function | Notes |
|-----------|---------|----------|-------|
| Open file | `Ctrl+O`, DnD, toolbar, welcome | `_open_file()` → `_open_file_path()` | 每标签页独立后台加载（见 §3.4A），占位页→worker 解析→原位填充 |
| Save | `Ctrl+S` | `_save_file()` → `_do_save(path)` | Serializes via LuaSerializer；临时文件原子替换，BOM/行尾还原 |
| Save As | `Ctrl+Shift+S` | `_save_file_as()` | Prompts for path |
| Refresh | `F5` | `_refresh_file()` | Re-reads from disk, warns on unsaved |
| Close tab | `Ctrl+W` | `_close_tab()` | Prompts save if modified |
| New table | `Ctrl+N` | `_new_table()` | Empty LuaTableData with auto-name |

### 5.2 Edit Operations

| Operation | Trigger | Function | Undoable |
|-----------|---------|----------|----------|
| Cell edit | Double-click / type / Backspace | `setData()` | Yes (cell-level) |
| Add data row | `Ctrl+Shift+N` / toolbar | `_add_row()` | No (structural) |
| Add N rows | `Ctrl+Shift+M` / toolbar dropdown | `_add_rows_batch()` | No |
| Add comment row | `Ctrl+Shift+C` | `_add_comment_row()` | No |
| Add column | `Ctrl+Shift+L` | `_add_column()` | No |
| Delete row | `Delete` / right-click | `_delete_row()` | No |
| Comment rows | `Ctrl+/` | `_comment_selected_rows()` | Yes (row-level) |
| Uncomment rows | `Ctrl+Shift+/` | `_uncomment_selected_rows()` | Yes (row-level) |
| Undo | `Ctrl+Z` | `eventFilter()` → `model.undo()` | N/A |
| Redo | `Ctrl+Y` / `Ctrl+Shift+Z` | `eventFilter()` → `model.redo()` | N/A |
| Repair keys | `Ctrl+Shift+K` / menu | `_repair_row_keys()` | No |

### 5.3 View Operations

| Operation | Trigger | Function |
|-----------|---------|----------|
| Toggle workspace | `Ctrl+B` | `_toggle_workspace()` |
| Toggle detail | `Ctrl+D` | `_toggle_detail()` |
| Search | `Ctrl+F` | `_show_find_dialog()` |
| Filter | Right-click column header | `_on_header_context_menu()` |
| Clear filter | Column header menu | `_clear_filter()` |

### 5.4 Navigation Keys (in LuaTableView)

| Key | Behavior |
|-----|----------|
| `Enter` | Confirm edit → move to next row same column |
| `Tab` | Move to next column (wrap to next row) |
| `Shift+Tab` | Move to previous column (wrap to previous row) |
| Any printable char | Start editing with that character |
| `Backspace` | Clear cell content without entering edit mode (Delete is bound to 删除当前行 menu action, which takes priority) |
| `Ctrl+Click` | Non-contiguous multi-select |
| `Shift+Click` | Range select |

### 5.5 Clipboard MIME Types

| MIME Type | Purpose |
|-----------|---------|
| `application/x-luatable-rows` | Internal copy/paste (JSON): preserve row_type, row_key, typed values |
| `text/plain` | External compatibility: tab-separated with header, or Lua-style `{k=v,...}` lines |

### 5.6 Drag & Drop

MainWindow accepts file drops:
- `dragEnterEvent`: accepts if any URL ends with `.lua`（不区分大小写）
- `dropEvent` → `_open_files(paths)` → 逐个 `_open_file_path()`：每标签页独立并发加载，互不阻塞

### 5.7 Welcome Page

WelcomeWidget appears when NO tabs are open (`_tab_widget.count() == 0`):
- Offers "Open" and "New" buttons
- Displays drag-drop hint
- Switched via `QStackedWidget` (index 0 = welcome, index 1 = tabs)

### 5.8 Status Bar

Shows: `"共 X 行 | 数据行: Y | 注释行: Z"` when data loaded.
During loading: progress bar (indeterminate animation).
Filtering active: appends `"| 显示: N 行 (已筛选)"`.

---

## 6. Maintenance & Extension

### 6.1 Known Limitations

| Limitation | Details |
|------------|---------|
| **No nested table editing** | Subtable values `{a=1,b=2}` are stored as `LuaTableStr` raw strings, not editable as structured data |
| **Field name constraints** | Field names extracted only from `key=value` pairs (not `[key]=value` bracket notation) |
| **Single-file architecture** | ~6050 lines in one file; no module separation |
| **CSV/Excel import** | Not supported; only Lua table format |
| **No versioning/git integration** | Undo is in-memory only, cleared on file open/close |
| **Field order determined by first occurrence** | If a field first appears in row 5, it is ordered based on row 5, not row 1 |
| **Unquoted identifier values** | `kind=SomeEnum` 解析为字符串，重新生成时带引号输出（引号信息解析时即丢失） |
| **Parse performance** | 解析为纯 Python（GIL 约束），5 万行约 4~5s，期间 UI 保持响应但 CPU 占用高 |

### 6.2 Constants Reference

```python
# Theme（v2.0：全部 UI 颜色常量的唯一来源，P4 美化入口）
Theme.ACCENT / SURFACE / SELECTION / SEL_ROW / SEL_COL / SEL_CROSS / SEL_BORDER
Theme.DUP_BG / DUP_TEXT / DUP_MARKER        # 重复值
Theme.CMT_* / HDR_CMT_* / RET_*             # 注释与结构行语义色
# ... 完整清单见 lua_table_editor.py 顶部 class Theme

# Delegate
LuaTableDelegate.MAX_CHARS = 40             # max visible chars per cell

# MainWindow
MainWindow.RESIZE_SAMPLE_ROWS = 300         # 列宽自适应抽样行数（勿改回全表测量）
MainWindow._CLIPBOARD_MIME = 'application/x-luatable-rows'

# Workspace
WorkspacePanel.DEFAULT_COLORS = [...]       # 8 preset workspace colors

# Row Types
LuaTableModel.ROW_HEADER_COMMENT = 'header_comment'
LuaTableModel.ROW_DATA           = 'data'
LuaTableModel.ROW_INLINE_COMMENT = 'inline_comment'
LuaTableModel.ROW_BLANK          = 'blank'
LuaTableModel.ROW_RETURN         = 'return'

# Filter Modes
FilterProxyModel._filter_mode: "equals" | "not_equals" | "contains" | "not_contains"

# Max recent files
_recent_files[:8]
```

### 6.3 Extension Points

| Extension Point | Location | How to Extend |
|----------------|----------|---------------|
| **New field types** | `LuaParser._parse_value()` + `LuaSerializer._value_to_lua()` | Add new type handling in both directions |
| **New Lua syntax** | `LuaParser.parse()` regexes | Add pattern to `_TABLE_PATTERN` / `_DECL_PATTERN` |
| **New edit operations** | `MainWindow` menu setup + handler | Add `QAction` to `_setup_menu()`, implement handler method |
| **New column/field operations** | `LuaTableModel` + `MainWindow` | Add model method for structural change, wire to UI |
| **Plugin architecture** | N/A (monolithic) | Would require refactoring into separate modules |
| **Theming** | `app.setStyleSheet()` in `main()` | Override the global stylesheet |
| **Multi-language** | Hardcoded strings throughout | Externalize all Chinese strings to resource files |
| **Import/Export** | New static methods | Add `LuaParser.parse_csv()` / `LuaSerializer.serialize_csv()` |

### 6.4 Critical Code Sections (Bug-Prone Areas)

1. **`_resolve_row()` / `_logical_to_data_row()` (lines 569–1100):** The logical-to-data row mapping MUST be used in every function that accesses `data.rows[]`. Failure to do so causes index errors and data corruption when header comments exist.

2. **`_copy_selection()` (line 3753):** Must include `__row_key` in clipboard entries. Missing `__row_key` causes `row_key` loss on paste.

3. **`_comment_selected_rows()` / `_uncomment_selected_rows()` (lines 4313–4426):** Must preserve `row_key` when creating new `RowEntry`. Missing `row_key` parameter causes key loss.

4. **`undo()` / `redo()` (lines 888–983):** Row-level undo uses `copy.deepcopy()` for `RowEntry` objects to prevent reference sharing. Cell-level undo uses direct value storage.

5. **`_split_by_comma()` (line 292):** Must handle all of: quoted strings, escape sequences, nested braces, empty tokens. Failure here breaks parsing of complex Lua values.

### 6.5 Testing

**`test_regressions.py`**（v2.0 唯一有效测试，37 项，offscreen headless，无需显示环境）：
- 序列化层：零 diff、最小 diff、幂等（`parse→serialize→写盘→再 parse→再 serialize` 第二轮字节一致）、BOM/CRLF 还原、行键引号、转义
- 模型层：modified 语义（BUG-02）、空行不可编辑、行号缓存、筛选越界、增量/预计算重复值
- 主窗口层：带表头注释的注释/取消注释、插入位置、粘贴安全、独立加载（占位页/并发/重复打开）、选区描边冒烟、抽样列宽

Run: `python test_regressions.py`（全部通过输出 `ALL PASSED`，失败非零退出）

**禁止**使用已过时的 `_test_rowkey.py` / `_gui_flow_test.py`（mock PyQt5，与现状不符，仅作历史参考）。

### 6.6 Visual Style Guide

**v2.0 起所有颜色统一收敛在 `class Theme`（文件顶部常量类），禁止在代码里散落十六进制色值。** 语义要点：

| Element | Theme 常量 | Purpose |
|---------|-----------|---------|
| Header comments | `HDR_CMT_BG` / `HDR_CMT_TEXT`（绿系） | File-level comments |
| Inline comments | `CMT_BG` / `CMT_TEXT`（灰） | Row-level comments |
| Return line | `RET_BG` / `RET_TEXT`（紫） | `return data` |
| Duplicate values | `DUP_BG` / `DUP_TEXT` / `DUP_MARKER` | Duplicate detection |
| Selection | `SELECTION` / `SEL_ROW` / `SEL_COL` / `SEL_CROSS` | 选中/整行/整列/交叉 |
| Selection outline | `SEL_BORDER` | 选区外沿描边（`_draw_selection_outline`） |
| Focus cell | `FOCUS` | 当前格焦点框 |
| Fill handle | `FILL` / `FILL_BORDER` / `FILL_HOVER` | Drag fill indicator |
| Tab modified | `*` prefix on tab title | Unsaved changes indicator |
| Workspace stripe | User-chosen color, 3px bar | Tab belongs to workspace |
| Loading placeholder | `SURFACE` 底 + `TEXT_MUTED` 文字 | 每标签页独立加载占位页 |
