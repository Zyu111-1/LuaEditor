# HANDOFF —— Lua Table Editor 缺陷修复与美化交接文档

> **读者**：接手本项目的下一个 AI 模型（或人类工程师）。
> **前提**：本文档由一次全量代码审查产出，缺陷条目均标注**是否已实证复现**。
> **项目根**：`f:/Workspace/codebuddy`　**当前分支**：`feature/rowkey-sync-load-refresh`
> **请勿删除** `.codebuddy/` 目录。

---

## ★ 修复状态快照（2026-09-24，P1 批次已完成）

P1-A / P1-B 的功能缺陷修复已全部落地，回归测试 **29/29 通过**（`python test_regressions.py`）。
真实数据端到端冒烟（`biaodan/ClothData.lua` 临时副本，1MB / 1307 行 / CRLF）：加载→改值→保存→重载→再序列化 **字节级幂等**，CRLF 无二次翻译。

**已修复**：BUG-01、02、03、04、05、06、07(a/b/c/d)、08、09、10、11、12、13、14、15、16、17、18、19、20、25、26、27、29、32；PERF-01、03、05。

**修复过程中新发现并已修复**：
1. `_commit_fill` 使用 `self._next_default_row_key(data)`（视图无此方法），填充柄拖过表尾必崩 `AttributeError` → 已改用 `LuaTableModel._next_default_row_key`。
2. 匿名数组表中 `add_data_row` 强行赋予 `[1]` 行键——Lua 中 `[1] = x` 会**覆盖第 1 个数组元素**，新行保存后顶掉原有行 → 已改为：表中无任何数字行键时新行保持匿名。
3. 注释行文本被编辑为不以 `--` 开头时，保存后重新加载该行被解析器**静默丢弃** → 已改为编辑时自动补 `-- ` 前缀。

**本次引入的机制（后续修改必须了解）**：
- `LuaTableData.non_undoable_modified`：所有不可撤销的修改（增删行/列、粘贴、修复/批量同步行键、填充追加行）必须置 True；`_finalize_undo_redo` 只有它为 False 时才允许复位 `modified`。
- `LuaTableData.has_bom` / `eol`：文件级元信息。内部文本统一 LF；读取（worker，`newline=''`）时检测真实行尾，保存（`_do_save`，`newline=''` + 临时文件原子替换）时还原。
- `LuaTableStr(str)`：解析自未加引号表表达式的值；序列化时裸写。普通 str 一律加引号。
- `LuaTableModel._row_seq`：行号前缀缓存，随 `_rebuild_duplicates()` 与 `endInsertRows/endRemoveRows` 自动刷新；就地改 `row_type` 后必须调用 `_rebuild_duplicates()`。
- `LuaTableModel.ROW_BLANK`：空行类型，不可编辑、不可删除、不占数据序号。
- `MainWindow._pending_opens` + `_drain_pending_opens`：多文件拖放排队。

**修复后仍遗留（未处理，后续批次）**：
- BUG-21（`_get_brace_content` 的 rfind 边界）、BUG-22（Lua 长字符串 `[[...]]`）、BUG-23（重复值误报，属设计取舍）、BUG-24（混合键类型的默认键）、BUG-28（未编辑的新建表关闭无提示）、BUG-30（工作区递归无符号链接保护/自然排序）、BUG-31（冻结浮层重建开销）。
- PERF-02（每次 `setData` 全列刷新 + O(N×M) 重复值重算）、PERF-04（`lru_cache` 缓存整行原文）。
- §5 的全部 MISSING 功能项（排序、查找替换、导入导出等）与 §4.1 的结构重构。
- 已知语义限制（未修，原样保留）：未加引号的标识符值（如 `kind=SomeEnum`）在行被重新生成时会变成 `"SomeEnum"` 字符串——引号信息在解析时即丢失，修复需改值表示方式，超出 P1 范围。

---

---

## 0. 任务边界（先读这一节）

本仓库存在**两条互不重叠的工作线**，必须串行、分批、单独提交：

| 批次 | 目标 | 允许改动 | 禁止改动 |
|---|---|---|---|
| **P1 数据安全修复** | 消灭会丢数据/坏文件的缺陷 | 解析、序列化、模型索引、保存 I/O | 视觉样式、布局、配色 |
| **P2 可维护性重构** | 抽取样式常量、消除重复索引转换、清理死代码 | 结构整理，**行为必须逐字不变** | 任何功能语义变更 |
| **P3 功能补齐** | 排序、行键唯一性、编码兼容等 | 新功能 | 破坏 P1 的守卫（回归测试） |
| **P4 UI 美化** | 视觉与交互打磨 | QSS、自绘、布局、图标 | 业务逻辑、序列化、索引映射 |

**硬性纪律**

- P1 未完成、未通过验收前，**不要开始 P4**（美化会大面积改 `setStyleSheet` 与 `paint`，与 P1 的 diff 直接冲突）。
- 不要重写 `lua_table_editor.py` 整体结构（见 §5 禁止清单）。
- 每修一条缺陷，**必须**同步补一个 headless 探针（§6），否则视为未完成。

---

## 1. 项目事实（环境与资产）

