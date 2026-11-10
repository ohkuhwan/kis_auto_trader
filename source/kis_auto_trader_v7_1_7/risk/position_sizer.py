"""주문가능금액과 미수 없는 매수한도를 반영한 주문수량 계산."""
import logging
import math

log = logging.getLogger(__name__)


def _num(d, *keys):
    for key in keys:
        try:
            value = d.get(key)
            if value not in (None, ""):
                return float(str(value).replace(",", ""))
        except (ValueError, TypeError, AttributeError):
            pass
    return 0.0


def calculate_buy_qty(
    summary: dict,
    held_count: int,
    price: float,
    max_positions: int = 3,
    min_order_amount: float = 10000.0,
    max_single_asset_ratio: float | None = None,
    fee_rate_pct: float = 0.02,
    price_buffer_pct: float = 0.0,
    reserved_cash: float = 0.0,
    *,
    cash_basis: str = "orderable",
    buyable_cash_limit: float | None = None,
    buyable_qty_limit: int | None = None,
) -> tuple[int, float, str]:
    """남은 포지션 슬롯에 주문가능 현금을 배분해 주문수량을 계산합니다.

    기본은 계좌 주문가능금액(ord_psbl_cash)입니다. 종목별 매수가능조회 결과가
    있으면 그 한도와 미수 없는 매수금액/수량(nrcvb_buy_amt/qty)을 추가 상한으로
    적용합니다. fallback 단계에서만 cash_basis='deposit'을 사용합니다.
    예약 현금은 모든 단계에서 차감하며, 미수 한도를 초과하지 않습니다.
    """
    if price <= 0:
        return 0, 0.0, "현재가 오류"
    max_positions = max(1, int(max_positions))
    held_count = max(0, int(held_count))
    remaining_slots = max_positions - held_count
    if remaining_slots <= 0:
        return 0, 0.0, f"보유 종목 한도 도달({held_count}/{max_positions})"

    deposit = _num(summary, "dnca_tot_amt", "DNCA_TOT_AMT")
    orderable_cash = _num(summary, "ord_psbl_cash", "ORD_PSBL_CASH")
    reserved_cash = max(0.0, float(reserved_cash or 0.0))
    basis = str(cash_basis or "orderable").strip().lower()
    if basis == "deposit":
        base_cash = deposit
        source = "deposit_fallback"
    elif basis == "orderable":
        base_cash = orderable_cash
        source = "orderable_cash"
    else:
        return 0, 0.0, f"지원하지 않는 cash_basis={basis}"

    if base_cash <= 0:
        return 0, 0.0, (
            f"{source} 확인 실패/0원; 신규 매수 차단 "
            f"(예수금={deposit:.0f}원, 주문가능금액={orderable_cash:.0f}원)"
        )

    limits = [base_cash]
    symbol_limit = None
    if buyable_cash_limit is not None:
        symbol_limit = max(0.0, float(buyable_cash_limit))
        if symbol_limit <= 0:
            return 0, 0.0, "종목별 매수가능조회 금액이 0원/누락되어 매수 차단"
        limits.append(symbol_limit)
    cash_before_reserve = min(limits)
    cash = max(0.0, cash_before_reserve - reserved_cash)
    budget = cash / remaining_slots

    if max_single_asset_ratio is not None:
        budget = min(budget, cash * max(0.0, float(max_single_asset_ratio)))
    if budget < float(min_order_amount):
        return 0, budget, (
            f"종목별 예산 부족({budget:.0f}원 < {float(min_order_amount):.0f}원); "
            f"basis={source}, cash_limit={cash_before_reserve:.0f}, reserved={reserved_cash:.0f}, "
            f"remaining_slots={remaining_slots}"
        )

    unit_cost = price * (1.0 + max(0.0, price_buffer_pct) / 100.0)
    unit_cost *= 1.0 + max(0.0, fee_rate_pct) / 100.0
    qty = int(math.floor(budget / unit_cost))
    if buyable_qty_limit is not None:
        qty = min(qty, max(0, int(buyable_qty_limit)))
    if qty <= 0:
        return 0, budget, (
            f"예산으로 1주 매수 불가 또는 미수 없는 매수수량 한도 0주; "
            f"basis={source}, budget={budget:.0f}, price={price:.2f}, "
            f"no_credit_qty_limit={buyable_qty_limit}"
        )

    estimated_cost = qty * unit_cost
    reason = (
        f"cash_source={source}; cash_available={cash:.0f}; cash_limit={cash_before_reserve:.0f}; "
        f"deposit={deposit:.0f}; orderable_cash={orderable_cash:.0f}; reserved_cash={reserved_cash:.0f}; "
        f"held_count={held_count}; max_positions={max_positions}; remaining_slots={remaining_slots}; "
        f"no_credit_cash_limit={symbol_limit if symbol_limit is not None else 'not_supplied'}; "
        f"budget={budget:.0f}; price={price:.2f}; buffer_pct={price_buffer_pct:.3f}; "
        f"fee_pct={fee_rate_pct:.3f}; no_credit_qty_limit={buyable_qty_limit}; "
        f"qty={qty}; estimated_cost={estimated_cost:.0f}"
    )
    log.info("매수 수량 계산: %s", reason)
    return qty, budget, reason
