# Forge 客诉稽查只读增量接口契约

本文件定义本地客诉稽查看板依赖的上游接口。当前仓库是数据消费者，不包含 Forge 数据服务代码；上游完成部署前，本地页面会明确显示“Forge尚未部署只读接口”，不会用模拟数据替代真实业务数据。

## 通用约束

- 仅支持 `GET`，不得修改 Forge 业务记录。
- 时间使用 ISO 8601，至少精确到秒；`inspect_completed_at` 为正式稽查完成时间。
- 每页最多 500 条，重复请求必须返回稳定结果。
- 删除记录通过 `deleted_ids` 返回；每项至少包含 `complaint_record_id`，建议同时返回 `source_updated_at`。
- `cursor` 是可持久化的增量游标；`next_cursor` 只用于当前请求的分页。
- 返回完整当前记录，而不是仅返回变化字段，以便本地准确识别字段清空。

## 回溯接口

`GET /ticket-dashboard/api/complaint-audits/backfill`

查询参数：

- `completed_from`：稽查完成日期起点，`YYYY-MM-DD`。
- `completed_to`：稽查完成日期终点，`YYYY-MM-DD`。
- `page_size`：建议 500。
- `page` 或 `page_cursor`：分页参数。
- `reconcile=1`：表示本地正在执行本月及上月日核对，返回结构不变。

## 增量接口

`GET /ticket-dashboard/api/complaint-audits/changes`

查询参数：

- `cursor`：上次成功保存的持久化游标，首次可为空。
- `modified_since`：上次源更新时间减5分钟后的重叠窗口。
- `page_size`、`page` 或 `page_cursor`：分页参数。

## 响应示例

```json
{
  "data": {
    "records": [
      {
        "complaint_record_id": "247229",
        "inspect_id": "61970831",
        "ticket_id": "1368113",
        "shop_id": "123456",
        "shop_name": "示例店铺",
        "brand": "示例品牌",
        "inspect_status": "已稽查",
        "inspect_completed_at": "2026-10-20T16:42:44+08:00",
        "is_complaint_true": true,
        "review_status": "审核通过",
        "complained_agent": "客服姓名",
        "reason_node_id": "node-guard-missed",
        "reason_path": "客诉成立 / 值守客服原因 / 值守未发现 / 客服原因 / 明显偷盗",
        "responsibility_subject": "人工客服责任",
        "reason_description": "偷盗未发现",
        "inspect_result": "正式稽查结果原文",
        "abnormal_state": "有异常",
        "abnormal_type": "明显偷盗",
        "handled_at": "2026-10-20T16:45:00+08:00",
        "reviewed_at": "2026-10-20T17:10:00+08:00",
        "source_updated_at": "2026-10-20T17:10:06+08:00",
        "deleted": false
      }
    ],
    "deleted_ids": [],
    "next_cursor": null,
    "has_more": false,
    "cursor": "opaque-sync-cursor-after-this-page"
  }
}
```

## 责任字段

优先由上游直接返回规范化的 `responsibility_subject`，值只能是：

- `人工客服责任`
- `AI工作台责任`
- `系统/平台责任`
- `其他责任`

若上游暂时只能返回原因节点ID，本机可通过 `COMPLAINT_AUDIT_RESPONSIBILITY_NODE_MAP` 指向一个 JSON 映射文件。未配置或未命中的节点一律显示“责任待确认”，不得用分类路径或原因描述关键词推测责任。