| 项 | 值 |
|---|---|
| 技术栈 | Python 3.9+ / PySide6（实测环境 6.11.1）/ PyInstaller |
| 入口 | `lua_table_editor.py`（**单文件 6073 行**，无内部模块依赖） |
| 运行 | `python lua_table_editor.py` 或 `run.bat` |
| 打包 | `build.bat` → `dist/LuaTableEditor.exe` |
| 依赖 | `requirements.txt`：`PySide6>=6.5.0`、`PyInstaller>=5.13.0`（无上界） |
| 样例数据 | `biaodan/ClothData.lua`（**UTF-8 无 BOM、CRLF、约 1MB、数千行、无表头注释、无 return**） |
| 测试现状 | **无测试框架**。`_test_rowkey.py` 用 **PyQt5** 假模块 mock（已失效），`_gui_flow_test.py` 是手工打印脚本 |
| 持久化 | `QSettings("LuaTableEditor","LuaTableEditor")`，实际键仅 `fileNotes` / `recentFiles` / `workspaceFolders` |
| 文档 | `README.md`、`AI_TECHNICAL_DOC.md` —— **均与实际不符**（行数、行号、Delete 语义、快捷键表都有偏差），以代码为准 |

### 单文件逻辑分区（便于定位）

| 行区间 | 内容 |
|---|---|
| 1–38 | import（含重复与未使用项，见 §4.2） |
| 43–72 | `RowEntry` / `LuaTableData` 数据类 |
| 79–529 | `LuaParser`（解析、行键、值解析、逗号切分） |
| 536–697 | `LuaSerializer`（零 diff 序列化、值转 Lua） |
| 704–1416 | `LuaTableModel`（双索引、undo/redo、行增删、重复值） |
| 1423–1779 | `LuaTableDelegate`（自绘、多行编辑器） |
| 1786–1884 | `FilterProxyModel`（列筛选、查找） |
| 1891–2424 | `LuaTableView`（多选、填充柄、**冻结列 overlay**） |
| 2431–2523 | `FindDialog` |
| 2530–2601 | `CellDetailPanel` |
| 2608–2694 | `WelcomeWidget` |
| 2701–3205 | `WorkspacePanel` |
| 3212–3511 | `HelpDialog` / `AboutDialog` |
| 3516–3539 | `_LoadSignals` / `LuaLoadWorker`（后台加载线程） |
| 3542–6002 | `MainWindow`（**上帝对象**：文件/编辑/剪贴板/行键/查找/筛选/工作区/状态栏/事件过滤） |
| 6009–6073 | 全局异常钩子 + `main()` |

---

## 2. 必须守住的核心不变量（改代码前请背下来）

违反其中任意一条，都会产生"看起来没问题、保存后数据变了"的隐性事故。

### INV-1　逻辑行 ≠ 数据行下标
模型有两套行号：

```
逻辑行（视图行号，QModelIndex.row()）
  0 .. H-1        → header_comments[H]
  H .. H+R-1      → data.rows[0..R-1]（数据行 + 内联注释行 + 空行）
  H+R             → return 行（仅当 data.return_var 非空）

数据行下标 = 逻辑行 - H（H = len(data.header_comments)）
```

- **任何**访问 `data.rows[...]` 的地方，都必须先经过 `LuaTableModel._resolve_row()` 或 `_logical_to_data_row()`。
- 反例（**已确认踩坑处**）：`filterAcceptsRow`（1832 行）、`_comment_selected_rows`（5339 行）、`_uncomment_selected_rows`（5389 行）、`_check_selected_row_types`（5926 行）。
- 注意：`header_comments` **包含表声明行本身**。例如
  ```lua
  --                     ← header_comments[0]
  -- X.xlsx              ← header_comments[1]
  --                     ← header_comments[2]
  local data =           ← header_comments[3]
  {                      ← 表体起点
  ```
  此时 H = 4，第一条数据行的逻辑行是 **4**，不是 0。

### INV-2　零 diff（未修改不得改动原文）
`LuaTableData` 保存了用于逐字还原的原文：`_raw_text`、`decl_raw`、`close_raw`、`footer_raw`、每行 `RowEntry._raw_lines` + `_dirty`。

- `modified=False` 且 `_raw_text` 非空 → `serialize()` 必须**原样返回** `_raw_text`。
- 未 `_dirty` 且有 `_raw_lines` 的行 → 必须输出 `_raw_lines` 原文，**不得重新生成**。
- 违反后果：改一个格子 → 整表行尾/缩进/对齐全部重排，diff 爆炸。

### INV-3　`modified` 的语义 = "磁盘内容与内存不一致"
全局唯一的"是否需要提示保存"依据。**不允许**被撤销逻辑无依据地复位（见 BUG-02）。

### INV-4　行键（`row_key`）必须存活
`row_key != ""` → 输出 `[key] = {...}`；`row_key == ""` → 输出 `{...}`。
任何"复制/粘贴/注释/取消注释/修复键"路径都必须显式传递 `row_key`，否则键丢失（历史上已发生过一次）。

### INV-5　文件字节级格式
- 不要改行尾。样例数据是 **CRLF**，当前实现会把它写成 `\r\r\n`（见 BUG-05）。
- 不要假设 UTF-8。BOM 与 GBK 都会出事（见 BUG-04、MISSING-05）。

