# user_data/strategies/meme_scalp.py
from functools import reduce
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as pta
import talib.abstract as ta

from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.persistence import Trade


class MemeScalp(IStrategy):
    """
    MEME/USDT scalping:
    - timeframe: 1m
    - Entry: uptrend + expanded volatility
    - Exit: limit TP at open_rate + 0.000002; 1m timeout -> market
    """

    timeframe = "1m"
    # 让最后一根K线未收盘也会反复评估（更灵敏）
    process_only_new_candles = False

    startup_candle_count = 50

    minimal_roi = {
        "0": 1  # 基本等于不靠 ROI 卖出，交给自定义退出
    }

    # 固定兜底止损（可按需调整或改成 custom_stoploss）
    stoploss = -0.10

    # 追踪止盈不启用（由我们的限价+超时策略主导）
    trailing_stop = False

    # 订单类型：入场走市价；出场先限价；紧急/超时用市价兜底
    order_types = {
        "entry": "market",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,  # 止损挂到交易所
        "stoploss_on_exchange_interval": 30,
        "stoploss_on_exchange_market_ratio": 0.99,
    }

    # 限价卖单超时控制：1 分钟没成交 -> 触发 emergency_exit (market)
    unfilledtimeout = {
        "entry": 2,  # 仅示例：买单最长等 2 分钟
        "exit": 1,  # 卖单 1 分钟没成交就超时
        "exit_timeout_count": 1,  # 超时一次后，走 emergency_exit=market
        "unit": "minutes",
    }

    # 你的“固定绝对价差”止盈（MEME/USDT）
    ABS_TP = 0.000002

    # ===== 指标计算 =====
    def populate_indicators(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # 动能/趋势
        df["ema_fast"] = ta.EMA(df["close"], timeperiod=9)
        df["ema_slow"] = ta.EMA(df["close"], timeperiod=21)
        df["rsi"] = ta.RSI(df["close"], timeperiod=14)

        # 波动：布林带宽度 + ATR 相对幅度
        bb = ta.BBANDS(df["close"], timeperiod=20, nbdevup=2, nbdevdn=2)
        df["bb_width"] = (bb["upperband"] - bb["lowerband"]) / df["close"]
        df["atr"] = ta.ATR(df["high"], df["low"], df["close"], timeperiod=14)
        df["atr_pct"] = df["atr"] / df["close"]

        # RSI 斜率（上升趋势）
        df["rsi_slope"] = df["rsi"] - df["rsi"].shift(1)

        return df

    # ===== 入场规则 =====
    def populate_entry_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["enter_long"] = 0
        df["enter_tag"] = ""

        # 顺势：短均线在长均线上方；RSI 在 45~70 且在上升
        trend_ok = (
            (df["ema_fast"] > df["ema_slow"]) & 
            (df["rsi"].between(45, 70)) & 
            (df["rsi_slope"] > 0)
        )

        # 有波动：布林带变宽 or ATR 较高（阈值可微调）
        vol_ok = (
            (df["bb_width"] > 0.01) |  # ~1% 带宽
            (df["atr_pct"] > 0.005)  # ~0.5% 的 ATR
        )

        cond = trend_ok & vol_ok
        df.loc[cond, "enter_long"] = 1
        df.loc[cond, "enter_tag"] = "trend_vol"

        return df

    # ===== 退出规则（信号层：何时触发卖出） =====
    def custom_exit(
        self, pair: str, trade: Trade, current_time: "datetime",
        current_rate: float, current_profit: float, **kwargs
    ) -> Optional[str]:
        """
        当现价 >= (买入均价 + ABS_TP) 时触发限价卖出信号。
        实际卖出价格在 custom_exit_price 里指定。
        """
        target = trade.open_rate + self.ABS_TP

        # 触发条件：现价达到或超过目标（也可加入时间窗口等附加条件）
        if current_rate >= target:
            return "tp_abs_hit"

        # 也可以添加“超时平仓”逻辑（例如持仓超过 N 分钟也触发卖出信号）
        # 按你现在的设想，主要靠 unfilledtimeout -> emergency_exit 处理，不必在这里重复。

        return None

    # ===== 退出价格（执行层：以什么价格卖） =====
    def custom_exit_price(
        self, pair: str, trade: Trade, current_time: "datetime",
        current_rate: float, current_profit: float, **kwargs
    ) -> Optional[float]:
        """
        返回限价卖单的价格：open_rate + ABS_TP
        Freqtrade 会按交易所精度自动处理小数位/步进。
        """
        price = trade.open_rate + self.ABS_TP
        return float(price)

    # 简化：不使用 populate_exit_trend（交给 custom_exit 系统）
    def populate_exit_trend(self, df: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df["exit_long"] = 0
        df["exit_tag"] = ""
        return df
