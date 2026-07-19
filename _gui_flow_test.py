"""精确模拟 GUI 完整流程：加载→显示→编辑→保存"""
import sys
sys.path.insert(0, '.')
from lua_table_editor import LuaParser, LuaSerializer

# 模拟原始 MessageData.lua 中的一行（含 \n）
lua_line_with_n = '''local data =
{
\t[1100008] = {id=1100008, MessageName="确定要替换伙伴服装吗？\\n记得给伙伴换上新衣服哦", Message="替换伙伴服装吗？\\n记得给伙伴换上"},
}
return data'''

print("=== 1. 原始文件内容 ===")
print(repr(lua_line_with_n))

# Step 1: 解析
data = LuaParser.parse(lua_line_with_n)
row = data.rows[0]
val_in_memory = row.data.get('MessageName', '')

print("\n=== 2. 解析后内存中的值 ===")
print(f"  repr: {repr(val_in_memory)}")
print(f"  has real newline(0x0a): {chr(10) in val_in_memory}")
print(f"  has literal backslash-n: {chr(92)+'n' in val_in_memory}")

# Step 3: DisplayRole (GUI 显示)
display_val = LuaSerializer._value_to_lua(val_in_memory)
print(f"\n=== 3. DisplayRole (GUI display) ===")
print(f"  repr: {repr(display_val)}")

# Step 4: EditRole
edit_val = val_in_memory
print(f"\n=== 4. EditRole (to editor) ===")
print(f"  repr: {repr(edit_val)}")

# Step 5: setData → _parse_value
parsed_back = LuaParser._parse_value(str(edit_val))
print(f"\n=== 5. _parse_value re-parse ===")
print(f"  repr: {repr(parsed_back)}")
print(f"  same as original? {parsed_back == val_in_memory}")

# Step 6: serialize output
row.data['MessageName'] = parsed_back  # simulate setData storing it back
serialized = LuaSerializer.serialize(data)
print(f"\n=== 6. Serialize output ===")
for line in serialized.split('\n'):
    if '1100008' in line:
        print(f"  {repr(line)}")
        
# Also test the critical case: what if original file already has \\n (literal)?
print("\n\n" + "="*60)
print("TEST CASE 2: Original file has LITERAL \\n (not escape)")
print("="*60)
lua_literal_n = '''local data =
{
\t[1100008] = {id=1100008, MessageName="确定要替换伙伴服装吗？\\\\n记得给伙伴换上新衣服哦", Message="test"},
}
return data'''
print(f"Input: {repr(lua_literal_n)}")

data2 = LuaParser.parse(lua_literal_n)
val2 = data2.rows[0].data.get('MessageName', '')
print(f"Parsed value: {repr(val2)}")

ser2 = LuaSerializer.serialize(data2)
for line in ser2.split('\n'):
    if '1100008' in line:
        print(f"Output: {repr(line)}")
        # count backslashes before n
        idx = line.find('n')
        if idx > 0:
            context = line[max(0,idx-5):idx+5]
            print(f"  Context around 'n': {repr(context)}")
