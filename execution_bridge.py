import os
import json
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.types import TokenAccountOpts
from jup_python_sdk.clients.ultra_api_client import UltraApiClient
from jup_python_sdk.models.ultra_api.ultra_order_request_model import UltraOrderRequest
from solders.transaction import VersionedTransaction
import base64

load_dotenv()

# --- 1. CONSTANTS & NETWORK SETUP ---
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

solana_client = Client("https://mainnet.helius-rpc.com/?api-key=e33556b4-614a-429f-943e-ec3e253d05b2")

# --- 2. WALLET IDENTITY & HARDENED PARSING ---
try:
    raw_key_val = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    
    if raw_key_val.startswith('['):
        key_list = json.loads(raw_key_val)
        keypair = Keypair.from_bytes(bytes(key_list))
    else:
        keypair = Keypair.from_base58_string(raw_key_val)
        
    orion_wallet = keypair.pubkey()
    print(f"[BRIDGE INIT] Mainnet Wallet Linked: {orion_wallet}")
    
except Exception as e:
    print(f"[BRIDGE CRITICAL] Could not parse private key. Ensure it is either a JSON array or Base58 string.")
    print(f"Error details: {e}")
    orion_wallet = None
    keypair = None

try:
    jup_client = UltraApiClient(private_key_env_var="SOLANA_PRIVATE_KEY")
except Exception as e:
    print(f"[BRIDGE CRITICAL] Failed to initialize Jupiter Ultra Client: {e}")
    jup_client = None

# --- 3. BALANCE SYNC ---
def get_live_on_chain_balance():
    if not orion_wallet:
        return {"USDC": 0.0, "SOL": 0.0}
        
    try:
        sol_resp = solana_client.get_balance(orion_wallet)
        sol_bal = float(sol_resp.value) / 1_000_000_000 if sol_resp.value else 0.0
        
        usdc_mint = Pubkey.from_string(USDC_MINT)
        usdc_resp = solana_client.get_token_accounts_by_owner(orion_wallet, TokenAccountOpts(mint=usdc_mint))
        
        usdc_bal = 0.0
        if usdc_resp.value:
            token_acc = usdc_resp.value[0].pubkey
            bal_resp = solana_client.get_token_account_balance(token_acc)
            if bal_resp.value:
                usdc_bal = float(bal_resp.value.ui_amount)
                
        return {"USDC": usdc_bal, "SOL": sol_bal}
    except Exception as e:
        print(f"[BRIDGE ERROR] Dual-Sync failed: {e}")
        return {"USDC": 0.0, "SOL": 0.0}

def get_true_blockchain_state():
    """
    Checks if a position is active by verifying if capital deployed out of USDC,
    syncing directly with the single unified orion_master_state.json
    """
    try:
        balances = get_live_on_chain_balance()
        if not balances or "USDC" not in balances:
            return None
            
        state_file = "orion_master_state.json"
        expected_capital = 0.0 
        
        if os.path.exists(state_file):
            try:
                with open(state_file, 'r') as f:
                    master_data = json.load(f)
                    expected_capital = master_data.get("capital", 0.0)
            except Exception:
                pass
                
        # If we expect to have money but our USDC is depleted, we are in a trade
        if expected_capital > 0 and balances["USDC"] < (expected_capital - 0.50):
            return ["LONG_POSITION_ACTIVE"]
            
        return [] 
    except Exception as e:
        print(f"[BRIDGE ERROR] On-chain position verification failed: {e}")
        return None

# --- 4. EXECUTION ROUTING ---
def execute_live_trade(side, size, price, ticker="SOL-USD"):
    if not jup_client or not keypair:
        print("[BRIDGE ERROR] Jupiter client or Keypair is uninitialized.")
        return False

    if side == "LONG":
        usdc_to_spend = size * price 
        input_mint = USDC_MINT
        output_mint = SOL_MINT
        raw_amount = int(usdc_to_spend * 1_000_000)
        print(f"[BRIDGE] Executing LONG: Spending ${usdc_to_spend:.2f} USDC to buy {size:.4f} SOL...")
    else:
        input_mint = SOL_MINT
        output_mint = USDC_MINT
        raw_amount = int(size * 1_000_000_000)
        print(f"[BRIDGE] Executing CLOSE: Selling {size:.4f} SOL for USDC...")

    if raw_amount <= 0:
        print(f"[BRIDGE ABORT] Calculated execution amount is zero.")
        return False

    try:
        print(f"[BRIDGE] Fetching unsigned transaction from Jupiter...")
        order_request = UltraOrderRequest(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=raw_amount,
            slippage_bps=150,
            taker=str(orion_wallet),
        )
        
        tx_response = jup_client.create_order(order_request)
        tx_bytes = base64.b64decode(tx_response["transaction"])
        
        transaction = VersionedTransaction.from_bytes(tx_bytes)
        transaction.sign([keypair])
        
        print(f"[BRIDGE] Dispatching signed transaction to Mainnet...")
        result = solana_client.send_raw_transaction(bytes(transaction))
        
        print(f"[BRIDGE SUCCESS] Transaction Broadcasted! Signature: {result.value}")
        return True
            
    except Exception as e:
        print(f"\n🚨 --- JUPITER REJECTION --- 🚨")
        print(f"Error: {e}")
        return False