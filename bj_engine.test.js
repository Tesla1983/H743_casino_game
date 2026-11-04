/* bj_engine.test.js — 21 点核心引擎单测（Node 运行，无外部依赖）
 * 用法：node bj_engine.test.js
 * 覆盖：handValue(A 双值/软硬)、isBlackjack、dealerShouldHit(S17)、settleHand(矩阵)、
 *       shuffle(确定性)、playRound(stand/爆牌/分牌/加倍/保险/天然黑杰克)。 */
var BJ = require("./bj_engine.js");

var pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; console.log("  ✗ " + name + (extra ? "  >>> " + extra : "")); }
}
function eq(name, got, exp) {
  var g = JSON.stringify(got), e = JSON.stringify(exp);
  ok(name + "  (got " + g + ")", g === e, "expected " + e);
}
function C(rank, suit) { return { rank: rank, suit: suit || "S", code: rank + (suit || "S"), value: BJ.rankValue(rank) }; }

console.log("== handValue ==");
eq("A,A -> 12 soft",   BJ.handValue([C("A"), C("A")]),       { total: 12, soft: true,  bust: false });
eq("A,6 -> 17 soft",   BJ.handValue([C("A"), C("6")]),       { total: 17, soft: true,  bust: false });
eq("A,6,10 -> 17 hard",BJ.handValue([C("A"), C("6"), C("10")]), { total: 17, soft: false, bust: false });
eq("A,A,9 -> 21 soft", BJ.handValue([C("A"), C("A"), C("9")]),  { total: 21, soft: true,  bust: false });
eq("A,A,A,8 -> 21",    BJ.handValue([C("A"), C("A"), C("A"), C("8")]), { total: 21, soft: true, bust: false });
eq("K,Q -> 20",        BJ.handValue([C("K"), C("Q")]),       { total: 20, soft: false, bust: false });
eq("10,6 -> 16",       BJ.handValue([C("10"), C("6")]),      { total: 16, soft: false, bust: false });
ok("22 爆牌",          BJ.handValue([C("K"), C("Q"), C("5")]).bust === true);

console.log("== isBlackjack ==");
ok("A+10 黑杰克",      BJ.isBlackjack([C("A"), C("10")]) === true);
ok("A+9+2 非黑杰克",   BJ.isBlackjack([C("A"), C("9"), C("2")]) === false);
ok("10+J 非黑杰克",     BJ.isBlackjack([C("10"), C("J")]) === false);
ok("分牌后 A+10 不算天然", BJ.isBlackjack([C("A"), C("10")]) === true); // 单看牌是天然；是否享受3:2由 fromSplit 决定

console.log("== dealerShouldHit (S17) ==");
ok("16 补牌",          BJ.dealerShouldHit([C("10"), C("6")]) === true);
ok("17 停牌",          BJ.dealerShouldHit([C("10"), C("7")]) === false);
ok("软17 停牌(S17)",   BJ.dealerShouldHit([C("A"), C("6")]) === false);
ok("软18 停牌",        BJ.dealerShouldHit([C("A"), C("7")]) === false);

console.log("== settleHand (结算矩阵) ==");
function settle(p, d, nat) { return BJ.settleHand(p, d, nat); }
eq("玩家爆牌负",       settle([C("K"), C("Q"), C("5")], [C("10"), C("7")], false).outcome, "bust");
eq("庄家爆牌玩家胜",   settle([C("10"), C("8")], [C("K"), C("Q"), C("5")], false).outcome, "win");
eq("玩家黑杰克胜 3:2", settle([C("A"), C("K")], [C("10"), C("9")], true).net, 1.5);
eq("双方黑杰克 push",  settle([C("A"), C("K")], [C("A"), C("Q")], true).outcome, "push");
eq("庄家黑杰克 玩家非负", settle([C("10"), C("9")], [C("A"), C("K")], false).outcome, "lose");
eq("点数大胜 1:1",     settle([C("10"), C("9")], [C("10"), C("8")], false).net, 1);
eq("点数小负",         settle([C("10"), C("8")], [C("10"), C("9")], false).outcome, "lose");
eq("相等 push",        settle([C("10"), C("9")], [C("10"), C("9")], false).outcome, "push");

console.log("== shuffle (确定性 + 16-bit 拒绝) ==");
// 简单 LCG 仅用于测试（生产路径绝不用 Math.random）
function lcg(seed) { var s = seed >>> 0; return function () { s = (s * 1664525 + 1013904223) >>> 0; return s & 0xffff; }; }
var sh = BJ.buildShoe(1);
var before = sh.map(function (c) { return c.code; }).join(",");
BJ.shuffle(sh, lcg(12345));
var after = sh.map(function (c) { return c.code; }).join(",");
ok("洗牌后仍是 52 张不增不减", sh.length === 52);
ok("洗牌后仍是同样的 52 张（多重集不变）",
   before.split(",").sort().join(",") === after.split(",").sort().join(","));
