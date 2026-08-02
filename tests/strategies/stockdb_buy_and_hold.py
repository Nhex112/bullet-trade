"""
StockDB 冒烟策略：开启真实价格模式（use_real_price=True），首日买入 510300.XSHG。

真实价格模式会按回测当日复权参考日取值，避免"前复权到最新因子"
与不复权成交价之间的口径错位（与聚宽引擎语义一致）。
"""
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')  # stockdb 无指数数据时引擎自动降级
    set_option('use_real_price', True)
    g.stock = '510300.XSHG'
    g.has_bought = False
    run_daily(market_open, time='open')


def market_open(context):
    if not g.has_bought:
        order_value(g.stock, context.portfolio.available_cash)
        g.has_bought = True
        log.info("买入 %s，金额: %.2f", g.stock, context.portfolio.available_cash)
