# Lua Table Editor — AI-Agent Technical Reference

> **Target:** AI coding agents for reading, understanding, and maintaining this codebase.
> **Language:** Chinese (project context) / English (code identifiers)
> **Version:** 1.0
> **Generated:** 2026-07-03
> **Entry Point:** `lua_table_editor.py` (4932 lines, single-file PySide6 desktop app)

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

---

## 3. Core Logic

### 3.1 Data Classes

```python
@dataclass
class RowEntry:
    row_type: str = "data"       # "data" | "comment"
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
```

**Critical invariants:**
- `data.rows` contains ONLY inline comments and data rows — NOT `header_comments`.
- `row_key == ""` → serialized as anonymous `{...}`
- `row_key != ""` → serialized as `[key] = {...}`
- `field_names[0]` is always `"id"` if `id` exists in any row.

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

#### 3.4.4 Duplicate Detection

`_rebuild_duplicates()` — O(N*M) scan:
- For each data row, collect `(col_index, serialized_value)`
- Count occurrences across all rows
- Mark as duplicate if count ≥ 2
- Used by delegate for visual highlighting (orange-red text + marker bar)

#### 3.4.5 Row Insertion

| Method | Batch Signal | Purpose |
|--------|-------------|---------|
| `add_data_row(pos)` | Single `beginInsertRows` | Add 1 data row at logical position |
| `add_data_rows_batch(count, pos)` | Single `beginInsertRows` for all | Add N data rows efficiently |
| `add_comment_row(pos)` | Single `beginInsertRows` | Add 1 comment row |
| `remove_row(row)` | `beginRemoveRows` | Delete row (rejects header/return rows) |

All `pos` parameters are logical row indices. Internal conversion to `data.rows` index uses `_logical_to_data_row()`.

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
    'data': LuaTableData,        # current data instance
    'model': LuaTableModel,      # Qt model (source)
    'proxy': FilterProxyModel,   # Qt proxy (filter/sort)
    'view': LuaTableView,        # Qt view widget
    'tab': QWidget,              # tab container widget
    'title': str,                # base filename
    'display_title': str,        # full title with path
    'file_path': str,            # absolute path
    'workspace_folder': str,     # matching workspace folder
    'workspace_color': str,      # matching workspace color hex
}
```

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
| Open file | `Ctrl+O`, DnD, toolbar, welcome | `_open_file()` → `_open_file_path()` | Parses via LuaParser, creates tab |
| Save | `Ctrl+S` | `_save_file()` → `_do_save(path)` | Serializes via LuaSerializer |
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
- `dragEnterEvent`: accepts if any URL ends with `.lua`
- `dropEvent`: calls `_open_file_path()` for first `.lua` file

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
| **No nested table editing** | Subtable values `{a=1,b=2}` are stored as raw strings, not editable as structured data |
| **Field name constraints** | Field names extracted only from `key=value` pairs (not `[key]=value` bracket notation) |
| **Single-file architecture** | ~4932 lines in one file; no module separation |
| **No async I/O** | File loading uses `QApplication.processEvents()` for progress bar; large files may briefly block UI |
| **CSV/Excel import** | Not supported; only Lua table format |
| **No versioning/git integration** | Undo is in-memory only, cleared on file open/close |
| **Field order determined by first occurrence** | If a field first appears in row 5, it is ordered based on row 5, not row 1 |
| **`[string_key]` not fully preserved** | Bracket notation with string keys like `['myKey']` is parsed as row_key but handling is limited |

### 6.2 Constants Reference

```python
# Delegate
LuaTableDelegate.MAX_CHARS = 40         # max visible chars per cell

# Workspace
WorkspacePanel.DEFAULT_COLORS = [...]    # 8 preset workspace colors

# Clipboard
MainWindow._CLIPBOARD_MIME = 'application/x-luatable-rows'

# Row Types
LuaTableModel.ROW_HEADER_COMMENT = 'header_comment'
LuaTableModel.ROW_DATA = 'data'
LuaTableModel.ROW_INLINE_COMMENT = 'inline_comment'
LuaTableModel.ROW_RETURN = 'return'

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

**`_test_rowkey.py`**: Standalone test script that:
- Creates a minimal Qt mock (so `LuaParser` and `LuaSerializer` can be imported without PySide6)
- Parses a test Lua string with `[1]=` through `[11]=` and `[7202]=`, `[7302]=` entries
- Verifies all `row_key` values are preserved through parse→serialize round-trip

Run: `python _test_rowkey.py`

### 6.6 Visual Style Guide

| Element | Color | Purpose |
|---------|-------|---------|
| Header comments | `#006600` (dark green), italic, `#f5f5ee` bg | File-level comments |
| Inline comments | `#666666` (gray), italic, `#f0f0e8` bg | Row-level comments |
| Return line | `#800080` (purple), italic, `#f0e8f5` bg | `return data` |
| Row numbers | `#888888` (light gray) | Row index column |
| Duplicate values | `#c0392b` text, `#ffe8e0` bg, `#e8836b` marker | Duplicate detection |
| Selection | `#a8d4f0` (light blue) bg | Selected cells |
| Highlight (row/col) | `#d8eafc` bg | Current cell highlight |
| Fill handle | `#2980b9` (normal), `#42A5F5` (hover) | Drag fill indicator |
| Tab modified | `*` prefix on tab title | Unsaved changes indicator |
| Workspace stripe | User-chosen color, 3px bar | Tab belongs to workspace |
| Toolbar | `#e8e8e0` bg, white buttons | Top toolbar area |
| Status bar | `#e8e8e0` bg | Bottom status area |