---

## 3. 缺陷清单（P1 优先，按严重度排序）

> 标【实证】者均已用 headless Qt 探针实际复现（探针已清理，需按 §6 重建）。

### P1-A 数据安全（不修不能发布）

---

#### BUG-01【实证｜严重】逻辑行当作 `data.rows` 下标 → 列筛选报错 + 注释功能失效
**位置**
```1832:1832:lua_table_editor.py
        row = model._data.rows[source_row]
```
```5335:5341:lua_table_editor.py
            src_idx = proxy.mapToSource(idx) if proxy else idx
            row = src_idx.row()
            if row < 0 or row >= len(data.rows):
                continue
            entry = data.rows[row]
```
同类：`5387:5389`、`5921:5926`。

**复现**
- 输入：`-- / -- ChangeClothData.xlsx / -- / local data = / { [1]={...},[2]={...},[3]={...} } / return data`
- 启用列筛选（任选一列、contains）→ `IndexError: list index out of range`（源行号 5 超出 `data.rows` 长度 3）。
- 即使无表头注释，只要表尾有 `return` 行，对 return 行调用同样越界。
- 选中视图中**第一条数据行**后调用 `_check_selected_row_types()` → 返回 `(False, False)`；`_comment_selected_rows()` 提示"选中的行中没有数据行可注释"。

**影响面**：所有带表头注释（即绝大多数真实策划文件）或带 `return` 的表 —— 列筛选、注释选中行、取消注释、右键菜单启用态判断全部失效或错位（错位时会把别的行注释掉）。样例 `ClothData.lua` 恰好无表头注释/无 return，所以**回归测试不能只用它**。

**验收**：上述两者在带表头注释 + return 的表上均正常；且不得出现任何 `data.rows[逻辑行]` 形式的直接索引。

---

#### BUG-02【实证｜严重】撤销/重做把 `modified` 错误复位 → 未保存数据静默丢失
**位置**
```1119:1127:lua_table_editor.py
    def _finalize_undo_redo(self):
        if not self._undo_stack:
            # 所有操作已撤销 → 内存状态回到加载时，整文件未修改
            self._data.modified = False
            for r in self._data.rows:
                r._dirty = False
```
**根因**：判定条件只有"undo 栈为空"，但**结构性操作（增删行、增删列、粘贴、填充新增行、批量同步行键）从不入栈**（见 §4.1）。于是"先加行、再改一格、再 Ctrl+Z"就会把 `modified` 打回 `False`。

**复现**：`add_data_row(1)` → `modified=True`；`setData(index(0,2),'999')` → `undo()` → `modified=False`，行仍多 1 行。此后关闭标签页**不提示保存**，新增行丢失。

**影响面**：全功能。最典型的策划操作序列（批量加行 → 填值 → 改错一格撤销）就会踩中。

**验收**：`modified` 只能由"内容是否等于磁盘/加载状态"决定，不能仅由 undo 栈空判定；且结构性操作应纳入撤销或至少不参与该判定。

---

#### BUG-03【实证｜严重】空行（`blank`）可编辑，输入内容保存时静默丢弃
**位置**
```791:795:lua_table_editor.py
        if data_idx < dr_count:
            entry = self._data.rows[data_idx]
            if entry.row_type == 'comment':
                return (self.ROW_INLINE_COMMENT, data_idx)
            return (self.ROW_DATA, data_idx)
```
```601:602:lua_table_editor.py
            if row.row_type == 'blank':
                gen = ['']
```
**根因**：`_resolve_row` 只识别 `'comment'`，`'blank'` 落到 `ROW_DATA` → `flags()` 授予 `ItemIsEditable` → `setData()` 把值写进 `row.data`（UI 也显示）→ 序列化对 `blank` 行固定输出空串。

**复现**：输入 `T = {\n    {id=1},\n\n    {id=2},\n}`，向空行输入 `123`，序列化输出仍为原文，值消失。

**验收**：空行要么不可编辑（推荐，保持原文），要么可编辑且能被正确序列化。不允许"能输入但不保存"。

---

#### BUG-04【实证｜严重】UTF-8 BOM 文件保存后丢失表声明行，产出非法 Lua
**位置**：解析兜底分支
```120:135:lua_table_editor.py
        if table_start == -1:
            # 尝试匹配 return {  或单纯的 {
            for i, line in enumerate(lines):
                s = line.strip()
                if s == '{' or s.startswith('{') or s == 'return {' or s.startswith('return {'):
```
**复现**：解析 `'\ufefflocal data =\n{\n    {id=1},\n}'` → `table_name='Table'`、`is_local=False`、`header_comments=[]`、`decl_raw='{'`；改动任一格后保存 → 输出 `'{\n\t{id=2, },\n}'`，**`local data =` 整行消失**。

**影响面**：Windows 下大量策划文件带 BOM。首次改动即破坏源文件。

**验收**：带 BOM 文件解析结果与不带 BOM 完全一致，保存后声明行原样保留。

---

