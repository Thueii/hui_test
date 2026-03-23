# user_data/strategies/meme_scalp.py
# ruff: noqa: RUF002, RUF003
import logging
import math
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy


logger = logging.getLogger(__name__)


class ScalpV2(IStrategy):
    """
    MEME/USDT scalping (1m):
    - Entry: uptrend + expanded volatility
    - Exit: place a LIMIT TP immediately after entry at (open_rate + ABS_TP)
            If unfilled for 1 minute -> cancel and emergency_exit MARKET.
    """

    # ===== Core pacing =====
    timeframe = "1m"

    process_only_new_candles = False  # 允许未收盘期间反复评估，更灵敏
    startup_candle_count = 5  # 够用以计算BB/ATR等, 之后设置成 50

    # [BUG FIX] 原来写的是 informative_timeframes = {"5m": "5m"}，但这个属性
    # freqtrade 根本不识别，导致 5m 数据从未被订阅，日志一直报 "No data found for (xxx, 5m)"
    # 正确做法是重写 informative_pairs() 方法，freqtrade 启动时会调用它来决定订阅哪些额外数据
    def informative_pairs(self):
        """告诉 freqtrade 需要为白名单每个交易对额外缓存 5m K线"""
        pairs = self.dp.current_whitelist()
        return [(pair, "5m") for pair in pairs]

    # ===== Risk / ROI =====
    minimal_roi = {"0": 1}  # 基本等于不靠 ROI 卖出（由自定义退出主导）
    stoploss = -0.99  # 固定兜底止损（亏10%强平）
    use_custom_stoploss = True  # 打开自定义止损
    trailing_stop = False  # 本策略由"入场即挂TP + 超时兜底"主导，不启用追踪止盈

    # ===== Order types =====
    # - 入场用市价，保证成交
    # - 出场用限价（我们会在 custom_exit/custom_exit_price 返回），若超时则走 emergency_exit=market
    order_types = {
        "entry": "market",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        # 如需把止损挂到交易所，打开下面两行（可选，增强容错，机器人宕机也会触发止损）
        "stoploss_on_exchange": False,
        # "stoploss_on_exchange_interval": 30,
    }

    # 限价卖单超时设置：1分钟没成交 -> 触发一次超时 -> emergency_exit=market 兜底
    unfilledtimeout = {
        "entry": 2,
        "exit": 4,  # 限价卖单 4 分钟未成就超时
        "exit_timeout_count": 1,  # 发生一次超时后，走 emergency_exit（市价）
        "unit": "minutes",
    }

    # ===== 指标 =====
    # 秒级缓冲：每个交易对一条 deque，存 (ts, price, vol_cum)
    _secbuf: dict = defaultdict(lambda: deque(maxlen=120))

    def _update_second_buffer(self, pair: str, price: float, vol_cum: float):
        now = datetime.now(UTC)
        buf = self._secbuf[pair]

        # 只在时间推进时写入（避免同一秒重复）
        if not buf or (now - buf[-1][0]).total_seconds() >= 1.0:
            buf.append((now, price, vol_cum))

    def _micro_metrics(self, pair: str):
        """
        返回 (micro_volatility_10s, micro_vol_ratio_10s)
        若数据不足则返回 (0.0, 0.0)
        """
        buf = self._secbuf[pair]
        if not buf:
            return 0.0, 0.0

        now = buf[-1][0]
        win10 = [x for x in buf if (now - x[0]).total_seconds() <= 10]
        win60 = [x for x in buf if (now - x[0]).total_seconds() <= 60]

        if len(win10) < 2 or len(win60) < 2:
            return 0.0, 0.0

        # 10秒波动（价差/当前价）
        prices10 = [p for _, p, _ in win10]
        last_price = prices10[-1]
        micro_volatility_10s = (max(prices10) - min(prices10)) / last_price if last_price else 0.0

        # 体量：把"当前K线累计量"的增量近似当作每秒成交量
        def vol_delta(seq):
            # 用相邻快照的差分求和
            total = 0.0
            for i in range(1, len(seq)):
                dv = seq[i][2] - seq[i - 1][2]
                if dv > 0:
                    total += dv
            return total

        vol10 = vol_delta(win10)
        vol60 = vol_delta(win60)
        # 60秒的每10秒平均量 = vol60 / 6
        base10 = (vol60 / 6.0) if vol60 > 0 else 1e-9
        micro_vol_ratio_10s = vol10 / base10

        return micro_volatility_10s, micro_vol_ratio_10s

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        pair = metadata["pair"]
        if not dataframe.empty:
            last_close = float(dataframe["close"].iloc[-1])
            last_vol_cum = float(dataframe["volume"].iloc[-1])
            self._update_second_buffer(pair, last_close, last_vol_cum)
            mvol, mratio = self._micro_metrics(pair)
            logger.info(f"[{pair}] micro_vol={mvol:.6f}(need>0.004) ratio={mratio:.2f}(need>1.8)")
            dataframe.loc[dataframe.index[-1], "micro_volatility_10s"] = mvol
            dataframe.loc[dataframe.index[-1], "micro_vol_ratio_10s"] = mratio

        dataframe["micro_volatility_10s"] = dataframe.get("micro_volatility_10s", 0).fillna(0.0)
        dataframe["micro_vol_ratio_10s"] = dataframe.get("micro_vol_ratio_10s", 0).fillna(0.0)

        # 用 5m，且加"空表/缺列保护"
        inf_tf = self.dp.get_pair_dataframe(pair=pair, timeframe="5m")
        if inf_tf is not None and (not inf_tf.empty) and {"date", "close"}.issubset(inf_tf.columns):
            inf_tf["ma2_5m"] = ta.SMA(inf_tf["close"], timeperiod=2)
            use_cols = inf_tf[["date", "close", "ma2_5m"]].rename(columns={"close": "close_5m"})
            dataframe = dataframe.merge(use_cols, on="date", how="left")
            dataframe[["close_5m", "ma2_5m"]] = dataframe[["close_5m", "ma2_5m"]].ffill()
        else:
            # 占位，避免刚启动时报错
            dataframe["close_5m"] = dataframe.get("close_5m", dataframe["close"])
            dataframe["ma2_5m"] = dataframe.get("ma2_5m", dataframe["close"])

        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # 例：10秒内价差超过 0.4%，且 10秒放量是60秒均值的1.8倍，且5m顺势
        cond = (
            (dataframe["micro_volatility_10s"] > 0.0040)
            & (dataframe["micro_vol_ratio_10s"] > 1.8)
            & (dataframe["close_5m"] > dataframe["ma2_5m"])  # 顺势过滤用 5m
        )
        dataframe.loc[cond, "enter_long"] = 1
        return dataframe

    # [BUG FIX] 原来的参数列表少了 leverage/entry_tag/side，且 min_stake 类型写死为 float
    # 导致 mypy 报 "Signature incompatible with supertype"，实际运行中 freqtrade 传 None 会崩
    # 改为与父类 IStrategy.custom_stake_amount 签名完全一致
    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,  # [BUG FIX] 原来是 float，父类是 float | None
        max_stake: float,
        leverage: float,  # [BUG FIX] 原来缺少这个参数
        entry_tag: str | None,  # [BUG FIX] 原来缺少这个参数
        side: str,  # [BUG FIX] 原来缺少这个参数
        **kwargs: Any,
    ) -> float:
        """
        proposed_stake: 现有的钱
        max_stake: 交易所允许的最大下单金额，是交易所返回的，不是需要我们配置的
        保守买入：按 current_rate + buffer 计算数量，确保买到的是100倍数个币。
        """
        if current_rate <= 0:
            return 0.0

        # 给买入价加一个 buffer，避免市价单实际成交价偏高时买超
        buffer_price = current_rate + 0.0005

        # 理论可以买多少个币
        raw_amount = proposed_stake / buffer_price

        # 向下取整为100的倍数
        amount = (int(raw_amount) // 100) * 100

        if amount <= 0:
            return 0.0

        # 换算回USDT金额
        stake = amount * current_rate  # 换回 current_rate

        # 保证在范围内
        if (
            min_stake is not None and stake < min_stake
        ):  # [BUG FIX] 原来直接 stake < min_stake，min_stake 为 None 时会报错
            return 0.0

        if stake > max_stake:
            stake = max_stake
            amount = (int(stake / buffer_price) // 100) * 100
            stake = amount * buffer_price

        logger.info(f"===============amount: {amount}, price: {current_rate}, stake: {stake}")
        return float(stake)

    # ===== 退出信号：入场后立即挂限价TP =====
    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        # 浮亏超过阈值立刻退出，防止横盘缓慢下跌超过止损范围
        # current_profit 是 freqtrade 计算的含手续费净利润
        PANIC_SL = -0.001  # 与 custom_stoploss 的 REAL_STOPLOSS 保持一致
        if current_profit <= PANIC_SL:
            return "panic_sl"  # 触发即走，freqtrade 会撤掉原 TP，再下市价平仓

        # 只在没有任何退出单时，挂一次限价 TP
        if trade.exit_order_status is None:
            return "immediate_tp"  # TP 标签

        return None

    # [BUG FIX] 原来参数名是 current_rate（可选），但父类签名是 proposed_rate（必填）
    # 且原来返回类型是 Optional[float]，父类要求 float，不一致会导致 mypy 报错
    def custom_exit_price(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        proposed_rate: float,  # [BUG FIX] 原来是 current_rate: Optional[float] = None
        current_profit: float,  # [BUG FIX] 原来是 Optional[float] = None
        exit_tag: str | None,  # [BUG FIX] 原来缺少这个参数
        **kwargs: Any,
    ) -> float:  # [BUG FIX] 原来是 Optional[float]，父类要求 float
        """
        返回限价卖出价格：
        - 至少覆盖双边手续费 (fee * 2)
        - 再加一个额外 buffer (extra_profit_pct)
        """
        # 获取交易所配置的手续费率 (默认0.001=0.1%)
        fee = 0.001

        # 需要的最小涨幅 = 双边费率
        min_required = fee * 2

        # 额外想要的净利幅度 (0.001=0.1% 净利润)
        extra_profit_pct = 0.001

        # 最终止盈幅度
        tp_pct = min_required + extra_profit_pct

        # 目标价
        target_origin = trade.open_rate * (1 + tp_pct)
        target = math.ceil(target_origin * 100000) / 100000.0

        # # 对齐到交易所精度
        # try:
        #     m = self.dp.market(pair)  # 获取交易所市场规则
        #     tick = m["limits"]["price"].get("min") or 0
        #     if tick > 0:
        #         target = (int(target / tick)) * tick
        # except Exception:
        #     pass  # 如果 dp 不可用，就不对齐

        logger.info(
            f"=======buy: {trade.open_rate}, "
            f"equal_fee_price: {(1 + min_required) * trade.open_rate}, "
            f"add_profit_price_origin: {target_origin}, target: {target}"
        )
        return float(target)

    # 不使用规则化的 exit_trend（全部交给 custom_exit 系统）
    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["exit_long"] = 0
        df["exit_tag"] = ""
        return df

    # [BUG FIX] 原来缺少 after_fill 参数，且 trade/current_time 等没有类型注解
    # 与父类签名不一致，mypy 报错
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,  # [BUG FIX] 原来缺少这个参数
        **kwargs: Any,
    ) -> float | None:
        """
        返回当前应生效的止损（负数，单位=相对开仓价的比例）
        需求：建仓后 30 秒内不触发（给一个极宽的止损以"等效忽略"）
        """
        # 计算建仓至今的秒数
        held_seconds = (current_time - trade.open_date_utc).total_seconds()

        GRACE_SECONDS = 30
        REAL_STOPLOSS = -0.001  # 价格止损 -0.1%（含手续费总亏损约 -0.3%）

        if held_seconds < GRACE_SECONDS:
            # 宽限期内：给一个极宽的止损，基本不可能被打到
            return -0.99  # -99%，等效"先不止损"
        else:
            # 宽限期结束：恢复到真实止损
            return REAL_STOPLOSS