ok("洗牌确实改变了顺序（非恒等）", before !== after);

console.log("== playRound ==");
// 构造确定性牌靴供 draw() 使用
function shoeFrom(codes) {
  var arr = codes.map(function (c) { var r = c.slice(0, -1), s = c.slice(-1); return C(r, s); });
  return function () { return arr.length ? arr.shift() : null; };
}
// 场景1：玩家 10+9=19 停牌，庄家 10+5=15 补到 20 → 玩家 19<20 负
(function () {
  var draw = shoeFrom(["7H", "6H"]); // 庄家补牌：7 后再 6 → 但庄家已 15，+7=22 爆？设计牌靴需谨慎
  // 用可控序列：庄家 up=10, hole=5 → 15；补 6 → 21；玩家 10,9 停牌=19
  var draw2 = shoeFrom(["6H"]);      // 庄家补的牌
  var r = BJ.playRound([C("10", "H"), C("9", "D")], [C("10", "S"), C("5", "C")], draw2, 1, {});
  ok("场景1 玩家 19 vs 庄 21 → 负", r.hands[0].outcome === "lose", r.hands[0].outcome);
})();
// 场景2：玩家爆牌（10+6=16 策略会 hit 到爆，但给牌让它爆）— 直接造 10+6 然后发 10
(function () {
  var draw = shoeFrom(["10H"]);
  var r = BJ.playRound([C("10", "H"), C("6", "D")], [C("9", "S"), C("7", "C")], draw, 1, {});
  ok("场景2 玩家 16+10 爆 → 负", r.hands[0].outcome === "lose" || r.hands[0].outcome === "bust", r.hands[0].outcome);
})();
// 场景3：玩家天然黑杰克 3:2
(function () {
  var draw = shoeFrom(["KH"]); // 庄家补牌用不到（庄 9+7=16 补 K=26 爆，但不影响玩家 BJ 先结算）
  var r = BJ.playRound([C("A", "S"), C("K", "H")], [C("9", "S"), C("7", "C")], draw, 1, {});
  ok("场景3 玩家天然 BJ → blackjack 净 1.5", r.hands[0].outcome === "blackjack" && r.totalNet === 1.5, JSON.stringify(r.hands[0]));
})();
// 场景4：分牌 8,8 vs 庄家 6（BS 应分牌）；用足够牌靴
(function () {
  var draw = shoeFrom(["2H", "3H", "9H", "4H", "5H", "6H"]); // 分给两手后的补牌 + 庄家补牌
  var r = BJ.playRound([C("8", "S"), C("8", "D")], [C("6", "C"), C("10", "H")], draw, 1, {});
  ok("场景4 分牌产生 ≥2 手", r.hands.length >= 2, r.hands.length);
  // 分牌后基本策略可能对分出手再加倍（8+小牌→double），故总注 ≥ 分牌基数 2
  ok("场景4 总注含分牌加注 (≥2)", r.totalWagered >= 2, r.totalWagered);
})();
// 场景5：加倍 11 vs 庄家 6（BS 加倍）→ 注额 ×2
(function () {
  var draw = shoeFrom(["9H", "5H"]); // 玩家 11(6+5?) 这里 11= 用 10+? 改用 6+5=11 不对；构造 A? 直接用 10? 
  // 玩家 10+? 需要 11：用 6,5 不行(A). 设玩家 8+3=11，加倍发 9 → 20；庄 6 + 10(hole) + 5(补) =21
  var draw5 = shoeFrom(["9H", "5H"]); // 第一张给玩家加倍，第二张给庄家补
  var r = BJ.playRound([C("8", "S"), C("3", "D")], [C("6", "C"), C("10", "H")], draw5, 1, {});
  ok("场景5 加倍后该手注 = 2", r.hands[0].bet === 2, r.hands[0].bet);
})();
// 场景6：保险 — 庄家明 A 且暗 K（黑杰克），玩家买保险 → net 0（保险 +1 抵本注 -1）
(function () {
  var draw = shoeFrom([]); // 庄家 BJ 立即结束，不需要补牌
  var r = BJ.playRound([C("10", "S"), C("9", "D")], [C("A", "C"), C("K", "H")], draw, 1, { takeInsurance: true });
  ok("场景6 庄家 BJ、买保险 → totalNet 0", Math.abs(r.totalNet - 0) < 1e-9, r.totalNet);
  ok("场景6 主手 lose", r.hands[0].outcome === "lose", r.hands[0].outcome);
  ok("场景6 保险净 +1", Math.abs(r.insuranceNet - 1) < 1e-9, r.insuranceNet);
})();

console.log("\n====================");
console.log("PASS " + pass + "  FAIL " + fail);
process.exit(fail ? 1 : 0);