#### BUG-05【实证｜严重】CRLF 文件保存后行尾变 `\r\r\n`，持续劣化
**位置**
```4363:4365:lua_table_editor.py
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(text)
```
**根因**：解析保留 `\r`（`text.split('\n')`），写回时用文本模式且未指定 `newline=''`，`\n` 被 `os.linesep` 再翻译一次。

**复现**：输入 `"T = {\r\n    {id=1},\r\n}\r\n"`，`modified=True` 后写盘 → `b'T = {\r\r\n    {id=1},\r\r\n}'`。

**影响面**：**项目自带的 `biaodan/ClothData.lua` 就是 CRLF** —— 每次保存污染全文件，diff 全红。

**验收**：CRLF 进 → CRLF 出；LF 进 → LF 出；混合行尾不被二次翻译。

---

#### BUG-06【实证｜高】"批量添加行"插入位置错误
**位置**
```5472:5481:lua_table_editor.py
        if pos is None:
            sel = view.selectionModel().selectedIndexes()
            if sel:
                # 找到选中区域最后一行，插在其后
                pos = max(idx.row() for idx in sel) + 1
            else:
                pos = len(model.table_data.rows)

        for i in range(count):
            model.add_data_row(pos + i)
```
**复现**：H=4 的表，无选中时 `pos = 3`（数据行数被当作逻辑行号）→ `_logical_to_data_row(3)` 因 `3 <= H` 返回 0 → **插到表首**。有选中时用了 proxy 行号当 source 逻辑行，筛选状态下同样错位。

**影响面**：`Ctrl+Shift+M`、右键"插入复数行"、工具栏"➕ 数据行"（三者共用 `_do_add_rows` 与 `_do_add_rows` 的兄弟 `_add_rows_custom`）。

**验收**：带表头注释 / 带 return / 筛选中三种场景下均在预期位置插入。

---

#### BUG-07【实证｜高】序列化产出非法或语义错误的 Lua
**位置**：`_value_to_lua`（678–697）与行输出（620–623）

| 子项 | 现象 | 复现输入 → 输出 |
|---|---|---|
| a. 非数字行键不加引号 | `[my_key]` 非法 Lua（应为 `['my_key']`） | `row_key='my_key'` → `[my_key] = {...}` |
| b. 反斜杠不转义 | 字面 `\n` 变成换行 | 内存 `a\nb`（4 字符）→ 文件 `"a\nb"` |
| c. 形似表的字符串丢引号 | `"{build}"` → `{build}`（变表/未定义变量） | 值 `{build}` → `tag={build}` |
| d. 数字被改写 | `0x1F`→`"0x1F"`（十六进制变字符串）、`007`→`7`、`1e3`→`1000.0`、`1.50`→`1.5` | 见 `_parse_value` 524–529 |

```687:694:lua_table_editor.py
            # 逐字符转义，避免链式 replace 的交叉干扰
            # 注意：不转义反斜杠，保留原始文本中的 \n \r \t 等字符
            _ESC_MAP = {
                '"': '\\"', '\n': '\\n',
                '\r': '\\r', '\t': '\\t', '\0': '\\0',
            }
```
**影响面**：任何"被编辑过的行"都可能被改写写法。b/c 会静默改变游戏侧语义，属高危。

**验收**：对上述四类值做 parse→serialize 往返，语义等价（允许格式差异，不允许语义差异）。

---

#### BUG-08【实证｜高】`Delete` 快捷键冲突，文档承诺的"清空单元格"失效
**位置**
```3620:3620:lua_table_editor.py
        self._act_del_row = self._add_action(edit_menu, "删除当前行", "Delete", self._delete_row)
```
```1955:1961:lua_table_editor.py
        if key == Qt.Key_Backspace or key == Qt.Key_Delete:
```
**复现**：窗口激活后按 Delete → 弹出"确认删除 N 行"对话框（计数 1 次），单元格未被清空。菜单 `QAction` 的 `WindowShortcut` 优先于视图 `keyPressEvent`，视图里的 Delete 分支是**死代码**。

**备注**：`Backspace` 分支正常（实测可清空）。`README.md`/帮助文档同时写了"Delete 删除选中行"和"Backspace/Delete 清空单元格"，需一并统一。

**验收**：Delete 与 Backspace 职责明确、文档与实现一致、无重复绑定。

---

#### BUG-09【实证｜高】自动刷新开关不持久化，且无法长期关闭
**位置**：`3656:3662`（`_toggle_auto_refresh`）、`5978:5979`（`closeEvent` 只保存工作区与备注）
**复现**：`QSettings.allKeys()` 实际为 `['fileNotes','recentFiles','workspaceFolders']`，无 autoRefresh。
**影响面**：每次启动强制开启 30s 轮询（大表持续磁盘+CPU），用户无法永久关闭。
**验收**：开关状态持久化并跨启动生效。

---

#### BUG-10【实证｜高】筛选 + 清空单元格：类型判断用错模型的索引
**位置**
```1956:1959:lua_table_editor.py
            if isinstance(src, LuaTableModel) and src.flags(idx) & Qt.ItemIsEditable:
                proxy_idx = self.model().index(idx.row(), idx.column()) \
                    if isinstance(self.model(), QSortFilterProxyModel) else idx
```
把 **proxy 索引**直接喂给 **source 模型**的 `flags()`：筛选后 proxy 行 0 可能对应 source 行 5。若 source 行 0 是头注释行 → 清空被错误拦截；反之可能放过应拒绝的行。写入本身走 `proxy_idx` 是正确的，问题仅在守卫判定。
**验收**：判定与写入使用同一坐标系。

