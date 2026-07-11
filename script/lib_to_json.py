#!/usr/bin/env python3
"""
RenLib（.lib）ファイルを showren の JSON 形式へ変換する。

- 木構造・各手のコメント・手のラベル（盤上テキスト）を再現する
- 局面ラベル（pl）は付けない
- 文字コードは自動判定（utf-8 → cp932 → gb18030 を厳密デコードで試行）。
  --encoding で明示指定もできる

.lib のバイナリ仕様は Renjulibviewer_v2 (src/io/parser.rs) を参考にした:
    ヘッダ 20 バイト（FF 'RenLib' FF + バージョン + 予約）
    以降、DFS 順に 1 手 = [座標 1B][フラグ 1B][(テキスト時パディング 2B)]
                     [(コメント: NUL 終端 + 2B 境界揃え)][(テキスト: 同左)]
    座標 0x00 は手なし（仮想ルート）。x = (b & 0x0f) - 1, y = b >> 4。
    フラグ: 0x01=テキストあり 0x08=コメントあり 0x40=子なし 0x80=兄弟あり

使い方:
    python lib_to_json.py Guide.濤.20230921.lib -o guide.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from rif_to_json import (
    BOARD_SIZE, CELLS, _INVS, inverse_transform, canonical_from_mirrors, open_out,
)

MASK_TEXT = 0x01
MASK_COMMENT = 0x08
MASK_NOCHILD = 0x40
MASK_SIBLING = 0x80

MAGIC = b'\xffRenLib\xff'
HEADER_SIZE = 20
# 改行・タブ以外の C0 制御文字と DEL（表示すると豆腐になるだけ）
_CONTROL_CHARS = re.compile(r'[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]')


# ---- .lib の読み込み（中間表現の木を作る）------------------------------

class LibNode:
    __slots__ = ('x', 'y', 'comment', 'text', 'children')

    def __init__(self, x: int, y: int, comment: bytes, text: bytes) -> None:
        self.x = x
        self.y = y
        self.comment = comment          # 生バイト（デコードは後段でまとめて行う）
        self.text = text
        self.children: list[LibNode] = []


def _read_entry(data: bytes, offset: int) -> tuple[LibNode, bool, bool, int] | None:
    """1 手分を読み、(ノード, 子あり, 兄弟あり, 次オフセット) を返す。"""
    if offset + 1 >= len(data):
        return None
    move_byte = data[offset]
    flag = data[offset + 1]
    offset += 2

    if move_byte == 0:
        x = y = -1  # 手なし（仮想ルートなど）
    else:
        x = (move_byte & 0x0f) - 1
        y = move_byte >> 4

    if flag & MASK_TEXT:
        offset += 2  # テキストありのときは 2 バイトのパディングが入る

    def read_string(off: int) -> tuple[bytes, int]:
        start = off
        end = data.find(b'\x00', off)
        if end == -1:
            end = len(data)
        raw = data[start:end]
        off = min(end + 1, len(data))
        if (len(raw) + 1) % 2:  # 2 バイト境界に揃える
            off = min(off + 1, len(data))
        return raw, off

    comment = b''
    text = b''
    if flag & MASK_COMMENT:
        comment, offset = read_string(offset)
    if flag & MASK_TEXT:
        text, offset = read_string(offset)

    node = LibNode(x, y, comment, text)
    return node, not (flag & MASK_NOCHILD), bool(flag & MASK_SIBLING), offset


def load_lib(path: str) -> LibNode:
    """DFS 直列化を復元し、仮想ルート（x=-1）を頂点とする木を返す。"""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < HEADER_SIZE or not data.startswith(MAGIC):
        raise SystemExit('RenLib ファイルではありません（マジックバイト不一致）')
    version = (data[8], data[9])
    if version[0] != 3:
        print(f'  警告: 未検証のバージョン {version[0]}.{version[1]} です', file=sys.stderr)

    offset = HEADER_SIZE
    first = _read_entry(data, offset)
    if first is None:
        raise SystemExit('.lib に棋譜データがありません')
    node, has_child, _has_sibling, offset = first

    # 先頭が手なしノードなら、それを木のルートにする（コメントも保持される）。
    # 先頭がいきなり着手のこともあるため、その場合は空のルートを作って繋ぐ。
    if node.x < 0:
        root = node
        if not has_child:
            return root
        entry = _read_entry(data, offset)
        if entry is None:
            return root
        node, has_child, has_sibling, offset = entry
    else:
        root = LibNode(-1, -1, b'', b'')
        has_sibling = _has_sibling

    root.children.append(node)

    # parser.rs と同じ明示スタックの DFS。
    # 行き(stage 0)で子を読み、帰り(stage 1)で兄弟を読む。
    stack: list[list] = [[node, root, 0, has_child, has_sibling]]
    while stack:
        frame = stack[-1]
        current, parent, stage, has_child, has_sibling = frame
        if stage == 0:
            frame[2] = 1
            if has_child:
                entry = _read_entry(data, offset)
                if entry is not None:
                    child, c_child, c_sibling, offset = entry
                    current.children.append(child)
                    stack.append([child, current, 0, c_child, c_sibling])
        else:
            stack.pop()
            if has_sibling:
                entry = _read_entry(data, offset)
                if entry is not None:
                    sibling, s_child, s_sibling, offset = entry
                    parent.children.append(sibling)
                    stack.append([sibling, parent, 0, s_child, s_sibling])
    return root


# ---- 文字コード判定 -----------------------------------------------------

def detect_encoding(root: LibNode) -> str:
    """全文字列を連結し、厳密デコードが通る最初の候補を返す。

    gb18030 はほぼ全バイト列を受理してしまうため最後に置く
    （cp932 は GBK のデータで高確率で失敗するので先に試せる）。
    """
    chunks: list[bytes] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.comment:
            chunks.append(n.comment)
        if n.text:
            chunks.append(n.text)
        stack.extend(n.children)
    blob = b'\n'.join(chunks)
    for candidate in ('utf-8', 'cp932', 'gb18030'):
        try:
            blob.decode(candidate)
            return candidate
        except UnicodeDecodeError:
            continue
    print('  警告: 文字コードを判定できません。gb18030 (置換あり) で読みます', file=sys.stderr)
    return 'gb18030'


# ---- showren JSON の構築 ------------------------------------------------

def build_showren(root: LibNode, encoding: str) -> tuple[dict, dict]:
    tree: dict[str, dict] = {
        'r': {'i': 'r', 'p': None, 'c': [], 'o': 0, 'm': 0, 'x': None, 'y': None}
    }
    pos_db: dict[str, dict] = {}
    kids: dict[str, dict[tuple[int, int], str]] = {'r': {}}
    canon: dict[str, tuple[str, int]] = {}
    next_id = 1
    stats = {'nodes': 0, 'comments': 0, 'texts': 0,
             'comment_collisions': 0, 'text_collisions': 0, 'spliced': 0}

    def decode(raw: bytes) -> str:
        # 改行は \r\n → \n に正規化する（textarea 用）。RenLib 旧形式は
        # コメントを「タイトル 0x08 本文」で持つため 0x08 は改行に変換し、
        # その他の制御文字は豆腐（□）表示になるだけなので除去する。
        text = raw.decode(encoding, 'replace').replace('\r\n', '\n').replace('\x08', '\n')
        return _CONTROL_CHARS.sub('', text).strip()

    def entry_for(key: str) -> dict:
        return pos_db.setdefault(key, {'c': '', 'l': {}, 'n': {}})

    def set_comment(key: str, raw: bytes) -> None:
        text = decode(raw)
        if not text:
            return
        entry = entry_for(key)
        if entry['c']:
            if entry['c'] != text:
                stats['comment_collisions'] += 1
            return
        entry['c'] = text
        stats['comments'] += 1

    # ルートのコメント（盤面が空の局面）を先に反映する
    mirrors = [bytearray(CELLS) for _ in range(8)]
    canon['r'] = canonical_from_mirrors(mirrors, 0)
    if root.comment:
        set_comment(canon['r'][0], root.comment)

    grid = bytearray(CELLS)  # 合流検出用の平盤面（0=空）

    # DFS。ミラー盤を行きで置き、帰りで外す（rif_to_json と同じ方式）。
    # フレーム: [LibNode, showren親id, 子index, このフレームで置いた石の(x,y) or None]
    stack: list[list] = [[root, 'r', 0, None]]
    while stack:
        frame = stack[-1]
        lib_node, parent_id, child_index, _placed = frame
        if child_index >= len(lib_node.children):
            stack.pop()
            if frame[3] is not None:
                x, y = frame[3]
                cell = x * BOARD_SIZE + y
                grid[cell] = 0
                for i in range(8):
                    mirrors[i][_INVS[i][cell]] = 0
            continue
        frame[2] += 1
        child = lib_node.children[child_index]

        x, y = child.x, child.y
        cell = x * BOARD_SIZE + y if 0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE else -1
        if cell < 0 or grid[cell]:
            # 手なし・盤外・既に石がある手は木に載せず、子を親へ繋ぎ替える
            stats['spliced'] += 1
            stack.append([child, parent_id, 0, None])
            continue

        parent = tree[parent_id]
        move_no = parent['m'] + 1
        color = 1 if move_no % 2 == 1 else 2

        # 親局面のカノニカル座標で次手ヒントと手のラベルを記録する
        p_key, p_index = canon[parent_id]
        cx, cy = inverse_transform(x, y, p_index)
        p_entry = entry_for(p_key)
        p_entry['n'][f'{cx},{cy}'] = 1
        if child.text:
            label = decode(child.text)
            if label:
                point = f'{cx},{cy}'
                if point in p_entry['l']:
                    if p_entry['l'][point] != label:
                        stats['text_collisions'] += 1
                else:
                    p_entry['l'][point] = label
                    stats['texts'] += 1

        node_id = kids[parent_id].get((x, y))
        if node_id is None:
            node_id = str(next_id)
            next_id += 1
            tree[node_id] = {
                'i': node_id, 'p': parent_id, 'c': [],
                'o': color, 'm': move_no, 'x': x, 'y': y,
            }
            parent['c'].append(node_id)
            kids[parent_id][(x, y)] = node_id
            kids[node_id] = {}
            stats['nodes'] += 1
        parent.setdefault('l', node_id)

        grid[cell] = color
        for i in range(8):
            mirrors[i][_INVS[i][cell]] = color
        if node_id not in canon:
            canon[node_id] = canonical_from_mirrors(mirrors, move_no)
        if child.comment:
            set_comment(canon[node_id][0], child.comment)

        stack.append([child, node_id, 0, (x, y)])

    data = {
        't': tree,
        'p': pos_db,
        'c': 'r',
        'n': next_id,
        'pl': {'byPosKey': {}, 'byName': {}},
    }
    stats['positions'] = len(pos_db)
    return data, stats


def main() -> None:
    parser = argparse.ArgumentParser(description='RenLib (.lib) → showren JSON 変換')
    parser.add_argument('lib', help='入力 .lib ファイル')
    parser.add_argument('-o', '--out', required=True, help='出力 JSON ファイル')
    parser.add_argument('--encoding', default=None,
                        help='コメントの文字コード（省略時は utf-8/cp932/gb18030 を自動判定）')
    parser.add_argument('--pretty', action='store_true', help='読みやすく整形して出力')
    args = parser.parse_args()

    root = load_lib(args.lib)
    encoding = args.encoding or detect_encoding(root)
    data, stats = build_showren(root, encoding)

    with open_out(args.out) as f:
        if args.pretty:
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    print(f'変換完了: {args.out}')
    print(f'  文字コード       : {encoding}')
    print(f'  ノード           : {stats["nodes"]:,}')
    print(f'  局面（posDb）    : {stats["positions"]:,}')
    print(f'  コメント         : {stats["comments"]:,}')
    print(f'  手のラベル       : {stats["texts"]:,}')
    if stats['spliced']:
        print(f'  無効手スキップ   : {stats["spliced"]}（子は親へ接続）')
    if stats['comment_collisions'] or stats['text_collisions']:
        print(f'  重複（先勝ち）   : コメント {stats["comment_collisions"]} / ラベル {stats["text_collisions"]}')


if __name__ == '__main__':
    main()
