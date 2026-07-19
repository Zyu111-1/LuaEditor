"""
Test: Parse Lua file with [1]=, [2]=, ..., [11]= and [7202]=, [7302]= entries,
then serialize back, check if row_key is preserved.
"""
import sys
sys.path.insert(0, '.')

# Minimal mock for PyQt5 so we can import the module
import types
mock_qt = types.ModuleType('PyQt5')
mock_qt_core = types.ModuleType('PyQt5.QtCore')
mock_qt_gui = types.ModuleType('PyQt5.QtGui')
mock_qt_widgets = types.ModuleType('PyQt5.QtWidgets')

class FakeQObject:
    pass
class FakeQWidget:
    pass
class FakeQModelIndex:
    pass
class FakeQByteArray:
    pass

# Create mock Qt module
sys.modules['PyQt5'] = mock_qt
sys.modules['PyQt5.QtCore'] = mock_qt_core
sys.modules['PyQt5.QtGui'] = mock_qt_gui
sys.modules['PyQt5.QtWidgets'] = mock_qt_widgets

mock_qt_core.QObject = FakeQObject
mock_qt_core.QMimeData = FakeQObject
mock_qt_core.Qt = types.ModuleType('Qt')
mock_qt_core.Qt.DisplayRole = 0
mock_qt_core.Qt.BackgroundRole = 1
mock_qt_core.Qt.ForegroundRole = 2
mock_qt_core.Qt.AlignCenter = 0
mock_qt_core.Qt.ItemIsEditable = 1
mock_qt_core.Qt.NoItemFlags = 0
mock_qt_core.QAbstractTableModel = FakeQObject
mock_qt_core.QModelIndex = FakeQModelIndex
mock_qt_core.QByteArray = FakeQByteArray
mock_qt_core.QSortFilterProxyModel = FakeQObject
mock_qt_core.QVariant = FakeQObject
mock_qt_gui.QColor = FakeQObject
mock_qt_gui.QFont = FakeQObject
mock_qt_gui.QBrush = FakeQObject
mock_qt_gui.QKeySequence = FakeQObject
mock_qt_widgets.QApplication = FakeQObject
mock_qt_widgets.QAbstractItemView = FakeQObject
mock_qt_widgets.QHeaderView = FakeQObject
mock_qt_widgets.QStyledItemDelegate = FakeQObject

# Patch undo command classes (imported in module)
sys.modules['PyQt5.QtWidgets'].QUndoCommand = FakeQObject

from lua_table_editor import LuaParser, LuaSerializer

# Test file with [1]= through [11]= and [7202]=, [7302]=
test_lua = """--
-- ChangeClothData.xlsx
--
local data =
{

    [1] = {id=1, NameMultiLanguage=161255, LabelLevel=1},
    [2] = {id=2, NameMultiLanguage=161256, LabelLevel=1},
    [3] = {id=3, NameMultiLanguage=161257, LabelLevel=1},
    [4] = {id=4, NameMultiLanguage=161258, LabelLevel=1},
    [5] = {id=5, NameMultiLanguage=162630, LabelLevel=1},
    [6] = {id=6, NameMultiLanguage=162631, LabelLevel=1},
    [7] = {id=7, NameMultiLanguage=162628, LabelLevel=1},
    [8] = {id=8, NameMultiLanguage=162629, LabelLevel=1},
    [9] = {id=9, NameMultiLanguage=162762, LabelLevel=1},
    [10] = {id=10, NameMultiLanguage=162763, LabelLevel=1},
    [11] = {id=11, NameMultiLanguage=161259, LabelLevel=1},
    [101] = {id=101, NameMultiLanguage=161260, LabelLevel=2},
    {id=7202, NameMultiLanguage=161262, LabelLevel=2},
    {id=7302, NameMultiLanguage=161266, LabelLevel=2},
}
return data"""

print("=== INPUT ===")
print(test_lua)
print()

data = LuaParser.parse(test_lua)

print("=== PARSED ROW_KEYS ===")
for i, row in enumerate(data.rows):
    if row.row_type == 'data':
        print(f"  data.rows[{i}]: row_key='{row.row_key}', id={row.data.get('id', 'N/A')}")

print()
print("=== SERIALIZED OUTPUT ===")
output = LuaSerializer.serialize(data)
print(output)

print()
print("=== ANALYSIS ===")
# Check which rows lost their row_key
output_lines = output.split('\n')
missing_keys = []
for i, row in enumerate(data.rows):
    if row.row_type == 'data' and row.row_key:
        if row.data.get('id') is not None:
            # Check if [row_key] = appears in output
            key_str = f"[{row.row_key}] ="
            found = any(key_str in line for line in output_lines)
            if not found:
                missing_keys.append(f"  MISSING: row_key='{row.row_key}', id={row.data['id']}")

if missing_keys:
    print("ROWS WITH LOST ROW_KEY:")
    for m in missing_keys:
        print(m)
else:
    print("All row_keys preserved correctly!")

# Also check: do 7202 and 7302 have row_key?
print()
for i, row in enumerate(data.rows):
    if row.row_type == 'data' and row.data.get('id') in (7202, 7302):
        print(f"  id={row.data['id']}: row_key='{row.row_key}' (should be '{row.data['id']}')")
