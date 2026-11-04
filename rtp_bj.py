"""21 点（Blackjack）RTP / 玩家优势 标定（与 blackjack.html 逻辑逐行对齐）。

规则口径（docs/blackjack_开发规划.md §7 落定）：
  - 6 副牌（312 张），切牌点 75%（约 234 张后重洗）
  - 庄家 S17（软 17 停牌）
  - 黑杰克赔付 3:2
  - 单玩家位；玩家不买保险（保险为负 EV，基本策略玩家不买）
  - 加倍：任何两张首手均可（全范围）
  - 分牌：允许重分至 4 手；A 分后只发一张
  - 投降：暂不做
  - 美式 peek：庄家明牌为 A/10 时检查暗牌，庄家黑杰克本局立即结束

模型说明：
  - 这里用 Python 标准 random 做蒙特卡洛（仅用于 EV 标定基线，分布只看均匀性）。
    生产路径抽牌接 H743 熵（见 bj_engine.js 的 draw 回调与 blackjack.html 的 ByteQueue）。
  - 玩家采用通用多副基本策略（basic_strategy），与 bj_engine.js 同款策略保持一致。

用法：
  python rtp_bj.py [N]
    N  样本轮数，默认 2_000_000
预期：庄家优势约 +0.40%（WoO 精确值 0.4026%：6副 S17 DAS 3:2 重分4手 A分一张）。
标定记录（2026-11-04，2M 轮）：HE +0.4001% / RTP 99.5999%，与理论差 0.0025pp ✓
"""
import random
import sys

NUM_DECKS = 6
PENETRATION = 0.75
MAX_SPLIT_HANDS = 4
BLACKJACK_PAY = 1.5
DEALER_HITS_SOFT17 = False
DOUBLE_ANY = True

RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']

def build_shoe():
    shoe = []
    for _ in range(NUM_DECKS):
        for r in RANKS:
            for _ in range(4):   # 每副每点数 4 张（黑桃/红心/方块/梅花）
                shoe.append(r)
    return shoe  # 312 张

def hand_value(cards):
    total = 0
    aces = 0
    for r in cards:
        if r == 'A':
            total += 11; aces += 1
        elif r in ('J', 'Q', 'K'):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces > 0:
        total -= 10; aces -= 1
    return total, aces > 0, total > 21

def is_blackjack(cards):
    t, _, _ = hand_value(cards)
    return len(cards) == 2 and t == 21 and 'A' in cards

def is_pair(cards):
    return len(cards) == 2 and cards[0] == cards[1]

def up_num(up):
    if up == 'A': return 11
    if up in ('J', 'Q', 'K'): return 10
    return int(up)

def basic_strategy(player, up, no_split=False):
    t, soft, _ = hand_value(player)
    un = up_num(up)
    if is_pair(player) and not no_split:
        pr = player[0]
        if pr == 'A': return 'P'
        if pr == '8': return 'P'
        if pr == '9': return 'S' if un in (7, 10, 11) else 'P'
        if pr == '7': return 'P' if un <= 7 else 'H'
        if pr == '6': return 'P' if un <= 6 else 'H'
        if pr == '5': return 'D' if un <= 9 else 'H'
        if pr == '4': return 'P' if un in (5, 6) else 'H'
        if pr in ('3', '2'): return 'P' if un <= 7 else 'H'
    if soft:
        if t >= 19: return 'S'
        if t == 18:
            if un in (2, 7, 8): return 'S'
            if 3 <= un <= 6: return 'D'
            return 'H'
        if t == 17: return 'D' if 3 <= un <= 6 else 'H'
        if t == 16: return 'D' if 4 <= un <= 6 else 'H'
        if t == 15: return 'D' if 4 <= un <= 6 else 'H'
        if t == 14: return 'D' if un in (5, 6) else 'H'
        if t == 13: return 'D' if un in (5, 6) else 'H'
    if t >= 17: return 'S'
    if 13 <= t <= 16: return 'S' if un <= 6 else 'H'
    if t == 12: return 'S' if un in (4, 5, 6) else 'H'
    if t == 11: return 'D' if un <= 10 else 'H'
    if t == 10: return 'D' if un <= 9 else 'H'
    if t == 9:  return 'D' if 3 <= un <= 6 else 'H'
    return 'H'

def dealer_play(dealer, shoe):
    while True:
        t, soft, _ = hand_value(dealer)
        if t < 17:
            dealer.append(shoe.pop())
        elif t == 17 and soft and DEALER_HITS_SOFT17:
            dealer.append(shoe.pop())
        else:
            break
    return dealer

