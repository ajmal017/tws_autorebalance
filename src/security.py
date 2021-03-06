"""
This file defines primitives to ensure financial security throughout the
application by validating that quantities representing financial risk, such as
trade size, loan size, etc., are bounds-checked at the time of calculation.

The goal is to ensure operational security and satisfaction of risk constraints.
This differs and complements standard bounds-checking, which should still be
used throughout to validate the mathematical soundness of program logic.

In order to keep security policies as concise as possible, this module should
not care if parameters are unreasonable in a non-dangerous direction. e.g. the
rebalance threshold is so high the program never trades.
"""
import operator
import sys
from abc import abstractmethod
from dataclasses import dataclass
from logging import INFO, Formatter, Logger, StreamHandler, getLogger
from types import MappingProxyType
from typing import Callable, Generic, TypeVar

from ibapi.order import Order

from src import config
from src.util.format import color

PERMIT_ERROR = MappingProxyType(
    {
        202: "WARNING",  # order canceled
        504: "DEBUG",  # not connected -- always thrown on start
        2103: "WARNING",  # datafarm connection broken
        2104: "DEBUG",  # Market data farm connection is OK
        2106: "DEBUG",  # A historical data farm is connected.
        2108: "DEBUG",  # data hiccup
        2158: "DEBUG",  # A historical data farm is connected.
    }
)

assert not set(PERMIT_ERROR.values()) - {"DEBUG", "INFO", "WARNING"}


# noinspection SpellCheckingInspection
def init_sec_logger() -> Logger:
    seclog = getLogger("FINSEC")
    seclog.setLevel(INFO)
    seclog.addHandler(StreamHandler(sys.stderr))
    seclog.handlers[0].setFormatter(
        Formatter(
            color("yellow", "{asctime} SEC-{levelname} ∷ {message}"),
            style="{",
        )
    )
    return seclog


LOG = init_sec_logger()


class SecurityFault(Exception):
    """
    This exception is raised whenever a dangerous operation is blocked by user
    or automatic intervention.
    """


T = TypeVar("T")


@dataclass(frozen=True)  # type: ignore
class ThreeTierGeneric(Generic[T]):

    name: str
    block_level: T
    confirm_level: T
    notify_level: T
    confirm_msg: str
    cmp_op: Callable[[T, T], bool]

    def __post_init__(self) -> None:
        assert self.cmp_op is not None

    def _confirm_danger(self, val: T) -> None:
        ans = input(
            color(
                "blue",
                f"SEC [{self.name}]"
                f"({self.fmt_val(val)} {self.fmt_op()} {self.confirm_level}) "
                f"'{self.confirm_msg}'. YES to confirm.\n",
            )
        )
        if ans != "YES":
            raise SecurityFault(self.confirm_msg)

    def validate(self, val: T) -> T:
        if self.cmp_op(val, self.block_level):  # type: ignore
            LOG.error(
                f"[{self.name}]("
                f"{self.fmt_val(val)} {self.fmt_op()} {self.block_level}) "
                "rejected by rule."
            )
            raise SecurityFault(self.confirm_msg)
        if self.cmp_op(val, self.confirm_level):  # type: ignore
            self._confirm_danger(val)
            LOG.warning(
                f"[{self.name}]({self.fmt_val(val)}) permitted on override."
            )
            return val

        msg = f"[{self.name}]({self.fmt_val(val)}) permitted as of right."
        if self.cmp_op(val, self.notify_level):  # type: ignore
            LOG.info(msg)
        else:
            LOG.debug(msg)

        return val

    @abstractmethod
    def fmt_val(self, val: T) -> str:
        """
        Formats the checked value for logging.

        :param val: the value to format
        :return: a prettified string representation of val
        """

    @abstractmethod
    def fmt_op(self) -> str:
        """
        :return:  a string representation of the operator
        """


N = TypeVar("N", int, float)


@dataclass(frozen=True)
class ThreeTierNMax(ThreeTierGeneric[N]):
    """
    Two-tier confirm/block security policy for numbers that should not exceed
    some maximum.
    """

    cmp_op: Callable[[N, N], bool] = operator.gt

    def __post_init__(self) -> None:
        super().__post_init__()
        assert self.block_level > self.confirm_level

    def fmt_val(self, val: N) -> str:
        if isinstance(val, float):
            return f"{val:.3f}"
        else:
            return f"{val:0d}"

    def fmt_op(self) -> str:
        return ">"


@dataclass(frozen=True)
class ThreeTierNMin(ThreeTierGeneric[N]):
    """
    Two-tier confirm/block security policy for numbers that should not go under
    some minimum.
    """

    cmp_op: Callable[[N, N], bool] = operator.lt

    def __post_init__(self) -> None:
        super().__post_init__()
        assert self.block_level < self.confirm_level

    def fmt_val(self, val: N) -> str:
        if isinstance(val, float):
            return f"{val:.3f}"
        else:
            return f"{val:0d}"

    def fmt_op(self) -> str:
        return "<"


class Policy:
    """
    The security policy object.

    The class members of Pol are the various opsec constraints that are
    to be checked throughout program flow.
    """

    MARGIN_USAGE = ThreeTierNMax(
        "MARGIN USAGE", 0.80, 0.60, 0.40, "High margin usage."
    )
    MARGIN_REQ = ThreeTierNMin(
        "MARGIN REQ", 0.15, 0.20, 0.25, "Low margin requirement."
    )
    LOAN_AMT = ThreeTierNMax(
        "LOAN AMOUNT", 100_000.0, 75_000.0, 50_000.0, "Large loan size."
    )
    MISALLOCATION = ThreeTierNMax(
        "MISALLOCATION", 3e-3, 1e-3, 3e-4, "Misallocated portfolio."
    )
    ORDER_QTY = ThreeTierNMax("ORDER SIZE", 250, 100, 50, "Large order size.")
    ORDER_TOTAL = ThreeTierNMax(
        "ORDER TOTAL", 50000.0, 5000.0, 1000.0, "Large order amount."
    )
    MISALLOC_DOLLARS = ThreeTierNMin(
        "MISALLOC $ MIN", 200, 400, 600, "Small dollar rebalance threshold."
    )
    REBALANCE_TRIGGER = ThreeTierNMin(
        "REBALANCE TRIGGER % MIN",
        0.5,
        0.75,
        1.0,
        "Small rebalance trigger.",
    )
    ATH_MARGIN_USE = ThreeTierNMax(
        "ATH MARGIN USER", 0.3, 0.2, 0.0, "High ATH margin usage."
    )
    DRAWDOWN_COEFFICIENT = ThreeTierNMax(
        "DRAWDOWN COEFFICIENT", 2.0, 1.5, 0.5, "High drawdown coefficient."
    )

    # number of seconds to wait before the same contract can be traded again
    ORDER_COOLOFF = 55

    MAX_PRICING_AGE = 20  # seconds
    MAX_ACCT_SUM_AGE = 120  # seconds


def audit_order(order: Order) -> Order:

    succ = order.orderType == "MIDPRICE"
    succ &= config()["app"].getboolean("armed") or not order.transmit
    succ &= not order.outsideRth

    assert succ, f"{order} failed audit."

    order._audited = True
    return order
