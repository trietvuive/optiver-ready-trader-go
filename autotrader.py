# Copyright 2021 Optiver Asia Pacific Pty. Ltd.
#
# This file is part of Ready Trader Go.
#
#     Ready Trader Go is free software: you can redistribute it and/or
#     modify it under the terms of the GNU Affero General Public License
#     as published by the Free Software Foundation, either version 3 of
#     the License, or (at your option) any later version.
#
#     Ready Trader Go is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU Affero General Public License for more details.
#
#     You should have received a copy of the GNU Affero General Public
#     License along with Ready Trader Go.  If not, see
#     <https://www.gnu.org/licenses/>.
import asyncio
import itertools
import time

from typing import List
from collections import deque

from ready_trader_go import BaseAutoTrader, Instrument, Lifespan, MAXIMUM_ASK, MINIMUM_BID, Side


LOT_SIZE = 10
POSITION_LIMIT = 100
ARBITRAGE_LIMIT = 50
TICK_SIZE_IN_CENTS = 100
MIN_BID_NEAREST_TICK = (MINIMUM_BID + TICK_SIZE_IN_CENTS) // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS
MAX_ASK_NEAREST_TICK = MAXIMUM_ASK // TICK_SIZE_IN_CENTS * TICK_SIZE_IN_CENTS

