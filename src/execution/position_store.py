"""
Active position inventory. (Fix 23 — Phase 2)

Replaces the hardcoded open_positions=0, deployed_capital=0 placeholders
in the pipeline with persistent JSON-backed state.

Usage:
    store = PositionStore()
    store.add_position(position_dict)
    store.close_position(position_id, net_pnl)
    open_pos = store.open_positions
    deployed = store.deployed_capital
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

POSITION_FILE = Path("data/positions.json")


class PositionStore:
    def __init__(self, position_file: Path = POSITION_FILE):
        self.position_file = position_file
        self._positions: dict = self._load()

    def _load(self) -> dict:
        if self.position_file.exists():
            try:
                return json.loads(self.position_file.read_text(encoding='utf-8'))
            except Exception as e:
                logger.warning("Position store load failed: %s — starting fresh.", e)
        return {}

    def _save(self):
        self.position_file.parent.mkdir(parents=True, exist_ok=True)
        self.position_file.write_text(json.dumps(self._positions, indent=2), encoding='utf-8')

    @property
    def open_positions(self) -> int:
        return sum(1 for p in self._positions.values() if p.get('status') == 'open')

    @property
    def deployed_capital(self) -> float:
        return sum(
            p.get('capital', 0.0)
            for p in self._positions.values()
            if p.get('status') == 'open'
        )

    def add_position(self, position: dict) -> str:
        """
        Record a new open position. Returns position_id.
        position dict should include: underlying, strategy, strike, type,
        entry_price, lots, capital, expiry (YYYY-MM-DD), bias, iv_regime,
        entry_spot, entry_dte.

        Fields 'expiry' and 'entry_spot' are required for 21-DTE monitoring.
        """
        if 'expiry' not in position:
            logger.warning(
                "Position for %s missing 'expiry' field — 21-DTE tracking will be disabled for this position.",
                position.get('underlying', 'UNKNOWN')
            )
        if 'entry_spot' not in position:
            logger.warning(
                "Position for %s missing 'entry_spot' field — entry context unavailable.",
                position.get('underlying', 'UNKNOWN')
            )

        pid = str(uuid.uuid4())[:8]
        self._positions[pid] = {
            **position,
            'position_id': pid,
            'status': 'open',
            'opened_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'closed_at': None,
            'net_pnl': None,
        }
        self._save()
        logger.info("Position %s opened: %s %s %s", pid,
                    position.get('underlying'), position.get('strategy'), position.get('strike'))
        return pid

    def close_position(self, position_id: str, net_pnl: float, reason: str = 'manual'):
        """Mark a position as closed with final P&L."""
        if position_id not in self._positions:
            logger.warning("Position %s not found.", position_id)
            return
        self._positions[position_id].update({
            'status': 'closed',
            'closed_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'net_pnl': net_pnl,
            'exit_reason': reason,
        })
        self._save()
        logger.info("Position %s closed: net_pnl=₹%.2f reason=%s", position_id, net_pnl, reason)

    def get_open(self) -> list:
        """Return list of all open positions."""
        return [p for p in self._positions.values() if p.get('status') == 'open']

    def check_21dte_exits(self) -> list:
        """
        Check all open positions for the 21-DTE exit trigger.

        Returns a list of alert dicts for any position at or below 21 DTE.
        Called automatically at pipeline startup (Step -0.5 in SKILL.md workflow).
        """
        from datetime import datetime as _dt
        alerts = []
        for p in self.get_open():
            expiry_str = p.get('expiry')
            if not expiry_str:
                continue
            try:
                exp = _dt.strptime(expiry_str, '%Y-%m-%d').date()
                dte = (exp - _dt.now().date()).days
                if dte <= 21:
                    alerts.append({
                        'position_id': p['position_id'],
                        'underlying':  p.get('underlying'),
                        'strategy':    p.get('strategy'),
                        'strike':      p.get('strike'),
                        'type':        p.get('type'),
                        'expiry':      expiry_str,
                        'dte_remaining': dte,
                        'action': 'REVIEW — 21-DTE rule triggered. Evaluate close or roll.',
                    })
            except ValueError:
                logger.warning("Position %s has invalid expiry format '%s'. Expected YYYY-MM-DD.",
                               p.get('position_id'), expiry_str)
                continue
        return sorted(alerts, key=lambda x: x['dte_remaining'])

    def summary(self) -> str:
        open_p = self.get_open()
        if not open_p:
            return "No open positions."
        lines = [f"Open Positions ({len(open_p)}):"]
        for p in open_p:
            lines.append(
                f"  [{p['position_id']}] {p.get('underlying')} {p.get('strategy')} "
                f"strike={p.get('strike')} lots={p.get('lots', 1)} "
                f"capital=₹{p.get('capital', 0):.0f} expiry={p.get('expiry', 'n/a')}"
            )
        return '\n'.join(lines)
