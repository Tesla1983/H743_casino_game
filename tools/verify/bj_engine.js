/* =============================================================================
 *  bj_engine.js — 21 点（Blackjack）核心引擎（纯逻辑，与 UI 解耦）
 *
 *  设计目标（见 docs/blackjack_开发规划.md §1 M-E/M-F/M-G）：
 *   - 纯函数 / 无 DOM / 无网络 / 无 Math.random（随机源只通过回调注入）。
 *   - Node 下 `require` 可用（unit test），浏览器下挂 `window.BJ`（UI 调用）。
 *   - 规则口径已在 §7 落定：6 副、S17、3:2、单玩家位、保险可开关、投降暂不做、
 *     允许重分至 4 手、A 分后只发一张、double 全范围、切牌点 75%。
 *
 *  重要：本文件不实现「真实抽牌的随机源」。抽牌由调用方通过 draw() 回调注入
 *  （浏览器生产路径接 H743 熵；Node 测试/仿真注入确定性或伪随机源）。
 * ========================================================================== */
(function (root, factory) {
  var mod = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = mod;
  else root.BJ = mod;
})(typeof self !== "undefined" ? self : this, function () {

  var SUITS = ["S", "H", "D", "C"];          // ♠ ♥ ♦ ♣
  var RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"];

  // ---- 落定规则（与 docs/blackjack_开发规划.md §7 完全一致） ----
  var CFG = {
    NUM_DECKS: 6,
    PENETRATION: 0.75,            // 切牌点：鞋用到约 75% 后重洗
    MAX_SPLIT_HANDS: 4,           // 允许重分至 4 手
    BLACKJACK_PAY: 1.5,           // 3:2
    DEALER_HITS_SOFT17: false,    // S17（软 17 停牌）
    INSURANCE: true,              // 保险纳入（UI 默认开启、可开关）
    SURRENDER: false,             // 投降 MVP 后置（暂不做）
    DOUBLE_ANY: true              // 任何两张首手均可 double
  };

  /* ---------------- 牌 ---------------- */
  function rankValue(rank) {
    if (rank === "A") return 11;
    if (rank === "J" || rank === "Q" || rank === "K") return 10;
    return parseInt(rank, 10);
  }
  function cardCode(rank, suit) { return rank + suit; }   // "AS" "10H" "KD"

  function buildShoe(numDecks) {
    numDecks = numDecks || CFG.NUM_DECKS;
    var shoe = [];
    for (var d = 0; d < numDecks; d++) {
      for (var s = 0; s < SUITS.length; s++) {
        for (var r = 0; r < RANKS.length; r++) {
          var rank = RANKS[r], suit = SUITS[s];
          shoe.push({ rank: rank, suit: suit, code: cardCode(rank, suit), value: rankValue(rank) });
        }
      }
    }
    return shoe;   // 长度 52 * numDecks
  }

  function isCardCode(c) {
    if (typeof c !== "string" || c.length < 2) return false;
    var suit = c.slice(-1), rank = c.slice(0, -1);
    return SUITS.indexOf(suit) >= 0 && RANKS.indexOf(rank) >= 0;
  }

  /* ---------------- 点数计算（A 双值） ----------------
   * 返回 { total, soft, bust }
   *  soft=true 表示仍有 A 按 11 计（软手）；bust = total>21。 */
  function handValue(cards) {
    var total = 0, aces = 0;
    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].rank;
      if (r === "A") { total += 11; aces++; }
      else if (r === "J" || r === "Q" || r === "K") total += 10;
      else total += parseInt(r, 10);
    }
    while (total > 21 && aces > 0) { total -= 10; aces--; }
    return { total: total, soft: aces > 0, bust: total > 21 };
  }

  // 天然黑杰克：仅限「首两手」且 total===21 且含一张 A（分牌后凑 21 不算）
  function isBlackjack(cards) {
    return cards.length === 2 && handValue(cards).total === 21 &&
      cards.some(function (c) { return c.rank === "A"; });
  }
  function isPair(cards) {
    // 按牌值判对子：10/J/Q/K 同为 10 值，可混合分牌（符合赌场惯例）。
    // 页面与单测的牌对象均带 .value，故直接比较 value。
    return cards.length === 2 && cards[0].value === cards[1].value;
  }

  /* ---------------- 洗牌：Fisher-Yates + 16-bit 拒绝采样 ----------------
   * rng16(): 返回 [0,65535] 的随机整数（由调用方注入）。
   * 生产环境接 H743 熵（两字节 → uint16）；绝不用 Math.random。 */
  function shuffle(shoe, rng16) {
    var n = shoe.length;
    for (var i = n - 1; i > 0; i--) {
      var limit = Math.floor(65536 / (i + 1)) * (i + 1);   // 最大可接受上界，消除取模偏差
      var v;
      do { v = rng16(); } while (v >= limit);
      var j = v % (i + 1);
      var t = shoe[i]; shoe[i] = shoe[j]; shoe[j] = t;
    }
    return shoe;
  }

  /* ---------------- 庄家规则（S17/H17） ---------------- */
  function dealerShouldHit(cards) {
    var v = handValue(cards);
    if (v.total < 17) return true;
    if (v.total > 17) return false;
    // total === 17
    if (v.total === 17 && v.soft && CFG.DEALER_HITS_SOFT17) return true;  // H17
    return false;                                                          // S17
  }
  // 庄家把牌补到规则停手（draw() 取牌，返回更新后的 dealer 数组）
  function dealerPlay(dealerCards, draw) {
    while (dealerShouldHit(dealerCards)) {
      var c = draw();
      if (!c) break;
      dealerCards.push(c);
    }
    return dealerCards;
  }

  /* ---------------- 单手结算（玩家一手 vs 庄家） ----------------
   * playerNaturalBJ: 该玩家手是否为「天然黑杰克」（仅首两手成立）。
   * 返回 { outcome, net }，net 为相对该手注额的倍数：
   *   blackjack +1.5 / win +1 / push 0 / lose -1 / bust -1。 */
  function settleHand(playerCards, dealerCards, playerNaturalBJ) {
    var pv = handValue(playerCards);
    var dv = handValue(dealerCards);
    var dealerBJ = isBlackjack(dealerCards);
    if (playerNaturalBJ && !dealerBJ) return { outcome: "blackjack", net: CFG.BLACKJACK_PAY };
    if (playerNaturalBJ && dealerBJ)  return { outcome: "push", net: 0 };
    if (dealerBJ && !playerNaturalBJ) return { outcome: "lose", net: -1 };
    if (pv.bust)  return { outcome: "bust", net: -1 };
    if (dv.bust)  return { outcome: "win", net: 1 };
    if (pv.total > dv.total) return { outcome: "win", net: 1 };
    if (pv.total < dv.total) return { outcome: "lose", net: -1 };
    return { outcome: "push", net: 0 };
  }

  /* ---------------- 基本策略（BS） ----------------
   * 返回 'H' 站/hit、'S' 停/stand、'D' 加倍、'P' 分牌。
   * 用于 Node 自测 + rtp_bj.py 的对照基线（玩家不买保险，故保险不在此策略内）。
   * 这是通用多副 BS（S17、3:2、可重分、A 分一张、全范围 double）的近似实现。 */
  function basicStrategy(playerCards, dealerUp, ctx) {
    ctx = ctx || {};
    var v = handValue(playerCards);
    var up = dealerUp.rank;
    var upN = (up === "A") ? 11 : (up === "J" || up === "Q" || up === "K") ? 10 : parseInt(up, 10);

    // 分牌（仅在可分时由调用方先判 isPair）
    if (isPair(playerCards) && !ctx.noSplit) {
      var pr = playerCards[0].rank;
      if (pr === "A") return "P";
      if (pr === "8") return "P";
      if (pr === "9") return (upN === 7 || upN === 10 || upN === 11) ? "S" : "P";
      if (pr === "7") return (upN <= 7) ? "P" : "H";
      if (pr === "6") return (upN <= 6) ? "P" : "H";
      if (pr === "5") return (upN <= 9) ? "D" : "H";   // 5,5 当硬 10 处理
      if (pr === "4") return (upN === 5 || upN === 6) ? "P" : "H";
      if (pr === "3" || pr === "2") return (upN <= 7) ? "P" : "H";
    }

    if (v.soft) {
      // 软手
      if (v.total >= 19) return "S";                     // A,8+ 软 19/20/21
      if (v.total === 18) {                              // A,7
        if (upN === 2 || upN === 7 || upN === 8) return "S";
        if (upN >= 3 && upN <= 6) return "D";
        return "H";                                       // 9,10,A
      }
      if (v.total === 17) return (upN >= 3 && upN <= 6) ? "D" : "H";  // A,6
      if (v.total === 16) return (upN >= 4 && upN <= 6) ? "D" : "H";  // A,5
      if (v.total === 15) return (upN >= 4 && upN <= 6) ? "D" : "H";  // A,4
      if (v.total === 14) return (upN === 5 || upN === 6) ? "D" : "H";// A,3
      if (v.total === 13) return (upN === 5 || upN === 6) ? "D" : "H";// A,2
    }

    // 硬手
    if (v.total >= 17) return "S";
    if (v.total >= 13 && v.total <= 16) return (upN <= 6) ? "S" : "H";
    if (v.total === 12) return (upN === 4 || upN === 5 || upN === 6) ? "S" : "H";
    if (v.total === 11) return (upN <= 10) ? "D" : "H";   // 11 加倍，对 A 停
    if (v.total === 10) return (upN <= 9) ? "D" : "H";
    if (v.total === 9)  return (upN >= 3 && upN <= 6) ? "D" : "H";
    return "H";                                            // 8 及以下
  }

  /* ---------------- 整手结算器（含分牌/加倍/保险；同步逻辑，供测试与 UI 复用） ----------------
   * 入参：
   *   player: [c0, c1]           玩家首两张
   *   dealer: [up, hole]         庄家明牌、暗牌
   *   draw:   function()->card|null   从牌靴取一张（空返回 null）
   *   bet:    首注（单位）
   *   opts:   { takeInsurance:false }
   * 出参：{ hands:[{cards,bet,outcome,net}], dealerCards, dealerBJ, insuranceNet,
   *         totalNet, totalWagered, playerBJ }
   */
  function playRound(player, dealer, draw, bet, opts) {
    opts = opts || {};
    var upcard = dealer[0];
    var dealerCards = dealer.slice();
    var dealerBJ = isBlackjack(dealerCards);
    var insuranceNet = 0;

    // 保险：庄家明牌为 A 且开启。BS 玩家不买（保险为负 EV），故默认 takeInsurance=false。
    if (CFG.INSURANCE && upcard.rank === "A" && opts.takeInsurance) {
      insuranceNet = dealerBJ ? 1.0 : -0.5;   // 保险注 0.5*bet，黑杰克 2:1 → 净 +0.5*bet
    }

    var playerBJ = isBlackjack(player);

    // 庄家暗牌为 10/J/Q/K（即黑杰克）时，美式 peek：本局立即结束
    if (dealerBJ) {
      var hands = [{ cards: player.slice(), bet: bet,
        outcome: playerBJ ? "push" : "lose", net: playerBJ ? 0 : -1 }];
      return finalize(hands, dealerCards, dealerBJ, playerBJ, insuranceNet, bet);
    }
    if (playerBJ) {
      // 玩家天然黑杰克、庄家非 BJ：直接以 3:2 结算（不需再行动）
      var h0 = [{ cards: player.slice(), bet: bet, outcome: "blackjack", net: CFG.BLACKJACK_PAY }];
      return finalize(h0, dealerCards, dealerBJ, playerBJ, insuranceNet, bet);
    }

    // 正常对局：逐手行动
    var handsArr = [{ cards: player.slice(), bet: bet, done: false, fromSplit: false, splitAce: false }];
    var i = 0;
    while (i < handsArr.length) {
      var h = handsArr[i];
      i++;
      if (h.splitAce) { h.done = true; continue; }   // A 分后只发一张（已发），强制停

      // 行动循环
      while (!h.done) {
        var v = handValue(h.cards);
        if (v.bust) { h.done = true; break; }
        var decision;
        if (h.cards.length === 2 && handsArr.length < CFG.MAX_SPLIT_HANDS && isPair(h.cards)) {
          decision = basicStrategy(h.cards, upcard, {});
        } else {
          decision = basicStrategy(h.cards, upcard, { noSplit: true });
        }

        if (decision === "P" && handsArr.length < CFG.MAX_SPLIT_HANDS) {
          // 分牌：拆成两手，各下等额注；A 分后只发一张
          var isA = (h.cards[0].rank === "A");
          var c1 = h.cards[0], c2 = h.cards[1];
          var newH1 = { cards: [c1, draw()].filter(Boolean), bet: h.bet, done: false, fromSplit: true, splitAce: isA };
          var newH2 = { cards: [c2, draw()].filter(Boolean), bet: h.bet, done: false, fromSplit: true, splitAce: isA };
          // 替换当前手为两手（注意：while 用 i++ 遍历，插入到 i 之前）
          handsArr.splice(handsArr.indexOf(h), 1, newH1, newH2);
          i--;            // 退回，先处理 newH1
          break;          // 结束本手循环，外层 while 继续
        } else if (decision === "D" && h.cards.length === 2) {
          h.bet *= 2;
          var dc = draw(); if (dc) h.cards.push(dc);
          h.done = true;  // 加倍只再发一张后强制停
        } else if (decision === "S") {
          h.done = true;
        } else { // H
          var hc = draw(); if (hc) h.cards.push(hc);
          if (handValue(h.cards).bust) h.done = true;
        }
      }
    }

    // 庄家揭示并补牌
    dealerPlay(dealerCards, draw);

    // 逐手结算（settleHand 返回的 net 已是「相对该手注」的倍数）
    var settled = handsArr.map(function (h) {
      var nat = !h.fromSplit && isBlackjack(h.cards);   // 分牌后凑的 21 不享受 3:2
      var res = settleHand(h.cards, dealerCards, nat);
      return { cards: h.cards, bet: h.bet, outcome: res.outcome, net: res.net };
    });
    return finalize(settled, dealerCards, dealerBJ, playerBJ, insuranceNet, bet);
  }

  /* 聚合并返回整手结果。
   * 约定：各 hand.net 是「相对该手注」的倍数（来自 settleHand / 加倍后的注），
   * 玩家该手实际净 = net × 该手注；totalWagered 为所有手注之和（含加倍/分牌加注）。 */
  function finalize(hands, dealerCards, dealerBJ, playerBJ, insuranceNet, baseBet) {
    var totalNet = insuranceNet;      // 保险已按「相对首注」给出（±0.5 / +1）
    var totalWagered = 0;
    hands.forEach(function (h) {
      totalNet += h.net * h.bet;      // net（相对手注倍数）× 手注 = 该手玩家净/输（首注=baseBet）
      totalWagered += h.bet;          // 含加倍/分牌加注
    });
    return {
      hands: hands, dealerCards: dealerCards, dealerBJ: dealerBJ, playerBJ: playerBJ,
      insuranceNet: insuranceNet, totalNet: totalNet, totalWagered: totalWagered, baseBet: baseBet
    };
  }

  return {
    CFG: CFG, SUITS: SUITS, RANKS: RANKS,
    rankValue: rankValue, cardCode: cardCode, buildShoe: buildShoe, isCardCode: isCardCode,
    handValue: handValue, isBlackjack: isBlackjack, isPair: isPair,
    shuffle: shuffle, dealerShouldHit: dealerShouldHit, dealerPlay: dealerPlay,
    settleHand: settleHand, basicStrategy: basicStrategy, playRound: playRound
  };
});
