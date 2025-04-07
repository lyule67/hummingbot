from decimal import Decimal
from typing import Dict, List, Optional, Set, Union
import time

from pydantic import Field, validator

from hummingbot.client.config.config_data_types import ClientFieldData
from hummingbot.core.data_type.common import OrderType, PositionMode, PriceType, TradeType
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy_v2.controllers import ControllerBase, ControllerConfigBase
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors_info import ExecutorInfo


class SolanaMemeConfig(ControllerConfigBase):
    """
    Configuration for the Solana Memecoin trading controller.
    """
    controller_name: str = "solana_memecoin"
    candles_config: List[CandlesConfig] = []
    
    # Connector settings
    connector_name: str = Field(
        default="jupiter",
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter the connector name (default: jupiter): ",
            prompt_on_new=True
        )
    )
    
    # Trading pair settings
    trading_pair: str = Field(
        default="SOL-USDC",
        client_data=ClientFieldData(
            prompt=lambda cm: "Enter the trading pair (e.g., SOL-USDC): ",
            prompt_on_new=True
        )
    )
    
    # Risk management settings
    max_position_size: Decimal = Field(
        default=Decimal("100"),  # Default max position size in USDC
        client_data=ClientFieldData(is_updatable=True)
    )
    
    min_position_size: Decimal = Field(
        default=Decimal("10"),  # Default min position size in USDC
        client_data=ClientFieldData(is_updatable=True)
    )
    
    max_slippage_pct: Decimal = Field(
        default=Decimal("1.0"),  # Default max slippage percentage
        client_data=ClientFieldData(is_updatable=True)
    )
    
    # Take profit and stop loss settings
    take_profit_pct: Decimal = Field(
        default=Decimal("5.0"),  # Default take profit percentage
        client_data=ClientFieldData(is_updatable=True)
    )
    
    stop_loss_pct: Decimal = Field(
        default=Decimal("3.0"),  # Default stop loss percentage
        client_data=ClientFieldData(is_updatable=True)
    )
    
    # Time limit for positions (in seconds)
    time_limit_seconds: Optional[int] = Field(
        default=3600,  # Default time limit of 1 hour
        client_data=ClientFieldData(is_updatable=True)
    )
    
    # Candle interval for analysis
    interval: str = Field(
        default="1m",
        client_data=ClientFieldData(is_updatable=True)
    )
    
    def update_markets(self, markets: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        """Update markets with the trading pair for this controller."""
        if self.connector_name not in markets:
            markets[self.connector_name] = set()
        markets[self.connector_name].add(self.trading_pair)
        return markets


class SolanaMemeController(ControllerBase):
    """
    Controller for Solana Memecoin trading.
    
    This controller handles:
    - Trade execution for Solana memecoins
    - Position management with take profit and stop loss
    - Integration with external signals
    """
    
    def __init__(self, config: SolanaMemeConfig, *args, **kwargs):
        """Initialize the Solana Memecoin controller."""
        self.config = config
        self.active_positions = {}
        self.pending_signals = []
        
        # Initialize candles config for the trading pair
        self.config.candles_config = [
            CandlesConfig(
                connector=config.connector_name,
                trading_pair=config.trading_pair,
                interval=config.interval,
                max_records=500
            )
        ]
        
        super().__init__(config, *args, **kwargs)
    
    async def update_processed_data(self):
        """Update processed market data for decision making."""
        # Get candles data for analysis
        candles = self.market_data_provider.get_candles_df(
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            interval=self.config.interval,
            max_records=100
        )
        
        if len(candles) > 0:
            # Store latest price and basic metrics
            latest_price = candles["close"].iloc[-1]
            self.processed_data[self.config.trading_pair] = {
                "current_price": latest_price,
                "last_update_time": time.time()
            }
    
    def determine_executor_actions(self) -> List[Union[CreateExecutorAction, StopExecutorAction]]:
        """Determine actions based on signals and current positions."""
        actions = []
        
        # Process any pending trade signals
        for signal in self.pending_signals:
            # Check if we already have an active position for this token
            if signal["token_address"] in self.active_positions:
                continue
                
            # Validate position size
            position_size = Decimal(str(signal["position_size"]))
            if position_size < self.config.min_position_size or position_size > self.config.max_position_size:
                continue
            
            # Create position executor action
            action = self.create_position_executor(
                token_address=signal["token_address"],
                side=signal["direction"],
                position_size=position_size,
                entry_price=Decimal(str(signal["price"])),
                confidence=signal["confidence"]
            )
            
            if action:
                actions.append(action)
                # Track the position
                self.active_positions[signal["token_address"]] = action.executor_config.id
        
        # Clear processed signals
        self.pending_signals = []
        
        return actions
    
    def create_position_executor(
        self,
        token_address: str,
        side: str,
        position_size: Decimal,
        entry_price: Decimal,
        confidence: float
    ) -> Optional[CreateExecutorAction]:
        """
        Create a position executor for a trade.
        
        Args:
            token_address: The token address to trade
            side: Trade direction (buy/sell)
            position_size: Size of the position in USDC
            entry_price: Entry price
            confidence: Confidence score of the signal
        
        Returns:
            CreateExecutorAction if successful, None otherwise
        """
        try:
            # Convert side string to TradeType
            trade_type = TradeType.BUY if side.lower() == "buy" else TradeType.SELL
            
            # Adjust take profit and stop loss based on confidence
            tp_multiplier = min(1.5, max(0.5, confidence))
            sl_multiplier = max(0.5, min(1.5, 1 - confidence))
            
            take_profit = self.config.take_profit_pct * tp_multiplier / Decimal("100")
            stop_loss = self.config.stop_loss_pct * sl_multiplier / Decimal("100")
            
            # Create the executor action
            return CreateExecutorAction(
                controller_id=self.config.id,
                executor_config=PositionExecutorConfig(
                    timestamp=self.market_data_provider.time(),
                    connector_name=self.config.connector_name,
                    trading_pair=self.config.trading_pair,
                    side=trade_type,
                    entry_price=entry_price,
                    amount=position_size / entry_price,  # Convert USDC value to token amount
                    order_type=OrderType.LIMIT,
                    leverage=1,  # No leverage for spot trading
                    position_mode=PositionMode.ONEWAY,
                    triple_barrier_config=TripleBarrierConfig(
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        time_limit=self.config.time_limit_seconds,
                        trailing_stop=None,
                        open_order_type=OrderType.LIMIT,
                        take_profit_order_type=OrderType.LIMIT,
                        stop_loss_order_type=OrderType.MARKET
                    ),
                    custom_info={
                        "token_address": token_address,
                        "confidence": confidence,
                        "signal_time": time.time()
                    }
                )
            )
        except Exception as e:
            self.logger().error(f"Error creating position executor: {str(e)}")
            return None
    
    def add_trade_signal(self, signal: Dict):
        """
        Add a trade signal to be processed.
        
        Args:
            signal: Trade signal with token_address, direction, position_size, price, confidence
        """
        self.pending_signals.append(signal)
        self.logger().info(f"Added trade signal for {signal['token_address']}: {signal['direction']} at {signal['price']}")
    
    def to_format_status(self) -> List[str]:
        """Format status for display."""
        status_lines = []
        status_lines.append(f"Solana Memecoin Controller - {self.config.trading_pair}")
        status_lines.append("=" * 50)
        
        # Show current price if available
        if self.config.trading_pair in self.processed_data:
            current_price = self.processed_data[self.config.trading_pair].get("current_price")
            if current_price:
                status_lines.append(f"Current price: {current_price}")
        
        # Show active positions
        active_positions = len(self.active_positions)
        status_lines.append(f"Active positions: {active_positions}")
        
        # Show pending signals
        pending_signals = len(self.pending_signals)
        status_lines.append(f"Pending signals: {pending_signals}")
        
        # Show configuration
        status_lines.append("\nConfiguration:")
        status_lines.append(f"Max position size: {self.config.max_position_size} USDC")
        status_lines.append(f"Take profit: {self.config.take_profit_pct}%")
        status_lines.append(f"Stop loss: {self.config.stop_loss_pct}%")
        
        return status_lines
