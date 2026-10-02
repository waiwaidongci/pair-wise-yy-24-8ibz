# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把“计划排期”和“实际播出”分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、直播临时替换、实播对账与版权越界检查。

## 运行

需要 Python 3.11+。

```bash
python app.py
```

默认端口为 `8111`，页面地址是 <http://127.0.0.1:8111>。第一次启动会创建 `radio.db` 并写入三条演示排期。也可以设置端口和数据库位置：

```bash
PORT=9000 RADIO_DB=/tmp/radio.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整流程：排期、临时替换、播放日志、按日期对账；同时覆盖时间重叠、未授权地区和实播错节目等失败场景。

## 主要 API

- `GET /api/state`：节目、排期和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/schedule`：创建排期
- `POST /api/slots/{id}/replace`：替换计划节目并重新校验
- `POST /api/playout`：登记实播记录
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。

## 广告名额账

广告合同、节目排期、播出回执共用一份名额账，扣次维度为 **(合同号, 地区, 日期, 节目版本)**：

- 各地联播各算各的名额；排期占用未播名额（`reserved`），回执核销后转为 `verified`，账面约束 `reserved + verified <= bought`。
- 节目改版（`revise`）/撤档（`cancel`）只重算未播部分；已播核销永久留在旧版本，不可改撤。
- 回执号是幂等键：同一回执只核销一次，写入失败后凭合同号/回执号重提不会重复扣次；超投和重复补量不在排期阶段拦截，而是在回执提交后写入 `ad_settlement_exceptions`。
- 所有写操作走 `BEGIN IMMEDIATE` 事务，两名排期员并发时先到先得；后到者收到 `409`，响应体携带剩余名额、占用/剩余时段和冲突节目。

广告相关 API：

- `POST /api/ads/contracts`：按明细建账；合同号为幂等键，重提返回 `recovered:true`，明细不一致则拒绝
- `GET  /api/ads/contracts/{no}`：凭合同号查看/恢复一份账
- `POST /api/ads/slots`：排期扣次，售罄或时段重叠返回 409 及冲突看板
- `POST /api/ads/slots/{id}/revise`：改版，直接重算未播名额
- `POST /api/ads/slots/{id}/cancel`：撤档，释放未播名额
- `POST /api/ads/receipts`：播出回执核销（可带 `slot_id`，也可登记无排期实播）
- `GET  /api/ads/board?date=&region=`：名额余量、占用与剩余时段、播后异常

