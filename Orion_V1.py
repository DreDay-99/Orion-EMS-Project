import os
import json
import time
import threading
import urllib.request
from datetime import datetime
import tkinter as tk
import numpy as np
import websocket
import atexit
import base64
from dotenv import load_dotenv
from solders.keypair import Keypair
from jup_python_sdk.clients.ultra_api_client import UltraApiClient 
from jup_python_sdk.models.ultra_api.ultra_order_request_model import UltraOrderRequest 
from jup_python_sdk.models.ultra_api.ultra_execute_request_model import UltraExecuteRequest 
from solders.transaction import VersionedTransaction 
from solders.message import to_bytes_versioned 
from flask import Flask, jsonify
from flask_cors import CORS
import logging

# Ensure these match your local file structure
from execution_bridge import get_live_on_chain_balance, get_true_blockchain_state, orion_wallet
from risk_manager import RiskManager   

# Master Gatekeeper
risk_gate = RiskManager(
    max_consecutive_losses=3, 
    cooldown_hours=1, 
    shorting_threshold=2000.0
)

def emergency_flatten():
    print("\n[CRITICAL] Script terminating! Flatting must be done manually via UI or Phantom Wallet.")
    
print("[SAFE] Bot offline. Shutting down securely.")
atexit.register(emergency_flatten)

# ==========================================
#      CORE SYSTEM ASSET SETUP
# ==========================================
SYMBOL = "SOL-USD"  
STATE_FILE = "orion_session.json"

# Core Mint Layout Constants
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"

state = {
    'price': 0.0,
    'candles': [], 
    'bids': [],
    'asks': [],
    'status': "INITIALIZING CORES...",
    'atr': 0.0,
    'cycle': "ANALYZING...", 
    'ws': None,
    'paused': False,
    'trade_active': False,
    'is_exiting': False,  
    'trade_type': "NONE", 
    'min_profit_secured': False, 
    'entry_price': 0.0,
    'stop_price': 0.0,
    'initial_stop_distance': 0.0, 
    'pos_size': 0.0, 
    'capital': 0.0,  
    'wins': 0,
    'losses': 0,
    # --- ICT PREDATORY VARIABLES ---
    'swing_high': 0.0,
    'swing_low': 0.0,
    'equilibrium': 0.0,
    'gp_top': 0.0,    
    'gp_bottom': 0.0, 
    'key_opens': {'midnight': None, 'ny_open': None, 'pm_open': None},
    'active_sweep': None, 
    'reversal_confirmed': False,
    'ifvg_50_level': 0.0
}

RISK_PCT = 0.015   
current_candle_minute = None
current_candle = {}

# ==========================================
#  LOCAL DATA PERSISTENCE ENGINE
# ==========================================
def load_session_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                state['wins'] = data.get('wins', 0)
                state['losses'] = data.get('losses', 0)
                state['capital'] = data.get('capital', 100.00) # Default if none
        except Exception:
            pass

def save_session_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                'capital': state['capital'],
                'wins': state['wins'],
                'losses': state['losses']
            }, f, indent=4)
    except Exception:
        pass

def log_trade(entry, exit_price, account_pnl_pct, trade_type):
    log_path = os.path.join(os.path.expanduser("~"), "Desktop", "trade_history.csv")
    
    if account_pnl_pct > 0:
        state['wins'] += 1
    else:
        state['losses'] += 1
        
    save_session_state()
 
    file_exists = os.path.isfile(log_path)
    try:
        with open(log_path, "a") as f:
            if not file_exists:
                f.write("Timestamp,Type,Entry_Price,Exit_Price,PnL_Percent,Updated_Balance\n")
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write(f"{timestamp},{trade_type},{entry:.4f},{exit_price:.4f},{account_pnl_pct*100:.2f}%,{state['capital']:.2f}\n")
    except Exception:
        pass

def get_target_balance(client, wallet_address, target_mint):
    """Queries wallet balances and extracts raw stringified amounts into integers."""
    try:
        balance_data = client.balances(wallet_address)
        token_entry = balance_data.get("SOL") if target_mint == SOL_MINT else balance_data.get(target_mint)
        if token_entry and "amount" in token_entry:
            return int(token_entry["amount"])
    except Exception as e:
        print(f"⚠️ Balance Check Error: {e}")
    return 0

