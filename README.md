# 跨平台促销价差归因与毛利底线审批（priceguard）

品牌在多电商平台与线下门店同步推广同一商品。本应用把**页面标价、优惠核销、
品牌结算、成本版本、活动报名与门店巡检**汇入同一套口径，回答三个问题：

1. 消费者看到的差价里，每一段优惠**实际由谁承担**？
2. 活动发布前各渠道的**有效到手价、品牌承担额、贡献毛利**是否守住底线？
3. 同一商品跨渠道是否存在**价格冲突**，线下价是否已经区域运营确认？

纯 Python 标准库实现，无第三方依赖；金额全程 `Decimal`，未确定部分显式呈现，
不用 0 掩盖缺失。

## 业务口径（代码化的规则）

- **结算记录是承担方的唯一权威来源**。核销表里的 `owner` 只是下单时的主张；
  结算缺失或 `pending` 时，优惠金额计入“未确定”，绝不默认归入品牌让利，
  也不把平台补贴误记为品牌承担。
- **毛利 = 品牌净收入 − 有效成本**，品牌净收入 = 到手价 − 品牌承担额；
  平台补贴不进品牌损益，即使与品牌优惠在同一订单叠加。
- **套组不与单品直接比价**。`spec_map.csv` 提供规格/标准单位映射与套组组件；
  跨规格按标准单位归一后比较单位价，缺映射时只输出“未确定”。
- **未核实的区域价不是违规**。门店巡检 `scope_confirmed=false` 时冲突为
  `pending`，区域运营确认后才可能成为 `confirmed`。
- **成本/结算更正后**：未上线活动按新口径重新校验（独立重验事件）；
  已上线（已承诺）活动不回滚审批，而是记录偏离底线的影响金额与处理负责人。
- **审批历史只追加**。事件日志 `events.jsonl` 永不就地改写，无法用覆盖旧价格
  隐藏审批历史；每次审批留存当时的计算快照与口径指纹。

## 目录结构

```
priceguard/          应用包
  models.py          领域模型（金额 Decimal，空单元格为 None）
  loader.py          CSV 装载与结构/外键校验
  engine.py          归因、毛利、冲突识别（纯函数）
  store.py           只追加事件日志与状态折叠
  workflow.py        提交/审批/上线/确认/更正重验/偏差处理
  report.py          中文文本与 JSON 呈现
  cli.py             命令行入口
samples/             合成输入（8 张 CSV，UTF-8，空单元格表示无源值）
tests/               40 个 unittest 用例
run.py               隔离模式（python3 -I）启动器
```

## 输入文件

| 文件 | 内容 | 维护角色 |
| --- | --- | --- |
| `prices.csv` | 渠道标价、实际到手价、规格 | 数据接入 |
| `discounts.csv` | 订单优惠核销及登记承担方/核实状态 | 数据接入 |
| `settlements.csv` | 财务核实的承担方与结算周期（权威） | 财务 |
| `costs.csv` | 商品成本有效版本（含生效区间） | 商品团队 |
| `campaigns.csv` / `campaign_discounts.csv` | 平台活动报名与计划优惠 | 运营 |
| `audits.csv` | 门店价格巡检与区域活动范围确认状态 | 区域运营 |
| `spec_map.csv` | 规格、标准单位、套组组件映射 | 商品团队 |

## 使用方式

隔离模式运行（不加载工作目录中的任何模块，避免不可信数据目录里的同名模块）：

```bash
python3 -I run.py orders                      # 全部订单承担方归因与毛利
python3 -I run.py order O2                    # 单订单（含未确定原因）
python3 -I run.py campaigns                   # 活动发布前校验，退出码 1=存在拦截项
python3 -I run.py conflicts                   # 跨渠道价格冲突三态
python3 -I run.py submit B1 --by qin          # 提交审批
python3 -I run.py approve B1 --by qin         # 批准（未确定/突破底线默认拦截）
python3 -I run.py approve B3 --by qin --force --note "大促例外"  # 显式特批留痕
python3 -I run.py launch B1 --by lin          # 批准后上线
python3 -I run.py finance-confirm ST03 --by he --bearer platform --cycle 2026-10M
python3 -I run.py region-confirm SH01 --by wang --note "华东活动范围核实"
python3 -I run.py cost-correct S1 v1 110 --from 2026-01-01 --to 2026-10-14 \
               --by bai --reason "包材涨价"   # 更正并自动重验
python3 -I run.py resolve-deviation B1 <event_id> --by lin --note "追加补贴"
python3 -I run.py status                      # 阶段、最近校验、偏差与负责人
python3 -I run.py history                     # 完整只追加事件历史
```

任一计算命令可在子命令后加 `--json` 输出机器可读结果。
默认数据目录 `samples/`，事件日志 `data/events.jsonl`（已在 `.gitignore`）；
可用 `--data-dir` / `--event-log` 指向独立目录，便于隔离演练。

退出码：`0` 成功；`1` 业务校验不通过（活动被拦截或存在已确认冲突）；
`2` 输入结构或工作流状态错误。

## 测试

```bash
python3 -I -c "import sys, unittest; sys.path.insert(0, '.'); \
  unittest.main(module=None, argv=['unittest','discover','-s','tests'])"
# 或仓库原有方式（结构检查一并执行）
python3 -m unittest discover -s tests -v
```

## 样例场景覆盖

- O1：平台补贴与品牌优惠同单叠加，按结算拆分，平台补贴不进品牌毛利；
- O2：结算 `pending` + 未核销差额，全部呈现为未确定，财务确认后才归入平台；
- B1：达标活动的完整 提交→批准→上线→成本更正→偏差→处理 闭环；
- B2：计划优惠承担方未落实，审批被“未确定”拦截；
- B3：贡献毛利低于底线，审批被拦截（可显式特批并留痕）；
- B4：套组按组件成本加总，不与单品直接比价；
- SH01/SH03：未确认区域价只产生 `pending`；SH04 缺规格映射只产生“未确定”。