---

#### BUG-11【高】新增的空数据行保存时被丢弃，且新增行不可撤销
**位置**：`613:619`（`if not parts: continue`）、`1300:1313`（`add_data_row` 未记 undo）
**影响面**：批量加行后未填值即保存，行数"变少"；`Ctrl+Z` 无法回退新增行。
**验收**：新增行要么被保存（即使是 `{}` 或保留行键），要么在保存前明确提示将被丢弃。

---

#### BUG-12【中】`_paste_*` 系列在结构性行参与时的位置漂移
**位置**：`4742:4748`、`4871:4877`、`4994:4999`。用 `selectedRows()` 取行，若选中头注释行，`remove_row` 拒绝删除但 `insert_at = min(src_rows)` 仍按该行号插入 → 漂移。
**验收**：粘贴前先把选区收敛到合法数据行集合。

---

#### BUG-13【中】后台回调打到已关闭的标签页
**位置**：`4421:4448`（`_reload_tab.on_loaded` 直接使用捕获的 `td`）、`4023`（`QTimer.singleShot(150, ...)` 自动调列宽）
**影响面**：加载/延迟回调期间关闭标签页 → `RuntimeError`（已删除的 Qt 对象）或被吞掉。
**验收**：回调入口校验对象存活（tab 是否仍在 `_tabs_data`）。

---

### P1-B 一致性/体验类（可与 P1-A 同批修）

| 编号 | 严重度 | 位置 | 问题 | 影响 |
|---|---|---|---|---|
| BUG-14【实证】 | 中 | `2452:2457` `5600:5605` | `FindDialog` 的"区分大小写""搜索所有列"复选框从未传入 `perform_search`，纯装饰 | 用户误以为生效 |
| BUG-15 | 中 | `5572` | 详情面板取 `selectedIndexes()[0]` 而非 `currentIndex()` | 多选时显示非焦点格；行号口径与"#"列不一致 |
| BUG-16 | 中 | `5987` | 状态栏 `共 N 行` 用 `len(data.rows)`，不含头注释/return | 与表格实际行数不符 |
| BUG-17 | 中 | `4201:4204` | 切换标签页不重置 `_filter_status` 永久标签 | 显示上一张表的筛选条件 |
| BUG-18 | 中 | `4177:4199`、`3992` | `_update_tab_title` 只改"当前"标签页；`td['display_title']`（含目录）算完从不使用 | 后台标签页标题不刷新；半成品逻辑 |
| BUG-19 | 中 | `5047:5056` | 删除确认的 N 行计数包含不可删的头注释/return 行 | 提示与实际删除数不符 |
| BUG-20 | 中 | `5317:5318` | 取消注释批量执行时"部分成功"静默 | 用户不知哪些行没恢复 |
| BUG-21 | 中 | `377:384` | `_get_brace_content` 用 `find('{')`+`rfind('}')`，字符串内含 `}` 或行尾 `-- }` 时区间错误 | 字段/取值解析错位 |
| BUG-22 | 中 | `386:425` | `_split_by_comma` 不支持 `[[长字符串]]`、`(a, b)` 内逗号、`--[[ ]]` 块注释 | 复杂值解析失败 |
| BUG-23 | 中 | `800:838` | 重复值按 `(列, 序列化值)` 统计，无类型区分 | 多行共享同值（如 `Type=18`）全表橙红误报 |
| BUG-24 | 中 | `1290:1298` | `_next_default_row_key` 只认数字键；不检测键冲突 | 混合键类型混乱；与 BUG-25 叠加会产生重复键 |
| BUG-25 | 中 | `5080:5086`、`5133:5141` | `_repair_row_keys` / `_batch_sync_row_keys` 不校验键唯一性 | 生成重复 `[key]`，Lua 侧静默覆盖 ⇒ 数据正确性风险 |
| BUG-26 | 中 | 多处 | 保存非原子（直接覆盖），无临时文件+重命名、无备份 | 中断即文件损坏 |
| BUG-27 | 低 | `5562:5588` | 空表无默认列，`_new_table` 生成 `field_names=[]`，不先加列则界面无内容 | 新建表几乎不可用 |
| BUG-28 | 低 | `4212:4221` | 新建表 `modified=False`，未编辑即关闭无提示 | 极小概率困惑 |
| BUG-29 | 低 | `5935:5949` | 拖放多文件只开第一个；`.LUA` 大写后缀被拒 | 批量拖入失效 |
| BUG-30 | 低 | `2854:2899` | 工作区递归 `os.listdir` 全量预展开、无软链接环保护、无自然排序 | 大目录启动阻塞 / 潜在无限递归 |
| BUG-31 | 低 | `2051:2127` | 每次 freeze/unfreeze 重建整侧 overlay，先取消全部隐藏再重设 | 列多时明显卡顿 |
| BUG-32 | 低 | `4326:4328` | `_on_worker_done` 未 `deleteLater()`；无加载取消 | 线程对象泄漏；大文件期间 UI 只能等待 |