# ==========================================
#  REST API HISTORICAL BOOTSTRAP 
# ==========================================
def bootstrap_historical_candles(target_symbol):
    try:
        state['status'] = f"BOOTSTRAPPING HISTORICAL {target_symbol.upper()} MATRIX..."
        url = f"https://api.exchange.coinbase.com/products/{target_symbol.upper()}/candles?granularity=300"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, timeout=5) as response:
            raw_data = json.loads(response.read().decode())
        
        raw_data.reverse()
        raw_data = raw_data[-250:] 
        
        pulled_candles = []
        for k in raw_data:
            t = int(k[0]) * 1000 
            t_str = datetime.fromtimestamp(t / 1000).strftime("%H:%M")
            pulled_candles.append({
                't': t, 'time_str': t_str,
                'o': float(k[3]), 'h': float(k[2]), 'l': float(k[1]), 'c': float(k[4]), 'v': float(k[5])
            })
        return pulled_candles
    except Exception as e:
        state['status'] = f"BOOTSTRAP ERROR: {e}"
        return []

# ==========================================
#     ASYNCHRONOUS DATA CORE PIPELINE
# ==========================================
def run_engine():
    global SYMBOL, current_candle_minute, current_candle
    
    print("[INIT] Fetching real-time collateral from Solana blockchain...")
    load_session_state()
    
    try:
        load_dotenv()
        raw_key = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))
        bot_keypair = Keypair.from_bytes(raw_key)
        jup_client = UltraApiClient(private_key_env_var="SOLANA_PRIVATE_KEY")
        print("[INIT JUPITER] Ultra Low-Latency Execution Client Mounted.")
    except Exception as je:
        print(f"[CRITICAL JUPITER MOUNT ERROR] Keypair or SDK instantiation failed: {je}")

    try:
        balances = get_live_on_chain_balance()
        if isinstance(balances, dict):
            state['capital'] = balances.get('USDC', 0.0) 
            state['raw_sol_balance'] = balances.get('SOL', 0.0)
            print(f"[SUCCESS] Mainnet dual-sync complete!")
            print(f"          -> Trading Ammo (USDC): ${state['capital']:.2f}")
            print(f"          -> Network Gas (SOL): {state['raw_sol_balance']:.4f} SOL")
        else:
            print("[WARNING] Could not verify on-chain funds. Falling back to local file.")
    except Exception as e:
        print(f"[CRITICAL] Error syncing blockchain balance: {e}")

    # ==========================================
    # BACKGROUND EXECUTION THREADS
    # ==========================================
    def background_buy_execution(usdc_lamports, current_price, stop_distance, calc_stop_price, calc_pos_size):
        try:
            order_request = UltraOrderRequest(
                input_mint=USDC_MINT,
                output_mint=SOL_MINT,
                amount=int(usdc_lamports),
                slippage_bps=50, 
                taker=str(bot_keypair.pubkey()),
            )
            raw_order = jup_client.order(order_request)
            tx = VersionedTransaction.from_bytes(base64.b64decode(raw_order["transaction"]))
            user_index = next(i for i, key in enumerate(tx.message.account_keys) if key == bot_keypair.pubkey())
            signatures = list(tx.signatures)
            signatures[user_index] = bot_keypair.sign_message(to_bytes_versioned(tx.message))
            fully_signed_tx = VersionedTransaction.populate(tx.message, signatures)
            final_tx_b64 = base64.b64encode(bytes(fully_signed_tx)).decode("utf-8")
            
            execute_kwargs = {}
            for field in UltraExecuteRequest.__annotations__.keys():
                if field in ['signed_transaction', 'transaction']:
                    execute_kwargs[field] = final_tx_b64
                    continue
                camel_field = ''.join(word.capitalize() if i > 0 else word for i, word in enumerate(field.split('_')))
                if field in raw_order: execute_kwargs[field] = raw_order[field]
                elif camel_field in raw_order: execute_kwargs[field] = raw_order[camel_field]
            
            execution_response = jup_client.execute(UltraExecuteRequest(**execute_kwargs))
            
            if execution_response and "signature" in execution_response:
                print(f"🚀 [JUPITER SUCCESS] Sniper Order Filled On Mainnet! Sig: {execution_response['signature']}")
                state['status'] = f"LONG POSITION FILLED AT ${current_price:.2f}"
            else:
                print("❌ [JUPITER EXECUTOR] Drop detected or unverified response layout.")
                state['status'] = "⚠️ JUPITER SWAP TRANSACTION DROP ENCOUNTERED"
                state['trade_active'] = False # Unlock on fail
        except Exception as tx_err:
            print(f"❌ [JUPITER CRYPTO EXECUTION ABORT]: {tx_err}")
            state['status'] = f"⚠️ CRITICAL SYSTEM STRAT RUNTIME REJECTION"
            state['trade_active'] = False # Unlock on fail

    def background_sell_execution(exit_reason, current_price, take_profit_target=None):
        try:
            actual_sol_held = get_target_balance(jup_client, str(bot_keypair.pubkey()), SOL_MINT)
            if actual_sol_held <= 0:
                print("⚠️ Exit Aborted: Blockchain lookup returned 0 SOL. Position likely already swept.")
                state['trade_active'] = False
                state['is_exiting'] = False # Unlock
                state['trade_type'] = "NONE"
                return

            order_request = UltraOrderRequest(
                input_mint=SOL_MINT,
                output_mint=USDC_MINT,
                amount=actual_sol_held, 
                slippage_bps=200, 
                taker=str(bot_keypair.pubkey()),
            )
            raw_order = jup_client.order(order_request)
            tx = VersionedTransaction.from_bytes(base64.b64decode(raw_order["transaction"]))
            user_index = next(i for i, key in enumerate(tx.message.account_keys) if key == bot_keypair.pubkey())
            signatures = list(tx.signatures)
            signatures[user_index] = bot_keypair.sign_message(to_bytes_versioned(tx.message))
            fully_signed_tx = VersionedTransaction.populate(tx.message, signatures)
            final_tx_b64 = base64.b64encode(bytes(fully_signed_tx)).decode("utf-8")
            
            execute_kwargs = {}
            for field in UltraExecuteRequest.__annotations__.keys():
                if field in ['signed_transaction', 'transaction']:
                    execute_kwargs[field] = final_tx_b64
                    continue
                camel_field = ''.join(word.capitalize() if i > 0 else word for i, word in enumerate(field.split('_')))
                if field in raw_order: execute_kwargs[field] = raw_order[field]
                elif camel_field in raw_order: execute_kwargs[field] = raw_order[camel_field]
            
            response = jup_client.execute(UltraExecuteRequest(**execute_kwargs))

            if response and "signature" in response:
                state['trade_active'] = False
                state['is_exiting'] = False # Unlock
                state['trade_type'] = "NONE"
                
                # Reset ICT states after trade concludes
                state['active_sweep'] = None
                state['reversal_confirmed'] = False
                state['ifvg_50_level'] = 0.0
                
                state['status'] = f"🛑 {exit_reason} EXITED. Sig: {response['signature']}"
                
                if exit_reason == "TAKE_PROFIT" and take_profit_target:
                    actual_pnl_usd = (take_profit_target - state['entry_price']) * state['pos_size']
                    account_pnl_pct = actual_pnl_usd / state['capital']
                    state['capital'] += actual_pnl_usd
                    log_trade(state['entry_price'], take_profit_target, account_pnl_pct, "LONG")
                    risk_gate.register_trade_result(is_win=True, closing_capital=state['capital'])
                else: 
                    actual_pnl_usd = (state['stop_price'] - state['entry_price']) * state['pos_size']
                    account_pnl_pct = actual_pnl_usd / state['capital']
                    state['capital'] += actual_pnl_usd
                    log_trade(state['entry_price'], state['stop_price'], account_pnl_pct, "LONG")
                    
                    is_actual_win = actual_pnl_usd > 0
                    risk_gate.register_trade_result(is_win=is_actual_win, closing_capital=state['capital'])
            else:
                print(f"⚠️ EXIT FAILED: Retaining position state to try again next tick.")
                state['is_exiting'] = False # Unlock so the bot can try again
        except Exception as exit_err:
            print(f"⚠️ Exit Error: {exit_err} - Position remains open on-chain!")
            state['is_exiting'] = False # Unlock so the bot can try again

    while True:
        if not state['candles']:
            state['candles'] = bootstrap_historical_candles(SYMBOL)
            if state['candles']:
                last_c = state['candles'][-1]
                state['price'] = last_c['c']

        def on_message(ws, message):
            global current_candle_minute, current_candle
            try:
                data = json.loads(message)
                
                if data.get('type') == 'snapshot' or data.get('type') == 'l2update':
                    if 'bids' in data or 'asks' in data:
                        state['bids'] = data.get('bids', [])[:10]
                        state['asks'] = data.get('asks', [])[:10]

                if data.get('type') == 'ticker' and 'price' in data:
                    c = float(data['price'])
                    state['price'] = c
                    
                    tick_time = datetime.now()
                    tick_minute = tick_time.minute
                    tick_hour = tick_time.hour
                    t_ms = int(time.time() * 1000)
                    
                    current_5m_bucket = (tick_minute // 5) * 5  
                    t_str = f"{tick_time.hour:02d}:{current_5m_bucket:02d}" 
                    
                    if current_candle_minute is None:
                        current_candle_minute = current_5m_bucket
                        current_candle = {'t': t_ms, 'time_str': t_str, 'o': c, 'h': c, 'l': c, 'c': c, 'v': 0.0}
                    
                    if current_5m_bucket == current_candle_minute:
                        current_candle['c'] = c
                        if c > current_candle['h']: current_candle['h'] = c
                        if c < current_candle['l']: current_candle['l'] = c
                        
                        if state['candles']:
                            if state['candles'][-1]['time_str'] == t_str:
                                state['candles'][-1] = current_candle.copy()
                            else:
                                state['candles'].append(current_candle.copy())
                    else:
                        if state['candles'] and state['candles'][-1]['time_str'] == current_candle['time_str']:
                            state['candles'][-1] = current_candle.copy()
                        else:
                            state['candles'].append(current_candle.copy())
                       
                        if len(state['candles']) > 250:
                            state['candles'].pop(0)
                        
                        current_candle_minute = current_5m_bucket
                        current_candle = {'t': t_ms, 'time_str': t_str, 'o': c, 'h': c, 'l': c, 'c': c, 'v': 0.0}

                    # --- TIME-BASED KEY OPENS TRACKING ---
                    if tick_minute == 0 and tick_time.second < 5:
                        if tick_hour == 0: state['key_opens']['midnight'] = c
                        elif tick_hour == 10: state['key_opens']['ny_open'] = c
                        elif tick_hour == 18: state['key_opens']['pm_open'] = c

                    closed_ranges = [candle['h'] - candle['l'] for candle in state['candles'][:-1]]
                    state['atr'] = np.mean(closed_ranges[-14:]) if len(closed_ranges) >= 14 else (c * 0.005)

                    # --- MACRO FIBONACCI (PREMIUM/DISCOUNT) ---
                    if len(state['candles']) > 100:
                        recent_100 = state['candles'][-100:]
                        state['swing_high'] = max(candle['h'] for candle in recent_100)
                        state['swing_low'] = min(candle['l'] for candle in recent_100)
                        range_dist = state['swing_high'] - state['swing_low']
                        
                        state['equilibrium'] = state['swing_high'] - (range_dist * 0.5)
                        state['gp_top'] = state['swing_high'] - (range_dist * 0.618)
                        state['gp_bottom'] = state['swing_high'] - (range_dist * 0.786)
                        
                    if c > state['equilibrium']:
                        state['cycle'] = "PREMIUM"
                    else:
                        state['cycle'] = "DISCOUNT"

                    # ----------------------------------------------------
                    # PREDATORY ICT ENTRY LOGIC
                    # ----------------------------------------------------
                    if not state['trade_active'] and not state['is_exiting']:
                        if state['paused']:
                            state['status'] = f"STREAM LIVE | AUTOMATION PAUSED BY USER PILOT"
                        elif state['swing_high'] > 0:
                            
                            if c > state['equilibrium']:
                                state['status'] = "BLOCKED | PRICE IN PREMIUM MATRIX"
                                state['active_sweep'] = None 
                            else:
                                state['status'] = "HUNTING IN DISCOUNT | WAITING FOR SWEEP"
                                
                                tolerance = state['atr'] * 0.25 
                                for name, level in state['key_opens'].items():
                                    if level is not None:
                                        if c < (level + tolerance) and not state['active_sweep']:
                                            state['active_sweep'] = {'name': name, 'level': level, 'lowest': c, 'reversal_high': c}
                                            state['status'] = f"🚨 {name.upper()} SWEEP DETECTED! WATCHING FOR REVERSAL."
                                            print(f"[{tick_time.strftime('%H:%M:%S')}] Sweep Detected on {name} at {c}")

                                if state['active_sweep']:
                                    sweep_lvl = state['active_sweep']['level']
                                    
                                    if c < state['active_sweep']['lowest']:
                                        state['active_sweep']['lowest'] = c
                                    if c > state['active_sweep'].get('reversal_high', c):
                                        state['active_sweep']['reversal_high'] = c
                                        
                                    if c > (sweep_lvl + state['atr'] * 1.5):
                                        state['reversal_confirmed'] = True

                                    if state['reversal_confirmed']:
                                        impulse_range = state['active_sweep']['reversal_high'] - state['active_sweep']['lowest']
                                        state['ifvg_50_level'] = state['active_sweep']['lowest'] + (impulse_range * 0.5)
                                        entry_zone_top = state['ifvg_50_level'] + tolerance
                                        
                                        state['status'] = f"⚡ IFVG RETEST SET AT ${state['ifvg_50_level']:.2f}"

                                        if c <= entry_zone_top:
                                            true_positions = get_true_blockchain_state()
                                            if true_positions is None:
                                                pass 
                                            elif len(true_positions) > 0:
                                                pass
                                            else:
                                                if risk_gate.check_execution_clearance(side="BUY"):
                                                    stop_distance = max(c - state['active_sweep']['lowest'], state['atr'] * 1.0)
                                                    calc_stop_price = c - stop_distance
                                                    risk_usd = state['capital'] * RISK_PCT
                                                    
                                                    if risk_usd < 0.50: risk_usd = 0.50
                                                    calc_pos_size = risk_usd / stop_distance
                                                    estimated_cost_usdc = calc_pos_size * c
                                                    
                                                    if estimated_cost_usdc < 10.0 and state['capital'] >= 10.50:
                                                        calc_pos_size = 10.0 / c
                                                        estimated_cost_usdc = 10.0
                                                        
                                                    max_allowed_spend = max(0.0, state['capital'] - 0.50)
                                                    if estimated_cost_usdc > max_allowed_spend:
                                                        calc_pos_size = max_allowed_spend / c
                                                    
                                                    if calc_pos_size * c < 5.0:
                                                        state['status'] = "⚠️ INSUFFICIENT BALANCE FOR MINIMUM MAINNET SIZE"
                                                    else:
                                                        state['trade_active'] = True
                                                        state['trade_type'] = "LONG"
                                                        state['entry_price'] = c
                                                        state['stop_price'] = calc_stop_price
                                                        state['initial_stop_distance'] = stop_distance
                                                        state['pos_size'] = calc_pos_size
                                                        state['min_profit_secured'] = False
                                                        state['status'] = f"ALGO LONG INITIATING | SIZE: {calc_pos_size:.2f} SOL"
                                                        
                                                        usdc_execution_lamports = int(calc_pos_size * c * 1_000_000)
                                                        threading.Thread(target=background_buy_execution, args=(usdc_execution_lamports, c, stop_distance, calc_stop_price, calc_pos_size), daemon=True).start()

                   # ----------------------------------------------------
                    # TRADE MANAGEMENT LOGIC (LIVE EXPOSURE)
                    # ----------------------------------------------------
                    elif state['trade_active']:
                        target_dist = state['initial_stop_distance']

                        if state['trade_type'] == "LONG":
                            current_pnl_usd = (c - state['entry_price']) * state['pos_size']
                            
                            buffer_multiplier = 1.5  
                            buffer_activation_price = state['entry_price'] + (target_dist * buffer_multiplier) 
                            take_profit_target = state['entry_price'] + (target_dist * 8.0) 

                            if c >= take_profit_target and not state['is_exiting']:
                                state['is_exiting'] = True # LOCK
                                state['status'] = f"🎯 SNIPER TP HIT. INITIATING JUPITER EXIT..."
                                threading.Thread(target=background_sell_execution, args=("TAKE_PROFIT", c, take_profit_target), daemon=True).start()

                            else:
                                if c >= buffer_activation_price and not state['min_profit_secured']:
                                    state['min_profit_secured'] = True
                                    break_even_price = state['entry_price'] + (state['entry_price'] * 0.0005)
                                    if break_even_price > state['stop_price']:
                                        state['stop_price'] = break_even_price
                                    state['status'] = f"🛡️ {buffer_multiplier}x BUFFER CLEARED. STOP SECURED AT BREAK-EVEN."

                                if state['min_profit_secured'] and len(state['candles']) >= 2:
                                    last_closed_candle = state['candles'][-2]
                                    closed_low = last_closed_candle['l']
                                    raw_buffer_floor = closed_low - (state['atr'] * 3.0) 
                                    if raw_buffer_floor > state['stop_price']:
                                        state['stop_price'] = raw_buffer_floor

                                if c <= state['stop_price'] and not state['is_exiting']:
                                    state['is_exiting'] = True # LOCK
                                    state['status'] = f"🛑 TRAILING STOP TRIGGERED. INITIATING JUPITER EXIT..."
                                    threading.Thread(target=background_sell_execution, args=("STOP_LOSS", c), daemon=True).start()
                                    
                                elif not state['is_exiting']:
                                    if not state['min_profit_secured']:
                                        state['status'] = f"LONG ACTIVE | PNL: ${current_pnl_usd:.2f} | AWAITING {buffer_multiplier}x BUFFER"
                                    else:
                                        state['status'] = f"LONG ACTIVE | PNL: ${current_pnl_usd:.2f} | TRAILING STOP: ${state['stop_price']:.2f}"

            except Exception as e:
                print(f"⚠️ [ENGINE FAULT] Live Stream Error: {e}")

        def on_error(ws, error):
            state['status'] = f"FEED INTERRUPTION: {error}"

        def on_close(ws, code, msg):
            state['status'] = "NETWORK STREAM DISCONNECTED. RECONNECTING..."

        def on_open(ws):
            state['status'] = f"LIVE DATA STREAM ACTIVE FOR {SYMBOL.upper()}"
            ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": [SYMBOL],
                "channels": ["ticker", "level2"]
            }))

        endpoint = "wss://ws-feed.exchange.coinbase.com"
        ws = websocket.WebSocketApp(endpoint, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
        state['ws'] = ws
        
        def wallet_sync_loop():
            while True:
                time.sleep(60)
                try:
                    if not state['trade_active'] and not state['is_exiting']:
                        live_balances = get_live_on_chain_balance()
                        if isinstance(live_balances, dict) and 'USDC' in live_balances:
                            state['capital'] = live_balances['USDC']
                except Exception:
                    pass

        sync_thread = threading.Thread(target=wallet_sync_loop, daemon=True)
        sync_thread.start()

        ws.run_forever(ping_interval=30)
        time.sleep(2)

# ==========================================
#     HEADLESS API SERVER (REPLACES TKINTER)
# ==========================================

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app) 

