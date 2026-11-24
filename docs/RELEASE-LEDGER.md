# 盘点放料、回执截止与留档预留

库存数量统一使用整数克。核心纯函数为 `app/modules/stock/availability.py` 的 `assess`，`POST /api/availability` 与 `POST /api/reconcile` 返回同一结果。

## 放料口径

按批号分别计算，不能用一个批号的富余抵消另一个批号的缺口：

```text
可领量 = max(0, 盘点数
             - 盘点后已到回执的实际出库量
             - 未签收领料单的未确认上限
             - 其他已留档试配单的预留量)
```

- `counted` / `included_in_count` / `already_deducted` 为真：该领料已计入盘点，晚到回执只做说明，不再扣料。
- `after_count` 为真，或出库时间晚于盘点时间：回执到达后按实际量补扣。
- 回执未到且未标记为已计入盘点：按 `max_g` 上限预留。
- 回执给出实际数量后，以上限计算的待核暴露解除；显式 `partial: true` 时，只把未确认差额继续预留。
- 若提供本单 `allocation`，结果另给 `release_g`、`short_g` 和按 `step_g` 向下取整的 `step_release_g`。

主要字段包括 `available_g`、`release_g`、`pending_hold_g`、`receipt_deducted_g`、`reserved_g`、`pending_reasons`、`pending_reason_details` 和逐批号 `rows`。

## 并发与幂等

`StockLedger` 在归档目录维护 `stock-ledger.json`，使用进程内锁和 `flock` 串行化“检查同一批号余量—写入预留”。

- `POST /api/orders` 提交 `{"allocation":{"PJ-01":5000}, "stock":{"PJ-01":5000}}`；同一批号合计超过盘点量时返回 `409 LOT_ALLOCATION_EXCEEDED`。
- `/api/batches` 可带 `order_id`、`request_id`、`Idempotency-Key` 或 `release/reserve/confirm: true`。
- 同一幂等键重试返回原 `record` / `batch_id`，`created=false`、`replayed=true`，不会再次扣减或预留。
- 回执可写入 `POST /api/requisitions/<id>/receipts`，随后用 `/api/reconcile` 重新对账；原试配单仍通过原幂等键取回原留档。

试算页 `/trial/stock` 展示“可领量和待核原因”，并可通过 `stock_events` JSON 文本框提交盘点、领料、回执和预留事件。
