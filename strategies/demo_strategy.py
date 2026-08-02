from jqdata import *

def initialize(context):
    set_benchmark('510300.XSHG')
    set_option('use_real_price', True)
    g.target = ['000001.XSHE', '600000.XSHG']
    run_daily(market_open, time='10:00')


def market_open(context):
    for stock in g.target:
        df = get_price(stock, count=5, fields=['close'])
        if df is None or len(df) < 5:
            log.warn(f"行情数据不足，跳过 {stock}")
            continue
        closes = df['close']
        if closes.iloc[-1] > closes.mean():
            order_target_value(stock, 10000)