class AutoTrader(BaseAutoTrader):
    """Example Auto-trader.

    When it starts this auto-trader places ten-lot bid and ask orders at the
    current best-bid and best-ask prices respectively. Thereafter, if it has
    a long position (it has bought more lots than it has sold) it reduces its
    bid and ask prices. Conversely, if it has a short position (it has sold
    more lots than it has bought) then it increases its bid and ask prices.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, team_name: str, secret: str):
        """Initialise a new instance of the AutoTrader class."""
        super().__init__(loop, team_name, secret)
        self.order_ids = itertools.count(1)
        self.bids = dict()
        self.asks = dict()
        self.hedge_bid = set()
        self.hedge_ask = set()
        self.msg_seq = 0
        self.order_timestamps = deque()

        self.position = self.future_bid = self.future_ask = self.delta = 0

    def on_error_message(self, client_order_id: int, error_message: bytes) -> None:
        """Called when the exchange detects an error.

        If the error pertains to a particular order, then the client_order_id
        will identify that order, otherwise the client_order_id will be zero.
        """
        self.logger.warning("error with order %d: %s", client_order_id, error_message.decode())
        if client_order_id != 0 and (client_order_id in self.bids or client_order_id in self.asks):
            self.on_order_status_message(client_order_id, 0, 0, 0)

    """ 
        Check if current message doesn't breach 50 messages limit
        Return true if can send, false if can't due to limit
    """
    def check_message_limit(self) -> bool:
        current_time = time.time()
        while len(self.order_timestamps) > 0 and self.order_timestamps[0] < current_time - 1.01:
            self.order_timestamps.popleft()

        if len(self.order_timestamps) == 50:
            print("Message limit reached! Drop order")
            return False

        print("Process order")
        self.order_timestamps.append(current_time)
        return True

    """
        Wrapper to send bid orders
    """
    def send_bid_order(self, price: int, volume: int, type = Lifespan.GOOD_FOR_DAY) -> bool:
        if not self.check_message_limit():
            return False

        bid_id = next(self.order_ids)
        self.send_insert_order(bid_id, Side.BUY, price, volume, type)
        self.bids[bid_id] = price
        return True

    """
        Wrapper to send ask orders
    """

    def send_ask_order(self, price: int, volume: int, type = Lifespan.GOOD_FOR_DAY) -> bool:
        if not self.check_message_limit():
            return False

        ask_id = next(self.order_ids)
        self.send_insert_order(ask_id, Side.SELL, price, volume, type)
        self.asks[ask_id] = price
        return True

    """
        Wrapper to send hedge orders
        Hedge cannot be ignored, must be sent
        This might be thread-unsafe, but hey, everything here is thread-unsafe anyway amirite :D
    """
    def send_hedge_order(self, price: int, volume: int, side: Side) -> bool:
        while not self.check_message_limit():
            time.sleep(0.1)

        order_id = next(self.order_ids)
        if side == Side.BID:
            self.hedge_bid.add(order_id)
        else:
            self.hedge_ask.add(order_id)

        super().send_hedge_order(order_id, side, price, volume)
        return True

    """
        Wrapper to send cancel orders
        Return False if throttled
    """
    def send_cancel_order(self, order_id: int) -> bool:
        if not self.check_message_limit():
            return False

        super().send_cancel_order(order_id)
        return True

    """
        Cancel all orders that can be arbitraged
        Example: If future trades at 100 and 120, cancel all bid > 120 and ask < 100
    """
    def trim_orders(self) -> None:
        removed_bids = []
        for bid_id, bid in self.bids.items():
            if bid > self.future_ask:
                if self.send_cancel_order(bid_id):
                    removed_bids.append(bid_id)

        removed_asks = []
        for ask_id, ask in self.asks.items():
            if ask < self.future_bid:
                if self.send_cancel_order(ask_id):
                    removed_asks.append(ask_id)

        for bid_id in removed_bids:
            del self.bids[bid_id]

        for ask_id in removed_asks:
            del self.asks[ask_id]

    """
       Cut down on the number of outstanding bid and ask orders
    """

    def limit_book_size(self, bid_limit : int, ask_limit : int) -> None:
        while len(self.bids) > bid_limit:
            bid_id = min(self.bids, key=self.bids.get)
            self.send_cancel_order(bid_id)
            del self.bids[bid_id]

        while len(self.asks) > ask_limit:
            ask_id = max(self.asks, key=self.asks.get)
            self.send_cancel_order(ask_id)
            del self.asks[ask_id]

    """
        arbitrage if bid is higher than ask between ETF/future
        Arbitrage can helps to reduce position as well, but can also limit market making
        Maybe show more preference towards arbitrage that reduce position rather than increase
    """
    def handle_arbitrage(self, ask_prices : List[int], ask_volumes: List[int], 
            bid_prices: List[int], bid_volumes: List[int]) -> None:

        if ask_prices[0] < self.future_bid:
            # arbitrage, buy etf and sell future
            buy_volume = min(ask_volumes[0], ARBITRAGE_LIMIT - self.position)
            buy_price = ask_prices[0]

            if buy_volume > 0:
                self.send_bid_order(buy_price, buy_volume, Lifespan.FILL_AND_KILL)


        elif bid_prices[0] > self.future_ask:
            # arbitrage, buy future and sell etf
            sell_volume = min(bid_volumes[0], self.position + ARBITRAGE_LIMIT)
            sell_price = bid_prices[0]

            if sell_volume > 0:
                self.send_ask_order(sell_price, sell_volume, Lifespan.FILL_AND_KILL)

    
    """
       Setup bid and ask order based on price of future
       bid: [future_bid - 3, future_bid - 2,... future_bid - 1]
       ask: [future_ask + 1, future_ask +2,...  future_ask + 3]
    """

    def handle_market_making(self, ask_prices : List[int], ask_volumes: List[int],
            bid_prices: List[int], bid_volumes: List[int]) -> None:

        max_buy_order = (POSITION_LIMIT - self.position) // LOT_SIZE - len(self.bids)
        max_sell_order = (self.position + POSITION_LIMIT) // LOT_SIZE - len(self.asks)
        left_bid = self.future_bid - 3 * TICK_SIZE_IN_CENTS
        right_ask = self.future_ask + 3 * TICK_SIZE_IN_CENTS

        for i in range(left_bid, left_bid + TICK_SIZE_IN_CENTS, TICK_SIZE_IN_CENTS):
            if i not in self.bids.values() and max_buy_order > 0:
                self.send_bid_order(i, LOT_SIZE)
                max_buy_order -= 1 

        for i in range(right_ask, right_ask + TICK_SIZE_IN_CENTS, TICK_SIZE_IN_CENTS):
            if i not in self.asks.values() and max_sell_order > 0:
                self.send_ask_order(i, LOT_SIZE)
                max_sell_order -= 1


        self.limit_book_size(3,3)

    def on_order_book_update_message(self, instrument: int, sequence_number: int, ask_prices: List[int],
                                     ask_volumes: List[int], bid_prices: List[int], bid_volumes: List[int]) -> None:
        """Called periodically to report the status of an order book.

        The sequence number can be used to detect missed or out-of-order
        messages. The five best available ask (i.e. sell) and bid (i.e. buy)
        prices are reported along with the volume available at each of those
        price levels.
        """

        # Discard old data
        self.msg_seq = max(self.msg_seq, sequence_number)
        if sequence_number != self.msg_seq:
            return

        # error data
        if bid_prices[0] == 0 or ask_prices[0] == 0:
            return

        print(f"Position: {self.position}, delta: {self.delta}, current speed {len(self.order_timestamps)}")

        if instrument == Instrument.ETF: 
            if ask_prices[0] < self.future_bid or bid_prices[0] > self.future_ask:
                print(f"Arbitrage, future at {self.future_bid} {self.future_ask}, etf at {bid_prices[0]} {ask_prices[0]}")
                self.handle_arbitrage(ask_prices, ask_volumes, bid_prices, bid_volumes)

            elif ask_prices[0] > self.future_ask and bid_prices[0] < self.future_bid:
                # set range for bid and ask and make the market
                # also need to cancel unnecessary orders
                # all orders 
                print(f"Market making, future at {self.future_bid} {self.future_ask}, etf at {bid_prices[0]} {ask_prices[0]}")
                self.handle_market_making(ask_prices, ask_volumes, bid_prices, bid_volumes)
                pass


        if instrument == Instrument.FUTURE:
            self.future_bid = bid_prices[0]
            self.future_ask = ask_prices[0]
            self.trim_orders()

    def on_order_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        """Called when one of your orders is filled, partially or fully.

        The price is the price at which the order was (partially) filled,
        which may be better than the order's limit price. The volume is
        the number of lots filled at that price.
        """

        self.logger.info("received order filled for order %d with price %d and volume %d", client_order_id,
                         price, volume)

        if client_order_id in self.bids:
            self.position += volume
            self.delta += volume
            self.send_hedge_order(MIN_BID_NEAREST_TICK, volume, Side.ASK)

        elif client_order_id in self.asks:
            self.position -= volume
            self.delta -= volume
            self.send_hedge_order(MAX_ASK_NEAREST_TICK, volume, Side.BID)

    def on_hedge_filled_message(self, client_order_id: int, price: int, volume: int) -> None:
        """Called when one of your hedge orders is filled.

        The price is the average price at which the order was (partially) filled,
        which may be better than the order's limit price. The volume is
        the number of lots filled at that price.
        """
        self.logger.info("received hedge filled for order %d with average price %d and volume %d", client_order_id,
                         price, volume)

        if client_order_id in self.hedge_bid:
            self.hedge_bid.remove(client_order_id)
            self.delta += volume

        elif client_order_id in self.hedge_ask:
            self.hedge_ask.remove(client_order_id)
            self.delta -= volume

        if self.delta > 0:
            self.send_hedge_order(MIN_BID_NEAREST_TICK, self.delta, Side.ASK)
        elif self.delta < 0:
            self.send_hedge_order(MAX_ASK_NEAREST_TICK, -self.delta, Side.BID)


    def on_order_status_message(self, client_order_id: int, fill_volume: int, remaining_volume: int,
                                fees: int) -> None:
        """Called when the status of one of your orders changes.

        The fill_volume is the number of lots already traded, remaining_volume
        is the number of lots yet to be traded and fees is the total fees for
        this order. Remember that you pay fees for being a market taker, but
        you receive fees for being a market maker, so fees can be negative.

        If an order is cancelled its remaining volume will be zero.
        """
        self.logger.info("received order status for order %d with fill volume %d remaining %d and fees %d",
                         client_order_id, fill_volume, remaining_volume, fees)

        if remaining_volume == 0:
            # It could be either a bid or an ask
            self.bids.pop(client_order_id, None)
            self.asks.pop(client_order_id, None)

    def on_trade_ticks_message(self, instrument: int, sequence_number: int, ask_prices: List[int],
                               ask_volumes: List[int], bid_prices: List[int], bid_volumes: List[int]) -> None:
        """Called periodically when there is trading activity on the market.

        The five best ask (i.e. sell) and bid (i.e. buy) prices at which there
        has been trading activity are reported along with the aggregated volume
        traded at each of those price levels.

        If there are less than five prices on a side, then zeros will appear at
        the end of both the prices and volumes arrays.
        """
        self.logger.info("received trade ticks for instrument %d with sequence number %d", instrument,
                         sequence_number)
