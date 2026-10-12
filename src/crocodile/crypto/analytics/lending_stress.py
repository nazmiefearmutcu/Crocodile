from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from web3 import AsyncWeb3


def lending_stress_test(
    collateral_usd: float,
    debt_usd: float,
    liquidation_threshold: float,
    haircut_pct: float,
) -> dict[str, Any]:
    """Perform a stress test on a lending position's LTV / Health Factor.

    Health Factor = (Collateral * Liquidation Threshold) / Debt.

    Args:
        collateral_usd: Current value of collateral in USD.
        debt_usd: Current value of debt in USD.
        liquidation_threshold: Liquidation threshold as a fraction (e.g., 0.8 for 80%).
        haircut_pct: Expected collateral drop as a percentage or fraction
                     (e.g., 0.20 or 20 for a 20% drop).

    Returns:
        A dictionary with stress test metrics.
    """
    # Standardize haircut_pct to a fraction (e.g., 20.0 -> 0.20, 0.20 -> 0.20)
    haircut_fraction = (
        haircut_pct / 100.0 if abs(haircut_pct) > 1.0 else haircut_pct
    )

    if debt_usd <= 0.0:
        current_hf = float("inf")
        simulated_hf = float("inf")
    else:
        current_hf = (collateral_usd * liquidation_threshold) / debt_usd
        simulated_collateral = collateral_usd * (1.0 - haircut_fraction)
        simulated_hf = (simulated_collateral * liquidation_threshold) / debt_usd

    return {
        "current_health_factor": current_hf,
        "simulated_health_factor": simulated_hf,
        "is_liquidatable": current_hf < 1.0,
        "simulated_is_liquidatable": simulated_hf < 1.0,
    }


class LendingStressTester:
    """Queries user account positions on Aave / Seamless Pool and simulates collateral stress."""

    def __init__(self, w3: AsyncWeb3, pool_address: str) -> None:
        self.w3 = w3
        self.pool_address = w3.to_checksum_address(pool_address)
        # ABI for getUserAccountData
        self.pool_abi = [{
            "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
            "name": "getUserAccountData",
            "outputs": [
                {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
                {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
                {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
                {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
                {"internalType": "uint256", "name": "ltv", "type": "uint256"},
                {"internalType": "uint256", "name": "healthFactor", "type": "uint256"}
            ],
            "stateMutability": "view",
            "type": "function"
        }]
        self.pool_contract = self.w3.eth.contract(address=self.pool_address, abi=self.pool_abi)

    async def get_user_positions(self, user_address: str) -> dict[str, float]:
        """Query user account position metrics from the pool contract."""
        user_checksum = self.w3.to_checksum_address(user_address)
        data = await self.pool_contract.functions.getUserAccountData(user_checksum).call()
        
        # Aave V3 uses 8 decimals for collateral and debt base values,
        # 4 decimals for threshold and LTV, and 18 decimals for health factor.
        collateral = float(data[0]) / 1e8
        debt = float(data[1]) / 1e8
        liquidation_threshold = float(data[3]) / 10000.0
        ltv = float(data[4]) / 10000.0
        
        # Aave encodes "no debt" as max uint256; zero is a real HF of 0 (underwater).
        hf_raw = data[5]
        if hf_raw == (2**256 - 1):
            health_factor = float("inf")
        else:
            health_factor = float(hf_raw) / 1e18

        return {
            "collateral_usd": collateral,
            "debt_usd": debt,
            "liquidation_threshold": liquidation_threshold,
            "ltv": ltv,
            "health_factor": health_factor,
        }

    async def simulate_stress(self, user_address: str, price_haircut: float) -> dict[str, Any]:
        """Calculates stressed position stats under a price haircut."""
        pos = await self.get_user_positions(user_address)
        collateral = pos["collateral_usd"]
        debt = pos["debt_usd"]
        lt = pos["liquidation_threshold"]

        # Support both e.g. 20% represented as 0.20 or 20.0
        haircut = price_haircut / 100.0 if abs(price_haircut) > 1.0 else price_haircut
        stressed_collateral = collateral * (1.0 - haircut)

        if debt <= 0.0:
            stressed_ltv = 0.0
            stressed_hf = float("inf")
        else:
            stressed_ltv = debt / stressed_collateral if stressed_collateral > 0 else float("inf")
            stressed_hf = (stressed_collateral * lt) / debt

        return {
            "user_address": user_address,
            "price_haircut": price_haircut,
            "current_collateral_usd": collateral,
            "current_debt_usd": debt,
            "current_health_factor": pos["health_factor"],
            "current_ltv": pos["ltv"],
            "stressed_collateral_usd": stressed_collateral,
            "stressed_ltv": stressed_ltv,
            "stressed_health_factor": stressed_hf,
            "is_liquidatable": pos["health_factor"] < 1.0,
            "simulated_is_liquidatable": stressed_hf < 1.0,
        }


async def simulate_stress(
    user_address: str,
    price_haircut: float,
    w3: AsyncWeb3 | None = None,
    pool_address: str | None = None,
) -> dict[str, Any]:
    """Standalone helper function to simulate stress on a user's position."""
    if w3 is None:
        raise ValueError("Web3 connection (w3) must be provided to simulate_stress.")
    if pool_address is None:
        # Default to Aave V3 Pool on Base
        pool_address = "0xA238Dd80C259b7051470e3E0f9d921008C39F93b"
    
    tester = LendingStressTester(w3, pool_address)
    return await tester.simulate_stress(user_address, price_haircut)

