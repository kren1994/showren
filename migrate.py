#!/usr/bin/env python3
"""
連珠検討ボード JSONフォーマット変換スクリプト
旧フォーマット → 新フォーマット（フィールド短縮 + posDb キー base64url 化）

使い方:
    python migrate.py input.json output.json
"""

import json
import sys
import base64


BOARD_SIZE = 15


# ---- カノニカルキー変換 ------------------------------------------------

def transform(cx, cy, i):
    last = BOARD_SIZE - 1
    match i:
        case 0: return cx, cy
        case 1: return cy, last - cx
        case 2: return last - cx, last - cy
        case 3: return last - cy, cx
        case 4: return last - cx, cy
        case 5: return cx, last - cy
        case 6: return cy, cx
        case 7: return last - cy, last - cx


def encode_board(s: str, color: int) -> str:
    """225トリットを5トリット/バイトで45バイトに詰めてbase64url化 → 61文字"""
    digits = [int(c) for c in s]
    data = bytearray(45)
    for i in range(45):
        v = 0
        for j in range(5):
            v = v * 3 + digits[i * 5 + j]
        data[i] = v
    b64 = base64.b64encode(bytes(data)).decode()
    b64url = b64.replace('+', '-').replace('/', '_').replace('=', '')
    return b64url + str(color)


def old_key_to_new(old_key: str) -> str:
    """
    旧キー: 225文字の'0'/'1'/'2'文字列 + '_' + 手番色
    新キー: base64url 60文字 + 手番色1文字
    """
    board_str, color_str = old_key.rsplit('_', 1)
    return encode_board(board_str, int(color_str))


# ---- ツリーノード変換 --------------------------------------------------

NODE_FIELD_MAP = {
    'id':               'i',
    'parent':           'p',
    'children':         'c',
    'color':            'o',
    'moveNumber':       'm',
    'lastSelectedChild':'l',
    # x, y はそのまま
}


def convert_node_id(nid):
    """'root' → 'r'、'node_N' → 'N'"""
    if nid is None:
        return None
    if nid == 'root':
        return 'r'
    if nid.startswith('node_'):
        return nid[len('node_'):]
    return nid  # すでに新形式


def convert_node(old_node: dict) -> dict:
    new_node = {}
    for old_field, new_field in NODE_FIELD_MAP.items():
        if old_field in old_node:
            new_node[new_field] = old_node[old_field]
    # x, y はそのまま
    for f in ('x', 'y'):
        if f in old_node:
            new_node[f] = old_node[f]

    # IDの変換
    if 'i' in new_node:
        new_node['i'] = convert_node_id(new_node['i'])
    if 'p' in new_node:
        new_node['p'] = convert_node_id(new_node['p'])
    if 'c' in new_node:
        new_node['c'] = [convert_node_id(cid) for cid in new_node['c']]
    if 'l' in new_node:
        new_node['l'] = convert_node_id(new_node['l'])

    return new_node


# ---- posDb エントリ変換 -----------------------------------------------

POSDB_FIELD_MAP = {
    'comment':   'c',
    'labels':    'l',
    'nextMoves': 'n',
}


def convert_posdb_entry(old_entry: dict) -> dict:
    new_entry = {}
    for old_f, new_f in POSDB_FIELD_MAP.items():
        if old_f in old_entry:
            new_entry[new_f] = old_entry[old_f]

    # nextMoves の値 true → 1
    if 'n' in new_entry:
        new_entry['n'] = {k: 1 for k in new_entry['n']}

    return new_entry


def is_old_key(key: str) -> bool:
    """旧キー判定: '_' を含む225+2文字形式"""
    return '_' in key and len(key) == 227


# ---- メイン変換 -------------------------------------------------------

def migrate(data: dict) -> dict:
    # トップレベルキー判定
    is_old_top = 'tree' in data

    if is_old_top:
        old_tree      = data['tree']
        old_posdb     = data.get('posDb', {})
        old_current   = data['currentNodeId']
        old_next      = data['nextNodeId']
    else:
        old_tree      = data['t']
        old_posdb     = data.get('p', {})
        old_current   = data['c']
        old_next      = data['n']

    # ツリー変換
    new_tree = {}
    for nid, node in old_tree.items():
        new_nid = convert_node_id(nid)
        new_tree[new_nid] = convert_node(node)

    # posDb 変換
    new_posdb = {}
    for key, entry in old_posdb.items():
        new_key = old_key_to_new(key) if is_old_key(key) else key
        new_posdb[new_key] = convert_posdb_entry(entry) if is_old_top else entry

    return {
        't': new_tree,
        'p': new_posdb,
        'c': convert_node_id(old_current),
        'n': old_next,
    }


# ---- CLI --------------------------------------------------------------

def main():
    if len(sys.argv) != 3:
        print(f'使い方: python {sys.argv[0]} input.json output.json')
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]

    with open(in_path, encoding='utf-8') as f:
        data = json.load(f)

    new_data = migrate(data)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(new_data, f, ensure_ascii=False, separators=(',', ':'))

    # サイズ比較
    import os
    old_size = os.path.getsize(in_path)
    new_size = os.path.getsize(out_path)
    print(f'変換完了: {in_path} ({old_size:,} bytes) → {out_path} ({new_size:,} bytes)')
    print(f'削減率: {(1 - new_size / old_size) * 100:.1f}%')


if __name__ == '__main__':
    main()
