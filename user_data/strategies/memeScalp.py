# user_data/strategies/meme_scalp.py
from typing import Optional
import pandas as pd
import logging
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade

logger = logging.getLogger(__name__)


class MemeScalp(IStrategy):
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
    stoploss = -0.10  # 固定兜底止损（亏10%强平）
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
        "exit": 1,  # 限价卖单 1 分钟未成就超时
        "exit_timeout_count": 2,  # 发生一次超时后，走 emergency_exit（市价）
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
        logger.info("===============00")

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
        保守买入：按 current_rate + buffer 计算数量，确保买到的是100倍数个币。
        """
        if current_rate <= 0:
            logger.info("===============11")
            return 0.0

        # 给买入价加一个 buffer，避免市价单实际成交价偏高时买超
        buffer_price = current_rate + 0.0005

        # 理论可以买多少个 MEME
        raw_amount = proposed_stake / buffer_price

        # 向下取整为100的倍数
        amount = (int(raw_amount) // 100) * 100

        if amount <= 0:
            logger.info("===============22")

            return 0.0

        # 换算回USDT金额
        stake = amount * buffer_price

        # 保证在范围内
        if stake < min_stake:
            logger.info("===============33")

            return 0.0
        if stake > max_stake:
            stake = max_stake
            amount = (int(stake / buffer_price) // 100) * 100
            stake = amount * buffer_price

        logger.info("===============44")
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
        """
        方法A：不等待“到价再挂单”，而是开仓后立即给出退出信号，
        由 custom_exit_price 返回具体限价 -> 机器人马上在交易所挂限价卖单。
        """
        # 只要有持仓，就返回一个退出理由，让框架按 custom_exit_price 挂TP限价单
        if trade.exit_order_status is None:
            return "immediate_tp"
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
        返回限价卖出价格：买入均价 + 绝对价差（与版本无关）
        兼容：某些 Freqtrade 版本不再显式传 current_rate / current_profit
        """
        # 新版可能把 rate 放在 kwargs
        if current_rate is None:
            current_rate = kwargs.get("current_rate")

        # 我们本就用 open_rate 做基准，不强依赖 current_rate
        target = ((0.03 / trade.amount) + 1.001 * trade.open_rate) / 0.999
        # target = trade.open_rate + self.ABS_TP
        return float(target)

    # 不使用规则化的 exit_trend（全部交给 custom_exit 系统）
    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["exit_long"] = 0
        df["exit_tag"] = ""
        return df
