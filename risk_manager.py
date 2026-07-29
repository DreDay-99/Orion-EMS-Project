import json
import os
import time
from datetime import datetime, timedelta

# UNIFIED STATE FILE
STATE_FILE = "orion_master_state.json"

class RiskManager:
    def __init__(self, max_consecutive_losses=6, cooldown_hours=4, shorting_threshold=2000.0):
        self.max_losses = max_consecutive_losses
        self.cooldown_duration = timedelta(hours=cooldown_hours)
        self.shorting_threshold = shorting_threshold
        
        # Unified state framework (merges session data and risk data)
        self.state = {
            "capital": 0.00,
            "wins": 0,
            "losses": 0,
            "consecutive_losses": 0,
            "shorts_unlocked": False,
            "cooldown_until": None 
        }
        self.load_state()

    def load_state(self):
        """Recovers the exact operational state from the hard drive."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    saved_state = json.load(f)
                    self.state.update(saved_state)
            except Exception:
                print("[WARNING] State file corrupted, initializing fresh defaults.")
                self.save_state()

    def save_state(self):
        """Persists the state to disk so it survives system reboots."""
        with open(STATE_FILE, 'w') as f:
            json.dump(self.state, f, indent=4)

    def update_capital(self, new_capital):
        self.state["capital"] = new_capital
        self.save_state()

    def is_cooling_down(self):
        """Checks if the bot is currently serving a volatility timeout."""
        if not self.state["cooldown_until"]:
            return False
            
        cooldown_time = datetime.fromisoformat(self.state["cooldown_until"])
        current_time = datetime.now()
        
        if current_time < cooldown_time:
            remaining = cooldown_time - current_time
            remaining_mins = int(remaining.total_seconds() / 60)
            print(f"[VOLATILITY LOCK] Orion is paused. Time remaining: {remaining_mins} minutes.")
            return True
        else:
            print("⏳ Cooldown period expired. Resetting loss counters and resuming trading.")
            self.state["cooldown_until"] = None
            self.state["consecutive_losses"] = 0
            self.save_state()
            return False

    def check_execution_clearance(self, side):
        """The master gatekeeper. Your execution loop calls this before every entry."""
        if self.is_cooling_down():
            return False
            
        if side in ["SELL", "SHORT"] and not self.state["shorts_unlocked"]:
            return False
            
        return True

    def register_trade_result(self, is_win, closing_capital):
        """Updates metrics immediately after a trade fills and settles."""
        self.state["capital"] = closing_capital
        
        if is_win:
            self.state["wins"] += 1
            self.state["consecutive_losses"] = 0
            print(f"[TRADE RECORDED] Win logged. Resetting consecutive loss streak to 0.")
        else:
            self.state["losses"] += 1
            self.state["consecutive_losses"] += 1
            print(f"[TRADE RECORDED] Loss logged. Current consecutive streak: {self.state['consecutive_losses']}/{self.max_losses}")
            
            if self.state["consecutive_losses"] >= self.max_losses:
                resume_time = datetime.now() + self.cooldown_duration
                self.state["cooldown_until"] = resume_time.isoformat()
                print(f"\n[🚨 CIRCUIT BREAKER] Hit {self.max_losses} consecutive losses.")
                print(f"Entering volatility lockdown until: {resume_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                
        if self.state["capital"] >= self.shorting_threshold and not self.state["shorts_unlocked"]:
            print("🎉 Milestone target reached! Shorting permissions unlocked.")
            self.state["shorts_unlocked"] = True
            
        self.save_state()