#!/usr/bin/env python3
"""
RenjuNet の RIF（XML）ファイルを showren の JSON 形式へ変換する。

- Taraguchi-10 ルールの対局のみを抽出する（ルール名で解決するので id 変更に強い）
- --player で対局者を絞り込める（姓・名・フルネームのいずれかに一致、大小文字無視）
- 局面ラベルを 3 階層で付与する
    k1: 年（例 2026）
    k2: 大会名
    k3: ラウンドと対局者（例 "R1 Kamiya-Kudomi"）

各対局の最終局面に局面ラベルを付ける。着手木は実戦の座標のまま合流させ、
次手ヒント（posDb.n）は showren と同じくカノニカル座標で保存する。

使い方:
    python rif_to_json.py renjunet_v10_20260214.rif -o kamiya.json --player Kamiya
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterator

BOARD_SIZE = 15
LAST = BOARD_SIZE - 1
CENTER = BOARD_SIZE // 2
DEFAULT_RULE = 'Taraguchi-10'


# ---- 盤面の対称変換とカノニカルキー ------------------------------------
# showren (index.html) の transform / inverseTransform / encodeBoard と同一。

def transform(cx: int, cy: int, i: int) -> tuple[int, int]:
    match i:
        case 0: return cx, cy
        case 1: return cy, LAST - cx
        case 2: return LAST - cx, LAST - cy
        case 3: return LAST - cy, cx
        case 4: return LAST - cx, cy
        case 5: return cx, LAST - cy
        case 6: return cy, cx
        case 7: return LAST - cy, LAST - cx
    raise ValueError(f'bad transform index: {i}')


def inverse_transform(px: int, py: int, i: int) -> tuple[int, int]:
    match i:
        case 0: return px, py
        case 1: return LAST - py, px
        case 2: return LAST - px, LAST - py
        case 3: return py, LAST - px
        case 4: return LAST - px, py
        case 5: return px, LAST - py
        case 6: return py, px
        case 7: return LAST - py, LAST - px
    raise ValueError(f'bad transform index: {i}')


def encode_board(s: str, color: int) -> str:
    """225 トリットを 5 トリット/バイトで 45 バイトに詰め base64url 化（60 文字）+ 手番色。"""
    data = bytearray(45)
    for i in range(45):
        v = 0
        for j in range(5):
            v = v * 3 + (ord(s[i * 5 + j]) - 48)
        data[i] = v
    b64 = base64.b64encode(bytes(data)).decode()
    return b64.replace('+', '-').replace('/', '_').rstrip('=') + str(color)


def canonical_info(grid: list[list[int]], move_count: int) -> tuple[str, int]:
    """8 対称のうち辞書順最小の盤面文字列を選び (キー, 変換インデックス) を返す。"""
    next_color = 1 if move_count % 2 == 0 else 2
    best_s: str | None = None
    best_i = 0
    for i in range(8):
        chars = []
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                tx, ty = transform(x, y, i)
                chars.append(str(grid[tx][ty]))
        s = ''.join(chars)
        if best_s is None or s < best_s:
            best_s, best_i = s, i
    assert best_s is not None
    return encode_board(best_s, next_color), best_i


# ---- 珠型の正位置への正規化 --------------------------------------------
# 中心を原点とした (u, v) 座標で D4（8 対称）を作用させる。v は下方向が正なので
# 「上」= v が負、「右」= u が正。

def _d4(u: int, v: int, i: int) -> tuple[int, int]:
    match i:
        case 0: return u, v        # 恒等
        case 1: return -v, u       # 90° 回転
        case 2: return -u, -v      # 180° 回転
        case 3: return v, -u       # 270° 回転
        case 4: return -u, v       # 縦軸鏡映
        case 5: return u, -v       # 横軸鏡映
        case 6: return v, u        # 主対角鏡映
        case 7: return -v, -u      # 反対角鏡映
    raise ValueError(f'bad d4 index: {i}')


def normalize_moves(coords: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """珠型の正位置へ手順全体を回転・反転する。

    - 2 手目: 直接（縦横の隣）なら真上、間接（斜めの隣）なら右上へ
    - 3 手目: 残る鏡映の自由度で右半分へ
      3 手目が鏡映軸上のときは、4 手目以降で最初に軸から外れる手が
      右半分に来る方を選ぶ（対称な棋譜が必ず同じ向きに揃うように）
    """
    if len(coords) < 2:
        return coords

    centered = [(x - CENTER, y - CENTER) for x, y in coords]
    if centered[0] != (0, 0):
        return coords  # 天元始まりでない棋譜は正規化しない

    u2, v2 = centered[1]
    if u2 == 0 or v2 == 0:
        target = (0, -1)                      # 直接 → 真上
        def side(u: int, v: int) -> int:      # 縦軸を境に右が正
            return u
    elif abs(u2) == abs(v2):
        target = (1, -1)                      # 間接 → 右上
        def side(u: int, v: int) -> int:      # 反対角を境に右（東）が正
            return u + v
    else:
        return coords  # Taraguchi-10 では 2 手目は必ず中央 3x3 内

    # 2 手目を目標へ写す変換はちょうど 2 つ（安定化群の位数が 2）
    candidates = [i for i in range(8) if _d4(u2, v2, i) == target]

    def first_side(index: int) -> int:
        """3 手目以降で最初に鏡映軸から外れる手の符号。全て軸上なら 0。"""
        for u, v in centered[2:]:
            s = side(*_d4(u, v, index))
            if s:
                return s
        return 0

    best = max(candidates, key=first_side)  # 右半分（正）に来る方を採用
    return [(u + CENTER, v + CENTER) for u, v in (_d4(u, v, best) for u, v in centered)]


# ---- RIF の読み込み ----------------------------------------------------

def parse_coord(token: str) -> tuple[int, int]:
    """'h8' → (7, 7)。列は a..o、行は下から 1..15。"""
    token = token.strip().lower()
    if len(token) < 2 or not token[0].isalpha() or not token[1:].isdigit():
        raise ValueError(f'bad coordinate: {token!r}')
    x = ord(token[0]) - ord('a')
    y = BOARD_SIZE - int(token[1:])
    if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
        raise ValueError(f'coordinate out of board: {token!r}')
    return x, y


def load_rif(path: str) -> tuple[dict, dict, dict, list[dict]]:
    """rules / players / tournaments の辞書と、games のリストを返す。"""
    rules: dict[str, str] = {}
    players: dict[str, dict] = {}
    tournaments: dict[str, dict] = {}
    games: list[dict] = []

    for event, elem in ET.iterparse(path, events=('end',)):
        tag = elem.tag
        if tag == 'rule':
            rules[elem.get('id', '')] = elem.get('name', '')
        elif tag == 'player':
            players[elem.get('id', '')] = {
                'name': elem.get('name', '') or '',
                'surname': elem.get('surname', '') or '',
            }
        elif tag == 'tournament':
            tournaments[elem.get('id', '')] = {
                'name': elem.get('name', '') or '',
                'year': elem.get('year', '') or '',
                'start': elem.get('start', '') or '',
            }
        elif tag == 'game':
            move_el = elem.find('move')
            moves = (move_el.text or '').split() if move_el is not None else []
            games.append({
                'id': elem.get('id', ''),
                'rule': elem.get('rule', ''),
                'tournament': elem.get('tournament', ''),
                'round': elem.get('round', '') or '',
                'black': elem.get('black', ''),
                'white': elem.get('white', ''),
                'swap': elem.get('swap', '') or '',
                'alt': elem.get('alt', '') or '',
                'info': (elem.findtext('info') or '').strip(),
                'moves': moves,
            })
        if tag in ('game', 'player', 'tournament', 'rule'):
            elem.clear()

    return rules, players, tournaments, games


# ---- 開局のスワップ解析（誰が 1〜5 手目を置いたか）----------------------
# RIF の swap 属性は 5 スロット。'R'（= '+'）がスワップ、'-' がスワップなし。
# 最終的な黒白（black / white 属性）から逆順にスワップを巻き戻して各手の
# 着手者を求める（nachirenjutools の analyzeTaraguchi と同じ手順）。
# choice 2（10 題提示）はスワップ機会が 3 回しかないため、不足分は '-' で埋める。

_INFO_BLACK = re.compile(r'[BbВв]\s*[=:]?\s*([0-9][0-9,\s]*)')


def opening_owners(swap: str) -> dict[int, str] | None:
    """1〜5 手目の着手者を最終的な色（'B' / 'W'）で返す。判定不能なら None。"""
    raw = swap.strip()
    if not raw or not set(raw) <= set('R-+'):
        return None  # 空、または 'x' などの不明文字を含む
    calc = raw.replace('R', '+').ljust(5, '-')[:5]

    black, white = 'B', 'W'  # 最終的な色から出発し、後ろからスワップを巻き戻す
    owners: dict[int, str] = {}
    for i in range(5, -1, -1):
        owners[i + 1] = black if (i + 1) % 2 == 1 else white
        if i > 0 and calc[i - 1] == '+':
            black, white = white, black
    return {k: owners[k] for k in range(1, 6)}


def info_swap_symbols(info: str) -> str:
    """<info> が '+ + +' のようなスワップ記号列ならそれを返す（なければ空文字）。"""
    text = info.strip()
    if text and set(text) <= set('+- '):
        return text.replace(' ', '')
    return ''


def owners_from_info(info: str) -> dict[int, str] | None:
    """swap 属性が使えないとき、<info> の自由記述から着手者を推定する。

    注意: <info> の 'B=...' は最終的な黒ではなく開局時の黒を指す例があり
    （swap='R--R-' と 'R--RR' で同じ 'B:1245' が書かれている等）表記が揺れる。
    swap 属性が全く無い局の最後の手段としてのみ使う。
    """
    text = info.strip()
    if not text:
        return None
    symbols = info_swap_symbols(text)
    if symbols:
        return opening_owners(symbols)
    match = _INFO_BLACK.search(text)
    if not match:
        return None
    digits = {int(c) for c in re.sub(r'[^0-9]', '', match.group(1)) if c in '12345'}
    if not digits:
        return None
    return {k: ('B' if k in digits else 'W') for k in range(1, 6)}


def resolve_owners(swap: str, info: str) -> dict[int, str] | None:
    """swap 属性と <info> のスワップ記号列のうち、記録が長い方を採用する。

    choice 2（10 題提示）の局は swap 属性が 1 文字に切り詰められている一方、
    <info> に '+ + +' と 3 回分が残っていることが多いため。
    どちらも使えない場合のみ <info> の自由記述にフォールバックする。
    """
    attr = swap.strip() if set(swap.strip()) <= set('R-+') else ''
    symbols = info_swap_symbols(info)
    best = max((attr, symbols), key=len)
    if best:
        return opening_owners(best)
    return owners_from_info(info)


def is_ten_offer(alt: str) -> bool:
    """alt に 10 題提示の痕跡（選ばれなかった候補が並ぶ）があるか。"""
    raw = alt.strip()
    if not raw or raw == '-':
        return False
    return len([x for x in raw.split(',') if x.strip()]) >= 7


def build_comment(black_name: str, white_name: str,
                  owners: dict[int, str] | None, ten_offer: bool) -> str:
    """最終局面に載せるコメント。

        B: Shunsuke Kamiya (125+)
        W: Shoma Matsuda (34)

    括弧内は「その人が 1〜5 手目のうち実際に置いた手番号」。'+' は 10 題提示。
    スワップ情報が無く着手者を特定できない場合は括弧を付けない。
    """
    if owners is None:
        return f'B: {black_name}\nW: {white_name}'

    def cell(role: str) -> str:
        digits = ''.join(str(k) for k in range(1, 6) if owners[k] == role)
        plus = '+' if ten_offer and owners[5] == role else ''
        return f'({digits}{plus})'

    return f'B: {black_name} {cell("B")}\nW: {white_name} {cell("W")}'


# ---- ラベル生成 --------------------------------------------------------

def player_label(player: dict | None) -> str:
    if not player:
        return '?'
    return player['surname'] or player['name'] or '?'


def player_full_name(player: dict | None) -> str:
    if not player:
        return '?'
    return f"{player['name']} {player['surname']}".strip() or '?'


def matches_player(player: dict | None, wanted: list[str]) -> bool:
    if not player:
        return False
    candidates = {
        player['name'].lower(),
        player['surname'].lower(),
        f"{player['name']} {player['surname']}".strip().lower(),
    }
    return any(w in candidates for w in wanted)


def format_round(raw: str) -> str:
    """数値のみのラウンドは 'R1' 形式に、それ以外（'F2' など）はそのまま。"""
    raw = raw.strip()
    if not raw:
        return '?'
    return f'R{raw}' if raw.isdigit() else raw


def tournament_year(t: dict | None) -> str:
    if not t:
        return '?'
    if t['year']:
        return t['year']
    if len(t['start']) >= 4 and t['start'][:4].isdigit():
        return t['start'][:4]
    return '?'


# ---- 変換本体 ----------------------------------------------------------

class TreeBuilder:
    """実戦の座標のまま着手木を合流させ、posDb と局面ラベルを組み立てる。"""

    def __init__(self) -> None:
        # showren と同じく 'l'（lastSelectedChild）は親になったノードだけが持つ
        self.tree: dict[str, dict] = {
            'r': {'i': 'r', 'p': None, 'c': [], 'o': 0, 'm': 0, 'x': None, 'y': None}
        }
        self.next_id = 1
        self.pos_db: dict[str, dict] = {}
        self.by_pos_key: dict[str, dict] = {}
        self.by_name: dict[str, str] = {}
        self._canon: dict[str, tuple[str, int]] = {}
        self.label_collisions = 0
        self.poskey_collisions = 0

    def _canonical(self, node_id: str, grid: list[list[int]], move_count: int) -> tuple[str, int]:
        cached = self._canon.get(node_id)
        if cached is None:
            cached = canonical_info(grid, move_count)
            self._canon[node_id] = cached
        return cached

    def add_game(self, moves: list[tuple[int, int]], label: tuple[str, str, str],
                 comment: str = '') -> None:
        grid = [[0] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        node_id = 'r'

        for index, (x, y) in enumerate(moves):
            parent = self.tree[node_id]
            move_no = index + 1
            color = 1 if move_no % 2 == 1 else 2

            # 親局面のカノニカル情報 → 次手ヒントをカノニカル座標で記録
            key, canon_index = self._canonical(node_id, grid, index)
            cx, cy = inverse_transform(x, y, canon_index)
            entry = self.pos_db.setdefault(key, {'c': '', 'l': {}, 'n': {}})
            entry['n'][f'{cx},{cy}'] = 1

            child_id = next(
                (cid for cid in parent['c'] if self.tree[cid]['x'] == x and self.tree[cid]['y'] == y),
                None,
            )
            if child_id is None:
                child_id = str(self.next_id)
                self.next_id += 1
                self.tree[child_id] = {
                    'i': child_id, 'p': node_id, 'c': [],
                    'o': color, 'm': move_no, 'x': x, 'y': y,
                }
                parent['c'].append(child_id)
            parent.setdefault('l', child_id)

            grid[x][y] = color
            node_id = child_id

        if node_id == 'r':
            return  # 着手が無い対局はラベルを付けない

        final_key, _ = self._canonical(node_id, grid, len(moves))
        if comment:
            # 最終局面へコメントを載せる。別の対局が同じ局面に到達済みなら先勝ち。
            entry = self.pos_db.setdefault(final_key, {'c': '', 'l': {}, 'n': {}})
            if not entry['c']:
                entry['c'] = comment
        self._register_label(final_key, node_id, label)

    def _register_label(self, pos_key: str, rep: str, label: tuple[str, str, str]) -> None:
        k1, k2, k3 = label
        # 同名ラベルが既にあれば連番を付けて衝突を避ける
        name = f'{k1}/{k2}/{k3}'
        if name in self.by_name:
            self.label_collisions += 1
            suffix = 2
            while f'{k1}/{k2}/{k3} ({suffix})' in self.by_name:
                suffix += 1
            k3 = f'{k3} ({suffix})'
            name = f'{k1}/{k2}/{k3}'

        if pos_key in self.by_pos_key:
            # 別の対局が同一の最終局面に到達した場合は先勝ち（1 局面 1 ラベル）
            self.poskey_collisions += 1
            return

        self.by_pos_key[pos_key] = {'k1': k1, 'k2': k2, 'k3': k3, 'rep': rep}
        self.by_name[name] = pos_key

    def build(self) -> dict:
        return {
            't': self.tree,
            'p': self.pos_db,
            'c': 'r',
            'n': self.next_id,
            'pl': {'byPosKey': self.by_pos_key, 'byName': self.by_name},
        }


def convert(
    rif_path: str,
    rule_name: str = DEFAULT_RULE,
    wanted_players: list[str] | None = None,
    limit: int | None = None,
    normalize: bool = True,
) -> tuple[dict, dict]:
    rules, players, tournaments, games = load_rif(rif_path)

    rule_ids = {rid for rid, name in rules.items() if name == rule_name}
    if not rule_ids:
        raise SystemExit(f'ルールが見つかりません: {rule_name!r} (候補: {sorted(rules.values())})')

    wanted = [w.strip().lower() for w in (wanted_players or []) if w.strip()]
    builder = TreeBuilder()
    stats = {'total': len(games), 'rule_matched': 0, 'player_matched': 0,
             'converted': 0, 'skipped': 0, 'reoriented': 0,
             'with_owners': 0, 'ten_offer': 0}

    for game in games:
        if game['rule'] not in rule_ids:
            continue
        stats['rule_matched'] += 1

        black = players.get(game['black'])
        white = players.get(game['white'])
        if wanted and not (matches_player(black, wanted) or matches_player(white, wanted)):
            continue
        stats['player_matched'] += 1

        if not game['moves']:
            stats['skipped'] += 1
            continue
        try:
            coords = [parse_coord(tok) for tok in game['moves']]
        except ValueError as error:
            print(f'  skip game {game["id"]}: {error}', file=sys.stderr)
            stats['skipped'] += 1
            continue
        if len({c for c in coords}) != len(coords):
            print(f'  skip game {game["id"]}: 同一交点への重複着手', file=sys.stderr)
            stats['skipped'] += 1
            continue

        if normalize:
            normalized = normalize_moves(coords)
            if normalized != coords:
                stats['reoriented'] += 1
            coords = normalized

        tournament = tournaments.get(game['tournament'])
        k1 = tournament_year(tournament)
        k2 = (tournament['name'] if tournament else '') or '?'
        k3 = f'{format_round(game["round"])} {player_label(black)}-{player_label(white)}'

        owners = resolve_owners(game['swap'], game['info'])
        ten_offer = is_ten_offer(game['alt'])
        if owners is not None:
            stats['with_owners'] += 1
        if ten_offer:
            stats['ten_offer'] += 1
        comment = build_comment(player_full_name(black), player_full_name(white), owners, ten_offer)

        builder.add_game(coords, (k1, k2, k3), comment)
        stats['converted'] += 1
        if limit and stats['converted'] >= limit:
            break

    stats['nodes'] = len(builder.tree)
    stats['positions'] = len(builder.pos_db)
    stats['labels'] = len(builder.by_pos_key)
    stats['label_collisions'] = builder.label_collisions
    stats['poskey_collisions'] = builder.poskey_collisions
    return builder.build(), stats


def main() -> None:
    parser = argparse.ArgumentParser(description='RenjuNet RIF → showren JSON 変換')
    parser.add_argument('rif', help='入力 RIF ファイル (例: renjunet_v10_20260214.rif)')
    parser.add_argument('-o', '--out', required=True, help='出力 JSON ファイル')
    parser.add_argument('--player', action='append', default=[],
                        help='対局者で絞り込む（姓/名/フルネーム、複数指定可）例: --player Kamiya')
    parser.add_argument('--rule', default=DEFAULT_RULE, help=f'ルール名（既定: {DEFAULT_RULE}）')
    parser.add_argument('--limit', type=int, default=None, help='変換する最大対局数')
    parser.add_argument('--no-normalize', dest='normalize', action='store_false',
                        help='珠型の正位置への回転・反転を行わない（実戦の座標のまま）')
    parser.add_argument('--pretty', action='store_true', help='読みやすく整形して出力')
    args = parser.parse_args()

    data, stats = convert(args.rif, args.rule, args.player, args.limit, args.normalize)

    with open(args.out, 'w', encoding='utf-8') as f:
        if args.pretty:
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    print(f'変換完了: {args.out}')
    print(f'  全対局           : {stats["total"]:,}')
    print(f'  {args.rule:<16}: {stats["rule_matched"]:,}')
    if args.player:
        print(f'  対局者一致       : {stats["player_matched"]:,} ({", ".join(args.player)})')
    print(f'  変換             : {stats["converted"]:,} 局（skip {stats["skipped"]}）')
    if args.normalize:
        print(f'  正位置へ回転/反転: {stats["reoriented"]:,} 局')
    print(f'  着手者を特定     : {stats["with_owners"]:,} 局（括弧付きコメント）')
    print(f'  10 題提示        : {stats["ten_offer"]:,} 局')
    print(f'  ノード           : {stats["nodes"]:,}')
    print(f'  局面（posDb）    : {stats["positions"]:,}')
    print(f'  局面ラベル       : {stats["labels"]:,}')
    if stats['label_collisions']:
        print(f'  ラベル名の重複   : {stats["label_collisions"]}（連番を付与）')
    if stats['poskey_collisions']:
        print(f'  最終局面の重複   : {stats["poskey_collisions"]}（先勝ち）')


if __name__ == '__main__':
    main()
