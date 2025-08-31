# user_data/strategies/meme_scalp.py
from typing import Optional
import pandas as pd
import logging
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade
import math

logger = logging.getLogger(__name__)


class ScalpV1(IStrategy):
    """
    MEME/USDT scalping (1m):
    - Entry: uptrend + expanded volatility
    - Exit: place a LIMIT TP immediately after entry at (open_rate + ABS_TP)
            If unfilled for 1 minute -> cancel and emergency_exit MARKET.
    """
    # ===== Core pacing =====
    timeframe = "1m"
    process_only_new_candles = False  # 允许未收盘期间反复评估，更灵敏
    startup_candle_count = 1  # 够用以计算BB/ATR等, 之后设置成 50

    # ===== Risk / ROI =====
    minimal_roi = {"0": 1}  # 基本等于不靠 ROI 卖出（由自定义退出主导）
    stoploss = -0.99  # 固定兜底止损（亏10%强平）
    use_custom_stoploss = True  # 打开自定义止损
    trailing_stop = False  # 本策略由“入场即挂TP + 超时兜底”主导，不启用追踪止盈

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
        "exit": 2,  # 限价卖单 2 分钟未成就超时
        "exit_timeout_count": 1,  # 发生一次超时后，走 emergency_exit（市价）
        "unit": "minutes",
    }

    # 你的“绝对价差”止盈（基于买入均价 open_rate）
    ABS_TP = 0.0004

    # ===== 指标 =====
    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # 趋势/动能
        df["ema_fast"] = ta.EMA(df["close"], timeperiod=9)
        df["ema_slow"] = ta.EMA(df["close"], timeperiod=21)
        df["rsi"] = ta.RSI(df["close"], timeperiod=14)
        df["rsi_slope"] = df["rsi"] - df["rsi"].shift(1)

        # 波动：布林带宽度 / ATR百分比
        upper, middle, lower = ta.BBANDS(
            df["close"], timeperiod=20, nbdevup=2.0, nbdevdn=2.0
        )
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["close"]
        df["atr"] = ta.ATR(df["high"], df["low"], df["close"], timeperiod=14)
        df["atr_pct"] = df["atr"] / df["close"]

        return df

    # ===== 入场：顺势 + 有波动 =====
    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"] = 0
        df["enter_tag"] = ""

        trend_ok = (
            (df["ema_fast"] > df["ema_slow"]) & 
            (df["rsi"].between(45, 70)) & 
            (df["rsi_slope"] > 0)  # RSI 正在上行
        )
        vol_ok = (
            (df["bb_width"] > 0.01) |  # 布林带宽度>1% 说明带宽扩张
            (df["atr_pct"] > 0.005)  # ATR>0.5% 有可吃的波动
        )

        cond = trend_ok & vol_ok
        df.loc[cond, "enter_long"] = 1
        df.loc[cond, "enter_tag"] = "trend_vol"

        return df

    def custom_stake_amount(
        self, pair: str, current_time, current_rate: float,
        proposed_stake: float, min_stake: float, max_stake: float, **kwargs
        ) -> float:
        """
        proposed_stake: 现有的钱
        max_stake: 交易所允许的最大下单金额,是交易所返回的,不是需要我们配置的
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
        if stake < min_stake:
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
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> Optional[str]:
        # 只在还没有退出订单时，挂一次TP
        GRACE_SECONDS = 30  # 建仓后前 30 秒不触发“亏损退出”
        PANIC_SL = -0.0015  # -0.15%

        # 1) 只在没有任何退出单时，挂一次限价 TP（你原来的逻辑）
        if trade.exit_order_status is None:
            return "immediate_tp"  # 你的 TP 标签，不改变

        # # 2) 过了 30 秒，若浮亏超过阈值，则立刻触发退出（市价）
        # held_seconds = (current_time - trade.open_date_utc).total_seconds()
        # if held_seconds >= GRACE_SECONDS and current_profit <= PANIC_SL:
        #     # 提示：部分版本可返回 ("exit_signal", "panic_sl")
        #     return "panic_sl"      # 触发即走，Freqtrade 会撤掉原 TP，再下市价平仓

        return None

    def custom_exit_price(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: Optional[float]=None,  # 设为可选
        current_profit: Optional[float]=None,  # 设为可选
        ** kwargs,
    ) -> Optional[float]:
        """
        返回限价卖出价格：
        - 至少覆盖双边手续费 (fee * 2)
        - 再加一个额外 buffer (extra_profit_pct)
        """
        # 获取交易所配置的手续费率 (默认0.001=0.1%)
        fee = 0.001

        # 需要的最小涨幅 = 双边费率
        min_required = fee * 2

        # 额外想要的净利幅度 (比如 0.001=0.1%)
        extra_profit_pct = 0.000008

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
        logger.info(f"=======buy: {trade.open_rate}, equal_fee_price: {(1+min_required) * trade.open_rate}, add_profit_price_origin: {target_origin}, target: {target}")
        return float(target)

    # 不使用规则化的 exit_trend（全部交给 custom_exit 系统）
    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["exit_long"] = 0
        df["exit_tag"] = ""
        return df

    def custom_stoploss(
        self,
        pair: str,
        trade,
        current_time,
        current_rate,
        current_profit,
        **kwargs
    ) -> float:
        """
        返回当前应生效的止损（负数，单位=相对开仓价的比例）
        需求：建仓后 30 秒内不触发（给一个极宽的止损以“等效忽略”）
        """
        # 计算建仓至今的秒数
        held_seconds = (current_time - trade.open_date_utc).total_seconds()

        GRACE_SECONDS = 30
        REAL_STOPLOSS = -0.0015  # 你的真实止损（-0.15%）

        if held_seconds < GRACE_SECONDS:
            # 宽限期内：给一个极宽的止损，基本不可能被打到
            return -0.99  # -99%，等效“先不止损”
        else:
            # 宽限期结束：恢复到真实止损
            return REAL_STOPLOSS
