# freqtrade 策略运行机制

## 主循环

freqtrade 启动后会持续运行一个主循环，间隔由 `internals.process_throttle_secs` 控制（config.json 里设的是 **1秒**）。

```
每秒触发一次
  ├── 拉取最新 K 线数据（1m + 5m）
  ├── 对白名单每个交易对跑策略函数
  └── 检查所有持仓的订单状态
```

> `process_only_new_candles = False` 让策略每秒都跑，而不是等 1m K 线收盘才跑一次。
> 这是 scalp_v2 能感知秒级变化的关键。

---

## 各函数调用时机

### 启动时（只调用一次）

| 函数 | 作用 |
|------|------|
| `informative_pairs()` | 告诉框架需要订阅哪些额外数据，scalp_v2 在这里注册 5m K 线 |

### 每秒循环（无持仓时）

```
populate_indicators()
    └── populate_entry_trend()
            └── 有 enter_long=1 且持仓数 < max_open_trades?
                    └── custom_stake_amount()  →  下市价买单
```

### 每秒循环（有持仓时）

```
populate_indicators()
    └── populate_entry_trend()  （同上，判断是否再开新仓）

对每个持仓：
    ├── custom_stoploss()       更新止损价格
    └── custom_exit()
            └── 返回退出信号?
                    └── custom_exit_price()  →  挂限价卖单
```

---

## 函数详解

### `populate_indicators(dataframe, metadata)`
- **何时调用**：每次有新数据时
- **作用**：计算指标，结果写入 dataframe 新列
- **scalp_v2 做了什么**：
  - 把当前 close 价写入 `_secbuf`（秒级缓冲区）
  - 计算 `micro_volatility_10s`：10秒内价差/当前价
  - 计算 `micro_vol_ratio_10s`：10秒成交量 vs 60秒均值的比值
  - 合并 5m 数据，计算 `ma2_5m`

### `populate_entry_trend(dataframe, metadata)`
- **何时调用**：紧接着 `populate_indicators()` 之后
- **作用**：读取指标列，在满足条件的行打 `enter_long = 1`
- **scalp_v2 的入场条件**：
  ```
  micro_volatility_10s > 0.004   （10秒价差超过 0.4%）
  micro_vol_ratio_10s  > 1.8     （10秒放量是60秒均值的1.8倍）
  close_5m > ma2_5m              （5m 顺势，价格在均线上方）
  ```

### `populate_exit_trend(dataframe, metadata)`
- **何时调用**：紧接着 `populate_entry_trend()` 之后
- **scalp_v2**：直接返回 0，退出完全由 `custom_exit()` 控制

### `custom_stake_amount(...)`
- **何时调用**：有入场信号，准备下单前
- **作用**：决定这笔买多少钱（USDT）
- **scalp_v2 做了什么**：
  - 按 `proposed_stake / price` 算出理论可买数量
  - 向下取整为 **100 的整数倍**（避免持有零头无法卖出）
  - 换算回 USDT 返回

### `custom_exit(pair, trade, current_time, current_rate, current_profit)`
- **何时调用**：每秒对每个持仓调用一次
- **返回值**：
  - 返回字符串 → 触发退出（字符串作为退出原因标签）
  - 返回 `None` → 继续持有
- **scalp_v2 做了什么**：
  - 如果还没有挂出退出单（`trade.exit_order_status is None`），返回 `"immediate_tp"` 触发挂限价单
  - 之后每次都返回 `None`，等限价单成交或超时

### `custom_exit_price(pair, trade, current_time, proposed_rate, current_profit, exit_tag)`
- **何时调用**：`custom_exit()` 触发退出后立即调用，决定限价卖单价格
- **scalp_v2 做了什么**：
  - TP 价格 = 买入价 × (1 + 双边手续费 + 0.0008% 利润)
  - 即：至少覆盖买卖两边的手续费，再赚一点点

### `custom_stoploss(pair, trade, current_time, current_rate, current_profit, after_fill)`
- **何时调用**：每秒对每个持仓调用一次
- **返回值**：负数，代表相对开仓价的止损比例
- **scalp_v2 做了什么**：
  - 建仓后 **30 秒内**：返回 -99%（等效不止损，给 TP 限价单时间成交）
  - 30 秒后：返回 -0.15%（真实止损）

---

## 离场完整链路

```
入场成功，持仓建立
    ↓
第1秒：custom_exit() → trade.exit_order_status is None → 返回 "immediate_tp"
    ↓
custom_exit_price() 计算 TP 价格
    ↓
框架挂出限价卖单
    ↓
情况A：限价单在 4 分钟内成交 → 盈利退出
情况B：4 分钟未成交（unfilledtimeout.exit=4）
    → 触发第1次超时
    → exit_timeout_count=1，走 emergency_exit（市价卖出，保本或小亏）
情况C：30 秒后浮亏超过 0.15% → custom_stoploss 触发止损（市价卖出）
```

---

## 关键配置对应关系

| config.json 配置 | 作用 |
|-----------------|------|
| `max_open_trades: 2` | 最多同时持有 2 个仓位 |
| `process_throttle_secs: 1` | 主循环每秒跑一次 |
| `unfilledtimeout.exit: 4` | 限价卖单 4 分钟未成交触发超时 |
| `exit_timeout_count: 1` | 超时1次后走市价兜底 |
| `emergency_exit: market` | 超时兜底用市价单 |
| `dry_run: true` | 模拟交易，不真实下单 |

---

## _secbuf 秒级缓冲区原理

1m K 线内，close 价格不会变化（只有 K 线收盘才更新），直接用 K 线数据无法感知秒级波动。

`_secbuf` 的做法：
- 每秒把当前 close 价和累计成交量存入一个滑动窗口（最多120条）
- 计算10秒内价格的高低差（`micro_volatility_10s`）
- 计算10秒内成交量增量 vs 60秒均值（`micro_vol_ratio_10s`）
- 这两个指标才是真正的入场触发条件

```
_secbuf[pair] = deque(maxlen=120)
每条记录 = (timestamp, close_price, cumulative_volume)
```