---

### P1-C 纯性能（可选，但会直接影响"大表是否可用"）

| 编号 | 位置 | 问题 | 量级 |
|---|---|---|---|
| PERF-01 | `885:886` | 行号列每格做前缀求和 `sum(1 for e in rows[:i+1])` | **O(N²)**，万行表全量重绘数千万次迭代 |
| PERF-02 | `1061:1065` | 每次 `setData` 全列 `dataChanged` + `_rebuild_duplicates()` 全表 O(N×M) 重算 | 每次按键触发 |
| PERF-03 | `1286:1298` | 每次插入行全表扫描求最大键 | 批量插入放大为 O(n·N) |
| PERF-04 | `359:363` | `lru_cache(maxsize=8192)` 缓存**整行原文**字符串 | 内存驻留偏高 |
| PERF-05 | `1315:1329` | `add_data_rows_batch` 已实现但**从未被调用**（`_do_add_rows` 用循环 `add_data_row`） | 白白损失批量插入优化 |

---

## 4. 代码隐患（P2 清理清单）

### 4.1 结构性缺口

1. **不可撤销的操作**：增删行、增删列、粘贴、填充新增行、批量同步行键、整列删除。且 `_add_column` / `_delete_column` 调用 `model.set_data()` 会**清空 undo 栈**（历史编辑一并不可撤销）。
2. **`MainWindow` 上帝对象**（3542–6002）：文件/编辑/剪贴板/行键/查找/筛选/工作区/状态栏全在一个类；`eventFilter` 内混合 undo/redo 与 Enter/Tab 导航。
3. **重复实现**：填充柄绘制在 `LuaTableDelegate._draw_fill_handle`（1716）与 `LuaTableView.draw_fill_handle`+`paintEvent`（2394、2416）各一份；冻结列宽度计算在 `_frozen_width`/`_frozen_side_width`/`_layout_frozen` 重复；**"逻辑行→数据行"转换在 10+ 处手工重写**（正是 BUG-01 的根因）。

### 4.2 死代码与冗余

| 项 | 位置 |
|---|---|
| `add_data_rows_batch` 从未被调用 | `1315:1329` |
| `_setup_shortcuts` 空实现 | `3914:3915` |
| `_on_header_clicked` 仅 `pass` | `5651:5653` |
| 未使用导入：`QComboBox, QUrl, QSizePolicy, QTextOption, QDesktopServices, QIcon, QPalette, QFontMetrics, Slot` | `20:36` |
| **`QDialogButtonBox` 重复导入两次** | `21` 与 `25` |
| 函数内 `import copy` × 5 | `1147, 1218, 1282, 5225, 5269` |

### 4.3 可读性

- **命名冲突**：模型属性 `_data` vs 方法 `data()`；局部变量 `re`（`830` 等）**遮蔽 `import re`**；同一语义混用类常量 `ROW_DATA` 与字面量 `'data'/'comment'/'blank'`（`'blank'` 甚至没有常量）。
- **魔法值散落**：颜色（`#bfe3ff`/`#ffd9b3`/`#c0392b`/`#f0e8f5`…）、缩进 `'\t'`、`MAX_CHARS=40`、30s 间隔、8 条最近文件、3px 色条，无集中常量区 —— **这是 P2 抽取样式常量、也是 P4 美化的前置条件**。
- **静默吞异常**：`5236:5237`、`5280:5281`（`except Exception: pass`）、`1110:1112`、`5312:5315`。
- **注释与实现漂移**：`_parse_kv_pairs` 注释"先去掉行首的 key = 前缀"与实现不符；`README.md` 的 Delete 语义/快捷键表/打包说明（`--add-data requirements.txt` 无意义）均与实现不一致；`AI_TECHNICAL_DOC.md` 行号与行数（称 4932 行，实际 6073 行）已过期。

### 4.4 工程化

- **无有效测试**：`_test_rowkey.py` 用 PyQt5 假模块（项目已是 PySide6，mock 无意义）；无 pytest/断言/CI。
- **依赖无上界**：`PySide6>=6.5.0`；无 `pyproject.toml`。
- **打包不完整**：无图标/版本号/`--noconfirm`/隐藏导入。
- **无日志**：全局异常钩子只弹 `msg[:500]` 或写 stderr，用户报错无法回溯。

---

## 5. 缺失功能（P3 候选，按建议优先级）

