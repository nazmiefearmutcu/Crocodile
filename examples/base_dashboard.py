import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
import os
import sys
from pathlib import Path
from web3 import Web3

# Setup python path to import crocodile
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from crocodile.crypto.exchanges.base_onchain.connector import FACTORIES, POOL_SPECS, TOKENS
from crocodile.crypto.exchanges.base_onchain.price import get_base_market_data

# Page configuration for rich aesthetics
st.set_page_config(
    page_title="Crocodile — Base On-chain Analytics",
    page_icon="🐊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom styles for vibrant dark theme / glassmorphism look
st.markdown("""
<style>
    .reportview-container {
        background: #0d1117;
    }
    .metric-card {
        background: rgba(20, 184, 166, 0.05);
        border: 1px solid rgba(20, 184, 166, 0.2);
        border-radius: 8px;
        padding: 15px;
        text-align: center;
    }
    .metric-value {
        font-size: 24px;
        font-weight: bold;
        color: #14B8A6;
    }
    .metric-label {
        font-size: 14px;
        color: #8b949e;
    }
</style>
""", unsafe_allow_html=True)

st.title("🔵 Built for the Base Ecosystem — Live On-chain DEX Dashboard")
st.markdown("""
This dashboard showcases **Crocodile's** capability to extract, normalize, and analyze live on-chain DEX data directly from the **Base mainnet** (polling Uniswap V3 and Aerodrome Finance).
""")

# Sidebar selection
st.sidebar.header("🐊 Settings")
rpc_url = st.sidebar.text_input("Base RPC URL", "https://mainnet.base.org")

pair_selection = st.sidebar.selectbox(
    "Select Token Pair",
    ["WETH/USDC", "AERO/USDC", "cbBTC/USDC", "DEGEN/WETH", "WELL/WETH"]
)

refresh_btn = st.sidebar.button("🔄 Refresh Data")

# Standardize selection
symbol = pair_selection.replace("/", "-").upper()
spec = POOL_SPECS.get(symbol)

if not spec:
    st.error(f"Spec for {symbol} not found.")
else:
    st.sidebar.markdown(f"**Pool Protocol:** `{spec['type'].upper()}`")
    st.sidebar.markdown(f"**Token 0:** `{spec['token0']}`")
    st.sidebar.markdown(f"**Token 1:** `{spec['token1']}`")
    if "fee" in spec:
        st.sidebar.markdown(f"**Fee Tier:** `{spec['fee'] / 10000}%`")
    if "stable" in spec:
        st.sidebar.markdown(f"**Stable Pool:** `{spec['stable']}`")

    st.subheader(f"📊 Live Metrics for {pair_selection} on Base")

    # Fetch data using our MCP server helpers
    with st.spinner("Fetching live on-chain data from Base..."):
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            st.error("Could not connect to the Base RPC. Please verify the RPC URL.")
        else:
            try:
                # 1. Fetch real-time market data
                # Run async helper inside sync Streamlit using loop
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                data = loop.run_until_complete(get_base_market_data(symbol, rpc_url))
                loop.close()
                
                if "error" in data:
                    st.error(f"Error fetching data: {data['error']}")
                else:
                    # Display metrics in 4 columns
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Current Price ({spec['token1']})</div>
                            <div class="metric-value">{data['price']:.6f if data['price'] < 1.0 else f"{data['price']:.2f}"}</div>
                        </div>
                        """, unsafe_allowed_html=True)
                    with col2:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">1-Hour Volume ({spec['token1']})</div>
                            <div class="metric-value">${data['volume_1h_quote']:.2f}</div>
                        </div>
                        """, unsafe_allowed_html=True)
                    with col3:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Reserve {spec['token0']}</div>
                            <div class="metric-value">{data['reserve0']:,.2f}</div>
                        </div>
                        """, unsafe_allowed_html=True)
                    with col4:
                        st.markdown(f"""
                        <div class="metric-card">
                            <div class="metric-label">Reserve {spec['token1']}</div>
                            <div class="metric-value">{data['reserve1']:,.2f}</div>
                        </div>
                        """, unsafe_allowed_html=True)

                    st.write("")
                    st.write(f"*DEX Pool Contract Address:* `{data['pool_address']}` | *Current Block:* `{data['block']}` | *1h Swaps:* `{data['num_swaps_1h']}`")

                    # 2. Fetch recent trade list for charts & tables
                    st.markdown("---")
                    st.subheader("⏱️ Recent Trade Feed")
                    
                    # We query the last 5000 blocks for historical logs to show a chart and table
                    swap_topic = (
                        "0xc42079f94a6350d7e6235f29174924f9287a20ac8e91c97b870daee5297f6e85"
                        if spec["type"] == "uniswap_v3"
                        else "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
                    )
                    
                    t0_addr = w3.to_checksum_address(TOKENS[str(spec["token0"])])
                    t1_addr = w3.to_checksum_address(TOKENS[str(spec["token1"])])
                    is_flipped = int(t1_addr, 16) < int(t0_addr, 16)
                    
                    # Fetch logs for recent blocks
                    latest_block = w3.eth.block_number
                    logs = w3.eth.get_logs({
                        "address": data["pool_address"],
                        "topics": [swap_topic],
                        "fromBlock": max(0, latest_block - 4000),
                        "toBlock": latest_block
                    })
                    
                    trades_list = []
                    for lg in reversed(logs):
                        log_data = lg["data"]
                        if spec["type"] == "uniswap_v3":
                            amount0 = int.from_bytes(log_data[0:32], byteorder='big', signed=True)
                            amount1 = int.from_bytes(log_data[32:64], byteorder='big', signed=True)
                            if not is_flipped:
                                abs_base = abs(amount0) / (10 ** int(spec["decimals0"]))
                                abs_quote = abs(amount1) / (10 ** int(spec["decimals1"]))
                                is_buy = amount0 < 0
                            else:
                                abs_base = abs(amount1) / (10 ** int(spec["decimals0"]))
                                abs_quote = abs(amount0) / (10 ** int(spec["decimals1"]))
                                is_buy = amount1 < 0
                        else:
                            amt0_in = int.from_bytes(log_data[0:32], byteorder='big', signed=False)
                            amt1_in = int.from_bytes(log_data[32:64], byteorder='big', signed=False)
                            amt0_out = int.from_bytes(log_data[64:96], byteorder='big', signed=False)
                            amt1_out = int.from_bytes(log_data[96:128], byteorder='big', signed=False)
                            if not is_flipped:
                                abs_base = (amt0_in if amt0_in > 0 else amt0_out) / (10 ** int(spec["decimals0"]))
                                abs_quote = (amt1_in if amt1_in > 0 else amt1_out) / (10 ** int(spec["decimals1"]))
                                is_buy = amt0_out > 0
                            else:
                                abs_base = (amt1_in if amt1_in > 0 else amt1_out) / (10 ** int(spec["decimals0"]))
                                abs_quote = (amt0_in if amt0_in > 0 else amt0_out) / (10 ** int(spec["decimals1"]))
                                is_buy = amt1_out > 0
                                
                        price = abs_quote / abs_base if abs_base > 0 else 0.0
                        trades_list.append({
                            "Block": lg["blockNumber"],
                            "Tx Hash": lg["transactionHash"].hex()[:10] + "...",
                            "Side": "BUY" if is_buy else "SELL",
                            "Price": price,
                            "Amount": abs_base,
                            "Volume (USDC)": abs_quote if "USDC" in symbol or "USDbC" in symbol else abs_quote * data["price"]
                        })
                        
                    df_trades = pd.DataFrame(trades_list)
                    
                    if df_trades.empty:
                        st.warning("No recent trades found in the last 4000 blocks (~2 hours).")
                    else:
                        # Split view: Chart on left, Table on right
                        chart_col, table_col = st.columns([3, 2])
                        with chart_col:
                            st.subheader("Price Trend")
                            # Plot price trend chronologically
                            df_chron = df_trades.iloc[::-1].copy()
                            df_chron.reset_index(drop=True, inplace=True)
                            st.line_chart(df_chron, x=None, y="Price", use_container_width=True)
                        with table_col:
                            st.subheader("Trade History")
                            st.dataframe(df_trades.head(50), use_container_width=True)
            except Exception as e:
                st.error(f"Error querying on-chain data: {e}")
                import traceback
                st.code(traceback.format_exc())

st.markdown("---")
st.markdown("""
<div align="center">
    <small>Crocodile Base L2 On-chain Analytics Engine © 2026. Built with Web3, DuckDB and Streamlit.</small>
</div>
""", unsafe_allow_html=True)
