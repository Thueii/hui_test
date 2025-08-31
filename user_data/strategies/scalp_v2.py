# user_data/strategies/meme_scalp.py
from typing import Optional
import pandas as pd
import logging
import talib.abstract as ta
from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.persistence import Trade
import math
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

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
    informative_timeframes = {"5m": "5m"}

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
        "exit": 4,  # 限价卖单 4 分钟未成就超时
        "exit_timeout_count": 1,  # 发生一次超时后，走 emergency_exit（市价）
        "unit": "minutes",
    }

    # ===== 指标 =====
    # 秒级缓冲：每个交易对一条 deque，存 (ts, price, vol_cum)
    _secbuf = defaultdict(lambda: deque(maxlen=120))

    def _update_second_buffer(self, pair: str, price: float, vol_cum: float):
        now = datetime.now(timezone.utc)
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

        # 体量：把“当前K线累计量”的增量近似当作每秒成交量
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
            # 取“正在形成”的最后一行快照
            last_close = float(dataframe["close"].iloc[-1])
            # 注意：这里的 volume 是“该1m烛的累计量”，非全局累计
            last_vol_cum = float(dataframe["volume"].iloc[-1])

            # 更新秒级缓冲
            self._update_second_buffer(pair, last_close, last_vol_cum)

            # 计算秒级指标，只写到最后一行
            mvol, mratio = self._micro_metrics(pair)
            dataframe.loc[dataframe.index[-1], "micro_volatility_10s"] = mvol
            dataframe.loc[dataframe.index[-1], "micro_vol_ratio_10s"] = mratio

        # 给缺失行填0，避免后续条件判断报NaN
        dataframe["micro_volatility_10s"] = dataframe.get("micro_volatility_10s", 0).fillna(0.0)
        dataframe["micro_vol_ratio_10s"] = dataframe.get("micro_vol_ratio_10s", 0).fillna(0.0)
        inf_tf = self.dp.get_pair_dataframe(pair=metadata['pair'], timeframe="5m")
        # 举例：用 5m 均线判断趋势
        inf_tf['ma2'] = ta.SMA(inf_tf['close'], timeperiod=2)
        dataframe = merge_informative_pair(dataframe, inf_tf, self.timeframe, "5m", ffill=True)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # 例：10秒内价差超过 0.12%，且 10秒放量是60秒均值的1.5倍
        cond = (
            (dataframe["micro_volatility_10s"] > 0.0040) & 
            (dataframe["micro_vol_ratio_10s"] > 1.8) & 
            (dataframe["close_5m"] > dataframe["ma5_2m"])
        )
        dataframe.loc[cond, "enter_long"] = 1
        return dataframe

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
