# 麦当劳 MCP 字段映射与实测响应结构

本文件记录 **2026-10-23 实测**的响应结构，供本 skill 抽取字段时对照。
字段名与真实返回一致；如官方后续改版，以实时返回为准，并同步修正本文件。

---

## 1. `query-my-account` —— 积分账户

返回 `data` 对象，**所有数值字段都是字符串**，需转 float：

```json
{"success":true,"code":200,"data":{
  "accountId":"1BB4...","currency":"麦享会积分",
  "availablePoint":"111.7",
  "accumulativePoint":"833.6",
  "expiredPoint":"721.9",
  "frozenPoint":"0",
  "usedPoint":"0",
  "currentMouthExpirePoint":"24.8",
  "lastMouthExpirePoint":"0",
  "nextMouthExpirePoint":"0"
}}
```

> 注意官方字段名拼写是 `currentMouthExpirePoint`（Mouth 而非 Month），照抄，勿"纠正"。
> `expiredPoint` 是**历史累计已过期**，不是本月过期。

映射到 assets.json：
`availablePoint → points.available`、`accumulativePoint → accumulative`、
`expiredPoint → expired`、`currentMouthExpirePoint → current_month_expire`。

---

## 2. `query-my-coupons` —— 券包

返回为**已渲染的 Markdown 文本**（非 JSON），每张券是一个二级标题 + 字段列表：

```
## 11.9元麦辣鸡腿堡
- **优惠**: ¥11.9 (用券价格)
- **有效期**: 2026-10-19 10:30-2026-10-23 23:59 周一、二、三、四、五 10:30-23:59
- **领取时间**: 6日前收到
- **标签**: 今日到期、到店专用、外送专用
```

抽取规则：
| 目标字段 | 抽取方式 |
|---|---|
| `name` | `## ` 后的标题 |
| `price_with` | `优惠` 行 `¥` 后的数字 |
| `expires_at` | `有效期` 行的**结束时刻**（第二个日期时间） |
| `channels` | `标签` 行含"到店专用"→到店；"外送专用"→外送 |
| `time_window` | `有效期` 行的 `HH:MM-HH:MM`；若出现两个（日期时段 + 日内时段），取**日内**那个 |
| `weekdays` | `有效期` 行的"周一、二、三…"→ 映射为 1..7 |
| 到期标记 | 标签含 **"今日到期"** → 最高优先级，直接进 T1 |

**重复券检测**：同名 + 同用券价 + 同有效期 → 归为同一 `duplicate_group`，报告中提示"一单只能用一张，需拆单"。

⚠️ 券名可能不含"原价"信息。"11.9元麦辣鸡腿堡"要拿到原价 25 元，必须去 `query-meals` 查同门店的麦辣鸡腿堡单价，或用 `calculate-price` 双算。**不要假设原价。**

---

## 3. `query-my-prizes` —— 抽奖奖品

返回 JSON：

```json
{"data":{"status":{"code":"SUCCESS"},"prizes":[{
  "id":"6a90...","name":"处暑","prizeType":25,"prizeTypeText":"门票",
  "recordTime":"2026-08-27 18:19:18","status":0,
  "statusText":"可用","timeRemindText":""
}],"hasMore":false}}
```

映射：`name→name`、`prizeTypeText→kind`、`statusText→status`、`timeRemindText→time_remind`、`recordTime→record_time`。
`statusText` 非"可用"（已使用/已过期/已失效）→ **不进入资产台账**，但可在报告中作为"已损失"举例。
`timeRemindText` 常为空 → `expires_at` 填 null，脚本会归入"有效期未知，尽快确认"。

---

## 4. `campaign-calendar` —— 活动日历

返回按日期分组的 Markdown。需要识别的是**带时限动作**的活动，关键词：
`预约`、`开抢`、`0元`、`免费`、`限量`、`仅需`、`起售`、`截止`。

典型的 T0 资产：
- "预约麦金喜抽奖，抽奖时间：10月24日 00:00 开始" → **现在预约，零点开抽**，零成本
- "APP 专享积分抢先购 10:40 开抢" → 有明确时间窗的先手优势

⚠️ 活动文案里的价格（"26.9 元起"）**不能直接当券面值**，它是活动价不是你的资产。
只有"你已持有/可免费领取"的权益才入账。

---

## 5. `mall-points-products` —— 积分商城

`catRuleIds` 取值：
`1>4` 商品券 / `2` 实物 / `2>8` 周边 / `2>9` 礼品卡 /
`1>6>20` 生日派对 / `1>6>21` 主题派对 / `1>6>22` 体验营 / `1>6>25` 品鉴会 / `1>6>34` 读书会 / `1>6>40` 积分兑换活动

**推算积分兑换率**：取若干"XX 餐品券 + 所需积分"，用对应餐品原价 ÷ 积分，取中位数。
若商城只有实物/活动、没有餐品券，则 `unit_value` 记 null。

---

## 6. 核价相关工具的关键参数

| 工具 | 用途 | 必记参数 |
|---|---|---|
| `query-meals` | 取原价、餐品 code | `storeCode`+`orderType`+`beType`；到店不传 `beCode` |
| `calculate-price` | 双算求券面值 | `items[]` 每项需 `productCode`+`quantity`；带券加 `couponId`/`couponCode` |
| `query-store-coupons` | 该门店**真正可用**的券 | 与 `query-my-coupons` 不同：后者不校验门店/渠道 |

> **价值提示**：`query-my-coupons` 返回的是"你拥有的券"，**不承诺当前门店可用**。
> 若用户已选门店，务必用 `query-store-coupons` 复核一遍，否则会把不可用券算进可回收价值，虚增总额。

---

## 7. 已知边界

- 所有价格字段单位为**分**（`calculate-price`），本 skill 内部统一换算为元。
- `query-my-coupons` 与 `campaign-calendar` 返回渲染文本而非 JSON，抽取需容错：正则失败时保留原文并标"解析失败"，不要静默丢弃。
- 抽奖 `draw-lottery` 属**写操作**，本 skill 默认不调用；仅当用户明确确认消耗后才执行。
- 团餐促销 `query-promotions` 的满减规则**工具不做最优推荐**，需自行解读——本 skill 的配置模式可承接该计算。