| 编号 | 缺失项 | 说明与影响 |
|---|---|---|
| MISSING-01 | **排序** | `setSortingEnabled(False)`，代理未实现 `lessThan`；无单列/多列/按行键排序。策划核对数据的核心诉求 |
| MISSING-02 | **行键唯一性校验** | 与 BUG-25 同源。`[key]` 表重复键在 Lua 中静默覆盖 |
| MISSING-03 | **查找替换** | 只有定位，无替换/正则替换 |
| MISSING-04 | **导入导出** | 无 CSV/Excel 导入导出、无 Lua↔JSON 转换 |
| MISSING-05 | **编码兼容** | 硬编码 `encoding='utf-8'`；无 GBK/GB18030 回退、无 BOM 处理（与 BUG-04 同源） |
| MISSING-06 | **多表文件** | 一个 `.lua` 多张表只编辑第一张，其余整段作为 `footer_raw` 保存，界面不可见 |
| MISSING-07 | **撤销覆盖结构操作** | 见 4.1 |
| MISSING-08 | **保存安全** | 无原子写入/备份/崩溃恢复/自动保存 |
| MISSING-09 | **列操作不全** | 无列重命名、列排序、插入到指定位置、字段类型/格式定义 |
| MISSING-10 | **偏好设置** | 无设置面板；自动刷新间隔固定 30s；窗口几何/分割条/上次标签页不记忆（与 BUG-09 同源） |
| MISSING-11 | **搜索高亮/结果列表** | 仅 `selectRow`，不标出匹配文本 |
| MISSING-12 | **数据字典/约束** | 无字段注释、无类型约束、无枚举下拉、无外键完整性检查 |
| MISSING-13 | **日志** | 无日志文件 |
| MISSING-14 | **测试/CI** | 见 4.4 |
| MISSING-15 | **国际化** | 全量硬编码中文 |
| MISSING-16 | **多文件拖放** | 只开第一个；拒绝 `.LUA` 大写 |

---

## 6. 验证方法（必须照做，否则无法证明修复有效）

本项目**没有测试框架**。请使用 PySide6 的 `offscreen` 平台写 headless 探针，输出到 UTF-8 文件（Windows 终端会把中文显示成乱码，**不要**直接读 stdout）。

**探针骨架（每次修复至少覆盖对应条目）**

```python
import os, sys, io, traceback
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, '.')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication([])
from lua_table_editor import LuaParser, LuaSerializer, LuaTableModel, FilterProxyModel

OUT = io.StringIO()
P = lambda *a: print(*a, file=OUT)

# 关键夹具：必须包含「表头注释 + return」，ClothData.lua 覆盖不到
FIXTURE = """--
-- X.xlsx
--
local data =
{
    [1] = {id=1, Name="a"},
    [2] = {id=2, Name="b"},
    [3] = {id=3, Name="c"},
}
return data"""

try:
    d = LuaParser.parse(FIXTURE)
    m = LuaTableModel(); m.set_data(d)
    P("header_comments =", len(d.header_comments), " rowCount =", m.rowCount())
    assert m._resolve_row(len(d.header_comments))[1] == 0, "逻辑行映射错误"
    p = FilterProxyModel(); p.setSourceModel(m)
    p.set_filter(1, 'b', 'contains')          # 修复前：IndexError
    assert p.filterAcceptsRow(len(d.header_comments), m.index(0, 0)) is True
except Exception:
    P(traceback.format_exc())

open('_probe_out.txt', 'w', encoding='utf-8').write(OUT.getvalue())
print("done")
```

**每批修复必跑的最小场景矩阵**

| 场景 | 夹具 | 覆盖条目 |
|---|---|---|
| 表头注释 + return | `FIXTURE` 上表 | BUG-01/12/16 |
| 无表头注释 + 无 return | `biaodan/ClothData.lua`（只读加载，勿保存） | 回归：不得退化 |
| CRLF 往返 | `"T = {\r\n    {id=1},\r\n}\r\n"` 写盘后比对字节 | BUG-05 |
| BOM 往返 | `"\ufefflocal data =\n{\n    {id=1},\n}"` | BUG-04 |
| 值往返 | `0x1F` / `007` / `1e3` / `a\nb`(字面反斜杠) / `{build}` / 超长中文串 | BUG-07 |
| 行键 | `my_key` / `1` / `'q'` / 空 | BUG-07a |
| modified 生命周期 | 加行→改格→撤销→再改→保存 | BUG-02 |
| 空行 | 表体内夹空行 | BUG-03 |
| 插入位置 | H=4 表 + 无选中 + 有选中 + 筛选中 | BUG-06 |
| CRLF 文件保存两次 | 连续保存，字节必须稳定（幂等） | BUG-05 回归 |

**幂等性铁律**：`parse → serialize → write → read → parse → serialize` 第二轮输出必须与第一轮**字节完全相同**。这条能一次性抓住绝大多数序列化缺陷。

---

## 7. 禁止清单（违反即视为任务失败）

1. **不要用 `write_to_file` 整体重写 `lua_table_editor.py`**（6073 行）。用 `replace_in_file` 做定点修改。
2. **不要改变输出的字节格式**：行尾、缩进（`'\t'`）、`[key] = {` 的空格、尾随 `, }`、声明行/结束行/尾部的原文。
3. **不要在 `data.rows` 上使用逻辑行号**（INV-1）。
4. **不要为了"顺手修好"而扩大改动面**：P1 批次只动逻辑，不动视觉；发现新问题写进本文件的 §3/§4，不要就地改。
5. **不要新增第三方依赖**（当前仅 PySide6；`requirements.txt` 不引入上界封锁之外的包）。
6. **不要引入 emoji 到代码/注释/日志**（现有 UI 文案里的 emoji 保持原样，不要扩散）。
7. **不要删除** `.codebuddy/`、`biaodan/`、`_test_rowkey.py`、`_gui_flow_test.py`（后两者保留供参考，是否改造由使用者决定）。
8. **不要提交** `biaodan/`、`build/`、`dist/`、`*.spec`（已在 `.gitignore`）。
9. **不要在没有探针证据的情况下宣称修复完成**。每批提交信息需包含"复现命令 + 修复前后输出"。
10. **P4（美化）不得修改**：`LuaParser`、`LuaSerializer`、`LuaTableModel` 的索引与 undo 逻辑、`_do_save`、`_paste_*`、`_resolve_row`、`_logical_to_data_row`。