@app.route('/api/state', methods=['GET'])
def api_state():
    full_state = {
        "price": state.get('price', 0.0),
        "capital": state.get('capital', 1000.0),
        "raw_sol_balance": state.get('raw_sol_balance', 0.0),
        "paused": state.get('paused', False),
        "cycle": state.get('cycle', "ANALYZING"),
        "wins": state.get('wins', 0),
        "losses": state.get('losses', 0),
        "trade_active": state.get('trade_active', False),
        "entry_price": state.get('entry_price', 0.0),
        "stop_price": state.get('stop_price', 0.0),
        "pos_size": state.get('pos_size', 0.0),
        "initial_stop_distance": state.get('initial_stop_distance', 0.0),
        "candles": state.get('candles', []),
        "bids": state.get('bids', []),
        "asks": state.get('asks', []),
        "status": state.get('status', "System Online"),
        "swing_high": state.get('swing_high', 0.0),
        "swing_low": state.get('swing_low', 0.0),
        "equilibrium": state.get('equilibrium', 0.0),
        "gp_top": state.get('gp_top', 0.0),
        "gp_bottom": state.get('gp_bottom', 0.0),
        "key_opens": state.get('key_opens', {}),
        "active_sweep": state.get('active_sweep', None),
        "ifvg_50_level": state.get('ifvg_50_level', 0.0)
    }
    return jsonify(full_state)

if __name__ == "__main__":
    threading.Thread(target=run_engine, daemon=True).start()
    print("[SYSTEM] Orion Headless Engine running.")
    print("[SYSTEM] API Broadcasting live at http://localhost:5000/api/state")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)