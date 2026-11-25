"""命令行入口。

约定：
- 默认数据目录 samples/，事件日志 data/events.jsonl（已被 .gitignore 忽略）；
- --json 输出机器可读结果，默认输出中文文本；
- 退出码：0 成功，1 业务校验不通过（如未确定/突破底线），2 输入或状态错误。
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from . import engine, report
from .loader import LoaderError, load_dataset
from .store import CampaignStage, EventStore, StoreError
from .workflow import (
    Workflow,
    WorkflowError,
    apply_state,
    projection_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="priceguard",
        description="跨平台促销价差归因与毛利底线审批",
    )
    parser.add_argument("--data-dir", default=str(ROOT / "samples"),
                        help="输入 CSV 目录（默认 samples/）")
    parser.add_argument("--event-log", default=str(ROOT / "data" / "events.jsonl"),
                        help="只追加事件日志路径")

    sub = parser.add_subparsers(dest="command", required=True)

    def sub_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        """每个子命令都支持 --json。"""
        child = sub.add_parser(name, **kwargs)
        child.add_argument("--json", action="store_true", help="输出 JSON")
        return child

    p = sub_parser("orders", help="全部订单的优惠承担方归因与毛利")
    p = sub_parser("order", help="单个订单归因")
    p.add_argument("order_id")

    sub_parser("campaigns", help="全部活动发布前校验")
    p = sub_parser("campaign", help="单个活动校验")
    p.add_argument("campaign_id")

    sub_parser("conflicts", help="跨渠道价格冲突")

    p = sub_parser("submit", help="提交活动审批")
    p.add_argument("campaign_id")
    p.add_argument("--by", required=True)

    p = sub_parser("approve", help="审批活动（未确定/突破底线默认拒绝）")
    p.add_argument("campaign_id")
    p.add_argument("--by", required=True)
    p.add_argument("--force", action="store_true", help="显式特批并留痕")
    p.add_argument("--note", default="")

    p = sub_parser("reject", help="驳回活动")
    p.add_argument("campaign_id")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)

    p = sub_parser("launch", help="已批准活动上线")
    p.add_argument("campaign_id")
    p.add_argument("--by", required=True)

    sub_parser("refresh", help="成本/结算更正后重验全部在途活动")

    p = sub_parser("finance-confirm", help="财务核实/补录结算承担方与周期")
    p.add_argument("settlement_id")
    p.add_argument("--by", required=True)
    p.add_argument("--bearer", required=True, choices=["platform", "brand"])
    p.add_argument("--cycle", required=True)
    p.add_argument("--state", default="confirmed", choices=["confirmed", "pending"])
    p.add_argument("--amount")
    p.add_argument("--order-id")
    p.add_argument("--sku")
    p.add_argument("--channel")
    p.add_argument("--kind")

    p = sub_parser("region-confirm", help="区域运营确认线下活动范围")
    p.add_argument("audit_id")
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")

    p = sub_parser("cost-correct", help="商品团队更正有效成本版本并触发重验")
    p.add_argument("sku")
    p.add_argument("version")
    p.add_argument("cost")
    p.add_argument("--from", dest="effective_from", required=True)
    p.add_argument("--to", dest="effective_to")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)

    p = sub_parser("resolve-deviation", help="登记已上线活动偏差的处理结果")
    p.add_argument("campaign_id")
    p.add_argument("deviation_event_id")
    p.add_argument("--by", required=True)
    p.add_argument("--note", required=True)

    sub_parser("status", help="查看事件日志折叠后的审批状态与偏差")
    sub_parser("history", help="列出全部事件（审批历史不可覆盖）")
    return parser


def _context(args) -> tuple:
    dataset = load_dataset(args.data_dir)
    store = EventStore(args.event_log)
    workflow = Workflow(dataset, store)
    # 所有计算都基于事件叠加后的有效数据集（财务确认/区域确认/成本更正生效）
    effective = apply_state(dataset, store.fold())
    return effective, store, workflow


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        dataset, store, workflow = _context(args)

        if args.command == "orders":
            rows = engine.all_order_attributions(dataset)
            if args.json:
                print(report.to_json([row.__dict__ for row in rows]))
            else:
                print("\n\n".join(report.render_order(row) for row in rows))
            return 0

        if args.command == "order":
            rows = engine.attribute_order(dataset, args.order_id)
            if args.json:
                print(report.to_json([row.__dict__ for row in rows]))
            else:
                print("\n\n".join(report.render_order(row) for row in rows))
            return 0

        if args.command == "campaigns":
            rows = [
                engine.project_campaign(dataset, campaign.campaign_id)
                for campaign in dataset.campaigns
            ]
            if args.json:
                print(report.to_json([projection_snapshot(row) for row in rows]))
            else:
                print("\n\n".join(report.render_campaign(row) for row in rows))
            blocked = [row for row in rows if row.status != "ok"]
            return 1 if blocked else 0

        if args.command == "campaign":
            row = engine.project_campaign(dataset, args.campaign_id)
            if args.json:
                print(report.to_json(projection_snapshot(row)))
            else:
                print(report.render_campaign(row))
            return 0 if row.status == "ok" else 1

        if args.command == "conflicts":
            rows = engine.find_conflicts(dataset)
            if args.json:
                print(report.to_json([row.__dict__ for row in rows]))
            else:
                print(
                    "\n\n".join(report.render_conflict(row) for row in rows)
                    or "未发现跨渠道价格冲突"
                )
            return 1 if any(row.status == "confirmed" for row in rows) else 0

        if args.command == "submit":
            event = workflow.submit(args.campaign_id, args.by)
            print(report.to_json(event) if args.json
                  else f"已提交审批: 事件 {event['event_id']}")
            return 0

        if args.command == "approve":
            event = workflow.approve(
                args.campaign_id, args.by, force=args.force, note=args.note
            )
            print(report.to_json(event) if args.json
                  else f"已批准: 事件 {event['event_id']}（快照已留存）")
            return 0

        if args.command == "reject":
            event = workflow.reject(args.campaign_id, args.by, args.reason)
            print(report.to_json(event) if args.json
                  else f"已驳回: 事件 {event['event_id']}")
            return 0

        if args.command == "launch":
            event = workflow.launch(args.campaign_id, args.by)
            print(report.to_json(event) if args.json
                  else f"已上线: 事件 {event['event_id']}")
            return 0

        if args.command == "refresh":
            events = workflow.refresh()
            if args.json:
                print(report.to_json(events))
            elif not events:
                print("无变化：所有在途活动口径与最近一次校验一致")
            else:
                print(f"追加 {len(events)} 个重验/偏差事件：")
                for event in events:
                    print(f"- {event['event_id']} {event['type']} {event['payload']}")
            return 0

        if args.command == "finance-confirm":
            event = workflow.confirm_settlement(
                args.settlement_id, args.by, args.bearer, args.cycle,
                state_value=args.state, amount=args.amount,
                order_id=args.order_id, sku=args.sku,
                channel=args.channel, kind=args.kind,
            )
            workflow.refresh()
            print(report.to_json(event) if args.json
                  else f"财务确认已记录: 事件 {event['event_id']}")
            return 0

        if args.command == "region-confirm":
            event = workflow.confirm_audit_scope(args.audit_id, args.by, args.note)
            print(report.to_json(event) if args.json
                  else f"线下活动范围已确认: 事件 {event['event_id']}")
            return 0

        if args.command == "cost-correct":
            event = workflow.correct_cost(
                args.sku, args.version, args.cost,
                args.effective_from, args.effective_to,
                args.by, args.reason,
            )
            print(report.to_json(event) if args.json
                  else f"成本更正已记录并触发重验: 事件 {event['event_id']}")
            return 0

        if args.command == "resolve-deviation":
            event = workflow.resolve_deviation(
                args.campaign_id, args.deviation_event_id, args.by, args.note
            )
            print(report.to_json(event) if args.json
                  else f"偏差处理已登记: 事件 {event['event_id']}")
            return 0

        if args.command == "status":
            state = store.fold()
            payload = []
            for campaign_id in sorted(state.records):
                record = state.records[campaign_id]
                payload.append({
                    "campaign_id": campaign_id,
                    "stage": record.stage,
                    "revision": record.revision,
                    "last_status": record.last_snapshot.get("status"),
                    "deviations": [
                        item.__dict__ for item in record.deviations
                    ],
                })
            if args.json:
                print(report.to_json(payload))
            else:
                for item in payload:
                    print(
                        f"活动 {item['campaign_id']} 阶段 {item['stage']} "
                        f"版本 r{item['revision']} 最近校验 {item['last_status']}"
                    )
                    for deviation in item["deviations"]:
                        flag = "已处理" if deviation["resolved"] else "待处理"
                        print(
                            f"  - [{flag}] r{deviation['revision']} "
                            f"{deviation['old_status']} -> {deviation['new_status']} "
                            f"影响 {deviation['impact_amount']} "
                            f"负责人 {deviation['owner']}"
                        )
            return 0

        if args.command == "history":
            events = store.events()
            if args.json:
                print(report.to_json(events))
            else:
                for event in events:
                    campaign = event["payload"].get("campaign_id", "-")
                    print(f"{event['at']} {event['event_id']} "
                          f"{event['type']} {campaign}")
            return 0

    except (LoaderError, StoreError, WorkflowError, KeyError, ValueError) as exc:
        message = str(exc)
        if args.json:
            print(report.to_json({"error": message}), file=sys.stderr)
        else:
            print(f"错误: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