---

## 8. 分批执行计划（建议 commit 粒度）

### P1-A：数据安全（6 个 commit，每个 = 1 缺陷 + 1 探针）
1. BUG-01 索引契约统一（**先做这个**，它是 BUG-12/15/19 的共同根因）
2. BUG-02 `modified` 语义
3. BUG-04 BOM + BUG-05 CRLF（同属"文件字节"主题，合并）
4. BUG-03 空行
5. BUG-07 序列化往返
6. BUG-06 插入位置

### P1-B：一致性/体验（可合并为 2–3 个 commit）
BUG-08、09、10、11 优先；其余（14–32）按影响排序批量处理。

### P1-C：性能（单独 commit，需附"万行表渲染耗时"前后对比）
PERF-01 → PERF-05 → PERF-02 → PERF-03/04。

### P2：可维护性（行为零变更，每个 commit 后跑 §6 全场景矩阵）
1. 抽取颜色/尺寸/间隔常量表（**P4 的前置条件**）
2. 收敛索引转换到统一助手函数
3. 删除死代码（4.2 表）、清理未使用导入与重复导入
4. 统一行类型常量（为 `'blank'` 补常量）
5. （可选，风险高，建议单独开分支）拆分模块

### P3：功能补齐
MISSING-02（行键唯一性，与 BUG-25 同批）→ MISSING-01（排序）→ MISSING-05（编码）→ MISSING-08（原子保存+自动备份）→ 其余。

### P4：UI 美化（**必须在 P2 的样式常量抽取完成后启动**）

**可美化清单（供 P4 模型使用）**

| 目标 | 位置 | 备注 |
|---|---|---|
| 全局 QSS 统一 | `6040:6063`（`main()` 里的 `app.setStyleSheet`）、各 `setStyleSheet` 调用点 | 目前样式分散在 20+ 处 |
| 表格单元格配色/斑马纹/选中/高亮/重复值 | `1628:1651`（`LuaTableDelegate.paint`） | 逻辑行配色分叉，注意别破坏重复值语义色 |
| 注释行/return 行/头注释行渲染 | `1511:1592` | 已有色板：头注释 `#f5f5ee/#006600`、内联 `#f0f0e8/#666666`、return `#f0e8f5/#800080` |
| 填充柄样式 | `1716:1756`、`2394:2414` | 两处重复实现（P2 应先合并） |
| 冻结列 overlay 边框 | `2096:2098` | 左右各一条 2px 蓝灰分割线 |
| 工具栏/菜单/状态栏 | `3669:3674`、`3776:3803`、`6044:6063` | |
| 标签页（含工作区色条） | `3724:3745`、`3926:3933` | |
| 欢迎页 | `2622:2694` | 含大量内联 QSS |
| 详情面板 | `2562:2586` | |
| 对话框（查找/帮助/关于） | `2431+`、`3212+`、`3470+` | 帮助页 HTML/CSS 可直接升级 |
| 图标 | 全线使用 emoji 占位（📂💾🔄➕💬➖📋📁🔍❓📌✖） | 建议换 QIcon 或统一图标主题 |

**P4 与 P1/P3 的隔离要求**：只允许改 QSS 字符串、颜色常量、`paint()` 内的绘制样式、尺寸/间距；**不得**触碰任何 `data.rows` 访问、`setData`、`serialize`、`_do_save`、索引映射。P4 完成后必须回归 §6 全场景矩阵。

---

## 9. 给下一个模型的建议工作流

1. 先读 §0 边界 → §2 不变量 → §3 缺陷清单。**不要**先通读 6073 行代码（低效）。
2. 用 §1 的分区表定位；用 `search_content` 找具体符号。
3. 修一条 → 写一条探针 → 跑 §6 矩阵 → 再修下一条。
4. 发现的**新问题**追加进本文件对应章节，不要顺手改。
5. 每批结束后更新本文档的"已完成"标记（可在每条 BUG 编号后追加 `[已修: <commit>]`）。
6. 若必须偏离 §7 禁止清单，先在回复中说明理由并等待确认。

---

## 附：本文档的可信度标注

- 标 **【实证】** 的条目：由审查阶段实际运行 headless 探针复现，输出可复核（探针已清理，需按 §6 重建）。
- 未标注的条目：由代码静态分析推断（含行号），**置信度高但未运行验证**，修复前应先复现。
- 引用行号基于审查时的 `lua_table_editor.py`（6073 行，分支 `feature/rowkey-sync-load-refresh`）。若代码已变动，以符号名检索为准。
