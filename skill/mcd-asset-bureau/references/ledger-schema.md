# assets.json 数据契约

`scripts/asset_ledger.py` 的唯一输入。由执行 skill 的模型在"采集 + 估值"之后生成。

**原则：所有拿不到的字段填 `null`，不要填估算值。** 脚本对 `null` 有专门的降级处理，对错误的数字没有。

```json
{
  "as_of": "2026-10-23T16:11:00+08:00",
  "points": {
    "available": 111.7,
    "accumulative": 833.6,
    "expired": 721.9,
    "current_month_expire": 24.8,
    "unit_value": 0.04
  },
  "coupons": [
    {
      "id": "c001",
      "name": "11.9元麦辣鸡腿堡",
      "price_with": 11.9,
      "price_without": 25.0,
      "value": null,
      "expires_at": "2026-10-23T23:59:00+08:00",
      "channels": ["到店", "外送"],
      "time_window": "10:30-23:59",
      "weekdays": [1, 2, 3, 4, 5],
      "note": ""
    }
  ],
  "prizes": [
    {
      "id": "p001",
      "name": "处暑",
      "kind": "门票",
      "status": "可用",
      "expires_at": null,
      "time_remind": "",
      "record_time": "2026-08-27 18:19:18"
    }
  ],
  "windows": [
    {
      "id": "w001",
      "name": "麦金喜抽积分霸王餐",
      "action": "预约抽奖",
      "starts_at": "2026-10-24T00:00:00+08:00",
      "ends_at": "2026-10-24T23:59:00+08:00",
      "expected_value": null,
      "cost": 0
    }
  ]
}
```

## 字段说明

### 顶层
| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `as_of` | ISO8601 带时区 | 是 | 快照时刻。脚本以它为"现在"，保证可复算、可回放 |
| `points` | object / null | 否 | 积分账户 |
| `coupons` | array | 否 | 券资产，可为空数组 |
| `prizes` | array | 否 | 抽奖奖品 |
| `windows` | array | 否 | 零成本正 EV 机会窗口 |

### points
| 字段 | 说明 | 缺失影响 |
|---|---|---|
| `available` | 可用积分 | 无法计入可回收价值 |
| `accumulative` | 累计积分 | 无法算蒸发率 |
| `expired` | 已过期积分 | 蒸发账单为空 |
| `current_month_expire` | 本月将过期 | 少一项 T2 资产 |
| `unit_value` | 每积分价值（元） | 为 null 时只报数量不折金额 |

### coupons[]
| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 券名，用于去重识别 |
| `price_with` | 否 | 用券价 |
| `price_without` | 否 | 同餐品原价；核不出来填 null |
| `value` | 否 | 显式面值。**为 null 时脚本自动用 `price_without - price_with`**；两者都缺 → 面值未知 |
| `expires_at` | 是 | 到期时刻，ISO8601。**这是全脚本的关键字段** |
| `channels` | 否 | `["到店"]` / `["外送"]` / 两者 |
| `time_window` | 否 | 每日可用时段，如 `10:30-23:59` |
| `weekdays` | 否 | 可用星期，1=周一…7=周日。为 null 表示不限 |
| `note` | 否 | 人工备注 |

### prizes[]
`expires_at` 为 null 时不参与 θ 排序，仅列入"待处置"，并在报告里标"有效期未知，建议尽快确认"。

### windows[]
零成本、正期望的机会。脚本把这类一律置为 **T0**，因为它们的共同特征是"过期即永久消失且不占资金"。
`expected_value` 可为 null——不影响置顶，因为优先级来自"成本为 0"而非"收益多大"。

## 最小可用输入

只需 `as_of` + 一个 `coupons` 数组（每项有 `name` 和 `expires_at`）即可运行。其余都可缺省。
这条设计是为了让 skill 在"用户只问券"时也能给出有价值的输出，而不是强制走满 6 个工具。
