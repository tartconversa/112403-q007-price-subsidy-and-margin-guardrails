# 跨平台促销价差归因与毛利底线审批

合成平台活动价、优惠结算承担方、成本和门店巡检样例。

All example identifiers and values are synthetic. CSV files use UTF-8. The first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `prices.csv` contains 渠道标价、实际到手价及商品规格。 Columns are `sku`, `channel`, `spec`, `list_price`, `paid`, `day`.
- `discounts.csv` contains 订单优惠金额及承担方核实状态。 Columns are `order_id`, `sku`, `channel`, `kind`, `amount`, `owner`, `state`.
- `costs.csv` contains 商品成本有效版本。 Columns are `sku`, `version`, `cost`, `from`, `to`.

Python 3 standard library is sufficient to inspect these inputs. Run `python3 -m unittest discover -s tests -v` to check the delivered file structures and example references.