def play_round(shoe):
    # 发牌
    player = [shoe.pop(), shoe.pop()]
    dealer = [shoe.pop(), shoe.pop()]   # [up, hole]
    up = dealer[0]
    dealer_bj = is_blackjack(dealer)
    player_bj = is_blackjack(player)

    wagered = 1.0
    stats = {'win': 0, 'lose': 0, 'push': 0, 'blackjack': 0, 'bust': 0, 'splits': 0, 'doubles': 0}
    # 美式 peek
    if dealer_bj:
        if player_bj:
            stats['push'] += 1
            return 0.0, wagered, stats          # push
        stats['lose'] += 1
        return -1.0, wagered, stats             # 玩家负
    if player_bj:
        stats['blackjack'] += 1
        return BLACKJACK_PAY, wagered, stats    # 天然黑杰克 3:2

    # 逐手行动（支持分牌/加倍）
    hands = [{'cards': player, 'bet': 1.0, 'from_split': False, 'split_ace': False}]
    i = 0
    while i < len(hands):
        h = hands[i]
        if h['split_ace']:
            i += 1
            continue
        while True:
            t, soft, bust = hand_value(h['cards'])
            if bust:
                i += 1               # 该手爆牌结束
                break
            if len(h['cards']) == 2 and len(hands) < MAX_SPLIT_HANDS and is_pair(h['cards']):
                dec = basic_strategy(h['cards'], up)
            else:
                dec = basic_strategy(h['cards'], up, no_split=True)
            if dec == 'P' and len(hands) < MAX_SPLIT_HANDS:
                # 原位替换 hands[i] 为两手：新第 1 手仍占下标 i，下一轮继续处理它。
                # 不能用 hands.index(h) 定位——dict 按值相等比较，两手内容相同
                # （如都是 [8,10]）时会返回错误下标、替换错手，导致 EV 虚高。
                is_a = (h['cards'][0] == 'A')
                c1, c2 = h['cards'][0], h['cards'][1]
                nh1 = {'cards': [c1, shoe.pop()], 'bet': 1.0, 'from_split': True, 'split_ace': is_a}
                nh2 = {'cards': [c2, shoe.pop()], 'bet': 1.0, 'from_split': True, 'split_ace': is_a}
                hands[i:i + 1] = [nh1, nh2]
                wagered += 1.0
                stats['splits'] += 1
                break                # 不推进 i，下一轮处理新第 1 手
            elif dec == 'D' and len(h['cards']) == 2:
                h['bet'] *= 2.0
                wagered += 1.0
                stats['doubles'] += 1
                h['cards'].append(shoe.pop())
                i += 1               # 加倍后强制停牌，该手结束
                break
            elif dec == 'S':
                i += 1               # 停牌，该手结束
                break
            else:  # H
                h['cards'].append(shoe.pop())
                # 继续内层循环重新决策

    # 庄家补牌
    dealer_play(dealer, shoe)

    dt, _, dbust = hand_value(dealer)
    total_net = 0.0
    for h in hands:
        t, _, bust = hand_value(h['cards'])
        if bust:
            stats['bust'] += 1
            total_net -= h['bet']
        elif dbust:
            stats['win'] += 1
            total_net += h['bet']
        elif t > dt:
            stats['win'] += 1
            total_net += h['bet']
        elif t < dt:
            stats['lose'] += 1
            total_net -= h['bet']
        else:
            stats['push'] += 1
            total_net += 0.0
    return total_net, wagered, stats

def run(n):
    shoe = build_shoe()
    random.shuffle(shoe)
    cut = int(len(shoe) * (1 - PENETRATION))   # 切牌点：剩 78 张(25%)即重洗
    net_sum = 0.0          # 玩家净收益（可负）
    wager_sum = 0.0
    reshuffles = 0
    cnt = {'win': 0, 'lose': 0, 'push': 0, 'blackjack': 0, 'bust': 0}
    splits = 0
    doubles = 0
    for _ in range(n):
        if len(shoe) <= cut:
            shoe = build_shoe()
            random.shuffle(shoe)
            reshuffles += 1
        net, wagered, stats = play_round(shoe)
        net_sum += net
        wager_sum += wagered
        for k in cnt:
            cnt[k] += stats.get(k, 0)
        splits += stats.get('splits', 0)
        doubles += stats.get('doubles', 0)
    house_edge = -net_sum / wager_sum          # 庄家优势（小数）
    rtp_gross = (net_sum + wager_sum) / wager_sum   # 总返还率（小数，应≈0.99x）
    print(f"规则              : 6副 S17 3:2 加倍全范围 重分至{MAX_SPLIT_HANDS}手 A分一张 不买保险 切牌点{PENETRATION}")
    print(f"样本轮数          : {n:,}")
    print(f"重洗次数          : {reshuffles:,}")
    print(f"总下注(单位)      : {wager_sum:,.0f}")
    print(f"RTP(总返还率)     : {rtp_gross*100:.4f}%")
    print(f"庄家优势(HE)      : {house_edge*100:+.4f}%   玩家优势 {(-house_edge)*100:+.4f}%")
    print(f"  胜负分布(按手)  : 赢 {cnt['win']:,}  输 {cnt['lose']:,}  平 {cnt['push']:,}  黑杰克 {cnt['blackjack']:,}  爆牌 {cnt['bust']:,}")
    print(f"  分牌次数        : {splits:,}   加倍次数 {doubles:,}")
    print(f"  → 理论参考：基本策略下庄家优势约 0.4%（±0.1pp，取决于加倍/分牌细则）")

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    run(n)
