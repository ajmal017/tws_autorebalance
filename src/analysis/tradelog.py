from __future__ import annotations

from argparse import Namespace
from collections import defaultdict
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import List, DefaultDict, Dict, Set, Any, Tuple

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from dateutil.rrule import MO
from matplotlib.axes import Axes
from matplotlib.dates import WeekdayLocator, DateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import MultipleLocator

from src import config
from src.model.calc import calculate_profit_attributions, PAttrMode
from src.model.data import Trade, AttributionSet, Portfolio

matplotlib.rc("figure", figsize=(12, 12))


def parse_tws_trade_log(path: Path) -> DefaultDict[str, Set[Trade]]:

    header_prefix = "Trades,Header,"
    data_prefix = "Trades,Data,Order,Stocks,"

    with path.open("r") as f:
        lines = f.readlines()

    columns = None
    data_lines = []
    for line in lines:
        line = line.strip()
        if line.startswith(header_prefix) and columns is None:
            # skip Order,Stocks columns on top of the prefix cols
            columns = line[len(header_prefix) :].split(",")[2:]
        elif line.startswith(data_prefix):
            data_lines.append(line[len(data_prefix) :])

    out: DefaultDict[str, Set[Trade]] = defaultdict(set)

    if not data_lines:
        return out

    df = pd.read_csv(StringIO("\n".join(data_lines)), thousands=",", header=None)
    df.columns = columns
    df = df[["Date/Time", "Symbol", "Quantity", "T. Price", "Comm/Fee"]]
    df["Date/Time"] = pd.to_datetime(df["Date/Time"])

    for date, symbol, qty, price, comm in df.itertuples(index=False):

        if qty == 0:
            continue

        t = date.to_pydatetime()
        # if qty < 0, we lower price; else we increase.
        price -= comm / qty
        out[symbol].add(Trade(symbol, t, qty, price))

    return out


def load_trade_logs(
    start: datetime = None, end: datetime = None
) -> Dict[str, List[Trade]]:
    trade_dir = Path(config()["trade_log"]["log_dir"]).expanduser()
    all_trades: DefaultDict[str, Set[Trade]] = defaultdict(set)
    for fn in trade_dir.glob("*.csv"):
        fn_log = parse_tws_trade_log(fn)
        for sym, trades in fn_log.items():
            all_trades[sym] |= {
                tr
                for tr in trades
                if (start is None or start <= tr.time)
                and (end is None or end >= tr.time)
            }
    return {sym: sorted(trades) for sym, trades in all_trades.items() if trades}


def get_args() -> Namespace:
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument(
        "--start",
        default="1900-01-01",
        type=datetime.fromisoformat,
        help="start date for trade attribution analysis",
    )
    parser.add_argument(
        "--end",
        default="2100-01-01",
        type=datetime.fromisoformat,
        help="end date for trade attribution analysis",
    )

    args = parser.parse_args()
    args.end = min(args.end, datetime.now())

    assert args.end >= args.start

    return args


def plot_trade_profits(
    attr_set: AttributionSet, **plot_kwargs: Any
) -> Tuple[Figure, Axes, Axes]:

    net_daily = attr_set.net_daily_attr

    dates, dailies = zip(*list(net_daily.items()))

    ax1: Axes
    ax2: Axes
    fig, (ax1, ax2) = plt.subplots(2, 1)

    for ax in (ax1, ax2):
        ax.xaxis.set_major_locator(WeekdayLocator(byweekday=MO))
        ax.xaxis.set_minor_locator(MultipleLocator(1))
        ax.grid()
        ax.grid(which="minor", lw=0.25)

    ax2.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    # ax2.xaxis.set_minor_formatter(DateFormatter("%a"))

    ax1.plot(dates, dailies, label="daily profits (back-projected)", **plot_kwargs)
    ax1.set_xticklabels([])
    ax1.legend()
    ax2.plot(
        dates,
        np.cumsum(dailies),
        label="cumulative profit (back-projected)",
        color="k",
        **plot_kwargs,
    )
    ax1.set_ylim(bottom=-100)
    ax1.set_xlim(*ax1.get_xlim())
    ax2.legend()
    plt.xticks(rotation=90)

    return fig, ax1, ax2


def analyze_trades(
    start: datetime,
    end: datetime,
    *,
    mode: PAttrMode = "min_variation",
    ylim1: Tuple[int, int] = (-100, 300),
    ylim2: Tuple[int, int] = (-2000, 4000),
) -> None:


    all_trades = load_trade_logs(start, end)
    portfolio = Portfolio()
    for ts in all_trades.values():
        for t in ts:
            portfolio.transact(t)

    non_zero_syms = {pos.sym for pos in portfolio.positions if pos.qty != 0}

    all_dates = []
    tot_cash = []
    tot_basis = []
    weighted_av_price = []

    print(start, end)

    delta = timedelta(days=1)
    cur_date = start + delta

    while cur_date <= end:

        all_dates.append(cur_date)
        tot_cash.append(0.0)
        tot_basis.append(0.0)

        attributed = {
            sym: calculate_profit_attributions(
                tuple(t for t in trades if t.time <= cur_date), mode=mode
            )
            for sym, trades in all_trades.items()
            if sym in non_zero_syms
        }

        tot_qty = 0
        attr_set = AttributionSet()
        for sym, (pas, pos) in attributed.items():
            if pos is None:
                basis = 0.0
                pos_cash = 0.0
            else:
                basis = pos.basis
                pos_cash = pos.credit
                tot_qty += pos.qty

            attr_set.extend(pas)
            net_pa = attr_set.get_total_for(sym)

            if net_pa is None:
                cash = pos_cash
            else:
                cash = net_pa.net_gain + pos_cash

            tot_cash[-1] += cash
            tot_basis[-1] += basis

        if tot_qty > 0:
            weighted_av_price.append(tot_basis[-1] / tot_qty)
        else:
            weighted_av_price.append(0.0)

        cur_date += delta

    fig, ax1, ax2 = plot_trade_profits(attr_set)
    ax1.plot(
        all_dates,
        weighted_av_price,
        color="green",
        lw=0.5,
        label="weighted average price",
    )
    ax2.plot(
        all_dates,
        np.array(tot_cash) + np.array(tot_basis),
        color="grey",
        label="cumulative profit (marching)",
    )
    ax2.legend()
    ax1.set_title(f"Profits from {start.date()} to {cur_date.date()}, method='{mode}'")
    ax1.legend()
    ax1.set_ylim(ylim1)
    ax1.set_xlim(ax2.get_xlim())
    ax2.set_ylim(ylim2)
    fig.show()


def summarize_closed_positions() -> None:

    all_trades = load_trade_logs()
    base_port = Portfolio.from_trade_dict(all_trades)
    portfolio = base_port.filter(lambda pos: pos.basis == 0.0)

    print("Closed positions:")
    for pos in sorted(portfolio.positions, key=lambda x: x.book_nlv):
        print(pos)

    print("")
    print("Effective portfolio:")
    print(portfolio)


if __name__ == "__main__":
    args = get_args()
    analyze_trades(args.start, args.end, mode="min_variation")
    summarize_closed_positions()